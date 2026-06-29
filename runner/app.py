from __future__ import annotations

import json
import os
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

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


class TelegramSendPayload(BaseModel):
    chat_id: str = ""
    text: str = ""
    disable_web_page_preview: bool = True
    reply_markup: dict = Field(default_factory=dict)


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


def telegram_bot_token(channel: str) -> str:
    load_dotenv(ENV_PATH, override=True)
    if channel == "phase1":
        return os.getenv("TELEGRAM_PHASE1_BOT_TOKEN", "").strip()
    if channel == "phase4":
        return os.getenv("TELEGRAM__PHASE4_BOT_TOKEN", "").strip()
    return ""


def post_telegram_message(bot_token: str, payload: TelegramSendPayload) -> dict[str, object]:
    chat_id = payload.chat_id.strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id or not payload.text.strip():
        raise RuntimeError("missing telegram bot token, chat id, or text")

    body: dict[str, object] = {
        "chat_id": chat_id,
        "text": payload.text,
        "disable_web_page_preview": payload.disable_web_page_preview,
    }
    if payload.reply_markup:
        body["reply_markup"] = payload.reply_markup

    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json() if response.content else {}
    return data.get("result") or {}


def fallback_admin_alert(channel: str, error_message: str, payload: TelegramSendPayload) -> None:
    fallback_channels = ["phase4", "phase1"] if channel == "phase1" else ["phase1", "phase4"]
    preview = " ".join(payload.text.split())[:300]
    alert_text = (
        f"[Admin alert] {channel} telegram send failed.\n"
        f"Error: {error_message}\n"
        f"Preview: {preview or '(empty)'}"
    )
    for fallback_channel in fallback_channels:
        bot_token = telegram_bot_token(fallback_channel)
        if not bot_token:
            continue
        try:
            post_telegram_message(
                bot_token,
                TelegramSendPayload(
                    chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
                    text=alert_text,
                    disable_web_page_preview=True,
                ),
            )
            return
        except Exception:
            continue


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


def resolve_workspace_path(raw_path: str) -> Path:
    candidate = Path(unquote(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(PROJECT_ROOT / "runtime")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"message": "path is outside the runtime directory"}) from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail={"message": "file not found"})
    return resolved


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> HTMLResponse:
    html = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>n8n AI Video Dashboard</title>
  <style>
    :root {
      --bg: #f4efe6;
      --panel: rgba(255, 250, 242, 0.92);
      --panel-strong: #fffaf2;
      --line: rgba(92, 58, 33, 0.12);
      --text: #2c2118;
      --muted: #6b5847;
      --accent: #c96f3b;
      --accent-dark: #8b4723;
      --accent-soft: #f6dcc6;
      --ok: #276749;
      --error: #b83232;
      --shadow: 0 18px 50px rgba(83, 48, 18, 0.12);
      --radius: 20px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(255, 214, 170, 0.8), transparent 30%),
        radial-gradient(circle at top right, rgba(240, 171, 98, 0.28), transparent 28%),
        linear-gradient(180deg, #fbf6ef 0%, var(--bg) 100%);
      min-height: 100vh;
    }
    .shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 48px; }
    .hero, .panel {
      background: linear-gradient(135deg, rgba(255,255,255,0.85), rgba(255,244,230,0.95));
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: var(--shadow);
    }
    .hero {
      padding: 28px;
      margin-bottom: 20px;
      position: relative;
      overflow: hidden;
    }
    .hero::after {
      content: "";
      position: absolute;
      inset: auto -40px -70px auto;
      width: 220px;
      height: 220px;
      background: radial-gradient(circle, rgba(201, 111, 59, 0.22), transparent 70%);
      pointer-events: none;
    }
    .eyebrow {
      display: inline-flex;
      padding: 6px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 12px;
    }
    h1 { margin: 0 0 10px; font-size: clamp(28px, 4vw, 46px); line-height: 1.05; }
    h2 { margin: 0 0 14px; font-size: 19px; }
    h3 { margin: 0 0 10px; font-size: 16px; }
    .subtitle { margin: 0; color: var(--muted); max-width: 760px; font-size: 16px; line-height: 1.6; }
    .grid { display: grid; grid-template-columns: 360px minmax(0, 1fr); gap: 20px; }
    .panel { backdrop-filter: blur(10px); }
    .panel-inner { padding: 20px; }
    .stack { display: grid; gap: 14px; }
    .row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .control { display: grid; gap: 8px; }
    label { font-size: 13px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
    input, button, textarea { font: inherit; }
    input, textarea {
      width: 100%;
      border: 1px solid rgba(92, 58, 33, 0.18);
      border-radius: 14px;
      padding: 12px 14px;
      background: rgba(255,255,255,0.82);
      color: var(--text);
    }
    button {
      border: 0;
      border-radius: 14px;
      padding: 12px 14px;
      background: linear-gradient(135deg, var(--accent), #e89a63);
      color: white;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, opacity 160ms ease;
      box-shadow: 0 10px 24px rgba(201, 111, 59, 0.26);
    }
    button.secondary {
      background: rgba(255,255,255,0.92);
      color: var(--accent-dark);
      border: 1px solid rgba(201, 111, 59, 0.25);
      box-shadow: none;
    }
    button:hover { transform: translateY(-1px); }
    button:disabled { opacity: 0.55; cursor: wait; transform: none; }
    .status {
      border-radius: 16px;
      padding: 14px 16px;
      background: rgba(255,255,255,0.72);
      border: 1px solid var(--line);
      color: var(--muted);
      line-height: 1.5;
      min-height: 58px;
    }
    .status.ok { color: var(--ok); border-color: rgba(39, 103, 73, 0.22); background: rgba(240, 255, 244, 0.9); }
    .status.error { color: var(--error); border-color: rgba(184, 50, 50, 0.22); background: rgba(255, 245, 245, 0.92); }
    .results, .post-list, .link-list { display: grid; gap: 14px; }
    .post-list { max-height: 440px; overflow: auto; padding-right: 4px; }
    .post-card {
      border: 1px solid rgba(92, 58, 33, 0.1);
      border-radius: 18px;
      padding: 14px;
      background: rgba(255,255,255,0.75);
      cursor: pointer;
    }
    .post-card.active { border-color: rgba(201, 111, 59, 0.7); background: rgba(255, 244, 230, 0.95); }
    .post-title { margin: 0 0 8px; font-size: 15px; line-height: 1.45; font-weight: 700; }
    .meta { display: flex; gap: 8px; flex-wrap: wrap; color: var(--muted); font-size: 12px; }
    .chip {
      display: inline-flex;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(201, 111, 59, 0.12);
      color: var(--accent-dark);
    }
    .stage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .stage {
      border: 1px solid rgba(92, 58, 33, 0.1);
      border-radius: 18px;
      padding: 16px;
      background: var(--panel-strong);
    }
    .stage p { margin: 0 0 14px; color: var(--muted); font-size: 14px; line-height: 1.5; }
    .output-box {
      border-radius: 18px;
      border: 1px solid rgba(92, 58, 33, 0.1);
      background: rgba(44, 33, 24, 0.96);
      color: #f5ede4;
      padding: 16px;
      min-height: 180px;
      overflow: auto;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: Consolas, "SFMono-Regular", monospace;
      font-size: 12px;
      line-height: 1.55;
    }
    .media-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
    .media-card {
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid rgba(92, 58, 33, 0.12);
      background: rgba(255,255,255,0.88);
    }
    .media-card img, .media-card video, .media-card audio {
      width: 100%;
      display: block;
      background: #1d140e;
    }
    .caption { padding: 10px 12px; color: var(--muted); font-size: 12px; word-break: break-word; }
    a { color: var(--accent-dark); font-weight: 700; text-decoration: none; word-break: break-all; }
    @media (max-width: 980px) {
      .grid, .stage-grid, .row { grid-template-columns: 1fr; }
      .shell { width: min(100% - 20px, 1180px); }
      .hero, .panel-inner { padding: 18px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">Demo UI</div>
      <h1>n8n AI Video Dashboard</h1>
      <p class="subtitle">Giao dien co ban de demo luong tu chon bai Threads den tao video, review link, va file upload. Muc tieu la giup project de nhin, de trinh bay, va de thao tac hon khi bao cao.</p>
    </section>
    <div class="grid">
      <aside class="panel">
        <div class="panel-inner stack">
          <div>
            <h2>1. Chon nguon bai viet</h2>
            <div class="row">
              <button id="autoMinerBtn">Chay Auto Miner</button>
              <button id="searchBtn" class="secondary">Tim Theo Keyword</button>
            </div>
          </div>
          <div class="control">
            <label for="keyword">Keyword demo</label>
            <input id="keyword" placeholder="Vi du: cong nghe, AI, kinh doanh..." />
          </div>
          <div class="control">
            <label for="maxPosts">So bai tim ve</label>
            <input id="maxPosts" type="number" min="1" max="20" value="5" />
          </div>
          <div id="status" class="status">San sang. Chay miner hoac search de lay danh sach bai viet.</div>
          <div>
            <h2>2. Danh sach bai viet</h2>
            <div id="postList" class="post-list"></div>
          </div>
        </div>
      </aside>
      <main class="results">
        <section class="panel">
          <div class="panel-inner stack">
            <div>
              <h2>Bai viet dang chon</h2>
              <div id="selectedPost" class="status">Chua co bai viet nao duoc chon.</div>
            </div>
            <div class="stage-grid">
              <article class="stage"><h3>2. Screenshot + Extract</h3><p>Capture bai viet, trich noi dung, va tao narrator script.</p><button id="phase2Btn">Chay Phase 2</button></article>
              <article class="stage"><h3>3A. Voice</h3><p>Tao audio narration tu narrator script.</p><button id="voiceBtn">Chay Voice</button></article>
              <article class="stage"><h3>3B. Visual</h3><p>Render visual video dua tren screenshots va timing.</p><button id="visualBtn">Chay Visual</button></article>
              <article class="stage"><h3>3C. Merge</h3><p>Ghep audio va visual thanh video cuoi cung.</p><button id="mergeBtn">Chay Merge</button></article>
              <article class="stage"><h3>4A. Review Link</h3><p>Tao draft review link de xem video trong trinh duyet.</p><button id="draftBtn">Tao Review Link</button></article>
              <article class="stage"><h3>4B. Manual Upload Prep</h3><p>Chuan bi file phuc vu upload thu cong len TikTok.</p><button id="manualBtn">Tao Upload Prep</button></article>
            </div>
            <div class="row">
              <button id="runAllBtn">Chay Tu Phase 2 Den Video</button>
              <button id="resetBtn" class="secondary">Reset Ket Qua</button>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-inner stack">
            <div>
              <h2>Tom tat ket qua</h2>
              <div id="summary" class="status">Ket qua tung phase se hien o day.</div>
            </div>
            <div id="links" class="link-list"></div>
            <div>
              <h2>Media preview</h2>
              <div id="mediaPreview" class="media-grid"></div>
            </div>
            <div>
              <h2>Raw output</h2>
              <div class="output-box"><pre id="rawOutput">{}</pre></div>
            </div>
          </div>
        </section>
      </main>
    </div>
  </div>
  <script>
    const state = { posts: [], selectedPost: null, phase2: null, voice: null, visual: null, merge: null, draft: null, manual: null };
    const statusEl = document.getElementById("status");
    const postListEl = document.getElementById("postList");
    const selectedPostEl = document.getElementById("selectedPost");
    const summaryEl = document.getElementById("summary");
    const rawOutputEl = document.getElementById("rawOutput");
    const mediaPreviewEl = document.getElementById("mediaPreview");
    const linksEl = document.getElementById("links");

    function setStatus(message, kind = "") {
      statusEl.textContent = message;
      statusEl.className = "status" + (kind ? " " + kind : "");
    }

    function setSummary(message, kind = "") {
      summaryEl.textContent = message;
      summaryEl.className = "status" + (kind ? " " + kind : "");
    }

    function pretty(value) { return JSON.stringify(value, null, 2); }
    function safeText(value, fallback = "") { return String(value || fallback).trim(); }
    function truncate(value, max = 220) {
      const text = safeText(value);
      return text.length > max ? text.slice(0, max) + "..." : text;
    }
    function tryJson(value) {
      if (!value || typeof value !== "string") return value;
      try { return JSON.parse(value); } catch { return value; }
    }
    function encodeFileUrl(path) { return "/demo/file?path=" + encodeURIComponent(path); }

    function flattenFilePaths(value, output = []) {
      if (!value) return output;
      if (typeof value === "string") {
        if (/\\.(png|jpg|jpeg|webp|mp4|wav|mp3|m4a)$/i.test(value)) output.push(value);
        return output;
      }
      if (Array.isArray(value)) {
        value.forEach(item => flattenFilePaths(item, output));
        return output;
      }
      if (typeof value === "object") Object.values(value).forEach(item => flattenFilePaths(item, output));
      return output;
    }

    function renderPosts() {
      if (!state.posts.length) {
        postListEl.innerHTML = '<div class="status">Chua co du lieu bai viet.</div>';
        return;
      }
      postListEl.innerHTML = "";
      state.posts.forEach(post => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "post-card" + (state.selectedPost && state.selectedPost.id === post.id ? " active" : "");
        const score = Number(post.content_fit_score || 0).toFixed(2);
        const text = truncate(post.full_text || post.text || post.caption || post.url || post.id, 200);
        const source = safeText(post.source, "unknown");
        card.innerHTML = `
          <div class="post-title">${text || "Khong co noi dung tom tat"}</div>
          <div class="meta">
            <span class="chip">ID: ${safeText(post.id, "-")}</span>
            <span class="chip">Fit: ${score}</span>
            <span class="chip">${source}</span>
          </div>
        `;
        card.addEventListener("click", () => {
          state.selectedPost = post;
          renderPosts();
          renderSelectedPost();
          setSummary("Da chon bai viet. Co the chay Phase 2 de bat dau tao asset demo.");
        });
        postListEl.appendChild(card);
      });
    }

    function renderSelectedPost() {
      if (!state.selectedPost) {
        selectedPostEl.textContent = "Chua co bai viet nao duoc chon.";
        return;
      }
      const post = state.selectedPost;
      selectedPostEl.innerHTML = `
        <strong>${safeText(post.id, "Unknown ID")}</strong><br>
        ${truncate(post.full_text || post.text || post.caption || post.url, 320) || "Khong co noi dung mo ta."}<br>
        <a href="${safeText(post.url, "#")}" target="_blank" rel="noreferrer">Mo bai Threads goc</a>
      `;
    }

    function renderRaw() {
      rawOutputEl.textContent = pretty({
        selected_post: state.selectedPost,
        phase2: state.phase2,
        voice: state.voice,
        visual: state.visual,
        merge: state.merge,
        draft: state.draft,
        manual: state.manual
      });
    }

    function renderLinks() {
      const links = [];
      if (state.merge && state.merge.Video_Path) links.push({ label: "Mo video cuoi cung", href: encodeFileUrl(state.merge.Video_Path) });
      if (state.voice && state.voice.Audio_Path) links.push({ label: "Nghe audio narration", href: encodeFileUrl(state.voice.Audio_Path) });
      if (state.visual && state.visual.Visual_Video_Path) links.push({ label: "Xem visual render", href: encodeFileUrl(state.visual.Visual_Video_Path) });
      if (state.draft && state.draft.Draft_Review_URL) links.push({ label: "Mo review link", href: state.draft.Draft_Review_URL });
      if (state.manual && state.manual.Upload_Caption_Path) links.push({ label: "Mo file caption", href: encodeFileUrl(state.manual.Upload_Caption_Path) });
      linksEl.innerHTML = links.map(item => `<a href="${item.href}" target="_blank" rel="noreferrer">${item.label}</a>`).join("");
    }

    function renderMedia() {
      const paths = [
        ...(state.phase2 ? flattenFilePaths(tryJson(state.phase2.Screenshots)) : []),
        ...(state.voice && state.voice.Audio_Path ? [state.voice.Audio_Path] : []),
        ...(state.visual && state.visual.Visual_Video_Path ? [state.visual.Visual_Video_Path] : []),
        ...(state.merge && state.merge.Video_Path ? [state.merge.Video_Path] : [])
      ];
      const unique = [...new Set(paths)].slice(0, 12);
      if (!unique.length) {
        mediaPreviewEl.innerHTML = '<div class="status">Chua co media de preview.</div>';
        return;
      }
      mediaPreviewEl.innerHTML = unique.map(path => {
        const url = encodeFileUrl(path);
        if (/\\.(png|jpg|jpeg|webp)$/i.test(path)) return `<div class="media-card"><img src="${url}" alt="preview"><div class="caption">${path}</div></div>`;
        if (/\\.(wav|mp3|m4a)$/i.test(path)) return `<div class="media-card"><audio src="${url}" controls preload="metadata"></audio><div class="caption">${path}</div></div>`;
        if (/\\.(mp4)$/i.test(path)) return `<div class="media-card"><video src="${url}" controls playsinline preload="metadata"></video><div class="caption">${path}</div></div>`;
        return "";
      }).join("");
    }

    function syncView() {
      renderPosts();
      renderSelectedPost();
      renderRaw();
      renderLinks();
      renderMedia();
    }

    function requirePost() {
      if (state.selectedPost) return true;
      setSummary("Can chon mot bai viet truoc khi chay phase.", "error");
      return false;
    }

    async function apiCall(path, payload) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      const data = await response.json();
      if (!response.ok) {
        const detail = data && data.detail ? data.detail : data;
        throw new Error(typeof detail === "string" ? detail : pretty(detail));
      }
      return data;
    }

    async function withBusy(buttonId, message, work) {
      const button = document.getElementById(buttonId);
      const oldText = button.textContent;
      button.disabled = true;
      setStatus(message);
      try {
        const result = await work();
        setStatus("Hoan thanh: " + message, "ok");
        return result;
      } catch (error) {
        setStatus(error.message || String(error), "error");
        throw error;
      } finally {
        button.disabled = false;
        button.textContent = oldText;
      }
    }

    function resetOutputs() {
      state.phase2 = null;
      state.voice = null;
      state.visual = null;
      state.merge = null;
      state.draft = null;
      state.manual = null;
      setSummary("Da reset ket qua cho bai viet dang chon.");
      syncView();
    }

    document.getElementById("autoMinerBtn").addEventListener("click", async () => {
      await withBusy("autoMinerBtn", "Dang chay auto miner...", async () => {
        const data = await apiCall("/phase1/threads-miner", {});
        state.posts = Array.isArray(data.posts) ? data.posts : [];
        state.selectedPost = state.posts[0] || null;
        resetOutputs();
        setSummary(`Lay duoc ${state.posts.length} bai viet tu auto miner.`, "ok");
      });
    });

    document.getElementById("searchBtn").addEventListener("click", async () => {
      await withBusy("searchBtn", "Dang tim bai viet theo keyword...", async () => {
        const keyword = document.getElementById("keyword").value.trim();
        const maxPosts = Number(document.getElementById("maxPosts").value || 5);
        if (!keyword) throw new Error("Vui long nhap keyword truoc khi search.");
        const data = await apiCall("/phase1/threads-search", { keyword, max_posts: maxPosts });
        state.posts = Array.isArray(data.posts) ? data.posts : [];
        state.selectedPost = state.posts[0] || null;
        resetOutputs();
        setSummary(`Keyword "${keyword}" tra ve ${state.posts.length} bai viet.`, "ok");
      });
    });

    document.getElementById("phase2Btn").addEventListener("click", async () => {
      if (!requirePost()) return;
      await withBusy("phase2Btn", "Dang chay Phase 2...", async () => {
        state.phase2 = await apiCall("/phase2/screenshot-extract", { id: state.selectedPost.id, url: state.selectedPost.url });
        setSummary("Phase 2 xong. Da tao screenshots, extracted content, va narrator script.", "ok");
        syncView();
      });
    });

    document.getElementById("voiceBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      if (!state.phase2 || !state.phase2.Narrator_Script) {
        setSummary("Can chay Phase 2 truoc de co narrator script.", "error");
        return;
      }
      await withBusy("voiceBtn", "Dang tao audio narration...", async () => {
        state.voice = await apiCall("/phase3/voice", {
          id: state.selectedPost.id,
          script: state.phase2.Narrator_Script,
          extracted_content: state.phase2.Extracted_Content || ""
        });
        setSummary("Da tao audio narration.", "ok");
        syncView();
      });
    });

    document.getElementById("visualBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      if (!state.phase2 || !state.phase2.Screenshots) {
        setSummary("Can chay Phase 2 truoc de co screenshots.", "error");
        return;
      }
      await withBusy("visualBtn", "Dang render visual video...", async () => {
        state.visual = await apiCall("/phase3/visual", {
          id: state.selectedPost.id,
          screenshots: state.phase2.Screenshots,
          script: state.phase2.Narrator_Script || "",
          audio_path: state.voice && state.voice.Audio_Path ? state.voice.Audio_Path : "",
          audio_timing: state.voice && state.voice.Audio_Timing ? state.voice.Audio_Timing : "",
          extracted_content: state.phase2.Extracted_Content || ""
        });
        setSummary("Da tao visual render.", "ok");
        syncView();
      });
    });

    document.getElementById("mergeBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      if (!state.voice || !state.voice.Audio_Path || !state.visual || !state.visual.Visual_Video_Path) {
        setSummary("Can co ca audio va visual truoc khi merge.", "error");
        return;
      }
      await withBusy("mergeBtn", "Dang merge video cuoi cung...", async () => {
        state.merge = await apiCall("/phase3/merge", {
          id: state.selectedPost.id,
          audio_path: state.voice.Audio_Path,
          visual_path: state.visual.Visual_Video_Path,
          script: state.phase2 && state.phase2.Narrator_Script ? state.phase2.Narrator_Script : "",
          extracted_content: state.phase2 && state.phase2.Extracted_Content ? state.phase2.Extracted_Content : ""
        });
        setSummary("Da tao video cuoi cung va caption.", "ok");
        syncView();
      });
    });

    document.getElementById("draftBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      if (!state.merge || !state.merge.Video_Path) {
        setSummary("Can co video cuoi cung truoc khi tao review link.", "error");
        return;
      }
      await withBusy("draftBtn", "Dang tao review link...", async () => {
        state.draft = await apiCall("/phase4/draft-review", {
          id: state.selectedPost.id,
          video_path: state.merge.Video_Path,
          caption: state.merge.Caption || ""
        });
        setSummary("Da tao review link de xem va demo video.", "ok");
        syncView();
      });
    });

    document.getElementById("manualBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      if (!state.merge || !state.merge.Video_Path) {
        setSummary("Can co video cuoi cung truoc khi tao upload prep.", "error");
        return;
      }
      await withBusy("manualBtn", "Dang chuan bi upload prep...", async () => {
        state.manual = await apiCall("/phase4/manual-upload-prep", {
          id: state.selectedPost.id,
          video_path: state.merge.Video_Path,
          caption: state.merge.Caption || ""
        });
        setSummary("Da tao file ho tro upload thu cong.", "ok");
        syncView();
      });
    });

    document.getElementById("runAllBtn").addEventListener("click", async () => {
      if (!requirePost()) return;
      await withBusy("runAllBtn", "Dang chay lien tuc tu Phase 2 den merge...", async () => {
        state.phase2 = await apiCall("/phase2/screenshot-extract", { id: state.selectedPost.id, url: state.selectedPost.url });
        syncView();
        state.voice = await apiCall("/phase3/voice", {
          id: state.selectedPost.id,
          script: state.phase2.Narrator_Script || "",
          extracted_content: state.phase2.Extracted_Content || ""
        });
        syncView();
        state.visual = await apiCall("/phase3/visual", {
          id: state.selectedPost.id,
          screenshots: state.phase2.Screenshots,
          script: state.phase2.Narrator_Script || "",
          audio_path: state.voice.Audio_Path || "",
          audio_timing: state.voice.Audio_Timing || "",
          extracted_content: state.phase2.Extracted_Content || ""
        });
        syncView();
        state.merge = await apiCall("/phase3/merge", {
          id: state.selectedPost.id,
          audio_path: state.voice.Audio_Path,
          visual_path: state.visual.Visual_Video_Path,
          script: state.phase2.Narrator_Script || "",
          extracted_content: state.phase2.Extracted_Content || ""
        });
        setSummary("Da chay xong tu Phase 2 den video cuoi cung.", "ok");
        syncView();
      });
    });

    document.getElementById("resetBtn").addEventListener("click", () => resetOutputs());
    syncView();
  </script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/demo/file")
def demo_file(path: str) -> FileResponse:
    file_path = resolve_workspace_path(path)
    return FileResponse(str(file_path))


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


@app.post("/telegram/send/{channel}")
def telegram_send(channel: str, payload: TelegramSendPayload) -> dict[str, object]:
    if channel not in {"phase1", "phase4"}:
        raise HTTPException(status_code=400, detail={"message": "channel must be phase1 or phase4"})

    bot_token = telegram_bot_token(channel)
    try:
        result = post_telegram_message(bot_token, payload)
        return {
            "ok": True,
            "sent": True,
            "channel": channel,
            "chat_id": payload.chat_id.strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            "result": result,
        }
    except Exception as exc:
        fallback_admin_alert(channel, str(exc), payload)
        return {
            "ok": False,
            "sent": False,
            "channel": channel,
            "chat_id": payload.chat_id.strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            "error": str(exc),
        }


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
    attached_posts = attach_phase1_context(posts, trend_signal, source="auto")
    top_limit = max(1, min(10, int_env("THREADS_PHASE1_TOP_RESULTS", 1)))
    ranked_posts = rank_phase1_posts(dedupe_posts(attached_posts), limit=top_limit)
    return {
        "posts": ranked_posts,
        "trend_signal": trend_signal,
        "post_count": len(ranked_posts),
        "raw_post_count": len(posts),
    }


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
    top_limit = max(1, min(10, int_env("THREADS_SEARCH_TOP_RESULTS", 1)))
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
