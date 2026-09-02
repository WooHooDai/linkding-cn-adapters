import base64
import json
import re
import urllib.request
from urllib.parse import urljoin, quote

_PAGE_ID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.IGNORECASE)


def _extract_page_id(url):
    """Extract Notion page ID from URL — UUID or 32-char hex slug."""
    m = _PAGE_ID_RE.search(url)
    if m:
        return m.group(1)
    m2 = re.search(r'([0-9a-f]{32})', url.split('?')[0].split('#')[0], re.IGNORECASE)
    if m2:
        h = m2.group(1)
        return f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}'
    return None


def _fetch_page_chunk(page_id, config):
    """Call Notion's public loadPageChunk API. Returns (page_value, all_blocks)."""
    payload = json.dumps({
        'pageId': page_id,
        'limit': 100,
        'cursor': {'stack': []},
        'chunkNumber': 0,
        'verticalColumns': False,
    }).encode()
    req = urllib.request.Request(
        'https://www.notion.so/api/v3/loadPageChunk',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    timeout = config.get('timeout', 30) if config else 30
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    blocks = data.get('recordMap', {}).get('block', {})
    page_block = blocks.get(page_id, {})
    page_value = page_block.get('value', {}).get('value', {})
    return page_value, blocks


def _flatten_text(rich_text):
    """Flatten Notion rich text array [[text, ...]] into a string."""
    parts = []
    for item in rich_text:
        if isinstance(item, list) and item:
            parts.append(item[0] if isinstance(item[0], str) else str(item[0]))
        elif isinstance(item, str):
            parts.append(item)
    return ''.join(parts)


def replace(url, config):
    """Metadata replace hook: extract title/description/image via Notion's
    public loadPageChunk API — ~2s instead of ~14s browser rendering."""
    page_id = _extract_page_id(url)
    if not page_id:
        return None

    try:
        page_value, blocks = _fetch_page_chunk(page_id, config)
    except Exception:
        return None
    if not page_value:
        return None

    title = None
    props = page_value.get('properties', {})
    if 'title' in props:
        title = _flatten_text(props['title']).strip()

    image = None
    fmt = page_value.get('format', {})
    cover = fmt.get('page_cover')
    if cover:
        image = urljoin(url, cover) if cover.startswith('/') else cover

    # Description: first text block's content
    description = None
    for cid in page_value.get('content', []):
        block = blocks.get(cid, {}).get('value', {}).get('value', {})
        if block.get('type') == 'text':
            text_props = block.get('properties', {})
            if 'title' in text_props:
                description = _flatten_text(text_props['title']).strip()
                if description:
                    break

    return {'title': title, 'description': description, 'image': image, 'url': url}


_SCROLL_FIX_STYLE = (
    '<style id="notion-snapshot-expand">'
    # html/body need overflow:auto (not visible) so the browser creates a scrollbar
    'html.notion-html,body.notion-body{overflow:auto!important;height:auto!important}'
    # Inner containers expand to full content height
    '#notion-app,.notion-app-inner,'
    '.notion-frame,.notion-scroller,.notion-cursor-listener{'
    'overflow:visible!important;max-height:none!important;height:auto!important}'
    ':root{--full-viewport-height:auto!important;--dynamic-viewport-height:auto!important}'
    '</style>'
)


def _inject_scroll_fix(html: str) -> str:
    """Inject a <style> tag to neutralise Notion's viewport-height constraints.

    SingleFile sometimes drops the <style> tag injected by the browser script,
    leaving ``height:calc(* + 100vh)`` on ``.notion-frame`` which prevents
    scrolling in the saved snapshot.  We inject (or re-inject) the fix right
    after ``<head>`` so it appears before Notion's own styles and wins via
    ``!important``.
    """
    if 'notion-snapshot-expand' in html:
        return html  # Already has the fix
    # Insert right after <head ...> so it loads before Notion's CSS
    import re as _re
    html = _re.sub(r'(<head[^>]*>)', r'\1' + _SCROLL_FIX_STYLE, html, count=1)
    return html


def after(output_path, config):
    """Snapshot after hook: fix images that SingleFile failed to inline.

    Also ensures the saved HTML is scrollable by injecting a ``<style>`` tag
    that overrides Notion's viewport-height constraints (``height:calc(* +
    100vh)`` on ``.notion-frame``, etc.).

    Image fixing: Notion's image proxy redirects to
    img.notionusercontent.com. SingleFile can usually inline these from
    browser cache, but larger images (~200KB+) sometimes fail and end up with
    ``src=data:,`` (no quotes) in the saved HTML.

    SingleFile strips the original URL, so we must re-open the page in a
    browser, collect image URLs, and match by index. Then we download each
    failed image via Playwright's protocol-level request API (bypasses CORS)
    and replace the failed ``src=data:,`` with a data URI.
    """
    import os
    import re as _re
    import logging
    if not output_path or not os.path.exists(output_path):
        return

    url = (config or {}).get('url') or (config or {}).get('request_url', '')
    _logger = logging.getLogger('notion')

    if not url:
        _logger.warning("notion after hook: no URL in config, skipping image fix")

    with open(output_path, encoding='utf-8') as f:
        html = f.read()

    # --- Scroll fix: always inject (idempotent) ---
    # set_styles in the cleanup script should already inject these, but
    # SingleFile may drop them during serialization. Belt-and-suspenders.
    html = _inject_scroll_fix(html)

    # --- Image fix ---
    # Find all img tags with failed src=data:, (SingleFile uses unquoted attributes)
    # Match both quoted and unquoted variants
    img_tag_re = _re.compile(r'<img\s[^>]*?src="data:,"[^>]*>', _re.DOTALL)
    failed_tags = list(img_tag_re.finditer(html))
    # Also try unquoted variant: src=data:,
    if not failed_tags:
        img_tag_re = _re.compile(r'<img\s[^>]*?src=data:,[^>]*>', _re.DOTALL)
        failed_tags = list(img_tag_re.finditer(html))

    if not failed_tags:
        # No failed images — just save the scroll-fixed HTML
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        return

    if not url:
        # Can't fix images without URL, but still save scroll fix
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        return

    _logger.info("notion after hook: found %d failed images, re-opening page", len(failed_tags))
    # SingleFile strips original URLs. Re-open the page to get image URLs.
    from site_adapters.services.engine.browser_provider import launch_browser
    ua = (config or {}).get('headers', {}).get('User-Agent', '')

    browser = launch_browser(headless=True)
    playwright = getattr(browser, '__playwright__', None)
    try:
        context = browser.new_context(user_agent=ua)
        page = context.new_page()
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_selector('.notion-page-content', timeout=30000)
        page.wait_for_timeout(3000)

        # Scroll to load all content blocks and images (virtual scroll)
        page.evaluate("""async () => {
            const scroller = document.querySelector('.notion-scroller.vertical')
                || document.querySelector('.notion-scroller');
            if (scroller) {
                const step = Math.max(scroller.clientHeight - 100, 200);
                const total = scroller.scrollHeight;
                for (let y = 0; y < total; y += step) {
                    scroller.scrollTo(0, y);
                    await new Promise(r => setTimeout(r, 300));
                }
                scroller.scrollTo(0, scroller.scrollHeight);
                await new Promise(r => setTimeout(r, 500));
                scroller.scrollTo(0, 0);
                await new Promise(r => setTimeout(r, 500));
            }
            const imgs = Array.from(document.querySelectorAll('img'));
            await Promise.allSettled(imgs.map(img => {
                if (img.complete) return Promise.resolve();
                return new Promise(r => { img.onload = r; img.onerror = r; setTimeout(r, 5000); });
            }));
        }""")

        # Collect all image URLs from the live page
        img_srcs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('img')).map(img => img.src);
        }""")

        # Find all img tags in the saved HTML (both successful and failed)
        all_img_tags = list(_re.finditer(r'<img\s[^>]*>', html))

        _logger.debug("notion after hook: live page has %d images, saved HTML has %d img tags",
                      len(img_srcs), len(all_img_tags))

        # Download failed images by matching index
        for m in failed_tags:
            tag = m.group()
            # Find this tag's index in the full img list
            tag_start = m.start()
            img_idx = None
            for i, im in enumerate(all_img_tags):
                if im.start() == tag_start:
                    img_idx = i
                    break
            if img_idx is None or img_idx >= len(img_srcs):
                continue

            src = img_srcs[img_idx]
            if not src or src.startswith('data:'):
                continue

            _logger.debug("notion after hook: failed img idx=%d, live src=%s", img_idx, src[:100])
            try:
                response = context.request.get(src, timeout=30000)
                if response.ok:
                    body = response.body()
                    mime = response.headers.get('content-type', 'image/png').split(';')[0].strip()
                    data_uri = f"data:{mime};base64,{base64.b64encode(body).decode()}"
                    _logger.info("notion after hook: downloaded %d bytes, replaced in HTML", len(body))
                    # Replace src=data:, with src=<data_uri> (both quoted and unquoted)
                    new_tag = _re.sub(r'src="data:,"', f'src="{data_uri}"', tag, count=1)
                    if new_tag == tag:
                        new_tag = _re.sub(r'src=data:,', f'src={data_uri}', tag, count=1)
                    html = html.replace(tag, new_tag, 1)
                else:
                    _logger.warning("notion after hook: download failed, status=%d", response.status)
            except Exception as e:
                _logger.warning("notion after hook: download error: %s", e)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
    finally:
        browser.close()
        if playwright:
            playwright.stop()
