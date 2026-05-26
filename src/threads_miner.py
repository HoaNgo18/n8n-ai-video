"""
Phase 1: Threads Miner.

Scrape Vietnamese Threads posts from Explore and print a JSON array to stdout.
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
from dataclasses import dataclass
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

DEFAULT_MAX_POSTS = 15
DEFAULT_SCROLL_COUNT = 6
DEFAULT_MIN_ENGAGEMENT_SCORE = 1000
THREADS_LOGIN_URL = "https://www.threads.net/login"
THREADS_EXPLORE_URL = "https://www.threads.net/explore"
THREADS_HOME_URL = "https://www.threads.net/"
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
SELF_PROMO_KEYWORDS = (
    "follow minh",
    "flop qua",
    "ung ho minh",
    "kenh minh",
    "profile minh",
    "bio minh",
    "link bio",
    "xem them o bio",
    "subcribe",
    "subscribe",
    "dang ky kenh",
    "kenh youtube",
    "tiktok cua minh",
    "facebook cua minh",
    "toi la",
    "minh la",
)

# Vietnamese diacritics, written as unicode escapes so the file stays ASCII-safe.
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


async def write_debug_artifacts(page: Page, config: Config, label: str) -> None:
    config.debug_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "debug"
    screenshot_path = config.debug_dir / f"{safe_label}.png"
    html_path = config.debug_dir / f"{safe_label}.html"

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log(f"Debug screenshot: {screenshot_path}")
    except Exception as exc:
        log(f"Could not write debug screenshot: {exc}")

    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        log(f"Debug HTML: {html_path}")
    except Exception as exc:
        log(f"Could not write debug HTML: {exc}")


def is_vietnamese(text: str) -> bool:
    return bool(VIET_PATTERN.search(text or ""))


def normalize_search_text(text: str) -> str:
    base = unicodedata.normalize("NFKD", text or "")
    ascii_text = base.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"\s+", " ", ascii_text)
    return ascii_text.strip()


def classify_preview_text(text: str) -> str | None:
    normalized = normalize_search_text(text)
    if not normalized:
        return "empty preview"

    sales_hits = sum(1 for keyword in SALES_KEYWORDS if keyword in normalized)
    self_promo_hits = sum(1 for keyword in SELF_PROMO_KEYWORDS if keyword in normalized)
    has_price = bool(re.search(r"\b\d{2,3}(?:[.,]\d{3})+\b|\b\d+\s*(k|tr|cu|usd)\b", normalized))
    has_contact = bool(re.search(r"\b\d{9,11}\b|zalo|sdt|so dien thoai", normalized))

    if sales_hits >= 2 or (sales_hits >= 1 and (has_price or has_contact)):
        return "sales/promotional preview"
    if self_promo_hits >= 2:
        return "self-promotional preview"
    return None


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
    if match:
        return match.group(1)
    return url


def parse_number_token(token: str) -> int | None:
    token = token.strip()
    match = re.fullmatch(r"(\d+(?:[,.]\d+)?)([KkMm]?)", token)
    if not match:
        return None

    raw_number, suffix = match.groups()
    if suffix:
        value = float(raw_number.replace(",", "."))
        multiplier = 1000 if suffix.lower() == "k" else 1000000
        return int(value * multiplier)

    digits = raw_number.replace(",", "").replace(".", "")
    if not digits:
        return None
    return int(digits)


def estimate_engagement(text: str) -> dict:
    """Estimate engagement from visible Threads UI text.

    Threads does not expose stable labels in the feed DOM, so this uses the
    numeric cluster near the end of a post card, where like/comment/repost/share
    counts usually appear.
    """
    tail = " ".join((text or "").split())[-260:]
    tokens = re.findall(r"\b\d+(?:[,.]\d+)?[KkMm]?\b", tail)
    values = []
    raw_tokens = []

    for token in tokens:
        value = parse_number_token(token)
        if value is None:
            continue
        # Drop obvious price-sized standalone numbers. Engagement can be high,
        # but product prices in VND tend to pollute Threads commerce posts.
        if value >= 100000 and not token.lower().endswith(("k", "m")):
            continue
        values.append(value)
        raw_tokens.append(token)

    metrics = values[-4:]
    return {
        "score": sum(metrics),
        "metrics": metrics,
        "raw": raw_tokens[-6:],
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


async def log_page_diagnostics(page: Page, label: str) -> None:
    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        hrefs = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).slice(0, 20).map(a => a.href || a.getAttribute('href'))"
        )
    except Exception:
        hrefs = []

    try:
        body_preview = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 500) : ''")
    except Exception:
        body_preview = ""

    log(f"{label}: url={page.url}")
    log(f"{label}: title={title}")
    log(f"{label}: sample hrefs={json.dumps(hrefs, ensure_ascii=False)[:1000]}")
    log(f"{label}: body preview={body_preview!r}")


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
    log(f"Login page loaded. url={page.url}")

    username_input = await first_visible(
        page,
        [
            'input[autocomplete="username"]',
            'input[name="username"]',
            'input[type="text"]',
        ],
        config.timeout_ms,
    )
    await username_input.fill(config.username)
    log("Username filled.")

    password_input = await first_visible(
        page,
        [
            'input[type="password"]',
            'input[autocomplete="current-password"]',
        ],
        config.timeout_ms,
    )
    await password_input.fill(config.password)
    log("Password filled.")
    await password_input.press("Enter")

    await page.wait_for_timeout(config.post_login_wait_ms)
    log(f"Login submitted. url={page.url}")


async def has_valid_session(page: Page, config: Config) -> bool:
    try:
        await page.goto(THREADS_EXPLORE_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
        await page.wait_for_timeout(3000)
        if "login" in page.url.lower():
            return False
        password_count = await page.locator('input[type="password"]').count()
        return password_count == 0
    except Exception:
        return False


async def collect_posts(page: Page, config: Config) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    async def scan_current_page(source_label: str) -> None:
        for scroll_index in range(config.scroll_count):
            candidates = await get_candidate_posts(page)
            log(
                f"{source_label} scan {scroll_index + 1}/{config.scroll_count}: "
                f"found {len(candidates)} post links."
            )

            if scroll_index == 0 and not candidates:
                await log_page_diagnostics(page, source_label)

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

                if not is_vietnamese(text):
                    continue

                reject_reason = classify_preview_text(text)
                if reject_reason:
                    log(f"Skip low-quality preview: {reject_reason} url={url}")
                    continue

                engagement = estimate_engagement(text)
                if engagement["score"] < config.min_engagement_score:
                    log(
                        f"Skip low engagement: score={engagement['score']} "
                        f"url={url}"
                    )
                    continue

                results.append(
                    {
                        "id": extract_post_id(url),
                        "url": url,
                        "text_preview": text[:240],
                        "engagement_score": engagement["score"],
                        "engagement_metrics": engagement["metrics"],
                        "engagement_raw": engagement["raw"],
                    }
                )

                if len(results) >= config.candidate_limit:
                    return

            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await page.wait_for_timeout(2500)

    log("Opening Threads Explore...")
    await page.goto(THREADS_EXPLORE_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
    await page.wait_for_timeout(5000)
    await scan_current_page("Explore")

    if results:
        return sorted(results, key=lambda item: item.get("engagement_score", 0), reverse=True)[: config.max_posts]

    log("Explore returned no Vietnamese post links. Trying Threads home feed...")
    await page.goto(THREADS_HOME_URL, wait_until="domcontentloaded", timeout=config.timeout_ms)
    await page.wait_for_timeout(5000)
    await scan_current_page("Home")

    if not results:
        await write_debug_artifacts(page, config, "threads_zero_results")

    return sorted(results, key=lambda item: item.get("engagement_score", 0), reverse=True)[: config.max_posts]


async def scrape(config: Config) -> list[dict[str, str]]:
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
        elif config.force_login:
            log("THREADS_FORCE_LOGIN is enabled. Ignoring saved session.")

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
    parser.add_argument(
        "--min-engagement-score",
        type=int,
        default=int(os.getenv("MIN_ENGAGEMENT_SCORE", DEFAULT_MIN_ENGAGEMENT_SCORE)),
        help="Minimum estimated sum of visible engagement counts.",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=int(os.getenv("CANDIDATE_LIMIT", str(DEFAULT_MAX_POSTS * 5))),
        help="Stop scanning after this many qualified candidates before final sorting.",
    )
    parser.add_argument(
        "--scroll-count",
        type=int,
        default=int(os.getenv("SCROLL_COUNT", DEFAULT_SCROLL_COUNT)),
    )
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("THREADS_TIMEOUT_MS", "30000")))
    parser.add_argument(
        "--post-login-wait-ms",
        type=int,
        default=int(os.getenv("THREADS_POST_LOGIN_WAIT_MS", "5000")),
    )
    parser.add_argument(
        "--storage-state",
        default=os.getenv("THREADS_STORAGE_STATE", "runtime/storage/threads-state.json"),
        help="Path for saved Threads cookies/session, relative to project root unless absolute.",
    )
    parser.add_argument(
        "--debug-dir",
        default=os.getenv("THREADS_DEBUG_DIR", "runtime/debug"),
        help="Directory for failure screenshots/HTML, relative to project root unless absolute.",
    )
    parser.add_argument(
        "--force-login",
        action="store_true",
        default=os.getenv("THREADS_FORCE_LOGIN", "").lower() in {"1", "true", "yes"},
        help="Ignore saved session and log in again.",
    )
    parser.add_argument("--headful", action="store_true", help="Show Chromium while scraping.")
    parser.add_argument("--mock", action="store_true", help="Print sample JSON without opening Threads.")
    return parser.parse_args()


def mock_posts() -> list[dict[str, str]]:
    return [
        {
            "id": "mock_threads_001",
            "url": "https://www.threads.net/@demo/post/mock_threads_001",
            "text_preview": "Bai test tieng Viet co dau de kiem tra n8n parse va ghi sheet.",
        }
    ]


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()

    if args.mock:
        print(json.dumps(mock_posts(), ensure_ascii=True))
        return 0

    if not args.username or not args.password:
        log("Missing THREADS_USERNAME or THREADS_PASSWORD. Create .env from .env.example first.")
        return 2

    config = Config(
        username=args.username,
        password=args.password,
        max_posts=args.max_posts,
        scroll_count=args.scroll_count,
        headless=not args.headful,
        timeout_ms=args.timeout_ms,
        post_login_wait_ms=args.post_login_wait_ms,
        storage_state=(
            Path(args.storage_state)
            if Path(args.storage_state).is_absolute()
            else PROJECT_ROOT / args.storage_state
        ),
        debug_dir=(
            Path(args.debug_dir)
            if Path(args.debug_dir).is_absolute()
            else PROJECT_ROOT / args.debug_dir
        ),
        force_login=args.force_login,
        min_engagement_score=args.min_engagement_score,
        candidate_limit=max(args.candidate_limit, args.max_posts),
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
