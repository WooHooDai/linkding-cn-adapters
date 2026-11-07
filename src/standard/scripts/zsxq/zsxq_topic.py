"""知识星球主题内容提取。

wx.zsxq.com 的主题详情与 t.zsxq.com 的分享短链都只返回 SPA 外壳，正文由前端
调用 api.zsxq.com/v2/topics/{topic_id} 渲染。这里复用同一接口，用一个 replace
钩子同时服务两段：metadata 返回字段，snapshot 直接写出完整 HTML（带内嵌图片，
绕开 SingleFile 对 file:// 临时页的限制）。Cookie 由 auth.cookie(type=login)
注入 config['user_cookie']，需用户手动维护。
"""
import base64
import html
import logging
import os
import re
import time
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

logger = logging.getLogger(__name__)

E_TAG = re.compile(r"<e\s+([^>]*?)/?>")
# /dweb2/index/topic_detail/<id>、/group/<gid>/topic/<id>、
# /groups/<gid>/topics/<id>/detail.html、mweb 分享页 ?topic_id=<id>
TOPIC_PATH = re.compile(r"/(?:topic_detail|topics?)/(\d+)")

API_URL = "https://api.zsxq.com/v2/topics/{topic_id}"
COMMENTS_URL = "https://api.zsxq.com/v2/topics/{topic_id}/comments?count=30"
# 仅防接口异常时的死循环；正常分页靠「本页不足 30 条 / 无新增 / 游标停滞」自然终止
MAX_COMMENT_PAGES = 50
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DESCRIPTION_LIMIT = 200


def _topic_id_from_url(url):
    parsed = urlparse(url or "")
    ids = parse_qs(parsed.query).get("topic_id")
    if ids:
        return ids[0]
    match = TOPIC_PATH.search(parsed.path)
    return match.group(1) if match else None


def _topic_id(url):
    found = _topic_id_from_url(url)
    if found:
        return found
    parsed = urlparse(url or "")
    if parsed.hostname and parsed.hostname.startswith("t."):
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=15, allow_redirects=True
        )
        candidates = [h.headers.get("Location", "") for h in resp.history]
        candidates.append(resp.url)
        for candidate in candidates:
            found = _topic_id_from_url(candidate)
            if found:
                return found
    return None


def _cookie(config):
    cookie = config.get("user_cookie")
    if cookie:
        return cookie
    from site_adapters.services.auth.credentials import get_shared_cookie

    return get_shared_cookie(hostname="zsxq.com")[0]


def _fetch_topic(url, config):
    """返回 (topic, status, reason)；成功时 status/reason 为 None。"""
    topic_id = _topic_id(url or config.get("url"))
    if not topic_id:
        logger.info("zsxq: no topic id in %s", url)
        return None, "missing", "链接不是知识星球主题页，无法解析主题 ID"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wx.zsxq.com/",
        "Origin": "https://wx.zsxq.com",
    }
    cookie = _cookie(config)
    if cookie:
        headers["Cookie"] = cookie
    status, reason = "failed", "主题接口请求失败"
    # 接口偶发返回 succeeded=false（风控/限流），重试一次即可恢复。
    for attempt in range(2):
        payload = None
        http_status = None
        try:
            resp = requests.get(
                API_URL.format(topic_id=topic_id),
                headers=headers,
                timeout=min(config.get("timeout") or 20, 30),
            )
            http_status = resp.status_code
            payload = resp.json()
        except Exception as exc:
            logger.warning("zsxq: topic %s fetch failed: %s", topic_id, exc)
            status, reason = "failed", f"主题接口请求失败（{type(exc).__name__}）"
        if payload and payload.get("succeeded"):
            topic = payload.get("resp_data", {}).get("topic")
            if topic:
                return topic, None, None
            return None, "missing", "接口未返回主题内容"
        code = (payload or {}).get("code")
        if http_status == 401 or code == 401:
            status, reason = "blocked", "登录 Cookie 已失效，请在站点适配器设置中重新粘贴"
        elif http_status and http_status >= 400:
            status, reason = "failed", f"主题接口返回 HTTP {http_status}"
        elif payload:
            status, reason = "missing", (
                f"主题不可访问（接口 code={code}）：可能已删除、无权限或触发限流"
            )
        if attempt == 0:
            logger.info("zsxq: topic %s retry (%s)", topic_id, reason)
            time.sleep(0.6)
    return None, status, reason


def _fetch_comments(topic_id, config):
    """穷尽分页，返回 (评论列表, 是否触到保险上限)。

    接口每页最多 30 条，用最早的 create_time 作 end_time 向前翻页；正常主题
    1~2 页就自然终止：本页不足 30 条 / 全是重复 / 游标不再前进。
    MAX_COMMENT_PAGES 只防接口异常时的死循环，不是业务上的条数上限。
    """
    if not topic_id:
        return [], False
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://wx.zsxq.com/",
    }
    cookie = _cookie(config)
    if cookie:
        headers["Cookie"] = cookie
    collected = {}
    end_time = None
    for _page in range(MAX_COMMENT_PAGES):
        url = COMMENTS_URL.format(topic_id=topic_id)
        if end_time:
            url += "&end_time=" + quote(end_time, safe="")
        payload = None
        for attempt in range(2):
            try:
                payload = requests.get(url, headers=headers, timeout=12).json()
            except Exception as exc:
                logger.info("zsxq: comments fetch failed: %s", exc)
            if payload and payload.get("succeeded"):
                break
            if attempt == 0:
                time.sleep(0.5)
        if not (payload and payload.get("succeeded")):
            logger.info("zsxq: comments unavailable for topic %s", topic_id)
            break
        comments = (payload.get("resp_data") or {}).get("comments") or []
        fresh = 0
        for item in comments:
            cid = item.get("comment_id")
            if cid and cid not in collected:
                collected[cid] = item
                fresh += 1
        if len(comments) < 30 or fresh == 0:
            break
        times = sorted(c.get("create_time") for c in comments if c.get("create_time"))
        if not times or times[0] == end_time:
            break
        end_time = times[0]
    else:
        logger.warning("zsxq: comments pagination hit safety cap for %s", topic_id)
        return sorted(collected.values(), key=lambda c: c.get("create_time") or ""), True
    return sorted(collected.values(), key=lambda c: c.get("create_time") or ""), False


def _comment_row(item, css_class="zsxq-comment"):
    esc = html.escape
    name = (item.get("owner") or {}).get("name") or "匿名"
    when = (item.get("create_time") or "")[:10]
    likes = item.get("likes_count") or 0
    meta = f"{esc(name)} · {esc(when)}"
    if likes:
        meta += f" · 赞 {likes}"
    return (
        f'<div class="{css_class}">'
        f'<div class="comment-meta">{meta}</div>'
        f'<div class="comment-text">{_html_line(item.get("text") or "")}</div>'
        "</div>"
    )


def _render_comments(comments, complete=True, truncated=False):
    if not comments:
        return ""
    rows = []
    reply_total = 0
    missing_replies = 0
    for item in comments:
        rows.append(_comment_row(item))
        replies = item.get("replied_comments") or []
        reply_total += len(replies)
        declared = item.get("replies_count") or 0
        if declared > len(replies):
            missing_replies += declared - len(replies)
        for reply in replies:
            rows.append(_comment_row(reply, "zsxq-reply"))
    if complete:
        heading = f"评论（{len(comments)} 条，含 {reply_total} 条回复）"
    else:
        heading = f"评论（仅预览 {len(comments)} 条，完整评论接口暂不可用）"
    if truncated:
        heading += " · 分页触顶，可能未抓全"
    if missing_replies:
        heading += f" · 另有 {missing_replies} 条回复未展开"
    return (
        f'<section class="zsxq-comments"><h2>{heading}</h2>'
        + "".join(rows)
        + "</section>"
    )


def _e_attrs(raw):
    return {
        key: unquote(html.unescape(value))
        for key, value in re.findall(r'(\w+)\s*=\s*"([^"]*)"', raw)
    }


def _plain_line(line):
    """把 <e type="web"|"hashtag" ...> 卡片展开成可读文字。"""
    return E_TAG.sub(lambda m: _e_attrs(m.group(1)).get("title") or "", line)


def _html_line(line):
    """渲染正文行：普通文本转义，链接卡片转成真正的 <a>。"""
    chunks = []
    pos = 0
    for match in E_TAG.finditer(line):
        chunks.append(html.escape(line[pos:match.start()]))
        attrs = _e_attrs(match.group(1))
        title = html.escape(attrs.get("title") or "")
        href = attrs.get("href") or ""
        if attrs.get("type") == "web" and href:
            chunks.append(f'<a href="{html.escape(href, quote=True)}">{title}</a>')
        else:
            chunks.append(title)
        pos = match.end()
    chunks.append(html.escape(line[pos:]))
    return "".join(chunks)


def _body(topic):
    for key in ("talk", "solution", "answer", "task"):
        value = topic.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _parts(topic):
    body = _body(topic)
    owner = body.get("owner") or (topic.get("question") or {}).get("owner") or {}
    text = (body.get("text") or body.get("content") or "").strip()
    title = re.sub(r"\s+", " ", topic.get("title") or "").strip()
    if not title:
        title = next((line.strip() for line in text.splitlines() if line.strip()), "")[:100]
    images = []
    for image in body.get("images") or []:
        src = (
            (image.get("original") or {}).get("url")
            or (image.get("large") or {}).get("url")
            or (image.get("thumbnail") or {}).get("url")
        )
        if src:
            images.append(src)
    plain = re.sub(
        r"\s+", " ", " ".join(_plain_line(line) for line in text.split("\n"))
    ).strip()
    description = plain[:DESCRIPTION_LIMIT]
    if len(plain) > DESCRIPTION_LIMIT:
        description += "…"
    return {
        "title": title,
        "description": description or None,
        "author": owner.get("name") or None,
        "group": (topic.get("group") or {}).get("name") or "",
        "date": (topic.get("create_time") or "")[:10],
        "text": text,
        "images": images,
        "image": images[0] if images else None,
    }


def _embed_images(images, config):
    """图片 URL 带签名 token，离线快照必须把字节内嵌。"""
    headers = {"User-Agent": USER_AGENT, "Referer": "https://wx.zsxq.com/"}
    cookie = _cookie(config)
    if cookie:
        headers["Cookie"] = cookie
    result = []
    for src in images:
        try:
            resp = requests.get(src, headers=headers, timeout=12)
            resp.raise_for_status()
            if len(resp.content) > 5 * 1024 * 1024:
                result.append(src)
                continue
            mime = resp.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"
            if not mime.startswith("image/"):
                result.append(src)
                continue
            encoded = base64.b64encode(resp.content).decode("ascii")
            result.append(f"data:{mime};base64,{encoded}")
        except Exception as exc:
            logger.info("zsxq: image embed failed, keep remote src: %s", exc)
            result.append(src)
    return result


def _render_html(topic, url, config):
    parts = _parts(topic)
    esc = html.escape
    paragraphs = "".join(
        f"<p>{_html_line(line)}</p>" for line in parts["text"].split("\n") if line.strip()
    ) or "<p>（该主题没有文字正文）</p>"
    figures = "".join(
        f'<img src="{esc(src, quote=True)}" alt="">'
        for src in _embed_images(parts["images"], config)
    )
    comments, truncated = _fetch_comments(topic.get("topic_id"), config)
    if comments:
        comments_html = _render_comments(comments, truncated=truncated)
    else:
        # 评论接口偶发 1059（非官方工具提示），退回到主题自带的预览评论。
        comments_html = _render_comments(topic.get("show_comments") or [], complete=False)
    meta = " · ".join(
        value for value in (parts["author"], parts["group"], parts["date"]) if value
    )
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(parts['title'])}</title>
<meta name="author" content="{esc(parts['author'] or '', quote=True)}">
<meta name="description" content="{esc(parts['description'] or '', quote=True)}">
<style>
body {{ margin: 0; background: #f5f7fa; color: #1f2329;
        font: 16px/1.8 -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; }}
.zsxq-topic {{ max-width: 760px; margin: 0 auto; padding: 32px 24px 64px; }}
h1.title {{ font-size: 26px; line-height: 1.4; margin: 0 0 12px; }}
.meta {{ color: #8a919f; font-size: 14px; margin-bottom: 28px; }}
.zsxq-content p {{ margin: 0 0 1em; white-space: pre-wrap; }}
.zsxq-images img {{ display: block; max-width: 100%; height: auto; margin: 16px auto; }}
.zsxq-comments {{ margin-top: 40px; border-top: 1px solid #e5e7eb; padding-top: 20px; }}
.zsxq-comments h2 {{ font-size: 18px; margin: 0 0 12px; }}
.zsxq-comment {{ padding: 10px 0; border-bottom: 1px solid #f0f1f3; }}
.comment-meta {{ color: #8a919f; font-size: 13px; margin-bottom: 4px; }}
.comment-text {{ font-size: 15px; }}
.zsxq-reply {{ margin: 8px 0 8px 20px; padding: 8px 12px; background: #eef1f5;
               border-radius: 6px; border-bottom: none; font-size: 14px; }}
.source {{ margin-top: 40px; font-size: 13px; }}
</style></head><body>
<article class="zsxq-topic">
<h1 class="title">{esc(parts['title'])}</h1>
<div class="meta">{esc(meta)}</div>
<div class="zsxq-content">{paragraphs}{comments_html}</div>
<div class="zsxq-images">{figures}</div>
<p class="source"><a href="{esc(url or '', quote=True)}">原文链接</a></p>
</article></body></html>"""


def _fallback_metadata(url, config):
    """登录页、专栏首页等没有 topic_id，回退内置解析，避免结果全空。"""
    headers = {"User-Agent": USER_AGENT}
    cookie = _cookie(config)
    if cookie:
        headers["Cookie"] = cookie
    try:
        from site_adapters.services.engine import parse_metadata

        resp = requests.get(url, headers=headers, timeout=min(config.get("timeout") or 20, 30))
        return parse_metadata(resp.content.decode("utf-8", errors="replace"), url, config)
    except Exception as exc:
        logger.info("zsxq: fallback metadata failed: %s", exc)
        return {"title": None, "description": None, "author": None, "image": None, "url": url}


def replace(url, config, output_path=None):
    """metadata 返回字段；snapshot（带 output_path）写出完整快照。

    取不到主题时不写文件，并返回带原因的拒绝信息，由框架转成
    SnapshotRejectedError 透传到管理页面板。
    """
    topic, status, reason = _fetch_topic(url, config)
    if output_path is None:
        if not topic:
            return _fallback_metadata(url, config)
        parts = _parts(topic)
        return {
            "title": parts["title"] or None,
            "description": parts["description"],
            "author": parts["author"],
            "image": parts["image"],
            "url": url,
        }
    if not topic:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except OSError:
            pass
        logger.info("zsxq: snapshot rejected for %s: %s", url, reason)
        return {"status": status, "error": reason}
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(_render_html(topic, url, config))
