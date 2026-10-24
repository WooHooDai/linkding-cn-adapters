"""Weixin article author fallback for special article forms.

普通图文文章的作者（公众号名）在渲染后的 DOM 里（`#js_name`），由声明式 `select_author` 覆盖。
但部分特殊形态（视频号/小程序绑定文章，桌面端为 "微信扫一扫可打开此内容，使用完整服务"页）服务端不渲染 `#js_name`，`meta[name=author]` 也为空——作者名只存在于服务端注入的 JS 变量中

本脚本仅在引擎提取的 author 为空时重新抓取 HTML 做正则兜底，正常文章（author 已提取）直接跳过，不影响性能与既有结果。
"""
import logging
import re

import requests

logger = logging.getLogger(__name__)

_WINDOW_NAME_RE = re.compile(r'window\.name\s*=\s*"([^"]+)"')
_NICK_NAME_RE = re.compile(r"nick_name:\s*'([^']+)'")

# 广告位的公众号名不是文章作者，跳过。
_IGNORED_NICKNAMES = {"微信广告", "广告"}


def _fetch_html(url, config):
    headers = dict(config.get("headers") or {})
    ua = (config.get("http") or {}).get("User-Agent")
    headers["User-Agent"] = ua or (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    )
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


def _author_from_html(html):
    m = _WINDOW_NAME_RE.search(html)
    if m and m.group(1).strip():
        return m.group(1).strip()
    for m in _NICK_NAME_RE.finditer(html):
        name = m.group(1).strip()
        if name and name not in _IGNORED_NICKNAMES:
            return name
    return None


def after(result, url, config):
    """Fill author from server-injected JS vars when the page has no DOM author."""
    if not isinstance(result, dict):
        return
    if result.get("author"):
        return

    try:
        html = _fetch_html(url, config)
    except Exception as exc:  # noqa: BLE001 - fallback must never break saving
        logger.warning("weixin_author: fetch failed for %s: %s", url, exc)
        return

    author = _author_from_html(html)
    if not author:
        return
    result["author"] = author
    logger.info(
        "weixin_author: recovered author from injected vars: %s",
        (author or "")[:40],
    )
