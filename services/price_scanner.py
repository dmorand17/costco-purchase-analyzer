"""Multi-source Costco deal scraper."""

import base64
import io
import json
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any

import boto3
import requests
from bs4 import BeautifulSoup

NOVA_LITE = "us.amazon.nova-2-lite-v1:0"

_bedrock = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    return _bedrock

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def scan_price_drops(force_refresh: bool = False) -> list[dict[str, Any]]:
    """
    Scrape all deal sources and return deduplicated list of price drop dicts.
    Each dict has keys: item_name, item_number, original_price, sale_price,
    promo_start, promo_end, source, link.
    """
    scrapers = [
        ("redflagdeals_hot", _scrape_redflagdeals_hot),
        ("redflagdeals_clearance", _scrape_redflagdeals_clearance),
        ("reddit_costco", _scrape_reddit_costco),
        ("reddit_costcoca", _scrape_reddit_costcoca),
        ("coupon_book", _scrape_coupon_book),
        ("cocowest", _scrape_cocowest),
        ("cocoeast", _scrape_cocoeast),
    ]

    all_items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    today = date.today().isoformat()

    for source_name, scraper in scrapers:
        try:
            items = scraper()
            for item in items:
                # Skip expired deals
                promo_end = item.get("promo_end", "")
                if promo_end and promo_end < today:
                    continue
                dedup_key = (
                    item.get("item_name", "").lower().strip(),
                    item.get("promo_end", ""),
                )
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                item.setdefault("source", source_name)
                all_items.append(item)
        except Exception as exc:
            print(f"[price_scanner] {source_name} failed: {exc}")
        time.sleep(1)  # rate limiting

    return all_items


# ── RedFlagDeals ───────────────────────────────────────────────────────────────

COSTCO_KEYWORDS = {"costco", "warehouse", "kirkland"}
RFD_HOT_URL = "https://forums.redflagdeals.com/hot-deals-f9/?c=5"
RFD_CLEARANCE_THREAD = (
    "https://forums.redflagdeals.com/costco-97-clearance-deals-2671415/"
)


def _scrape_redflagdeals_hot() -> list[dict[str, Any]]:
    resp = requests.get(RFD_HOT_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for post in soup.select(".topic_title_link"):
        title = post.get_text(strip=True)
        if not any(kw in title.lower() for kw in COSTCO_KEYWORDS):
            continue
        price_match = re.search(r"\$(\d+(?:\.\d+)?)", title)
        if not price_match:
            continue
        href = post.get("href", "")
        link = f"https://forums.redflagdeals.com{href}" if href.startswith("/") else href
        items.append(
            {
                "item_name": title,
                "item_number": "",
                "original_price": None,
                "sale_price": float(price_match.group(1)),
                "promo_start": "",
                "promo_end": "",
                "source": "redflagdeals_hot",
                "link": link,
            }
        )
    return items


def _scrape_redflagdeals_clearance() -> list[dict[str, Any]]:
    resp = requests.get(RFD_CLEARANCE_THREAD, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    items = []
    for post in soup.select(".post_content"):
        text = post.get_text()
        for line in text.splitlines():
            line = line.strip()
            match = re.search(r"\$(\d+)\.97", line)
            if not match:
                continue
            items.append(
                {
                    "item_name": line[:120],
                    "item_number": "",
                    "original_price": None,
                    "sale_price": float(f"{match.group(1)}.97"),
                    "promo_start": "",
                    "promo_end": "",
                    "source": "redflagdeals_clearance",
                    "link": RFD_CLEARANCE_THREAD,
                }
            )
    return items[:50]  # cap results


# ── Reddit ─────────────────────────────────────────────────────────────────────

REDDIT_HEADERS = {**HEADERS, "Accept": "application/json"}


def _scrape_reddit(subreddit: str) -> list[dict[str, Any]]:
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {"q": "price drop sale coupon $", "sort": "new", "t": "month", "limit": 50}
    resp = requests.get(url, headers=REDDIT_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = []
    for post in data.get("data", {}).get("children", []):
        pd = post["data"]
        title = pd.get("title", "")
        if not any(kw in title.lower() for kw in COSTCO_KEYWORDS | {"$"}):
            continue
        price_match = re.search(r"\$(\d+(?:\.\d+)?)", title)
        if not price_match:
            continue
        items.append(
            {
                "item_name": title[:200],
                "item_number": "",
                "original_price": None,
                "sale_price": float(price_match.group(1)),
                "promo_start": "",
                "promo_end": "",
                "source": f"reddit_{subreddit.lower()}",
                "link": f"https://reddit.com{pd.get('permalink', '')}",
            }
        )
    return items


def _scrape_reddit_costco() -> list[dict[str, Any]]:
    return _scrape_reddit("Costco")


def _scrape_reddit_costcoca() -> list[dict[str, Any]]:
    return _scrape_reddit("CostcoCanada")


# ── Costco Coupon Book (SmartCanucks) ─────────────────────────────────────────

SMARTCANUCKS_URL = "https://smartcanucks.ca/category/costco-canada/"

COUPON_PROMPT = """Extract all Costco coupon items from this flyer page image.
Return a JSON array only (no markdown), each element:
{
  "item_name": "...",
  "item_number": "<5-8 digits or empty>",
  "original_price": <float or null>,
  "sale_price": <float>,
  "promo_start": "YYYY-MM-DD or empty",
  "promo_end": "YYYY-MM-DD or empty",
  "savings": <float or null>
}
Only include items with a clear sale price. Return [] if none found."""


def _scrape_coupon_book() -> list[dict[str, Any]]:
    resp = requests.get(SMARTCANUCKS_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find latest warehouse sale post
    flyer_link = None
    for a in soup.select("article h2 a, .entry-title a"):
        href = a.get("href", "")
        text = a.get_text(strip=True).lower()
        if "costco" in text and ("coupon" in text or "flyer" in text or "sale" in text):
            flyer_link = href
            break

    if not flyer_link:
        return []

    post_resp = requests.get(flyer_link, headers=HEADERS, timeout=15)
    post_resp.raise_for_status()
    post_soup = BeautifulSoup(post_resp.text, "html.parser")

    items: list[dict[str, Any]] = []
    for img_tag in post_soup.select(".entry-content img")[:12]:  # cap pages
        img_url = img_tag.get("src") or img_tag.get("data-src", "")
        if not img_url or not img_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        try:
            img_resp = requests.get(img_url, headers=HEADERS, timeout=15)
            img_resp.raise_for_status()
            img_bytes = img_resp.content
            # Detect format
            fmt = "jpeg"
            if img_url.lower().endswith(".png"):
                fmt = "png"
            elif img_url.lower().endswith(".webp"):
                fmt = "webp"

            result = _get_bedrock().converse(
                modelId=NOVA_LITE,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "image": {
                                    "format": fmt,
                                    "source": {"bytes": img_bytes},
                                }
                            },
                            {"text": COUPON_PROMPT},
                        ],
                    }
                ],
            )
            raw = result["output"]["message"]["content"][0]["text"]
            raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
            page_items = json.loads(raw) if raw.startswith("[") else []
            for it in page_items:
                it["source"] = "coupon_book"
                it["link"] = flyer_link
                items.append(it)
        except Exception as exc:
            print(f"[coupon_book] page failed: {exc}")
        time.sleep(0.5)

    return items


# ── CocoWest / CocoEast ────────────────────────────────────────────────────────

COCOWEST_URL = "https://cocowest.ca"
COCOEAST_URL = "https://cocoeast.ca"

# Matches: 12345  ITEM NAME  $X.XX  exp Dec 31
ITEM_RE = re.compile(
    r"^(\d{5,8})\s+(.+?)\s+\$?(\d+\.\d+)(?:\s+.*?(\w+ \d{1,2}|\d{4}-\d{2}-\d{2}))?$"
)


def _scrape_coco(base_url: str, source_name: str) -> list[dict[str, Any]]:
    resp = requests.get(base_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find latest in-store sales post
    post_link = None
    for a in soup.select("article h2 a, .entry-title a, h2.post-title a"):
        text = a.get_text(strip=True).lower()
        if "in-store" in text or "instant savings" in text or "weekend" in text:
            post_link = a.get("href")
            break

    if not post_link:
        return []

    post_resp = requests.get(post_link, headers=HEADERS, timeout=15)
    post_resp.raise_for_status()
    post_soup = BeautifulSoup(post_resp.text, "html.parser")

    items = []
    content = post_soup.select_one(".entry-content, .post-content, article")
    if not content:
        return []

    # Try to extract date range from post title
    title = post_soup.select_one("h1.entry-title, h1.post-title")
    promo_end = ""
    if title:
        date_match = re.search(
            r"(\w+ \d{1,2})[,\s–-]+(\w+ \d{1,2},?\s*\d{4})", title.get_text()
        )
        if date_match:
            promo_end = date_match.group(2).strip()

    for line in content.get_text().splitlines():
        line = line.strip()
        m = ITEM_RE.match(line)
        if not m:
            continue
        item_number, name, price_str, end_date = (
            m.group(1),
            m.group(2).strip(),
            m.group(3),
            m.group(4) or promo_end,
        )
        items.append(
            {
                "item_name": name,
                "item_number": item_number,
                "original_price": None,
                "sale_price": float(price_str),
                "promo_start": "",
                "promo_end": end_date or "",
                "source": source_name,
                "link": post_link,
            }
        )
    return items


def _scrape_cocowest() -> list[dict[str, Any]]:
    return _scrape_coco(COCOWEST_URL, "cocowest")


def _scrape_cocoeast() -> list[dict[str, Any]]:
    return _scrape_coco(COCOEAST_URL, "cocoeast")
