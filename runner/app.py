from __future__ import annotations

import json
import os
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from src.local_review import validate_review_token


PROJECT_ROOT = Path("/workspace")
ENV_PATH = PROJECT_ROOT / ".env"
PYTHON_BIN = "python"
PHASE3_VOICE_TIMEOUT_SECONDS = max(420, int(os.getenv("PHASE3_VOICE_TIMEOUT_SECONDS", "900")))

app = FastAPI(title="n8n AI Video Runner")


class Phase2Payload(BaseModel):
    id: str
    url: str


class Phase1SearchPayload(BaseModel):
    keyword: str
    max_posts: int | None = None
    chat_id: str = ""
    requested_by: str = ""


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


def n8n_phase4_callback_url() -> str:
    load_dotenv(ENV_PATH, override=True)
    return (
        os.getenv("N8N_PHASE4_CALLBACK_URL", "").strip()
        or "http://n8n:5678/webhook/phase4-review-callback"
    )


def n8n_phase1_search_callback_url() -> str:
    load_dotenv(ENV_PATH, override=True)
    return (
        os.getenv("N8N_PHASE1_SEARCH_CALLBACK_URL", "").strip()
        or "http://n8n:5678/webhook/phase1-search-callback"
    )


def forward_telegram_callback_to_n8n(payload: dict) -> None:
    target = n8n_phase4_callback_url()
    try:
        requests.post(target, json=payload, timeout=1200).raise_for_status()
    except Exception as exc:
        print(f"Could not forward Telegram callback to n8n: {exc}", flush=True)


def forward_phase1_search_to_n8n(payload: dict) -> None:
    target = n8n_phase1_search_callback_url()
    try:
        requests.post(target, json=payload, timeout=1200).raise_for_status()
    except Exception as exc:
        print(f"Could not forward Phase 1 Telegram search to n8n: {exc}", flush=True)


def run_python(args: list[str], timeout: int, extra_env: dict[str, str] | None = None) -> dict:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            [PYTHON_BIN, *args],
            cwd=PROJECT_ROOT,
            env=env,
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


def compact_error_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            parts = [str(detail.get("message") or "").strip()]
            stderr = str(detail.get("stderr") or "").strip()
            stdout = str(detail.get("stdout") or "").strip()
            if stderr:
                parts.append(stderr)
            elif stdout:
                parts.append(stdout)
            message = " | ".join(part for part in parts if part)
        else:
            message = str(detail)
    else:
        message = str(exc)
    return " ".join(message.split())[:500]


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def failed_result(post_id: str, phase: str, exc: Exception, **extra: str) -> dict[str, str]:
    return {
        "ID": post_id,
        "Status": "Failed",
        "Note": f"{phase} failed: {compact_error_detail(exc)}",
        **extra,
    }


def fetch_trend_signal(keyword: str = "") -> dict:
    load_dotenv(ENV_PATH, override=True)
    if not bool_env("TREND_SIGNAL_ENABLED", True):
        return {"keyword": keyword, "topics": [], "brief": "", "context": [], "errors": ["trend signal disabled"]}
    args = ["src/trend_signal.py"]
    if keyword:
        args.extend(["--keyword", keyword])
    fetch_timeout = int_env("TREND_FETCH_TIMEOUT_SECONDS", 12)
    data = run_python(args, timeout=int_env("TREND_RUN_TIMEOUT_SECONDS", max(30, fetch_timeout * 4)))
    return data if isinstance(data, dict) else {"keyword": keyword, "topics": [], "brief": "", "context": []}


def attach_phase1_context(posts: list[dict], trend_signal: dict, source: str, keyword: str = "") -> list[dict]:
    brief = str(trend_signal.get("brief") or "").strip()
    topics = trend_signal.get("topics") if isinstance(trend_signal.get("topics"), list) else []
    news_context = trend_signal.get("context") if isinstance(trend_signal.get("context"), list) else []
    output: list[dict] = []
    for item in posts:
        if not isinstance(item, dict):
            continue
        output.append({
            **item,
            "source": item.get("source") or source,
            "search_keyword": item.get("search_keyword") or keyword,
            "trend_brief": brief,
            "trend_topics": topics[:10],
            "news_context": news_context[:8],
        })
    return output


def dedupe_posts(posts: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for item in posts:
        post_id = str(item.get("id") or "").strip()
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        result.append(item)
    return result


def rank_phase1_posts(posts: list[dict], limit: int | None = None) -> list[dict]:
    ranked = sorted(
        posts,
        key=lambda item: (
            float(item.get("content_fit_score") or 0) * 1000.0
            + float(item.get("engagement_score") or 0),
            float(item.get("engagement_strongest_metric") or 0),
        ),
        reverse=True,
    )
    if limit is None or limit <= 0:
        return ranked
    return ranked[:limit]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/review/{post_id}", response_class=HTMLResponse)
def review_page(post_id: str, token: str) -> HTMLResponse:
    load_dotenv(ENV_PATH, override=True)
    try:
        video_path = validate_review_token(post_id, token)
    except Exception as exc:
        raise HTTPException(status_code=403, detail={"message": str(exc)}) from exc

    quoted_id = quote(post_id, safe="")
    quoted_token = quote(token, safe="")
    video_url = f"/review/{quoted_id}/video?token={quoted_token}"
    download_url = f"/review/{quoted_id}/download?token={quoted_token}"
    safe_post_id = escape(post_id)
    safe_file_name = escape(video_path.name)
    html = f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Review {safe_post_id}</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #111; color: #f5f5f5; }}
    main {{ max-width: 860px; margin: 0 auto; padding: 24px 16px 40px; }}
    h1 {{ font-size: 20px; font-weight: 700; margin: 0 0 14px; }}
    video {{ display: block; width: min(100%, 430px); max-height: 82vh; margin: 0 auto; background: #000; border-radius: 8px; }}
    .bar {{ display: flex; gap: 10px; justify-content: center; margin-top: 16px; flex-wrap: wrap; }}
    a {{ color: #111; background: #f5f5f5; border-radius: 6px; padding: 9px 12px; text-decoration: none; font-weight: 700; }}
    p {{ color: #aaa; font-size: 13px; text-align: center; }}
  </style>
</head>
<body>
  <main>
    <h1>Review: {safe_post_id}</h1>
    <video src="{video_url}" controls playsinline preload="metadata"></video>
    <div class="bar">
      <a href="{video_url}" target="_blank" rel="noreferrer">Open video</a>
      <a href="{download_url}">Download MP4</a>
    </div>
    <p>{safe_file_name}</p>
  </main>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/review/{post_id}/video")
def review_video(post_id: str, token: str) -> FileResponse:
    load_dotenv(ENV_PATH, override=True)
    try:
        video_path = validate_review_token(post_id, token)
    except Exception as exc:
        raise HTTPException(status_code=403, detail={"message": str(exc)}) from exc
    return FileResponse(str(video_path), media_type="video/mp4")


@app.get("/review/{post_id}/download")
def review_download(post_id: str, token: str) -> FileResponse:
    load_dotenv(ENV_PATH, override=True)
    try:
        video_path = validate_review_token(post_id, token)
    except Exception as exc:
        raise HTTPException(status_code=403, detail={"message": str(exc)}) from exc
    return FileResponse(
        str(video_path),
        media_type="video/mp4",
        filename=video_path.name,
    )


@app.post("/phase4/telegram-callback")
async def phase4_telegram_callback(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    payload = await request.json()
    background_tasks.add_task(forward_telegram_callback_to_n8n, payload)
    return {"status": "accepted"}


@app.post("/phase1/telegram-search-callback")
async def phase1_telegram_search_callback(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    payload = await request.json()
    background_tasks.add_task(forward_phase1_search_to_n8n, payload)
    return {"status": "accepted"}


@app.post("/phase1/noop")
async def phase1_noop(request: Request) -> dict[str, object]:
    payload = await request.json()
    return payload if isinstance(payload, dict) else {"ok": True}


@app.post("/phase1/threads-miner")
def phase1_threads_miner() -> dict[str, object]:
    load_dotenv(ENV_PATH, override=True)
    trend_signal = fetch_trend_signal()
    posts = run_python(["src/threads_miner.py"], timeout=int_env("THREADS_MINER_TIMEOUT_SECONDS", 420))
    if not isinstance(posts, list):
        raise HTTPException(status_code=500, detail={"message": "threads_miner.py must return a JSON array"})
    return {"posts": dedupe_posts(attach_phase1_context(posts, trend_signal, source="auto")), "trend_signal": trend_signal}


@app.post("/phase1/threads-search")
def phase1_threads_search(payload: Phase1SearchPayload) -> dict[str, object]:
    load_dotenv(ENV_PATH, override=True)
    keyword = payload.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail={"message": "keyword is required"})
    trend_signal = fetch_trend_signal(keyword)
    max_posts = max(1, min(50, payload.max_posts or int_env("THREADS_SEARCH_MAX_POSTS", 10)))
    posts = run_python(
        [
            "src/threads_miner.py",
            "--search-keyword",
            keyword,
            "--max-posts",
            str(max_posts),
            "--candidate-limit",
            str(max(max_posts * 4, max_posts)),
        ],
        timeout=int_env("THREADS_SEARCH_TIMEOUT_SECONDS", 240),
    )
    if not isinstance(posts, list):
        raise HTTPException(status_code=500, detail={"message": "threads_miner.py must return a JSON array"})
    attached_posts = attach_phase1_context(posts, trend_signal, source=f"search:{keyword}", keyword=keyword)
    top_limit = max(1, min(10, int_env("THREADS_SEARCH_TOP_RESULTS", 2)))
    ranked_posts = rank_phase1_posts(dedupe_posts(attached_posts), limit=top_limit)
    return {
        "posts": ranked_posts,
        "trend_signal": trend_signal,
        "request": {
            "keyword": keyword,
            "max_posts": max_posts,
            "chat_id": payload.chat_id,
            "requested_by": payload.requested_by,
        },
        "post_count": len(ranked_posts),
        "raw_post_count": len(posts),
    }


@app.post("/phase2/screenshot-extract")
def phase2_screenshot_extract(payload: Phase2Payload) -> dict:
    try:
        data = run_python(
            ["src/screenshot_extractor.py", "--id", payload.id, "--url", payload.url],
            timeout=240,
        )
        if not isinstance(data, dict):
            raise RuntimeError("screenshot_extractor.py must return a JSON object")
        return data
    except Exception as exc:
        return failed_result(
            payload.id,
            "Phase 2 screenshot/extract",
            exc,
            Screenshots="",
            Extracted_Content="",
            Narrator_Script="",
        )


@app.post("/phase3/voice")
def phase3_voice(payload: Phase3VoicePayload) -> dict:
    try:
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
            timeout=PHASE3_VOICE_TIMEOUT_SECONDS,
        )
        if not isinstance(data, dict):
            raise RuntimeError("video_factory.py voice mode must return a JSON object")
        return data
    except Exception as exc:
        return failed_result(payload.id, "Phase 3A voice", exc, Audio_Path="", Audio_Timing="")


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
    try:
        data = run_python(args, timeout=720)
        if not isinstance(data, dict):
            raise RuntimeError("video_factory.py visual mode must return a JSON object")
        return data
    except Exception as exc:
        return failed_result(payload.id, "Phase 3B visual", exc, Visual_Video_Path="")


@app.post("/phase3/merge")
def phase3_merge(payload: Phase3MergePayload) -> dict:
    try:
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
            raise RuntimeError("video_factory.py merge mode must return a JSON object")
        return data
    except Exception as exc:
        return failed_result(payload.id, "Phase 3C merge", exc, Video_Path="", Caption="")


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
