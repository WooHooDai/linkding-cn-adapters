"""Reddit metadata extraction via embed.reddit.com.

embed.reddit.com is Reddit's embed host — it is not covered by the
network-security block that 403s www.reddit.com on restricted IPs, and it
needs no cookies. The page is server-side rendered with the full post:
title (shreddit-embed-title or h1), selftext (div[id$="-post-rtjson-content"]),
and the post image (largest redd.it-hosted src/srcset).
"""
import logging
import re

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

logger = logging.getLogger(__name__)


def _build_embed_url(url):
    """Build the embed.reddit.com URL for a reddit post URL (no query string)."""
    clean_url = url.split("?", 1)[0]
    return re.sub(
        r"^https?://(?:www\.|old\.)?reddit\.com/",
        "https://embed.reddit.com/",
        clean_url,
    )


def replace(url: str, config: dict) -> dict:
    """Fetch Reddit metadata from embed.reddit.com."""
    empty = {"title": None, "description": None, "image": None, "url": url}

    if BeautifulSoup is None:
        logger.error("reddit_metadata: beautifulsoup4 not available")
        return empty

    timeout = config.get("timeout", 30) or 30
    embed_url = _build_embed_url(url)

    try:
        resp = requests.get(
            embed_url,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )
    except Exception as e:
        logger.warning("reddit_metadata: embed fetch error: %s", e)
        return empty

    if resp.status_code != 200:
        logger.info("reddit_metadata: embed got %s for %s", resp.status_code, embed_url)
        return empty

    soup = BeautifulSoup(resp.text, "html.parser")

    # Title: text posts use shreddit-embed-title; media posts use h1.
    title = None
    embed_title = soup.select_one("shreddit-embed-title")
    if embed_title:
        title = embed_title.get_text(strip=True)
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # Description: the full selftext. Media posts have no selftext div.
    description = None
    for div in soup.find_all("div", id=True):
        if div.get("id", "").endswith("-post-rtjson-content"):
            description = div.get_text(" ", strip=True) or None
            break

    # Image: pick the largest redd.it-hosted image (skip subreddit icons/ads).
    image_candidates = []
    for img in soup.find_all(["img", "faceplate-img"]):
        src = img.get("src") or img.get("data-src") or ""
        if re.search(r"(?:preview|external-preview)\.redd\.it|i\.redd\.it", src):
            image_candidates.append(src)
        srcset = img.get("srcset") or ""
        for part in srcset.split(","):
            part = part.strip()
            url_part = part.split(" ")[0] if part else ""
            if re.search(r"(?:preview|external-preview)\.redd\.it|i\.redd\.it", url_part):
                if url_part not in image_candidates:
                    image_candidates.append(url_part)

    def _width(u):
        m = re.search(r"[?&]width=(\d+)", u)
        return int(m.group(1)) if m else 0

    image = max(image_candidates, key=_width) if image_candidates else None

    logger.info(
        "reddit_metadata: title=%s, image=%s",
        (title or "")[:50],
        (image or "")[:50],
    )

    return {"title": title, "description": description, "image": image, "url": url}
