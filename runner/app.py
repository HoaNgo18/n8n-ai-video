from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv
from pydantic import BaseModel, Field


PROJECT_ROOT = Path("/workspace")
ENV_PATH = PROJECT_ROOT / ".env"
PYTHON_BIN = "python"

app = FastAPI(title="n8n AI Video Runner")


class Phase2Payload(BaseModel):
    id: str
    url: str


class Phase3VoicePayload(BaseModel):
    id: str
    script: str
    extracted_content: str = ""


class Phase3VisualPayload(BaseModel):
    id: str
    screenshots: str
    script: str = ""
    audio_path: str = ""
    audio_timing: str = ""
    extracted_content: str = ""


class Phase3MergePayload(BaseModel):
    id: str
    audio_path: str
    visual_path: str
    script: str = ""
    extracted_content: str = ""


class Phase4PublishPayload(BaseModel):
    id: str
    video_path: str
    caption: str


class Phase4DraftReviewPayload(BaseModel):
    id: str
    video_path: str
    caption: str


class Phase4ManualUploadPayload(BaseModel):
    id: str
    video_path: str
    caption: str


class Phase4CompactPayload(BaseModel):
    mode: str = "tick"
    update: dict = Field(default_factory=dict)


def run_python(args: list[str], timeout: int) -> dict:
    try:
        result = subprocess.run(
            [PYTHON_BIN, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "message": f"Python command timed out after {timeout}s",
                "command": [PYTHON_BIN, *args[:8], "..."] if len(args) > 8 else [PYTHON_BIN, *args],
                "stdout": (exc.stdout or "")[:4000],
                "stderr": (exc.stderr or "")[:4000],
            },
        ) from exc
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Python command failed with exit {result.returncode}",
                "command": [PYTHON_BIN, *args],
                "stdout": stdout[:4000],
                "stderr": stderr[:4000],
            },
        )

    try:
        return json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        for line in reversed(stdout.splitlines()):
            candidate = line.strip()
            if not candidate.startswith(("{", "[")):
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        raise HTTPException(
            status_code=500,
            detail={
                "message": f"Could not parse Python JSON output: {exc}",
                "command": [PYTHON_BIN, *args],
                "stdout": stdout[:4000],
                "stderr": stderr[:4000],
            },
        ) from exc


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/phase1/threads-miner")
def phase1_threads_miner() -> dict[str, object]:
    posts = run_python(["src/threads_miner.py"], timeout=180)
    if not isinstance(posts, list):
        raise HTTPException(status_code=500, detail={"message": "threads_miner.py must return a JSON array"})
    return {"posts": posts}


@app.post("/phase2/screenshot-extract")
def phase2_screenshot_extract(payload: Phase2Payload) -> dict:
    data = run_python(
        ["src/screenshot_extractor.py", "--id", payload.id, "--url", payload.url],
        timeout=240,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "screenshot_extractor.py must return a JSON object"})
    return data


@app.post("/phase3/voice")
def phase3_voice(payload: Phase3VoicePayload) -> dict:
    data = run_python(
        [
            "src/video_factory.py",
            "--mode",
            "voice",
            "--id",
            payload.id,
            "--script",
            payload.script,
            "--extracted-content",
            payload.extracted_content,
        ],
        timeout=420,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "video_factory.py voice mode must return a JSON object"})
    return data


@app.post("/phase3/visual")
def phase3_visual(payload: Phase3VisualPayload) -> dict:
    args = [
        "src/video_factory.py",
        "--mode",
        "visual",
        "--id",
        payload.id,
        "--screenshots",
        payload.screenshots,
        "--script",
        payload.script,
        "--extracted-content",
        payload.extracted_content,
    ]
    if payload.audio_path:
        args.extend(["--audio-path", payload.audio_path])
    if payload.audio_timing:
        args.extend(["--audio-timing", payload.audio_timing])
    data = run_python(args, timeout=720)
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "video_factory.py visual mode must return a JSON object"})
    return data


@app.post("/phase3/merge")
def phase3_merge(payload: Phase3MergePayload) -> dict:
    data = run_python(
        [
            "src/video_factory.py",
            "--mode",
            "merge",
            "--id",
            payload.id,
            "--audio-path",
            payload.audio_path,
            "--visual-path",
            payload.visual_path,
            "--script",
            payload.script,
            "--extracted-content",
            payload.extracted_content,
        ],
        timeout=900,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "video_factory.py merge mode must return a JSON object"})
    return data


@app.post("/phase4/draft-review")
def phase4_draft_review(payload: Phase4DraftReviewPayload) -> dict:
    data = run_python(
        [
            "src/draft_review_helper.py",
            "--id",
            payload.id,
            "--video-path",
            payload.video_path,
            "--caption",
            payload.caption,
        ],
        timeout=420,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "draft_review_helper.py must return a JSON object"})
    return data


@app.post("/phase4/publish")
def phase4_publish(payload: Phase4PublishPayload) -> dict:
    load_dotenv(ENV_PATH, override=True)
    mode = os.getenv("TIKTOK_PUBLISHER_MODE", "api").strip().lower()
    script = "src/tiktok_playwright_publisher.py" if mode == "playwright" else "src/tiktok_publisher.py"
    data = run_python(
        [
            script,
            "--id",
            payload.id,
            "--video-path",
            payload.video_path,
            "--caption",
            payload.caption,
        ],
        timeout=900,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": f"{script} must return a JSON object"})
    return data


@app.post("/phase4/manual-upload-prep")
def phase4_manual_upload_prep(payload: Phase4ManualUploadPayload) -> dict:
    data = run_python(
        [
            "src/manual_upload_helper.py",
            "--id",
            payload.id,
            "--video-path",
            payload.video_path,
            "--caption",
            payload.caption,
        ],
        timeout=180,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "manual_upload_helper.py must return a JSON object"})
    return data


@app.post("/phase4/compact")
def phase4_compact(payload: Phase4CompactPayload) -> dict:
    if payload.mode not in {"tick", "review", "telegram_callback"}:
        raise HTTPException(status_code=400, detail={"message": "mode must be tick, review, or telegram_callback"})

    data = run_python(
        [
            "src/phase4_compact_helper.py",
            "--mode",
            payload.mode,
            "--update-json",
            json.dumps(payload.update, ensure_ascii=True),
        ],
        timeout=1200,
    )
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail={"message": "phase4_compact_helper.py must return a JSON object"})
    return data
