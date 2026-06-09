from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_COOKIES_PATH = PROJECT_ROOT / "tiktok_cookies.json"
DEFAULT_COVER_DIR = PROJECT_ROOT / "runtime" / "data" / "temp" / "tiktok_covers"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def decode_cli_text(value: str | None) -> str:
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


def clean_caption(value: str) -> str:
    text = " ".join((value or "").split()).strip()
    return text[:2200]


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def send_telegram_notification(message: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        ).raise_for_status()
    except Exception as exc:
        log(f"Failed to send Telegram notification: {exc}")


def clean_log_text(value: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize_upload_log(stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in [stdout, stderr] if part.strip())
    lines = [clean_log_text(line) for line in combined.splitlines()]
    useful = [
        line
        for line in lines
        if line
        and not line.startswith("DEBUG: Adding cookie:")
        and "sessionid" not in line.lower()
    ]
    if not useful:
        return "no uploader detail"
    return " | ".join(useful[-4:])[:260]


def cookie_source_path() -> Path:
    configured = os.getenv("TIKTOK_UPLOADER_COOKIES_PATH", "").strip()
    return resolve_path(configured) if configured else DEFAULT_COOKIES_PATH


def load_cookie_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid TikTok cookie JSON: {path}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"TikTok cookie JSON must be a list: {path}")
    return [cookie for cookie in data if isinstance(cookie, dict)]


def browser_kwargs() -> dict[str, Any]:
    return {
        "browser": os.getenv("TIKTOK_UPLOAD_BROWSER", "chrome").strip() or "chrome",
        "headless": bool_env("TIKTOK_UPLOAD_HEADLESS", True),
    }


def upload_visibility() -> str:
    privacy_level = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").strip().upper()
    return {
        "PUBLIC_TO_EVERYONE": "everyone",
        "MUTUAL_FOLLOW_FRIENDS": "friends",
        "FOLLOWER_OF_CREATOR": "friends",
        "SELF_ONLY": "only_you",
    }.get(privacy_level, "only_you")


def make_upload_cover(video_path: Path) -> Path | None:
    timestamp_ms = int(os.getenv("TIKTOK_COVER_TIMESTAMP_MS", "1000"))
    if timestamp_ms < 0:
        return None

    cover_dir = resolve_path(os.getenv("TIKTOK_COVER_DIR", str(DEFAULT_COVER_DIR)))
    cover_dir.mkdir(parents=True, exist_ok=True)
    cover_path = cover_dir / f"{video_path.stem}_cover_{timestamp_ms}.jpg"
    seek_seconds = max(0, timestamp_ms) / 1000

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-q:v",
        "2",
        str(cover_path),
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as exc:
        log(f"Could not generate TikTok cover image: {exc}")
        return None

    if not cover_path.exists() or cover_path.stat().st_size <= 0:
        log("Could not generate TikTok cover image: empty output")
        return None

    log(f"Using TikTok cover image: {cover_path}")
    return cover_path


def upload_with_tiktok_uploader(video_path: Path, caption: str) -> bool:
    try:
        import tiktok_uploader.upload as upload_module
        from tiktok_uploader.upload import TikTokUploader
    except ImportError as exc:
        raise RuntimeError("Missing dependency: install tiktok-uploader in the runner image") from exc

    patch_tiktok_uploader_modals(upload_module)

    sessionid = os.getenv("TIKTOK_SESSION_ID", "").strip()
    username = os.getenv("TIKTOK_USERNAME", "").strip()
    password = os.getenv("TIKTOK_PASSWORD", "").strip()
    cookies_path = cookie_source_path()
    auth_kwargs: dict[str, Any]

    if sessionid:
        auth_kwargs = {
            "cookies_list": [
                {
                    "name": "sessionid",
                    "value": sessionid,
                    "domain": ".tiktok.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        }
    elif cookies_path.exists() and cookies_path.suffix.lower() == ".json":
        auth_kwargs = {"cookies_list": load_cookie_json(cookies_path)}
    elif cookies_path.exists():
        auth_kwargs = {"cookies": str(cookies_path)}
    elif username and password:
        auth_kwargs = {"username": username, "password": password}
    else:
        raise RuntimeError(
            "TikTok upload auth is missing. Set TIKTOK_SESSION_ID, provide "
            f"TIKTOK_UPLOADER_COOKIES_PATH / {DEFAULT_COOKIES_PATH.name}, "
            "or set TIKTOK_USERNAME and TIKTOK_PASSWORD."
        )

    uploader = TikTokUploader(**auth_kwargs, **browser_kwargs())
    cover_path = make_upload_cover(video_path)
    return bool(
        uploader.upload_video(
            str(video_path),
            description=caption,
            cover=str(cover_path) if cover_path else None,
            visibility=upload_visibility(),
            num_retries=int(os.getenv("TIKTOK_UPLOAD_RETRIES", "1")),
            skip_split_window=bool_env("TIKTOK_SKIP_SPLIT_WINDOW", False),
        )
    )


def patch_tiktok_uploader_modals(upload_module: Any) -> None:
    if getattr(upload_module, "_project_iii_modal_patch", False):
        return

    def dismiss_tiktok_modals(page: Any) -> None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        script = """
        () => {
          const labels = ['got it', 'ok', 'okay', 'continue', 'not now', 'close', 'done', 'i understand'];
          const roots = [
            ...document.querySelectorAll('[data-floating-ui-portal], .TUXModal-overlay, [role="dialog"]'),
            document.body,
          ];
          for (const root of roots) {
            const candidates = [...root.querySelectorAll('button, [role="button"]')];
            for (const el of candidates) {
              const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const visible = rect.width > 0 && rect.height > 0;
              if (visible && labels.some(label => text === label || text.includes(label))) {
                el.click();
                return true;
              }
            }
          }
          const modalTextNeedles = [
            "we'll automatically check",
            "automatically check if your video",
            "copyright",
          ];
          for (const portal of document.querySelectorAll('[data-floating-ui-portal], [role="dialog"]')) {
            const text = (portal.innerText || portal.textContent || '').toLowerCase();
            const rect = portal.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0 && modalTextNeedles.some(needle => text.includes(needle))) {
              portal.remove();
              return true;
            }
          }
          for (const overlay of document.querySelectorAll('.TUXModal-overlay')) {
            const rect = overlay.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
              overlay.remove();
              return true;
            }
          }
          return false;
        }
        """
        for _ in range(4):
            try:
                clicked = page.evaluate(script)
                if clicked:
                    page.wait_for_timeout(700)
                    continue
            except Exception:
                pass
            break

    original_set_description = upload_module._set_description
    original_set_visibility = upload_module._set_visibility
    original_post_video = upload_module._post_video
    original_set_interactivity = upload_module._set_interactivity

    def patched_set_interactivity(page: Any, *args: Any, **kwargs: Any) -> Any:
        dismiss_tiktok_modals(page)
        result = original_set_interactivity(page, *args, **kwargs)
        dismiss_tiktok_modals(page)
        return result

    def patched_set_description(page: Any, *args: Any, **kwargs: Any) -> Any:
        dismiss_tiktok_modals(page)
        result = original_set_description(page, *args, **kwargs)
        dismiss_tiktok_modals(page)
        return result

    def patched_set_visibility(page: Any, *args: Any, **kwargs: Any) -> Any:
        dismiss_tiktok_modals(page)
        result = original_set_visibility(page, *args, **kwargs)
        dismiss_tiktok_modals(page)
        return result

    def patched_post_video(page: Any, *args: Any, **kwargs: Any) -> Any:
        dismiss_tiktok_modals(page)
        return original_post_video(page, *args, **kwargs)

    upload_module._set_interactivity = patched_set_interactivity
    upload_module._set_description = patched_set_description
    upload_module._set_visibility = patched_set_visibility
    upload_module._post_video = patched_post_video
    upload_module._project_iii_modal_patch = True


def publish_video(post_id: str, video_path: str, caption: str) -> dict[str, str]:
    resolved_video_path = resolve_path(video_path)
    if not resolved_video_path.exists():
        raise RuntimeError(f"Video file not found: {resolved_video_path}")

    cleaned_caption = clean_caption(caption)
    if not cleaned_caption:
        raise RuntimeError("Caption is empty")

    log("Starting TikTok browser/cookie upload via tiktok-uploader...")
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
        uploaded = upload_with_tiktok_uploader(resolved_video_path, cleaned_caption)

    upload_stdout = captured_stdout.getvalue()
    upload_stderr = captured_stderr.getvalue()
    if upload_stdout.strip():
        log(upload_stdout.strip())
    if upload_stderr.strip():
        log(upload_stderr.strip())

    if not uploaded:
        detail = summarize_upload_log(upload_stdout, upload_stderr)
        raise RuntimeError(f"tiktok-uploader returned an unsuccessful upload result: {detail}")
    log("TikTok upload finished.")

    return {
        "ID": post_id,
        "TikTok_Publish_ID": f"browser_{int(time.time())}",
        "Published_URL": "",
        "Status": "Published",
        "Note": "Phase 4: published to TikTok via browser cookie uploader",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a video to TikTok via browser cookie uploader.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--caption", required=True)
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    post_id = decode_cli_text(args.id)
    video_path = decode_cli_text(args.video_path)
    caption = clean_caption(decode_cli_text(args.caption))

    try:
        result = publish_video(post_id, video_path, caption)
    except Exception as exc:
        log(f"TikTok browser upload failed: {exc}")
        send_telegram_notification(
            f"TikTok upload failed for Post {post_id}:\n{str(exc)[:700]}"
        )
        result = {
            "ID": post_id,
            "TikTok_Publish_ID": "",
            "Published_URL": "",
            "Status": "Failed",
            "Note": f"Phase 4 browser upload failed: {str(exc)[:320]}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
