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
OVERLAY_TOP_RATIO = 0.25


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


def ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def ffprobe_executable() -> str:
    ffmpeg_path = Path(ffmpeg_executable())
    ffprobe_path = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
    if ffprobe_path.exists():
        return str(ffprobe_path)
    return ""


def probe_duration(path: Path) -> float:
    if not path.exists():
        raise RuntimeError(f"Media file not found: {path}")

    ffprobe = ffprobe_executable()
    if ffprobe:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            try:
                return float((result.stdout or "0").strip())
            except ValueError:
                pass

    result = subprocess.run(
        [ffmpeg_executable(), "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stderr or "") + "\n" + (result.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError(f"Could not parse duration for {path}: {(output or '').strip()[:400]}")

    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def clean_script(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:4800]


def clean_caption_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"(^|\s)[#@]\S+", " ", text)
    text = re.sub(r"[\"'`*_~|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_extracted_content(value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def build_caption(script: str = "", extracted_content: str = "") -> str:
    extracted = parse_extracted_content(extracted_content)
    post_text = clean_caption_text(str(extracted.get("post_text") or ""))
    source_text = clean_caption_text(script) or post_text

    if post_text:
        lead = post_text
    elif source_text:
        sentences = re.split(r"(?<=[.!?])\s+", source_text)
        lead = next((clean_caption_text(sentence) for sentence in sentences if clean_caption_text(sentence)), source_text)
    else:
        lead = "Cau chuyen nay dang duoc ban luan tren Threads"

    if len(lead) > 140:
        lead = lead[:140].rsplit(" ", 1)[0].strip()

    caption = (
        f"{lead}\n\n"
        "Ban nghi sao ve cau chuyen nay?\n\n"
        "#threads #chuyencuocsong #tamly #xuhuong"
    )
    return caption[:900].strip()


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


def parse_script_sections(script: str) -> list[str]:
    sections = []
    for line in (script or "").splitlines():
        line = clean_script(line)
        if not line:
            continue
        line = re.sub(r"^(TOPIC|COMMENT\s+\d+)\s*:\s*", "", line, flags=re.IGNORECASE)
        line = clean_script(line)
        if line:
            sections.append(line)
    if not sections:
        sections = [
            clean_script(chunk)
            for chunk in re.split(r"(?<=[.!?…])\s+", clean_script(script))
            if clean_script(chunk)
        ]
    return sections


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


def compute_timeline_durations(text_blocks: list[str], total_duration: float, gap_seconds: float = 0.35, intro_seconds: float = 0.35, outro_seconds: float = 0.35) -> list[tuple[float, float]]:
    if not text_blocks or total_duration <= 0:
        return []

    slot_count = len(text_blocks)
    total_gaps = gap_seconds * max(0, slot_count - 1)
    usable_duration = max(2.0, total_duration - intro_seconds - outro_seconds - total_gaps)

    base_durations = []
    for index, block in enumerate(text_blocks):
        compact_len = len(re.sub(r"\s+", "", block))
        if index == 0:
            base_duration = 2.8 + min(1.6, compact_len / 90)
            base_duration = min(5.0, max(2.8, base_duration))
        else:
            base_duration = 1.8 + min(1.4, compact_len / 120)
            base_duration = min(3.4, max(1.8, base_duration))
        base_durations.append(base_duration)

    durations = list(base_durations)
    base_total = sum(base_durations)

    if base_total < usable_duration:
        extra = usable_duration - base_total
        growth_weights = [0.7] + [1.0] * max(0, slot_count - 1)
        growth_caps = [1.2] + [1.4] * max(0, slot_count - 1)
        while extra > 0.01:
            distributed = 0.0
            active_indexes = [idx for idx in range(slot_count) if durations[idx] < base_durations[idx] + growth_caps[idx] - 0.01]
            if not active_indexes:
                durations[-1] += extra
                break
            weight_sum = sum(growth_weights[idx] for idx in active_indexes) or len(active_indexes)
            for idx in active_indexes:
                allowed = (base_durations[idx] + growth_caps[idx]) - durations[idx]
                share = extra * (growth_weights[idx] / weight_sum)
                delta = min(allowed, share)
                durations[idx] += delta
                distributed += delta
            if distributed <= 0.01:
                durations[-1] += extra
                break
            extra -= distributed
    elif base_total > usable_duration:
        scale = usable_duration / base_total
        for index, value in enumerate(durations):
            min_duration = 2.2 if index == 0 else 1.4
            durations[index] = max(min_duration, value * scale)

        overflow = sum(durations) - usable_duration
        for index in range(slot_count - 1, -1, -1):
            if overflow <= 0.01:
                break
            min_duration = 2.2 if index == 0 else 1.4
            reducible = max(0.0, durations[index] - min_duration)
            delta = min(reducible, overflow)
            durations[index] -= delta
            overflow -= delta

    timeline = []
    cursor = intro_seconds
    for index, slot_duration in enumerate(durations):
        end = min(total_duration - outro_seconds, cursor + slot_duration)
        timeline.append((round(cursor, 3), round(max(cursor + 1.0, end), 3)))
        cursor = end + gap_seconds

    if timeline:
        last_start, _ = timeline[-1]
        timeline[-1] = (last_start, round(max(last_start + 1.0, total_duration - outro_seconds), 3))
    return timeline


def build_overlay_plan(paths: list[Path], text_blocks: list[str], duration: float) -> list[dict]:
    if not paths:
        return []

    sequence = paths[:6]
    text_blocks = (text_blocks or [])[: len(sequence)]
    if len(text_blocks) < len(sequence):
        text_blocks.extend(["..."] * (len(sequence) - len(text_blocks)))
    timeline = compute_timeline_durations(text_blocks, duration)

    plan = []
    for index, path in enumerate(sequence):
        if index >= len(timeline):
            break
        start, end = timeline[index]
        plan.append(
            {
                "path": path,
                "start": round(start, 3),
                "end": round(max(start + 1.0, end), 3),
                "width": TARGET_SIZE[0],
            }
        )
    return plan


def escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_visual_ffmpeg(background_path: Path, output_path: Path, overlays: list[dict], duration: float, fps: int = 20) -> None:
    ffmpeg = ffmpeg_executable()
    target_w, target_h = TARGET_SIZE
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]

    if background_path.exists():
        command.extend(["-stream_loop", "-1", "-i", str(background_path)])
        bg_label = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,trim=duration={duration},setpts=PTS-STARTPTS[base]".format(
            duration=duration
        )
        next_input_index = 1
    else:
        command.extend(["-f", "lavfi", "-i", f"color=c=0x121212:s={target_w}x{target_h}:r={fps}:d={duration}"])
        bg_label = "[0:v]trim=duration={duration},setpts=PTS-STARTPTS[base]".format(duration=duration)
        next_input_index = 1

    filter_parts = [bg_label]
    current_label = "base"
    # Keep the overlay high on screen, but avoid complex nested expressions
    # that break ffmpeg filter parsing across environments.
    overlay_y_expr = f"(H-h)*{OVERLAY_TOP_RATIO}"

    for index, item in enumerate(overlays):
        command.extend(["-loop", "1", "-i", str(item["path"])])
        input_label = f"{next_input_index}:v"
        overlay_label = f"ov{index}"
        output_label = f"v{index}"
        width = int(item["width"])
        start = float(item["start"])
        end = float(item["end"])
        filter_parts.append(
            f"[{input_label}]scale={width}:-2[{overlay_label}]"
        )
        filter_parts.append(
            f"[{current_label}][{overlay_label}]overlay=(W-w)/2:{overlay_y_expr}:enable='between(t,{start},{end})'[{output_label}]"
        )
        current_label = output_label
        next_input_index += 1

    filter_complex = ";".join(filter_parts)
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{current_label}]",
            "-t",
            str(duration),
            "-r",
            str(fps),
            "-an",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg visual build failed").strip())


def write_voice(post_id: str, script: str, audio_dir: Path, temp_dir: Path, voice: str, fpt_api_key: str, fpt_voice: str, fpt_speed: str) -> dict:
    script = clean_script(script)
    if not script:
        raise RuntimeError("Narrator_Script is empty.")

    output_dir = dated_output_dir(audio_dir, post_id)
    temp_post_dir = temp_dir / safe_filename(post_id)
    temp_post_dir.mkdir(parents=True, exist_ok=True)
    fallback_duration = estimate_duration(script, 1)
    audio_path, audio_note = generate_audio(script, output_dir / "narration.mp3", fallback_duration, voice, fpt_api_key, fpt_voice, fpt_speed)
    duration = round(probe_duration(audio_path), 2)
    return {
        "ID": post_id,
        "Audio_Path": str(audio_path),
        "Status": "In Progress",
        "Note": f"Phase 3A: voice ready duration={duration}s {audio_note}",
    }


def build_visual(post_id: str, screenshots: dict, script: str, background_path: Path, visuals_dir: Path, audio_path: Path | None = None) -> dict:
    images = screenshot_paths(screenshots)
    if not images:
        raise RuntimeError("No screenshot files found for visual build.")

    text_blocks = parse_script_sections(script)
    if audio_path and audio_path.exists():
        duration = min(75.0, max(8.0, probe_duration(audio_path) + 0.8))
    else:
        duration = estimate_duration(clean_script(script), len(images)) + 2.0
        duration = min(75.0, max(10.0, duration))

    output_dir = dated_output_dir(visuals_dir, post_id)
    output_path = output_dir / "visual.mp4"
    overlay_plan = build_overlay_plan(images, text_blocks, duration)
    if not overlay_plan:
        raise RuntimeError("No overlay plan generated for visual build.")
    build_visual_ffmpeg(background_path, output_path, overlay_plan, duration, fps=20)

    return {
        "ID": post_id,
        "Visual_Video_Path": str(output_path),
        "Status": "In Progress",
        "Note": f"Phase 3B: visual ready duration={round(duration, 2)}s images={len(images)} comments={max(0, len(images)-1)}",
    }


def merge_final(post_id: str, audio_path: Path, visual_path: Path, videos_dir: Path, script: str = "", extracted_content: str = "") -> dict:
    if not audio_path.exists():
        raise RuntimeError(f"Audio file not found: {audio_path}")
    if not visual_path.exists():
        raise RuntimeError(f"Visual video not found: {visual_path}")

    output_dir = dated_output_dir(videos_dir, post_id)
    output_path = output_dir / "final.mp4"
    audio_duration = probe_duration(audio_path)
    visual_duration = probe_duration(visual_path)
    duration = max(1.0, min(audio_duration, visual_duration) - 0.15)

    command = [
        ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(visual_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-t",
        str(duration),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg merge failed").strip())

    return {
        "ID": post_id,
        "Video_Path": str(output_path),
        "Caption": build_caption(script=script, extracted_content=extracted_content),
        "Status": "Draft",
        "Note": f"Phase 3C: draft ready duration={round(duration, 2)}s",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split phase 3 video pipeline.")
    parser.add_argument("--mode", required=True, choices=["voice", "visual", "merge"])
    parser.add_argument("--id", required=True)
    parser.add_argument("--screenshots")
    parser.add_argument("--script")
    parser.add_argument("--extracted-content")
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
    args.id = decode_cli_text(args.id)
    args.screenshots = decode_cli_text(args.screenshots)
    args.script = decode_cli_text(args.script)
    args.extracted_content = decode_cli_text(args.extracted_content)
    args.audio_path = decode_cli_text(args.audio_path)
    args.visual_path = decode_cli_text(args.visual_path)

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
                audio_path=resolve_path(args.audio_path) if args.audio_path else None,
            )
        else:
            if not args.audio_path or not args.visual_path:
                raise RuntimeError("--audio-path and --visual-path are required for mode=merge")
            result = merge_final(
                post_id=args.id,
                audio_path=resolve_path(args.audio_path),
                visual_path=resolve_path(args.visual_path),
                videos_dir=resolve_path(args.videos_dir),
                script=args.script or "",
                extracted_content=args.extracted_content or "",
            )
    except Exception as exc:
        log(f"Video factory failed: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
