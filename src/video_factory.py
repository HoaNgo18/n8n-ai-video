"""
Phase 3: split video pipeline.

Modes:
    voice  -> generate narration audio from Narrator_Script
    visual -> build silent background+screenshots video
    merge  -> mux Audio_Path + Visual_Video_Path into final MP4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageChops, ImageDraw, ImageFilter


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent if Path(__file__).resolve().parent.name == "src" else Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=True)
TARGET_SIZE = (
    int(os.getenv("VIDEO_WIDTH", "1080")),
    int(os.getenv("VIDEO_HEIGHT", "1920")),
)
DEFAULT_BACKGROUND = "runtime/assets/background.mp4"
DEFAULT_BACKGROUND_DIR = "runtime/assets/backgrounds"
DEFAULT_AUDIO_DIR = "runtime/data/audio"
DEFAULT_VISUALS_DIR = "runtime/data/visuals"
DEFAULT_VIDEOS_DIR = "runtime/data/videos"
DEFAULT_TEMP_DIR = "runtime/data/temp"
DEFAULT_BACKGROUND_MUSIC_DIR = "runtime/assets/music/lofi"
DEFAULT_TTS_VOICE = ""
DEFAULT_DISCUSSION_VOICES: list[str] = []
DEFAULT_AUTHOR_VOICES: list[str] = []
DEFAULT_TTS_ENGINE_ORDER = "vieneu"
OVERLAY_TOP_RATIO = float(os.getenv("OVERLAY_TOP_RATIO", "0.24"))
OVERLAY_WIDTH_RATIO = max(0.55, min(1.0, float(os.getenv("OVERLAY_WIDTH_RATIO", "0.98"))))
OVERLAY_CORNER_RADIUS_RATIO = max(
    0.01,
    min(0.12, float(os.getenv("OVERLAY_CORNER_RADIUS_RATIO", "0.035"))),
)
OVERLAY_SHADOW_OPACITY = max(0, min(255, int(os.getenv("OVERLAY_SHADOW_OPACITY", "105"))))
OVERLAY_ANIMATION = os.getenv("OVERLAY_ANIMATION", "fade_slide").strip().lower() or "fade_slide"
OVERLAY_ANIMATION_SECONDS = max(0.0, min(1.2, float(os.getenv("OVERLAY_ANIMATION_SECONDS", "0.35"))))
OVERLAY_SLIDE_PIXELS = max(0, min(180, int(os.getenv("OVERLAY_SLIDE_PIXELS", "56"))))
VIDEO_ENCODE_PRESET = os.getenv("VIDEO_ENCODE_PRESET", "medium").strip() or "medium"
VIDEO_CRF = os.getenv("VIDEO_CRF", "16").strip() or "16"
VIDEO_TARGET_BITRATE = os.getenv("VIDEO_TARGET_BITRATE", "14M").strip() or "14M"
VIDEO_MAXRATE = os.getenv("VIDEO_MAXRATE", "18M").strip() or "18M"
VIDEO_BUFSIZE = os.getenv("VIDEO_BUFSIZE", "36M").strip() or "36M"
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192k").strip() or "192k"
BACKGROUND_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
BACKGROUND_VIDEO_PICK = os.getenv("BACKGROUND_VIDEO_PICK", "hash").strip().lower() or "hash"
BACKGROUND_MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
BACKGROUND_MUSIC_ENABLED = str(os.getenv("BACKGROUND_MUSIC_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}
BACKGROUND_MUSIC_DIR = os.getenv("BACKGROUND_MUSIC_DIR", DEFAULT_BACKGROUND_MUSIC_DIR).strip() or DEFAULT_BACKGROUND_MUSIC_DIR
BACKGROUND_MUSIC_PATH = os.getenv("BACKGROUND_MUSIC_PATH", "").strip()
BACKGROUND_MUSIC_PICK = os.getenv("BACKGROUND_MUSIC_PICK", "hash").strip().lower() or "hash"
BACKGROUND_MUSIC_VOLUME = max(0.0, min(1.0, float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0.08"))))
BACKGROUND_MUSIC_FADE_SECONDS = max(0.0, min(6.0, float(os.getenv("BACKGROUND_MUSIC_FADE_SECONDS", "1.5"))))
BACKGROUND_MUSIC_START_OFFSET_SECONDS = max(0.0, float(os.getenv("BACKGROUND_MUSIC_START_OFFSET_SECONDS", "0.0")))
BACKGROUND_MUSIC_DUCKING = str(os.getenv("BACKGROUND_MUSIC_DUCKING", "true")).strip().lower() in {"1", "true", "yes"}
MAX_OVERLAY_IMAGES = 40
VISUAL_TIMING_LEAD_SECONDS = float(os.getenv("VISUAL_TIMING_LEAD_SECONDS", "0.0"))
AUDIO_TRIM_SEGMENT_SILENCE = str(os.getenv("AUDIO_TRIM_SEGMENT_SILENCE", "false")).strip().lower() in {"1", "true", "yes"}
AUDIO_SILENCE_THRESHOLD_DB = os.getenv("AUDIO_SILENCE_THRESHOLD_DB", "-45dB")
AUDIO_LEADING_SILENCE_SECONDS = float(os.getenv("AUDIO_LEADING_SILENCE_SECONDS", "0.08"))
AUDIO_TRAILING_SILENCE_SECONDS = float(os.getenv("AUDIO_TRAILING_SILENCE_SECONDS", "0.18"))
AUDIO_SEGMENT_OVERLAP_SECONDS = max(0.0, float(os.getenv("AUDIO_SEGMENT_OVERLAP_SECONDS", "0.15")))
AUDIO_MAX_SEGMENTS = max(0, int(os.getenv("AUDIO_MAX_SEGMENTS", "7")))
AUDIO_MAX_POST_CHARS = max(80, int(os.getenv("AUDIO_MAX_POST_CHARS", "220")))
AUDIO_MAX_COMMENT_CHARS = max(80, int(os.getenv("AUDIO_MAX_COMMENT_CHARS", "260")))
VIENEU_TTS_ENABLED = str(os.getenv("VIENEU_TTS_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}
VIENEU_MODE = os.getenv("VIENEU_MODE", "standard").strip() or "standard"
VIENEU_BACKBONE_REPO = os.getenv("VIENEU_BACKBONE_REPO", "").strip()
VIENEU_BACKBONE_DEVICE = os.getenv("VIENEU_BACKBONE_DEVICE", "").strip()
VIENEU_CODEC_REPO = os.getenv("VIENEU_CODEC_REPO", "").strip()
VIENEU_CODEC_DEVICE = os.getenv("VIENEU_CODEC_DEVICE", "").strip()
VIENEU_VOICE_REF = os.getenv("VIENEU_VOICE_REF", "").strip()
VIENEU_VOICE_REF_TEXT = os.getenv("VIENEU_VOICE_REF_TEXT", "").strip()
_VIENEU_CLIENT = None
_VIENEU_VOICE = None
_VIENEU_VOICE_CACHE: dict[str, object | None] = {}
VIENEU_REFERENCE_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}


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


def tts_engine_order() -> list[str]:
    raw = os.getenv("TTS_ENGINE_ORDER", DEFAULT_TTS_ENGINE_ORDER)
    for item in str(raw or "").split(","):
        engine = item.strip().lower()
        if not engine:
            continue
        if engine != "vieneu":
            log(f"Ignoring non-VieNeu TTS engine in TTS_ENGINE_ORDER: {engine}")
            continue
        return ["vieneu"]
    return ["vieneu"]


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


def list_background_videos(background_dir: Path) -> list[Path]:
    if not background_dir.exists() or not background_dir.is_dir():
        return []
    return sorted(
        path
        for path in background_dir.iterdir()
        if path.is_file() and path.suffix.lower() in BACKGROUND_VIDEO_EXTENSIONS
    )


def choose_background_video(post_id: str, background_path: Path, background_dir: Path | None = None) -> Path:
    candidates = list_background_videos(background_dir) if background_dir else []
    if not candidates:
        return background_path

    strategy = BACKGROUND_VIDEO_PICK
    if strategy == "first":
        return candidates[0]
    if strategy == "random":
        import random

        return random.choice(candidates)

    digest = hashlib.sha256(str(post_id or "").encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(candidates)
    return candidates[index]


def list_background_music(music_dir: Path) -> list[Path]:
    if not music_dir.exists() or not music_dir.is_dir():
        return []
    return sorted(
        path
        for path in music_dir.iterdir()
        if path.is_file() and path.suffix.lower() in BACKGROUND_MUSIC_EXTENSIONS
    )


def choose_background_music(post_id: str) -> Path | None:
    if not BACKGROUND_MUSIC_ENABLED:
        return None

    if BACKGROUND_MUSIC_PATH:
        music_path = resolve_path(BACKGROUND_MUSIC_PATH)
        if not music_path.exists():
            raise RuntimeError(f"BACKGROUND_MUSIC_PATH does not exist: {music_path}")
        return music_path

    music_dir = resolve_path(BACKGROUND_MUSIC_DIR)
    tracks = list_background_music(music_dir)
    if not tracks:
        raise RuntimeError(f"BACKGROUND_MUSIC_DIR has no supported audio files: {music_dir}")

    strategy = BACKGROUND_MUSIC_PICK
    if strategy == "first":
        return tracks[0]
    if strategy == "random":
        import random

        return random.choice(tracks)

    digest = hashlib.sha256(str(post_id or "").encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(tracks)
    return tracks[index]


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


def normalize_tts_shorthand(text: str) -> str:
    text = str(text or "")
    replacements = [
        (r"\bbọn\s+tớ\b", "bọn tao"),
        (r"\btụi\s+tớ\b", "tụi tao"),
        (r"\bbọn\s+t\b", "bọn tao"),
        (r"\btụi\s+t\b", "tụi tao"),
        (r"\bbon\s+t\b", "bon tao"),
        (r"\btớ\b", "tao"),
        (r"\bcmay\b", "chúng mày"),
        (r"\bcm\b", "chúng mày"),
        (r"\btui\b", "tôi"),
        (r"\bt\b", "tao"),
        (r"\bm\b", "mày"),
        (r"\bmng\b", "mọi người"),
        (r"\bmn\b", "mọi người"),
        (r"\bnma\b", "nhưng mà"),
        (r"\bko\b", "không"),
        (r"\bkh\b", "không"),
        (r"\bk\b", "không"),
        (r"\br\b", "rồi"),
        (r"\bny\b", "người yêu"),
        (r"\bđt\b", "điện thoại"),
        (r"\bdt\b", "điện thoại"),
        (r"\bđvi\b", "định vị"),
        (r"\bdvi\b", "định vị"),
        (r"\blsao\b", "làm sao"),
        (r"\bsđt\b", "số điện thoại"),
        (r"\bsdt\b", "số điện thoại"),
        (r"\bgg\s+map\b", "Google Maps"),
        (r"\bvs\b", "với"),
        (r"\bv\b", "vậy"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def clean_tts_segment_text(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"^(?:Pinned\s+)?@?[A-Za-z0-9_.-]{3,}\s+\d+\s*[smhdw]\b\s*",
        " ",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(
        r"^@?[A-Za-z0-9_.-]{3,}\s+\(?\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)?[\s:.,;-]*",
        " ",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"^@?[A-Za-z0-9.]*_[A-Za-z0-9_.-]*\b[\s:.,;-]*", " ", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^\(?\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)?[\s:.,;-]*", " ", text)
    text = re.sub(r"(?<!\d)\(\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)(?!\d)", " ", text)
    text = re.sub(r"\bTranslate\b\s*\(?\s*[1-9]\d?\s*(?:/\s*[1-9]\d?)?\s*\)?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:Reply|Like|Share|Repost|View activity|Top)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\s+\d+(?:[.,]\d+)?[KMkm]?){1,4}\s*$", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_leading_author_label(text: str, *author_values: object) -> str:
    cleaned = str(text or "").strip()
    candidates = []
    for value in author_values:
        raw = str(value or "").strip()
        if not raw:
            continue
        candidates.extend(
            [
                raw,
                raw.lstrip("@"),
                raw.replace("@", ""),
                raw.replace("_", " "),
            ]
        )

    for candidate in sorted({item for item in candidates if item}, key=len, reverse=True):
        escaped = re.escape(candidate)
        cleaned = re.sub(rf"^@?{escaped}\b[\s:.,;-]*", " ", cleaned, flags=re.IGNORECASE).strip()

    return cleaned


def clean_script(text: str) -> str:
    text = clean_tts_segment_text(text)
    text = normalize_tts_shorthand(text)
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:4800]


def normalize_line_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_json_script_candidate(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    candidates = [raw]
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    if fenced_match:
        candidates.append(fenced_match.group(1).strip())

    brace_matches = re.findall(r"\{[\s\S]*?\}", raw)
    candidates.extend(brace_matches)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            script = parsed.get("script") or parsed.get("narrator_script")
            if isinstance(script, str) and script.strip():
                return script.strip()
        if isinstance(parsed, str) and parsed.strip():
            return parsed.strip()

    return ""


def build_segment_script(extracted_content: str | None) -> str:
    segments = parse_extracted_segments(extracted_content)
    if not segments:
        return ""
    lines = []
    seen = set()
    for item in segments:
        text = normalize_line_text(str(item.get("text") or ""))
        key = re.sub(r"[^0-9a-z\u00c0-\u1ef9]+", "", text.lower())
        if not text or not key or key in seen:
            continue
        seen.add(key)
        lines.append(text)
    return "\n".join(lines)[:8000]


def normalize_narration_script(script: str, extracted_content: str = "") -> str:
    raw = str(script or "").replace("\r", "").strip()
    parsed_script = extract_json_script_candidate(raw)
    base_text = parsed_script or raw

    lines = []
    seen = set()
    for line in base_text.splitlines():
        cleaned = normalize_line_text(line)
        if not cleaned:
            continue
        key = re.sub(r"[^0-9a-z\u00c0-\u1ef9]+", "", cleaned.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(cleaned)

    normalized = "\n".join(lines).strip()
    segment_script = build_segment_script(extracted_content)

    suspicious_json_leak = raw.lstrip().startswith("{") and "\"script\"" in raw[:80]
    has_ui_noise = bool(re.search(r"\bto\s+from[_a-z0-9.]+", raw, flags=re.IGNORECASE))
    duplicate_heavy = len(lines) >= 2 and len(lines) != len(set(lines))
    if segment_script and (suspicious_json_leak or has_ui_noise or not normalized or duplicate_heavy):
        return segment_script[:8000]

    return normalized[:8000]


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


def parse_extracted_segments(extracted_content: str | None) -> list[dict]:
    extracted = parse_extracted_content(extracted_content)
    segments = extracted.get("segments") or []
    if not isinstance(segments, list):
        return []

    parsed_segments = []
    for index, item in enumerate(segments):
        if not isinstance(item, dict):
            continue
        raw_text = strip_leading_author_label(
            str(item.get("text") or ""),
            item.get("author_name", ""),
            item.get("author_key", ""),
        )
        text = clean_script(raw_text)
        if not text:
            continue
        try:
            image_index = int(item.get("image_index", index))
        except (TypeError, ValueError):
            image_index = index
        parsed_segments.append(
            {
                "type": str(item.get("type") or "segment"),
                "text": text,
                "image_index": image_index,
                "author_name": str(item.get("author_name") or ""),
                "author_key": str(item.get("author_key") or ""),
            }
        )
    return parsed_segments


def parse_content_mode(extracted_content: str | None) -> str:
    extracted = parse_extracted_content(extracted_content)
    return str(extracted.get("content_mode") or extracted.get("capture_mode") or "general").strip().lower() or "general"


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


def style_screenshot_card(source_path: Path, output_path: Path) -> Path:
    with Image.open(source_path) as source:
        image = source.convert("RGBA")

    width, height = image.size
    radius = max(8, round(min(width, height) * OVERLAY_CORNER_RADIUS_RATIO))
    padding = max(12, round(width * 0.025))
    shadow_offset = max(4, round(padding * 0.3))
    shadow_blur = max(8, round(padding * 0.75))

    rounded_mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(rounded_mask).rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=255,
    )
    original_alpha = image.getchannel("A")
    image.putalpha(ImageChops.multiply(original_alpha, rounded_mask))

    canvas_size = (width + padding * 2, height + padding * 2)
    shadow = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    shadow_mask = Image.new("L", canvas_size, 0)
    ImageDraw.Draw(shadow_mask).rounded_rectangle(
        (
            padding,
            padding + shadow_offset,
            padding + width - 1,
            padding + shadow_offset + height - 1,
        ),
        radius=radius,
        fill=OVERLAY_SHADOW_OPACITY,
    )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(shadow_blur))
    shadow.putalpha(shadow_mask)

    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(image, (padding, padding))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path


def prepare_overlay_images(paths: list[Path], output_dir: Path) -> list[Path]:
    prepared = []
    for index, path in enumerate(paths):
        output_path = output_dir / f"overlay_{index:02d}.png"
        prepared.append(style_screenshot_card(path, output_path))
    return prepared


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


def split_script_for_extracted_segments(script: str, extracted_segments: list[dict]) -> list[str]:
    if not extracted_segments:
        return parse_script_sections(script)

    target_count = len(extracted_segments)
    sentences = [
        clean_script(chunk)
        for chunk in re.split(r"(?<=[.!?])\s+", clean_script(script))
        if clean_script(chunk)
    ]
    if not sentences:
        return parse_script_sections(script)
    if len(sentences) <= target_count:
        chunks = sentences[:]
        while len(chunks) < target_count:
            chunks.append("")
        return chunks[:target_count]

    weights = [max(1, len(clean_script(str(item.get("text") or "")))) for item in extracted_segments]
    total_weight = sum(weights) or target_count
    total_chars = sum(len(sentence) for sentence in sentences)
    target_chars = [max(80, total_chars * weight / total_weight) for weight in weights]

    chunks: list[str] = []
    current_sentences: list[str] = []
    current_len = 0
    segment_index = 0
    for sentence in sentences:
        remaining_sentences = len(sentences) - sum(len(chunk.split(". ")) for chunk in chunks) - len(current_sentences)
        remaining_slots = target_count - segment_index
        should_close = (
            current_sentences
            and current_len >= target_chars[min(segment_index, len(target_chars) - 1)]
            and remaining_slots > 1
            and remaining_sentences >= remaining_slots
        )
        if should_close:
            chunks.append(clean_script(" ".join(current_sentences)))
            current_sentences = []
            current_len = 0
            segment_index += 1
        current_sentences.append(sentence)
        current_len += len(sentence)

    if current_sentences:
        chunks.append(clean_script(" ".join(current_sentences)))
    while len(chunks) < target_count:
        chunks.append("")
    return chunks[:target_count]


def trim_audio_segment_text(text: str, max_chars: int) -> str:
    text = clean_script(text)
    if len(text) <= max_chars:
        return text

    clipped = text[:max_chars].strip()
    breakpoints = [
        clipped.rfind(". "),
        clipped.rfind("! "),
        clipped.rfind("? "),
        clipped.rfind(", "),
        clipped.rfind("; "),
        clipped.rfind(" "),
    ]
    cut_at = max(breakpoints)
    if cut_at >= 80:
        clipped = clipped[:cut_at].strip()
    return clipped.rstrip(".,;:!?") + "."


def apply_audio_segment_limits(segments: list[dict]) -> list[dict]:
    limited: list[dict] = []
    for item in segments:
        segment = dict(item)
        segment_type = str(segment.get("type") or "").strip().lower()
        max_chars = AUDIO_MAX_POST_CHARS if segment_type in {"post", "continuation"} else AUDIO_MAX_COMMENT_CHARS
        segment["text"] = trim_audio_segment_text(str(segment.get("text") or ""), max_chars)
        if clean_script(str(segment.get("text") or "")):
            limited.append(segment)

    if AUDIO_MAX_SEGMENTS > 0:
        limited = limited[:AUDIO_MAX_SEGMENTS]
    return limited


def select_overlay_text_blocks(
    images: list[Path],
    script: str,
    extracted_content: str = "",
) -> list[str]:
    segments = parse_extracted_segments(extracted_content)
    if segments:
        segment_text_by_image = {int(item["image_index"]): str(item["text"]) for item in segments}
        blocks = []
        for image_index in range(len(images)):
            text = clean_script(segment_text_by_image.get(image_index, ""))
            if text:
                blocks.append(text)
        if blocks:
            while len(blocks) < len(images):
                blocks.append(blocks[-1])
            return blocks[: len(images)]

    text_blocks = parse_script_sections(script)
    if not text_blocks:
        return ["..."] * len(images)
    while len(text_blocks) < len(images):
        text_blocks.append(text_blocks[-1])
    return text_blocks[: len(images)]


def select_audio_segments(script: str, extracted_content: str = "") -> list[dict]:
    script_sections = parse_script_sections(script)
    extracted_segments = parse_extracted_segments(extracted_content)
    content_mode = parse_content_mode(extracted_content)
    segment_source = str(os.getenv("AUDIO_SEGMENT_SOURCE", "extracted")).strip().lower()

    if extracted_segments and (segment_source == "extracted" or content_mode == "story"):
        return apply_audio_segment_limits(remove_embedded_later_segments(extracted_segments))

    if not script_sections:
        return []

    if extracted_segments:
        if len(script_sections) < len(extracted_segments):
            script_sections = split_script_for_extracted_segments(script, extracted_segments)
        paired_segments = []
        for index, text in enumerate(script_sections):
            matched_segment = extracted_segments[index] if index < len(extracted_segments) else extracted_segments[-1]
            if not text:
                text = matched_segment.get("text", "")
            paired_segments.append(
                {
                    "type": matched_segment["type"],
                    "text": text,
                    "image_index": matched_segment.get("image_index", index),
                    "author_name": matched_segment.get("author_name", ""),
                    "author_key": matched_segment.get("author_key", ""),
                }
            )
        return apply_audio_segment_limits(remove_embedded_later_segments(paired_segments))

    return apply_audio_segment_limits([{"type": "segment", "text": text} for text in script_sections])


def overlap_key(text: str) -> str:
    return re.sub(r"[^0-9a-z\u00c0-\u1ef9]+", "", clean_script(text).lower())


def remove_text_fragment(text: str, fragment: str) -> str:
    text = clean_script(text)
    fragment = clean_script(fragment)
    if not text or not fragment:
        return text

    escaped_fragment = re.escape(fragment)
    cleaned = re.sub(escaped_fragment, " ", text, count=1, flags=re.IGNORECASE)
    if cleaned != text:
        return clean_script(cleaned)

    fragment_sentences = [
        clean_script(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+", fragment)
        if len(clean_script(sentence)) >= 12
    ]
    for sentence in sorted(fragment_sentences, key=len, reverse=True):
        cleaned = re.sub(re.escape(sentence), " ", cleaned, count=1, flags=re.IGNORECASE)

    return clean_script(cleaned)


def remove_embedded_later_segments(segments: list[dict]) -> list[dict]:
    cleaned_segments = [dict(item) for item in segments]
    later_items = [
        (index, overlap_key(str(item.get("text") or "")), str(item.get("text") or ""))
        for index, item in enumerate(cleaned_segments)
        if len(overlap_key(str(item.get("text") or ""))) >= 24
    ]

    for later_index, later_key, later_text in later_items:
        later_type = str(cleaned_segments[later_index].get("type") or "").lower()
        for earlier_index in range(later_index):
            earlier = cleaned_segments[earlier_index]
            earlier_type = str(earlier.get("type") or "").lower()
            if later_type == "comment" and earlier_type == "comment":
                continue
            earlier_key = overlap_key(str(earlier.get("text") or ""))
            if later_key and later_key in earlier_key:
                trimmed = remove_text_fragment(str(earlier.get("text") or ""), later_text)
                if trimmed and overlap_key(trimmed) != earlier_key:
                    earlier["text"] = trimmed

    return [item for item in cleaned_segments if clean_script(str(item.get("text") or ""))]


def dated_output_dir(base_dir: Path, post_id: str) -> Path:
    run_date = datetime.now().strftime("%Y-%m-%d")
    path = base_dir / run_date / safe_filename(post_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def split_tts_chunks(text: str, max_chars: int = 850) -> list[str]:
    text = clean_script(text)
    if len(text) <= max_chars:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", text) if item.strip()]
    for sentence in sentences:
        if len(sentence) > max_chars:
            words = sentence.split()
            for word in words:
                candidate = f"{current} {word}".strip()
                if len(candidate) > max_chars and current:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            continue

        candidate = f"{current} {sentence}".strip()
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def get_vieneu_client():
    global _VIENEU_CLIENT
    if _VIENEU_CLIENT is not None:
        return _VIENEU_CLIENT

    try:
        from vieneu import Vieneu
    except ImportError as exc:
        raise RuntimeError("vieneu is not installed") from exc

    kwargs = {}
    if VIENEU_BACKBONE_REPO:
        kwargs["backbone_repo"] = VIENEU_BACKBONE_REPO
    if VIENEU_BACKBONE_DEVICE:
        kwargs["backbone_device"] = VIENEU_BACKBONE_DEVICE
    if VIENEU_CODEC_REPO:
        kwargs["codec_repo"] = VIENEU_CODEC_REPO
    if VIENEU_CODEC_DEVICE:
        kwargs["codec_device"] = VIENEU_CODEC_DEVICE

    _VIENEU_CLIENT = Vieneu(mode=VIENEU_MODE, **kwargs)
    return _VIENEU_CLIENT


def get_vieneu_voice(client):
    global _VIENEU_VOICE
    if _VIENEU_VOICE is not None:
        return _VIENEU_VOICE

    if not VIENEU_VOICE_REF:
        return None

    ref_audio = Path(VIENEU_VOICE_REF)
    if not ref_audio.is_absolute():
        ref_audio = PROJECT_ROOT / ref_audio
    if not ref_audio.exists():
        raise RuntimeError(f"VIENEU_VOICE_REF does not exist: {ref_audio}")

    _VIENEU_VOICE = client.encode_reference(str(ref_audio))
    return _VIENEU_VOICE


def get_vieneu_voice_by_spec(client, voice_spec: str = ""):
    spec = str(voice_spec or "").strip()
    if not spec:
        return get_vieneu_voice(client)

    cache_key = spec
    if cache_key in _VIENEU_VOICE_CACHE:
        return _VIENEU_VOICE_CACHE[cache_key]

    lowered = spec.lower()
    if lowered in {"default", "vieneu", "auto"}:
        voice = get_vieneu_voice(client)
        _VIENEU_VOICE_CACHE[cache_key] = voice
        return voice

    if lowered.startswith("preset:"):
        preset_id = spec.split(":", 1)[1].strip()
        if not preset_id:
            raise RuntimeError("VieNeu preset voice spec is missing preset id")
        if not hasattr(client, "get_preset_voice"):
            raise RuntimeError("Installed VieNeu version does not support preset voices")
        voice = client.get_preset_voice(preset_id)
        _VIENEU_VOICE_CACHE[cache_key] = voice
        return voice

    if lowered.startswith("ref:"):
        ref_value = spec.split(":", 1)[1].strip()
    else:
        ref_value = spec

    ref_audio = Path(ref_value)
    if not ref_audio.is_absolute():
        ref_audio = PROJECT_ROOT / ref_audio
    if not ref_audio.exists():
        raise RuntimeError(
            f"VieNeu voice spec must be preset:<id> or ref:<audio_path>; file not found: {ref_audio}"
        )

    voice = client.encode_reference(str(ref_audio))
    _VIENEU_VOICE_CACHE[cache_key] = voice
    return voice


def normalize_vieneu_voice_spec(voice_spec: str = "") -> str:
    spec = str(voice_spec or "").strip()
    lowered = spec.lower()
    if not spec or lowered in {"default", "vieneu", "auto"}:
        return ""
    if lowered.startswith(("preset:", "ref:")):
        return spec
    if Path(spec).suffix.lower() in VIENEU_REFERENCE_EXTENSIONS:
        return spec
    return ""


def generate_vieneu_tts_stable(text: str, output_path: Path, voice_spec: str = "") -> Path:
    chunks = split_tts_chunks(text, max_chars=500)
    if not chunks:
        raise RuntimeError("No text available for VieNeu TTS")

    client = get_vieneu_client()
    voice_spec = normalize_vieneu_voice_spec(voice_spec)
    voice = get_vieneu_voice_by_spec(client, voice_spec)
    output_path = output_path.with_suffix(".wav")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(chunks) == 1:
        infer_kwargs = {"text": chunks[0]}
        if voice is not None:
            infer_kwargs["voice"] = voice
        audio = client.infer(**infer_kwargs)
        client.save(audio, str(output_path))
        return output_path

    chunk_dir = output_path.parent / f"{output_path.stem}_vieneu_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_paths = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_path = chunk_dir / f"{output_path.stem}_{index:02d}.wav"
        infer_kwargs = {"text": chunk}
        if voice is not None:
            infer_kwargs["voice"] = voice
        audio = client.infer(**infer_kwargs)
        client.save(audio, str(chunk_path))
        chunk_paths.append(chunk_path)

    return concat_audio_files(chunk_paths, output_path)


def generate_audio(text: str, output_path: Path, voice: str) -> tuple[Path, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not VIENEU_TTS_ENABLED:
        raise RuntimeError("VieNeu TTS is required but VIENEU_TTS_ENABLED is false")
    vieneu_path = generate_vieneu_tts_stable(text, output_path.with_suffix(".wav"), voice)
    return vieneu_path, f"tts=vieneu mode={VIENEU_MODE} voice={voice or 'default'}"


def generate_segment_audio(
    text: str,
    output_path: Path,
    fallback_duration: float,
    voice: str,
    tts_engine: str,
) -> tuple[Path, str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tts_engine = str(tts_engine or "auto").strip().lower()

    if tts_engine not in {"auto", "vieneu"}:
        raise RuntimeError(f"Unsupported TTS engine for VieNeu-only mode: {tts_engine}")
    if not VIENEU_TTS_ENABLED:
        raise RuntimeError("VieNeu TTS is required but VIENEU_TTS_ENABLED is false")
    try:
        vieneu_path = generate_vieneu_tts_stable(text, output_path.with_suffix(".wav"), voice)
        return vieneu_path, f"tts=vieneu mode={VIENEU_MODE} voice={voice or 'default'}"
    except Exception as exc:
        raise RuntimeError(f"VieNeu TTS failed for fixed segment voice {voice or 'default'}: {exc}") from exc


def concat_audio_files(paths: list[Path], output_path: Path) -> Path:
    if not paths:
        raise RuntimeError("No audio segment files to concatenate.")
    if len(paths) == 1:
        source_path = paths[0]
        if source_path.resolve() != output_path.resolve():
            output_path.write_bytes(source_path.read_bytes())
        return output_path

    filter_parts = []
    concat_inputs = []
    for index in range(len(paths)):
        label = f"a{index}"
        filter_parts.append(
            f"[{index}:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=mono,asetpts=N/SR/TB[{label}]"
        )
        concat_inputs.append(f"[{label}]")
    filter_parts.append(f"{''.join(concat_inputs)}concat=n={len(paths)}:v=0:a=1[outa]")

    command = [
        ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    for path in paths:
        command.extend(["-i", str(path)])
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[outa]",
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "libmp3lame",
            str(output_path),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg audio concat failed").strip())
    return output_path


def normalize_audio_segment(source_path: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio_filter = (
        "aresample=44100,"
        "aformat=sample_fmts=s16:channel_layouts=mono"
    )
    if AUDIO_TRIM_SEGMENT_SILENCE:
        audio_filter += (
            ",silenceremove="
            f"start_periods=1:start_duration={AUDIO_LEADING_SILENCE_SECONDS}:start_threshold={AUDIO_SILENCE_THRESHOLD_DB}:"
            f"stop_periods=1:stop_duration={AUDIO_TRAILING_SILENCE_SECONDS}:stop_threshold={AUDIO_SILENCE_THRESHOLD_DB}"
        )
    command = [
        ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-vn",
        "-af",
        audio_filter,
        "-c:a",
        "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg audio normalize failed").strip())
    return output_path


def normalize_timed_audio_segments(timed_segments: list[dict], temp_dir: Path) -> list[dict]:
    normalized_dir = temp_dir / "normalized_wav"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized_segments = []

    for index, item in enumerate(timed_segments, start=1):
        source_path = Path(item["audio_path"])
        wav_path = normalized_dir / f"segment_{index:02d}.wav"
        normalize_audio_segment(source_path, wav_path)
        duration = round(probe_duration(wav_path), 3)
        payload = dict(item)
        payload["audio_path"] = wav_path
        payload["duration"] = duration
        normalized_segments.append(payload)

    return normalized_segments


def concat_normalized_audio_files(paths: list[Path], output_path: Path, overlap_seconds: float = 0.0) -> Path:
    if not paths:
        raise RuntimeError("No normalized audio segment files to concatenate.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if len(paths) == 1:
        source_path = paths[0]
        if source_path.resolve() != output_path.resolve():
            output_path.write_bytes(source_path.read_bytes())
        return output_path

    filter_parts = []
    concat_inputs = []
    for index in range(len(paths)):
        label = f"a{index}"
        filter_parts.append(
            f"[{index}:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=mono,asetpts=N/SR/TB[{label}]"
        )
        concat_inputs.append(f"[{label}]")

    overlap_seconds = round(max(0.0, min(float(overlap_seconds or 0.0), 0.35)), 3)
    if overlap_seconds > 0:
        current_label = "a0"
        for index in range(1, len(paths)):
            output_label = "outa" if index == len(paths) - 1 else f"xf{index}"
            filter_parts.append(
                f"[{current_label}][a{index}]acrossfade=d={overlap_seconds}:c1=tri:c2=tri[{output_label}]"
            )
            current_label = output_label
    else:
        filter_parts.append(f"{''.join(concat_inputs)}concat=n={len(paths)}:v=0:a=1[outa]")

    command = [
        ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    for path in paths:
        command.extend(["-i", str(path)])
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[outa]",
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=240)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg normalized audio concat failed").strip())
    return output_path


def build_timing_manifest_from_segments(timed_segments: list[dict], audio_duration: float, overlap_seconds: float = 0.0) -> list[dict]:
    cursor = 0.0
    manifest = []
    overlap_seconds = round(max(0.0, min(float(overlap_seconds or 0.0), 0.35)), 3)

    for index, item in enumerate(timed_segments):
        duration = max(0.0, float(item.get("duration", 0.0) or 0.0))
        start = round(cursor, 3)
        end = round(cursor + duration, 3)
        if index == len(timed_segments) - 1:
            end = round(max(start, audio_duration), 3)
        manifest.append(
            {
                "image_index": item["image_index"],
                "start": start,
                "end": end,
                "type": item.get("type", "segment"),
            }
        )
        cursor = max(start, end - overlap_seconds)

    return manifest


def write_audio_timing_debug(path: Path, timed_segments: list[dict], timing_manifest: list[dict], audio_duration: float) -> None:
    debug_items = []
    for index, item in enumerate(timed_segments):
        timing = timing_manifest[index] if index < len(timing_manifest) else {}
        debug_items.append(
            {
                "segment_index": index + 1,
                "image_index": item.get("image_index", index),
                "type": item.get("type", "segment"),
                "duration": round(float(item.get("duration", 0.0) or 0.0), 3),
                "start": timing.get("start"),
                "end": timing.get("end"),
                "text": str(item.get("text") or "")[:1000],
            }
        )

    payload = {
        "audio_duration": round(float(audio_duration), 3),
        "segments": debug_items,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def concat_audio_files_legacy(paths: list[Path], output_path: Path) -> Path:
    if not paths:
        raise RuntimeError("No audio segment files to concatenate.")

    list_path = output_path.with_suffix(".concat.txt")
    lines = []
    for path in paths:
        safe_path = str(path).replace("\\", "/").replace("'", "")
        lines.append(f"file '{safe_path}'")
    list_path.write_text("\n".join(lines), encoding="utf-8")

    command = [
        ffmpeg_executable(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-vn",
        "-ar",
        "44100",
        "-ac",
        "1",
        "-c:a",
        "libmp3lame",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=720)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg audio concat failed").strip())
    return output_path


def select_segment_voice(
    segment: dict,
    default_voice: str,
    discussion_voices: list[str],
    author_voices: list[str],
    author_voice_map: dict[str, str],
) -> str:
    segment_type = str(segment.get("type") or "").strip().lower()
    if segment_type in {"post", "continuation"}:
        return default_voice

    if author_voices:
        author_key = str(segment.get("author_key") or "").strip().lower()
        if author_key:
            if author_key not in author_voice_map:
                author_voice_map[author_key] = author_voices[len(author_voice_map) % len(author_voices)]
            return author_voice_map[author_key]

    if discussion_voices:
        author_key = str(segment.get("author_key") or "").strip().lower()
        if author_key:
            if author_key not in author_voice_map:
                author_voice_map[author_key] = discussion_voices[len(author_voice_map) % len(discussion_voices)]
            return author_voice_map[author_key]

    return default_voice


def generate_segments_with_timing(
    segments: list[dict],
    temp_dir: Path,
    voice: str,
    discussion_voices: list[str],
    author_voices: list[str],
    tts_engine: str = "auto",
) -> tuple[list[dict], list[str]]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    notes: list[str] = []
    author_voice_map: dict[str, str] = {}

    for index, item in enumerate(segments, start=1):
        text = clean_script(str(item.get("text") or ""))
        if not text:
            continue

        segment_type = str(item.get("type") or "segment").lower()
        image_index = item.get("image_index", index - 1)
        segment_voice = select_segment_voice(
            item,
            voice,
            discussion_voices,
            author_voices,
            author_voice_map,
        )

        segment_path = temp_dir / f"segment_{index:02d}.mp3"
        fallback_duration = max(2.0, len(text) / 12)
        audio_path, audio_note = generate_segment_audio(
            text,
            segment_path,
            fallback_duration,
            segment_voice,
            tts_engine=tts_engine,
        )

        try:
            real_duration = probe_duration(audio_path)
        except Exception:
            real_duration = fallback_duration

        results.append(
            {
                "type": segment_type,
                "text": text,
                "audio_path": audio_path,
                "duration": round(real_duration, 3),
                "image_index": int(image_index) if str(image_index).strip() else index - 1,
                "author_key": item.get("author_key", ""),
                "voice": segment_voice,
            }
        )
        notes.append(audio_note)

    return results, notes


def add_absolute_timing(
    timed_segments: list[dict],
    gap_seconds: float = 0.0,
    intro_seconds: float = 0.0,
) -> list[dict]:
    cursor = intro_seconds
    results: list[dict] = []

    for item in timed_segments:
        duration = max(0.8, float(item.get("duration", 0.0) or 0.0))
        start = round(cursor, 3)
        end = round(cursor + duration, 3)
        payload = dict(item)
        payload["start_abs"] = start
        payload["end_abs"] = end
        results.append(payload)
        cursor = end + gap_seconds

    return results


def align_timing_to_available_images(timing_data: list[dict], image_count: int) -> list[dict]:
    if image_count <= 0 or not timing_data:
        return []

    aligned = []
    for index, item in enumerate(timing_data):
        payload = dict(item)
        try:
            image_index = int(item.get("image_index", index))
        except (TypeError, ValueError):
            image_index = index
        payload["image_index"] = min(max(0, image_index), image_count - 1)
        aligned.append(payload)

    return aligned


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

    sequence = paths[:MAX_OVERLAY_IMAGES]
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
                "width": round(TARGET_SIZE[0] * OVERLAY_WIDTH_RATIO),
            }
        )
    return plan


def build_overlay_plan_from_timing(paths: list[Path], timing_data: list[dict], duration: float) -> list[dict]:
    if not paths or not timing_data:
        return []

    sequence = paths[:MAX_OVERLAY_IMAGES]
    timing_data = align_timing_to_available_images(timing_data, len(sequence))
    max_timing_end = 0.0
    for item in timing_data:
        try:
            max_timing_end = max(max_timing_end, float(item.get("end", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
    timing_scale = (duration / max_timing_end) if max_timing_end > duration > 0 else 1.0
    plan = []
    used_image_indexes: set[int] = set()

    for timing_index, item in enumerate(timing_data):
        try:
            image_index = int(item.get("image_index", 0))
        except (TypeError, ValueError):
            image_index = timing_index
        if timing_index < len(sequence) and (image_index < 0 or image_index >= len(sequence)):
            image_index = timing_index
        if image_index < 0 or image_index >= len(sequence):
            continue
        used_image_indexes.add(image_index)

        start_raw = float(item.get("start", 0.0) or 0.0) * timing_scale
        start = round(max(0.0, start_raw - VISUAL_TIMING_LEAD_SECONDS), 3)
        end_raw = float(item.get("end", start + 1.0) or (start + 1.0)) * timing_scale
        end = round(min(duration, end_raw), 3)
        if end <= start:
            end = round(min(duration, start + 1.0), 3)

        plan.append(
            {
                "path": sequence[image_index],
                "start": start,
                "end": end,
                "width": round(TARGET_SIZE[0] * OVERLAY_WIDTH_RATIO),
            }
        )

    if len(plan) < min(len(sequence), len(timing_data)):
        missing_image_indexes = [index for index in range(len(sequence)) if index not in used_image_indexes]
        missing_cursor = 0
        existing_keys = {(round(float(item["start"]), 3), round(float(item["end"]), 3)) for item in plan}
        for item in timing_data:
            if missing_cursor >= len(missing_image_indexes):
                break
            try:
                image_index = int(item.get("image_index", 0))
            except (TypeError, ValueError):
                image_index = -1
            if 0 <= image_index < len(sequence):
                continue
            start = round(max(0.0, float(item.get("start", 0.0) or 0.0) * timing_scale), 3)
            end = round(min(duration, float(item.get("end", start + 1.0) or (start + 1.0)) * timing_scale), 3)
            key = (start, end)
            if key in existing_keys:
                continue
            fallback_index = missing_image_indexes[missing_cursor]
            missing_cursor += 1
            plan.append(
                {
                    "path": sequence[fallback_index],
                    "start": start,
                    "end": max(start + 0.2, end),
                    "width": round(TARGET_SIZE[0] * OVERLAY_WIDTH_RATIO),
                }
            )
            existing_keys.add(key)

    plan.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    for index, item in enumerate(plan):
        next_start = float(plan[index + 1]["start"]) if index + 1 < len(plan) else duration
        item["end"] = round(min(max(float(item["start"]) + 0.2, float(item["end"])), next_start), 3)
    if plan:
        last_audio_end = float(plan[-1]["end"])
        plan[-1]["end"] = round(min(last_audio_end, duration), 3)
    return plan


def escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def ffmpeg_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def overlay_animation_duration(start: float, end: float) -> float:
    visible_duration = max(0.0, end - start)
    if OVERLAY_ANIMATION in {"none", "off", "false", "0"} or visible_duration <= 0.2:
        return 0.0
    return min(OVERLAY_ANIMATION_SECONDS, max(0.0, visible_duration / 3.0))


def overlay_y_expression(start: float, end: float) -> str:
    base_y = f"(H-h)*{OVERLAY_TOP_RATIO}"
    animation_duration = overlay_animation_duration(start, end)
    if animation_duration <= 0 or OVERLAY_SLIDE_PIXELS <= 0 or "slide" not in OVERLAY_ANIMATION:
        return base_y

    start_s = ffmpeg_number(start)
    intro_end_s = ffmpeg_number(start + animation_duration)
    duration_s = ffmpeg_number(animation_duration)
    # Escape commas because this expression is embedded inside the overlay filter.
    return (
        f"{base_y}+if(lt(t\\,{intro_end_s})\\,"
        f"{OVERLAY_SLIDE_PIXELS}*(1-(t-{start_s})/{duration_s})\\,0)"
    )


def overlay_video_filters(input_label: str, output_label: str, width: int, start: float, end: float, duration: float, fps: int) -> str:
    visible_duration = max(0.2, min(duration, end) - max(0.0, start))
    filters = [
        f"[{input_label}]fps={fps}",
        f"trim=duration={ffmpeg_number(visible_duration)}",
        "setpts=PTS-STARTPTS",
        f"scale={width}:-2:flags=lanczos",
        "format=rgba",
    ]

    animation_duration = overlay_animation_duration(start, end)
    if animation_duration > 0 and "fade" in OVERLAY_ANIMATION:
        fade_in_start = "0"
        fade_duration = ffmpeg_number(animation_duration)
        fade_out_start = ffmpeg_number(max(0.0, visible_duration - animation_duration))
        filters.append(f"fade=t=in:st={fade_in_start}:d={fade_duration}:alpha=1")
        filters.append(f"fade=t=out:st={fade_out_start}:d={fade_duration}:alpha=1")

    filters.append(f"setpts=PTS+{ffmpeg_number(max(0.0, start))}/TB")
    return ",".join(filters) + f"[{output_label}]"


def build_visual_ffmpeg(background_path: Path, output_path: Path, overlays: list[dict], duration: float, fps: int = 30) -> None:
    ffmpeg = ffmpeg_executable()
    target_w, target_h = TARGET_SIZE
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]

    if background_path.exists():
        command.extend(["-stream_loop", "-1", "-i", str(background_path)])
        bg_label = "[0:v]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},fps={fps},trim=duration={duration},setpts=PTS-STARTPTS[base]".format(
            width=target_w,
            height=target_h,
            fps=fps,
            duration=duration
        )
        next_input_index = 1
    else:
        command.extend(["-f", "lavfi", "-i", f"color=c=0x121212:s={target_w}x{target_h}:r={fps}:d={duration}"])
        bg_label = "[0:v]trim=duration={duration},setpts=PTS-STARTPTS[base]".format(duration=duration)
        next_input_index = 1

    filter_parts = [bg_label]
    current_label = "base"
    for index, item in enumerate(overlays):
        command.extend(["-loop", "1", "-i", str(item["path"])])
        input_label = f"{next_input_index}:v"
        overlay_label = f"ov{index}"
        output_label = f"v{index}"
        width = int(item["width"])
        start = float(item["start"])
        end = float(item["end"])
        overlay_y_expr = overlay_y_expression(start, end)
        filter_parts.append(overlay_video_filters(input_label, overlay_label, width, start, end, duration, fps))
        filter_parts.append(
            f"[{current_label}][{overlay_label}]overlay=(W-w)/2:{overlay_y_expr}:enable='between(t,{start},{end})':eof_action=pass[{output_label}]"
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
            "-fps_mode",
            "cfr",
            "-an",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-crf",
            VIDEO_CRF,
            "-preset",
            VIDEO_ENCODE_PRESET,
            "-b:v",
            VIDEO_TARGET_BITRATE,
            "-maxrate",
            VIDEO_MAXRATE,
            "-bufsize",
            VIDEO_BUFSIZE,
            "-profile:v",
            "high",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "ffmpeg visual build failed").strip()
        raise RuntimeError(f"{detail} (ffmpeg returncode={result.returncode})")


def write_voice(
    post_id: str,
    script: str,
    extracted_content: str,
    audio_dir: Path,
    temp_dir: Path,
    voice: str,
    discussion_voices: list[str],
    author_voices: list[str],
) -> dict:
    script = normalize_narration_script(script, extracted_content)
    if not script:
        raise RuntimeError("Narrator_Script is empty.")

    output_dir = dated_output_dir(audio_dir, post_id)
    temp_post_dir = temp_dir / safe_filename(post_id)
    temp_post_dir.mkdir(parents=True, exist_ok=True)
    fallback_duration = estimate_duration(script, 1)
    content_mode = parse_content_mode(extracted_content)
    audio_segments = select_audio_segments(script, extracted_content)

    if audio_segments:
        effective_discussion_voices = discussion_voices if content_mode == "discussion" else []
        voice_engines = tts_engine_order()

        last_error = None
        timed_segments, segment_notes = [], []
        for engine in voice_engines:
            try:
                timed_segments, segment_notes = generate_segments_with_timing(
                    audio_segments,
                    temp_post_dir / f"segments_{engine}",
                    voice,
                    effective_discussion_voices,
                    author_voices,
                    tts_engine=engine,
                )
                break
            except RuntimeError as exc:
                last_error = exc
                log(f"{engine} batch TTS failed: {exc}")

        if not timed_segments:
            raise RuntimeError(f"VieNeu fixed voice TTS failed: {last_error}")
    else:
        timed_segments, segment_notes = [], []

    if timed_segments:
        timed_segments = normalize_timed_audio_segments(timed_segments, temp_post_dir)
        overlap_seconds = AUDIO_SEGMENT_OVERLAP_SECONDS if len(timed_segments) > 1 else 0.0
        audio_path = concat_normalized_audio_files(
            [Path(item["audio_path"]) for item in timed_segments],
            output_dir / "narration.wav",
            overlap_seconds=overlap_seconds,
        )
        final_audio_duration = round(probe_duration(audio_path), 3)
        expected_timing_end = round(
            sum(float(item.get("duration", 0.0) or 0.0) for item in timed_segments)
            - overlap_seconds * max(0, len(timed_segments) - 1),
            3,
        )
        timing_delta = round(final_audio_duration - expected_timing_end, 3)
        if overlap_seconds > 0 and abs(timing_delta) > 0.25:
            log(f"Audio overlap produced unexpected duration delta={timing_delta:.3f}s; falling back to plain concat.")
            overlap_seconds = 0.0
            audio_path = concat_normalized_audio_files(
                [Path(item["audio_path"]) for item in timed_segments],
                output_dir / "narration.wav",
                overlap_seconds=0.0,
            )
            final_audio_duration = round(probe_duration(audio_path), 3)
            expected_timing_end = round(sum(float(item.get("duration", 0.0) or 0.0) for item in timed_segments), 3)
            timing_delta = round(final_audio_duration - expected_timing_end, 3)
        audio_note = "tts=per-segment " + ", ".join(dict.fromkeys(segment_notes))
        if overlap_seconds > 0:
            audio_note = f"{audio_note} overlap={overlap_seconds:.2f}s"
        timing_manifest = build_timing_manifest_from_segments(timed_segments, final_audio_duration, overlap_seconds=overlap_seconds)
        write_audio_timing_debug(output_dir / "audio_timing_debug.json", timed_segments, timing_manifest, final_audio_duration)
        if abs(timing_delta) > 0.05:
            audio_note = f"{audio_note} timing_delta={timing_delta:.3f}s"
    else:
        audio_path, audio_note = generate_audio(
            script,
            output_dir / "narration.mp3",
            voice,
        )
        duration = round(probe_duration(audio_path), 2)
        timing_manifest = [{"image_index": 0, "start": 0.0, "end": round(duration, 3), "type": "segment"}]

    duration = round(probe_duration(audio_path), 2)
    return {
        "ID": post_id,
        "Audio_Path": str(audio_path),
        "Audio_Timing": json.dumps(timing_manifest, ensure_ascii=True),
        "Status": "In Progress",
        "Note": f"Phase 3A: voice ready duration={duration}s mode={content_mode} segments={len(timing_manifest)} {audio_note}",
    }


def build_visual(
    post_id: str,
    screenshots: dict,
    script: str,
    background_path: Path,
    visuals_dir: Path,
    audio_path: Path | None = None,
    extracted_content: str = "",
    audio_timing: str = "",
) -> dict:
    images = screenshot_paths(screenshots)
    if not images:
        raise RuntimeError("No screenshot files found for visual build.")

    text_blocks = select_overlay_text_blocks(images, script, extracted_content)
    timing_data: list[dict] = []
    if audio_timing:
        try:
            parsed_timing = json.loads(audio_timing)
            if isinstance(parsed_timing, list):
                timing_data = [item for item in parsed_timing if isinstance(item, dict)]
        except json.JSONDecodeError:
            timing_data = []

    if audio_path and audio_path.exists():
        # Match visual length to the generated narration instead of hard-capping
        # at short-form defaults, otherwise the final merge trims the audio.
        audio_duration = probe_duration(audio_path)
        duration = max(1.0, audio_duration)
    else:
        duration = estimate_duration(clean_script(script), len(images)) + 2.0
        duration = min(120.0, max(10.0, duration))

    output_dir = dated_output_dir(visuals_dir, post_id)
    output_path = output_dir / "visual.mp4"
    images = prepare_overlay_images(images, output_dir / "overlays")
    overlay_plan = build_overlay_plan_from_timing(images, timing_data, duration) if timing_data else []
    if not overlay_plan:
        overlay_plan = build_overlay_plan(images, text_blocks, duration)
    if not overlay_plan:
        raise RuntimeError("No overlay plan generated for visual build.")
    build_visual_ffmpeg(background_path, output_path, overlay_plan, duration, fps=30)

    return {
        "ID": post_id,
        "Visual_Video_Path": str(output_path),
        "Status": "In Progress",
        "Note": f"Phase 3B: visual ready duration={round(duration, 2)}s images={len(images)} overlays={len(overlay_plan)} background={background_path.name} lead={VISUAL_TIMING_LEAD_SECONDS}s sync={'audio-timing' if timing_data else ('segments' if parse_extracted_segments(extracted_content) else 'script')}",
    }


def merge_final(post_id: str, audio_path: Path, visual_path: Path, videos_dir: Path, script: str = "", extracted_content: str = "") -> dict:
    if not audio_path.exists():
        raise RuntimeError(f"Audio file not found: {audio_path}")
    if not visual_path.exists():
        raise RuntimeError(f"Visual video not found: {visual_path}")

    output_dir = dated_output_dir(videos_dir, post_id)
    output_path = output_dir / "final.mp4"
    audio_duration = probe_duration(audio_path)
    duration = max(1.0, audio_duration)
    music_path = choose_background_music(post_id)

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
    ]
    if music_path:
        command.extend(["-stream_loop", "-1"])
        if BACKGROUND_MUSIC_START_OFFSET_SECONDS > 0:
            command.extend(["-ss", f"{BACKGROUND_MUSIC_START_OFFSET_SECONDS:.3f}"])
        command.extend(["-i", str(music_path)])

    fade_duration = min(BACKGROUND_MUSIC_FADE_SECONDS, max(0.0, duration / 3.0))
    fade_out_start = max(0.0, duration - fade_duration)
    if music_path:
        music_filters = [
            "[2:a]aresample=44100:first_pts=0",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"volume={BACKGROUND_MUSIC_VOLUME}",
        ]
        if fade_duration > 0:
            music_filters.append(f"afade=t=in:st=0:d={fade_duration:.3f}")
            music_filters.append(f"afade=t=out:st={fade_out_start:.3f}:d={fade_duration:.3f}")
        music_filter = ",".join(music_filters) + "[musicbase]"
        if BACKGROUND_MUSIC_DUCKING:
            music_filter = (
                music_filter
                + ";[musicbase][voice_sc]sidechaincompress=threshold=0.045:ratio=8:attack=60:release=450[ducked]"
            )
            music_label = "ducked"
            voice_split_filter = "[voicebase]asplit=2[voice_sc][voice_mix];"
            voice_mix_label = "voice_mix"
        else:
            music_label = "musicbase"
            voice_split_filter = ""
            voice_mix_label = "voicebase"
        filter_complex = (
            "[0:v]setpts=PTS-STARTPTS,fps=30[v];"
            "[1:a]aresample=44100:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=mono,asetpts=PTS-STARTPTS[voicebase];"
            f"{voice_split_filter}"
            f"{music_filter};"
            f"[{voice_mix_label}][{music_label}]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[a]"
        )
    else:
        filter_complex = (
            "[0:v]setpts=PTS-STARTPTS,fps=30[v];"
            "[1:a]aresample=44100:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=mono,asetpts=PTS-STARTPTS[a]"
        )

    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            str(duration),
            "-fps_mode",
            "cfr",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-crf",
            VIDEO_CRF,
            "-preset",
            VIDEO_ENCODE_PRESET,
            "-b:v",
            VIDEO_TARGET_BITRATE,
            "-maxrate",
            VIDEO_MAXRATE,
            "-bufsize",
            VIDEO_BUFSIZE,
            "-profile:v",
            "high",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-b:a",
            AUDIO_BITRATE,
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg merge failed").strip())

    return {
        "ID": post_id,
        "Video_Path": str(output_path),
        "Narrator_Script": script,
        "Extracted_Content": extracted_content,
        "Caption": build_caption(script=script, extracted_content=extracted_content),
        "Status": "Draft",
        "Note": f"Phase 3C: draft ready duration={round(duration, 2)}s music={music_path.name if music_path else 'off'}",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split phase 3 video pipeline.")
    parser.add_argument("--mode", required=True, choices=["voice", "visual", "merge"])
    parser.add_argument("--id", required=True)
    parser.add_argument("--screenshots")
    parser.add_argument("--script")
    parser.add_argument("--extracted-content")
    parser.add_argument("--audio-path")
    parser.add_argument("--audio-timing")
    parser.add_argument("--visual-path")
    parser.add_argument("--background", default=os.getenv("BACKGROUND_VIDEO_PATH", DEFAULT_BACKGROUND))
    parser.add_argument("--background-dir", default=os.getenv("BACKGROUND_VIDEO_DIR", DEFAULT_BACKGROUND_DIR))
    parser.add_argument("--audio-dir", default=os.getenv("AUDIO_DIR", DEFAULT_AUDIO_DIR))
    parser.add_argument("--visuals-dir", default=os.getenv("VISUALS_DIR", DEFAULT_VISUALS_DIR))
    parser.add_argument("--videos-dir", default=os.getenv("VIDEOS_DIR", DEFAULT_VIDEOS_DIR))
    parser.add_argument("--temp-dir", default=os.getenv("TEMP_DIR", DEFAULT_TEMP_DIR))
    parser.add_argument("--voice", default=os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE))
    parser.add_argument("--discussion-voices", default=os.getenv("TTS_DISCUSSION_VOICES", ",".join(DEFAULT_DISCUSSION_VOICES)))
    parser.add_argument("--author-voices", default=os.getenv("TTS_AUTHOR_VOICES", ",".join(DEFAULT_AUTHOR_VOICES)))
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    args = parse_args()
    args.id = decode_cli_text(args.id)
    args.screenshots = decode_cli_text(args.screenshots)
    args.script = decode_cli_text(args.script)
    args.extracted_content = decode_cli_text(args.extracted_content)
    args.audio_path = decode_cli_text(args.audio_path)
    args.audio_timing = decode_cli_text(args.audio_timing)
    args.visual_path = decode_cli_text(args.visual_path)

    try:
        if args.mode == "voice":
            if not args.script:
                raise RuntimeError("--script is required for mode=voice")
            discussion_voices = [item.strip() for item in str(args.discussion_voices or "").split(",") if item.strip()]
            author_voices = [item.strip() for item in str(args.author_voices or "").split(",") if item.strip()]
            result = write_voice(
                post_id=args.id,
                script=args.script,
                extracted_content=args.extracted_content or "",
                audio_dir=resolve_path(args.audio_dir),
                temp_dir=resolve_path(args.temp_dir),
                voice=args.voice,
                discussion_voices=discussion_voices,
                author_voices=author_voices,
            )
        elif args.mode == "visual":
            if not args.screenshots:
                raise RuntimeError("--screenshots is required for mode=visual")
            background_path = choose_background_video(
                args.id,
                resolve_path(args.background),
                resolve_path(args.background_dir) if args.background_dir else None,
            )
            result = build_visual(
                post_id=args.id,
                screenshots=parse_screenshots(args.screenshots),
                script=args.script or "",
                extracted_content=args.extracted_content or "",
                background_path=background_path,
                visuals_dir=resolve_path(args.visuals_dir),
                audio_path=resolve_path(args.audio_path) if args.audio_path else None,
                audio_timing=args.audio_timing or "",
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
