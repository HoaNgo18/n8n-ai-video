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
from urllib.parse import urlsplit, urlunsplit

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
DEFAULT_MIN_CONTENT_FIT_SCORE = 2

THREADS_LOGIN_URL = "https://www.threads.net/login"
THREADS_HOME_URL = "https://www.threads.net/"

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
    "mn nghi sao", "mo nguoi nghi sao", "theo moi nguoi", "theo mn",
    "co ai thay", "co ai tung", "ban se chon", "neu la ban",
    "goc nhin", "tranh cai", "y kien", "quan diem", "ban luan",
    "thao luan", "drama", "red flag", "toxic",
)
HOT_TOPIC_KEYWORDS = (
    "gia nha", "bat dong san", "chung cu", "nong len", "mat dien",
    "gia vang", "lam phat", "kinh te", "chinh tri", "xa hoi",
    "luong", "that nghiep", "thue", "hoc phi", "benh vien",
    "giao thong", "tai nan", "viral", "dang hot",
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
    candidate_limit: int
    min_content_fit_score: int
    dry_run: bool


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
    return bool(VIET_PATTERN.search(text or ""))


def normalize_search_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "")
    ascii_text = base.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_text).strip()


def classify_preview_text(text: str) -> str | None:
    normalized = normalize_search_text(text)
    if not normalized:
        return "empty preview"
    sales_hits = sum(1 for k in SALES_KEYWORDS if k in normalized)
    self_promo_hits = sum(1 for k in SELF_PROMO_KEYWORDS if k in normalized)
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

    discussion_hits = [k for k in DISCUSSION_KEYWORDS if k in normalized]
    topic_hits = [k for k in HOT_TOPIC_KEYWORDS if k in normalized]
    strong_story_hits = [k for k in STORY_STRONG_KEYWORDS if k in normalized]
    weak_story_hits = [k for k in STORY_WEAK_KEYWORDS if k in normalized]
    low_signal_hits = [k for k in LOW_SIGNAL_KEYWORDS if k in normalized]

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
    if topic_hits:
        score += min(2, len(topic_hits))
        tags.append("topic-hot")
    if has_question:
        score += 1
        tags.append("question")
    if sentence_count >= 3 and word_count >= 25:
        score += 1
        tags.append("multi-sentence")
    if low_signal_hits:
        score -= min(2, len(low_signal_hits))
        tags.append("low-signal")

    return {
        "score": score,
        "tags": tags,
        "reason": ",".join(discussion_hits[:2] + topic_hits[:2] + strong_story_hits[:2] + weak_story_hits[:1] + low_signal_hits[:1]),
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
    match = re.fullmatch(r"(\d+(?:[,.]\d+)?)([KkMm]?)", token)
    if not match:
        return None
    raw_number, suffix = match.groups()
    if suffix:
        value = float(raw_number.replace(",", "."))
        return int(value * (1000 if suffix.lower() == "k" else 1_000_000))
    digits = raw_number.replace(",", "").replace(".", "")
    return int(digits) if digits else None


def estimate_engagement(text: str) -> dict:
    tail = " ".join((text or "").split())[-260:]
    tokens = re.findall(r"\b\d+(?:[,.]\d+)?[KkMm]?\b", tail)
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


async def collect_posts(page: Page, config: Config) -> list[dict]:
    results: list[dict] = []
    diagnostics: list[dict] = []
    seen: set[str] = set()
    stats = RejectStats()

    # ── Go straight to Home feed (skip Explore — unreliable in headless) ──
    log(f"Opening Threads Home feed... (headless={config.headless})")
    await page.goto(THREADS_HOME_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
    await page.wait_for_timeout(5000)
    log(f"Home feed loaded. final_url={page.url}")

    total_candidates = 0
    viet_candidates = 0

    for scroll_index in range(config.scroll_count):
        candidates = await get_candidate_posts(page)
        new_candidates = [c for c in candidates if extract_post_id(
            canonical_post_url(c.get("href", ""))
        ) not in seen]

        log(
            f"Scroll {scroll_index + 1}/{config.scroll_count}: "
            f"{len(candidates)} links found, {len(new_candidates)} new."
        )
        total_candidates += len(new_candidates)

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
                                        "stage": "language", "reason": "non-vietnamese"})
                continue
            viet_candidates += 1

            # ── Gate 2: Sales / self-promo ──
            reject_reason = classify_preview_text(text)
            if reject_reason:
                stats.add(f"preview:{reject_reason}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "preview", "reason": reject_reason,
                                        "text_preview": text[:240]})
                continue

            # ── Gate 3: Content fit ──
            content_fit = assess_content_fit(text)
            if content_fit["score"] < config.min_content_fit_score:
                stats.add(f"content-fit:score={content_fit['score']} tags={content_fit['tags']}")
                if config.dry_run:
                    diagnostics.append({"id": post_id, "url": url, "accepted": False,
                                        "stage": "content-fit",
                                        "content_fit_score": content_fit["score"],
                                        "content_fit_tags": content_fit["tags"],
                                        "reason": content_fit["reason"] or "low score",
                                        "text_preview": text[:240]})
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
                                        "text_preview": text[:240]})
                continue

            # ── Accepted ──
            item = {
                "id": post_id,
                "url": url,
                "text_preview": text[:240],
                "content_fit_score": content_fit["score"],
                "content_fit_tags": content_fit["tags"],
                "engagement_score": engagement["score"],
                "engagement_metrics": engagement["metrics"],
                "engagement_raw": engagement["raw"],
                "engagement_metric_count": engagement["metric_count"],
                "engagement_strongest_metric": engagement["strongest_metric"],
            }
            results.append(item)
            if config.dry_run:
                diagnostics.append({**item, "accepted": True, "stage": "accepted"})

            if len(results) >= config.candidate_limit:
                break

        if len(results) >= config.candidate_limit:
            log("Candidate limit reached — stopping scroll.")
            break

        await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
        await page.wait_for_timeout(2500)

    # ── Summary log ──
    log(f"=== Scan complete: total_candidates={total_candidates}, viet={viet_candidates}, accepted={len(results)} ===")
    stats.log_summary()

    if config.dry_run:
        diagnostics.sort(key=lambda d: (0 if d.get("accepted") else 1, -int(d.get("engagement_score", 0))))
        return diagnostics[: max(config.max_posts * 4, 40)]

    if not results:
        await write_debug_artifacts(page, config, "threads_zero_results")

    return sorted(
        results,
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
    parser.add_argument("--mock", action="store_true")
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
        candidate_limit=max(args.candidate_limit, args.max_posts),
        min_content_fit_score=args.min_content_fit_score,
        dry_run=args.dry_run,
    )

    log(f"Config: headless={config.headless} max_posts={config.max_posts} "
        f"scroll={config.scroll_count} min_eng={config.min_engagement_score} "
        f"min_fit={config.min_content_fit_score} dry_run={config.dry_run}")

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