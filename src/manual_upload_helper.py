from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"


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
    return " ".join((value or "").split()).strip()


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def get_exports_dir() -> Path:
    configured = os.getenv("EXPORTS_DIR", "runtime/data/exports").strip() or "runtime/data/exports"
    return resolve_path(configured)


def build_package(post_id: str, video_path: Path, caption: str) -> dict:
    if not video_path.exists():
        raise RuntimeError(f"Video file not found: {video_path}")
    if not caption:
        raise RuntimeError("Caption is empty")

    date_part = datetime.now().strftime("%Y-%m-%d")
    package_dir = get_exports_dir() / "manual_upload" / date_part / post_id
    package_dir.mkdir(parents=True, exist_ok=True)

    caption_path = package_dir / "caption.txt"
    metadata_path = package_dir / "upload_metadata.json"
    checklist_path = package_dir / "upload_checklist.txt"

    caption_path.write_text(caption + "\n", encoding="utf-8")

    packaged_video_path = ""
    if bool_env("MANUAL_UPLOAD_COPY_VIDEO", False):
        packaged_video = package_dir / video_path.name
        shutil.copy2(video_path, packaged_video)
        packaged_video_path = str(packaged_video)

    metadata = {
        "id": post_id,
        "original_video_path": str(video_path),
        "caption_path": str(caption_path),
        "packaged_video_path": packaged_video_path,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "Approved",
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    checklist_lines = [
        f"Manual TikTok upload package for {post_id}",
        "",
        "1. Open the video file.",
        f"   Original video path: {video_path}",
    ]
    if packaged_video_path:
        checklist_lines.extend(
            [
                f"   Copied video path: {packaged_video_path}",
            ]
        )
    checklist_lines.extend(
        [
            "2. Copy the caption from caption.txt.",
            "3. Upload the video manually to TikTok.",
            "4. Paste the final TikTok URL into Published_URL in Google Sheets.",
            "",
            f"Caption file: {caption_path}",
            f"Metadata file: {metadata_path}",
        ]
    )
    checklist_path.write_text("\n".join(checklist_lines) + "\n", encoding="utf-8")

    return {
        "ID": post_id,
        "Status": "Approved",
        "Upload_Package_Path": str(package_dir),
        "Upload_Caption_Path": str(caption_path),
        "Published_URL": "",
        "Note": "Phase 4: manual upload package prepared",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a manual TikTok upload package.")
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
        result = build_package(post_id, video_path, caption)
    except Exception as exc:
        result = {
            "ID": post_id,
            "Status": "Approved",
            "Upload_Package_Path": "",
            "Upload_Caption_Path": "",
            "Published_URL": "",
            "Note": f"Phase 4 helper failed: {str(exc)[:320]}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
