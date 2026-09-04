"""Feishu document metadata extraction from SSR window.DATA."""
import json
import re
from urllib.parse import quote, unquote

import requests


GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)
_IMAGE_PREVIEW = (
    "https://internal-api-drive-stream.feishu.cn/space/api/box/stream/"
    "download/preview/{}/?preview_type=16"
)
_DESCRIPTION_MAX_CHARS = 300
_DESCRIPTION_MAX_WORDS = 300
_GALLERY_ENTRY_RE = re.compile(r'"gallery"\s*,\s*"((?:\\.|[^"\\])*)"')
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _fetch(url, config):
    session = requests.Session()
    headers = dict(config.get("headers") or {})
    headers.setdefault("User-Agent", GOOGLEBOT_UA)
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    session.headers.update(headers)
    cookie = config.get("_user_cookie")
    if cookie:
        session.headers["Cookie"] = cookie
    timeout = config.get("timeout") or 30
    proxy = config.get("proxy")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    response = session.get(
        config.get("_request_url") or url,
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def _balanced_json(html, start):
    """Extract a balanced JSON value beginning with ``{`` or ``[`` at start."""
    if start >= len(html):
        return None
    open_char = html[start]
    close_char = {"{": "}", "[": "]"}.get(open_char)
    if close_char is None:
        return None
    depth = 1
    i = start + 1
    in_string = False
    escaped = False
    while i < len(html) and depth:
        char = html[i]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char == open_char:
                depth += 1
            elif char == close_char:
                depth -= 1
        i += 1
    if depth:
        return None
    try:
        return json.loads(html[start:i])
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_initial_text(html):
    match = re.search(r'"initialAttributedText"\s*:\s*\{', html)
    if not match:
        return None
    data = _balanced_json(html, match.end() - 1)
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _image_url_from_item(item):
    if not isinstance(item, dict):
        return None
    token = item.get("file_token") or item.get("token")
    if token:
        return _IMAGE_PREVIEW.format(quote(str(token), safe=""))
    src = item.get("src")
    if isinstance(src, str) and src:
        src = unquote(src)
        if src.startswith(("http://", "https://")):
            return src
    return None


def _image_url_from_block(data):
    if not isinstance(data, dict):
        return None
    if data.get("type") != "image":
        return None
    image = data.get("image") or {}
    if not isinstance(image, dict):
        return None
    token = image.get("token") or image.get("file_token")
    if token:
        return _IMAGE_PREVIEW.format(quote(str(token), safe=""))
    src = image.get("src")
    if isinstance(src, str) and src:
        src = unquote(src)
        if src.startswith(("http://", "https://")):
            return src
    return None


def _first_gallery_image_url(html):
    for match in _GALLERY_ENTRY_RE.finditer(html):
        try:
            gallery_json = json.loads(f'"{match.group(1)}"')
            gallery = json.loads(gallery_json)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        items = gallery.get("items") if isinstance(gallery, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            image_url = _image_url_from_item(item)
            if image_url:
                return image_url
    return None


def _first_resources_image_url(html):
    match = re.search(r'"images"\s*:\s*\[', html)
    if not match:
        return None
    images = _balanced_json(html, match.end() - 1)
    if not isinstance(images, list):
        return None
    for item in images:
        image_url = _image_url_from_item(item)
        if image_url:
            return image_url
    return None


def _first_block_map_image_url(html):
    block_map_match = re.search(r'"block_map"\s*:\s*\{', html)
    if not block_map_match:
        return None
    block_map = _balanced_json(html, block_map_match.end() - 1)
    if not isinstance(block_map, dict):
        return None

    sequence_match = re.search(r'"block_sequence"\s*:\s*\[', html)
    if sequence_match:
        sequence = _balanced_json(html, sequence_match.end() - 1)
        if isinstance(sequence, list):
            for block_id in sequence:
                if not isinstance(block_id, str):
                    continue
                block = block_map.get(block_id)
                image_url = _image_url_from_block(
                    block.get("data") if isinstance(block, dict) else None
                )
                if image_url:
                    return image_url

    for block in block_map.values():
        image_url = _image_url_from_block(
            block.get("data") if isinstance(block, dict) else None
        )
        if image_url:
            return image_url
    return None


def _format_description(text, title):
    text = " ".join(text.split())
    if title and text.startswith(title):
        text = text[len(title):].lstrip(" ,.:;。！？：；")
    if not text:
        return None
    words = text.split()
    if not _CJK_RE.search(text) and len(words) > _DESCRIPTION_MAX_WORDS:
        return " ".join(words[:_DESCRIPTION_MAX_WORDS]).strip()
    return text[:_DESCRIPTION_MAX_CHARS].strip() or None


def _meta_fallback(html):
    title = None
    description = None
    image = None
    title_match = re.search(
        r'<meta\s+property="og:title"\s+content="([^"]*)"', html
    )
    if title_match:
        title = title_match.group(1)
    desc_match = re.search(
        r'<meta\s+property="og:description"\s+content="([^"]*)"', html
    )
    if desc_match:
        description = desc_match.group(1)
    image_match = re.search(
        r'<meta\s+property="og:image"\s+content="([^"]*)"', html
    )
    if image_match:
        image = image_match.group(1)
    if image and "feishu.ico" in image:
        image = None
    return title, description, image


def replace(url, config):
    html = _fetch(url, config)
    title, description, image = _meta_fallback(html)

    text = _extract_initial_text(html)
    if text:
        description = _format_description(text, title)

    image = (
        _first_gallery_image_url(html)
        or _first_resources_image_url(html)
        or _first_block_map_image_url(html)
        or image
    )
    return {"title": title, "description": description, "image": image, "url": url}
