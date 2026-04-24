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


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


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


def is_vietnamese(text: str) -> bool:
    return bool(VIET_PATTERN.search(text or ""))


def build_narrator_script(post_text: str, comments: list[dict[str, str]]) -> str:
    post_text = clean_text(post_text)
    comment_texts = [clean_text(c.get("text", "")) for c in comments if clean_text(c.get("text", ""))]
    parts = []

    if post_text:
        parts.append(f"Cau chuyen dang duoc chu y tren Threads: {post_text}")
    for index, comment in enumerate(comment_texts[:3], start=1):
        parts.append(f"Binh luan {index}: {comment}")

    script = " ".join(parts)
    return script[:1800]


def trim_ui_text(text: str) -> str:
    text = clean_text(text)
    text = re.sub(r"\bTop\s+View activity\b", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def dedupe_comments(comments: list[dict[str, str]]) -> list[dict[str, str]]:
    unique = []
    seen = set()

    for comment in comments:
        text = trim_ui_text(comment.get("text", ""))
        if not text:
            continue
        normalized = re.sub(r"[^0-9A-Za-z\u00c0-\u1ef9]+", "", text.lower())
        # Drop nested duplicate comment text: keep the richer block with author
        if any(normalized and normalized in old for old in seen):
            continue
        seen.add(normalized)
        unique.append({"text": text})

    return unique


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
            blocks.append({"index": index, "handle": handle, "text": text, "rect": box})
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


def choose_post_and_comments(blocks: list[dict], max_comments: int) -> tuple[dict | None, list[dict]]:
    reasonable = [block for block in blocks if is_reasonable_pressable(block)]
    if not reasonable:
        return None, []

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

    comments = []
    seen = {clean_text(post_block.get("text", ""))[:180]}
    for block in reasonable:
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
        comments.append(block)
        if len(comments) >= max_comments:
            break

    return post_block, comments


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


async def screenshot_clip(page: Page, rect: dict, output_path: Path, padding: int = 8) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    viewport = page.viewport_size or {"width": 1365, "height": 900}
    x = max(0, float(rect.get("x", 0)) - padding)
    y = max(0, float(rect.get("y", 0)) - padding)
    width = float(rect.get("width", 1)) + padding * 2
    height = float(rect.get("height", 1)) + padding * 2
    width = min(width, max(1, viewport["width"] - x))
    height = min(height, max(1, viewport["height"] - y))

    await page.screenshot(
        path=str(output_path),
        clip={"x": x, "y": y, "width": max(1, width), "height": max(1, height)},
    )


async def screenshot_post_and_comments(page: Page, post_id: str, post_dir: Path, max_comments: int) -> tuple[dict, dict]:
    post_path = post_dir / "post.png"
    comments_dir = post_dir / "comments"
    await prepare_threads_page(page)
    await page.locator(POST_LOCATOR).first.wait_for(state="visible", timeout=10000)
    blocks = await get_pressable_blocks(page)
    post_block, comment_blocks = choose_post_and_comments(blocks, max_comments)

    if not post_block:
        log("Could not isolate post block from pressable containers; falling back to first visible post area.")
        await screenshot_primary_area(page, post_path)
        return {"post": str(post_path), "comments": []}, {"post_text": "", "comments": []}

    await wait_for_element_media(page, post_block["handle"])
    post_rect = await refresh_block_box(post_block)
    post_rect = await trim_post_rect_before_discussion_toolbar(page, post_rect)
    await screenshot_clip(page, post_rect, post_path, padding=8)

    comment_paths = []
    extracted_comments = []
    for index, block in enumerate(comment_blocks, start=1):
        comment_path = comments_dir / f"comment_{index:02d}.png"
        comment_rect = await refresh_block_box(block)
        await wait_for_element_media(page, block["handle"])
        await screenshot_clip(page, comment_rect, comment_path, padding=8)
        comment_paths.append(str(comment_path))
        extracted_comments.append({"text": trim_ui_text(block.get("text", ""))})

    screenshots = {"post": str(post_path), "comments": comment_paths}
    extracted = {
        "post_text": trim_ui_text(post_block.get("text", "")),
        "comments": dedupe_comments(extracted_comments),
    }
    return screenshots, extracted


async def process_post(post_id: str, url: str, config: Config) -> dict:
    url = canonical_url(url)
    run_date = datetime.now().strftime("%Y-%m-%d")
    post_dir = config.screenshots_dir / run_date / safe_filename(post_id)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config.headless)
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
                max_comments=5,
            )
            content = await extract_visible_content(page)

            post_text = trim_ui_text(isolated_content.get("post_text", "")) or trim_ui_text(content.get("post_text", ""))
            comments = dedupe_comments([
                {"text": trim_ui_text(comment.get("text", ""))}
                for comment in (isolated_content.get("comments", []) or content.get("comments", []))
                if trim_ui_text(comment.get("text", ""))
            ])
            narrator_script = build_narrator_script(post_text, comments)

            extracted = {
                "post_text": post_text,
                "comments": comments,
                "page_title": content.get("page_title", ""),
                "current_url": content.get("current_url", page.url),
            }

            note = f"Phase 2: isolated post screenshot + {len(screenshots.get('comments', []))} comment screenshots"
            if not post_text:
                note = "Phase 2 warning: screenshot saved but no text extracted"

            return {
                "ID": post_id,
                "Screenshots": json.dumps(screenshots, ensure_ascii=True),
                "Extracted_Content": json.dumps(extracted, ensure_ascii=True),
                "Narrator_Script": narrator_script,
                "Status": "In Progress",
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
    load_dotenv(PROJECT_ROOT / ".env")
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
        result = asyncio.run(process_post(args.id, args.url, config))
    except Exception as exc:
        log(f"Screenshot extractor failed: {exc}")
        log(traceback.format_exc())
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
