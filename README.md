# n8n threads video

Automated pipeline for turning Vietnamese Threads posts into short-form videos for TikTok, YouTube Shorts, Reels, or similar platforms.

The project mines discussion-worthy Threads posts, captures the post and selected comments, generates Vietnamese narration, renders a vertical video over reusable background clips, and sends drafts through a human review step before publishing.

## Pipeline

```text
Phase 1: Threads Miner
  -> finds candidate Threads posts
  -> writes Pending rows to Google Sheets

Phase 2: Screenshot + Extract
  -> captures post/comment screenshots
  -> cleans extracted text into Narrator_Script
  -> sets rows to In Progress

Phase 3: Video Maker
  -> generates voiceover
  -> renders screenshot cards over a background video
  -> merges audio and visual into a final MP4
  -> sets rows to Draft

Phase 4: Review + Publish
  -> sends draft link to Telegram
  -> waits for approve/reject
  -> publishes approved videos through the TikTok browser uploader
```

## Tech Spec

- Automation: n8n running in Docker.
- Runner API: FastAPI service in `runner/`, mounted with the repo as `/workspace`.
- Browser automation: Playwright for Threads capture and TikTok upload.
- Data source: Google Sheets tab `Threads`.
- AI: Gemini nodes in n8n for post filtering, script cleanup, and captions.
- TTS chain: VieNeu local TTS, then FPT.AI, edge-tts, gTTS, Windows SAPI, then silent fallback.
- Video rendering: ffmpeg and MoviePy.
- Background downloader: `yt-dlp`.
- Review channel: Telegram inline buttons plus Google Drive draft links.
- Runtime paths: generated assets live under `runtime/` and are intentionally not committed.

## Repository Layout

```text
n8n-ai-video/
  docs/                         Project notes and phase docs
  runner/                       FastAPI runner service
  src/                          Python automation and rendering code
  workflows/                    n8n workflow exports
  runtime/
    assets/                     Local background videos and reusable assets
    data/                       Generated screenshots, audio, visuals, videos
    storage/                    Browser state and local runtime storage
  docker-compose.yml
  .env.example
```

## Requirements

- Docker Desktop.
- A Threads account for mining and screenshots.
- Google Sheets credentials.
- Gemini API key for AI filtering/rewriting/captioning.
- Optional FPT.AI key for paid Vietnamese TTS.
- Optional Telegram bot and Google Drive credentials for Phase 4 review.
- Optional TikTok account cookies/session for browser publishing.

## Setup

1. Clone the repo and enter the project directory:

```powershell
git clone https://github.com/HoaNgo18/n8n-ai-video.git
cd "D:\Project III\n8n-ai-video"
```

2. Create your local environment file:

```powershell
Copy-Item .env.example .env
```

3. Fill `.env` with the required values. Keep `.env` private.

Important values:

```env
THREADS_USERNAME=
THREADS_PASSWORD=
GOOGLE_SHEET_ID=
GOOGLE_SERVICE_ACCOUNT_FILE=/workspace/google-service-account.json
GEMINI_API_KEY=
VIENEU_TTS_ENABLED=true
BACKGROUND_VIDEO_DIR=runtime/assets/backgrounds
BACKGROUND_VIDEO_PICK=hash
```

4. Configure the Google service account mount.

`docker-compose.yml` mounts the service-account JSON into the runner container. Either place the JSON at the host path used by the compose file, or edit the mount to match your real local path. Do not commit the JSON key.

5. Start the stack:

```powershell
docker compose up -d --build
```

6. Open n8n:

```text
http://localhost:5678
```

7. Import or update the workflow JSON files from `workflows/`.

Current local exports include:

```text
workflows/01-threads-miner.json
workflows/02-screenshot-extract.json
workflows/03-video-maker.json
workflows/04-review&publish.json
```

Phase 4 uses the runner helper endpoints plus Google Drive and Telegram nodes for review/publish callbacks.

## Google Sheet

Use a tab named `Threads`. The important columns are:

```text
ID
Source_URL
Source_Text
Author
Status
Extracted_Content
Narrator_Script
Post_Screenshot_Path
Comment_Screenshots
Audio_Path
Audio_Timing
Visual_Video_Path
Video_Path
Caption
Draft_Video_URL
Draft_Drive_File_ID
Admin_Decision
TikTok_Publish_ID
Published_URL
Note
Created_At
Updated_At
```

See `docs/02-database-schema.md` for the fuller schema.

## Running The Pipeline

Start services:

```powershell
docker compose up -d
```

Check runner health:

```powershell
docker compose exec -T runner python -c "import requests; print(requests.get('http://localhost:8000/health').text)"
```

Run Phase 1 in n8n to mine candidates. Valid posts are written to the sheet with `Status = Pending`.

Run Phase 2 in n8n to capture screenshots and clean text. It updates rows to `Status = In Progress`.

Run Phase 3 in n8n to generate audio, render visuals, merge final video, and create a caption. It updates rows to `Status = Draft`.

Run Phase 4 in n8n to send a review link to Telegram, process approve/reject callbacks, and publish approved drafts.

Useful logs:

```powershell
docker compose logs -f runner
docker compose logs -f n8n
```

Rebuild only the runner after Python dependency changes:

```powershell
docker compose build runner
docker compose up -d runner
```

## Ngrok For Telegram Callbacks

Use ngrok when Telegram needs to call your local n8n webhook.

1. Start n8n locally:

```powershell
docker compose up -d
```

2. Start an ngrok tunnel to n8n:

```powershell
ngrok http 5678
```

3. Copy the HTTPS forwarding URL, for example:

```text
https://example.ngrok-free.app
```

4. In n8n, use the production webhook URL for the Telegram callback node. For Phase 4 the path is usually:

```text
https://example.ngrok-free.app/webhook/phase4-review-callback
```

5. Set the Telegram bot webhook:

```powershell
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://example.ngrok-free.app/webhook/phase4-review-callback"
```

For n8n test mode, use the temporary `/webhook-test/...` URL shown by the Webhook node while it is listening.

## Background Videos

Put reusable background clips here:

```text
runtime/assets/backgrounds/
```

Supported extensions:

```text
.mp4 .mov .mkv .webm
```

Selection mode is controlled by `.env`:

```env
BACKGROUND_VIDEO_DIR=runtime/assets/backgrounds
BACKGROUND_VIDEO_PICK=hash
```

Modes:

- `hash`: stable choice per post ID.
- `random`: different choice on each render.
- `first`: first file alphabetically.

Download a YouTube video into the background folder:

```powershell
docker compose exec -T runner python -m yt_dlp -f "bestvideo[height<=1080][ext=mp4]+bestaudio/best" --merge-output-format mp4 -o "/workspace/runtime/assets/backgrounds/%(title).120s.%(ext)s" "https://www.youtube.com/watch?v=VIDEO_ID"
```

Run this command from the repo directory that contains `docker-compose.yml`.

## Voiceover

The current fallback chain is:

```text
VieNeu -> FPT.AI -> edge-tts -> gTTS -> Windows SAPI -> silent
```

VieNeu runs locally after the model is downloaded. To clone a specific voice, set:

```env
VIENEU_VOICE_REF=runtime/assets/voice_sample.wav
```

If disk space on C or D is tight, keep model/cache paths on E using the cache environment variables in `.env.example`.

## Git Notes

Commit source, workflows, docs, and `.env.example`.

Do not commit:

- `.env`
- service-account JSON keys
- `venv/`
- `runtime/data/`
- generated screenshots, audio, video, or cache files
- downloaded background videos
