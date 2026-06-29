# n8n-ai-video

An automated pipeline that converts Vietnamese Threads posts into short-form narrated videos for TikTok, Reels, and Shorts.

```
Threads post → mine & filter → screenshot & extract → voice & render → review & publish
```

**Output:** `1080×1920` MP4 with Vietnamese TTS narration, visual overlay, and AI-generated caption.

---

## Table of Contents

1. [How It Works](#1-how-it-works)
2. [Architecture](#2-architecture)
3. [Tech Stack](#3-tech-stack)
4. [Setup & Running](#4-setup--running)
5. [Environment Variables](#5-environment-variables)
6. [Directory Structure](#6-directory-structure)
7. [Google Sheets Schema](#7-google-sheets-schema)
8. [Daily Operations](#8-daily-operations)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. How It Works

The pipeline runs in four sequential phases. Each phase is orchestrated by an n8n workflow and executed by the Python runner service.

| Phase | Purpose | Input | Output |
|---|---|---|---|
| **1. Threads Miner** | Discover and score posts worth turning into videos | Threads feed, trend signals, Gemini classifier | New row in Google Sheets with `Status = Pending` |
| **2. Screenshot + Extract** | Capture post/comments and produce a narration script | `Source_URL` from Google Sheets | `Screenshots`, `Extracted_Content`, `Narrator_Script` |
| **3. Video Maker** | Generate audio, visual overlay, final video, and caption | `Narrator_Script`, screenshots, media config | `Audio_Path`, `Visual_Video_Path`, `Video_Path`, `Caption` |
| **4. Review + Publish** | Send draft for human review, then publish on approval | Video draft, Telegram bot, review link | `Draft_Video_URL`, `Admin_Decision`, `Published_URL` |

---

## 2. Architecture

### Components

```
n8n
  → workflow orchestration, scheduling, Google Sheets read/write,
    Gemini nodes, HTTP calls to runner

runner (FastAPI + uvicorn)
  → Python worker exposing REST endpoints for each phase
  → runs Playwright, ffmpeg, VieNeu, yt-dlp
  → serves review pages and handles Telegram callbacks

Google Sheets
  → central state store for the entire pipeline

Cloudflare Tunnel
  → exposes runner to the Internet for review links and Telegram webhooks

Telegram Bot
  → receives search commands, sends review links, receives approve/reject decisions

TikTok
  → final publish destination
```

### Data Flow

```
Threads → Phase 1 (runner) → Google Sheets → Phase 2 (runner)
        → Phase 3 (runner) → Phase 4 (runner) → TikTok
```

### Phase ↔ File Mapping

| Phase | n8n Workflow | Python Files |
|---|---|---|
| 1 | `workflows/01-threads-miner.json` | `src/threads_miner.py`, `src/trend_signal.py` |
| 2 | `workflows/02-screenshot-extract.json` | `src/screenshot_extractor.py` |
| 3 | `workflows/03-video-maker.json` | `src/video_factory.py` |
| 4 | `workflows/04-review&publish.json` | `src/phase4_compact_helper.py`, `src/draft_review_helper.py`, `src/local_review.py`, `src/tiktok_playwright_publisher.py`, `src/tiktok_publisher.py`, `src/manual_upload_helper.py` |

### Docker Services

| Service | Role | Port |
|---|---|---|
| `n8n` | Workflow orchestration | `5678` |
| `runner` | Python worker + review server | `8000` |

### Runner Endpoints

```
GET  /health
GET  /review/{post_id}
GET  /review/{post_id}/video
GET  /review/{post_id}/download

POST /phase1/threads-miner
POST /phase1/threads-search
POST /phase1/telegram-search-callback
POST /phase1/noop

POST /phase2/screenshot-extract

POST /phase3/voice
POST /phase3/visual
POST /phase3/merge

POST /phase4/draft-review
POST /phase4/compact
POST /phase4/publish
POST /phase4/manual-upload-prep
POST /phase4/telegram-callback

POST /telegram/send/{channel}
```

---

## 3. Tech Stack

### Full Component List

| Component | Used For |
|---|---|
| `n8n` | Orchestrates all 4 phases — scheduling, triggers, Sheets nodes, Gemini nodes, HTTP calls to runner |
| Docker Compose | Runs `n8n` and `runner` in a shared workspace; mounts source code, runtime folders, and secrets |
| `FastAPI` | Local API server (`runner/app.py`) that n8n calls for each phase |
| `uvicorn` | ASGI server for the FastAPI runner |
| Python `3.11` | Primary runtime for all scripts in `src/` and `runner/` |
| `python-dotenv` | Loads environment variables from `.env` |
| `Playwright` | Browser automation for Threads scraping, screenshot capture, and TikTok upload |
| Chromium | Browser used by Playwright for login, post rendering, and screenshots |
| `requests` | HTTP calls to external APIs and webhook callbacks from Python |
| `Gemini` | AI classifier (Phase 1), script rewriter (Phase 2), caption generator (Phase 3) |
| Google News RSS | Trend signal source for Phase 1 — no API key required |
| Google Sheets | Central pipeline state store: paths, scripts, captions, publish results |
| `google-api-python-client` | Google APIs access from Python helpers |
| `google-auth` | Service account authentication for Google APIs |
| `VieNeu` | Vietnamese text-to-speech for Phase 3 narration |
| `ffmpeg` | Audio normalization, video merging, final MP4 encoding |
| `imageio-ffmpeg` | Stable ffmpeg binary resolution from Python |
| `Pillow` | Overlay card generation and image preprocessing before visual render |
| `yt-dlp` | Downloads background videos or media assets from YouTube |
| `tiktok-uploader` | Browser-based TikTok upload support |
| TikTok Content Posting API | API-based publish path when full OAuth/app credentials are available |
| Telegram Bot API | Receives search commands, sends review links, handles approve/reject callbacks |
| Cloudflare Tunnel (`cloudflared`) | Exposes local runner to the Internet for Telegram webhooks and review links |
| `fonts-liberation`, `fonts-noto-*`, `fonts-noto-color-emoji` | Stable Vietnamese text and emoji rendering inside the container |
| `espeak-ng` | Audio/TTS support dependency in the runner environment |
| Chromium system libs | Linux libraries required for Playwright/Chromium inside the container |

### Tech Stack by Phase

| Phase | Technologies |
|---|---|
| 1. Threads Miner | Playwright, Chromium, Google News RSS, Gemini, Google Sheets |
| 2. Screenshot + Extract | Playwright, Chromium, Gemini, Google Sheets |
| 3. Video Maker | VieNeu, ffmpeg, imageio-ffmpeg, Pillow, Gemini |
| 4. Review + Publish | FastAPI, Telegram Bot API, Cloudflare Tunnel, Google Sheets, tiktok-uploader, Playwright, TikTok API |

---

## 4. Setup & Running

The recommended way to run this project is via Docker. The runner image ships with Chromium, ffmpeg, Playwright, VieNeu, and all Python dependencies pre-installed.

### Prerequisites

| Requirement | Notes |
|---|---|
| OS | Windows with Docker Desktop (current setup). macOS/Linux should work with adjusted mount paths. |
| Docker | Docker Desktop + Docker Compose plugin |
| RAM | 8 GB minimum; 16 GB recommended for Chromium + n8n + video rendering |
| Disk | Sufficient space for `runtime/` — screenshots, audio, visuals, and final videos accumulate quickly |
| Internet | Required for Threads, Gemini, Telegram, TikTok, Cloudflare, and Google APIs |
| Cloudflare Tunnel | Required if using public review links or Telegram webhooks |

> **Running outside Docker (for debugging):** You will additionally need Python 3.11, `ffmpeg` in `PATH`, and Playwright Chromium (`python -m playwright install chromium`). Node.js is not required unless developing n8n plugins or JS tooling.

### Step 1 — Clone the repository

```powershell
git clone <your-repo-url>
cd n8n-ai-video
```

### Step 2 — Create your `.env` file

```powershell
Copy-Item .env.example .env
```

### Step 3 — Fill in secrets and credentials

Open `.env` and configure the following before starting:

- `THREADS_USERNAME` and `THREADS_PASSWORD`
- `GOOGLE_SHEET_ID` — from your Google Sheet URL
- `GEMINI_API_KEY` — from Google AI Studio
- Google service account JSON — place it at the path referenced in `docker-compose.yml`
- `TELEGRAM_PHASE1_BOT_TOKEN` and `TELEGRAM__PHASE4_BOT_TOKEN` — if using search/review bots
- TikTok session or API credentials — if publishing to TikTok
- `REVIEW_PUBLIC_BASE_URL` — your Cloudflare Tunnel hostname, or `http://localhost:8000` for local testing

See [Section 5 — Environment Variables](#5-environment-variables) for the full reference.

### Step 4 — Fix mount paths in `docker-compose.yml`

The repo ships with Windows-specific mount paths. Update these two lines to match your environment if needed:

```yaml
- ../project-iii-492308-fb6a394893e0.json:/workspace/google-service-account.json:ro
- E:/n8n-ai-video-cache:/models
```

### Step 5 — Create the Docker volume for n8n

`docker-compose.yml` expects an external volume named `n8n_data`. On a fresh machine, create it once:

```powershell
docker volume create n8n_data
```

### Step 6 — Build and start

```powershell
docker compose up -d --build
```

### Step 7 — Verify services are running

```powershell
# Open n8n
start http://localhost:5678

# Health check the runner
curl http://localhost:8000/health

# Tail logs
docker compose logs -f runner
docker compose logs -f n8n
```

### Step 8 — Import workflows into n8n

In the n8n UI, import the following four workflow files from `workflows/`:

```
01-threads-miner.json
02-screenshot-extract.json
03-video-maker.json
04-review&publish.json
```

### Step 9 — Configure public review/search callbacks (Telegram only)

Skip this step if you are testing locally without Telegram integration. For Telegram search, review buttons, or public review links, complete the following:

**1. Start a public Cloudflare Tunnel:**

```powershell
cloudflared tunnel --url http://localhost:8000
```

**2.** Set `REVIEW_PUBLIC_BASE_URL` in `.env` to the assigned public HTTPS hostname.

**3.** Ensure the n8n callback URLs in `.env` point to the correct webhook routes:

```env
N8N_PHASE1_SEARCH_CALLBACK_URL=http://n8n:5678/webhook/phase1-search-callback
N8N_PHASE4_CALLBACK_URL=http://n8n:5678/webhook/phase4-review-callback
```

**4.** Register Telegram webhooks pointing to the runner:

```powershell
curl "https://api.telegram.org/bot<TELEGRAM_PHASE1_BOT_TOKEN>/setWebhook?url=https://your-hostname.example.com/phase1/telegram-search-callback"
curl "https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/setWebhook?url=https://your-hostname.example.com/phase4/telegram-callback"
```

**5.** Verify webhook registration:

```powershell
curl "https://api.telegram.org/bot<TELEGRAM_PHASE1_BOT_TOKEN>/getWebhookInfo"
curl "https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/getWebhookInfo"
```

### Step 10 — Run the pipeline

Trigger phases in order from the n8n UI or their respective schedule triggers:

1. **Phase 1** — mines Threads and writes `Pending` rows to Google Sheets
2. **Phase 2** — captures screenshots and generates `Narrator_Script`
3. **Phase 3** — produces audio, visual overlay, final video, and caption
4. **Phase 4** — sends a review link via Telegram; publishes on approval

---

## 5. Environment Variables

All variables are loaded from `.env`. Start by copying `.env.example`.

This section covers the variables most commonly needed during initial setup and daily operation. The canonical full list — including low-level tuning flags, timeout values, and advanced publish options — lives in `.env.example`.

### Threads Account

| Variable | Purpose | Source |
|---|---|---|
| `THREADS_USERNAME` | Login for Threads scraping and screenshots | Your Threads account |
| `THREADS_PASSWORD` | Login password | Your Threads account |
| `THREADS_STORAGE_STATE` | Local browser session file | Default: `runtime/storage/threads-state.json` |
| `THREADS_DEBUG_DIR` | HTML/PNG debug dumps on browser failure | Default: `runtime/debug` |
| `THREADS_FORCE_LOGIN` | Force re-login instead of reusing session | Set `true` when the session expires |

### Phase 1 — Mining & Filtering

| Variable | Purpose |
|---|---|
| `MAX_POSTS`, `SCROLL_COUNT`, `CANDIDATE_LIMIT` | Control scraping volume |
| `MIN_ENGAGEMENT_SCORE`, `MIN_STRONGEST_METRIC_SCORE`, `MIN_CONTENT_FIT_SCORE` | Post filtering thresholds |
| `THREADS_CLASSIFIER_ENABLED` | Enable Gemini classifier |
| `THREADS_CLASSIFIER_MODEL` | Gemini model for Phase 1 |
| `TREND_SIGNAL_ENABLED` | Enable RSS trend signals |
| `TREND_RSS_URLS` | Custom RSS feed URLs |
| `THREADS_SEARCH_MAX_POSTS` | Max posts per bot search |

Additional tuning from `.env.example`:

| Variable | Purpose |
|---|---|
| `THREADS_TIMEOUT_MS`, `THREADS_POST_LOGIN_WAIT_MS` | Browser timeout and post-login wait behavior |
| `THREADS_MINER_TIMEOUT_SECONDS`, `THREADS_SEARCH_TIMEOUT_SECONDS` | Runner timeouts for Phase 1 jobs |
| `TREND_TOPICS_LIMIT`, `TREND_CONTEXT_LIMIT` | How much RSS trend context is attached to each post |
| `THREADS_TREND_KEYWORDS_LIMIT`, `THREADS_SWEEP_SCROLL_COUNT`, `THREADS_SWEEP_QUERIES` | Extra mining/search controls |

### Runtime Folders

| Variable | Default Purpose |
|---|---|
| `SCREENSHOTS_DIR` | Screenshot output |
| `AUDIO_DIR` | WAV/audio output |
| `VISUALS_DIR` | Visual MP4 and overlay output |
| `VIDEOS_DIR` | Final MP4 output |
| `TEMP_DIR` | Temporary files |
| `EXPORTS_DIR` | Manual export files |

All runtime folder variables have sensible defaults. Leave them unchanged unless you need custom paths.

### Google Sheets

| Variable | Purpose | Source |
|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Service account JSON path inside container | Google Cloud — download the key JSON |
| `GOOGLE_SHEET_ID` | Target spreadsheet | From your Google Sheet URL |
| `GOOGLE_SHEET_TAB` | Sheet tab name | Default: `Threads` |

**Setup:** Create a service account in Google Cloud → download the JSON key → share your Sheet with the service account email as **Editor** → set `GOOGLE_SERVICE_ACCOUNT_FILE=/workspace/google-service-account.json`.

### Telegram & n8n Callbacks

| Variable | Purpose | Source |
|---|---|---|
| `TELEGRAM__PHASE4_BOT_TOKEN` | Review/publish bot | BotFather |
| `TELEGRAM_PHASE1_BOT_TOKEN` | Search bot | BotFather |
| `TELEGRAM_CHAT_ID` | Admin chat for review notifications | Telegram `/getUpdates` |
| `N8N_PHASE1_SEARCH_CALLBACK_URL` | Runner → n8n callback for Phase 1 | Webhook URL from n8n workflow |
| `N8N_PHASE2_START_URL` | Trigger Phase 2 | Webhook URL from n8n workflow |
| `N8N_PHASE3_START_URL` | Trigger Phase 3 | Webhook URL from n8n workflow |
| `N8N_PHASE4_START_URL` | Trigger Phase 4 | Webhook URL from n8n workflow |
| `N8N_PHASE4_CALLBACK_URL` | Runner → n8n callback for review/publish | Webhook URL from n8n workflow |

### Review Server & Cloudflare

| Variable | Purpose |
|---|---|
| `REVIEW_PUBLIC_BASE_URL` | Base URL for review links — Cloudflare hostname, or `http://localhost:8000` for local testing |
| `REVIEW_TOKEN_SECRET` | Secret for signing review tokens — use a long random string |

### Gemini / AI

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | API key for all Gemini nodes |
| `SCRIPT_REWRITE_ENABLED` | Enable Phase 2 script rewriting |
| `SCRIPT_REWRITE_API_KEY` | Separate key if using a different account for rewrite |
| `SCRIPT_REWRITE_MODEL` | Gemini model for script rewriting |
| `SCRIPT_REWRITE_BASE_URL` | Gemini endpoint (leave as default) |
| `AI_CAPTION_ENABLED` | Enable AI caption generation in Phase 3 |
| `AI_CAPTION_MODEL` | Gemini model for captions |

Additional tuning:

| Variable | Purpose |
|---|---|
| `SCRIPT_REWRITE_TIMEOUT_MS` | Timeout for script rewrite requests |

### TTS / VieNeu

| Variable | Purpose |
|---|---|
| `TTS_ENGINE_ORDER` | TTS engine priority — set to `vieneu` |
| `TTS_VOICE` | Default voice |
| `TTS_AUTHOR_VOICES` | Per-author voice rotation — `preset:<voice_id>` or `ref:<audio_path>` |
| `TTS_DISCUSSION_VOICES` | Voices for dialogue segments |
| `VIENEU_TTS_ENABLED` | Enable VieNeu — set `true` |
| `VIENEU_MODE` | VieNeu operating mode |
| `VIENEU_BACKBONE_REPO`, `VIENEU_CODEC_REPO` | Model repositories |
| `VIENEU_BACKBONE_DEVICE`, `VIENEU_CODEC_DEVICE` | Inference device |
| `VIENEU_VOICE_REF` | Reference audio file for voice cloning |
| `VIENEU_VOICE_REF_TEXT` | Transcript matching the reference audio |
| `HF_HOME`, `HUGGINGFACE_HUB_CACHE` | Model cache location |

### Background Video, Music & Layout

| Variable | Purpose |
|---|---|
| `BACKGROUND_VIDEO_PATH` | Single fixed background video |
| `BACKGROUND_VIDEO_DIR` | Directory of background videos to pick from |
| `BACKGROUND_VIDEO_PICK` | Selection strategy: `hash`, `random`, or `first` |
| `BACKGROUND_MUSIC_*` | Background music file or folder |
| `VIDEO_WIDTH`, `VIDEO_HEIGHT` | Output resolution — default `1080×1920` |
| `OVERLAY_*`, `VISUAL_TIMING_LEAD_SECONDS` | Overlay layout and timing |
| `VIDEO_*`, `AUDIO_BITRATE` | Encode quality settings |

### TikTok Publishing

| Variable | Purpose | Source |
|---|---|---|
| `TIKTOK_PUBLISHER_MODE` | `playwright` (browser) or API-based upload | Your choice |
| `TIKTOK_DRY_RUN` | Test publish without actually posting | Set `false` when ready to go live |
| `TIKTOK_SESSION_ID` | Browser session for Playwright publisher | Your TikTok account |
| `TIKTOK_USERNAME`, `TIKTOK_PASSWORD` | Fallback login credentials | Your TikTok account |
| `TIKTOK_UPLOADER_COOKIES_PATH` | Cookies file for browser uploader | Exported from an active session |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` | TikTok API app credentials | TikTok developer portal |
| `TIKTOK_ACCESS_TOKEN`, `TIKTOK_REFRESH_TOKEN` | OAuth tokens | TikTok OAuth flow |

Additional tuning from `.env.example`:

| Variable | Purpose |
|---|---|
| `TIKTOK_COVER_DIR`, `TIKTOK_COVER_TIMESTAMP_MS` | Cover image generation behavior |
| `TIKTOK_UPLOAD_BROWSER`, `TIKTOK_UPLOAD_HEADLESS`, `TIKTOK_UPLOAD_RETRIES` | Browser upload runtime behavior |
| `TIKTOK_SKIP_SPLIT_WINDOW` | Upload UI compatibility toggle |
| `TIKTOK_PRIVACY_LEVEL`, `TIKTOK_DISABLE_DUET`, `TIKTOK_DISABLE_COMMENT`, `TIKTOK_DISABLE_STITCH` | Publish flags for the final post |

---

## 6. Directory Structure

```
n8n-ai-video/
├── .codex/                             Internal skills for Codex tooling
├── runner/
│   ├── app.py                          FastAPI server — all phase and review endpoints
│   ├── Dockerfile                      Runner image: Python 3.11, Chromium, ffmpeg, Playwright
│   └── requirements.txt                Python dependencies
├── runtime/                            Generated data — do not commit
│   ├── assets/                         Background videos, music, voice references
│   ├── data/                           Screenshots, audio, visuals, final videos
│   ├── storage/                        Browser session and state files
│   └── debug/                          HTML/PNG/log dumps from failed crawls
├── scripts/
│   ├── cleanup_runtime.py              Removes old runtime files
│   └── quality_check.py               Validates video output after render
├── src/
│   ├── threads_miner.py                Phase 1 — Threads scraping and scoring
│   ├── trend_signal.py                 Phase 1 — RSS trend context
│   ├── screenshot_extractor.py         Phase 2 — screenshot capture and text extraction
│   ├── video_factory.py                Phase 3 — TTS, visual render, merge
│   ├── phase4_compact_helper.py        Phase 4 — compact review/publish flow
│   ├── draft_review_helper.py          Phase 4 — signed review link generation
│   ├── local_review.py                 Phase 4 — review token signing and verification
│   ├── manual_upload_helper.py         Phase 4 — manual upload support
│   ├── tiktok_playwright_publisher.py  Phase 4 — browser-based TikTok publish
│   ├── tiktok_publisher.py             Phase 4 — API-based TikTok publish
│   └── tiktok_auth.py                  Phase 4 — TikTok API auth helper
├── workflows/
│   ├── 01-threads-miner.json
│   ├── 02-screenshot-extract.json
│   ├── 03-video-maker.json
│   └── 04-review&publish.json
├── .env.example                        Environment variable template
├── docker-compose.yml                  Defines n8n and runner services
└── README.md
```

> `runtime/` is generated output, not source code. Never commit `runtime/data/`, cookies, secrets, or credential JSON files.

---

## 7. Google Sheets Schema

Each row represents one Threads post moving through the pipeline.

| Column | Description |
|---|---|
| `ID` | Unique post identifier |
| `Source_URL` | Original Threads post URL |
| `Source` | Source label |
| `Collected_At` | Timestamp when post was mined |
| `Status` | Current pipeline stage |
| `Screenshots` | Screenshot file paths (Phase 2 output) |
| `Extracted_Content` | Raw extracted post content |
| `Narrator_Script` | Final narration script |
| `Audio_Path` | Path to generated WAV file |
| `Audio_Timing` | Per-segment timing data |
| `Visual_Video_Path` | Path to visual overlay video |
| `Video_Path` | Path to final merged MP4 |
| `Caption` | AI-generated TikTok caption |
| `Draft_Video_URL` | Review link sent to admin |
| `Draft_Drive_File_ID` | Google Drive file ID if applicable |
| `Admin_Decision` | `Approved` or `Rejected` |
| `TikTok_Publish_ID` | TikTok post ID after publish |
| `Published_URL` | Live TikTok URL |
| `Note` | Manual notes or error messages |

**Minimum columns required before running the workflows:**

```
ID, Source_URL, Source, Collected_At, Status,
Screenshots, Extracted_Content, Narrator_Script,
Audio_Path, Audio_Timing, Visual_Video_Path, Video_Path,
Caption, Draft_Video_URL, Draft_Drive_File_ID,
Admin_Decision, TikTok_Publish_ID, Published_URL, Note
```

**Implementation notes:**

- `Screenshots`, `Extracted_Content`, and `Audio_Timing` are JSON strings stored in flat Sheets cells.
- `Audio_Path`, `Visual_Video_Path`, and `Video_Path` are local runner file paths, not public URLs.
- Renaming columns in the sheet requires updating the corresponding mappings in n8n.

**Status flow:**

```
Pending → In Progress → Draft → Approved / Rejected → Published / Failed
```

---

## 8. Daily Operations

```powershell
# Start all services
docker compose up -d

# Rebuild and restart all services
docker compose up -d --build

# Tail logs
docker compose logs -f runner
docker compose logs -f n8n

# Rebuild runner only (faster when only Python code changed)
docker compose build runner && docker compose up -d runner

# Run quality check on video output
docker compose exec -T runner python scripts/quality_check.py

# Clean up runtime files older than 14 days
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14
```

---

## 9. Troubleshooting

### `docker compose up` fails due to mount path errors

The repo ships with Windows-specific paths for the service account JSON and model cache. Update those paths in `docker-compose.yml` to match your machine, then re-run `docker compose up -d --build`.

### Runner starts but `/health` returns an error

The runner likely crashed on startup. Check `docker compose logs -f runner` for the cause. Common culprits: malformed `.env` values, incorrect Google service account JSON path, or missing required variables.

### Threads login fails or screenshots are wrong

The session stored at `runtime/storage/threads-state.json` may have expired. Set `THREADS_FORCE_LOGIN=true` to force a fresh login, or delete the session file manually. Check `runtime/debug/` for HTML/PNG dumps and verify `THREADS_USERNAME` and `THREADS_PASSWORD`.

### Playwright / Chromium errors during crawl

Running inside Docker is strongly recommended — the runner image ships with all Chromium dependencies pre-installed. If running locally, install the browser with `python -m playwright install chromium` and ensure `ffmpeg` is in `PATH`.

### Google Sheets read/write fails

Verify that `GOOGLE_SHEET_ID` matches the URL of your sheet, the service account email has **Editor** access to that sheet, and the JSON file is mounted correctly at `/workspace/google-service-account.json`.

### Gemini returns empty results or errors

Check that `GEMINI_API_KEY` is set and valid. Confirm the model names in `.env` are currently available in the Gemini API. Review the Gemini node execution log in n8n for the exact error message.

### Video render fails or final video has no audio

Verify `BACKGROUND_VIDEO_PATH` or `BACKGROUND_VIDEO_DIR` points to an existing file. Confirm `Audio_Path`, `Visual_Video_Path`, and `Video_Path` are populated in Google Sheets before Phase 3 runs. Run `scripts/quality_check.py` to inspect the output.

### VieNeu produces no audio

Ensure `VIENEU_TTS_ENABLED=true` and `TTS_ENGINE_ORDER=vieneu`. Verify that model cache paths are mounted correctly and `VIENEU_VOICE_REF` points to an existing audio file.

### Telegram callbacks are not received

`REVIEW_PUBLIC_BASE_URL` must be a publicly accessible HTTPS URL — a Cloudflare Tunnel hostname, not `localhost`. Ensure `cloudflared tunnel --url http://localhost:8000` is running, and that `N8N_PHASE1_SEARCH_CALLBACK_URL` and `N8N_PHASE4_CALLBACK_URL` reference the correct n8n webhook URLs.

### TikTok publish fails

Session cookies may have expired. Re-export cookies or set a fresh `TIKTOK_SESSION_ID`. Confirm `TIKTOK_DRY_RUN=false` if you intend to publish. Note that the Playwright-based upload path may occasionally require manual intervention due to TikTok CAPTCHAs or UI changes.

### `runtime/` grows too large

Screenshots, audio files, and videos accumulate across runs. Use `scripts/cleanup_runtime.py --days N` to remove files older than N days. Do not delete files that are still referenced by active rows in Google Sheets.