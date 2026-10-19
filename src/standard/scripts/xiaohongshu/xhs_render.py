"""Render the note in a real browser before SingleFile captures it.

Xiaohongshu gates anonymous note views by **UA family**, not by session cookie.
Measured on 2026-10: a Gecko/Firefox UA renders the note with no session at
all; Chrome/Edge UAs are redirected to ``/login``; Safari-family UAs (desktop,
iOS, Android) are redirected to ``/website-login/captcha?verifyType=217``, a
slider challenge. The decision is made server-side after the page's risk SDK
posts its device profile to ``as.xiaohongshu.com/api/sec/v1/shield/webprofile``
and is not influenced by headless mode, a valid ``web_session``, or how
consistent the UA is with the engine's own fingerprint (masking
``sec-ch-ua``/``navigator.vendor`` while keeping a Safari UA still yields the
slider).

A ``web_session`` therefore buys nothing, and it cannot be warmed up either:
the landing page ``https://www.xiaohongshu.com/`` is gated by the same rule,
so the old warm-up-and-cache flow only added risk-scored requests. The hook
now renders once with the configured Firefox UA — the only anonymous door
left — and keeps a mobile UA as a last resort in case that changes back.

The declarative snapshot rules in ``adapters.jsonc`` list selectors for both the
desktop (``#noteContainer``) and the mobile (``.narmal-note-container``) layouts,
so SingleFile cleans up either DOM. The desktop layout also mounts a login
modal (``.reds-modal`` wrapping ``.login-container``, with its own
``i.reds-mask`` backdrop) over the note; those selectors are already removed
declaratively, and dropping ``.reds-modal`` takes the mask with it.

Whether a note is gated is only known after the request, so this decision cannot
be expressed with static config fields or URL routes and lives in the hook.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Note pages carry an id segment; the gated home feed does not.
NOTE_URL_RE = re.compile(r"/(?:discovery/item|explore)/[0-9a-fA-F]{8,}")

MOBILE_SELECTOR = ".narmal-note-container"

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}

DEFAULT_DESKTOP_UA = (
    # 小红书按 UA 家族分流：Safari 系直接吃滑块验证，Chrome 系跳登录墙，
    # 只有 Gecko 能匿名读到笔记。
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) "
    "Gecko/20100101 Firefox/143.0"
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _reached_note(page):
    return bool(NOTE_URL_RE.search(page.url))


def _settle(page, mobile):
    """Wait for the note to render, then nudge lazy media on the desktop layout.

    The mobile note page must not be scrolled: scrolling triggers the
    ``oia.xiaohongshu.com`` app-open deep link and replaces the note with an
    "打开小红书" interstitial, so the scroll step is desktop-only.
    """
    if mobile:
        try:
            # The mobile SPA renders the note client-side.
            page.wait_for_selector(MOBILE_SELECTOR, timeout=15000)
        except Exception:
            pass
        return

    try:
        # Let the page's own scripts produce the signed comment API request.
        # SingleFile still captures the returned DOM with scripts blocked.
        page.wait_for_function(
            """() => {
                const root = document.querySelector(".comments-el");
                return root && !root.querySelector(".loading");
            }""",
            timeout=15000,
        )
    except Exception:
        pass

    try:
        page.evaluate(
            """async () => {
                window.scrollTo(0, document.body.scrollHeight);
                await new Promise((resolve) => setTimeout(resolve, 300));
                window.scrollTo(0, 0);
            }"""
        )
    except Exception:
        # The SPA may navigate mid-script; the captured DOM is still usable.
        pass


def _flatten_overlay(page):
    """Repaint the note overlay's backdrop so the snapshot reads as white.

    A gated note lands on ``/explore/<id>``, where the note is rendered as an
    overlay wrapped in ``.note-detail-mask`` — a full-viewport ``position:fixed``
    backdrop painted with ``var(--mask-backdrop)`` (``rgba(0,0,0,.25)``), which
    tints the whole snapshot grey. ``#noteContainer`` is *inside* that mask, so
    the declarative ``keep_elements``/``remove_elements`` rules cannot drop it
    (removing the mask would remove the note). The standalone ``/discovery/item``
    layout is not reachable either — the site redirects it back to ``/explore``.
    So the backdrop is repainted white via an inline ``!important`` style on the
    element itself, which beats its own ``background: var(--mask-backdrop)`` and
    survives SingleFile's style handling (an appended ``<style>`` tag does not).
    """
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('.note-detail-mask').forEach((el) => {
                    el.style.setProperty('background', '#fff', 'important');
                });
            }"""
        )
    except Exception:
        pass


def _capture(page, attempts=6):
    """Grab the rendered HTML, tolerating transient same-document navigations.

    The xiaohongshu SPA repeatedly issues history navigations for the same URL,
    which makes ``page.content()`` intermittently raise "page is navigating".
    """
    last_error = None
    for _ in range(attempts):
        try:
            return page.content()
        except Exception as exc:  # noqa: BLE001 - retry, then surface if truly stuck
            last_error = exc
            try:
                page.wait_for_timeout(700)
            except Exception:
                pass
    raise last_error


def before(url, config):
    """Render the note with the Firefox UA; mobile is only a last resort."""
    from urllib.parse import urlparse

    from site_adapters.services.engine.browser_provider import launch_browser
    from site_adapters.services.auth.cookies import cookie_string_to_playwright_list

    target_url = config.get("request_url") or url
    timeout_ms = int((config.get("timeout") or 30) * 1000)

    headers = config.get("headers") or {}
    desktop_ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or DEFAULT_DESKTOP_UA
    )

    cookie_str = config.get("user_cookie") or ""
    domain = urlparse(target_url).hostname or ""

    browser = launch_browser(headless=True)
    contexts = []
    playwright = getattr(browser, "__playwright__", None)

    def open_page(user_agent, viewport):
        context = browser.new_context(user_agent=user_agent, viewport=viewport)
        contexts.append(context)
        if cookie_str:
            cookies = cookie_string_to_playwright_list(cookie_str, domain)
            if cookies:
                context.add_cookies(cookies)
        page = context.new_page()
        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        return page

    try:
        page = open_page(desktop_ua, DESKTOP_VIEWPORT)
        if _reached_note(page):
            _settle(page, mobile=False)
        else:
            # Gecko is the only anonymous door today; a mobile UA stays as a
            # fallback in case the site re-opens that route.
            logger.warning(
                "xhs_render: desktop UA gated (%s), falling back to mobile UA",
                page.url,
            )
            page = open_page(MOBILE_UA, MOBILE_VIEWPORT)
            if not _reached_note(page):
                raise RuntimeError(
                    f"xiaohongshu gated both desktop and mobile: {page.url}"
                )
            _settle(page, mobile=True)
        _flatten_overlay(page)
        return _capture(page)
    finally:
        for context in contexts:
            try:
                context.close()
            except Exception:
                pass
        try:
            browser.close()
        except Exception:
            pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass
