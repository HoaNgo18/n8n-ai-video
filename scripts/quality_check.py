from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from imageio_ffmpeg import get_ffmpeg_exe


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def resolve_project_path(value: str) -> Path:
    path = Path(value.strip().strip('"').strip("'"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def ffprobe_path() -> str:
    ffmpeg = Path(get_ffmpeg_exe())
    candidate = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return str(candidate if candidate.exists() else "ffprobe")


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or f"ffprobe failed for {path}").strip())
    return json.loads(result.stdout or "{}")


def duration_seconds(metadata: dict[str, Any]) -> float:
    value = (metadata.get("format") or {}).get("duration")
    try:
        return float(value)
    except Exception:
        return 0.0


def video_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    return next((stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "video"), None)


def audio_stream(metadata: dict[str, Any]) -> dict[str, Any] | None:
    return next((stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"), None)


def latest_file(root: Path, name: str) -> Path | None:
    if not root.exists():
        return None
    files = [path for path in root.rglob(name) if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def find_by_id(root: Path, post_id: str, name: str) -> Path | None:
    if not root.exists():
        return None
    files = [path for path in root.rglob(name) if path.is_file() and post_id in str(path)]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def load_timing(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ["segments", "timing", "items"]:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quick quality checks for generated audio/visual/final video.")
    parser.add_argument("--id", default="", help="Post ID. If omitted, checks the latest final.mp4.")
    parser.add_argument("--final", default="", help="Explicit final video path.")
    parser.add_argument("--visual", default="", help="Explicit visual video path.")
    parser.add_argument("--audio", default="", help="Explicit narration audio path.")
    parser.add_argument("--timing", default="", help="Explicit audio timing JSON path.")
    parser.add_argument("--max-duration-drift", type=float, default=0.75)
    return parser.parse_args()


def main() -> int:
    load_dotenv(ENV_PATH, override=True)
    args = parse_args()
    videos_root = resolve_project_path(os.getenv("VIDEOS_DIR", "runtime/data/videos"))
    visuals_root = resolve_project_path(os.getenv("VISUALS_DIR", "runtime/data/visuals"))
    audio_root = resolve_project_path(os.getenv("AUDIO_DIR", "runtime/data/audio"))

    final_path = resolve_project_path(args.final) if args.final else None
    visual_path = resolve_project_path(args.visual) if args.visual else None
    audio_path = resolve_project_path(args.audio) if args.audio else None
    timing_path = resolve_project_path(args.timing) if args.timing else None

    if not final_path:
        final_path = find_by_id(videos_root, args.id, "final.mp4") if args.id else latest_file(videos_root, "final.mp4")
    if not visual_path and args.id:
        visual_path = find_by_id(visuals_root, args.id, "visual.mp4")
    if not audio_path and args.id:
        audio_path = find_by_id(audio_root, args.id, "narration.wav")
    if not timing_path and args.id:
        timing_path = find_by_id(audio_root, args.id, "audio_timing_debug.json")

    checks: list[dict[str, Any]] = []
    if not final_path or not final_path.exists():
        add_check(checks, "final_exists", False, "final.mp4 not found")
        print(json.dumps({"ok": False, "checks": checks}, ensure_ascii=False, indent=2))
        return 1

    final_meta = probe(final_path)
    final_video = video_stream(final_meta)
    final_audio = audio_stream(final_meta)
    final_duration = duration_seconds(final_meta)
    expected_width = int(os.getenv("VIDEO_WIDTH", "1080") or "1080")
    expected_height = int(os.getenv("VIDEO_HEIGHT", "1920") or "1920")

    add_check(checks, "final_video_stream", final_video is not None, str(final_path))
    add_check(checks, "final_audio_stream", final_audio is not None, str(final_path))
    if final_video:
        width = int(final_video.get("width") or 0)
        height = int(final_video.get("height") or 0)
        add_check(checks, "final_dimensions", width == expected_width and height == expected_height, f"{width}x{height}")
    add_check(checks, "final_duration", final_duration > 1.0, f"{final_duration:.2f}s")

    visual_duration = 0.0
    if visual_path and visual_path.exists():
        visual_duration = duration_seconds(probe(visual_path))
        drift = abs(final_duration - visual_duration)
        add_check(checks, "visual_duration_sync", drift <= args.max_duration_drift, f"drift={drift:.2f}s")

    audio_duration = 0.0
    if audio_path and audio_path.exists():
        audio_duration = duration_seconds(probe(audio_path))
        drift = abs(final_duration - audio_duration)
        add_check(checks, "audio_duration_sync", drift <= args.max_duration_drift + 1.0, f"drift={drift:.2f}s")

    timing = load_timing(timing_path)
    if timing:
        bad_segments = []
        for index, segment in enumerate(timing, start=1):
            start = float(segment.get("start", segment.get("start_time", 0)) or 0)
            end = float(segment.get("end", segment.get("end_time", start)) or start)
            if start < 0 or end <= start or (audio_duration and end > audio_duration + 0.5):
                bad_segments.append(index)
        add_check(checks, "audio_timing_manifest", not bad_segments, f"segments={len(timing)} bad={bad_segments[:8]}")

    ok = all(check["ok"] for check in checks)
    print(
        json.dumps(
            {
                "ok": ok,
                "id": args.id,
                "final": str(final_path),
                "visual": str(visual_path) if visual_path else "",
                "audio": str(audio_path) if audio_path else "",
                "timing": str(timing_path) if timing_path else "",
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
