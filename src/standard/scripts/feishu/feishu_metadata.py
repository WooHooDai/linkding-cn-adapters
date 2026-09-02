"""Feishu document metadata extraction via pure HTTP (no browser).

Feishu is a SPA. Normal browser UA gets an empty shell HTML. However, the
server returns a fully server-side-rendered page (including og:title,
og:description, og:image, and the complete block_map with 200+ blocks) when
the User-Agent looks like a search-engine crawler.

Strategy:
  1. Fetch the document URL with a Googlebot User-Agent.
  2. Parse og:title / og:description / og:image from <meta> tags.
  3. If og:image is the generic feishu favicon, fall back to the first
     content image token found in window.DATA.block_map.
 4. Download the preview image and inline it as a data URI.
  4. Download the preview image using the session cookie obtained from the
     page fetch, save it directly into the linkding preview temp directory so
     the downstream preview_image_loader can pick it up without needing
     cookies.  Return the original (cookie-protected) preview URL as the
     metadata image field so the URL is stored for future reference.
"""
import json
import re
from urllib.parse import unquote


GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_IMAGE_PREVIEW = "https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/preview/{}/?preview_type=16"
_GENERIC_ICON = "feishu-static/ccm/pc/web/resource/bear/feishu.ico"


def _fetch(url, config):
    """HTTP GET with Googlebot UA. Returns (text, session)."""
    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent": GOOGLEBOT_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    for k, v in (config.get("headers") or {}).items():
        if k.lower() != "user-agent":
            session.headers[k] = v
    cookie_str = config.get("user_cookie") or ""
    if cookie_str:
        session.headers["Cookie"] = cookie_str
    timeout = config.get("timeout") or 30
    proxy = config.get("proxy")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    return resp.text, session


def _extract_block_map(html):
    """Parse window.DATA clientVars data dict from the SSR HTML."""
    idx = html.find("window.DATA = Object.assign")
    if idx < 0:
        return None
    start = html.find("Object({", idx)
    if start < 0:
        return None
    start += len("Object({")
    depth = 1
    i = start
    in_string = False
    escape = False
    while depth > 0 and i < len(html):
        c = html[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\":
            escape = True
            i += 1
            continue
        if c == '"':
            in_string = not in_string
        elif not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
        i += 1
    raw = "{" + html[start:i - 1] + "}"
    try:
        obj = json.loads(raw)
        return obj.get("data", {})
    except Exception:
        return None


def _first_image_token(block_map):
    """Find the first image block's token in document order."""
    if not block_map:
        return None
    seq = block_map.get("block_sequence", [])
    bm = block_map.get("block_map", {})
    for bid in seq:
        bdata = bm.get(bid, {}).get("data", {})
        if bdata.get("type") == "image":
            img = bdata.get("image", {})
            token = img.get("token")
            if token:
                return token
    return None


def _inline_image(url, config):
    """Download image with session cookies, save to linkding preview temp dir.

    Saves the downloaded image to the linkding preview temp directory using
    the same hash naming convention as preview_image_loader._url_to_filename,
    so the downstream loader finds it in cache and skips re-downloading.

    Returns the original URL (for storage in preview_image_remote_url) on
    success, or None on failure.
    """
    import requests
    import hashlib
    import mimetypes
    from pathlib import Path
    from django.conf import settings
    from bookmarks.utils import get_clean_url

    session = config.get("_session")
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": GOOGLEBOT_UA, "Referer": "https://feishu.cn/"})
        cookie_str = config.get("user_cookie") or ""
        if cookie_str:
            session.headers["Cookie"] = cookie_str
    else:
        session.headers["Referer"] = "https://feishu.cn/"
    timeout = config.get("timeout") or 30
    proxy = config.get("proxy")
    if proxy and not session.proxies:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
            # Save image to linkding preview temp directory using the same
            # hash-based naming convention as preview_image_loader so the
            # downstream loader finds the cached file and skips downloading.
            content_type = resp.headers.get("Content-Type", "image/png").split(";", 1)[0]
            ext = mimetypes.guess_extension(content_type) or ".png"
            clean_url = get_clean_url(url)
            file_hash = hashlib.md5(clean_url.encode()).hexdigest()
            temp_dir = Path(settings.LD_PREVIEW_FOLDER) / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"{file_hash}{ext}"
            temp_path.write_bytes(resp.content)
            # Return the original URL for storage; the cached file will be
            # found by preview_image_loader.load_preview_image via temp lookup.
            return url
    except Exception:
        pass
    return None


def replace(url, config):
    """Metadata replace hook: pure HTTP, no browser."""
    html, session = _fetch(url, config)
    config = dict(config)
    config["_session"] = session

    title = None
    description = None
    image = None

    # Extract og: tags (present in Googlebot SSR HTML)
    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    if og_title:
        title = unquote(og_title.group(1))
    og_desc = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
    if og_desc:
        description = unquote(og_desc.group(1))
    og_image = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
    if og_image:
        image_raw = og_image.group(1)
        if image_raw.startswith("//"):
            image = "https:" + image_raw
        elif image_raw.startswith("http"):
            image = image_raw
        if image and _GENERIC_ICON in image:
            image = None

    # Fall back to <title> tag
    if not title:
        m = re.search(r"<title>([^<]*)</title>", html)
        if m:
            title = m.group(1).strip()

    # If no usable og:image, find first content image from block_map
    if not image:
        block_map = _extract_block_map(html)
        token = _first_image_token(block_map)
        if token:
            preview_url = _IMAGE_PREVIEW.format(token)
            inlined = _inline_image(preview_url, config)
            image = inlined if inlined else preview_url

    # If description is the generic feishu placeholder, try block_map text
    if not description or "多人实时在线编辑" in (description or ""):
        block_map = _extract_block_map(html)
        if block_map:
            bm = block_map.get("block_map", {})
            seq = block_map.get("block_sequence", [])
            for bid in seq[1:]:
                bdata = bm.get(bid, {}).get("data", {})
                if bdata.get("type") in ("text", "heading1", "heading2", "heading3", "bullet"):
                    iat = bdata.get("text", {}).get("initialAttributedTexts", {})
                    texts = iat.get("text", {})
                    if texts:
                        first_text = list(texts.values())[0]
                        if len(first_text) > 10:
                            description = first_text[:300]
                            break

    return {"title": title, "description": description, "image": image, "url": url}
