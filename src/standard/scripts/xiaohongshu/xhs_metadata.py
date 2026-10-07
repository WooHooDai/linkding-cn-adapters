"""Xiaohongshu metadata fallback for notes that desktop web is not allowed to see.

Xiaohongshu serves a note's SSR markup (og:title / og:description / og:image)
only to desktop user agents. Part of the notes — typically ones shared from the
app — are additionally gated per user agent: a desktop UA is redirected to
``/explore?...&undertake_note_error=该内容暂时无法查看`` (or ``/login`` for
Chrome-family UAs), so the built-in engine only finds the site homepage
metadata ("小红书 - 你的生活兴趣社区"). A mobile UA always reaches the note, but
that response is an SPA shell with no og: tags — the note payload is shipped in
``window.__INITIAL_STATE__`` instead.

This ``after`` hook leaves healthy results untouched and only re-fetches with a
mobile UA when the desktop pass came back with the gated homepage metadata.
"""
import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Titles the gate falls back to. The built-in rewrite_title rule strips a
# trailing " - 小红书", which does not match these, so they survive untouched.
GATED_TITLES = {
    "小红书 - 你的生活兴趣社区",
    "小红书",
    "小红书 - 你访问的页面不见了",
}

_TOPIC_TAG_RE = re.compile(r"\[话题\]#")
_STATE_MARKER = "window.__INITIAL_STATE__"
_INITIAL_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*", re.DOTALL
)


def _strip_undefined(raw):
    """Replace JS ``undefined`` literals with ``null`` outside of strings."""
    out = []
    in_string = False
    escaped = False
    i = 0
    length = len(raw)
    while i < length:
        char = raw[i]
        if escaped:
            out.append(char)
            escaped = False
            i += 1
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            i += 1
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            i += 1
            continue
        if not in_string and raw.startswith("undefined", i):
            out.append("null")
            i += len("undefined")
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _parse_initial_state(html):
    """Extract ``window.__INITIAL_STATE__`` as a dict (best effort)."""
    match = _INITIAL_STATE_RE.search(html)
    if not match:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(
            _strip_undefined(html[match.end():].lstrip())
        )
    except (ValueError, TypeError) as exc:
        logger.debug("xhs_metadata: initial state parse failed: %s", exc)
        return None
    return state if isinstance(state, dict) else None


def _image_from_preload(preload):
    images = preload.get("imagesList") or []
    if not isinstance(images, list):
        return None
    for item in images:
        if not isinstance(item, dict):
            continue
        url = item.get("urlSizeLarge") or item.get("url") or item.get("urlPre")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def _image_from_note_data(note):
    """Fall back to the note's own imageList when preload carries no image."""
    for entry in note.get("imageList") or []:
        if not isinstance(entry, dict):
            continue
        for info in entry.get("infoList") or []:
            if not isinstance(info, dict):
                continue
            url = info.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None


def _note_from_state(state):
    """Pull (title, description, image) out of the parsed initial state."""
    note_data = state.get("noteData")
    if not isinstance(note_data, dict):
        return None, None, None

    preload = note_data.get("normalNotePreloadData")
    preload = preload if isinstance(preload, dict) else {}
    data = note_data.get("data")
    data = data if isinstance(data, dict) else {}
    note = data.get("noteData")
    note = note if isinstance(note, dict) else {}

    title = preload.get("title") or note.get("title")
    description = preload.get("desc") or note.get("desc")
    image = _image_from_preload(preload) or _image_from_note_data(note)

    if isinstance(title, str):
        title = title.strip() or None
    if isinstance(description, str):
        # Mobile payload marks hashtags as "#tag[话题]#"; desktop og:description
        # does not, so normalise to keep both paths consistent.
        description = _TOPIC_TAG_RE.sub("", description)
        description = " ".join(description.split()).strip() or None
    return title, description, image


def _fetch_mobile(url, config):
    headers = dict(config.get("headers") or {})
    headers["User-Agent"] = MOBILE_UA
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9")
    cookie = config.get("user_cookie") or config.get("_user_cookie")
    if cookie:
        headers["Cookie"] = cookie

    timeout = config.get("timeout") or 30
    proxies = None
    proxy = config.get("proxy")
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    response = requests.get(
        url, headers=headers, timeout=timeout, allow_redirects=True, proxies=proxies
    )
    response.raise_for_status()
    return response.text


def after(result, url, config):
    """Fill in metadata when the desktop pass hit the mobile-only gate."""
    if not isinstance(result, dict):
        return

    title = result.get("title")
    if title not in GATED_TITLES:
        return

    target = (
        config.get("_request_url")
        or config.get("request_url")
        or url
    )
    try:
        html = _fetch_mobile(target, config)
    except Exception as exc:  # noqa: BLE001 - fallback must never break saving
        logger.warning("xhs_metadata: mobile fallback failed for %s: %s", url, exc)
        return

    if _STATE_MARKER not in html:
        return

    note_title, description, image = _note_from_state(_parse_initial_state(html) or {})
    if not note_title:
        return

    result["title"] = note_title
    if description:
        result["description"] = description
    if image:
        result["image"] = image
    logger.info(
        "xhs_metadata: recovered gated note via mobile UA: %s", (note_title or "")[:40]
    )
