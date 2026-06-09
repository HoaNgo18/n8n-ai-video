from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
RUNTIME_PATH_RE = re.compile(r"runtime[/\\][^\s\"',\]}]+", re.IGNORECASE)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def resolve_project_path(value: str) -> Path:
    path = Path(value.strip().strip('"').strip("'"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def service_account_path() -> Path:
    configured = (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    )
    return resolve_project_path(configured) if configured else PROJECT_ROOT / "google-service-account.json"


def sheet_id() -> str:
    return os.getenv("GOOGLE_SHEET_ID", "").strip()


def sheet_tab() -> str:
    return os.getenv("GOOGLE_SHEET_TAB", "").strip() or "Threads"


def read_sheet_values() -> list[list[str]]:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    key_path = service_account_path()
    if not sheet_id():
        raise RuntimeError("GOOGLE_SHEET_ID is empty")
    if not key_path.exists():
        raise RuntimeError(f"Google service account file not found: {key_path}")
    credentials = service_account.Credentials.from_service_account_file(str(key_path), scopes=SHEETS_SCOPES)
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id(), range=f"{sheet_tab()}!A:ZZ")
        .execute()
    )
    return response.get("values") or []


def iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(iter_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(iter_strings(item))
        return result
    return []


def extract_runtime_paths(text: str) -> set[Path]:
    paths: set[Path] = set()
    candidates = [text]
    try:
        parsed = json.loads(text)
        candidates.extend(iter_strings(parsed))
    except Exception:
        pass

    for candidate in candidates:
        for match in RUNTIME_PATH_RE.findall(candidate.replace("\\\\", "\\")):
            cleaned = match.rstrip(".,;)")
            try:
                paths.add(resolve_project_path(cleaned))
            except Exception:
                continue
    return paths


def referenced_paths_from_sheet() -> set[Path]:
    values = read_sheet_values()
    references: set[Path] = set()
    for row in values:
        for cell in row:
            references.update(extract_runtime_paths(str(cell)))
    return references


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def is_referenced(candidate: Path, references: set[Path]) -> bool:
    if candidate in references:
        return True
    if candidate.is_dir():
        return any(is_under(reference, candidate) for reference in references)
    return False


def target_roots(include_videos: bool, temp_only: bool = False) -> list[Path]:
    names = [os.getenv("TEMP_DIR", "runtime/data/temp")]
    if not temp_only:
        names.extend(
            [
                os.getenv("VISUALS_DIR", "runtime/data/visuals"),
                os.getenv("AUDIO_DIR", "runtime/data/audio"),
                os.getenv("SCREENSHOTS_DIR", "runtime/data/screenshots"),
            ]
        )
    if include_videos and not temp_only:
        names.append(os.getenv("VIDEOS_DIR", "runtime/data/videos"))
    roots: list[Path] = []
    for name in names:
        root = resolve_project_path(name)
        if root.exists() and is_under(root, PROJECT_ROOT / "runtime"):
            roots.append(root)
    return roots


def all_target_roots_for_docs() -> list[str]:
    return [
        os.getenv("TEMP_DIR", "runtime/data/temp"),
        os.getenv("VISUALS_DIR", "runtime/data/visuals"),
        os.getenv("AUDIO_DIR", "runtime/data/audio"),
        os.getenv("SCREENSHOTS_DIR", "runtime/data/screenshots"),
    ]


def collect_old_files(roots: list[Path], cutoff: datetime, references: set[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified >= cutoff:
                continue
            resolved = path.resolve()
            if is_referenced(resolved, references):
                continue
            candidates.append(resolved)
    return sorted(candidates)


def remove_empty_dirs(roots: list[Path], references: set[Path], apply: bool) -> list[Path]:
    removed: list[Path] = []
    for root in roots:
        dirs = sorted([path for path in root.rglob("*") if path.is_dir()], key=lambda p: len(p.parts), reverse=True)
        for directory in dirs:
            resolved = directory.resolve()
            if is_referenced(resolved, references):
                continue
            try:
                next(directory.iterdir())
                continue
            except StopIteration:
                pass
            if apply:
                directory.rmdir()
            removed.append(resolved)
    return removed


def format_size(bytes_count: int) -> str:
    value = float(bytes_count)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{bytes_count} B"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely clean old runtime files not referenced by Google Sheet.")
    parser.add_argument("--days", type=int, default=14, help="Only consider files older than this many days.")
    parser.add_argument("--apply", action="store_true", help="Delete files. Without this, only prints a dry run.")
    parser.add_argument("--include-videos", action="store_true", help="Also scan runtime/data/videos.")
    parser.add_argument(
        "--without-sheet",
        action="store_true",
        help="Skip Google Sheet lookup and scan only TEMP_DIR. This never touches audio/visual/screenshots/videos.",
    )
    parser.add_argument("--limit", type=int, default=200, help="Max file paths to print in the report.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, args.days))

    sheet_error = ""
    if args.without_sheet:
        references = set()
        roots = target_roots(args.include_videos, temp_only=True)
    else:
        try:
            references = referenced_paths_from_sheet()
        except Exception as exc:
            sheet_error = str(exc)
            report = {
                "mode": "apply" if args.apply else "dry-run",
                "ok": False,
                "error": sheet_error,
                "message": (
                    "Sheet references are required before cleaning audio/visual/screenshots/videos. "
                    "Set GOOGLE_SHEET_ID/GOOGLE_SERVICE_ACCOUNT_FILE or rerun with --without-sheet to scan TEMP_DIR only."
                ),
                "target_roots": all_target_roots_for_docs(),
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        roots = target_roots(args.include_videos)
    candidates = collect_old_files(roots, cutoff, references)
    total_bytes = sum(path.stat().st_size for path in candidates if path.exists())

    if args.apply:
        for path in candidates:
            path.unlink(missing_ok=True)
    empty_dirs = remove_empty_dirs(roots, references, args.apply)

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "ok": True,
        "cutoff_days": args.days,
        "sheet_error": sheet_error,
        "without_sheet": args.without_sheet,
        "referenced_paths": len(references),
        "target_roots": [str(path) for path in roots],
        "candidate_files": len(candidates),
        "candidate_bytes": total_bytes,
        "candidate_size": format_size(total_bytes),
        "empty_dirs": len(empty_dirs),
        "files": [str(path) for path in candidates[: max(0, args.limit)]],
        "dirs": [str(path) for path in empty_dirs[: max(0, args.limit)]],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
