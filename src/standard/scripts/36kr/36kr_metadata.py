"""36kr metadata extraction.

36kr.com serves a Volcengine (火山引擎) JS security challenge page for
plain HTTP requests. The built-in use_browser engine with `networkidle` can
trigger prematurely during the challenge pause, yielding the interstitial
HTML instead of the real article.

This replace hook launches a browser, waits for networkidle (which gives the
challenge time to resolve), then uses wait_for_function to confirm og:title
is present before extracting page HTML for the built-in parser.
"""
import logging

from site_adapters.services.engine.browser_provider import launch_browser
from site_adapters.services.engine import parse_metadata

logger = logging.getLogger(__name__)

_OG_TITLE_WAIT = (
    "() => { const el = document.querySelector(\"meta[property='og:title']\"); "
    "return el && el.getAttribute('content'); }"
)


def replace(url, config):
    browser = None
    playwright = None
    try:
        browser = launch_browser(headless=True)
        playwright = getattr(browser, "__playwright__", None)
        context = browser.new_context()
        page = context.new_page()
        # networkidle lets the Volcengine challenge JS finish before we poll
        page.goto(url, wait_until="networkidle", timeout=30000)
        try:
            page.wait_for_function(_OG_TITLE_WAIT, timeout=15000)
        except Exception:
            logger.warning("36kr: og:title not found within timeout. url=%s", url)
            return {"title": None, "description": None, "image": None, "url": url}
        html = page.content()
    except Exception as exc:
        logger.warning("36kr: browser load failed. url=%s: %s", url, exc)
        return {"title": None, "description": None, "image": None, "url": url}
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright:
            try:
                playwright.stop()
            except Exception:
                pass

    return parse_metadata(html, url, config)
