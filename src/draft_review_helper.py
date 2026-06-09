from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from local_review import build_review_links, resolve_path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


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


def prepare_draft_review(post_id: str, video_path: Path, caption: str) -> dict:
    if not post_id:
        raise RuntimeError("Missing review post ID")
    if not video_path.exists():
        raise RuntimeError(f"Video file not found: {video_path}")

    links = build_review_links(post_id, video_path)
    return {
        "ID": post_id,
        "Video_Path": str(video_path),
        "Caption": caption,
        "Draft_Video_URL": links["review_url"],
        "Draft_Video_Download_URL": links["video_url"],
        "Draft_Drive_File_ID": "",
        "Status": "Draft",
        "Note": "Phase 4: local review link created; keep runner and tunnel online until admin approves or rejects",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a local signed review link for a Phase 3 draft.")
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
            "Draft_Video_Download_URL": "",
            "Draft_Drive_File_ID": "",
            "Status": "Draft",
            "Note": f"Phase 4 local review link failed: {str(exc)[:320]}",
        }

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
