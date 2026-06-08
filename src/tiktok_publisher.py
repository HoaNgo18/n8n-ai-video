from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
TIKTOK_OAUTH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
DEFAULT_CHUNK_SIZE = 64 * 1024 * 1024
TOKEN_REFRESH_SKEW_SECONDS = 300


class TikTokApiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code or "unknown_error"
        self.message = message or ""
        super().__init__(f"{self.code} {self.message}".strip())


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


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def clean_caption(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:2200]


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def int_env(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def now_epoch() -> int:
    return int(time.time())


def update_env_file(updates: dict[str, str]) -> None:
    if not updates:
        return

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    updated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue

        key, _, _ = line.partition("=")
        if key in remaining:
            updated_lines.append(f"{key}={remaining.pop(key)}")
        else:
            updated_lines.append(line)

    if remaining:
        if updated_lines and updated_lines[-1].strip():
            updated_lines.append("")
        for key, value in remaining.items():
            updated_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def persist_token_bundle(bundle: dict) -> dict:
    access_expires_in = int(bundle.get("expires_in") or 0)
    refresh_expires_in = int(bundle.get("refresh_expires_in") or 0)
    updates = {
        "TIKTOK_ACCESS_TOKEN": str(bundle.get("access_token") or ""),
        "TIKTOK_REFRESH_TOKEN": str(bundle.get("refresh_token") or ""),
        "TIKTOK_OPEN_ID": str(bundle.get("open_id") or ""),
        "TIKTOK_TOKEN_SCOPE": str(bundle.get("scope") or ""),
        "TIKTOK_ACCESS_TOKEN_EXPIRES_AT": str(now_epoch() + access_expires_in) if access_expires_in else "",
        "TIKTOK_REFRESH_TOKEN_EXPIRES_AT": str(now_epoch() + refresh_expires_in) if refresh_expires_in else "",
    }
    update_env_file(updates)
    for key, value in updates.items():
        os.environ[key] = value
    return {
        "access_token": updates["TIKTOK_ACCESS_TOKEN"],
        "refresh_token": updates["TIKTOK_REFRESH_TOKEN"],
        "open_id": updates["TIKTOK_OPEN_ID"],
        "scope": updates["TIKTOK_TOKEN_SCOPE"],
        "expires_at": updates["TIKTOK_ACCESS_TOKEN_EXPIRES_AT"],
        "refresh_expires_at": updates["TIKTOK_REFRESH_TOKEN_EXPIRES_AT"],
    }


def request_oauth_token(payload: dict[str, str]) -> dict:
    response = requests.post(
        TIKTOK_OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(
            f"TikTok OAuth failed: {data.get('error')} {data.get('error_description') or ''}".strip()
        )
    return data


def require_tiktok_client_credentials() -> tuple[str, str]:
    client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
    client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()
    if not client_key or not client_secret:
        raise RuntimeError("TIKTOK_CLIENT_KEY or TIKTOK_CLIENT_SECRET is missing")
    return client_key, client_secret


def exchange_code_for_tokens(code: str) -> dict:
    client_key, client_secret = require_tiktok_client_credentials()
    redirect_uri = os.getenv("TIKTOK_REDIRECT_URI", "").strip()
    if not redirect_uri:
        raise RuntimeError("TIKTOK_REDIRECT_URI is missing")
    data = request_oauth_token(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code.strip(),
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    )
    return persist_token_bundle(data)


def refresh_access_token(refresh_token: str | None = None) -> dict:
    client_key, client_secret = require_tiktok_client_credentials()
    token = (refresh_token or os.getenv("TIKTOK_REFRESH_TOKEN", "")).strip()
    if not token:
        raise RuntimeError("TIKTOK_REFRESH_TOKEN is missing")
    data = request_oauth_token(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": token,
        }
    )
    return persist_token_bundle(data)


def get_access_token() -> str:
    access_token = os.getenv("TIKTOK_ACCESS_TOKEN", "").strip()
    expires_at = int_env("TIKTOK_ACCESS_TOKEN_EXPIRES_AT", 0)
    if access_token and (not expires_at or expires_at > now_epoch() + TOKEN_REFRESH_SKEW_SECONDS):
        return access_token

    refresh_token = os.getenv("TIKTOK_REFRESH_TOKEN", "").strip()
    if refresh_token:
        return refresh_access_token(refresh_token)["access_token"]

    if access_token:
        return access_token

    raise RuntimeError("TikTok token is missing. Exchange an auth code first or set TIKTOK_ACCESS_TOKEN.")


def mime_type_for(path: Path) -> str:
    guessed = mimetypes.guess_type(str(path))[0]
    if guessed in {"video/mp4", "video/quicktime", "video/webm"}:
        return guessed
    if path.suffix.lower() in {".mov", ".qt"}:
        return "video/quicktime"
    if path.suffix.lower() == ".webm":
        return "video/webm"
    return "video/mp4"


def query_creator_info(access_token: str) -> dict:
    response = requests.post(
        TIKTOK_CREATOR_INFO_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json={},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    error = data.get("error") or {}
    if error.get("code") not in {None, "ok"}:
        raise TikTokApiError(error.get("code") or "unknown_error", error.get("message") or "")
    return data.get("data") or {}


def choose_privacy_level(creator_info: dict) -> str:
    requested = os.getenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY").strip() or "SELF_ONLY"
    options = creator_info.get("privacy_level_options") or []
    if not options or requested in options:
        return requested
    if "SELF_ONLY" in options:
        return "SELF_ONLY"
    return str(options[0])


def initialize_publish(video_path: Path, caption: str, access_token: str, chunk_size: int, creator_info: dict) -> dict:
    size = video_path.stat().st_size
    total_chunk_count = max(1, (size + chunk_size - 1) // chunk_size)
    payload = {
        "post_info": {
            "title": caption,
            "privacy_level": choose_privacy_level(creator_info),
            "disable_duet": bool_env("TIKTOK_DISABLE_DUET", False),
            "disable_comment": bool_env("TIKTOK_DISABLE_COMMENT", False),
            "disable_stitch": bool_env("TIKTOK_DISABLE_STITCH", False),
            "video_cover_timestamp_ms": int(os.getenv("TIKTOK_COVER_TIMESTAMP_MS", "1000")),
            "brand_content_toggle": bool_env("TIKTOK_BRAND_CONTENT", False),
            "brand_organic_toggle": bool_env("TIKTOK_BRAND_ORGANIC", False),
            "is_aigc": bool_env("TIKTOK_IS_AIGC", False),
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": min(chunk_size, size),
            "total_chunk_count": total_chunk_count,
        },
    }

    response = requests.post(
        TIKTOK_INIT_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    error = data.get("error") or {}
    if error.get("code") not in {None, "ok"}:
        raise TikTokApiError(error.get("code") or "unknown_error", error.get("message") or "")

    publish_data = data.get("data") or {}
    if not publish_data.get("publish_id") or not publish_data.get("upload_url"):
        raise RuntimeError(f"TikTok init response missing publish_id/upload_url: {data}")
    return publish_data


def upload_video(upload_url: str, video_path: Path, chunk_size: int) -> None:
    size = video_path.stat().st_size
    content_type = mime_type_for(video_path)
    with video_path.open("rb") as handle:
        start = 0
        while start < size:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            end = start + len(chunk) - 1
            response = requests.put(
                upload_url,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                data=chunk,
                timeout=180,
            )
            response.raise_for_status()
            start = end + 1


def publish(post_id: str, video_path: Path, caption: str) -> dict:
    if not video_path.exists():
        raise RuntimeError(f"Video file not found: {video_path}")
    if not caption:
        raise RuntimeError("Caption is empty")

    if bool_env("TIKTOK_DRY_RUN", False):
        return {
            "ID": post_id,
            "TikTok_Publish_ID": f"dry_run_{post_id}",
            "Published_URL": "",
            "Status": "Published",
            "Note": "Phase 4: dry-run publish completed; no TikTok upload was sent",
        }

    chunk_size = int(os.getenv("TIKTOK_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
    access_token = get_access_token()

    try:
        creator_info = query_creator_info(access_token)
    except TikTokApiError as exc:
        if exc.code != "access_token_invalid":
            raise RuntimeError(f"TikTok creator info failed: {exc}") from exc
        access_token = refresh_access_token()["access_token"]
        creator_info = query_creator_info(access_token)

    try:
        init_data = initialize_publish(video_path, caption, access_token, chunk_size, creator_info)
    except TikTokApiError as exc:
        if exc.code == "access_token_invalid":
            access_token = refresh_access_token()["access_token"]
            init_data = initialize_publish(video_path, caption, access_token, chunk_size, creator_info)
        elif exc.code == "scope_not_authorized":
            raise RuntimeError("TikTok token lacks video.publish scope") from exc
        else:
            raise RuntimeError(f"TikTok init failed: {exc}") from exc

    upload_video(init_data["upload_url"], video_path, chunk_size)

    return {
        "ID": post_id,
        "TikTok_Publish_ID": init_data["publish_id"],
        "Published_URL": "",
        "Status": "Published",
        "Note": "Phase 4: published to TikTok; URL may need manual fill after processing",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a draft video to TikTok.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--caption", required=True)
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    post_id = decode_cli_text(args.id)
    video_path = resolve_path(decode_cli_text(args.video_path))
    caption = clean_caption(decode_cli_text(args.caption))

    try:
        result = publish(post_id, video_path, caption)
    except Exception as exc:
        log(f"TikTok publish failed: {exc}")
        result = {
            "ID": post_id,
            "TikTok_Publish_ID": "",
            "Published_URL": "",
            "Status": "Failed",
            "Note": f"Phase 4 publish failed: {str(exc)[:320]}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
