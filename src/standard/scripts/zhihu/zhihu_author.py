"""Restore the nickname zhihu masks as 知乎用户.

Zhihu replaces the author of every answer written by an account that enabled
「隐私保护」 with the literal placeholder 知乎用户: ``og:title`` becomes
"<问题> - 知乎用户 的回答", ``meta[itemprop=name]`` and ``data-zop`` say the
same, and the answer API returns ``author.name = "知乎用户"`` with an empty
``url_token`` and ``is_privacy = true``. The head-only selectors therefore have
nothing better to store.

The account id survives that masking, and the member endpoint still resolves it
to the real nickname, so this ``after`` hook only fires on the placeholder: it
reads the author id from the answer API's *masked* payload and asks
``/api/v4/members/<id>`` for the name the page refused to show.
"""

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

PLACEHOLDER = "知乎用户"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_ANSWER_RE = re.compile(r"(?:question/\d+/)?answer/(\d+)")
# og:title 的固定形态「<问题> - <作者> 的回答」，只在结尾匹配，避免误改问题标题。
_MASKED_TITLE_RE = re.compile(r" - 知乎用户 的回答$")


def _answer_id(url):
    match = _ANSWER_RE.search(url or "")
    return match.group(1) if match else None


def _headers(config, referer):
    headers = {"Referer": referer or "https://www.zhihu.com/"}
    for key, value in (config.get("headers") or {}).items():
        if key.lower() == "user-agent":
            headers["User-Agent"] = value
            break
    else:
        headers["User-Agent"] = DEFAULT_UA
    cookie = config.get("user_cookie") or config.get("_user_cookie")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _get_json(url, config, referer):
    """Fetch ``url`` as JSON, preferring the Chrome fingerprint zhihu expects."""
    timeout = min(int(config.get("timeout") or 20), 20)
    headers = _headers(config, referer)
    response = None
    # 刚刷新出的 __zse_ck 首次调 API 可能被风控拒绝（新令牌有数十秒的观察期），
    # 退避重试直至通过；正常令牌首击即 200，无额外开销。
    for attempt, backoff in ((1, 0), (2, 5), (3, 10)):
        if backoff:
            time.sleep(backoff)
        try:
            from curl_cffi import requests as chttp

            response = chttp.get(
                url, headers=headers, impersonate="chrome", timeout=timeout
            )
        except ImportError:
            import requests

            response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 403 or attempt == 3:
            break
    if response is None or response.status_code != 200:
        logger.debug(
            "zhihu_author: %s returned %s",
            url,
            response.status_code if response is not None else "no response",
        )
        return None
    try:
        return json.loads(response.text)
    except ValueError:
        return None


def _author_id(answer_id, config, referer):
    payload = _get_json(
        f"https://www.zhihu.com/api/v4/answers/{answer_id}?include=author",
        config,
        referer,
    )
    author = (payload or {}).get("author") or {}
    return author.get("id") or None


def _member_name(member_id, config, referer):
    payload = _get_json(
        f"https://www.zhihu.com/api/v4/members/{member_id}", config, referer
    )
    name = (payload or {}).get("name")
    return name.strip() if isinstance(name, str) else None


def after(result, url, config):
    """Replace 知乎用户 with the account's real nickname."""
    if not isinstance(result, dict):
        return
    author = (result.get("author") or "").strip()
    if author and author != PLACEHOLDER:
        return

    answer_id = _answer_id(url) or _answer_id(config.get("request_url"))
    if not answer_id:
        return

    member_id = _author_id(answer_id, config, url)
    if not member_id:
        return

    name = _member_name(member_id, config, url)
    if not name or name == PLACEHOLDER:
        return

    result["author"] = name
    title = result.get("title")
    if isinstance(title, str):
        result["title"] = _MASKED_TITLE_RE.sub(f" - {name} 的回答", title)
    logger.info(
        "zhihu_author: resolved masked author for answer %s: %s", answer_id, name
    )
