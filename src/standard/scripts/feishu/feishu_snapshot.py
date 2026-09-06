"""Feishu/Lark snapshots from the complete document model and embedded previews.

Pipeline
--------
1. Fetch the document SSR HTML anonymously (search-engine UA is enough, no
   login / cookies are provided by the user).  The SSR embeds the full
   Etherpad collaboration model in ``window.DATA.clientVars``:
   ``data.collab_client_vars`` holds ``apool`` (attribute dictionary),
   ``initialAttributedText`` (main document text + attribute ops) and
   ``initialAttributedTexts`` (per-zone sub-documents: callouts, table
   cells) plus ``resources.images``.
2. Decode the Etherpad attributed text into lines with block / inline
   attributes and render them with Feishu's own class names, so the
   original stylesheets style everything natively.
3. Fetch the comment model from ``broadcast/get_init_data`` (same
   anonymous session) and render the right-hand comment panel.
4. Inline stylesheets / images / avatars as data URLs, strip scripts and
   write a fully static snapshot to ``output_path``.

Paginated docx models are completed with the page's anonymous session. Canvas
embeds are captured separately in the project browser after they have loaded.
"""
import base64
import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

import requests

GOOGLEBOT_UA = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)

COMMENT_API = (
    "/space/api/broadcast/get_init_data/?obj_type={obj_type}"
    "&obj_token={token}&data_type=COMMENT"
)
PREVIEW_URL = (
    "https://internal-api-drive-stream.feishu.cn/space/api/box/stream/"
    "download/preview/{token}/?preview_type=16"
)

# calloutEmojiId -> glyph (Feishu emoji sprite is not loaded offline)
CALLOUT_EMOJI = {
    "books": "\U0001f4da",
    "star2": "\U0001f31f",
    "bulb": "\U0001f4a1",
    "no_entry_sign": "\U0001f6ab",
    "warning": "⚠️",
    "info": "ℹ️",
    "like": "\U0001f44d",
    "question": "❓",
    "sparkles": "✨",
    "memo": "\U0001f4dd",
    "trophy": "\U0001f3c6",
    "flag": "\U0001f6a9",
    "fire": "\U0001f525",
    "heart": "❤️",
    "target": "\U0001f3af",
    "rocket": "\U0001f680",
    # new /docx block_map uses emoji shortcodes
    "monkey_face": "\U0001f435",
    "thinking_face": "\U0001f914",
    "white_check_mark": "✅",
    "pushpin": "\U0001f4cc",
    "eyes": "\U0001f440",
    "speech_balloon": "\U0001f4ac",
    "exclamation": "❗",
    "zap": "⚡",
    "key": "\U0001f511",
    "mag": "\U0001f50d",
    "bulb1": "\U0001f4a1",
    "smile": "\U0001f604",
    "tada": "\U0001f389",
    "point_right": "👉",
    "100": "\U0001f4af",
    "clap": "\U0001f44f",
    "muscle": "\U0001f4aa",
    "pray": "\U0001f64f",
    "sunny": "☀️",
    "star": "⭐",
    "bookmark": "\U0001f516",
    "pencil": "\U0001f4dd",
    "hammer_and_wrench": "\U0001f6e0",
    "chart": "\U0001f4ca",
    "bullettrain_front": "\U0001f685",
    "bangbang": "‼️",
    "ok_hand": "👌",
}

ZERO_WIDTH = "\u200b"


# ===========================================================================
# Fetching
# ===========================================================================

def _make_session(config):
    session = requests.Session()
    headers = dict(config.get("headers") or {})
    headers.setdefault("User-Agent", GOOGLEBOT_UA)
    headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
    session.headers.update(headers)
    cookie = config.get("_user_cookie") or config.get("user_cookie")
    if cookie:
        session.headers["Cookie"] = cookie
    proxy = config.get("proxy")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _balanced(text, start):
    """Return the balanced {...}/[...] JSON substring beginning at start."""
    open_char = text[start]
    close_char = {"{": "}", "[": "]"}[open_char]
    depth = 1
    i = start + 1
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    raise ValueError("unbalanced JSON structure")


def extract_client_vars(ssr_html):
    """Extract clientVars.

    Old ``/docs`` pages embed it at the first ``window.DATA = { clientVars: Object(...)``.
    New ``/docx`` pages first set ``clientVars: undefined`` plus a meta Object(),
    then assign the real clientVars in a second statement — so try every
    ``clientVars: Object(`` candidate and return the first carrying a ``data`` dict.
    """
    candidates = list(re.finditer(r"clientVars\s*:\s*Object\(", ssr_html))
    if not candidates:
        # legacy shape: window.DATA = { clientVars: Object(
        match = re.search(r"window\.DATA\s*=\s*\{", ssr_html)
        if not match:
            raise RuntimeError("window.DATA not found in SSR HTML")
        obj_match = re.search(r"Object\(", ssr_html[match.end():])
        if not obj_match:
            raise RuntimeError("clientVars Object(...) not found")
        candidates = [obj_match]
        starts = [match.end() + obj_match.end()]
    else:
        starts = [m.end() for m in candidates]
    last_error = None
    for start in starts:
        try:
            parsed = json.loads(_balanced(ssr_html, start))
        except ValueError as exc:  # malformed candidate, try next
            last_error = exc
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
            return parsed
    if last_error:
        raise last_error
    raise RuntimeError("no usable clientVars object found in SSR HTML")


def fetch_models(url, config):
    session = _make_session(config)
    timeout = config.get("timeout") or 30
    resp = session.get(
        config.get("request_url") or config.get("_request_url") or url,
        timeout=timeout, allow_redirects=True,
    )
    resp.raise_for_status()
    ssr_html = resp.text
    client_vars = extract_client_vars(ssr_html)
    _complete_docx_model(session, client_vars, resp.url, min(timeout, 30))

    token = client_vars.get("token") or urlparse(resp.url).path.rstrip("/").split("/")[-1]
    # old /docs Etherpad documents use obj_type=2; new /docx block_map uses 22
    path_url = urlparse(url).path
    obj_type = "22" if "/docx/" in path_url or "/docx" == path_url[-5:] else "2"
    api_url = "{scheme}://{host}{path}".format(
        scheme=urlparse(url).scheme,
        host=urlparse(url).netloc,
        path=COMMENT_API.format(token=token, obj_type=obj_type),
    )
    comment_resp = session.get(
        api_url, timeout=timeout, headers={"Referer": url}
    )
    comment_data = None
    if comment_resp.ok:
        try:
            payload = comment_resp.json()
            if payload.get("code") == 0:
                comment_data = payload["data"]["data"]
        except (ValueError, KeyError):
            comment_data = None
    return session, ssr_html, client_vars, comment_data


def _complete_docx_model(session, client_vars, url, timeout):
    """Follow the same cursor/skip-block requests as the public docx viewer."""
    data = client_vars["data"]
    if "block_map" not in data:
        return
    origin = urlparse(url)
    endpoint = f"{origin.scheme}://{origin.netloc}/space/api/docx/pages/client_vars"
    headers = {"Referer": url}
    csrf = next((c.value for c in session.cookies if c.name == "_csrf_token"), "")
    if csrf:
        headers["X-CSRFToken"] = csrf
    seen = set()
    pending = []

    def enqueue(part, mode):
        cursors = part.get("next_cursors") or []
        if not part.get("concurrent") and part.get("has_more"):
            cursors = [part.get("cursor")]
        jobs = [(mode, cursor, "") for cursor in cursors if cursor]
        jobs.extend((4, part.get("cursor") or "", bid)
                    for bid in part.get("skip_blocks") or [])
        for job in jobs:
            if job not in seen:
                seen.add(job)
                pending.append(job)

    def fetch(job):
        mode, cursor, block_id = job
        # SSR concurrent cursors mark 239-block slices, like the viewer's
        # clientvar fetcher. A smaller limit silently leaves gaps between them.
        params = {"id": data["id"], "mode": mode, "limit": 239}
        if cursor:
            params["cursor"] = cursor
        if block_id:
            params["block_id"] = block_id
        response = session.get(endpoint, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("data", {}).get("block_map"):
            raise RuntimeError(f"Feishu document page failed: code={payload.get('code')}")
        return mode, payload["data"]

    enqueue(data, client_vars.get("mode", 7))
    with ThreadPoolExecutor(max_workers=3) as pool:
        while pending:
            jobs, pending = pending, []
            for mode, part in pool.map(fetch, jobs):
                for key in ("block_map", "user_map", "editor_map", "meta_map"):
                    if isinstance(part.get(key), dict):
                        data.setdefault(key, {}).update(part[key])
                data.setdefault("block_sequence", []).extend(part.get("block_sequence") or [])
                enqueue(part, mode)
    data["block_sequence"] = list(dict.fromkeys(data.get("block_sequence") or []))
    missing = {child for block in data["block_map"].values()
               for child in block.get("data", {}).get("children") or []
               if child not in data["block_map"]}
    if missing:
        raise RuntimeError(f"Feishu document is incomplete: {len(missing)} blocks missing")
    data.update(has_more=False, next_cursors=[], skip_blocks=[])


# ===========================================================================
# Etherpad attributed-text decoder
# ===========================================================================

_B36_RE = re.compile(r"[0-9a-zA-Z]+")


def _read_b36(ops, i):
    match = _B36_RE.match(ops, i)
    return int(match.group(0), 36), match.end()


# Attributes that describe the block itself rather than inline text style.
BLOCK_ATTR_KEYS = {
    "heading", "list", "ol-id", "start", "origin-start", "align",
    "zoneId", "zoneType", "calloutEmojiId", "calloutBackgroundColor",
    "calloutBorderColor", "gallery", "aceTable", "horizontal-line",
    "colWidth",
}


def decode_attributed_text(text, ops, attr_pool):
    """Decode Etherpad AText into ordered lines.

    Each line::
        {"guid": str|None, "block": {attr: val},
         "block_pairs": [(attr, val), ...] (ordered, keeps duplicates),
         "segs": [(text, {attr: val}), ...], "text": visible text}

    Feishu line encoding::
        '*'                 - line marker op (1 char); its attributes are the
                              block descriptor for heading/list/zone/table.
        ' ' (atomic block)  - gallery / horizontal-line blocks encode their
                              descriptor on a following single-space op.
        content ops         - inline text runs.
        '\\n' (|1+1 op)      - line terminator; carries author / lineguid.
    A line consisting of the terminator alone is an empty paragraph.
    """
    events = []  # (start, end, attr_idxs, is_newline)
    pos = 0
    i = 0
    active = []
    is_line_op = False
    n = len(ops)
    while i < n:
        code = ops[i]
        i += 1
        if code == "*":
            idx, i = _read_b36(ops, i)
            active.append(idx)
        elif code == "|":
            _, i = _read_b36(ops, i)
            is_line_op = True
        elif code in "+-=":
            length, i = _read_b36(ops, i)
            events.append((pos, pos + length, list(active), is_line_op))
            pos += length
            active = []
            is_line_op = False
        elif code == "$":
            break

    def pairs_of(idxs):
        return [attr_pool[idx] for idx in idxs if idx in attr_pool]

    def attrs_of(pairs):
        out = {}
        for key, val in pairs:
            out.setdefault(key, val)
        return out

    # group events into lines at newline chars (a terminator may or may not
    # carry a ``|N`` line-marker, so split on the '\n' itself)
    raw_lines = []
    current = []
    for s, e, idxs, is_newline in events:
        chunk = text[s:e]
        pairs = pairs_of(idxs)
        if "\n" in chunk:
            # one op may merge several consecutive empty lines
            parts = chunk.split("\n")
            for p_i, part in enumerate(parts):
                if part:
                    current.append((part, pairs, False))
                if p_i < len(parts) - 1:
                    current.append(("\n", pairs, True))
                    raw_lines.append(current)
                    current = []
        else:
            current.append((chunk, pairs, is_newline))
    if current:
        raw_lines.append(current)

    lines = []
    for raw in raw_lines:
        block_pairs = []
        segs = []
        guid = None
        idx = 0
        # 1. leading '*' marker contributes block attributes. Etherpad merges
        # the marker with the first content op when their attribute sets are
        # equal, so a leading '*' inside a longer op must be stripped too.
        if raw and not raw[0][2] and raw[0][0].startswith("*"):
            first_text, first_pairs = raw[0][0], raw[0][1]
            block_pairs.extend(first_pairs)
            if len(first_text) == 1:
                idx = 1
            else:
                raw[0] = (first_text[1:], first_pairs, False)
                idx = 0
        # 2. atomic-block descriptor on a following single-space op
        if (
            idx < len(raw)
            and not raw[idx][2]
            and raw[idx][0] == " "
            and any(key in BLOCK_ATTR_KEYS for key, _ in raw[idx][1])
        ):
            block_pairs.extend(raw[idx][1])
            idx += 1
        # 3. remaining content ops; the terminator contributes lineguid
        for chunk, pairs, is_newline in raw[idx:]:
            if is_newline:
                for key, val in pairs:
                    if key == "lineguid":
                        guid = val
                continue
            for key, val in pairs:
                if key == "lineguid" and guid is None:
                    guid = val
            segs.append((chunk, attrs_of(pairs)))
        block = attrs_of(block_pairs)
        lines.append(
            {
                "guid": guid,
                "block": block,
                "block_pairs": block_pairs,
                "segs": segs,
                "text": "".join(seg[0] for seg in segs),
            }
        )
    return lines


# ===========================================================================
# Model rendering (native Feishu class structure)
# ===========================================================================

def esc(value):
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class ModelRenderer:
    def __init__(self, collab, resource_store):
        self.ccv = collab
        self.attr_pool = {
            int(k): tuple(v)
            for k, v in collab["apool"]["numToAttrib"].items()
        }
        iats = collab.get("initialAttributedTexts") or {}
        self.zone_texts = iats.get("texts") or {}
        self.zone_ops = iats.get("attribs") or {}
        self.cols = iats.get("cols") or {}
        self.rows = iats.get("rows") or {}
        self.resources = resource_store
        self.dom_seq = 0
        # first-occurrence order of comment anchors in the body
        self.comment_order = []
        self._comment_seen = set()
        # lineguids of headings, in document order (for catalogue links)
        self.heading_guids = []

    # ---- id / comment bookkeeping ------------------------------------

    def next_id(self):
        value = self.dom_seq
        self.dom_seq += 1
        return "magicdomid-%02d" % value if value < 10 else "magicdomid-%d" % value

    def note_comments(self, attrs):
        for key in attrs:
            if key.startswith("comment-id-") or key.startswith("comment-resolved-"):
                cid = key.split("-", 2)[-1]
                if cid not in self._comment_seen:
                    self._comment_seen.add(cid)
                    self.comment_order.append(cid)

    # ---- zone decoding ------------------------------------------------

    def decode_zone(self, zone_id):
        text = self.zone_texts.get(zone_id)
        ops = self.zone_ops.get(zone_id)
        if not isinstance(text, str):
            return []
        if not isinstance(ops, str):
            ops = ""
        return decode_attributed_text(text, ops, self.attr_pool)

    # ---- inline segments ---------------------------------------------

    def render_segments(self, segs):
        html_parts = []
        for text, attrs in segs:
            if text == "":
                continue
            self.note_comments(attrs)
            html_parts.append(self.render_segment(text, attrs))
        return "".join(html_parts)

    def render_segment(self, text, attrs):
        inner = esc(text)

        # innermost visual styling
        classes = []
        style = []
        if attrs.get("bold") == "true":
            classes.append("bold")
        if attrs.get("italic") == "true":
            style.append("font-style:italic")
        if attrs.get("underline") == "true":
            style.append("text-decoration:underline")
        if attrs.get("strikethrough") == "true":
            style.append("text-decoration:line-through")
        tcolor = attrs.get("textcolor")
        if tcolor:
            classes.append("text-highlight")
            classes.append("color-%s" % tcolor.replace(" ", ""))
            style.append("color:%s" % tcolor)
        bcolor = attrs.get("backcolor")
        if bcolor:
            classes.append("text-highlight")
            classes.append("background-color-%s" % bcolor.replace(" ", ""))
            classes.append("bgcolor-wrap-padding")
            style.append("background-color:%s" % bcolor)

        class_attr = (" class=%s" % " ".join(classes)) if classes else ""
        style_attr = (' style="%s"' % ";".join(style)) if style else ""
        if classes or style:
            inner = '<span%s%s data-leaf="true"><span data-string="true">%s</span></span>' % (
                class_attr,
                style_attr,
                inner,
            )

        # mention of another document / user (mention-* attrs)
        m_link = None
        m_type = None
        for key, val in attrs.items():
            if key.startswith("mention-link_"):
                m_link = unquote(key[len("mention-link_"):])
            elif key == "mention-type_22" or key.startswith("mention-type_"):
                m_type = key[len("mention-type_"):]
        if m_link is not None or any(k.startswith("mention-token_") for k in attrs):
            cls = " ".join(
                "%s_%s" % (k, v) if v != "true" else k
                for k, v in attrs.items()
                if k.startswith("mention-")
            )
            label = inner
            if m_link:
                inner = (
                    '<span class="%s mention popover-disabled" data-leaf="true" '
                    'style="margin:0 .1px"><a href="%s" target="_blank" '
                    'rel="noopener noreferrer">%s</a></span>'
                    % (esc(cls), esc(m_link), label)
                )
            else:
                inner = (
                    '<span class="%s mention" data-leaf="true">%s</span>'
                    % (esc(cls), label)
                )

        # @user holder (at-uuid / at-holder) — body @ mentions, plain text
        if attrs.get("at-holder") == "true":
            inner = '<span class="at-user-text">%s</span>' % inner

        # hyperlink: attr name is url-<internalKey>, attr VALUE is the
        # percent-encoded destination URL
        for key, val in attrs.items():
            if key.startswith("url-"):
                href = unquote(val)
                inner = (
                    '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                    % (esc(href), inner)
                )
                break

        # comment anchor wrappers (also drive right-panel ordering)
        wrappers = []
        for key in attrs:
            if key.startswith("comment-id-"):
                cid = key[len("comment-id-"):]
                wrappers.append(
                    '<span class="comment-local-render-%s comment-id-%s" data-leaf="true">'
                    % (cid, cid)
                )
            elif key.startswith("comment-resolved-"):
                cid = key[len("comment-resolved-"):]
                wrappers.append(
                    '<span class="comment-local-render-%s comment-resolved-%s" data-leaf="true">'
                    % (cid, cid)
                )
        if wrappers:
            inner = "".join(wrappers) + inner + "</span>" * len(wrappers)
        return inner

    # ---- leaf / line scaffolding -------------------------------------

    @staticmethod
    def _pocket():
        return (
            '<span class="ace-line-pocket ignore-dom pocket-ignore" '
            'data-ignore-mutation="true" data-fake-text="" contenteditable="false" '
            'data-ignore-selection=""></span>'
        )

    @staticmethod
    def _list_marker(kind, start):
        """Ordered-list marker label for a level (1 decimal / 2 alpha / 3 roman)."""
        try:
            num = int(start or 1)
        except (TypeError, ValueError):
            num = 1
        if kind == "number2":
            return chr(ord("a") + num - 1)
        if kind == "number3":
            romans = ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
            value, out = num, []
            for weight, glyph in romans:
                while value >= weight:
                    out.append(glyph)
                    value -= weight
            return "".join(out)
        return str(num)

    def render_line_inner(self, line, extra_line_class="", in_zone=False):
        """Render one ace-line outer div from a decoded line."""
        block = line["block"]
        classes = ["ace-line"]

        align = block.get("align")
        if align:
            classes.append("align-%s" % align)
        heading = block.get("heading")
        if heading:
            classes += ["r-heading", "heading-%s" % heading]
        list_kind = block.get("list")
        if list_kind:
            classes.append("list-div")
            if list_kind.startswith("number"):
                ol_id = block.get("ol-id")
                if ol_id:
                    classes.append("ol-id%s" % ol_id)
                level = list_kind[-1]
                classes.append("list-start-number%s" % level)
        if block.get("gallery"):
            classes += ["gallery-line", "IGNORE_LIST", "IGNORE_HEADING"]
        if block.get("zoneType"):
            classes += [
                "zone-plugin-ace-line",
                "IGNORE_LIST",
                "IGNORE_HYPERLINK",
                "IGNORE_INLINE_CODE",
                "IGNORE_BACKGROUND_COLOR",
                "IGNORE_LINE_BACKGROUND",
            ]
            ztype = block["zoneType"]
            zid = block.get("zoneId", "")
            classes.append("%s-zoneId-%s" % (ztype, zid))
            classes.append("zoneType-%s" % ztype)
        if block.get("aceTable"):
            classes.append("ace-table-line")
        if extra_line_class:
            classes.append(extra_line_class)
        classes.append("locate")
        if line.get("guid"):
            classes.append("lineguid-%s" % line["guid"])
        if not in_zone:
            classes.append("wrapper")

        line_id = self.next_id()
        # headings carry their lineguid as id so the catalogue can jump to
        # them (the SSR catalogue links to #<lineguid>)
        elem_id = line["guid"] if heading and line.get("guid") else line_id
        if heading and line.get("guid"):
            self.heading_guids.append(line["guid"])
        opening = (
            '<div id="%s" data-node="true" class="%s"%s>'
            % (
                elem_id,
                " ".join(classes),
                ' contenteditable="false"' if block.get("zoneType") else "",
            )
        )

        body_html = self.render_line_body(line)
        return opening + '<div data-line-wrapper="true" dir="auto">' + body_html + "</div></div>"

    def render_line_body(self, line):
        block = line["block"]
        pocket = self._pocket()

        # compound / atomic blocks
        if block.get("zoneType") == "calloutBlock":
            return pocket + self.render_callout(line, block)
        if block.get("aceTable"):
            return pocket + self.render_table_mount(line, block)
        if block.get("gallery"):
            return pocket + self.render_galleries(block)
        if block.get("horizontal-line") == "true":
            return pocket + self.render_horizontal_line()

        content = self.render_segments(line["segs"]) or self._empty_span(line)
        list_kind = block.get("list")
        if list_kind:
            content = self.render_list(list_kind, block, content)
        return pocket + content

    @staticmethod
    def _empty_span(line):
        return '<span data-leaf="true"><span data-string="true">%s</span></span>' % esc(
            line["text"]
        )

    def render_list(self, kind, block, content):
        if kind.startswith("bullet"):
            return (
                '<ul class="list-%s r-list r-list-bullet"><li>%s</li></ul>'
                % (kind, content)
            )
        if kind.startswith("number"):
            start = block.get("start") or "1"
            marker = self._list_marker(kind, start)
            if kind == "number1":
                ol_open = (
                    '<ol data-origin-start="%s" start="%s" data-start="%s" '
                    'class="list-%s r-list r-list-number">'
                    % (start, start, start, kind)
                )
                li_open = '<li start="%s" data-start="%s">' % (start, marker)
            else:
                # nested levels: ol carries numeric start, li carries the
                # rendered marker (a/b/…, i/ii/…) used by CSS ::before
                ol_open = (
                    '<ol start="%s" data-start="%s" class="list-%s r-list r-list-number">'
                    % (start, start, kind)
                )
                li_open = '<li data-start="%s">' % marker
            return ol_open + li_open + content + "</li></ol>"
        # indent
        return (
            '<ul class="list-%s r-list r-list-indent"><li>%s</li></ul>'
            % (kind, content)
        )

    # ---- atomic blocks -----------------------------------------------

    def render_horizontal_line(self):
        return (
            '<span class="horizontal-line" data-leaf="true">'
            '<span data-fake-text=" " contenteditable="false" '
            'style="display:block;box-sizing:content-box;padding:13px 0 12px;'
            'overflow:hidden;height:1px;background-origin:content-box;'
            'background-image:linear-gradient(90deg,var(--ccmtoken-doc-highlightcolor-neutral-solid),'
            'var(--ccmtoken-doc-highlightcolor-neutral-solid));'
            'background-repeat:no-repeat;border:0">'
            '<span data-zero-space="true">%s</span></span></span>' % ZERO_WIDTH
        )

    def render_galleries(self, block):
        try:
            gallery = json.loads(block["gallery"])
            items = gallery.get("items") or []
        except (ValueError, TypeError):
            items = []
        rendered = []
        for item in items:
            rendered.append(self.render_gallery_item(item))
        return (
            '<div class="new-gallery" contenteditable="false" '
            'style="justify-content:center;display:flex;align-items:flex-start;'
            'padding:8px 0;text-indent:initial">%s</div>' % "".join(rendered)
        )

    def render_gallery_item(self, item):
        token = item.get("file_token") or ""
        anchor_open, anchor_close = "", ""
        for ref in item.get("comments") or []:
            # ref looks like "comment-id-123" / "comment-resolved-123"
            cid = ref.split("-", 2)[-1]
            cls = (
                "comment-resolved-%s" % cid
                if ref.startswith("comment-resolved")
                else "comment-id-%s" % cid
            )
            anchor_open += (
                '<span class="comment-local-render-%s %s ld-img-anchor" data-leaf="true">'
                % (cid, cls)
            )
            anchor_close += "</span>"
            if cid not in self._comment_seen:
                self._comment_seen.add(cid)
                self.comment_order.append(cid)
        src = unquote(item.get("src") or "")
        if not src and token:
            src = PREVIEW_URL.format(token=token)
        data_url = self.resources.get_image(src) if src else ""
        width = None
        for key in ("currWidth", "width"):
            try:
                width = int(float(item.get(key)))
                break
            except (TypeError, ValueError):
                continue
        width = width or 400
        try:
            h = float(item.get("currHeight") or item.get("height") or 0)
            w = float(item.get("currWidth") or item.get("width") or 1)
            ratio = h / w * 100
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 50
        img_src = data_url or src
        uuid = esc(item.get("uuid") or token)
        rendered = (
            '<span class="IGNORE_HYPERLINK IGNORE_LINE_BACKGROUND IGNORE_BOLD '
            'IGNORE_ITALIC IGNORE_INLINE_CODE IGNORE_UNDERLINE IGNORE_STRIKETHROUGH '
            'IGNORE_BACKGROUND_COLOR" data-leaf="true" '
            'style="max-width:100%;min-width:0;line-height:0;margin:0 .1px">'
            '<div style="display:flex;align-items:flex-start;justify-content:inherit;'
            'white-space:nowrap;max-width:100%;min-width:0">'
            f'<span data-zero-space="true">{ZERO_WIDTH}</span>'
            '<span data-fake-text=" " contenteditable="false" class="image-container-wrap" '
            f'style="width:{width}px;display:inline-flex;box-sizing:border-box;max-width:100%;padding:2px">'
            '<div style="position:relative;display:inline-block;vertical-align:middle;'
            'height:100%;width:100%;line-height:0">'
            '<div class="image-view-placeholder" style="display:inline-block;'
            f'vertical-align:top;width:100%;height:0;padding-top:{ratio:.4f}%">'
            '<div class="image-highlight-wrapper" style="height:100%;width:100%;'
            'display:inline-block;position:absolute;top:0;left:0">'
            f'<img data-uuid="{uuid}" src="{esc(img_src)}" alt="" '
            'style="width:100%;height:100%;object-fit:fill;display:block">'
            "</div></div></div></span></div></span>"
        )
        return anchor_open + rendered + anchor_close

    def render_callout(self, line, block):
        zid = block.get("zoneId", "")
        emoji = CALLOUT_EMOJI.get(block.get("calloutEmojiId"), "")
        bg = block.get("calloutBackgroundColor")
        bd = block.get("calloutBorderColor")
        bg_style = ("background-color:%s;" % bg) if bg else (
            "background-color:var(--ccmtoken-doc-highlightcolor-bg-orange-soft);"
        )
        bd_style = ("border:1px solid %s;" % bd) if bd else (
            "border:1px solid var(--ccmtoken-doc-blockbackground-orange-solid);"
        )
        child_lines = self.decode_zone(zid)
        child_html = "".join(
            self.render_line_inner(child, extra_line_class="callout-line", in_zone=True)
            for child in child_lines
        )
        return (
            '<span class="zone-plugin-region calloutBlock-zoneId-%s zoneType-calloutBlock" '
            'data-leaf="true"><div class="callout-container">'
            '<span data-zero-space="true" class="not-display-enter">%s</span>'
            '<div class="ignore-dom callout-emoji-icon callout-emoji-icon-disabled" '
            'style="top:18px"><span class="emoji-mart-emoji emoji-mart-emoji-native">'
            '<span style="font-size:20px">%s</span></span></div>'
            '<div class="callout-block" data-zone-container="*" style="%s%scolor:unset">'
            '<div class="zone-scroll-container"><div class="callout-cursor-mount-point">'
            '<div class="adit-container zoneId-%s ace-editor" data-zone-id="%s" '
            'contenteditable="false" data-gramm="false" style="outline:none">'
            "%s"
            "</div></div></div></div></div></span>"
        ) % (esc(zid), ZERO_WIDTH, emoji, bg_style, bd_style, esc(zid), esc(zid), child_html)

    def render_table_mount(self, line, block):
        ids = block["aceTable"].split()
        rows_id = ids[0]
        cols_id = ids[1] if len(ids) > 1 else ""
        row_frags = self.rows.get(rows_id, [])
        col_frags = self.cols.get(cols_id, [])
        # colWidth attrs are repeated on the marker line, in column order
        widths = [v for k, v in line.get("block_pairs", []) if k == "colWidth"]
        if not widths:
            widths = ["120"] * len(col_frags)
        colgroup = "".join('<col width="%s">' % esc(w) for w in widths)
        rows_html = []
        for r_frag in row_frags:
            cells = []
            for c_frag in col_frags:
                cell_zone = "x%sx%s" % (r_frag, c_frag)
                cells.append(self.render_table_cell(cell_zone))
            rows_html.append('<tr class="ace-table-row">%s</tr>' % "".join(cells))
        width_attr = ";".join(widths)
        table = (
            '<div class="ace-table-wrapper-outer table-toolbar-perf-adaptor">'
            '<span data-zero-space="true" class="not-display-enter">%s</span>'
            '<div class="ace-table-wrapper"><div class="ace-table-wrapper-inner '
            'tb-scrollable tb-scrollable-with-scrollbar">'
            '<div class="tb-scrollable-content" style="overflow-x:auto">'
            '<div class="tb-scrollable-children" style="width:fit-content;position:relative">'
            '<table class="ace-table" data-zone-container="*" data-ace-table-col-widths="%s">'
            "<colgroup>%s</colgroup><tbody>%s</tbody></table>"
            "</div></div></div></div></div>"
        ) % (ZERO_WIDTH, esc(width_attr), colgroup, "".join(rows_html))
        classes = "aceTable-%s %s ace-table-mount-point table-id-%s" % (
            esc(rows_id),
            esc(cols_id),
            esc("%s-%s" % (rows_id, cols_id)),
        )
        return (
            '<span class="%s" data-leaf="true">%s</span>' % (classes, table)
        )

    def render_table_cell(self, zone_id):
        cell_lines = self.decode_zone(zone_id)
        inner = "".join(
            self.render_zone_line_plain(child) for child in cell_lines
        )
        return (
            '<td class="ace-table-cell" contenteditable="false" data-gramm="false">'
            '<div class="inner-zone-ace-editor-wrapper" data-zone-type="aceTableCell" '
            'data-ace-inner-zone-content-wrapper-zone-id="%s" contenteditable="false">'
            '<div class="adit-container zoneId-%s ace-editor" data-zone-id="%s" '
            'data-gramm="false" style="outline:none">%s</div></div>'
            '<div class="ep-ranges ignore-dom"></div><div class="ep-cursors ignore-dom"></div></td>'
        ) % (esc(zone_id), esc(zone_id), esc(zone_id), inner)

    def render_zone_line_plain(self, line):
        """Table cell / zone child line: plain ace-line (no wrapper class)."""
        block = line["block"]
        classes = ["ace-line"]
        if block.get("align"):
            classes.append("align-%s" % block["align"])
        if line.get("guid"):
            classes.append("lineguid-%s" % line["guid"])
        line_id = self.next_id()
        content = self.render_segments(line["segs"])
        return (
            '<div id="%s" class="%s" data-node="true"><div data-line-wrapper="true" dir="auto">'
            "%s%s</div></div>"
            % (line_id, " ".join(classes), self._pocket(), content or self._empty_span(line))
        )

    # ---- document -----------------------------------------------------

    def render_document(self):
        text = self.ccv["initialAttributedText"]["text"]
        ops = self.ccv["initialAttributedText"]["attribs"]
        lines = decode_attributed_text(text, ops, self.attr_pool)
        html_parts = [self.render_line_inner(line) for line in lines]
        return "".join(html_parts)


# ===========================================================================
# Resource store (HTTP -> data URL, deduplicated)
# ===========================================================================

class ResourceStore:
    def __init__(self, session, source_url=""):
        self.session = session
        self.source_url = source_url
        self.embedded = {}
        self._cache = {}
        self._pending = {}
        self._once = {}  # url -> css class name (embedded once)
        self.pool = ThreadPoolExecutor(max_workers=8)

    def prefetch(self, urls):
        for url in urls:
            if url and url not in self._cache and url not in self._pending:
                self._pending[url] = self.pool.submit(self._fetch, url)

    def _fetch(self, url):
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code != 200 or not resp.content:
                return url
            ctype = resp.headers.get("content-type", "").split(";")[0] or "image/png"
            if ctype == "image/webp" or not ctype.startswith("image/"):
                # keep declared type conservative; feishu previews are png/jpeg
                if not ctype.startswith("image/"):
                    ctype = "image/png"
            b64 = base64.b64encode(resp.content).decode("ascii")
            return "data:%s;base64,%s" % (ctype, b64)
        except requests.RequestException:
            return url

    def get_image(self, url):
        if not url:
            return url
        if url in self._cache:
            return self._cache[url]
        future = self._pending.get(url)
        if future is None:
            future = self.pool.submit(self._fetch, url)
            self._pending[url] = future
            result = future.result()
            self._cache[url] = result
            return result
        return future.result()

    def once_class(self, url):
        """Embed a frequently-reused resource (e.g. avatar) exactly once.

        Returns a CSS class whose background-image is the data URL; the
        resulting rules are emitted by :meth:`once_style_rules`.
        """
        if not url:
            return ""
        if url not in self._once:
            import hashlib
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
            self._once[url] = "ld-bin-%s" % digest
        return self._once[url]

    def once_style_rules(self):
        rules = []
        for url, cls in self._once.items():
            data = self.get_image(url)
            rules.append(".%s{background-image:url(%s)!important}" % (cls, data))
        return "\n".join(rules)

    def shutdown(self):
        for future in list(self._pending.values()):
            future.result()
        self.pool.shutdown(wait=True)


# ===========================================================================
# Comments
# ===========================================================================

_AT_RE = re.compile(r"<at\b([^>]*)>(.*?)</at>", re.S)
_ATTR_RE = re.compile(r'([a-zA-Z_:-]+)\s*=\s*"([^"]*)"')


def _format_comment_time(create_time, modified):
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(create_time or ""))
    text = ""
    if match:
        text = "%s年%d月%d日" % (match.group(1), int(match.group(2)), int(match.group(3)))
    if modified:
        text += "（编辑过）"
    return text


def _render_comment_content(raw):
    """Escape comment text while turning <at> mention tags into links/spans."""
    # the API double-encodes a few entities (&#x2F; -> '/', &gt; -> '>');
    # normalise first so esc() below does not turn them into visible text
    raw = html.unescape(raw or "")
    out = []
    pos = 0
    for match in _AT_RE.finditer(raw or ""):
        out.append(esc(raw[pos:match.start()]).replace("\n", "<br>"))
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        label = esc(match.group(2))
        href = attrs.get("href", "")
        at_type = attrs.get("type", "0")
        if href:
            out.append(
                '<a class="ld-fs-at at-link" href="%s" target="_blank" '
                'rel="noopener noreferrer">%s</a>' % (esc(href), label)
            )
        else:
            out.append('<span class="ld-fs-at at-user" data-at-type="%s">%s</span>' % (
                esc(at_type), label))
        pos = match.end()
    out.append(esc(raw[pos:]).replace("\n", "<br>"))
    return "".join(out)


def render_comment_card(thread, resources):
    cid = thread["comment_id"]
    quote = esc(html.unescape(thread.get("quote") or "")).replace("\n", "<br>")
    replies = [r for r in (thread.get("comment_list") or []) if not r.get("delete_flag")]

    replies_html = []
    for reply in replies:
        name = esc(reply.get("name") or "匿名用户")
        time_text = _format_comment_time(reply.get("create_time"), reply.get("modify"))
        content = _render_comment_content(reply.get("content") or "")
        images = []
        for token in (reply.get("extra") or {}).get("image_list") or []:
            url = PREVIEW_URL.format(token=token)
            data = resources.get_image(url)
            images.append(
                '<img class="ld-fs-comment-img" src="%s" alt="">' % esc(data)
            )
        avatar_url = reply.get("avatar_url") or ""
        avatar = ""
        if avatar_url:
            cls = resources.once_class(avatar_url)
            avatar = (
                '<span class="avatar_inner__1V31t avatar_inner_source '
                'ld-fs-avatar-img %s" role="img"></span>' % cls
            )
        replies_html.append(
            '<div class="reply__xKr71 card-panel-reply reply__show__TMPgF ld-fs-reply">'
            '<div class="reply__main__pwHdW">'
            '<div class="avatar__kA9Cb ld-fs-avatar">%s</div>'
            '<div class="reply__main__right__ahmlT">'
            '<div class="reply__main__right__info__0dw-I">'
            '<div class="reply__main__right__info__xxx__oy9MU">'
            '<div class="reply__main__right__info__text__1Vwgp">'
            '<span class="reply__main__right__info__text__name__NUwFx reply-info-text-name ld-fs-name">%s</span>'
            '<span class="reply__main__right__info__text__time__ZkL77 ld-fs-time">%s</span>'
            "</div></div></div>"
            '<div style="margin-top:4px"><div class="reply-content__text__rGojE reply-content-source__text">'
            '<div class="js-reply-content reply-content__text__sub-wrapper__LC8S2">'
            '<div class="reply-content__text__main__cOeNj"><div class="inner-text__lE0U4">'
            '<div class="inner-text__outer-wrapper__eA1-B"><div>'
            '<div class="js-reply-content-line inner-text__line-wrapper__100MJ '
            'inner-text__line-wrapper-expanded__OuB1B ld-fs-content">%s%s</div>'
            "</div></div></div></div></div></div></div>"
            "</div></div></div>"
            % (avatar, name, time_text, content, "".join(images))
        )

    quote_html = ""
    if quote:
        quote_html = (
            '<div class="comment-panel__header__bGoxU comment-panel__header-v2__8If-D ld-fs-card-header">'
            '<div class="comment-panel__header__quote__C3A82 ld-fs-quote">'
            "<span>%s</span></div></div>" % quote
        )
    return (
        '<div class="js-panel-card comment-panel__K6rtJ comment-panel--readonly__TZtxN '
        'comment-panel--absolute__eV768 comment-panel__disable-transition__iFNKT ld-fs-card" '
        'data-id="%s" data-commenttype="0">%s'
        '<div class="reply-list_%s ld-fs-reply-list"><div>%s</div></div></div>'
        % (esc(cid), quote_html, esc(cid), "".join(replies_html))
    )


def split_threads(comment_data):
    threads = comment_data.get("comments") or {}
    all_threads = [t for t in threads.values() if not t.get("delete_flag")]
    anchored = [t for t in all_threads if not t.get("is_whole")]
    whole = [t for t in all_threads if t.get("is_whole")]
    return all_threads, anchored, whole


def render_comment_panel(comment_data, body_order, resources):
    """Right panel: anchored threads only, y-aligned to body by STATIC_SCRIPT."""
    all_threads, anchored, _whole = split_threads(comment_data)
    if not anchored:
        return ""

    position = {cid: i for i, cid in enumerate(body_order)}
    anchored.sort(key=lambda t: position.get(t["comment_id"], len(position) + 1))

    anchored_slots = []
    for thread in anchored:
        card = render_comment_card(thread, resources)
        anchored_slots.append(
            '<div class="ld-fs-card-slot" data-id="%s">%s</div>'
            % (esc(thread["comment_id"]), card)
        )

    panel = (
        '<div class="styles__DocCommentContainer-fMFYpS diwsJc doc-comment-v2 ld-fs-panel" '
        'style="overflow:visible!important;height:auto!important">'
        '<div class="ld-fs-panel-header styles__DocCommentSticky-dCcvFK eVZnmJ">'
        '<div class="styles__DocCommentHeader-dUlNkO jwCxbN doc-comment-v2-header">'
        '<span class="comment-side-header-left-text"><span>评论（%d）</span></span>'
        '<div class="comment-side-header-right"></div></div></div>'
        '<div class="styles__DocCommentContainerInner-kjlkBk dBigYn">'
        '<div class="comment-container__8vLlN ld-fs-track" '
        'style="opacity:1;min-height:0;overflow:visible!important;height:auto!important">'
        '%s</div></div></div>'
        % (len(anchored), "".join(anchored_slots))
    )
    return panel


def render_global_comments(comment_data, resources):
    """Whole-document comments: native places them BELOW #innerdocbody."""
    _all, _anchored, whole = split_threads(comment_data)
    if not whole:
        return ""
    cards = []
    for thread in whole:
        card = render_comment_card(thread, resources)
        cards.append(
            '<div class="ld-global-item" data-id="%s">%s</div>'
            % (esc(thread["comment_id"]), card)
        )
    return (
        '<div id="globalComment" class="global-comment ld-global">'
        '<div class="ld-global-title"><span>全文评论</span></div>'
        '%s</div>' % "".join(cards)
    )


# ===========================================================================
# New /docx block_map renderer
# ===========================================================================

class DocxBlockRenderer(ModelRenderer):
    """Render the new block_map document model (clientVars.data.block_map).

    Text blocks still carry a tiny Etherpad attributed text per block, so the
    inherited segment renderer / decoder are reused.
    """

    HEADING_TYPES = {"heading1": 1, "heading2": 2, "heading3": 3,
                     "heading4": 4, "heading5": 5, "heading6": 6,
                     "heading7": 7, "heading8": 8, "heading9": 9}

    def __init__(self, data, resource_store):
        self.data = data
        self.block_map = data["block_map"]
        self.resources = resource_store
        self.dom_seq = 0
        self.comment_order = []
        self._comment_seen = set()
        self.heading_guids = []      # block ids of headings (catalogue anchors)
        self.heading_records = []   # (id, level, plain text)
        self._ordered_counters = {}

    def block(self, bid):
        return self.block_map.get(bid, {}).get("data", {})

    # ---- per-block attributed text -----------------------------------

    def block_lines(self, block):
        text_model = block.get("text") or {}
        iats = text_model.get("initialAttributedTexts") or {}
        texts = iats.get("text") or {}
        ops_map = iats.get("attribs") or {}
        raw_pool = (text_model.get("apool") or {}).get("numToAttrib") or {}
        pool = {int(k): tuple(v) for k, v in raw_pool.items()}
        raw = texts.get("0") or ""
        ops = ops_map.get("0") or ""
        if not ops:
            return [{"text": raw, "segs": [(raw, {})], "block": {}, "guid": None}]
        return decode_attributed_text(raw, ops, pool)

    def render_zone(self, block, extra_class=""):
        align = block.get("align") or ""
        lines = self.block_lines(block)
        # comment anchors wrap the whole line for text-bearing blocks
        anchor_open = anchor_close = ""
        for cid in block.get("comments") or []:
            anchor_open += '<span class="comment-id-%s">' % cid
            anchor_close += "</span>"
            if cid not in self._comment_seen:
                self._comment_seen.add(cid)
                self.comment_order.append(cid)
        line_html = []
        for line in lines:
            segs = []
            for text, attrs in line["segs"]:
                # new model names text colour "textHighlight"
                if "textHighlight" in attrs and "textcolor" not in attrs:
                    attrs = dict(attrs)
                    attrs["textcolor"] = attrs.pop("textHighlight")
                segs.append((text, attrs))
            inner = self.render_segments(segs)
            line_html.append(
                '<div class="ace-line" data-node="true" dir="auto">%s%s%s</div>'
                % (anchor_open, inner, anchor_close)
            )
        cls = "zone-container text-editor non-empty %s" % extra_class
        return (
            '<div class="%s" style="text-align:%s" data-slate-editor="true" '
            'contenteditable="false">%s</div>'
            % (cls, align, "".join(line_html))
        )

    # ---- block dispatch ----------------------------------------------

    def render_children(self, block):
        return "".join(
            self.render_block(cid)
            for cid in (block.get("children") or [])
            if cid in self.block_map
        )

    def render_nested_children(self, block):
        if not block.get("children"):
            return ""
        return '<div class="render-unit-wrapper ld-nested-blocks">%s</div>' % self.render_children(block)

    def render_block(self, bid, block=None):
        if bid not in self.block_map:
            return ""  # dangling id (some SSRs pollute children arrays)
        block = self.block(bid)
        btype = block.get("type", "text")
        method = getattr(self, "render_%s" % btype, None)
        if method:
            return method(bid, block)
        return self.render_text_block(bid, block)

    def _wrap(self, btype, bid, inner, extra_class=""):
        return (
            '<div data-block-type="%s" data-record-id="%s" '
            'class="block docx-%s-block %s">%s</div>'
            % (btype, esc(bid), btype, extra_class, inner)
        )

    def render_text_block(self, bid, block):
        inner = (
            '<div class="highlight-container-ssr"><div class="text-block-wrapper">'
            '<div class="text-block">%s</div></div></div>' % self.render_zone(block)
        )
        return self._wrap("text", bid, inner + self.render_nested_children(block))

    def render_heading(self, bid, block, level):
        plain = "".join(ln["text"] for ln in self.block_lines(block))
        self.heading_guids.append(bid)
        self.heading_records.append((bid, level, plain))
        align = block.get("align") or ""
        folded_cls = " ld-folded" if block.get("folded") else ""
        fold = (
            '<div class="fold-wrapper can-fold" contenteditable="false">'
            '<div class="fold-wrapper fold-handler-wrapper">'
            '<button type="button" class="fold-handler ld-fold-handler" '
            'aria-label="折叠或展开章节" aria-expanded="%s"><span class="svg-wrapper">'
            '<svg width="16" height="16" viewBox="0 0 16 16" fill="none">'
            '<path d="M7.712 11.351L3.34 5.9a.45.45 0 010-.538.278.278 0 '
            '01.215-.112h8.89c.168 0 .305.17.305.381a.432.432 0 '
            '01-.09.269l-4.372 5.451c-.159.199-.417.199-.576 0z" '
            'fill="currentColor"/></svg></span></button></div></div>'
            % ("false" if block.get("folded") else "true")
        )
        inner = (
            '<div class="highlight-container-ssr"><div class="heading-block">'
            '<div class="heading heading-h%d heading-block-align-%s">'
            '<div class="heading-content">%s</div></div></div></div>%s'
            % (level, align or "", self.render_zone(block), fold)
        )
        return (
            '<div data-block-type="heading%d" data-level="%d" data-record-id="%s" id="%s" '
            'class="block docx-heading%d-block%s">%s</div>'
            % (level, level, esc(bid), esc(bid), level, folded_cls, inner)
        )

    def render_list_block(self, bid, block, ordered):
        level = int(block.get("indentation_level") or 0)
        if ordered:
            num = self._ordered_counters.get(level, 0) + 1
            self._ordered_counters[level] = num
            marker = (
                '<div class="order"><span class="order-number">%s.</span></div>' % num
            )
            btype, cls = "ordered", "list-order"
        else:
            marker = (
                '<div class="bullet"><span class="bullet-dot-style">•</span></div>'
            )
            btype, cls = "bullet", "list-bullet"
        inner = (
            '<div class="list-wrapper"><div class="list %s indentation-level-%d">'
            '%s<div class="list-content">%s</div></div></div>'
            % (cls, level, marker, self.render_zone(block))
        )
        return self._wrap(btype, bid, inner + self.render_nested_children(block))

    def render_bullet(self, bid, block):
        return self.render_list_block(bid, block, False)

    def render_ordered(self, bid, block):
        return self.render_list_block(bid, block, True)

    def render_todo(self, bid, block):
        done = "task-done" if block.get("style", {}).get("done") else ""
        inner = (
            '<div class="todo-block %s"><div class="list-wrapper">'
            '<div class="list list-todo"><div class="todo-checkbox">'
            '<span class="todo-checkbox-icon">%s</span></div>'
            '<div class="list-content">%s</div></div></div></div>'
            % (done, "✓" if done else "", self.render_zone(block))
        )
        return self._wrap("todo", bid, inner + self.render_nested_children(block))

    def render_code(self, bid, block):
        inner = (
            '<div class="code-block"><div class="code-block-content">%s</div></div>'
            % self.render_zone(block, "code-text-editor")
        )
        return self._wrap("code", bid, inner)

    def render_quote_container(self, bid, block):
        inner = (
            '<div class="quote-container-block">%s</div>'
            % self.render_children(block)
        )
        return self._wrap("quote_container", bid, inner)

    def render_callout(self, bid, block):
        emoji = CALLOUT_EMOJI.get(block.get("emoji_id", ""), "\U0001f4a1")
        bg = block.get("background_color") or "rgb(255,245,235)"
        bd = block.get("border_color") or "rgb(254,212,164)"
        inner = (
            '<div class="callout-block" style="background-color:%s;border:1px solid %s;'
            'border-radius:8px;padding:12px;display:flex;align-items:flex-start;gap:8px">'
            '<div class="callout-block-emoji" style="font-size:20px;line-height:1.4">%s</div>'
            '<div class="callout-block-children" style="flex:1 1 auto;min-width:0">%s</div>'
            '</div>' % (bg, bd, emoji, self.render_children(block))
        )
        return self._wrap("callout", bid, inner)

    GRID_GAP = 36  # px column gap, matches native SSR

    def render_grid(self, bid, block):
        child_ids = [c for c in (block.get("children") or []) if c in self.block_map]
        n = max(len(child_ids), 1)
        total_gap = self.GRID_GAP * (n - 1)
        cols = []
        for cid in child_ids:
            col = self.block(cid)
            ratio = float(col.get("width_ratio") or 1.0 / n)
            cols.append(
                '<div data-block-type="grid_column" data-record-id="%s" '
                'class="block docx-grid_column-block" '
                'style="flex:0 0 calc((100%% - %dpx) * %.6f)">'
                '<div class="grid-column-block"><div class="render-unit-wrapper">%s</div>'
                '</div></div>' % (esc(cid), total_gap, ratio, self.render_children(col))
            )
        inner = (
            '<div class="grid-block j-grid-block grid-horizontal">'
            '<div class="render-unit-wrapper grid-render-unit" '
            'style="display:flex;gap:%dpx;width:100%%">%s</div></div>'
            % (self.GRID_GAP, "".join(cols))
        )
        return self._wrap("grid", bid, inner)

    def render_grid_column(self, bid, block):  # rendered via render_grid
        return self._wrap("grid_column", bid, self.render_children(block))

    # ---- native docx table (table + table_cell via cell_set) ---------

    def render_table(self, bid, block):
        cols = block.get("columns_id") or []
        rows = block.get("rows_id") or []
        col_set = block.get("column_set") or {}
        cell_set = block.get("cell_set") or {}
        colgroup = "".join(
            '<col width="%d"/>' % int(
                float((col_set.get(cid) or {}).get("column_width") or 160)
            )
            for cid in cols
        )
        trs = []
        for r_idx, rid in enumerate(rows):
            tds = []
            for cid in cols:
                cell_info = cell_set.get(rid + cid) or cell_set.get(cid + rid) or {}
                merge = cell_info.get("merge_info") or {}
                row_span = int(merge.get("row_span") or 1)
                col_span = int(merge.get("col_span") or 1)
                cell_id = cell_info.get("block_id")
                cell_html = self.render_block(cell_id) if cell_id in self.block_map else ""
                first_row = " first-row" if r_idx == 0 else ""
                tds.append(
                    '<td data-block-type="table_cell" data-record-id="%s" '
                    'rowspan="%d" colSpan="%d" contenteditable="false" '
                    'class="block docx-table_cell-block table-cell-block '
                    'table-cell-content-wrapper%s"><div class="render-unit-wrapper">%s'
                    '</div></td>'
                    % (esc(cell_id or ""), row_span, col_span, first_row, cell_html)
                )
            trs.append(
                '<tr class="docx-table-tr%s" data-index="%d">%s</tr>'
                % (" first-row" if r_idx == 0 else "", r_idx, "".join(tds))
            )
        inner = (
            '<div class="table-block docx-table-inner-wrapper">'
            '<div class="table-scrollable-content"><div class="table-content-padding">'
            '<table class="table"><colgroup>%s</colgroup><tbody>%s</tbody></table>'
            '</div></div></div>' % (colgroup, "".join(trs))
        )
        return self._wrap("table", bid, inner)

    def render_table_cell(self, bid, block):  # rendered via render_table
        return self._wrap("table_cell", bid, self.render_children(block))

    # ---- embedded objects and attachment cards -----------------------

    def _embedded_placeholder(self, bid, block, label):
        source = getattr(self.resources, "source_url", "").split("#")[0]
        inner = (
            '<div class="ld-embedded-placeholder">'
            '<span class="ld-embedded-icon">▦</span>'
            '<a class="ld-embedded-label" href="%s#%s">%s：未能加载，查看原文</a></div>'
            % (esc(source), esc(bid), esc(label))
        )
        return self._wrap(block.get("type", "embed"), bid, inner)

    def render_embedded(self, bid, block, label):
        preview = getattr(self.resources, "embedded", {}).get(bid)
        if not preview:
            return self._embedded_placeholder(bid, block, label)
        inner = (
            '<figure class="ld-embedded-preview"><img class="ld-embedded-image" '
            'src="%s" alt="%s" style="width:%spx;max-width:100%%;height:auto"></figure>'
            % (esc(preview["src"]), esc(label), esc(preview["width"]))
        )
        return self._wrap(block["type"], bid, inner)

    def render_sheet(self, bid, block):
        return self.render_embedded(bid, block, "内嵌电子表格")

    def render_whiteboard(self, bid, block):
        return self.render_embedded(bid, block, "内嵌白板")

    def render_bitable(self, bid, block):
        return self.render_embedded(bid, block, "内嵌多维表格")

    render_task_list = render_bitable

    def render_isv(self, bid, block):
        settings = block.get("data") or {}
        if "showCataLogLevel" not in settings:
            return self.render_embedded(bid, block, "内嵌应用")
        # The public catalogue app stores its options in the block model.
        # Render its links directly so the complete outline stays selectable.
        max_level = 9 if settings.get("isShowAllLevel") else settings["showCataLogLevel"]
        ignored = set(settings.get("ignoreCataLogRecordIds") or [])
        items = []
        for child in self.block(self.data["id"]).get("children") or []:
            heading = self.block(child)
            level = self.HEADING_TYPES.get(heading.get("type"), 0)
            if not level or level > max_level or child in ignored:
                continue
            title = "".join(line["text"] for line in self.block_lines(heading)).strip()
            items.append('<li style="margin-left:%dpx"><a href="#%s">%s</a></li>'
                         % ((level - 1) * 16, esc(child), esc(title)))
        return self._wrap("isv", bid, '<nav class="ld-inline-catalogue"><ul>%s</ul></nav>' % "".join(items))

    def render_view(self, bid, block):
        return self._wrap("view", bid, self.render_children(block))

    def render_file(self, bid, block):
        file = block.get("file") or {}
        source = getattr(self.resources, "source_url", "").split("#")[0]
        inner = '<a class="ld-file-card" href="%s#%s">📄 %s</a>' % (
            esc(source), esc(bid), esc(file.get("name") or "附件"),
        )
        return self._wrap("file", bid, inner)

    def render_divider(self, bid, block):
        inner = (
            '<div class="divider-block" style="line-height:10px">'
            '<div style="border-top:1px solid var(--line-divider-default,#dee0e3);'
            'margin:6px 0"></div></div>'
        )
        return self._wrap("divider", bid, inner)

    def render_image(self, bid, block):
        img = block.get("image") or {}
        token = img.get("token") or ""
        nat_w = float(img.get("width") or 400)
        nat_h = float(img.get("height") or nat_w * 0.6)
        width = int(min(nat_w, 730))
        ratio = nat_h / nat_w * 100
        src = PREVIEW_URL.format(token=token) if token else ""
        data_url = self.resources.get_image(src) if src else ""
        align = block.get("align") or "left"
        comments = block.get("comments") or (img.get("area_comments") or [])
        anchor_open = anchor_close = ""
        for cid in comments:
            anchor_open += (
                '<span class="comment-local-render-%s comment-id-%s ld-img-anchor">'
                % (cid, cid)
            )
            anchor_close += "</span>"
            if cid not in self._comment_seen:
                self._comment_seen.add(cid)
                self.comment_order.append(cid)
        inner = (
            '<div class="image-block align-%s" contenteditable="false" image-token="%s">'
            '<div class="image-block-width-wrapper" style="width:%dpx;max-width:100%%">'
            '<div class="image-block-container" style="width:100%%;padding-top:%.4f%%;position:relative">'
            '<div class="resizable-wrapper" contenteditable="false" '
            'style="position:absolute;inset:0">'
            '<div class="img ssr" style="width:100%%;height:100%%">'
            '<img class="docx-image" draggable="false" '
            'src="%s" style="width:100%%;height:100%%;display:block;object-fit:contain">'
            '</div></div></div></div></div>'
            % (align, esc(token), width, ratio, esc(data_url or src))
        )
        return self._wrap("image", bid, anchor_open + inner + anchor_close)

    def render_page(self, bid, block):  # root is handled by render_document
        return self.render_children(block)

    def render_document(self):
        page_id = self.data.get("id") or next(
            (bid for bid, b in self.block_map.items()
             if b.get("data", {}).get("type") == "page"), "")
        # AroundV2 SSR includes both the start and end of a long document. Its
        # sequence is only a slice; the completed page.children gives real order.
        top_ids = self.block(page_id).get("children") or [
            bid for bid in self.data.get("block_sequence", [])
            if bid in self.block_map
            and self.block_map[bid]["data"].get("parent_id") == page_id
        ]
        return "".join(self.render_block(cid) for cid in top_ids)

    def build_catalogue(self):
        """Left section-nav built from heading records (native classes)."""
        title = "".join(line["text"] for line in self.block_lines(self.block(self.data.get("id"))))
        items = [
            '<li class="full-entry r-title"><a class="full-entry-title" '
            'href="#ld-document-start">%s</a></li>' % esc(title.strip())
        ] if title.strip() else []
        for bid, level, text in self.heading_records:
            items.append(
                '<li class="full-entry r-heading heading-h%d indentation-level-%d">'
                '<a class="full-entry-title" href="#%s">%s</a></li>'
                % (level, min(level, 3), esc(bid), esc(text.strip()))
            )
        if not items:
            return ""
        return (
            '<div class="section-nav-container doc-selection-nav">'
            '<div class="section-nav showing-full">'
            '<div class="entries-container"><ul class="full-entries">%s</ul></div>'
            '</div></div>' % "".join(items)
        )


# headingN methods generated from HEADING_TYPES
for _level in range(1, 10):
    def _make(lvl):
        def fn(self, bid, block):
            return self.render_heading(bid, block, lvl)
        return fn
    setattr(DocxBlockRenderer, "render_heading%d" % _level, _make(_level))


# ===========================================================================
# Static styles
# ===========================================================================

OVERRIDE_STYLE = """
/* linkding model-driven snapshot: force static three-column layout */
html,body{overflow:visible!important;height:auto!important}
#ssrBox{position:static!important;height:auto!important;-webkit-user-select:text!important;user-select:text!important}
/* Theme variables are normally installed by viewer JS; use a literal fallback
   so selection remains visible in both /docs and /docx offline snapshots. */
html body ::selection{background:rgba(51,112,255,.34)!important;color:inherit!important}
/* text must stay selectable without the viewer JS (SSR sets user-select:none) */
#innerdocbody,#innerdocbody *,.etherpad-container *,.section-nav *,.ld-fs-panel *,#globalComment *{
  -webkit-user-select:text!important;user-select:text!important}
#mainBox,#mainContainer,.app,.app-main-container,.app-main,.suite-body{position:static!important;height:auto!important;overflow:visible!important;padding-right:0!important;margin-right:0!important}
.app{min-height:100vh!important}
.etherpad-container-wrapper{display:flex!important;flex-direction:row;align-items:flex-start;overflow:visible!important;height:auto!important;max-height:none!important;padding-right:0!important;box-sizing:border-box}
.etherpad-container{flex:1 1 auto;min-width:0}
/* left catalogue: sticky column with its own scroll area */
.section-nav-container,.doc-selection-nav{position:sticky!important;top:72px!important;float:none!important;height:auto!important;
 flex:0 0 260px!important;width:260px!important;max-width:260px!important;min-width:0!important;margin:0!important;
 align-self:flex-start;display:block!important;z-index:5}
.section-nav{position:static!important;width:100%!important;max-width:260px!important;min-width:0!important;
 margin:0!important;padding:0!important;left:auto!important;height:auto!important;
 max-height:calc(100vh - 96px)!important;overflow:hidden!important}
.section-nav .entries-container{position:static!important;height:auto!important;max-height:calc(100vh - 96px)!important;overflow-y:auto!important;overflow-x:hidden!important}
.section-nav .full-entries{position:static!important;display:block!important;z-index:auto!important;margin:0!important;padding:8px 10px 20px!important}
.full-entry{height:auto!important;min-height:24px;line-height:1.5!important;margin-bottom:2px!important}
.full-entry-title{white-space:normal!important;height:auto!important;display:block!important;line-height:1.5!important;padding:2px 4px!important;border-radius:4px}
.full-entry-title:hover{background:rgba(31,35,41,.06)}
#innerdocbodyWrap,#innerdocbody,.outerdocbody{overflow:visible!important;height:auto!important}
#innerdocbody{min-height:70vh!important}
/* Keep anchored cards in document flow so they scroll with their paragraphs. */
.ld-fs-panel{flex:0 0 380px!important;width:380px!important;max-width:380px!important;margin:0 0 0 12px!important;
 position:relative!important;top:auto!important;align-self:flex-start;padding:0!important}
.ld-fs-panel-header{position:sticky;top:0;z-index:6;background:#fff;height:48px;
 display:flex;align-items:center;padding:0 14px;margin-bottom:8px;
 border-bottom:1px solid #e5e6eb;box-sizing:border-box}
.ld-fs-panel-header .doc-comment-v2-header{height:48px;display:flex;align-items:center;
 width:100%;font-size:15px;font-weight:500;color:#1f2329;background:transparent}
.ld-fs-panel-header .comment-side-header-left-text{font-size:15px;font-weight:500}
.ld-fs-track{position:relative!important}
.ld-fs-card-slot{margin-bottom:10px}
.ld-fs-track.ld-positioned .ld-fs-card-slot{position:absolute;left:0;right:0;margin:0}
.ld-fs-card{position:static!important;top:auto!important;left:auto!important;right:auto!important;
 transform:none!important;float:none!important;width:100%!important;
 visibility:visible!important;opacity:1!important;margin:0;padding:12px 14px;background:#fff;
 border:1px solid #dee0e3;border-radius:8px;box-sizing:border-box;box-shadow:0 1px 2px rgba(0,0,0,.04);
 font-size:14px;line-height:1.6;color:#1f2329;overflow-wrap:anywhere;word-break:break-word;cursor:pointer}
.ld-fs-card a{white-space:normal!important;overflow-wrap:anywhere!important;word-break:break-word!important}
.ld-fs-card-header{margin-bottom:8px;cursor:default}
.ld-fs-quote{font-size:12px;line-height:1.5;color:#646a73;border-left:3px solid #ffd84d;padding-left:8px;
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.ld-fs-reply{padding:4px 0}
.ld-fs-reply+.ld-fs-reply{border-top:1px solid #f0f1f2;margin-top:8px;padding-top:8px}
.ld-fs-avatar{width:32px!important;height:32px!important;border-radius:50%;overflow:hidden;flex:0 0 auto;background:#bbbfc4;display:inline-block}
.ld-fs-avatar-img{width:32px;height:32px;border-radius:50%;background-size:cover;background-position:center;display:inline-block}
.ld-fs-reply .reply__main__pwHdW{display:flex;gap:8px}
.ld-fs-reply .reply__main__right__ahmlT{flex:1 1 auto;min-width:0}
.ld-fs-name{font-weight:500;color:#1f2329;margin-right:8px;font-size:13px}
.ld-fs-time{color:#8f959e;font-size:12px}
.ld-fs-content{white-space:normal}
.ld-fs-comment-img{display:block;max-width:100%;margin:6px 0;border-radius:6px}
/* whole-document comments live below the body in the middle column */
.ld-global{margin:32px 0 48px;padding-top:20px;border-top:1px solid #e5e6eb}
.ld-global-title{font-size:16px;font-weight:600;color:#1f2329;margin-bottom:12px;padding-left:8px}
.ld-global-item{margin-bottom:10px}
.ld-global-item .ld-fs-card{cursor:default;width:100%!important}
.ld-fs-at{color:#3370ff;text-decoration:none}
a.ld-fs-at:hover{text-decoration:underline}
.at-user{background:rgba(51,112,255,.08);border-radius:3px;padding:0 2px}
/* commented text highlight in the body */
#innerdocbody span[class*="comment-id-"],#innerdocbody span[class*="comment-resolved-"]{
 border-bottom:2px solid #ffd84d;cursor:pointer;border-radius:2px}
/* image anchors must stay inline-block or the gallery blows past the column */
#innerdocbody span.ld-img-anchor{display:inline-block!important;max-width:100%!important;
 border-bottom:none!important;vertical-align:top}
.ld-img-anchor>span>div>span>div>div>div img,
.ld-img-anchor .image-highlight-wrapper{max-width:100%}
.ld-img-anchor img{outline:2px solid #ffd84d;outline-offset:1px}
#innerdocbody span.ld-anchor-active{background:#fff3c4!important;box-shadow:0 0 0 2px #ffe148;border-radius:3px}
.ld-fs-card.ld-card-active{box-shadow:0 0 0 2px #3370ff!important}
/* table grid lines (native vars, with static fallback) */
.ace-table{border-collapse:collapse!important;border:1px solid var(--line-divider-default,rgba(31,35,41,.15))!important}
.ace-table-cell{border:1px solid var(--line-divider-default,rgba(31,35,41,.15))!important}
/* watermark never survives into a static snapshot */
.ssrWaterMark,[class*="watermark"]{display:none!important}
html{scroll-behavior:smooth}
""".replace("!2$s", "!important")


STATIC_SCRIPT = """
(function(){
  function ready(fn){
    if(document.readyState!=='loading'){fn();}
    else{document.addEventListener('DOMContentLoaded',fn);}
  }
  function anchorFor(id){
    return document.querySelector('.comment-id-'+id+',.comment-resolved-'+id);
  }
  function bodyRoot(){
    return document.querySelector('#innerdocbody')
      ||document.querySelector('.root-render-unit-wrapper')
      ||document.querySelector('.page-block-children');
  }
  /* heading collapse/expand (new /docx block model) */
  function sectionLevel(el){
    var m=el&&el.getAttribute&&(el.getAttribute('data-block-type')||'').match(/^heading(\\d)$/);
    return m?+m[1]:null;
  }
  function initFolds(){
    document.querySelectorAll('.page-block-children .render-unit-wrapper').forEach(function(root){
      var folded=[];
      [].forEach.call(root.children,function(el){
        var level=sectionLevel(el);
        if(level!==null){while(folded.length&&folded[folded.length-1]>=level){folded.pop();}}
        el.hidden=folded.length>0;
        if(level!==null){
          var collapsed=el.classList.contains('ld-folded');
          var button=el.querySelector('.ld-fold-handler');
          if(button){button.setAttribute('aria-expanded',String(!collapsed));}
          if(collapsed){folded.push(level);}
        }
      });
    });
  }
  function positionCover(){
    document.querySelectorAll('.ld-doc-cover').forEach(function(cover){
      var img=cover.querySelector('img'),wrapper=img.parentElement;
      if(!img.naturalWidth||!img.naturalHeight){return;}
      var scale=Math.max(cover.clientWidth/img.naturalWidth,cover.clientHeight/img.naturalHeight);
      var w=Math.round(img.naturalWidth*scale),h=Math.round(img.naturalHeight*scale);
      var x=Math.max(cover.clientWidth-w,Math.min(0,(cover.clientWidth-w)/2+(+cover.dataset.offsetX||0)*w));
      var y=Math.max(cover.clientHeight-h,Math.min(0,(cover.clientHeight-h)/2+(+cover.dataset.offsetY||0)*h));
      wrapper.style.cssText='width:'+w+'px;height:'+h+'px;margin-left:'+Math.round(x)+'px;margin-top:'+Math.round(y)+'px';
    });
  }
  function position(){
    var track=document.querySelector('.ld-fs-track');
    var body=bodyRoot();
    if(!track||!body){return;}
    var slots=[].slice.call(track.querySelectorAll('.ld-fs-card-slot'));
    if(!slots.length){return;}
    var base=track.getBoundingClientRect().top;
    var prevBottom=0;
    track.classList.add('ld-positioned');
    slots.forEach(function(slot){
      var id=slot.getAttribute('data-id');
      var anchor=anchorFor(id);
      var desired=anchor?anchor.getBoundingClientRect().top-base-6:prevBottom+8;
      var h=slot.offsetHeight;
      var y=Math.max(desired,prevBottom+8);
      slot.style.top=y+'px';
      prevBottom=y+h;
    });
    track.style.minHeight=(prevBottom+24)+'px';
  }
  function flash(el,cls){
    if(!el){return;}
    el.classList.add(cls);
    setTimeout(function(){el.classList.remove(cls);},1400);
  }
  ready(function(){
    initFolds();positionCover();position();
    window.addEventListener('resize',function(){positionCover();position();});
    document.querySelectorAll('img').forEach(function(img){img.addEventListener('load',function(){positionCover();position();});});
    if(document.fonts&&document.fonts.ready){document.fonts.ready.then(position);}
    setTimeout(position,300);setTimeout(position,1200);setTimeout(initFolds,300);
    document.addEventListener('click',function(e){
      var fold=e.target.closest&&e.target.closest('.ld-fold-handler');
      if(fold){
        var h=fold.closest('[data-block-type^="heading"]');
        if(h){h.classList.toggle('ld-folded');initFolds();position();}
        return;
      }
      if(window.getSelection().toString()){return;}
      var a=e.target.closest&&e.target.closest('span[class*="comment-id-"],span[class*="comment-resolved-"]');
      if(a){
        var m=a.className.match(/comment-(?:id|resolved)-([0-9]+)/);
        if(m){
          var card=document.querySelector('.ld-fs-card-slot[data-id="'+m[1]+'"]');
          if(card){card.scrollIntoView({block:'center',behavior:'smooth'});
            flash(card.querySelector('.ld-fs-card'),'ld-card-active');}
        }
        return;
      }
      if(e.target.closest&&e.target.closest('a')){return;}
      var slot=e.target.closest&&e.target.closest('.ld-fs-card-slot');
      if(slot){
        var anchor=anchorFor(slot.getAttribute('data-id'));
        if(anchor){anchor.scrollIntoView({block:'center',behavior:'smooth'});flash(anchor,'ld-anchor-active');}
      }
    });
  });
})();
"""


# ===========================================================================
# SSR shell surgery
# ===========================================================================

STYLESHEET_RE = re.compile(
    r'<link[^>]*rel=["\']?stylesheet["\']?[^>]*>', re.I
)
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
SCRIPT_SELF_RE = re.compile(r"<script\b[^>]*/>", re.I)
TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)\b[^>]*?(/?)>")


def _find_matching_close(html, open_start, tag):
    depth = 0
    for match in TAG_RE.finditer(html, open_start):
        name = match.group(2).lower()
        if name != tag:
            continue
        if match.group(1) == "/":
            depth -= 1
            if depth == 0:
                return match.start(), match.end()
        elif not match.group(3):
            depth += 1
    raise RuntimeError("no matching close for %s" % tag)


def inline_stylesheets(ssr_html, session, timeout=30):
    found = list(STYLESHEET_RE.finditer(ssr_html))

    def load(link_match):
        href_match = re.search(r'href=["\']?([^"\' >]+)', link_match.group(0))
        if not href_match:
            return ""
        href = href_match.group(1)
        if href.startswith("//"):
            href = "https:" + href
        try:
            resp = session.get(href, timeout=timeout)
            if resp.ok:
                return "<style>/* %s */\n%s</style>" % (href.split("/")[-1], resp.text)
        except requests.RequestException:
            pass
        return link_match.group(0)

    with ThreadPoolExecutor(max_workers=6) as pool:
        replacements = list(pool.map(load, found))
    out = ssr_html
    for match, repl in zip(reversed(found), reversed(replacements)):
        out = out[:match.start()] + repl + out[match.end():]
    return out


def _strip_watermark(html_doc):
    """Remove SSR watermark elements (repeated 'visitor' background)."""
    while True:
        match = re.search(r"<(\w+)\b[^>]*class=[\"'][^\"']*ssrWaterMark[^\"']*[\"'][^>]*>", html_doc)
        if not match:
            return html_doc
        close_start, close_end = _find_matching_close(html_doc, match.start(), match.group(1))
        html_doc = html_doc[:match.start()] + html_doc[close_end:]


def _link_catalogue(html_doc, heading_guids):
    """Point SSR catalogue entries at heading lineguids (first entry = top)."""
    targets = ["innerdocbody"] + heading_guids
    idx = {"i": 0}

    def repl(_match):
        i = idx["i"]
        idx["i"] += 1
        target = targets[i] if i < len(targets) else ""
        return '<a class="full-entry-title" href="#%s">' % target

    return re.sub(r'<a class="full-entry-title" href="#">', repl, html_doc)


def assemble_snapshot(ssr_html, body_html, panel_html, session, heading_guids=None,
                      global_html="", extra_css=""):
    heading_guids = heading_guids or []
    html_doc = inline_stylesheets(ssr_html, session)

    # 1. drop every script (static document: must not hydrate / wipe the body)
    html_doc = SCRIPT_RE.sub("", html_doc)
    html_doc = SCRIPT_SELF_RE.sub("", html_doc)
    # remove SSR visitor watermark
    html_doc = _strip_watermark(html_doc)
    # drop JS preload/prefetch hints (dead without the scripts, and protocol-
    # relative URLs fail under file://) and normalize remaining //-URLs
    html_doc = re.sub(
        r'<link\b[^>]*rel=["\']?(?:preload|prefetch|modulepreload|dns-prefetch|preconnect)["\']?[^>]*>',
        "",
        html_doc,
        flags=re.I,
    )
    html_doc = re.sub(r'(href|src)="//', r'\1="https://', html_doc)

    # 2. replace #innerdocbody children
    body_match = re.search(
        r'<div\b[^>]*id=["\']innerdocbody["\'][^>]*>', html_doc
    )
    if not body_match:
        raise RuntimeError("#innerdocbody not found in SSR")
    # whole-document comments ride along right after #innerdocbody (middle column)
    html_doc = _replace_contents(html_doc, body_match, body_html)
    if global_html:
        body_match2 = re.search(
            r'<div\b[^>]*id=["\']innerdocbody["\'][^>]*>', html_doc
        )
        close_start, close_end = _find_matching_close(
            html_doc, body_match2.start(), "div"
        )
        html_doc = html_doc[:close_end] + global_html + html_doc[close_end:]

    # 3. inject comment panel right after .etherpad-container inside wrapper
    # (class token must be exact: etherpad-container-wrapper shares the prefix
    # and must NOT match here)
    ep_match = None
    for candidate in re.finditer(r'<div\b[^>]*class=["\']([^"\']*)["\'][^>]*>', html_doc):
        tokens = candidate.group(1).split()
        if "etherpad-container" in tokens:
            ep_match = candidate
            break
    if ep_match:
        close_start, close_end = _find_matching_close(
            html_doc, ep_match.start(), "div"
        )
        html_doc = html_doc[:close_end] + panel_html + html_doc[close_end:]
        # mark wrapper as comment-open (harmless helper classes)
        html_doc = html_doc.replace(
            "etherpad-container-wrapper clearfix",
            "etherpad-container-wrapper has-comment-toggle clearfix has-comment",
            1,
        )

    # 4. expand the collapsed SSR catalogue and wire its anchors
    html_doc = re.sub(
        r'(class="section-nav showing-full"[^>]*?)style="[^"]*"',
        r'\1',
        html_doc,
        count=1,
    )
    html_doc = _link_catalogue(html_doc, heading_guids)

    # 5. override styles before </head>
    inject = "<style>%s\n%s</style>" % (OVERRIDE_STYLE, extra_css)
    head_close = html_doc.lower().find("</head>")
    if head_close >= 0:
        html_doc = html_doc[:head_close] + inject + html_doc[head_close:]
    else:
        html_doc = inject + html_doc

    # 6. our own static script: y-align comment cards + click navigation
    static_script = "<script>%s</script>" % STATIC_SCRIPT
    body_close = html_doc.lower().rfind("</body>")
    if body_close >= 0:
        html_doc = html_doc[:body_close] + static_script + html_doc[body_close:]
    else:
        html_doc += static_script
    return html_doc


def _replace_contents(html, open_match, inner_html):
    tag_match = re.match(r"<([a-zA-Z][\w-]*)", open_match.group(0))
    tag = tag_match.group(1).lower()
    open_end = open_match.end()
    close_start, close_end = _find_matching_close(html, open_match.start(), tag)
    return html[:open_end] + inner_html + html[close_start:]


def _clean_ssr(ssr_html, session):
    """Shared SSR prep: inline CSS, drop scripts/watermark/preloads."""
    html_doc = inline_stylesheets(ssr_html, session)
    html_doc = SCRIPT_RE.sub("", html_doc)
    html_doc = SCRIPT_SELF_RE.sub("", html_doc)
    html_doc = _strip_watermark(html_doc)
    html_doc = re.sub(
        r'<link\b[^>]*rel=["\']?(?:preload|prefetch|modulepreload|dns-prefetch|preconnect)["\']?[^>]*>',
        "",
        html_doc,
        flags=re.I,
    )
    html_doc = re.sub(r'(href|src)="//', r'\1="https://', html_doc)
    return html_doc


def assemble_docx_snapshot(ssr_html, body_html, panel_html, catalogue_html,
                           global_html, session, extra_css=""):
    """Assemble a snapshot for the new /docx block_map renderer shell."""
    html_doc = _clean_ssr(ssr_html, session)

    # 1. replace children of .page-block-children (keeps the page title header)
    root_match = re.search(
        r'<div\b[^>]*class=["\'][^"\']*\bpage-block-children\b[^"\']*["\'][^>]*>',
        html_doc,
    )
    if not root_match:
        raise RuntimeError("docx page-block-children not found in SSR")
    wrapped = (
        '<div class="root-render-unit-container">'
        '<div class="render-unit-wrapper">%s</div></div>%s'
        % (body_html, global_html)
    )
    html_doc = _replace_contents(html_doc, root_match, wrapped)

    # 2. The cover spans the page; the catalogue, document and comments follow
    #    in that order. Keep the native document shell and its typography.
    cover_html = ""
    cover_match = re.search(r'<div\b[^>]*class="ssr-cover\b[^>]*>', html_doc)
    if cover_match:
        _, end = _find_matching_close(html_doc, cover_match.start(), "div")
        cover_html = html_doc[cover_match.start():end]
        html_doc = html_doc[:cover_match.start()] + html_doc[end:]
    main_match = re.search(r'<div\b[^>]*class="bear-web-x-container\b[^>]*>', html_doc)
    if not main_match:
        raise RuntimeError("docx document container not found in SSR")
    _, end = _find_matching_close(html_doc, main_match.start(), "div")
    main_html = html_doc[main_match.start():end].replace(
        '<div ', '<div id="ld-document-start" ', 1
    )
    columns = '<div class="ld-docx-columns%s">%s%s%s%s</div>' % (
        " ld-has-comments" if panel_html else "", cover_html,
        catalogue_html, main_html, panel_html,
    )
    html_doc = html_doc[:main_match.start()] + columns + html_doc[end:]

    # 3. override styles + static script
    inject = "<style>%s\n%s</style>" % (OVERRIDE_STYLE + DOCX_EXTRA_STYLE, extra_css)
    head_close = html_doc.lower().find("</head>")
    if head_close >= 0:
        html_doc = html_doc[:head_close] + inject + html_doc[head_close:]
    else:
        html_doc = inject + html_doc

    static_script = "<script>%s</script>" % STATIC_SCRIPT
    body_close = html_doc.lower().rfind("</body>")
    if body_close >= 0:
        html_doc = html_doc[:body_close] + static_script + html_doc[body_close:]
    else:
        html_doc += static_script
    return html_doc


DOCX_EXTRA_STYLE = """
/* new /docx shell: SSR hides .page-main pending hydration */
.page-main{visibility:visible!important}
/* text must stay selectable without the viewer JS (SSR sets user-select:none) */
.bear-web-x-container,.page-main,.page-main *,.section-nav *,.ld-fs-panel *{
  -webkit-user-select:text!important;user-select:text!important}
/* images never overflow the column / grid cell */
.image-block-width-wrapper{max-width:100%!important;box-sizing:border-box}
.image-block-container{max-width:100%!important}
.docx-grid_column-block .image-block-width-wrapper{width:100%!important}
.docx-grid_column-block .image-block{align:center}
.docx-grid_column-block .docx-image-block{margin:4px 0}
/* heading fold arrow */
.block[data-level]>.fold-wrapper{left:0}
.ld-fold-handler{display:flex!important;align-items:center;justify-content:center;background:transparent;border:0;padding:0;color:#2b2f36;border-radius:2px;cursor:pointer}
.ld-fold-handler:hover{background:rgba(31,35,41,.08)}
.ld-fold-handler svg{display:block;transition:transform .15s ease}
.ld-folded>.fold-wrapper .ld-fold-handler svg{transform:rotate(-90deg)}
.page-block-children [hidden]{display:none!important}
/* native docx table */
.docx-table-block{margin:8px 0}
.table-block.docx-table-inner-wrapper{overflow-x:auto}
table.table{border-collapse:collapse;width:100%;border:1px solid var(--line-divider-default,rgba(31,35,41,.15));table-layout:fixed}
table.table td.table-cell-content-wrapper{border:1px solid var(--line-divider-default,rgba(31,35,41,.15));padding:6px 10px;vertical-align:top;min-width:40px;word-break:break-word}
table.table tr.first-row td{background:var(--ccmtoken-doc-block-bg-area,#f5f6f7);font-weight:600}
table.table .ace-line{min-height:20px}
/* embedded object placeholder */
.ld-embedded-placeholder{display:flex;align-items:center;gap:10px;padding:18px 20px;margin:8px 0;border:1px dashed #c9cdd4;border-radius:8px;background:#fafbfc;color:#8f959e;font-size:13px}
.ld-embedded-icon{font-size:18px;color:#bbbfc4}
.ld-embedded-preview{margin:8px 0;max-width:100%;overflow:hidden}
.ld-embedded-image{display:block;border:1px solid #dee0e3;border-radius:4px;box-sizing:border-box}
.ld-nested-blocks{margin-left:26px}
.ld-file-card{display:block;padding:14px 16px;margin:8px 0;border:1px solid #dee0e3;border-radius:6px;overflow-wrap:anywhere}
.ld-inline-catalogue ul{list-style:none;padding:12px 0;line-height:1.8}
/* cover dimensions and crop offsets match the native viewer */
.ld-doc-cover{height:clamp(128px,calc(32vh - 20.48px),400px)!important;width:100%!important}
.ld-doc-cover .doc-cover-image-wrapper{width:100%;height:100%}
.ld-doc-cover .doc-cover-image{display:block;visibility:visible!important;width:100%;height:100%;object-fit:cover}
.docx-page-block .page-block.root-block{display:block}
/* list markers (rendered explicitly, no JS) */
.docx-bullet-block .list,.docx-ordered-block .list,.docx-todo-block .list{display:flex;align-items:flex-start}
.docx-bullet-block .bullet-dot-style{font-size:14px;line-height:26px;color:var(--primary-content-default,#1f2329)}
.docx-ordered-block .order-number{font-size:14px;color:var(--primary-content-default,#1f2329)}
.docx-todo-block .todo-checkbox{width:18px;height:26px;display:flex;align-items:center;justify-content:center;flex:0 0 18px}
.docx-todo-block .todo-checkbox-icon{display:inline-block;width:14px;height:14px;border:1.5px solid #bbbfc4;border-radius:3px;font-size:10px;color:#fff;text-align:center;line-height:12px}
.docx-todo-block.task-done .todo-checkbox-icon{background:#3370ff;border-color:#3370ff}
.docx-code-block .code-block{background:var(--ccmtoken-doc-block-bg-area,#f5f6f7);border-radius:6px;padding:10px 12px;margin:6px 0}
.docx-code-block .ace-line{white-space:pre-wrap;font-family:"SFMono-Regular",Consolas,Menlo,monospace;font-size:13px}
.docx-divider-block .divider-block{padding:4px 0}
.docx-image-block .image-block{margin:6px 0}
.docx-grid-block .grid-block{margin:6px 0}
/* The empty right gutter keeps uncommented documents centred like the viewer. */
.ld-docx-columns{display:grid;grid-template-columns:260px minmax(0,1fr) 260px;align-items:start;width:100%;min-width:0;flex:1}
.ld-docx-columns.ld-has-comments{grid-template-columns:260px minmax(0,1fr) 392px}
.ld-docx-columns>.ssr-cover{grid-column:1/-1}
.ld-docx-columns>.section-nav-container{grid-column:1;margin-top:60px!important}
.ld-docx-columns>.bear-web-x-container{grid-column:2;min-width:0;width:100%!important;height:auto!important;overflow:visible!important}
.ld-docx-columns>.ld-fs-panel{grid-column:3;margin-top:60px!important}
.ld-docx-columns .page-main{width:100%!important;min-width:0!important;box-sizing:border-box}
.ld-docx-columns .docx-grid_column-block{min-width:0}
@media(max-width:1200px){
 .ld-docx-columns{grid-template-columns:220px minmax(0,1fr) 0}
 .ld-docx-columns.ld-has-comments{grid-template-columns:220px minmax(0,1fr) 312px}
 .ld-docx-columns>.section-nav-container{width:220px!important;max-width:220px!important}
 .ld-docx-columns>.ld-fs-panel{width:300px!important;max-width:300px!important}
 .ld-docx-columns .page-main{padding-left:28px!important;padding-right:28px!important}
}
@media(max-width:800px){
 .ld-docx-columns,.ld-docx-columns.ld-has-comments{grid-template-columns:minmax(0,1fr)}
 .ld-docx-columns>.section-nav-container{display:none!important}
 .ld-docx-columns>.bear-web-x-container,.ld-docx-columns>.ld-fs-panel{grid-column:1}
 .ld-docx-columns>.ld-fs-panel{position:static!important;width:auto!important;max-width:none!important;margin:24px!important}
}
/* catalogue entries: native muted look (anchors default to blue) */
.section-nav .full-entry-title{color:var(--text-caption,#646a73)!important;font-size:13px;line-height:22px;text-decoration:none;display:block;padding:1px 8px;border-radius:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.section-nav .full-entry-title:hover{color:var(--primary-pri-500,#3370ff)!important;background:var(--fill-hover,#f5f6f7)}
.section-nav .full-entries{list-style:none;margin:0;padding:6px 8px}
.section-nav .r-title,.section-nav .heading-h1{font-weight:600}
.section-nav .indentation-level-2{padding-left:14px}
.section-nav .indentation-level-3{padding-left:28px}
.page-block-children .root-render-unit-container>.render-unit-wrapper>.block:first-child{margin-top:0}
"""


# ===========================================================================
# Entry point
# ===========================================================================

def _capture_embedded_blocks(url, config, data, resources):
    """Load canvas embeds in the public viewer and save their full bitmap.

    Text comes from the complete model, so virtual scrolling cannot discard
    paragraphs or limit the snapshot to the viewport.
    """
    pending = {
        bid for bid, entry in data["block_map"].items()
        if entry.get("data", {}).get("type") in {"sheet", "whiteboard", "bitable", "task_list"}
    }
    if not pending:
        return
    from site_adapters.services.engine.browser_provider import launch_browser
    from site_adapters.services.auth.cookies import cookie_string_to_playwright_list

    logger = logging.getLogger(__name__)
    kwargs = {"headless": True}
    if config.get("proxy"):
        kwargs["proxy"] = {"server": config["proxy"]}
    browser = launch_browser(**kwargs)
    try:
        headers = {k: v for k, v in (config.get("headers") or {}).items()
                   if k.lower() not in {"cookie", "user-agent"}}
        context_options = dict(viewport={"width": 1600, "height": 1000},
                               device_scale_factor=2, extra_http_headers=headers)
        ua = (config.get("headers") or {}).get("User-Agent", "")
        if ua and "bot" not in ua.lower():
            context_options["user_agent"] = ua
        context = browser.new_context(**context_options)
        context.add_cookies([
            {"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path or "/", "secure": c.secure}
            for c in resources.session.cookies if c.domain
        ])
        cookie = config.get("user_cookie") or config.get("_user_cookie")
        if cookie:
            context.add_cookies(cookie_string_to_playwright_list(cookie, urlparse(url).hostname or ""))
        page = context.new_page()
        deadline = time.monotonic() + min(config.get("timeout") or 180, 180)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        scroller = page.locator('#mainBox .bear-web-x-container')
        scroller.wait_for(state="visible", timeout=30000)
        page.wait_for_timeout(1000)
        stalled = 0
        while pending and time.monotonic() < deadline:
            for button in page.locator('.fold-handler-wrapper.fold-folded .fold-handler').all():
                button.evaluate('(el) => el.click()')
            visible = page.locator('#mainBox [data-record-id]').evaluate_all(
                '(els) => els.filter(e => e.getBoundingClientRect().height > 0).map(e => e.dataset.recordId)'
            )
            for bid in visible:
                if bid not in pending:
                    continue
                block = page.locator('#mainBox [data-record-id="%s"]' % bid)
                try:
                    block.scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_function("""bid => {
                        const el = document.querySelector('#mainBox [data-record-id="'+bid+'"]');
                        return el && (el.querySelector('.spread-loaded canvas.spreadsheet-canvas')
                            || el.querySelector('.whiteboad-x-content-canvas canvas')
                            || el.querySelector('.bitable canvas'));
                    }""", arg=bid, timeout=15000)
                    page.wait_for_timeout(1000)
                    preview = block.evaluate("""el => {
                        const source = el.querySelector('.spread-loaded canvas.spreadsheet-canvas')
                            || el.querySelector('.whiteboad-x-content-canvas canvas')
                            || el.querySelector('.bitable canvas');
                        if (!source || !source.width || !source.height) return null;
                        const canvas = document.createElement('canvas');
                        canvas.width = source.width; canvas.height = source.height;
                        const ctx = canvas.getContext('2d');
                        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
                        ctx.drawImage(source, 0, 0);
                        return {src:canvas.toDataURL('image/png'),
                            width:source.getBoundingClientRect().width,
                            height:source.getBoundingClientRect().height};
                    }""")
                    if preview:
                        resources.embedded[bid] = preview
                        logger.info("Captured Feishu embedded block %s", bid)
                except Exception as exc:
                    logger.warning("Feishu embedded block %s did not load: %s", bid, exc)
                pending.remove(bid)
            state = scroller.evaluate("""el => {
                const end = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
                el.scrollTop += el.clientHeight * .75;
                return end;
            }""")
            stalled = stalled + 1 if state else 0
            if stalled >= 4:
                break
            page.wait_for_timeout(200)
        if pending:
            logger.warning("Feishu embedded blocks not reached: %s", ", ".join(sorted(pending)))
    finally:
        browser.close()


def replace(url, config, output_path):
    session, ssr_html, client_vars, comment_data = fetch_models(url, config)
    data = client_vars["data"]

    resources = ResourceStore(session, url)
    try:
        if "block_map" in data:
            resources.prefetch(
                PREVIEW_URL.format(token=b["data"]["image"]["token"])
                for b in data["block_map"].values()
                if b.get("data", {}).get("image", {}).get("token")
            )
            _capture_embedded_blocks(url, config, data, resources)
            snapshot = _render_block_map(
                data, ssr_html, comment_data, resources
            )
        else:
            collab = data["collab_client_vars"]
            renderer = ModelRenderer(collab, resources)
            body_html = renderer.render_document()

            panel_html = global_html = ""
            if comment_data:
                panel_html = render_comment_panel(
                    comment_data, renderer.comment_order, resources
                )
                global_html = render_global_comments(comment_data, resources)

            extra_css = resources.once_style_rules()
            snapshot = assemble_snapshot(
                ssr_html, body_html, panel_html, session,
                heading_guids=renderer.heading_guids, global_html=global_html,
                extra_css=extra_css,
            )
    finally:
        resources.shutdown()
        session.close()

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(snapshot)


def _inline_cover(ssr_html, resources):
    """Keep the native banner crop, replacing its loading/hydration markup."""
    match = re.search(
        r'<div\b[^>]*class="ssr-cover\b[^>]*>',
        ssr_html, re.S,
    )
    if not match:
        return ssr_html
    _, end = _find_matching_close(ssr_html, match.start(), "div")
    hd = re.search(
        r'<img\b[^>]*class="[^"]*\bssr-cover-hd-image\b[^"]*"[^>]*>',
        ssr_html[match.end():end],
    )
    if not hd:
        return ssr_html
    src_match = re.search(r'src="([^"]+)"', hd.group(0))
    if not src_match:
        return ssr_html
    data_url = resources.get_image(html.unescape(src_match.group(1)))
    info_match = re.search(r'data-cover-info=(["\'])(.*?)\1', match.group(0))
    info = json.loads(html.unescape(info_match.group(2))) if info_match else {}
    cover = (
        '<div class="ssr-cover pc ld-doc-cover" data-offset-x="%s" data-offset-y="%s">'
        '<div class="doc-cover-image-wrapper">'
        '<img class="doc-cover-image" alt="文档封面" src="%s"></div></div>'
        % (esc(info.get("offset_ratio_x", 0)), esc(info.get("offset_ratio_y", 0)), esc(data_url))
    )
    return ssr_html[:match.start()] + cover + ssr_html[end:]


def _render_block_map(data, ssr_html, comment_data, resources):
    ssr_html = _inline_cover(ssr_html, resources)
    renderer = DocxBlockRenderer(data, resources)
    body_html = renderer.render_document()

    panel_html = global_html = ""
    if comment_data:
        panel_html = render_comment_panel(
            comment_data, renderer.comment_order, resources
        )
        global_html = render_global_comments(comment_data, resources)

    catalogue_html = renderer.build_catalogue()
    extra_css = resources.once_style_rules()
    return assemble_docx_snapshot(
        ssr_html, body_html, panel_html, catalogue_html, global_html,
        resources.session, extra_css=extra_css,
    )
