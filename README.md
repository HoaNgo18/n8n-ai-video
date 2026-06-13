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
- TTS: VieNeu only, with optional preset/reference voice registry.
- Video rendering: ffmpeg and MoviePy.
- Background downloader: `yt-dlp`.
- Review channel: signed runner review links plus Telegram inline buttons.
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
- VieNeu model/cache access for local TTS.
- Optional Telegram bot for Phase 4 review.
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

Phase 4 uses the runner review server plus Telegram nodes for review/publish callbacks.

## Google Sheet

Use a tab named `Threads`. The important columns are:

```text
ID
Source_URL
Source
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

Phase 1 now supports two user-facing collection paths in the same workflow:

- Auto mining: the schedule/manual branch calls `/phase1/threads-miner`. The miner searches RSS-derived trend keywords, static mass-appeal queries, then Home feed fallback, and the runner injects RSS trend context into the Gemini classifier.
- Search by topic: the `Telegram Search Bot` branch listens for admin Telegram messages. Send `/search <keyword>` or `/search <keyword> | <max_posts>`, for example `/search gia vang | 10`. It calls `/phase1/threads-search`, then reuses the same classifier, dedupe, and append nodes as the auto branch.

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

Run Phase 4 in n8n to send a signed runner review link to Telegram, process approve/reject callbacks, and publish approved drafts.

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

## Ngrok For Phase 4 Review

Use one ngrok tunnel to the runner. The runner serves the review video and forwards Telegram callbacks to n8n internally.

1. Start n8n locally:

```powershell
docker compose up -d
```

2. Start an ngrok tunnel to the runner:

```powershell
ngrok http 8000
```

3. Copy the HTTPS forwarding URL, for example:

```text
https://example.ngrok-free.app
```

4. Set `REVIEW_PUBLIC_BASE_URL` in `.env` to the HTTPS forwarding URL:

```env
REVIEW_PUBLIC_BASE_URL=https://example.ngrok-free.app
```

5. Keep runner callback forwarding pointed at n8n:

```env
N8N_PHASE4_CALLBACK_URL=http://n8n:5678/webhook/phase4-review-callback
```

6. Restart runner after changing `.env`:

```powershell
docker compose up -d runner
```

7. Set the Telegram bot webhook to the runner callback endpoint:

```powershell
curl "https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/setWebhook?url=https://example.ngrok-free.app/phase4/telegram-callback"
curl "https://api.telegram.org/bot<TELEGRAM_PHASE1_BOT_TOKEN>/setWebhook?url=https://example.ngrok-free.app/phase1/telegram-search-callback"
```

For n8n test mode, temporarily set `N8N_PHASE4_CALLBACK_URL` to the `/webhook-test/...` URL inside the Docker network, then switch it back to `/webhook/...` when the workflow is active.

Test local review link creation:

```powershell
docker compose exec -T runner python src/draft_review_helper.py --id "POST_ID" --video-path "runtime/data/videos/YYYY-MM-DD/POST_ID/final.mp4" --caption "test caption"
```

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

The current TTS engine is VieNeu only:

```env
TTS_ENGINE_ORDER=vieneu
```

If another engine is accidentally left in `TTS_ENGINE_ORDER`, the runner ignores it and still uses VieNeu.

VieNeu runs locally after the model is downloaded. To clone a specific voice, set:

```env
VIENEU_VOICE_REF=runtime/assets/voice_sample.wav
```

For 3-4 rotating author voices with VieNeu, set `TTS_AUTHOR_VOICES` to a
comma-separated voice registry. Each unique author is assigned one voice and
keeps that voice for later segments:

```env
TTS_ENGINE_ORDER=vieneu
TTS_AUTHOR_VOICES=preset:voice_1,preset:voice_2,preset:voice_3,preset:voice_4
```

You can also use reference audio files instead of presets:

```env
TTS_AUTHOR_VOICES=ref:runtime/assets/voices/a.wav,ref:runtime/assets/voices/b.wav,ref:runtime/assets/voices/c.wav
```

If disk space on C or D is tight, keep model/cache paths on E using the cache environment variables in `.env.example`.

## Background Music

Put reusable instrumental tracks in:

```text
runtime/assets/music/lofi/
```

Enable low-volume music under the narration:

```env
BACKGROUND_MUSIC_ENABLED=true
BACKGROUND_MUSIC_DIR=runtime/assets/music/lofi
BACKGROUND_MUSIC_PICK=hash
BACKGROUND_MUSIC_VOLUME=0.08
BACKGROUND_MUSIC_DUCKING=true
```

The merge step loops the track if needed, trims it to the narration duration,
adds a short fade, and ducks it under the AI voice.

For short-form pacing, keep narration around 60-75 seconds and avoid going past
90 seconds unless the post is unusually strong:

```env
AUDIO_MAX_SEGMENTS=7
AUDIO_MAX_POST_CHARS=220
AUDIO_MAX_COMMENT_CHARS=260
```

## Runtime Cleanup

Dry-run old runtime cleanup:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14
```

Delete files only after reviewing the dry-run report:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14 --apply
```

The cleanup script reads Google Sheets first and skips files still referenced by sheet cells.
If Google Sheets is not configured in the runner yet, use temp-only dry-run mode:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14 --without-sheet
```

## Quality Check

Check the latest final video:

```powershell
docker compose exec -T runner python scripts/quality_check.py
```

Check a specific post ID:

```powershell
docker compose exec -T runner python scripts/quality_check.py --id "POST_ID"
```

The check verifies final video dimensions, audio stream presence, duration drift between final/visual/audio, and audio timing manifest sanity when available.

## Git Notes

Commit source, workflows, docs, and `.env.example`.

Do not commit:

- `.env`
- service-account JSON keys
- `venv/`
- `runtime/data/`
- generated screenshots, audio, video, or cache files
- downloaded background videos
