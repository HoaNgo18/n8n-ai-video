"""
Phase 1 trend signal helper.

Fetches lightweight Vietnam trend context from public RSS feeds. The output is
small on purpose: n8n/Gemini only needs enough context to prefer currently
relevant Threads posts.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from dotenv import load_dotenv


DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_TOPICS_LIMIT = 10
DEFAULT_CONTEXT_LIMIT = 8

GOOGLE_NEWS_VN_URL = "https://news.google.com/rss?hl=vi&gl=VN&ceid=VN:vi"
GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=vi&gl=VN&ceid=VN:vi"

DEFAULT_RSS_URLS = (
    GOOGLE_NEWS_VN_URL,
    "https://vnexpress.net/rss/tin-moi-nhat.rss",
    "https://tuoitre.vn/rss/tin-moi-nhat.rss",
)

STOPWORDS = {
    "anh", "ban", "bao", "bi", "bo", "cac", "can", "cho", "co", "con", "cua",
    "dang", "day", "de", "den", "duoc", "gia", "hai", "hon", "khi", "khong",
    "la", "lai", "lam", "len", "luc", "mot", "nam", "nay", "nguoi", "nhieu",
    "nhung", "noi", "nuoc", "qua", "ra", "roi", "sau", "se", "tai", "the",
    "thi", "tin", "toi", "trong", "tu", "vao", "ve", "voi", "vnexpress",
    "tuoi", "tre", "online", "video", "moi", "nhat", "viet", "quoc",
}

GENERIC_TOPIC_TERMS = {
    "viet", "nam", "quoc", "trung", "thai", "lan", "my", "nga", "eu",
    "hop", "tac", "giai", "toan", "nang", "dong", "lanh", "tin", "moi",
}

GENERIC_PHRASES = {
    "hop tac", "toan quoc", "viet nam", "thai lan", "lien hop", "trung quoc",
}


@dataclass
class NewsItem:
    title: str
    source: str
    published: str
    link: str


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def normalize_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_search_text(value: str) -> str:
    base = unicodedata.normalize("NFKD", (value or "").replace("đ", "d").replace("Đ", "D"))
    ascii_text = base.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_text).strip()


def fetch_url(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        data = response.read(1_500_000)
    return data.decode("utf-8", errors="replace")


def parse_rss(xml_text: str, source_hint: str = "") -> list[NewsItem]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    channel_title = normalize_text(root.findtext("./channel/title") or source_hint)
    items: list[NewsItem] = []
    for item in root.findall(".//item"):
        title = normalize_text(item.findtext("title") or "")
        if not title:
            continue
        source = normalize_text(item.findtext("{*}source") or channel_title or source_hint)
        published = normalize_text(item.findtext("pubDate") or "")
        link = normalize_text(item.findtext("link") or "")
        items.append(NewsItem(title=title, source=source, published=published, link=link))
    return items


def parse_published(value: str) -> datetime:
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def dedupe_items(items: list[NewsItem]) -> list[NewsItem]:
    result: list[NewsItem] = []
    seen: set[str] = set()
    for item in sorted(items, key=lambda entry: parse_published(entry.published), reverse=True):
        key = normalize_search_text(item.title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def keyword_phrases(titles: list[str], limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    for title in titles:
        normalized = normalize_search_text(title)
        words = [
            word for word in re.findall(r"[a-z0-9]{2,}", normalized)
            if word not in STOPWORDS and not word.isdigit()
        ]
        for size in (3, 2):
            for index in range(0, max(0, len(words) - size + 1)):
                phrase = " ".join(words[index:index + size])
                if is_useful_topic_phrase(phrase):
                    counts[phrase] += size
        for word in words:
            if is_useful_topic_phrase(word):
                counts[word] += 1

    ranked = counts.most_common()
    repeated = [
        (phrase, count)
        for phrase, count in ranked
        if count >= max(2, len(phrase.split()) * 2)
    ]
    candidates = repeated if repeated else ranked
    return [phrase for phrase, _ in candidates[:limit]]


def is_useful_topic_phrase(phrase: str) -> bool:
    phrase = normalize_search_text(phrase)
    if not phrase or phrase in GENERIC_PHRASES:
        return False
    words = phrase.split()
    if not words:
        return False
    if all(word in GENERIC_TOPIC_TERMS for word in words):
        return False
    if len(words) == 1:
        return len(words[0]) >= 5 and words[0] not in GENERIC_TOPIC_TERMS
    return len(phrase) >= 7


def rss_urls_for(keyword: str = "") -> list[str]:
    if keyword:
        return [GOOGLE_NEWS_SEARCH_URL.format(query=quote_plus(keyword))]
    raw = os.getenv("TREND_RSS_URLS", "").strip()
    if not raw:
        return list(DEFAULT_RSS_URLS)
    return [url.strip() for url in re.split(r"[\n,]+", raw) if url.strip()]


def build_trend_signal(keyword: str = "", limit: int = DEFAULT_TOPICS_LIMIT) -> dict:
    timeout = int(os.getenv("TREND_FETCH_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    context_limit = int(os.getenv("TREND_CONTEXT_LIMIT", str(DEFAULT_CONTEXT_LIMIT)))

    all_items: list[NewsItem] = []
    errors: list[str] = []
    for url in rss_urls_for(keyword):
        try:
            all_items.extend(parse_rss(fetch_url(url, timeout), source_hint=url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            log(f"Trend RSS fetch failed: {url}: {exc}")

    items = dedupe_items(all_items)[: max(limit * 3, context_limit)]
    titles = [item.title for item in items]
    topics = keyword_phrases(titles, limit)
    context = [
        {
            "title": item.title,
            "source": item.source,
            "published": item.published,
            "link": item.link,
        }
        for item in items[:context_limit]
    ]
    brief_bits = topics[:limit]
    brief = ", ".join(brief_bits)
    if keyword:
        brief = f"Search keyword: {keyword}. Related Vietnam news topics: {brief}" if brief else f"Search keyword: {keyword}"
    else:
        brief = f"Trending topics in Vietnam right now: {brief}" if brief else ""

    return {
        "keyword": keyword,
        "topics": topics[:limit],
        "brief": brief,
        "context": context,
        "errors": errors[:5],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch lightweight Vietnam trend context from RSS.")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--limit", type=int, default=int(os.getenv("TREND_TOPICS_LIMIT", DEFAULT_TOPICS_LIMIT)))
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    data = build_trend_signal(keyword=args.keyword.strip(), limit=max(1, args.limit))
    print(json.dumps(data, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
