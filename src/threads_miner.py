"""
Phase 1: Threads Miner.

Scrape Vietnamese Threads posts from Home feed and print a JSON array to stdout.
n8n reads stdout, normalizes the rows, and appends new IDs to Google Sheets.

Local test without login/network:
    venv\\Scripts\\python.exe src\\threads_miner.py --mock

Real run:
    copy .env.example .env
    # edit .env with your Threads credentials
    venv\\Scripts\\python.exe src\\threads_miner.py
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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlsplit, urlunsplit

from dotenv import load_dotenv
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


PROJECT_ROOT = (
    Path(__file__).resolve().parent.parent
    if Path(__file__).resolve().parent.name == "src"
    else Path(__file__).resolve().parent
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

DEFAULT_MAX_POSTS = 15
DEFAULT_SCROLL_COUNT = 8
DEFAULT_MIN_ENGAGEMENT_SCORE = 200
DEFAULT_MIN_STRONGEST_METRIC_SCORE = 1000
DEFAULT_MIN_CONTENT_FIT_SCORE = 2

THREADS_LOGIN_URL = "https://www.threads.net/login"
THREADS_HOME_URL = "https://www.threads.net/"
THREADS_SEARCH_URL = "https://www.threads.net/search?q={query}&serp_type=default"

# ── Mining mode constants ──
MINING_MODE_AUTO = "auto"    # L1 trend-keywords + L2 static sweeps + L3 home feed fallback
MINING_MODE_SEARCH = "search"  # single keyword (legacy behaviour)
MINING_MODE_HOME = "home"    # home feed only (legacy behaviour)

DEFAULT_MINING_MODE = MINING_MODE_AUTO
DEFAULT_TREND_KEYWORDS_LIMIT = 4   # how many RSS trend-keywords to search on Threads
DEFAULT_SWEEP_SCROLL_COUNT = 4     # shallower scroll per query in auto mode
DEFAULT_HOME_SCROLL_COUNT = 6      # scroll count when falling back to home feed

# Static sweep queries: broad mass-appeal Vietnamese topics that are almost
# always active on Threads. Ordered from highest to lowest expected yield.
# Override entirely via env THREADS_SWEEP_QUERIES (newline or comma-separated).
DEFAULT_SWEEP_QUERIES: list[str] = [
    "chuyện đời sống",       # everyday life stories
    "drama",                 # universal engagement hook
    "tâm sự",               # confession / personal story
    "giá nhà chung cư",     # housing prices — perennial hot topic
    "lương thưởng công việc", # salary & work
    "giao thông Hà Nội Sài Gòn", # traffic — daily frustration
    "câu chuyện gia đình",   # family drama
    "storytime",             # explicitly story-format posts
]

SALES_KEYWORDS = (
    "bao gia", "order", "dat hang", "mua ngay", "sale", "giam gia",
    "uu dai", "khuyen mai", "freeship", "cod", "san pham", "dich vu",
    "tuyen ctv", "affiliate", "booking", "bang gia", "lien he", "inbox", "ib",
)
SELF_PROMO_KEYWORDS = (
    "follow minh", "flop qua", "ung ho minh", "kenh minh", "profile minh",
    "bio minh", "link bio", "xem them o bio", "subcribe", "subscribe",
    "dang ky kenh", "kenh youtube", "tiktok cua minh", "facebook cua minh",
)
DISCUSSION_KEYWORDS = (
    "mn nghi sao", "mng nghi sao", "moi nguoi nghi sao", "nghi sao",
    "theo moi nguoi", "theo mn", "theo mng", "moi nguoi oi",
    "co ai thay", "co ai tung", "ban se chon", "neu la ban",
    "goc nhin", "tranh cai", "y kien", "quan diem", "ban luan",
    "thao luan", "muon nghe", "xin y kien", "drama", "red flag", "toxic",
)
HOT_TOPIC_KEYWORDS = (
    "gia nha", "bat dong san", "chung cu", "thue nha", "mua nha",
    "nong len", "mat dien", "gia vang", "lam phat", "kinh te",
    "chinh tri", "xa hoi", "that nghiep", "thue",
    "hoc phi", "benh vien", "giao thong", "tai nan", "viral",
    "dang hot", "gia xang", "xang", "quy hoach", "do thi",
    "ha noi", "sai gon", "tp hcm", "dao duong", "sua duong",
    "pha duong", "ket xe", "tac duong", "metro", "vanh dai",
    "truyen ma", "tam linh", "ma quy", "nha ma", "kinh di",
)
MASS_APPEAL_KEYWORDS = (
    "tien", "xang", "dien", "nuoc", "duong", "xe",
    "viec lam", "that nghiep", "hoc", "benh",
    "ly hon", "con cai", "phu huynh", "hang xom", "cong ty",
    "sep", "dong nghiep", "phap luat", "quy dinh", "chinh sach",
    "ha noi", "sai gon", "tp hcm", "chung cu", "dat dai",
)
NICHE_COMMUNITY_KEYWORDS = (
    "idol", "fandom", "fan", "comeback", "concert", "bias", "ship",
    "fl", "follower", "mua fl", "stream", "vote", "album", "lightstick",
)
SOFT_PERSONAL_ADVICE_KEYWORDS = (
    "podcast", "networking", "personal branding", "marketing agency",
    "hanh trinh", "truyen cam hung", "kinh nghiem ca nhan",
)
STORY_STRONG_KEYWORDS = (
    "cau chuyen", "chuyen nay", "xong roi", "ket qua la",
    "plot twist", "bi soc", "gap chuyen", "ke chuyen",
    "tam su", "confession", "storytime",
)
STORY_WEAK_KEYWORDS = (
    "hom nay", "hom qua", "toi vua", "minh vua", "ngay xua", "luc do",
)
LOW_SIGNAL_KEYWORDS = (
    "chao buoi sang", "good morning", "chuc moi nguoi",
    "haha", "hihi", "xinh qua", "dep qua", "xin via", "test",
)
IMAGE_ONLY_PATTERNS = re.compile(
    r"\b(check in|outfit|ootd|selfie|chup anh|photo|pic|album|len do|len hinh)\b",
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
VIET_SPECIFIC_PATTERN = re.compile(r"[ăđĩũơưạ-ỹ]", re.IGNORECASE)
VIET_STOPWORDS = {
    "ban", "biet", "cua", "cung", "duoc", "khong", "minh", "moi",
    "mot", "nguoi", "nghi", "nhieu", "nhung", "qua", "roi", "sao",
    "theo", "toi", "trong", "voi",
}
SEARCH_MATCH_STOPWORDS = {
    "la", "va", "voi", "cua", "cho", "trong", "tren", "duoi", "tai",
    "mot", "nhung", "nhieu", "cac", "nhung", "nay", "kia", "do", "roi",
    "thi", "ma", "co", "khong", "bi", "duoc", "ve", "ra", "vao",
}


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    max_posts: int
    scroll_count: int
    headless: bool
    timeout_ms: int
    post_login_wait_ms: int
    storage_state: Path
    debug_dir: Path
    force_login: bool
    min_engagement_score: int
    min_strongest_metric_score: int
    candidate_limit: int
    min_content_fit_score: int
    dry_run: bool
    search_keyword: str = ""
    # ── Auto mining fields ──
    mining_mode: str = MINING_MODE_AUTO
    trend_keywords_limit: int = DEFAULT_TREND_KEYWORDS_LIMIT
    sweep_queries: tuple = ()       # empty = use DEFAULT_SWEEP_QUERIES
    sweep_scroll_count: int = DEFAULT_SWEEP_SCROLL_COUNT
    trend_brief: str = ""           # passed to output for Gemini classifier


@dataclass
class RejectStats:
    counts: Counter = field(default_factory=Counter)

    def add(self, stage: str) -> None:
        self.counts[stage] += 1

    def log_summary(self) -> None:
        if not self.counts:
            return
        log("=== Reject breakdown ===")
        for stage, count in self.counts.most_common():
            log(f"  {stage}: {count}")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def chromium_executable_path() -> str | None:
    candidate = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", "").strip()
    known_paths = [candidate, "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    for path in known_paths:
        if path and Path(path).exists():
            return path
    return None


async def write_debug_artifacts(page: Page, config: Config, label: str) -> None:
    config.debug_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "debug"
    try:
        await page.screenshot(path=str(config.debug_dir / f"{safe_label}.png"), full_page=True)
        log(f"Debug screenshot: {config.debug_dir / f'{safe_label}.png'}")
    except Exception as exc:
        log(f"Could not write debug screenshot: {exc}")
    try:
        (config.debug_dir / f"{safe_label}.html").write_text(await page.content(), encoding="utf-8")
        log(f"Debug HTML: {config.debug_dir / f'{safe_label}.html'}")
    except Exception as exc:
        log(f"Could not write debug HTML: {exc}")


def is_vietnamese(text: str) -> bool:
    if VIET_SPECIFIC_PATTERN.search(text or ""):
        return True
    normalized = normalize_search_text(text)
    tokens = set(re.findall(r"[a-z]{2,}", normalized))
    return sum(1 for word in VIET_STOPWORDS if word in tokens) >= 4


def normalize_search_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    ascii_text = base.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_text).strip()


def env_keyword_terms(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    terms = []
    for item in re.split(r"[,;\n]+", raw):
        normalized = normalize_search_text(item)
        if normalized:
            terms.append(normalized)
    return terms


def contains_normalized_keyword(normalized_text: str, keyword: str) -> bool:
    keyword = normalize_search_text(keyword)
    if not keyword:
        return False
    if " " in keyword:
        if keyword in normalized_text:
            return True
        terms = [
            token for token in re.findall(r"[a-z0-9]+", keyword)
            if len(token) >= 2 and token not in SEARCH_MATCH_STOPWORDS
        ]
        if not terms:
            return False
        matched = sum(
            1 for token in terms
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized_text)
        )
        required = len(terms) if len(terms) <= 2 else max(2, (len(terms) * 2 + 2) // 3)
        return matched >= required
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", normalized_text) is not None


def matched_keywords(normalized_text: str, keywords) -> list[str]:
    return [keyword for keyword in keywords if contains_normalized_keyword(normalized_text, keyword)]


def classify_preview_text(text: str) -> str | None:
    normalized = normalize_search_text(text)
    if not normalized:
        return "empty preview"
    sales_hits = len(matched_keywords(normalized, SALES_KEYWORDS))
    self_promo_hits = len(matched_keywords(normalized, SELF_PROMO_KEYWORDS))
    has_price = bool(re.search(r"\b\d{2,3}(?:[.,]\d{3})+\b|\b\d+\s*(k|tr|cu|usd)\b", normalized))
    has_contact = bool(re.search(r"\b\d{9,11}\b|zalo|sdt|so dien thoai", normalized))
    if sales_hits >= 2 or (sales_hits >= 1 and (has_price or has_contact)):
        return "sales/promotional"
    if self_promo_hits >= 2:
        return "self-promotional"
    return None


def assess_content_fit(text: str) -> dict:
    normalized = normalize_search_text(text)
    if not normalized:
        return {"score": 0, "tags": [], "reason": "empty content"}

    discussion_hits = matched_keywords(normalized, DISCUSSION_KEYWORDS)
    priority_hits = matched_keywords(normalized, env_keyword_terms("THREADS_PRIORITY_TOPICS"))
    topic_hits = matched_keywords(normalized, HOT_TOPIC_KEYWORDS)
    mass_hits = matched_keywords(normalized, MASS_APPEAL_KEYWORDS)
    strong_story_hits = matched_keywords(normalized, STORY_STRONG_KEYWORDS)
    weak_story_hits = matched_keywords(normalized, STORY_WEAK_KEYWORDS)
    low_signal_hits = matched_keywords(normalized, LOW_SIGNAL_KEYWORDS)
    niche_hits = matched_keywords(normalized, NICHE_COMMUNITY_KEYWORDS)
    soft_personal_hits = matched_keywords(normalized, SOFT_PERSONAL_ADVICE_KEYWORDS)

    sentence_count = len([p for p in re.split(r"[.!?\n]+", text) if p.strip()])
    word_count = len(normalized.split())
    has_question = "?" in text or any(
        p in normalized for p in ("ban nghi sao", "mn nghi sao", "co ai", "ban co", "lam sao", "vi sao")
    )

    if IMAGE_ONLY_PATTERNS.search(normalized) and word_count < 60 and not strong_story_hits:
        return {"score": -1, "tags": ["image-post"], "reason": "photo-focused post"}

    score = 0
    tags: list[str] = []

    if discussion_hits:
        score += min(3, len(discussion_hits))
        tags.append("discussion")
    if strong_story_hits:
        score += min(3, len(strong_story_hits))
        tags.append("story")
    elif len(weak_story_hits) >= 2:
        score += 1
        tags.append("story-weak")
    if priority_hits:
        score += min(4, 2 + len(priority_hits))
        tags.append("priority-topic")
    if topic_hits:
        score += min(3, len(topic_hits))
        tags.append("topic-hot")
    if mass_hits:
        score += min(2, len(mass_hits))
        tags.append("mass-appeal")
    if has_question:
        score += 1
        tags.append("question")
    if sentence_count >= 3 and word_count >= 25:
        score += 1
        tags.append("multi-sentence")
    if low_signal_hits:
        score -= min(2, len(low_signal_hits))
        tags.append("low-signal")
    if niche_hits and not (priority_hits or topic_hits or mass_hits):
        score -= min(3, len(niche_hits))
        tags.append("niche-community")
    if soft_personal_hits and not (discussion_hits or priority_hits or topic_hits):
        score -= min(2, len(soft_personal_hits))
        tags.append("soft-personal")

    return {
        "score": score,
        "tags": tags,
        "reason": ",".join(
            priority_hits[:3]
            + topic_hits[:2]
            + mass_hits[:2]
            + discussion_hits[:2]
            + strong_story_hits[:2]
            + weak_story_hits[:1]
            + niche_hits[:1]
            + soft_personal_hits[:1]
            + low_signal_hits[:1]
        ),
    }


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def extract_post_id(url: str) -> str:
    match = re.search(r"/post/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return canonical_url(url).rstrip("/").split("/")[-1]


def canonical_post_url(url: str) -> str:
    url = canonical_url(url)
    match = re.search(r"^(https?://[^/]+/[^/]+/post/[A-Za-z0-9_-]+)", url)
    return match.group(1) if match else url


def parse_number_token(token: str) -> int | None:
    token = token.strip()
    match = re.fullmatch(r"(\d+(?:[,.]\d+)?)([KkM]?)", token)
    if not match:
        return None
    raw_number, suffix = match.groups()
    if suffix:
        value = float(raw_number.replace(",", "."))
        return int(value * (1000 if suffix.lower() == "k" else 1_000_000))
    digits = raw_number.replace(",", "").replace(".", "")
    return int(digits) if digits else None


def estimate_engagement(text: str) -> dict:
    compact_text = " ".join((text or "").split())
    metric_region = compact_text.rsplit("Translate", 1)[-1] if "Translate" in compact_text else compact_text
    tail = metric_region[-260:]
    tokens = re.findall(r"\b\d+(?:[,.]\d+)?[KkM]?\b", tail)
    values, raw_tokens = [], []
    for token in tokens:
        value = parse_number_token(token)
        if value is None:
            continue
        raw_plain = token.rstrip("KkMm").replace(",", "").replace(".", "")
        if raw_plain.isdigit() and 1990 <= int(raw_plain) <= 2099:
            continue
        if value >= 100_000 and not token.lower().endswith(("k", "m")):
            continue
        values.append(value)
        raw_tokens.append(token)
    metrics = values[-4:]
    return {
        "score": sum(metrics),
        "metrics": metrics,
        "raw": raw_tokens[-6:],
        "metric_count": len(metrics),
        "strongest_metric": max(metrics) if metrics else 0,
    }



# ──────────────────────────────────────────────────────────────────────────────
# Auto mining helpers
# ──────────────────────────────────────────────────────────────────────────────

def resolve_sweep_queries(config: "Config") -> list[str]:
    """Return the ordered list of search queries for L2 static sweep.

    Priority: env THREADS_SWEEP_QUERIES > config.sweep_queries > DEFAULT_SWEEP_QUERIES.
    """
    env_raw = os.getenv("THREADS_SWEEP_QUERIES", "").strip()
    if env_raw:
        queries = [q.strip() for q in re.split(r"[\n,]+", env_raw) if q.strip()]
        if queries:
            return queries

    if config.sweep_queries:
        return list(config.sweep_queries)

    return list(DEFAULT_SWEEP_QUERIES)


def fetch_trend_keywords(limit: int) -> tuple[list[str], str]:
    """Call trend_signal.build_trend_signal() and return (keywords, brief).

    Returns ([], "") on any error so the caller can degrade gracefully.
    """
    try:
        # Import here to avoid hard dependency if trend_signal is missing.
        import sys as _sys
        import importlib

        _trend_mod_name = "trend_signal"
        if _trend_mod_name not in _sys.modules:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                _trend_mod_name,
                Path(__file__).resolve().parent / f"{_trend_mod_name}.py",
            )
            if _spec and _spec.loader:
                _mod = importlib.util.module_from_spec(_spec)
                _sys.modules[_trend_mod_name] = _mod
                _spec.loader.exec_module(_mod)
        trend_mod = _sys.modules[_trend_mod_name]
        effective_limit = max(limit, 6) if limit > 0 else 6
        signal = trend_mod.build_trend_signal(keyword="", limit=effective_limit)
        keywords: list[str] = signal.get("topics") or []
        brief: str = signal.get("brief") or ""
        log(f"Trend signal fetched: {len(keywords)} topics, brief={brief[:80]!r}")
        if limit == 0:
            return [], brief
        return keywords[:limit], brief
    except Exception as exc:
        log(f"Trend signal fetch failed (non-fatal): {exc}")
        return [], ""


def build_query_plan(config: "Config", trend_keywords: list[str] | None = None) -> list[dict]:
    """Build an ordered list of query jobs for multi-source auto mining.

    Each job is:
        {
            "query": str,       # search term (empty string = home feed)
            "source": str,      # label for logging / output tagging
            "scroll_count": int,
            "posts_quota": int, # target accepted posts from this job
        }
    """
    if config.mining_mode == MINING_MODE_HOME:
        return [{
            "query": "",
            "source": "home",
            "scroll_count": config.scroll_count,
            "posts_quota": config.max_posts,
        }]

    if config.mining_mode == MINING_MODE_SEARCH:
        return [{
            "query": config.search_keyword,
            "source": f"search:{config.search_keyword}",
            "scroll_count": config.scroll_count,
            "posts_quota": config.max_posts,
        }]

    # ── MINING_MODE_AUTO ──
    plan: list[dict] = []

    # L1: trend-driven keywords from RSS
    if config.trend_keywords_limit > 0:
        trend_keywords = trend_keywords or []
        posts_per_trend = max(3, config.max_posts // max(1, len(trend_keywords) + 4))
        for kw in trend_keywords:
            plan.append({
                "query": kw,
                "source": f"trend:{kw}",
                "scroll_count": config.sweep_scroll_count,
                "posts_quota": posts_per_trend,
            })

    # L2: static sweep queries
    sweep_qs = resolve_sweep_queries(config)
    posts_per_sweep = max(3, config.max_posts // max(1, len(sweep_qs) + len(plan)))
    for q in sweep_qs:
        plan.append({
            "query": q,
            "source": f"sweep:{q}",
            "scroll_count": config.sweep_scroll_count,
            "posts_quota": posts_per_sweep,
        })

    # L3: home feed fallback (always appended — used only if still under quota)
    plan.append({
        "query": "",
        "source": "home",
        "scroll_count": DEFAULT_HOME_SCROLL_COUNT,
        "posts_quota": config.max_posts,   # fill up to max_posts if needed
    })

    return plan


async def get_candidate_posts(page: Page) -> list[dict[str, str]]:
    return await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]'))
          .map((a) => {
            const href = a.href || a.getAttribute('href') || '';
            const container =
              a.closest('article') ||
              a.closest('[role="article"]') ||
              a.closest('[data-pressable-container="true"]') ||
              a.parentElement;
            const text = container ? container.innerText : a.innerText;
            return { href, text: text || '' };
          })
          .filter((item) => item.href && item.href.includes('/post/'))
        """
    )


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
    await page.wait_for_timeout(3000)
    log(f"Login page loaded. url={page.url}")

    password_count = await page.locator('input[type="password"]').count()
    username_count = await page.locator(
        'input[autocomplete="username"], input[name="username"], input[type="text"]'
    ).count()
    if "login" not in page.url.lower() and password_count == 0 and username_count == 0:
        log("Already authenticated — skipping login form.")
        return

    username_input = await first_visible(
        page,
        ['input[autocomplete="username"]', 'input[name="username"]', 'input[type="text"]'],
        config.timeout_ms,
    )
    await username_input.fill(config.username)
    log("Username filled.")

    password_input = await first_visible(
        page,
        ['input[type="password"]', 'input[autocomplete="current-password"]'],
        config.timeout_ms,
    )
    await password_input.fill(config.password)
    log("Password filled.")
    await password_input.press("Enter")
    await page.wait_for_timeout(config.post_login_wait_ms)
    log(f"Login submitted. url={page.url}")


async def has_valid_session(page: Page, config: Config) -> bool:
    try:
        await page.goto(THREADS_HOME_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
        await page.wait_for_timeout(3000)
        if "login" in page.url.lower():
            return False
        return await page.locator('input[type="password"]').count() == 0
    except Exception:
        return False


async def _collect_from_source(
    page: Page,
    config: Config,
    query: str,
    source_label: str,
    scroll_count: int,
    posts_quota: int,
    seen: set,
    stats: "RejectStats",
    trend_brief: str = "",
) -> tuple[list[dict], list[dict]]:
    """Scrape one URL (search query or home feed) and return (accepted, diagnostics).

    `seen` is shared across calls so duplicates are de-duplicated globally.
    `posts_quota` is a soft cap: stop accepting once reached (but finish current scroll).
    """
    results: list[dict] = []
    diagnostics: list[dict] = []

    if query:
        start_url = THREADS_SEARCH_URL.format(query=quote_plus(query))
    else:
        start_url = THREADS_HOME_URL

    log(f"  → {source_label} | url={start_url[:80]}")
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=config.timeout_ms)
    except Exception as exc:
        log(f"  ✗ Navigation failed for {source_label}: {exc}")
        return [], []
    await page.wait_for_timeout(4000)

    total_candidates = 0
    viet_candidates = 0

    for scroll_index in range(scroll_count):
        candidates = await get_candidate_posts(page)
        new_candidates = [
            c for c in candidates
            if extract_post_id(canonical_post_url(c.get("href", ""))) not in seen
        ]
        total_candidates += len(new_candidates)

        log(
            f"    scroll {scroll_index + 1}/{scroll_count}: "
            f"{len(candidates)} links, {len(new_candidates)} new, accepted so far={len(results)}"
        )

        for candidate in candidates:
            href = candidate.get("href", "")
            if not href or "/post/" not in href:
                continue

            url = href if href.startswith("http") else f"https://www.threads.net{href}"
            url = canonical_post_url(url)
            post_id = extract_post_id(url)
            if post_id in seen:
                continue
            seen.add(post_id)

            text = " ".join((candidate.get("text") or "").split())

            # ── Gate 1: Language ──
            if not is_vietnamese(text):
                stats.add("language:non-viet")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "language", "reason": "non-vietnamese",
                                        "source": source_label})
                continue
            viet_candidates += 1

            # ── Gate 2: Sales / self-promo ──
            reject_reason = classify_preview_text(text)
            if reject_reason:
                stats.add(f"preview:{reject_reason}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "preview", "reason": reject_reason,
                                        "text_preview": text[:240], "source": source_label})
                continue

            # ── Gate 3: Content fit ──
            normalized_text = normalize_search_text(text)
            query_match = bool(query and contains_normalized_keyword(normalized_text, query))
            if config.mining_mode == MINING_MODE_SEARCH and query and not query_match:
                stats.add("search-query:no-match")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "query-match", "reason": "manual search keyword not found in preview",
                                        "text_preview": text[:240], "source": source_label})
                continue

            content_fit = assess_content_fit(text)
            if query_match:
                content_fit = {
                    **content_fit,
                    "score": int(content_fit.get("score") or 0) + 2,
                    "tags": [*content_fit.get("tags", []), "query-match"],
                    "reason": ",".join(part for part in [content_fit.get("reason") or "", normalize_search_text(query)] if part),
                }
            if content_fit["score"] < config.min_content_fit_score:
                stats.add(f"content-fit:score={content_fit['score']} tags={content_fit['tags']}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "content-fit",
                                        "content_fit_score": content_fit["score"],
                                        "content_fit_tags": content_fit["tags"],
                                        "reason": content_fit["reason"] or "low score",
                                        "text_preview": text[:240], "source": source_label})
                continue

            # ── Gate 4: Engagement ──
            engagement = estimate_engagement(text)
            if engagement["score"] < config.min_engagement_score:
                stats.add(f"engagement:score={engagement['score']}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "engagement",
                                        "engagement_score": engagement["score"],
                                        "engagement_metrics": engagement["metrics"],
                                        "content_fit_score": content_fit["score"],
                                        "content_fit_tags": content_fit["tags"],
                                        "text_preview": text[:240], "source": source_label})
                continue
            if engagement["strongest_metric"] < config.min_strongest_metric_score:
                stats.add(f"engagement:strongest={engagement['strongest_metric']}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "engagement",
                                        "engagement_score": engagement["score"],
                                        "engagement_metrics": engagement["metrics"],
                                        "engagement_strongest_metric": engagement["strongest_metric"],
                                        "content_fit_score": content_fit["score"],
                                        "content_fit_tags": content_fit["tags"],
                                        "text_preview": text[:240], "source": source_label})
                continue

            # ── Accepted ──
            item = {
                "id": post_id,
                "url": url,
                "source": source_label,
                "search_keyword": query,
                "query_match": query_match,
                "text_preview": text[:240],
                "content_fit_score": content_fit["score"],
                "content_fit_tags": content_fit["tags"],
                "engagement_score": engagement["score"],
                "engagement_metrics": engagement["metrics"],
                "engagement_raw": engagement["raw"],
                "engagement_metric_count": engagement["metric_count"],
                "engagement_strongest_metric": engagement["strongest_metric"],
                "trend_brief": trend_brief,
            }
            results.append(item)
            if config.dry_run:
                diagnostics.append({**item, "accepted": True, "stage": "accepted"})

            if len(results) >= posts_quota:
                break

        if len(results) >= posts_quota:
            log(f"    quota {posts_quota} reached for {source_label} — moving to next source.")
            break

        await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
        await page.wait_for_timeout(2000)

    log(f"  ✓ {source_label}: candidates={total_candidates} viet={viet_candidates} accepted={len(results)}")
    return results, diagnostics


async def collect_posts(page: Page, config: Config) -> list[dict]:
    """Multi-source orchestrator. Iterates through the query plan and collects
    posts across L1 (trend), L2 (sweep), and L3 (home feed) sources.

    Backward-compatible: MINING_MODE_HOME / MINING_MODE_SEARCH behave identically
    to the original single-source collect_posts.
    """
    all_results: list[dict] = []
    all_diagnostics: list[dict] = []
    seen: set[str] = set()
    stats = RejectStats()

    # Fetch trend signal once for the entire auto run.
    trend_keywords: list[str] = []
    trend_brief = config.trend_brief
    if not trend_brief and config.mining_mode == MINING_MODE_AUTO and config.trend_keywords_limit > 0:
        trend_keywords, trend_brief = fetch_trend_keywords(config.trend_keywords_limit)

    query_plan = build_query_plan(config, trend_keywords=trend_keywords)
    log(f"=== Auto mining plan: mode={config.mining_mode} sources={len(query_plan)} max_posts={config.max_posts} ===")
    for job in query_plan:
        log(f"  job: source={job['source']!r} scroll={job['scroll_count']} quota={job['posts_quota']}")

    for job in query_plan:
        remaining = config.max_posts - len(all_results)
        if remaining <= 0:
            log("=== max_posts quota reached — skipping remaining sources ===")
            break

        effective_quota = min(job["posts_quota"], remaining + max(0, remaining // 2))
        accepted, diags = await _collect_from_source(
            page=page,
            config=config,
            query=job["query"],
            source_label=job["source"],
            scroll_count=job["scroll_count"],
            posts_quota=effective_quota,
            seen=seen,
            stats=stats,
            trend_brief=trend_brief,
        )
        all_results.extend(accepted)
        all_diagnostics.extend(diags)

    # ── Summary ──
    log(f"=== Mining complete: sources={len(query_plan)} total_accepted={len(all_results)} ===")
    stats.log_summary()

    if config.dry_run:
        all_diagnostics.sort(key=lambda d: (0 if d.get("accepted") else 1, -int(d.get("engagement_score", 0))))
        return all_diagnostics[: max(config.max_posts * 4, 40)]

    if not all_results:
        await write_debug_artifacts(page, config, "threads_zero_results")

    return sorted(
        all_results,
        key=lambda item: item["content_fit_score"] * 1000 + item["engagement_score"],
        reverse=True,
    )[: config.max_posts]



async def scrape(config: Config) -> list[dict]:
    async with async_playwright() as playwright:
        launch_kwargs: dict = {"headless": config.headless}
        exe = chromium_executable_path()
        if exe:
            launch_kwargs["executable_path"] = exe
        browser = await playwright.chromium.launch(**launch_kwargs)
        context_options: dict = {
            "viewport": {"width": 1365, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
            ),
        }
        if config.storage_state.exists() and not config.force_login:
            context_options["storage_state"] = str(config.storage_state)
            log(f"Using saved session: {config.storage_state}")
        elif config.force_login:
            log("THREADS_FORCE_LOGIN is enabled — ignoring saved session.")

        context = await browser.new_context(**context_options)
        page = await context.new_page()
        try:
            if config.force_login or not await has_valid_session(page, config):
                await login(page, config)
                config.storage_state.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(config.storage_state))
                log(f"Saved session: {config.storage_state}")
            return await collect_posts(page, config)
        except Exception:
            await write_debug_artifacts(page, config, "threads_miner_failure")
            raise
        finally:
            await context.close()
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Vietnamese Threads posts for n8n.")
    parser.add_argument("--username", default=os.getenv("THREADS_USERNAME"))
    parser.add_argument("--password", default=os.getenv("THREADS_PASSWORD"))
    parser.add_argument("--max-posts", type=int, default=int(os.getenv("MAX_POSTS", DEFAULT_MAX_POSTS)))
    parser.add_argument("--min-engagement-score", type=int,
                        default=int(os.getenv("MIN_ENGAGEMENT_SCORE", DEFAULT_MIN_ENGAGEMENT_SCORE)))
    parser.add_argument("--min-strongest-metric-score", type=int,
                        default=int(os.getenv("MIN_STRONGEST_METRIC_SCORE", DEFAULT_MIN_STRONGEST_METRIC_SCORE)))
    parser.add_argument("--candidate-limit", type=int,
                        default=int(os.getenv("CANDIDATE_LIMIT", str(DEFAULT_MAX_POSTS * 5))))
    parser.add_argument("--min-content-fit-score", type=int,
                        default=int(os.getenv("MIN_CONTENT_FIT_SCORE", DEFAULT_MIN_CONTENT_FIT_SCORE)))
    parser.add_argument("--scroll-count", type=int,
                        default=int(os.getenv("SCROLL_COUNT", DEFAULT_SCROLL_COUNT)))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("THREADS_TIMEOUT_MS", "30000")))
    parser.add_argument("--post-login-wait-ms", type=int,
                        default=int(os.getenv("THREADS_POST_LOGIN_WAIT_MS", "5000")))
    parser.add_argument("--storage-state",
                        default=os.getenv("THREADS_STORAGE_STATE", "runtime/storage/threads-state.json"))
    parser.add_argument("--debug-dir", default=os.getenv("THREADS_DEBUG_DIR", "runtime/debug"))
    parser.add_argument("--force-login", action="store_true",
                        default=os.getenv("THREADS_FORCE_LOGIN", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Return full candidate diagnostics instead of accepted posts only.")
    parser.add_argument("--search-keyword", default=os.getenv("THREADS_SEARCH_KEYWORD", "").strip(),
                        help="Single-keyword search (sets mining-mode=search automatically).")
    parser.add_argument("--mock", action="store_true")
    # ── Auto mining flags ──
    parser.add_argument(
        "--mining-mode",
        choices=[MINING_MODE_AUTO, MINING_MODE_SEARCH, MINING_MODE_HOME],
        default=os.getenv("THREADS_MINING_MODE", DEFAULT_MINING_MODE).strip().lower() or DEFAULT_MINING_MODE,
        help=(
            "auto  = L1 trend-keywords + L2 static sweeps + L3 home feed (default); "
            "search = single --search-keyword (legacy); "
            "home  = home feed only (legacy)."
        ),
    )
    parser.add_argument(
        "--trend-keywords-limit",
        type=int,
        default=int(os.getenv("THREADS_TREND_KEYWORDS_LIMIT", str(DEFAULT_TREND_KEYWORDS_LIMIT))),
        help="How many RSS trend-keywords to auto-search on Threads (auto mode only).",
    )
    parser.add_argument(
        "--sweep-scroll-count",
        type=int,
        default=int(os.getenv("THREADS_SWEEP_SCROLL_COUNT", str(DEFAULT_SWEEP_SCROLL_COUNT))),
        help="Scroll count per query in L1/L2 sweep (shallower than home feed default).",
    )
    return parser.parse_args()



def mock_posts() -> list[dict]:
    return [{"id": "mock_001", "url": "https://www.threads.net/@demo/post/mock_001",
             "text_preview": "Bai test tieng Viet co dau."}]


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    args = parse_args()

    if args.mock:
        print(json.dumps(mock_posts(), ensure_ascii=True))
        return 0

    if not args.username or not args.password:
        log("Missing THREADS_USERNAME or THREADS_PASSWORD.")
        return 2

    storage_state = (
        Path(args.storage_state) if Path(args.storage_state).is_absolute()
        else PROJECT_ROOT / args.storage_state
    )
    debug_dir = (
        Path(args.debug_dir) if Path(args.debug_dir).is_absolute()
        else PROJECT_ROOT / args.debug_dir
    )

    # If --search-keyword is given, force search mode (backward-compat)
    mining_mode = args.mining_mode
    if args.search_keyword.strip() and mining_mode == MINING_MODE_AUTO:
        mining_mode = MINING_MODE_SEARCH

    config = Config(
        username=args.username,
        password=args.password,
        max_posts=args.max_posts,
        scroll_count=args.scroll_count,
        headless=not args.headful,
        timeout_ms=args.timeout_ms,
        post_login_wait_ms=args.post_login_wait_ms,
        storage_state=storage_state,
        debug_dir=debug_dir,
        force_login=args.force_login,
        min_engagement_score=args.min_engagement_score,
        min_strongest_metric_score=args.min_strongest_metric_score,
        candidate_limit=max(args.candidate_limit, args.max_posts),
        min_content_fit_score=args.min_content_fit_score,
        dry_run=args.dry_run,
        search_keyword=args.search_keyword.strip(),
        mining_mode=mining_mode,
        trend_keywords_limit=args.trend_keywords_limit,
        sweep_scroll_count=args.sweep_scroll_count,
    )

    log(
        f"Config: mode={config.mining_mode} headless={config.headless} "
        f"max_posts={config.max_posts} scroll={config.scroll_count} "
        f"sweep_scroll={config.sweep_scroll_count} "
        f"trend_kw_limit={config.trend_keywords_limit} "
        f"min_eng={config.min_engagement_score} "
        f"min_strongest={config.min_strongest_metric_score} "
        f"min_fit={config.min_content_fit_score} dry_run={config.dry_run}"
        + (f" search_keyword={config.search_keyword!r}" if config.search_keyword else "")
    )

    try:
        posts = asyncio.run(scrape(config))
    except Exception as exc:
        log(f"Threads miner failed: {exc}")
        log(traceback.format_exc())
        return 1

    print(json.dumps(posts, ensure_ascii=True))
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
