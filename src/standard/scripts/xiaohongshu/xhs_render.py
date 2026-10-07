"""Render the note in a real browser before SingleFile captures it.

Xiaohongshu gates app-shared notes for **anonymous sessions**: a client whose
cookie jar carries no session cookie is redirected away from the note (desktop
UAs land on the id-less ``/explore`` feed, Chrome-family UAs on ``/login``), so
the note DOM never appears. The gate is keyed on session cookies, and testing
shows a single valid ``web_session`` cookie is enough to unlock any note (``a1``
/ ``webId`` / ``websectiga`` are not required, and a fabricated ``web_session``
is rejected because the value is server-signed).

``web_session`` is only issued by the site's own JS when a page such as
``https://www.xiaohongshu.com/`` loads, so it cannot be constructed locally.
Each hook run gets a *fresh* Playwright context (``launch_browser`` returns a
plain ``Browser``; cookies never survive between runs), therefore the hook:

1. renders with the configured desktop UA, seeding the cached ``web_session``
   if one is stored — this is the steady-state path and costs a single page load;
2. when that pass is still gated (no cache / expired cache), warms a session by
   loading ``https://www.xiaohongshu.com/`` in the same context, captures the
   resulting cookies into the framework's encrypted shared-cookie store under
   ``www.xiaohongshu.com``, and renders once more — self-healing every time;
3. only if the desktop session still fails does it fall back to a mobile UA
   (mobile is never gated and lands on ``/discovery/item/<id>``).

The cached cookie is stored against ``www.xiaohongshu.com`` (not the alias
domain ``xhslink.cn``) so that both the encryption layer's domain filter and the
injected cookie's scope resolve to ``.xiaohongshu.com``.

The declarative snapshot rules in ``adapters.jsonc`` list selectors for both the
desktop (``#noteContainer``) and the mobile (``.narmal-note-container``) layouts,
so SingleFile cleans up either DOM.

Whether a note is gated is only known after the request, so this decision cannot
be expressed with static config fields or URL routes and lives in the hook.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Note pages carry an id segment; the gated home feed does not.
NOTE_URL_RE = re.compile(r"/(?:discovery/item|explore)/[0-9a-fA-F]{8,}")

# Loading this page lets the site's JS establish the anonymous session cookie
# (web_session) that unlocks app-shared notes.
WARMUP_URL = "https://www.xiaohongshu.com/"

# The session cookie belongs to xiaohongshu.com, not the alias domain
# xhslink.cn the bookmark is stored under. Persisting/injecting it against this
# host makes the derived cookie domain resolve to ".xiaohongshu.com".
SESSION_DOMAIN = "www.xiaohongshu.com"

MOBILE_SELECTOR = ".narmal-note-container"

DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_VIEWPORT = {"width": 390, "height": 844}

DEFAULT_DESKTOP_UA = (
    # 小红书对 Chrome 系 UA 强制跳转登录页，仅放行 Safari/Firefox；
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_8_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15"
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _reached_note(page):
    return bool(NOTE_URL_RE.search(page.url))


def _load_cached_session():
    """Return the cached anonymous session cookie string, or '' when absent."""
    try:
        from site_adapters.services.auth.credentials import get_shared_cookie

        cookie_str, _ = get_shared_cookie(hostname=SESSION_DOMAIN, source="auto")
        return cookie_str or ""
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        logger.debug("xhs_render: session cache read failed: %s", exc)
        return ""


def _store_session(cookie_str):
    """Persist the freshly captured session so later runs skip the warm-up."""
    if not cookie_str:
        return
    try:
        from site_adapters.services.auth.credentials import save_shared_cookie

        save_shared_cookie(domain=SESSION_DOMAIN, cookie_str=cookie_str, source="auto")
        logger.info("xhs_render: cached anonymous session for %s", SESSION_DOMAIN)
    except Exception as exc:  # noqa: BLE001 - caching must never break the snapshot
        logger.warning("xhs_render: session cache write failed: %s", exc)


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
    """Render the note, reusing a cached session and warming up only on a miss."""
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
    cached_session = "" if cookie_str else _load_cached_session()
    captured = {"value": ""}

    browser = launch_browser(headless=True)
    contexts = []
    playwright = getattr(browser, "__playwright__", None)

    def open_page(user_agent, viewport, warm=False, seed=True):
        context = browser.new_context(user_agent=user_agent, viewport=viewport)
        contexts.append(context)

        # An explicit user cookie always wins; otherwise seed the cached session.
        if cookie_str:
            seed_str, seed_host = cookie_str, domain
        elif seed and cached_session:
            seed_str, seed_host = cached_session, SESSION_DOMAIN
        else:
            seed_str, seed_host = "", ""
        if seed_str:
            cookies = cookie_string_to_playwright_list(seed_str, seed_host)
            if cookies:
                context.add_cookies(cookies)

        page = context.new_page()
        if warm:
            # Establish the anonymous session cookie (web_session) that unlocks
            # app-shared notes; without it the note request is gated.
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                jar = context.cookies([WARMUP_URL])
                captured["value"] = "; ".join(
                    f"{c.get('name')}={c.get('value')}"
                    for c in jar
                    if c.get("name")
                )
            except Exception:
                pass
        page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        return page

    try:
        # 1) Steady state: trusted cached session (or explicit user cookie).
        page = open_page(desktop_ua, DESKTOP_VIEWPORT)
        if _reached_note(page):
            logger.debug("xhs_render: rendered note with cached session")
            _settle(page, mobile=False)
        else:
            # 2) Cache miss / expired: warm up, capture, cache, retry once.
            logger.info("xhs_render: no usable session for %s, warming up", target_url)
            page = open_page(desktop_ua, DESKTOP_VIEWPORT, warm=True, seed=False)
            if _reached_note(page):
                _store_session(captured["value"])
                _settle(page, mobile=False)
            else:
                # 3) Last resort: a mobile UA is never gated.
                logger.warning("xhs_render: desktop session still gated, using mobile UA")
                page = open_page(MOBILE_UA, MOBILE_VIEWPORT, seed=False)
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
