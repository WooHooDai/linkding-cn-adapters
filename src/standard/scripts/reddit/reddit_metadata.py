"""Reddit metadata extraction.

Reddit's .json endpoint returns 403 for requests without the loid cookie.
loid is a long-lived (~400 days) cookie set by Reddit's JS on first visit.

This script uses the framework's auth.cookie auto system:
  - On a warm run, the config already carries user_cookie (from the
    credential store) and we extract loid from it -- a plain requests.get
    completes in ~1-2s, no browser involved.
  - On a cold run (no cookie yet), we call verify_and_refresh which
    launches a headless browser, visits Reddit, waits for the loid
    cookie, and saves it to the credential store.  Future runs are warm.
  - If everything fails, a full browser navigation to the .json URL is
    used as a last-resort fallback.
"""
import json
import logging
import os
import re
import time

import requests

logger = logging.getLogger(__name__)


def _extract_loid(cookie_str):
    """Extract the loid value from a raw cookie header string."""
    if not cookie_str:
        return None
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if pair.startswith("loid="):
            val = pair[len("loid="):]
            if val:
                return val
    return None


def _get_loid_from_config(config):
    """Return the loid cookie value, triggering a browser refresh on cold start.

    Priority:
      1. config["user_cookie"] -- already populated by the framework's
         _build_section_config from the credential store (warm path).
      2. If absent and cookie.type == "auto", call verify_and_refresh
         which launches a browser, waits for loid, saves it to the
         credential store, and returns the cookie string (cold path).
    """
    # Warm path: cookie already in config from credential store
    loid = _extract_loid(config.get("user_cookie"))
    if loid:
        return loid

    # Cold path: trigger framework's auto cookie refresh
    cookie_config = config.get("cookie") or {}
    if not isinstance(cookie_config, dict) or cookie_config.get("type") != "auto":
        return None

    from site_adapters.services.auth.cookies import verify_and_refresh

    domain_key = config.get("domain_key", "www.reddit.com")
    refresh_url = cookie_config.get("refresh", {}).get("url", "https://www.reddit.com")

    cookie_str = verify_and_refresh(
        cookie_config=cookie_config,
        url=refresh_url,
        domain_key=domain_key,
        verify_context={
            "url": refresh_url,
            "status": 0,
            "title": "",
            "body_preview": "",
        },
        username="",
        scope="",
    )
    return _extract_loid(cookie_str)


def _compute_429_wait(response, attempt, freq_backoff):
    """Compute the optimal wait (seconds) after a 429 based on response headers.

    Priority:
      1. Retry-After header (standard HTTP) — use directly
      2. X-Ratelimit-Remaining=0 — quota exhausted, returns 0 (give up)
      3. X-Ratelimit-Remaining>0 — frequency limit only, use short backoff
      4. No rate-limit headers — use short backoff
    """
    # 1. Retry-After (seconds or HTTP-date)
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), 1)
        except ValueError:
            pass  # HTTP-date format not handled; fall through

    # 2-3. X-Ratelimit-* (Reddit-specific)
    remaining = response.headers.get("X-Ratelimit-Remaining")
    reset = response.headers.get("X-Ratelimit-Reset")
    if remaining is not None and reset is not None:
        try:
            rem = float(remaining)
            if rem <= 0:
                # Quota exhausted — reset window is minutes away, not worth
                # waiting.  Signal the caller to give up by returning 0.
                return 0
            # Frequency limit only — short backoff
            return freq_backoff[min(attempt, len(freq_backoff) - 1)]
        except ValueError:
            pass

    # 4. Fallback
    return freq_backoff[min(attempt, len(freq_backoff) - 1)]


def _fetch_json_requests(json_url, loid, timeout):
    """Fetch Reddit .json via standard requests with the loid cookie.

    Retries on 429 using response headers to compute wait.  Returns (data, status):
      - (parsed_json, 200) on success
      - (None, 429) on rate-limited — caller should NOT use browser fallback
      - (None, other_status) on other errors — browser fallback is OK

    Wait strategy (first match wins):
      1. Retry-After header — standard, use directly
      2. X-Ratelimit-Remaining=0 — quota exhausted, give up immediately
      3. X-Ratelimit-Remaining>0 — frequency limit only, short backoff [2, 3, 5]
      4. No headers — fallback to [2, 3, 5]
    """
    max_retries = 3
    # Measured recovery for frequency-limit 429s: ~50% in 1s, ~95% in 3s, 100% in 8s
    freq_backoff = [2, 3, 5]

    for attempt in range(max_retries + 1):
        try:
            r = requests.get(json_url, cookies={"loid": loid}, timeout=timeout)
            if r.status_code == 200:
                return json.loads(r.text), 200
            if r.status_code == 429 and attempt < max_retries:
                delay = _compute_429_wait(r, attempt, freq_backoff)
                if delay == 0:
                    logger.warning("reddit_metadata: quota exhausted (remaining=0), not retrying")
                    return None, 429
                logger.info("reddit_metadata: 429 on attempt %d, retrying in %.1fs", attempt + 1, delay)
                time.sleep(delay)
                continue
            logger.info("reddit_metadata: requests got %s for %s", r.status_code, json_url)
            return None, r.status_code
        except Exception as e:
            logger.warning("reddit_metadata: requests error: %s", e)
            return None, 0
    # Exhausted all retries on 429
    return None, 429


def _fetch_json_browser(json_url, config, timeout):
    """Last-resort fallback: load .json via persistent Chromium browser."""
    try:
        from django.conf import settings
        from playwright.sync_api import sync_playwright
        from site_adapters.services.engine.browser_provider import _find_chromium_path
    except ImportError as e:
        logger.error("reddit_metadata: browser fallback dependency missing: %s", e)
        return None

    engine = getattr(settings, "LD_BROWSER_ENGINE", "chromium")
    profile_dir = config.get("profile_dir") or os.path.join(
        os.path.abspath(os.getcwd()), "chromium-profile"
    )

    exec_path = None
    try:
        exec_path = _find_chromium_path()
    except FileNotFoundError:
        pass

    html_url = re.sub(r"/\.json$", "", json_url)

    pw = None
    context = None
    try:
        pw = sync_playwright().start()
        if engine == "cloakbrowser":
            from cloakbrowser import launch
            browser = launch(headless=True)
            context = browser.new_context()
        else:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                executable_path=exec_path,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
        page = context.new_page()
        # Warmup: load HTML to establish session cookies
        try:
            page.goto(html_url, timeout=timeout * 1000, wait_until="domcontentloaded")
            time.sleep(1)
        except Exception as e:
            logger.warning("reddit_metadata: warmup failed for %s: %s", html_url, e)
        # Load .json
        resp = page.goto(json_url, timeout=timeout * 1000, wait_until="networkidle")
        if resp and resp.status == 200:
            text = page.inner_text("body")
            return json.loads(text)
        logger.error(
            "reddit_metadata: browser .json status %s",
            resp.status if resp else 0,
        )
    except Exception as e:
        logger.error("reddit_metadata: browser fallback failed: %s", e)
    finally:
        if context:
            try:
                context.close()
            except Exception:
                pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
    return None


def _extract_field(data, paths):
    """Extract a value from JSON data using JSONPath selectors."""
    from jsonpath_rfc9535 import find as jsonpath_find

    if isinstance(paths, str):
        paths = [paths]
    for path in paths or []:
        if not path or not path.strip():
            continue
        try:
            normalized = path.strip()
            if normalized.startswith("["):
                normalized = "$" + normalized
            elif not normalized.startswith("$"):
                normalized = "$." + normalized
            nodes = jsonpath_find(normalized, data)
            for node in nodes:
                val = node.value
                if isinstance(val, str):
                    val = val.strip()
                    if val:
                        return val
                elif val is not None:
                    return str(val)
        except Exception:
            continue
    return None


def replace(url, config):
    """Fetch Reddit .json and extract metadata.

    Warm path: requests + loid from credential store (~1-2s, no browser).
    Cold path: verify_and_refresh launches browser once, saves loid (~3-5s).
    Fallback: persistent Chromium browser with warmup (~8-10s).
    """
    empty = {"title": None, "description": None, "image": None, "url": url}

    # request_url is normally the resolved .json URL string, but guard
    # against the raw [pattern, replacement] list slipping through.
    json_url = config.get("request_url") or url
    if isinstance(json_url, (list, tuple)):
        json_url = url
    if not json_url.rstrip("/").endswith(".json"):
        json_url = json_url.rstrip("/") + "/.json"

    timeout = config.get("timeout", 30) or 30

    # --- Fast path: requests + loid cookie ---
    loid = _get_loid_from_config(config)
    if loid:
        data, status = _fetch_json_requests(json_url, loid, timeout)
        if data is not None:
            logger.info("reddit_metadata: requests success for %s", json_url)
            return _build_result(data, config, url, json_url)
        # 429 = rate limited; browser would also be rate-limited, so skip it
        if status == 429:
            logger.warning("reddit_metadata: rate-limited (429) for %s, skipping browser fallback", json_url)
            return empty

    # --- Fallback: browser ---
    logger.info("reddit_metadata: falling back to browser for %s", json_url)
    data = _fetch_json_browser(json_url, config, timeout)
    if data is None:
        return empty

    return _build_result(data, config, url, json_url)


def _build_result(data, config, url, json_url):
    """Extract title/description/image from JSON data and build result dict."""
    title = _extract_field(data, config.get("select_title"))
    description = _extract_field(data, config.get("select_description"))
    image = _extract_field(data, config.get("select_image"))

    # Filter out Reddit's placeholder thumbnail values (self, default, nsfw,
    # spoiler) that have no corresponding image.
    if image in ("self", "default", "nsfw", "spoiler"):
        image = None

    logger.info(
        "reddit_metadata: title=%s, image=%s from %s",
        title[:50] if title else None,
        image[:50] if image else None,
        json_url,
    )

    return {"title": title, "description": description, "image": image, "url": url}
