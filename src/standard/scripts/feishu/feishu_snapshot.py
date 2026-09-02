"""Feishu snapshot via pure HTTP — render block_map to self-contained HTML.

Feishu serves a fully server-side-rendered page to crawlers (Googlebot UA).
The SSR HTML body only shows ~40 summary blocks, but window.DATA contains
the complete block_map (200+ blocks) with text, image tokens, grid/table
structure, and links.

This script:
  1. Fetches the page with Googlebot UA (gets cookies for image download).
  2. Parses block_map from window.DATA.
  3. Renders the full document as clean semantic HTML with inline CSS.
  4. Downloads all images using the session cookies and inlines as data URIs.
  5. Writes the result to output_path — no SingleFile or browser needed.
"""
import base64
import html as html_module
import json
import re
from urllib.parse import unquote, urlparse


GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_IMAGE_PREVIEW = "https://internal-api-drive-stream.feishu.cn/space/api/box/stream/download/preview/{}/?preview_type=16"
_CDP_ENDPOINT = "{origin}/space/api/file/f/cdp-{kind}-{token}~noop/"


def _fetch_page(url, config):
    """Fetch the SSR HTML and return (html, session)."""
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
    timeout = config.get("timeout") or 60
    proxy = config.get("proxy")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    resp = session.get(url, timeout=timeout, allow_redirects=True)
    return resp.text, session


def _extract_block_map(html):
    """Parse window.DATA clientVars data dict from SSR HTML."""
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


def _extract_server_data(html):
    """Parse window.SERVER_DATA for title/description."""
    idx = html.find("window.SERVER_DATA = Object(")
    if idx < 0:
        return {}
    start = html.find("{", idx)
    if start < 0:
        return {}
    depth = 1
    i = start + 1
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
    try:
        return json.loads(html[start:i])
    except Exception:
        return {}


def _parse_attribs(attrib_str, apool):
    """Parse feishu attribs format: *N applies attrib N, +M means M chars.

    Returns list of (count, {attr_name: attr_value}) segments.
    """
    segments = []
    current_attribs = set()
    i = 0
    while i < len(attrib_str):
        if attrib_str[i] == "*":
            j = i + 1
            while j < len(attrib_str) and attrib_str[j].isdigit():
                j += 1
            current_attribs.add(attrib_str[i + 1:j])
            i = j
        elif attrib_str[i] == "+":
            j = i + 1
            while j < len(attrib_str) and attrib_str[j] in "0123456789abcdef":
                j += 1
            hex_part = attrib_str[i + 1:j]
            if not hex_part:
                i = j
                continue
            count = int(hex_part, 16)
            attrs = {}
            for aid in current_attribs:
                pair = apool.get(aid, [])
                if len(pair) >= 2:
                    attrs[pair[0]] = pair[1]
            segments.append((count, attrs))
            current_attribs = set()
            i = j
        else:
            i += 1
    return segments


def _render_text_segments(text, attribs_str, apool):
    """Render text with inline formatting (bold, italic, link, highlight)."""
    if not attribs_str:
        return html_module.escape(text)

    segments = _parse_attribs(attribs_str, apool)
    result = []
    pos = 0
    for count, attrs in segments:
        chunk = text[pos:pos + count]
        pos += count
        rendered = html_module.escape(chunk)

        link = attrs.get("link")
        if link:
            link_url = unquote(link) if "%" in link else link
            rendered = f'<a href="{html_module.escape(link_url)}">{rendered}</a>'

        if attrs.get("code") == "true" or attrs.get("inlineCode") == "true":
            rendered = f"<code>{rendered}</code>"

        if attrs.get("bold") == "true":
            rendered = f"<strong>{rendered}</strong>"

        if attrs.get("italic") == "true":
            rendered = f"<em>{rendered}</em>"

        if attrs.get("strikethrough") == "true":
            rendered = f"<del>{rendered}</del>"

        if attrs.get("underline") == "true":
            rendered = f"<u>{rendered}</u>"

        bg = attrs.get("textHighlightBackground")
        if bg:
            rendered = f'<span style="background-color:{html_module.escape(bg)}">{rendered}</span>'

        mention = attrs.get("mention")
        if mention:
            rendered = f'<span class="mention">{rendered}</span>'

        result.append(rendered)

    if pos < len(text):
        result.append(html_module.escape(text[pos:]))

    return "".join(result)


def _render_text_content(text_data):
    """Render the text field of a block to HTML."""
    if not text_data:
        return ""
    iat = text_data.get("initialAttributedTexts", {})
    if not iat or not iat.get("text"):
        return ""
    apool = text_data.get("apool", {}).get("numToAttrib", {})
    texts = iat.get("text", {})
    attribs = iat.get("attribs", {})

    parts = []
    for key in sorted(texts.keys()):
        segment_text = texts[key]
        segment_attribs = attribs.get(key, "")
        parts.append(_render_text_segments(segment_text, segment_attribs, apool))
    return "".join(parts)


def _download_image(token, session, config):
    """Download image by token, return data URI or None."""
    url = _IMAGE_PREVIEW.format(token)
    try:
        resp = session.get(url, timeout=config.get("timeout") or 60)
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ct.startswith("image/"):
            b64 = base64.b64encode(resp.content).decode("ascii")
            return f"data:{ct};base64,{b64}"
    except Exception:
        pass
    return None


def _download_embed_screenshot(token, session, config, kind, origin):
    """Download screenshot for embedded sheet/whiteboard via cdp endpoint."""
    url = _CDP_ENDPOINT.format(origin=origin, kind=kind, token=token)
    try:
        resp = session.get(url, timeout=config.get("timeout") or 60)
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ct.startswith("image/"):
            b64 = base64.b64encode(resp.content).decode("ascii")
            return f"data:{ct};base64,{b64}"
    except Exception:
        pass
    return None


def _render_block(bid, bm, session, config, depth=0):
    """Render a single block to HTML based on its type."""
    bval = bm.get(bid, {})
    bdata = bval.get("data", {})
    btype = bdata.get("type", "text")
    children = bdata.get("children", [])

    # --- Text blocks ---
    if btype == "page":
        return "".join(_render_block(c, bm, session, config, depth + 1) for c in children)

    if btype in ("text", "heading1", "heading2", "heading3", "heading4",
                 "heading5", "heading6", "heading7", "heading8", "heading9"):
        content = _render_text_content(bdata.get("text", {}))
        if btype == "text":
            return f'<p class="block text-block">{content}</p>'
        level = btype.replace("heading", "")
        return f'<h{level} class="block heading-block">{content}</h{level}>'

    if btype in ("bullet", "ordered", "todo"):
        content = _render_text_content(bdata.get("text", {}))
        if btype == "todo":
            done = bdata.get("done", False)
            checked = "checked" if done else ""
            cls = "todo-done" if done else "todo-pending"
            return f'<div class="block todo-block {cls}"><input type="checkbox" disabled {checked}><span>{content}</span></div>'
        tag = "ul" if btype == "bullet" else "ol"
        return f'<{tag} class="block list-block"><li>{content}</li></{tag}>'

    if btype in ("quote", "quote_container"):
        inner = "".join(_render_block(c, bm, session, config, depth + 1) for c in children)
        return f'<blockquote class="block quote-block">{inner}</blockquote>'

    if btype == "code":
        content = _render_text_content(bdata.get("text", {}))
        lang = bdata.get("style", {}).get("language", "")
        return f'<pre class="block code-block" data-lang="{html_module.escape(lang)}"><code>{content}</code></pre>'

    if btype in ("divider", "horizontal_rule"):
        return '<hr class="block divider-block">'

    if btype == "callout":
        inner = "".join(_render_block(c, bm, session, config, depth + 1) for c in children)
        return f'<div class="block callout-block">{inner}</div>'

    # --- Image block ---
    if btype == "image":
        img_data = bdata.get("image", {})
        token = img_data.get("token")
        width = img_data.get("width", 0)
        height = img_data.get("height", 0)
        align = bdata.get("align", "center")

        data_uri = _download_image(token, session, config) if token else None
        src = data_uri or _IMAGE_PREVIEW.format(token or "")

        style_parts = []
        if width:
            style_parts.append(f"width:{width}px")
        if align == "center":
            style_parts.append("margin-left:auto;margin-right:auto")
        style = ";".join(style_parts)

        img_html = f'<img src="{html_module.escape(src)}" alt="飞书文档图片" loading="lazy"'
        if style:
            img_html += f' style="{style}"'
        if width and height:
            img_html += f' width="{width}" height="{height}"'
        img_html += ">"

        caption_text = img_data.get("caption", {}).get("text", {})
        caption = _render_text_content(caption_text) if caption_text.get("initialAttributedTexts", {}).get("text") else ""
        caption_html = f"<figcaption>{caption}</figcaption>" if caption else ""
        return f'<figure class="block image-block" style="text-align:{align}">{img_html}{caption_html}</figure>'

    # --- Grid (multi-column layout) ---
    if btype == "grid":
        cols = []
        for cid in children:
            cdata = bm.get(cid, {}).get("data", {})
            if cdata.get("type") != "grid_column":
                continue
            col_children = cdata.get("children", [])
            col_inner = "".join(_render_block(c, bm, session, config, depth + 1) for c in col_children)
            col_width = cdata.get("width", 0)
            width_style = f"width:{col_width}px;" if col_width else "flex:1;"
            cols.append(f'<div class="grid-col" style="{width_style}">{col_inner}</div>')
        return f'<div class="block grid-block">{"".join(cols)}</div>'

    # --- Sheet (embedded spreadsheet) ---
    if btype == "sheet":
        token = bdata.get("token", "")
        origin = config.get("_origin", "https://rqz3til02g.feishu.cn")
        screenshot = _download_embed_screenshot(token, session, config, "sheet", origin) if token else None
        if screenshot:
            return f'<div class="block sheet-block"><img src="{screenshot}" alt="飞书表格" style="width:100%"></div>'
        return '<div class="block sheet-block"><div class="sheet-placeholder">📋 飞书表格</div></div>'

    # --- Whiteboard ---
    if btype == "whiteboard":
        token = bdata.get("token", "")
        origin = config.get("_origin", "https://rqz3til02g.feishu.cn")
        screenshot = _download_embed_screenshot(token, session, config, "whiteboard", origin) if token else None
        if screenshot:
            return f'<div class="block whiteboard-block"><img src="{screenshot}" alt="飞书画板" style="width:100%"></div>'
        return '<div class="block whiteboard-block"><div class="wb-placeholder">📐 飞书画板</div></div>'

    # --- Iframe / embed ---
    if btype == "iframe":
        src = bdata.get("url", "") or bdata.get("src", "")
        if src:
            return f'<div class="block iframe-block"><iframe src="{html_module.escape(src)}" style="width:100%;min-height:400px;border:0"></iframe></div>'
        return '<div class="block iframe-block"><div class="embed-placeholder">🔗 嵌入内容</div></div>'

    # --- Fallback: render children if any ---
    if children:
        return "".join(_render_block(c, bm, session, config, depth + 1) for c in children)

    return f'<div class="block unknown-block" data-type="{btype}">[{btype}]</div>'


_CSS = """
:root {
  --text-title: #1f2329;
  --text-body: #3f3f3f;
  --text-caption: #646a73;
  --text-link: #336df4;
  --bg-body: #ffffff;
  --bg-highlight: rgba(255,246,122,0.8);
  --border: #dee0e3;
  --bg-filler: #f5f6f7;
  --bg-quote: #f5f6f7;
  --bg-code: #f5f6f7;
  --bg-callout: #f0f4ff;
  --font-size: 16px;
  --line-height: 1.75;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
  font-size: var(--font-size);
  line-height: var(--line-height);
  color: var(--text-body);
  background: var(--bg-body);
  margin: 0 auto;
  padding: 40px 20px;
  max-width: 820px;
}
.doc-container { padding: 40px 24px; }
.block { margin: 8px 0; }
.text-block { margin: 6px 0; color: var(--text-body); }
h1.block, h2.block, h3.block, h4.block, h5.block, h6.block {
  color: var(--text-title);
  font-weight: 600;
  margin: 24px 0 12px;
  line-height: 1.4;
}
h1.block { font-size: 1.9em; }
h2.block { font-size: 1.5em; }
h3.block { font-size: 1.25em; }
h4.block { font-size: 1.1em; }
.list-block { padding-left: 28px; margin: 6px 0; }
ol.list-block, ul.list-block { padding-left: 28px; }
.todo-block { display: flex; align-items: flex-start; gap: 8px; margin: 6px 0; }
.todo-block input { margin-top: 5px; }
.todo-done span { text-decoration: line-through; color: var(--text-caption); }
.quote-block {
  border-left: 4px solid var(--text-link);
  background: var(--bg-quote);
  padding: 12px 16px;
  margin: 12px 0;
  border-radius: 0 6px 6px 0;
  color: var(--text-body);
}
.code-block {
  background: var(--bg-code);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 16px;
  overflow-x: auto;
  font-family: "SF Mono", "Fira Code", Consolas, Monaco, monospace;
  font-size: 0.9em;
  line-height: 1.6;
}
.code-block code { background: none; padding: 0; }
.divider-block { border: 0; border-top: 1px solid var(--border); margin: 20px 0; }
.callout-block {
  background: var(--bg-callout);
  border-radius: 8px;
  padding: 12px 16px;
  margin: 12px 0;
}
.image-block { margin: 16px 0; text-align: center; }
.image-block img { max-width: 100%; height: auto; border-radius: 4px; display: inline-block; }
.image-block figcaption { font-size: 0.85em; color: var(--text-caption); margin-top: 6px; text-align: center; }
.grid-block { display: flex; gap: 12px; margin: 12px 0; flex-wrap: wrap; }
.grid-col { min-width: 0; }
.sheet-block, .whiteboard-block, .iframe-block { margin: 16px 0; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.sheet-placeholder, .wb-placeholder, .embed-placeholder { padding: 40px; text-align: center; color: var(--text-caption); background: var(--bg-filler); }
a { color: var(--text-link); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--bg-code); padding: 2px 6px; border-radius: 4px; font-size: 0.88em; font-family: "SF Mono", "Fira Code", Consolas, Monaco, monospace; }
.mention { color: var(--text-link); background: var(--bg-callout); border-radius: 3px; padding: 0 4px; }
"""


def replace(url, config, output_path):
    """Snapshot replace hook: render block_map to self-contained HTML."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else url
    config = dict(config)
    config["_origin"] = origin

    html_raw, session = _fetch_page(url, config)
    block_map = _extract_block_map(html_raw)

    server_data = _extract_server_data(html_raw)
    title = server_data.get("meta", {}).get("title", "") or "飞书文档"

    if not block_map:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_raw)
        return

    bm = block_map.get("block_map", {})
    seq = block_map.get("block_sequence", [])

    page_id = seq[0] if seq else None
    if page_id and page_id in bm:
        page_children = bm[page_id].get("data", {}).get("children", [])
        content_html = "".join(_render_block(c, bm, session, config) for c in page_children)
    else:
        content_html = "".join(_render_block(bid, bm, session, config) for bid in seq[1:])

    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_module.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="doc-container">
{content_html}
</div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)
