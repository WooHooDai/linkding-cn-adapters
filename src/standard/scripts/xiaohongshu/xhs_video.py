"""Embed the note video into the saved snapshot.

``xhs.js`` writes a ``<video>`` whose ``src`` is the page's ``og:video`` URL —
a signed CDN link that expires a few days after capture (the ``t`` query
parameter is the expiry timestamp). Keeping that URL makes the snapshot playable
only until it goes stale, after which the CDN answers 403 and the player stays
black.

This ``after`` hook runs on the saved HTML — after the JS hook injected the
element, and after SingleFile has finished embedding resources, which is why
``--block-videos`` cannot help here — and replaces the URL with the media bytes
as a data URI, so the snapshot plays offline for good.
"""

import base64
import logging
import re

import requests

logger = logging.getLogger(__name__)

# Mirrors the engine's own --max-resource-size default: past this we keep the
# remote URL instead of turning the snapshot into a multi-megabyte file.
MAX_INLINE_BYTES = 10 * 1024 * 1024

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) "
    "Gecko/20100101 Firefox/143.0"
)

_VIDEO_SRC_RE = re.compile(r'<video\b[^>]*?\bsrc="(?P<src>https://[^"]+)"')


def _headers(config):
    """Adapter headers with the UA normalised, so exactly one survives."""
    headers = dict(config.get("headers") or {})
    for key in list(headers):
        if key.lower() == "user-agent":
            headers["User-Agent"] = headers.pop(key)
            break
    headers.setdefault("User-Agent", DEFAULT_UA)
    return headers


def _fetch(url, config):
    """Return (bytes, content_type) or None when the media must be left remote."""
    timeout = min(int(config.get("timeout") or 30), 30)
    proxies = None
    proxy = config.get("proxy")
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    try:
        response = requests.get(
            url,
            headers=_headers(config),
            timeout=timeout,
            stream=True,
            proxies=proxies,
        )
        with response:
            response.raise_for_status()
            length = response.headers.get("Content-Length") or ""
            if length.isdigit() and int(length) > MAX_INLINE_BYTES:
                logger.info(
                    "xhs_video: skipping %s KB video (over the inline limit)",
                    int(length) // 1024,
                )
                return None

            content_type = (response.headers.get("Content-Type") or "").split(";")[0]
            if not content_type.startswith("video/"):
                logger.info("xhs_video: unexpected content type %r", content_type)
                return None

            chunks = []
            size = 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_INLINE_BYTES:
                    logger.info("xhs_video: aborting oversized video download")
                    return None
                chunks.append(chunk)
    except Exception as exc:  # noqa: BLE001 - fallback must never break saving
        logger.warning("xhs_video: video download failed for %s: %s", url, exc)
        return None

    return b"".join(chunks), content_type


def after(output_path, config):
    """Inline the injected video element's remote src into the saved HTML."""
    config = config or {}
    try:
        with open(output_path, encoding="utf-8") as f:
            html = f.read()
    except OSError as exc:
        logger.warning("xhs_video: cannot read snapshot %s: %s", output_path, exc)
        return

    match = _VIDEO_SRC_RE.search(html)
    if not match:
        return

    fetched = _fetch(match.group("src"), config)
    if not fetched:
        return

    body, content_type = fetched
    data_uri = "data:{};base64,{}".format(
        content_type,
        base64.b64encode(body).decode("ascii"),
    )
    html = html[: match.start("src")] + data_uri + html[match.end("src") :]

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as exc:
        logger.warning("xhs_video: cannot write snapshot %s: %s", output_path, exc)
        return

    logger.info("xhs_video: embedded %d KB video into the snapshot", len(body) // 1024)
