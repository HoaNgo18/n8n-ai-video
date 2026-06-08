from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_SERVICE_ACCOUNT_PATH = PROJECT_ROOT / "google-service-account.json"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


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


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def service_account_path() -> Path:
    configured = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )
    return resolve_path(configured) if configured else DEFAULT_SERVICE_ACCOUNT_PATH


def drive_service():
    key_path = service_account_path()
    if not key_path.exists():
        raise RuntimeError(
            f"Google service account file not found: {key_path}. "
            "Set GOOGLE_SERVICE_ACCOUNT_FILE or mount google-service-account.json into /workspace."
        )
    credentials = service_account.Credentials.from_service_account_file(str(key_path), scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def upload_to_drive(post_id: str, video_path: Path) -> dict:
    if not video_path.exists():
        raise RuntimeError(f"Video file not found: {video_path}")

    folder_id = os.getenv("DRAFT_REVIEW_DRIVE_FOLDER_ID", "").strip()
    file_name = f"phase3_draft_{post_id}_{video_path.name}"
    metadata: dict[str, object] = {"name": file_name}
    if folder_id:
        metadata["parents"] = [folder_id]

    service = drive_service()
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    file_data = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,webViewLink,webContentLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    share_note = "private"
    if bool_env("DRAFT_REVIEW_SHARE_ANYONE", True):
        try:
            service.permissions().create(
                fileId=file_data["id"],
                body={"type": "anyone", "role": "reader"},
                fields="id",
                supportsAllDrives=True,
            ).execute()
            share_note = "anyone_with_link"
        except Exception as exc:
            share_note = f"share_failed: {str(exc)[:160]}"
            log(f"Could not share draft video publicly: {exc}")

    return {
        "file_id": file_data.get("id", ""),
        "web_view_link": file_data.get("webViewLink", ""),
        "web_content_link": file_data.get("webContentLink", ""),
        "share_note": share_note,
    }


def prepare_draft_review(post_id: str, video_path: Path, caption: str) -> dict:
    upload = upload_to_drive(post_id, video_path)
    draft_url = upload["web_view_link"] or upload["web_content_link"]
    if not draft_url:
        raise RuntimeError("Google Drive upload succeeded but no review URL was returned")

    return {
        "ID": post_id,
        "Video_Path": str(video_path),
        "Caption": caption,
        "Draft_Video_URL": draft_url,
        "Draft_Drive_File_ID": upload["file_id"],
        "Status": "Draft",
        "Note": f"Phase 4: draft uploaded for review ({upload['share_note']})",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload the final Phase 3 draft for admin review.")
    parser.add_argument("--id", required=True)
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--caption", required=True)
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    post_id = decode_cli_text(args.id)
    video_path = resolve_path(decode_cli_text(args.video_path))
    caption = decode_cli_text(args.caption)

    try:
        result = prepare_draft_review(post_id, video_path, caption)
    except Exception as exc:
        log(f"Draft review prep failed: {exc}")
        result = {
            "ID": post_id,
            "Video_Path": str(video_path),
            "Caption": caption,
            "Draft_Video_URL": "",
            "Draft_Drive_File_ID": "",
            "Status": "Draft",
            "Note": f"Phase 4 draft review upload failed: {str(exc)[:320]}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
