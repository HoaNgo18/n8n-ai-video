"""
Phase 2: Screenshot + Extract.

Input:
    venv\\Scripts\\python.exe src\\screenshot_extractor.py --id DX... --url https://www.threads.com/@x/post/DX...

Output:
    JSON object to stdout with fields for Google Sheets:
    ID, Screenshots, Extracted_Content, Narrator_Script, Status, Note
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

THREADS_LOGIN_URL = "https://www.threads.net/login"
POST_LOCATOR = 'div[data-pressable-container="true"]'
TARGET_COMMENT_COUNT = 5
TARGET_DISCUSSION_COMMENT_COUNT = 6
MAX_STORY_CONTINUATIONS = 12
MAX_TOTAL_SEGMENTS = 12
MAX_SCRIPT_TEXT_LENGTH = 8000
MIN_ACCEPTED_COMMENT_COUNT = 3
MAX_COMMENT_CAPTURE_ATTEMPTS = 8
COMMENT_CAPTURE_WAIT_MS = 2500
SALES_KEYWORDS = (
    "gia",
    "bao gia",
    "order",
    "dat hang",
    "mua ngay",
    "sale",
    "giam gia",
    "uu dai",
    "khuyen mai",
    "freeship",
    "ship",
    "cod",
    "san pham",
    "dich vu",
    "khoa hoc",
    "tuyen ctv",
    "affiliate",
    "booking",
    "bang gia",
    "lien he",
    "inbox",
    "ib",
)
WEAK_SALES_KEYWORDS = {"gia", "ship", "ib", "inbox"}
SELF_PROMO_KEYWORDS = (
    "follow minh",
    "ung ho minh",
    "kenh minh",
    "profile minh",
    "bio minh",
    "link bio",
    "xem them o bio",
    "subscribe",
    "dang ky kenh",
    "toi la",
    "minh la",
)
DISCUSSION_KEYWORDS = (
    "nghi sao",
    "quan diem",
    "theo moi nguoi",
    "co nen",
    "vi sao",
    "tai sao",
    "dung hay sai",
    "tranh cai",
    "ban luan",
    "goc nhin",
    "van de",
    "neu la ban",
    "theo ban",
)
STORY_KEYWORDS = (
    "cau chuyen",
    "ke chuyen",
    "tam su",
    "confession",
    "storytime",
    "ket qua la",
    "luc do",
    "hom nay",
    "hom qua",
    "plot twist",
    "bi soc",
    "gap chuyen",
)
HOT_TOPIC_KEYWORDS = (
    "thoi tiet",
    "gia nha",
    "bat dong san",
    "chung cu",
    "kinh te",
    "lam phat",
    "gia vang",
    "chinh tri",
    "xa hoi",
    "luong",
    "that nghiep",
    "hoc phi",
    "benh vien",
    "giao thong",
    "tai nan",
    "trend",
    "viral",
    "dang hot",
)
COMMENT_REASONING_KEYWORDS = (
    "minh nghi",
    "toi nghi",
    "theo minh",
    "theo toi",
    "vi ",
    "nhung",
    "tuy nhien",
    "neu ",
    "boi vi",
    "van de la",
)
CONTINUATION_MARKER_RE = re.compile(
    r"(?:^|[\s(])(?:\d+\s*/\s*\d+|part\s*\d+|p\s*\d+|\(\s*\d+\s*\))(?:$|[\s).,:;-])",
    re.IGNORECASE,
)
VIET_PATTERN = re.compile(
    "["
    "\\u00e0\\u00e1\\u00e2\\u00e3\\u00e8\\u00e9\\u00ea\\u00ec\\u00ed"
    "\\u00f2\\u00f3\\u00f4\\u00f5\\u00f9\\u00fa\\u00fd"
    "\\u0103\\u0111\\u0129\\u0169\\u01a1\\u01b0"
    "\\u1ea1-\\u1ef9"
    "]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    storage_state: Path
    screenshots_dir: Path
    debug_dir: Path
    timeout_ms: int
    post_login_wait_ms: int
    force_login: bool
    headless: bool


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def chromium_executable_path() -> str | None:
    candidate = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    known_paths = [
        candidate,
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    for path in known_paths:
        if path and Path(path).exists():
            return path
    return None


def decode_cli_text(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw[:1] in {'"', "["}:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            pass
    return raw


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def extract_author_from_url(url: str) -> str:
    match = re.search(r"threads\.(?:net|com)/@([A-Za-z0-9_.]+)/post/", url or "", flags=re.IGNORECASE)
    return match.group(1).lower() if match else ""


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    noisy_words = {
        "Translate",
        "Reply",
        "Like",
        "Share",
        "Repost",
    }
    for word in noisy_words:
        text = re.sub(rf"\b{re.escape(word)}\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def strip_threads_context_noise(text: str) -> str:
    text = re.sub(
        r"\(?\s*\d+\s*\)?\s*(?:/\s*\d+\s*)?"
        r"(?:\d+(?:[.,]\d+)?[KMkm]?\s*){2,6}"
        r"(?:to\s+[A-Za-z0-9_.\s-]+)?\.{0,3}\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\(?\s*\d+\s*\)?\s*(?:/\s*\d+\s*)?(?:to\s+[A-Za-z0-9_.\s-]+)?\.{0,3}\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bto\s+[A-Za-z0-9_.\s-]+\.{0,3}\s*$", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def is_vietnamese(text: str) -> bool:
    return bool(VIET_PATTERN.search(text or ""))


def normalize_search_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "")
    ascii_text = base.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"\s+", " ", ascii_text)
    return ascii_text.strip()


def contains_normalized_keyword(text: str, keyword: str) -> bool:
    keyword = normalize_search_text(keyword)
    if not keyword:
        return False

    if " " in keyword:
        return keyword in text

    pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
    return bool(re.search(pattern, text))


def matched_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if contains_normalized_keyword(text, keyword)]


def classify_content_quality(
    post_text: str,
    comments: list[dict[str, str]],
    continuations: list[dict[str, str]] | None = None,
) -> tuple[bool, str]:
    normalized_post = normalize_search_text(post_text)
    normalized_comments = [normalize_search_text(comment.get("text", "")) for comment in comments]
    continuation_count = len(continuations or [])

    if not normalized_post:
        return False, "missing post text"

    sales_hits = matched_keywords(normalized_post, SALES_KEYWORDS)
    strong_sales_hits = [keyword for keyword in sales_hits if keyword not in WEAK_SALES_KEYWORDS]
    weak_sales_hits = [keyword for keyword in sales_hits if keyword in WEAK_SALES_KEYWORDS]
    self_promo_hits = matched_keywords(normalized_post, SELF_PROMO_KEYWORDS)
    has_price = bool(re.search(r"\b\d{2,3}(?:[.,]\d{3})+\b|\b\d+\s*(k|tr|cu|usd)\b", normalized_post))
    has_contact = bool(re.search(r"\b\d{9,11}\b|zalo|sdt|so dien thoai", normalized_post))

    if len(strong_sales_hits) >= 2:
        return False, f"sales/promotional post keywords={','.join(strong_sales_hits[:3])}"
    if strong_sales_hits and (has_price or has_contact):
        return False, f"sales/promotional post keywords={','.join(strong_sales_hits[:3])}"
    if len(weak_sales_hits) >= 2 and (has_price or has_contact):
        return False, f"sales/promotional post keywords={','.join(weak_sales_hits[:3])}"
    if len(self_promo_hits) >= 2:
        return False, "self-promotional post"

    discussion_hits = [keyword for keyword in DISCUSSION_KEYWORDS if keyword in normalized_post]
    story_hits = [keyword for keyword in STORY_KEYWORDS if keyword in normalized_post]
    topic_hits = [keyword for keyword in HOT_TOPIC_KEYWORDS if keyword in normalized_post]

    discussion_score = 0
    story_score = 0

    if discussion_hits:
        discussion_score += 2
    if "?" in post_text:
        discussion_score += 1
    if len(normalized_post.split()) >= 18:
        discussion_score += 1
    if story_hits:
        story_score += 2
    if topic_hits:
        story_score += 2
    if len(normalized_post.split()) >= 22:
        story_score += 1
    if continuation_count >= 1:
        story_score += 2
    if continuation_count >= 2:
        story_score += 1

    substantive_comments = [text for text in normalized_comments if len(text.split()) >= 8]
    if len(substantive_comments) >= 3:
        discussion_score += 2
        story_score += 1
    elif len(substantive_comments) >= 2:
        discussion_score += 1

    reasoning_comments = sum(
        1 for text in normalized_comments if any(keyword in text for keyword in COMMENT_REASONING_KEYWORDS)
    )
    if reasoning_comments >= 2:
        discussion_score += 2
    elif reasoning_comments >= 1:
        discussion_score += 1

    unique_comment_count = len({text for text in normalized_comments if text})
    if unique_comment_count >= 4:
        discussion_score += 1
        story_score += 1

    has_split_opinions = unique_comment_count >= 3 and reasoning_comments >= 1
    if discussion_score >= 3 and has_split_opinions:
        return True, f"discussion accepted keywords={','.join(discussion_hits[:2]) or 'none'}"

    if story_score >= 4 and (len(substantive_comments) >= 2 or continuation_count >= 1):
        topic_reason = ",".join((topic_hits[:2] + story_hits[:2])) or "story/topic"
        return True, f"story/topic accepted keywords={topic_reason}"

    if continuation_count >= 1 and len(normalized_post.split()) >= 40:
        return True, "story accepted by continuation flow"

    if len(substantive_comments) < 2 and continuation_count == 0:
        return False, "not enough substantive comments"
    if not (discussion_hits or story_hits or topic_hits):
        return False, "missing discussion/story/topic signals"
    return False, "weak discussion/story signals"


def strip_leading_handle_and_time(text: str) -> str:
    text = clean_text(text)
    text = re.sub(
        r"^(?:Pinned\s+)?(?:@?[A-Za-z0-9_.]+(?:\s+[A-Za-z0-9_.&'/-]+){0,8})\s+\d+\s*[mhdsw]\b\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^@?[A-Za-z0-9_.]{3,}\s+\d+\s*[mhdsw]\b\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d+\s*[mhdsw]\b\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^@?[A-Za-z0-9.]*_[A-Za-z0-9_.-]*\b[\s:.,;-]*", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def strip_leading_known_author(text: str, *author_values: object) -> str:
    text = clean_text(text)
    candidates = []
    for value in author_values:
        raw = str(value or "").strip()
        if not raw:
            continue
        candidates.extend(
            [
                raw,
                raw.lstrip("@"),
                raw.replace("@", ""),
                raw.replace("_", " "),
            ]
        )

    for candidate in sorted({item for item in candidates if item}, key=len, reverse=True):
        text = re.sub(rf"^@?{re.escape(candidate)}\b[\s:.,;-]*", " ", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^\(?\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)?[\s:.,;-]*", " ", text)
    return clean_text(text)


def strip_trailing_metrics(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"(?:\s+\d+(?:[.,]\d+)?K?){2,}\s*$", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def parse_metric_value(value: str) -> float:
    compact = (value or "").strip().upper().replace(",", ".")
    if not compact:
        return 0.0
    multiplier = 1.0
    if compact.endswith("K"):
        multiplier = 1000.0
        compact = compact[:-1]
    elif compact.endswith("M"):
        multiplier = 1000000.0
        compact = compact[:-1]
    try:
        return float(compact) * multiplier
    except ValueError:
        return 0.0


def extract_engagement_score(text: str) -> int:
    matches = re.findall(r"(\d+(?:[.,]\d+)?(?:K|M)?)", text or "", flags=re.IGNORECASE)
    if not matches:
        return 0
    values = [parse_metric_value(item) for item in matches[-4:]]
    if not values:
        return 0
    if len(values) == 1:
        return int(values[0])
    like_score = values[0]
    comment_score = values[1] if len(values) > 1 else 0
    repost_score = values[2] if len(values) > 2 else 0
    share_score = values[3] if len(values) > 3 else 0
    weighted = like_score + comment_score * 3.0 + repost_score * 2.0 + share_score * 1.5
    return int(weighted)


def cleanup_screen_text(text: str, *, is_comment: bool = False) -> str:
    text = trim_ui_text(text)
    text = re.sub(r"\bAuthor\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTop\s+View activity\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bReply\s+to\s+[A-Za-z0-9_.-]+\.{0,3}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTranslate\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)\b\d+\s*/\s*\d+\b(?!\d)", " ", text)
    text = re.sub(r"\b\d+\s*[smhdw]\b", " ", text, flags=re.IGNORECASE)
    text = strip_threads_context_noise(text)
    text = strip_trailing_metrics(text)
    text = strip_threads_context_noise(text)
    text = strip_leading_handle_and_time(text)
    text = re.sub(r"^\(?\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)?[\s:.,;-]*", " ", text)
    text = clean_text(text)

    if is_comment:
        text = re.sub(r"^\u00b7\s*", "", text)
    return clean_text(text)


def is_low_value_comment(text: str) -> bool:
    normalized = normalize_search_text(text)
    if not normalized:
        return True
    navigation_patterns = (
        r"\bde nghi\b.*\bchu tus\b.*\bso\b.*\btheo doi\b",
        r"\bchu tus\b.*\bde so\b",
        r"\bso len truoc\b",
        r"\bcho xin phan\b",
        r"\btag\b.*\bphan\b",
    )
    return any(re.search(pattern, normalized) for pattern in navigation_patterns)


def build_basic_narrator_script(post_text: str, comments: list[dict[str, str]]) -> str:
    story_segments = [
        cleanup_screen_text(segment, is_comment=False)
        for segment in re.split(r"\n{2,}", post_text or "")
        if cleanup_screen_text(segment, is_comment=False)
    ]
    if not story_segments:
        cleaned_post = cleanup_screen_text(post_text, is_comment=False)
        story_segments = [cleaned_post] if cleaned_post else []

    comment_texts = [
        cleanup_screen_text(c.get("text", ""), is_comment=True)
        for c in comments
        if cleanup_screen_text(c.get("text", ""), is_comment=True)
    ]
    parts = []
    parts.extend(story_segments[:MAX_TOTAL_SEGMENTS])
    parts.extend(comment_texts[: max(0, MAX_TOTAL_SEGMENTS - len(parts))])

    script = "\n".join(parts)
    return script[:MAX_SCRIPT_TEXT_LENGTH]


def build_narrator_script(post_text: str, comments: list[dict[str, str]]) -> str:
    return build_basic_narrator_script(post_text, comments)


def trim_ui_text(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"\bTop\s+View activity\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bReply\s+to\s+[A-Za-z0-9_.-]+\.{0,3}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTranslate\b", " ", text, flags=re.IGNORECASE)
    return clean_text(text)


def dedupe_comments(comments: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = []
    seen = set()

    for comment in comments:
        text = cleanup_screen_text(comment.get("text", ""), is_comment=True)
        if not text or is_low_value_comment(text):
            continue
        normalized = re.sub(r"[^0-9A-Za-z\u00c0-\u1ef9]+", "", text.lower())
        # Drop nested duplicate comment text: keep the richer block with author
        if any(normalized and normalized in old for old in seen):
            continue
        seen.add(normalized)
        unique.append(
            {
                "text": text,
                "author_name": comment.get("author_name", ""),
                "author_key": comment.get("author_key", ""),
            }
        )

    return unique


def extract_block_metadata(text: str) -> dict[str, object]:
    raw_text = clean_text(text)
    stripped = re.sub(r"^\s*Pinned\s+", "", raw_text, flags=re.IGNORECASE)
    match = re.match(
        r"^(?P<author>@?[A-Za-z0-9_.]+(?:\s+[A-Za-z0-9_.&'/-]+){0,8})\s+\d+\s*[mhdsw]\b(?P<rest>.*)$",
        stripped,
        flags=re.IGNORECASE,
    )

    author_name = ""
    remainder = stripped
    if match:
        author_name = clean_text(match.group("author"))
        remainder = clean_text(match.group("rest"))

    body_text = cleanup_screen_text(raw_text, is_comment=False)
    author_key = normalize_search_text(author_name) if author_name else ""
    has_author_badge = bool(re.search(r"\bAuthor\b", remainder, flags=re.IGNORECASE))
    has_continuation_marker = bool(
        CONTINUATION_MARKER_RE.search(remainder) or CONTINUATION_MARKER_RE.search(body_text)
    )

    return {
        "author_name": author_name,
        "author_key": author_key,
        "has_author_badge": has_author_badge,
        "has_continuation_marker": has_continuation_marker,
        "engagement_score": extract_engagement_score(raw_text),
        "body_text": body_text,
        "raw_text": raw_text,
    }


def combine_story_segments(post_text: str, continuations: list[dict[str, str]]) -> str:
    segments = [cleanup_screen_text(post_text, is_comment=False)]
    segments.extend(
        cleanup_screen_text(item.get("text", ""), is_comment=False)
        for item in continuations
    )

    unique_segments = []
    seen = set()
    for segment in segments:
        if not segment:
            continue
        normalized = re.sub(r"[^0-9A-Za-z\u00c0-\u1ef9]+", "", segment.lower())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_segments.append(segment)

    return "\n\n".join(unique_segments)[:MAX_SCRIPT_TEXT_LENGTH]


def remove_embedded_continuations_from_post(post_text: str, continuations: list[dict[str, str]]) -> str:
    cleaned_post = cleanup_screen_text(post_text, is_comment=False)
    if not cleaned_post or not continuations:
        return cleaned_post

    continuation_numbers = []
    for item in continuations:
        text = cleanup_screen_text(item.get("text", ""), is_comment=False)
        match = re.search(r"\((\d{1,2})\)", text)
        if match:
            continuation_numbers.append(int(match.group(1)))

    if continuation_numbers:
        first_continuation = min(continuation_numbers)
        marker = re.search(rf"\({first_continuation}\)", cleaned_post)
        if marker:
            return cleanup_screen_text(cleaned_post[: marker.start()], is_comment=False)

    for item in continuations:
        text = cleanup_screen_text(item.get("text", ""), is_comment=False)
        if len(text) < 40:
            continue
        leading_words = " ".join(text.split()[:8])
        if leading_words and leading_words in cleaned_post:
            return cleanup_screen_text(cleaned_post.split(leading_words, 1)[0], is_comment=False)

    return cleaned_post


def detected_story_part_numbers(text: str) -> set[int]:
    numbers = set()
    for match in re.finditer(r"(?<!\d)\(?([1-9]\d?)\)?\s*/\s*([1-9]\d?)(?!\d)", text or ""):
        numbers.add(int(match.group(1)))
    for match in re.finditer(r"(?<!\d)\(([1-9]\d?)\)", text or ""):
        numbers.add(int(match.group(1)))
    return numbers


def expected_story_part_count(text: str) -> int:
    totals = [
        int(match.group(2))
        for match in re.finditer(r"(?<!\d)\(?([1-9]\d?)\)?\s*/\s*([1-9]\d?)(?!\d)", text or "")
    ]
    if totals:
        return max(totals)
    numbers = detected_story_part_numbers(text)
    return max(numbers) if numbers else 0


def story_capture_gap(post_text: str, continuations: list[dict[str, str]]) -> tuple[bool, str]:
    combined = "\n".join([post_text, *[item.get("text", "") for item in continuations]])
    expected = expected_story_part_count(combined)
    if expected <= 1:
        return False, ""

    captured_numbers = detected_story_part_numbers(combined)
    missing = [number for number in range(1, expected + 1) if number not in captured_numbers]
    if missing:
        return True, f"missing story parts {','.join(str(number) for number in missing)}/{expected}"
    return False, ""


def build_content_segments(
    post_text: str,
    continuations: list[dict[str, str]],
    comments: list[dict[str, str]],
    post_author: str = "",
) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    image_index = 0
    post_author_key = normalize_search_text(post_author)

    cleaned_post = cleanup_screen_text(post_text, is_comment=False)
    cleaned_post = strip_leading_known_author(cleaned_post, post_author, post_author_key)
    if cleaned_post:
        segments.append(
            {
                "type": "post",
                "text": cleaned_post,
                "image_index": image_index,
                "author_name": post_author,
                "author_key": post_author_key,
            }
        )

    for item in continuations:
        cleaned = cleanup_screen_text(item.get("text", ""), is_comment=False)
        cleaned = strip_leading_known_author(
            cleaned,
            item.get("author_name", post_author),
            item.get("author_key", post_author_key),
        )
        if not cleaned:
            continue
        image_index += 1
        segments.append(
            {
                "type": "continuation",
                "text": cleaned,
                "image_index": image_index,
                "author_name": item.get("author_name", post_author),
                "author_key": item.get("author_key", post_author_key),
            }
        )

    for item in comments:
        cleaned = cleanup_screen_text(item.get("text", ""), is_comment=True)
        cleaned = strip_leading_known_author(
            cleaned,
            item.get("author_name", ""),
            item.get("author_key", ""),
        )
        if not cleaned:
            continue
        image_index += 1
        segments.append(
            {
                "type": "comment",
                "text": cleaned,
                "image_index": image_index,
                "author_name": item.get("author_name", ""),
                "author_key": item.get("author_key", ""),
            }
        )

    return segments


def detect_content_mode(post_text: str, continuations: list[dict[str, str]], comments: list[dict[str, str]]) -> str:
    if continuations:
        return "story"

    normalized_post = normalize_search_text(post_text)
    discussion_hits = [keyword for keyword in DISCUSSION_KEYWORDS if keyword in normalized_post]
    reasoning_comments = [
        comment for comment in comments
        if any(keyword in normalize_search_text(comment.get("text", "")) for keyword in COMMENT_REASONING_KEYWORDS)
    ]
    if discussion_hits or len(reasoning_comments) >= 2 or len(comments) >= 4:
        return "discussion"
    return "general"


def is_continuation_block(block: dict, post_block: dict, sequence_index: int, post_author: str = "") -> bool:
    block_meta = block.get("meta", {})
    post_meta = post_block.get("meta", {})
    block_author_key = str(block_meta.get("author_key") or "")
    post_author_key = str(post_meta.get("author_key") or "")
    same_author = bool(block_author_key) and (
        (post_author_key and block_author_key == post_author_key)
        or (post_author and block_author_key == normalize_search_text(post_author))
    )
    if not same_author:
        return False

    if block_meta.get("has_continuation_marker"):
        return True

    post_has_split_marker = bool(post_meta.get("has_continuation_marker"))
    if post_has_split_marker and sequence_index <= 2:
        body_text = str(block_meta.get("body_text") or "")
        if len(body_text.split()) >= 18:
            return True

    if block_meta.get("has_author_badge") and sequence_index == 1:
        body_text = str(block_meta.get("body_text") or "")
        if len(body_text.split()) >= 24:
            return True

    return False


async def first_visible(page: Page, selectors: list[str], timeout_ms: int):
    last_error: Exception | None = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError as exc:
            last_error = exc
    raise RuntimeError(f"Could not find visible selector from: {selectors}") from last_error


async def login(page: Page, config: Config) -> None:
    log("Opening Threads login page...")
    await page.goto(THREADS_LOGIN_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
    username_input = await first_visible(
        page,
        ['input[autocomplete="username"]', 'input[name="username"]', 'input[type="text"]'],
        config.timeout_ms,
    )
    await username_input.fill(config.username)

    password_input = await first_visible(
        page,
        ['input[type="password"]', 'input[autocomplete="current-password"]'],
        config.timeout_ms,
    )
    await password_input.fill(config.password)
    await password_input.press("Enter")
    await page.wait_for_timeout(config.post_login_wait_ms)
    log(f"Login submitted. url={page.url}")


async def has_valid_session(page: Page, url: str, config: Config) -> bool:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
        await page.wait_for_timeout(3000)
        if "login" in page.url.lower():
            return False
        return await page.locator('input[type="password"]').count() == 0
    except Exception:
        return False


async def write_debug_artifacts(page: Page, config: Config, label: str) -> None:
    config.debug_dir.mkdir(parents=True, exist_ok=True)
    label = safe_filename(label)
    try:
        await page.screenshot(path=str(config.debug_dir / f"{label}.png"), full_page=True)
    except Exception as exc:
        log(f"Could not write debug screenshot: {exc}")
    try:
        (config.debug_dir / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    except Exception as exc:
        log(f"Could not write debug HTML: {exc}")


async def extract_visible_content(page: Page) -> dict:
    return await page.evaluate(
        """
        () => {
          const candidates = Array.from(document.querySelectorAll('article, [role="article"], main, body'));
          const ranked = candidates
            .map((el) => ({
              tag: el.tagName,
              text: (el.innerText || '').trim(),
              rect: (() => {
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, width: r.width, height: r.height };
              })(),
            }))
            .filter((item) => item.text.length > 20)
            .sort((a, b) => b.text.length - a.text.length);

          const primary = ranked[0] || { text: document.body ? document.body.innerText : '', rect: null };
          const comments = ranked.slice(1, 6).map((item) => ({ text: item.text.slice(0, 600) }));
          return {
            post_text: primary.text.slice(0, 1800),
            comments,
            page_title: document.title,
            current_url: location.href,
          };
        }
        """
    )


async def screenshot_primary_area(page: Page, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    locator = page.locator(f"{POST_LOCATOR}, article, [role='article'], main").first
    try:
        await locator.wait_for(state="visible", timeout=5000)
        await locator.screenshot(path=str(output_path))
    except Exception:
        await page.screenshot(path=str(output_path), full_page=True)


async def prepare_threads_page(page: Page) -> None:
    await page.evaluate(
        """
        () => {
          const header = document.getElementById('barcelona-header');
          if (header) header.style.display = 'none';

          for (const el of document.querySelectorAll('[role="banner"], nav')) {
            const rect = el.getBoundingClientRect();
            if (rect.y <= 5 && rect.height < 120) el.style.display = 'none';
          }

          for (const el of document.querySelectorAll('[role="tooltip"], [aria-live], [data-visualcompletion="ignore"]')) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 20 && rect.height > 10) el.style.display = 'none';
          }

          for (const el of document.querySelectorAll('body *')) {
            const style = window.getComputedStyle(el);
            if (style.position !== 'fixed' && style.position !== 'sticky') continue;
            const rect = el.getBoundingClientRect();
            const z = Number.parseInt(style.zIndex || '0', 10) || 0;
            const text = (el.innerText || '').trim();
            const looksLikeOverlay =
              z >= 10 &&
              rect.width > 40 &&
              rect.height > 20 &&
              rect.left < window.innerWidth &&
              rect.top < window.innerHeight &&
              !el.closest('[role="main"], main, article');
            if (looksLikeOverlay || /correctly on|translate|reply to/i.test(text)) {
              el.style.display = 'none';
            }
          }
        }
        """
    )


async def wait_for_element_media(page: Page, handle) -> None:
    try:
        await page.evaluate(
            """
            async (el) => {
              const media = Array.from(el.querySelectorAll('img, video'));
              await Promise.all(media.map((item) => {
                if (item.tagName === 'IMG') {
                  if (item.complete) return true;
                  return new Promise((resolve) => {
                    item.addEventListener('load', resolve, { once: true });
                    item.addEventListener('error', resolve, { once: true });
                    setTimeout(resolve, 2500);
                  });
                }
                if (item.readyState >= 2) return true;
                return new Promise((resolve) => {
                  item.addEventListener('loadeddata', resolve, { once: true });
                  item.addEventListener('error', resolve, { once: true });
                  setTimeout(resolve, 2500);
                });
              }));
            }
            """,
            handle,
        )
    except Exception as exc:
        log(f"Media wait skipped: {exc}")


async def get_pressable_blocks(page: Page) -> list[dict]:
    handles = await page.query_selector_all(POST_LOCATOR)
    blocks = []

    for index, handle in enumerate(handles):
        try:
            text = trim_ui_text(await handle.inner_text())
            box = await handle.bounding_box()
            if not box:
                continue
            blocks.append(
                {
                    "index": index,
                    "handle": handle,
                    "text": text,
                    "rect": box,
                    "meta": extract_block_metadata(text),
                }
            )
        except Exception:
            continue

    return blocks


def is_reasonable_pressable(block: dict) -> bool:
    text = clean_text(block.get("text", ""))
    rect = block.get("rect", {})
    if len(text) < 18:
        return False
    if rect.get("width", 0) < 320 or rect.get("height", 0) < 44:
        return False
    if rect.get("height", 0) > 1400:
        return False
    noise = ("For you New thread Search Activity Profile", "Log in", "Sign up", "Not all who wander")
    return not any(fragment.lower() in text.lower() for fragment in noise)


def choose_post_and_comments(
    blocks: list[dict],
    post_author: str,
    max_comments: int,
) -> tuple[dict | None, list[dict], list[dict], str]:
    reasonable = [block for block in blocks if is_reasonable_pressable(block)]
    if not reasonable:
        return None, [], [], "general"

    vietnamese_blocks = [block for block in reasonable if is_vietnamese(block.get("text", ""))]
    post_candidates = sorted(
        vietnamese_blocks or reasonable,
        key=lambda block: (
            0 if len(re.findall(r"\d+(?:[.,]\d+)?K?|\d+", block.get("text", ""), re.IGNORECASE)) >= 2 else 1,
            block.get("rect", {}).get("y", 99999),
            -block.get("rect", {}).get("height", 0),
        ),
    )
    post_block = post_candidates[0]
    post_rect = post_block.get("rect", {})
    post_y = post_rect.get("y", 0)
    post_bottom = post_y + post_rect.get("height", 0)

    continuation_blocks = []
    regular_comments = []
    seen = {clean_text(post_block.get("text", ""))[:180]}
    for sequence_index, block in enumerate(reasonable, start=1):
        if block is post_block:
            continue

        text = clean_text(block.get("text", ""))
        rect = block.get("rect", {})
        text_key = text[:180]
        if not text or text_key in seen:
            continue
        if rect.get("y", 0) < post_bottom - 30:
            continue
        if rect.get("height", 0) > 360:
            continue
        if not is_vietnamese(text):
            continue

        seen.add(text_key)
        if is_continuation_block(block, post_block, sequence_index, post_author):
            continuation_blocks.append(block)
            continue
        regular_comments.append(block)

    continuation_blocks.sort(key=lambda block: block.get("rect", {}).get("y", 99999))
    regular_comments.sort(
        key=lambda block: (
            -int(block.get("meta", {}).get("engagement_score") or 0),
            block.get("rect", {}).get("y", 99999),
        )
    )

    if continuation_blocks:
        # Story-first rule: fill slots with the author's continuation blocks first,
        # then use any remaining room for outside comments.
        selected_continuations = continuation_blocks[: min(MAX_STORY_CONTINUATIONS, max_comments)]
        remaining_slots = max(0, max_comments - len(selected_continuations))
        selected_comments = regular_comments[:remaining_slots]
        return post_block, selected_continuations, selected_comments, "story"

    selected_comments = regular_comments[: max(max_comments, TARGET_DISCUSSION_COMMENT_COUNT)]
    mode = "discussion" if len(selected_comments) >= 2 else "general"
    return post_block, [], selected_comments, mode


async def refresh_block_box(block: dict) -> dict:
    handle = block["handle"]
    try:
        await handle.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    box = await handle.bounding_box()
    if not box:
        return block.get("rect", {})
    block["rect"] = box
    return box


async def trim_post_rect_before_discussion_toolbar(page: Page, rect: dict) -> dict:
    toolbar_y = await page.evaluate(
        """
        (rect) => {
          const minY = rect.y + 70;
          const maxY = rect.y + rect.height;
          const matches = Array.from(document.querySelectorAll('div, span'))
            .map((el) => {
              const text = (el.innerText || '').trim();
              const r = el.getBoundingClientRect();
              return { text, y: r.y, width: r.width, height: r.height };
            })
            .filter((item) =>
              item.y > minY &&
              item.y < maxY &&
              item.width > 20 &&
              item.height > 8 &&
              (
                item.text === 'Top' ||
                item.text === 'View activity' ||
                item.text.includes('View activity')
              )
            )
            .sort((a, b) => a.y - b.y);

          return matches.length ? matches[0].y : null;
        }
        """,
        rect,
    )
    if toolbar_y and toolbar_y > rect.get("y", 0) + 90:
        rect = dict(rect)
        rect["height"] = max(90, toolbar_y - rect["y"] - 4)
    return rect


async def screenshot_clip(page: Page, rect: dict, output_path: Path, padding: int = 8) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    viewport = page.viewport_size or {"width": 1365, "height": 900}
    viewport_width = float(viewport["width"])
    viewport_height = float(viewport["height"])

    left = float(rect.get("x", 0)) - padding
    top = float(rect.get("y", 0)) - padding
    right = float(rect.get("x", 0)) + float(rect.get("width", 0)) + padding
    bottom = float(rect.get("y", 0)) + float(rect.get("height", 0)) + padding

    x = max(0.0, min(left, viewport_width))
    y = max(0.0, min(top, viewport_height))
    right = max(0.0, min(right, viewport_width))
    bottom = max(0.0, min(bottom, viewport_height))
    width = right - x
    height = bottom - y

    if width < 1 or height < 1:
        log(
            "Skipping screenshot clip outside viewport: "
            f"rect={rect} viewport={viewport}"
        )
        return False

    await page.screenshot(
        path=str(output_path),
        clip={"x": x, "y": y, "width": width, "height": height},
    )
    return True


async def screenshot_block(page: Page, block: dict, output_path: Path, padding: int = 8) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle = block.get("handle")
    if handle:
        try:
            await handle.scroll_into_view_if_needed(timeout=5000)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            await page.mouse.move(1, 1)
            await prepare_threads_page(page)
            await page.wait_for_timeout(250)
            await wait_for_element_media(page, handle)
            await handle.screenshot(path=str(output_path), timeout=8000)
            return True
        except Exception as exc:
            log(f"Element screenshot failed, falling back to viewport clip: {exc}")

    rect = await refresh_block_box(block)
    return await screenshot_clip(page, rect, output_path, padding=padding)


async def screenshot_post_and_comments(
    page: Page,
    post_id: str,
    post_dir: Path,
    max_comments: int,
    url: str = "",
) -> tuple[dict, dict]:
    post_path = post_dir / "post.png"
    comments_dir = post_dir / "comments"
    best_post_block = None
    best_continuation_blocks: list[dict] = []
    best_comment_blocks: list[dict] = []
    best_mode = "general"
    post_author = extract_author_from_url(url)
    accumulated_continuations: list[dict] = []
    accumulated_comments: list[dict] = []
    accumulated_keys: set[str] = set()

    await prepare_threads_page(page)
    await page.locator(POST_LOCATOR).first.wait_for(state="visible", timeout=10000)

    for attempt in range(1, MAX_COMMENT_CAPTURE_ATTEMPTS + 1):
        blocks = await get_pressable_blocks(page)
        post_block, continuation_blocks, comment_blocks, detected_mode = choose_post_and_comments(blocks, post_author, max_comments)

        if post_block and not best_post_block:
            best_post_block = post_block

        active_post_block = best_post_block or post_block
        if active_post_block:
            sequence_index = len(accumulated_continuations) + len(accumulated_comments) + 1
            post_key = clean_text(active_post_block.get("text", ""))[:180]
            for block in [item for item in blocks if is_reasonable_pressable(item)]:
                if block is active_post_block:
                    continue
                text = clean_text(block.get("text", ""))
                key = text[:180]
                if not text or key == post_key or key in accumulated_keys:
                    continue
                if not is_vietnamese(text):
                    continue
                accumulated_keys.add(key)
                if is_continuation_block(block, active_post_block, sequence_index, post_author):
                    accumulated_continuations.append(block)
                else:
                    accumulated_comments.append(block)
                sequence_index += 1

        current_total = len(continuation_blocks) + len(comment_blocks)
        best_total = len(best_continuation_blocks) + len(best_comment_blocks)
        if post_block and current_total > best_total:
            best_post_block = post_block
            best_continuation_blocks = continuation_blocks
            best_comment_blocks = comment_blocks
            best_mode = detected_mode

        log(
            f"Comment capture attempt {attempt}/{MAX_COMMENT_CAPTURE_ATTEMPTS}: "
            f"found {len(continuation_blocks)} continuations and {len(comment_blocks)} comments; "
            f"accumulated {len(accumulated_continuations)} continuations and {len(accumulated_comments)} comments"
        )

        target_total = MAX_STORY_CONTINUATIONS if detected_mode == "story" else max(max_comments, TARGET_DISCUSSION_COMMENT_COUNT)
        accumulated_total = len(accumulated_continuations) + len(accumulated_comments)
        if active_post_block and accumulated_total >= target_total:
            best_post_block = active_post_block
            best_continuation_blocks = accumulated_continuations
            best_comment_blocks = accumulated_comments
            best_mode = detected_mode
            break

        if attempt < MAX_COMMENT_CAPTURE_ATTEMPTS:
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
            await page.wait_for_timeout(COMMENT_CAPTURE_WAIT_MS)
            await prepare_threads_page(page)

    post_block = best_post_block
    if accumulated_continuations or accumulated_comments:
        continuation_blocks = accumulated_continuations[: min(MAX_STORY_CONTINUATIONS, max_comments)]
        remaining_slots = max(0, max_comments - len(continuation_blocks))
        comment_blocks = accumulated_comments[:remaining_slots]
    else:
        continuation_blocks = best_continuation_blocks
        comment_blocks = best_comment_blocks

    if not post_block:
        log("Could not isolate post block from pressable containers; falling back to first visible post area.")
        await screenshot_primary_area(page, post_path)
        return {"post": str(post_path), "comments": []}, {"post_text": "", "comments": []}

    await wait_for_element_media(page, post_block["handle"])
    post_rect = await refresh_block_box(post_block)
    post_rect = await trim_post_rect_before_discussion_toolbar(page, post_rect)
    if not await screenshot_clip(page, post_rect, post_path, padding=8):
        log("Could not capture isolated post clip; falling back to primary visible area.")
        await screenshot_primary_area(page, post_path)

    comment_paths = []
    extracted_comments = []
    extracted_continuations = []
    ordered_blocks = continuation_blocks + comment_blocks
    for index, block in enumerate(ordered_blocks, start=1):
        comment_path = comments_dir / f"comment_{index:02d}.png"
        captured = await screenshot_block(page, block, comment_path, padding=8)
        if not captured:
            log(f"Skipping extracted block without screenshot: index={index}")
            continue

        comment_paths.append(str(comment_path))
        cleaned_text = trim_ui_text(block.get("text", ""))
        meta = block.get("meta", {})
        content_item = {
            "text": cleaned_text,
            "author_name": meta.get("author_name", ""),
            "author_key": meta.get("author_key", ""),
        }
        if is_continuation_block(block, post_block, index, post_author):
            extracted_continuations.append(content_item)
        else:
            extracted_comments.append(content_item)

    screenshots = {"post": str(post_path), "comments": comment_paths}
    extracted = {
        "post_text": cleanup_screen_text(post_block.get("text", ""), is_comment=False),
        "continuations": dedupe_comments(extracted_continuations),
        "comments": dedupe_comments(extracted_comments),
        "capture_mode": best_mode,
    }
    return screenshots, extracted


async def process_post(post_id: str, url: str, config: Config) -> dict:
    url = canonical_url(url)
    run_date = datetime.now().strftime("%Y-%m-%d")
    post_dir = config.screenshots_dir / run_date / safe_filename(post_id)

    async with async_playwright() as playwright:
        launch_kwargs = {"headless": config.headless}
        executable_path = chromium_executable_path()
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = await playwright.chromium.launch(**launch_kwargs)
        context_options = {
            "viewport": {"width": 1365, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
            ),
        }
        if config.storage_state.exists() and not config.force_login:
            context_options["storage_state"] = str(config.storage_state)
            log(f"Using saved session: {config.storage_state}")

        context = await browser.new_context(**context_options)
        page = await context.new_page()
        try:
            if config.force_login or not await has_valid_session(page, url, config):
                await login(page, config)
                config.storage_state.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(config.storage_state))

            log(f"Opening post: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=config.timeout_ms)
            await page.wait_for_timeout(5000)

            screenshots, isolated_content = await screenshot_post_and_comments(
                page,
                post_id=post_id,
                post_dir=post_dir,
                max_comments=MAX_TOTAL_SEGMENTS,
                url=url,
            )
            content = await extract_visible_content(page)

            continuation_blocks = dedupe_comments([
                {
                    "text": cleanup_screen_text(comment.get("text", ""), is_comment=False),
                    "author_name": comment.get("author_name", ""),
                    "author_key": comment.get("author_key", ""),
                }
                for comment in (isolated_content.get("continuations", []) or [])
                if cleanup_screen_text(comment.get("text", ""), is_comment=False)
            ])
            base_post_text = (
                cleanup_screen_text(isolated_content.get("post_text", ""), is_comment=False)
                or cleanup_screen_text(content.get("post_text", ""), is_comment=False)
            )
            base_post_text = remove_embedded_continuations_from_post(base_post_text, continuation_blocks)
            post_text = combine_story_segments(base_post_text, continuation_blocks)
            comments = dedupe_comments([
                {
                    "text": cleanup_screen_text(comment.get("text", ""), is_comment=True),
                    "author_name": comment.get("author_name", ""),
                    "author_key": comment.get("author_key", ""),
                }
                for comment in (isolated_content.get("comments", []) or [])
                if cleanup_screen_text(comment.get("text", ""), is_comment=True)
            ])
            content_mode = detect_content_mode(base_post_text, continuation_blocks, comments)
            script_comments = comments[:2] if content_mode == "story" else comments
            narrator_script = build_narrator_script(post_text, script_comments)
            comment_count = len(comments)
            continuation_count = len(continuation_blocks)
            supporting_block_count = comment_count + continuation_count
            content_ok, content_reason = classify_content_quality(post_text, comments, continuation_blocks)
            story_incomplete, story_gap_reason = story_capture_gap(base_post_text, continuation_blocks)

            extracted = {
                "post_text": post_text,
                "continuations": continuation_blocks,
                "comments": comments,
                "segments": build_content_segments(base_post_text, continuation_blocks, script_comments, post_author=extract_author_from_url(url)),
                "content_mode": content_mode,
                "page_title": content.get("page_title", ""),
                "current_url": content.get("current_url", page.url),
            }

            screenshot_comment_count = len(screenshots.get("comments", []))
            note = (
                f"Phase 2: isolated post screenshot + {screenshot_comment_count} comment screenshots; "
                f"extracted {comment_count} comments and {continuation_count} continuation parts mode={content_mode}"
            )
            status = "In Progress"
            min_required_blocks = 2 if content_mode == "story" else MIN_ACCEPTED_COMMENT_COUNT
            target_blocks = MAX_STORY_CONTINUATIONS if content_mode == "story" else TARGET_DISCUSSION_COMMENT_COUNT

            if not post_text:
                note = "Phase 2 warning: screenshot saved but no text extracted"
                status = "Rejected"
            elif story_incomplete:
                note = f"Phase 2 rejected: incomplete author story capture ({story_gap_reason})"
                status = "Rejected"
            elif supporting_block_count < min_required_blocks:
                note = (
                    f"Phase 2 rejected: only extracted {supporting_block_count}/{target_blocks} supporting blocks "
                    f"after {MAX_COMMENT_CAPTURE_ATTEMPTS} attempts"
                )
                status = "Rejected"
            elif not content_ok:
                note = f"Phase 2 rejected: {content_reason}"
                status = "Rejected"
            elif supporting_block_count < target_blocks:
                note = (
                    f"Phase 2 warning: extracted {supporting_block_count}/{target_blocks} supporting blocks "
                    f"after {MAX_COMMENT_CAPTURE_ATTEMPTS} attempts"
                )

            return {
                "ID": post_id,
                "Screenshots": json.dumps(screenshots, ensure_ascii=True),
                "Extracted_Content": json.dumps(extracted, ensure_ascii=True),
                "Narrator_Script": narrator_script,
                "Status": status,
                "Note": note,
            }
        except Exception:
            await write_debug_artifacts(page, config, f"screenshot_extract_failure_{post_id}")
            raise
        finally:
            await context.close()
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Screenshot and extract one Threads post.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", default=os.getenv("THREADS_USERNAME"))
    parser.add_argument("--password", default=os.getenv("THREADS_PASSWORD"))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("THREADS_TIMEOUT_MS", "30000")))
    parser.add_argument(
        "--post-login-wait-ms",
        type=int,
        default=int(os.getenv("THREADS_POST_LOGIN_WAIT_MS", "5000")),
    )
    parser.add_argument("--storage-state", default=os.getenv("THREADS_STORAGE_STATE", "runtime/storage/threads-state.json"))
    parser.add_argument("--debug-dir", default=os.getenv("THREADS_DEBUG_DIR", "runtime/debug"))
    parser.add_argument("--screenshots-dir", default=os.getenv("SCREENSHOTS_DIR", "runtime/data/screenshots"))
    parser.add_argument(
        "--force-login",
        action="store_true",
        default=os.getenv("THREADS_FORCE_LOGIN", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument("--headful", action="store_true")
    return parser.parse_args()


def resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    args = parse_args()

    if not args.username or not args.password:
        log("Missing THREADS_USERNAME or THREADS_PASSWORD.")
        return 2

    config = Config(
        username=args.username,
        password=args.password,
        storage_state=resolve_path(args.storage_state),
        screenshots_dir=resolve_path(args.screenshots_dir),
        debug_dir=resolve_path(args.debug_dir),
        timeout_ms=args.timeout_ms,
        post_login_wait_ms=args.post_login_wait_ms,
        force_login=args.force_login,
        headless=not args.headful,
    )

    try:
        result = asyncio.run(process_post(decode_cli_text(args.id), decode_cli_text(args.url), config))
    except Exception as exc:
        log(f"Screenshot extractor failed: {exc}")
        log(traceback.format_exc())
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
