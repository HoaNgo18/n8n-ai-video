"""
Phase 3: split video pipeline.

Modes:
    voice  -> generate narration audio from Narrator_Script
    visual -> build silent background+screenshots video
    merge  -> mux Audio_Path + Visual_Video_Path into final MP4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from gtts import gTTS
from moviepy import AudioFileClip, ColorClip, CompositeVideoClip, ImageClip, VideoFileClip

try:
    import edge_tts
except ImportError:  # pragma: no cover
    edge_tts = None


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
TARGET_SIZE = (1080, 1920)
DEFAULT_BACKGROUND = "runtime/assets/background.mp4"
DEFAULT_AUDIO_DIR = "runtime/data/audio"
DEFAULT_VISUALS_DIR = "runtime/data/visuals"
DEFAULT_VIDEOS_DIR = "runtime/data/videos"
DEFAULT_TEMP_DIR = "runtime/data/temp"
DEFAULT_TTS_VOICE = "vi-VN-HoaiMyNeural"
FPT_TTS_URL = "https://api.fpt.ai/hmi/tts/v5"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def resolve_media_path(value: str | Path) -> Path:
    path = resolve_path(value)
    if path.exists():
        return path

    raw = str(value).replace("/", "\\")
    marker = "\\screenshots\\"
    if marker in raw:
        legacy_tail = raw.split(marker, 1)[1]
        legacy_path = PROJECT_ROOT / "runtime" / "data" / "screenshots" / "legacy" / Path(legacy_tail)
        if legacy_path.exists():
            return legacy_path

    if raw.lower().startswith("screenshots\\"):
        legacy_tail = raw.split("\\", 1)[1]
        legacy_path = PROJECT_ROOT / "runtime" / "data" / "screenshots" / "legacy" / Path(legacy_tail)
        if legacy_path.exists():
            return legacy_path

    return path


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


def clean_script(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:4800]


def parse_screenshots(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Screenshots must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Screenshots must be a JSON object.")
    return data


def screenshot_paths(screenshots: dict) -> list[Path]:
    paths = []
    if screenshots.get("post"):
        paths.append(resolve_media_path(screenshots["post"]))
    for item in screenshots.get("comments", []) or []:
        if item:
            paths.append(resolve_media_path(item))
    return [path for path in paths if path.exists()]


def estimate_duration(text: str, image_count: int) -> float:
    word_count = len(re.findall(r"\S+", text))
    by_words = max(12.0, word_count / 2.55)
    by_images = max(12.0, 4.0 + image_count * 2.4)
    return min(70.0, max(by_words, by_images))


def dated_output_dir(base_dir: Path, post_id: str) -> Path:
    run_date = datetime.now().strftime("%Y-%m-%d")
    path = base_dir / run_date / safe_filename(post_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_silent_audio(path: Path, duration: float, sample_rate: int = 44100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration * sample_rate)
    chunk = b"\x00\x00" * min(sample_rate, max(sample_rate, frames))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        remaining = frames
        while remaining > 0:
            frame_count = min(sample_rate, remaining)
            wav.writeframes(chunk[: frame_count * 2])
            remaining -= frame_count


def generate_fpt_tts(text: str, output_path: Path, api_key: str, voice: str, speed: str) -> None:
    if not api_key:
        raise RuntimeError("FPT_TTS_API_KEY is missing")
    text = text[:5000]
    response = requests.post(
        FPT_TTS_URL,
        headers={
            "api_key": api_key,
            "voice": voice,
            "speed": str(speed),
            "format": "mp3",
            "Cache-Control": "no-cache",
        },
        data=text.encode("utf-8"),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("error", -1)) != 0:
        raise RuntimeError(payload.get("message") or f"FPT TTS error: {payload}")

    audio_url = payload.get("async")
    if not audio_url:
        raise RuntimeError(f"FPT TTS response missing async URL: {payload}")

    last_error = None
    for _ in range(18):
        time.sleep(4)
        audio_response = requests.get(audio_url, timeout=30)
        content_type = audio_response.headers.get("content-type", "").lower()
        if audio_response.status_code == 200 and audio_response.content and ("audio" in content_type or len(audio_response.content) > 1024):
            output_path.write_bytes(audio_response.content)
            return
        last_error = f"status={audio_response.status_code} content_type={content_type} bytes={len(audio_response.content)}"

    raise RuntimeError(f"FPT TTS audio not ready after polling. Last response: {last_error}")


def generate_windows_sapi(text: str, output_path: Path) -> None:
    text_path = output_path.with_suffix(".txt")
    script_path = output_path.with_suffix(".ps1")
    text_path.write_text(text, encoding="utf-8")
    script_path.write_text(
        """
param(
  [Parameter(Mandatory=$true)][string]$TextPath,
  [Parameter(Mandatory=$true)][string]$OutputPath
)
Add-Type -AssemblyName System.Speech
$ErrorActionPreference = 'Stop'
$text = Get-Content -LiteralPath $TextPath -Raw -Encoding UTF8
$voiceName = $env:SAPI_VOICE
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($voiceName) { $speaker.SelectVoice($voiceName) }
$speaker.Rate = 1
$speaker.Volume = 100
$speaker.SetOutputToWaveFile($OutputPath)
$speaker.Speak($text)
$speaker.Dispose()
""".strip(),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-TextPath",
            str(text_path),
            "-OutputPath",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Windows SAPI failed").strip())
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Windows SAPI did not create an audio file.")


async def generate_edge_tts(text: str, output_path: Path, voice: str) -> None:
    if edge_tts is None:
        raise RuntimeError("edge-tts is not installed")
    await edge_tts.Communicate(text=text, voice=voice).save(str(output_path))


def generate_audio(text: str, output_path: Path, fallback_duration: float, voice: str, fpt_api_key: str, fpt_voice: str, fpt_speed: str) -> tuple[Path, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        generate_fpt_tts(text, output_path, fpt_api_key, fpt_voice, fpt_speed)
        return output_path, f"tts=fpt voice={fpt_voice}"
    except Exception as exc:
        log(f"FPT TTS failed, trying edge-tts: {exc}")

    try:
        asyncio.run(generate_edge_tts(text, output_path, voice))
        return output_path, f"tts=edge-tts voice={voice}"
    except Exception as exc:
        log(f"edge-tts failed, trying gTTS: {exc}")

    try:
        gTTS(text=text, lang="vi").save(str(output_path))
        return output_path, "tts=gTTS"
    except Exception as exc:
        log(f"gTTS failed, trying Windows SAPI: {exc}")

    try:
        sapi_path = output_path.with_suffix(".wav")
        generate_windows_sapi(text, sapi_path)
        return sapi_path, "tts=windows-sapi"
    except Exception as exc:
        log(f"Windows SAPI failed, using silent fallback: {exc}")

    silent_path = output_path.with_suffix(".wav")
    create_silent_audio(silent_path, fallback_duration)
    return silent_path, "tts=silent-fallback"


def cover_background(background_path: Path, duration: float) -> VideoFileClip | ColorClip:
    if not background_path.exists():
        return ColorClip(size=TARGET_SIZE, color=(18, 18, 18)).with_duration(duration)

    clip = VideoFileClip(str(background_path)).without_audio()
    source_duration = max(0.1, min(duration, clip.duration or duration))
    clip = clip.subclipped(0, source_duration)

    target_w, target_h = TARGET_SIZE
    scale = max(target_w / clip.w, target_h / clip.h)
    clip = clip.resized((math.ceil(clip.w * scale), math.ceil(clip.h * scale)))
    clip = clip.cropped(x_center=clip.w / 2, y_center=clip.h / 2, width=target_w, height=target_h)
    return clip.with_duration(source_duration)


def build_overlay_clips(paths: list[Path], duration: float) -> list[ImageClip]:
    if not paths:
        return []

    clips = []
    sequence = paths[:6]
    segment = max(2.0, duration / len(sequence))
    for index, path in enumerate(sequence):
        start = index * segment
        clip_duration = max(1.0, min(segment + 0.08, duration - start))
        width = 940 if index == 0 else 900
        clip = ImageClip(str(path)).resized(width=width).with_duration(clip_duration).with_start(start)
        y = max(120, int((TARGET_SIZE[1] - clip.h) / 2))
        clips.append(clip.with_position(("center", y)))
    return clips


def write_voice(post_id: str, script: str, audio_dir: Path, temp_dir: Path, voice: str, fpt_api_key: str, fpt_voice: str, fpt_speed: str) -> dict:
    script = clean_script(script)
    if not script:
        raise RuntimeError("Narrator_Script is empty.")

    output_dir = dated_output_dir(audio_dir, post_id)
    temp_post_dir = temp_dir / safe_filename(post_id)
    temp_post_dir.mkdir(parents=True, exist_ok=True)
    fallback_duration = estimate_duration(script, 1)
    audio_path, audio_note = generate_audio(script, output_dir / "narration.mp3", fallback_duration, voice, fpt_api_key, fpt_voice, fpt_speed)
    audio_clip = AudioFileClip(str(audio_path))
    duration = round(float(audio_clip.duration or fallback_duration), 2)
    audio_clip.close()
    return {
        "ID": post_id,
        "Audio_Path": str(audio_path),
        "Status": "In Progress",
        "Note": f"Phase 3A: voice ready duration={duration}s {audio_note}",
    }


def build_visual(post_id: str, screenshots: dict, script: str, background_path: Path, visuals_dir: Path) -> dict:
    images = screenshot_paths(screenshots)
    if not images:
        raise RuntimeError("No screenshot files found for visual build.")

    duration = estimate_duration(clean_script(script), len(images)) + 4.0
    duration = min(75.0, max(12.0, duration))
    output_dir = dated_output_dir(visuals_dir, post_id)
    output_path = output_dir / "visual.mp4"

    background = cover_background(background_path, duration)
    overlay_clips = build_overlay_clips(images, duration)
    final = CompositeVideoClip([background, *overlay_clips], size=TARGET_SIZE).with_duration(duration)
    final.write_videofile(
        str(output_path),
        fps=24,
        codec="libx264",
        audio=False,
        preset="veryfast",
        threads=4,
        logger=None,
    )

    final.close()
    background.close()
    for clip in overlay_clips:
        clip.close()

    return {
        "ID": post_id,
        "Visual_Video_Path": str(output_path),
        "Status": "In Progress",
        "Note": f"Phase 3B: visual ready duration={round(duration, 2)}s images={len(images)} comments={max(0, len(images)-1)}",
    }


def merge_final(post_id: str, audio_path: Path, visual_path: Path, videos_dir: Path) -> dict:
    if not audio_path.exists():
        raise RuntimeError(f"Audio file not found: {audio_path}")
    if not visual_path.exists():
        raise RuntimeError(f"Visual video not found: {visual_path}")

    output_dir = dated_output_dir(videos_dir, post_id)
    output_path = output_dir / "final.mp4"

    audio_clip = AudioFileClip(str(audio_path))
    visual_clip = VideoFileClip(str(visual_path)).without_audio()
    duration = min(float(audio_clip.duration or 0), float(visual_clip.duration or 0))
    duration = max(1.0, duration - 0.15)

    final = visual_clip.subclipped(0, duration).with_audio(audio_clip.subclipped(0, duration))
    final.write_videofile(
        str(output_path),
        fps=24,
        codec="libx264",
        audio_codec="aac",
        preset="veryfast",
        threads=4,
        logger=None,
    )

    final.close()
    audio_clip.close()
    visual_clip.close()

    return {
        "ID": post_id,
        "Video_Path": str(output_path),
        "Status": "Done",
        "Note": f"Phase 3C: final merged duration={round(duration, 2)}s",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split phase 3 video pipeline.")
    parser.add_argument("--mode", required=True, choices=["voice", "visual", "merge"])
    parser.add_argument("--id", required=True)
    parser.add_argument("--screenshots")
    parser.add_argument("--script")
    parser.add_argument("--audio-path")
    parser.add_argument("--visual-path")
    parser.add_argument("--background", default=os.getenv("BACKGROUND_VIDEO_PATH", DEFAULT_BACKGROUND))
    parser.add_argument("--audio-dir", default=os.getenv("AUDIO_DIR", DEFAULT_AUDIO_DIR))
    parser.add_argument("--visuals-dir", default=os.getenv("VISUALS_DIR", DEFAULT_VISUALS_DIR))
    parser.add_argument("--videos-dir", default=os.getenv("VIDEOS_DIR", DEFAULT_VIDEOS_DIR))
    parser.add_argument("--temp-dir", default=os.getenv("TEMP_DIR", DEFAULT_TEMP_DIR))
    parser.add_argument("--voice", default=os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE))
    parser.add_argument("--fpt-api-key", default=os.getenv("FPT_TTS_API_KEY", ""))
    parser.add_argument("--fpt-voice", default=os.getenv("FPT_TTS_VOICE", "banmai"))
    parser.add_argument("--fpt-speed", default=os.getenv("FPT_TTS_SPEED", "0"))
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()

    try:
        if args.mode == "voice":
            if not args.script:
                raise RuntimeError("--script is required for mode=voice")
            result = write_voice(
                post_id=args.id,
                script=args.script,
                audio_dir=resolve_path(args.audio_dir),
                temp_dir=resolve_path(args.temp_dir),
                voice=args.voice,
                fpt_api_key=args.fpt_api_key,
                fpt_voice=args.fpt_voice,
                fpt_speed=args.fpt_speed,
            )
        elif args.mode == "visual":
            if not args.screenshots:
                raise RuntimeError("--screenshots is required for mode=visual")
            result = build_visual(
                post_id=args.id,
                screenshots=parse_screenshots(args.screenshots),
                script=args.script or "",
                background_path=resolve_path(args.background),
                visuals_dir=resolve_path(args.visuals_dir),
            )
        else:
            if not args.audio_path or not args.visual_path:
                raise RuntimeError("--audio-path and --visual-path are required for mode=merge")
            result = merge_final(
                post_id=args.id,
                audio_path=resolve_path(args.audio_path),
                visual_path=resolve_path(args.visual_path),
                videos_dir=resolve_path(args.videos_dir),
            )
    except Exception as exc:
        log(f"Video factory failed: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
