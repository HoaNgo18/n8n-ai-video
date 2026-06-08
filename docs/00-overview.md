# AI Video Automation Project - Overview

## Goal

Build an automated n8n pipeline that turns Vietnamese Threads posts into short-form AI videos for TikTok, YouTube Shorts, Reels, or similar platforms.

Target video format:

- Real Threads post screenshot on top of gameplay/background video.
- AI Vietnamese narration based on the post and selected comments.
- Human approval before publishing.

## Pipeline

```text
Phase 1: Threads Miner
  -> Status: Pending

Phase 2: Screenshot + Extract
  -> Status: In Progress

Phase 3: Video Maker
  -> Status: Draft

Phase 4: Admin Review + Publisher
  -> Status: Draft / Approved / Rejected / Published / Failed
```

## Phase Details

### Phase 1: Threads Miner

Files:

- `workflows/01-threads-miner.json`
- `src/threads_miner.py`

Current status: working MVP.

What it does:

- Uses Playwright with a saved Threads session.
- Opens Threads feed and collects candidate post URLs.
- Filters for Vietnamese text.
- Estimates visible engagement and keeps posts above `MIN_ENGAGEMENT_SCORE`.
- Applies a heuristic content-fit filter with `MIN_CONTENT_FIT_SCORE`.
- Runs a Gemini classifier in the Phase 1 workflow to keep only posts that still look like either `discussion` or `story_hot`.
- Rejects weak personal-photo/status posts before writing to the sheet.
- Writes new rows into Google Sheet tab `Threads`.
- Sets `Status = Pending`.

Known notes:

- Threads Explore currently fails in headless browser, so the miner falls back to the home feed.
- `Note` now stores engagement, heuristic fit, and AI classifier reason alongside the raw preview.
- Engagement parsing and ranking are future improvements.

### Phase 2: Screenshot + Extract

Files:

- `workflows/02-screenshot-extract.json`
- `src/screenshot_extractor.py`

Current status: MVP implemented.

MVP behavior:

- Read rows where `Status = Pending`.
- Open each `Source_URL` with Playwright.
- Save the isolated original post screenshot as `runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png`.
- Save 3-5 isolated comment screenshots as `runtime/data/screenshots/YYYY-MM-DD/<ID>/comments/comment_XX.png` when available.
- Extract visible text from the post page.
- Create initial `Extracted_Content` and `Narrator_Script`.
- n8n runs an `AI Rewrite Script` node after extraction to rewrite the narration into cleaner spoken Vietnamese before saving it back to the sheet.
- If `SCRIPT_REWRITE_ENABLED=true` and rewrite credentials are configured, rewrite the raw extracted text into a shorter natural narration script before TTS.
- Update the row and set `Status = In Progress`.

Future behavior:

- Use Gemini Vision on screenshots for more reliable extraction.
- Capture post + 3-5 comments as separate screenshots.
- Add rejection rules for low-quality, ad-like, or non-story content.

### Phase 3: Video Maker

Files:

- `workflows/03-video-maker.json`
- `src/video_factory.py`

Current status: split-lane MVP implemented in one workflow.

Planned behavior:

- Lane A reads rows where `Status = In Progress` and `Audio_Path` is empty, then generates narration audio plus `Audio_Timing`.
- Lane B reads rows where `Status = In Progress`, `Audio_Path` exists, and `Audio_Timing` exists, then renders the silent visual video.
- Lane C reads rows where `Status = In Progress`, `Audio_Path` exists, and `Visual_Video_Path` exists, then merges them into the final MP4.
- Export audio under `runtime/data/audio/YYYY-MM-DD/<ID>/`.
- Export silent visual video under `runtime/data/visuals/YYYY-MM-DD/<ID>/`.
- Export final MP4 under `runtime/data/videos/YYYY-MM-DD/<ID>/`.
- The runner returns a fallback `Caption` after merge, then the workflow uses Gemini to read `Narrator_Script` / `Extracted_Content` and rewrite it into a TikTok caption plus hashtags.
- Update `Video_Path`, final `Caption`, and set `Status = Draft` only after merge and AI caption finalization.

MVP notes:

- Local rendering uses `runtime/assets/background.mp4` as fallback. Put multiple background videos in `runtime/assets/backgrounds/` to let Phase 3 pick one automatically.
- `BACKGROUND_VIDEO_PICK=hash` keeps the same post on the same background; use `random` for a different background each render or `first` for the first file alphabetically.
- Backgrounds can be downloaded with `python -m yt_dlp -f "bestvideo[height<=1080][ext=mp4]+bestaudio/best" --merge-output-format mp4 -o "runtime/assets/backgrounds/%(title)s.%(ext)s" URL`.
- `FPT_TTS_API_KEY` enables FPT.AI Speech TTS.
- `FPT_TTS_VOICE` controls the FPT voice. Current default: `banmai`.
- `FPT_TTS_SPEED` controls FPT speed. Current default: `0`.
- `TTS_VOICE` controls the Edge fallback voice. Current default: `vi-VN-HoaiMyNeural`.
- `SCRIPT_REWRITE_ENABLED` toggles optional AI rewrite for `Narrator_Script`.
- `GEMINI_API_KEY` is the preferred key for the Phase 2 rewrite node.
- `SCRIPT_REWRITE_MODEL` and `SCRIPT_REWRITE_BASE_URL` configure the Gemini rewrite request.
- `AI_CAPTION_ENABLED` toggles the Phase 3C Gemini caption node; `AI_CAPTION_MODEL` controls its model.
- Optional `SAPI_VOICE` can select an installed Windows offline voice if network TTS is unavailable.
- Final duration follows the generated narration length, with a short tail buffer.
- Add `Audio_Path`, `Audio_Timing`, `Visual_Video_Path`, `Caption`, `Draft_Video_URL`, `Draft_Drive_File_ID`, `Admin_Decision`, `TikTok_Publish_ID`, and `Published_URL` columns to the Google Sheet before importing the updated Phase 3 and Phase 4 workflows.
- Generated media stays in `runtime/` and should not be committed to git.
- The workflow should update the same row by `ID`.

### Phase 4: Publisher

Current status: balanced Phase 4 workflow implemented in n8n, with noisy helper logic moved into the Python runner.

Current n8n workflows:

- `Phase 4 - Review and Publish v2` (`jIwgeCiSLUefvteg`): visible review, approval callback, and publish branches.
- Local export: `workflows/04-review&publish.json`.

Runner code:

- `src/phase4_compact_helper.py`: saves admin decisions and acknowledges Telegram callbacks. It also keeps a compact helper path, but the active review branch uses n8n Google Drive OAuth nodes for large video files.
- `src/tiktok_playwright_publisher.py`: publishes approved videos through a browser/cookie uploader using `tiktok-uploader`.
- `runner/app.py`: exposes `/phase4/compact`.

MVP behavior:

- Manual tick reads the sheet, picks one eligible Phase 4 action, and routes it to review or publish.
- If a completed draft has `Status = Draft` and empty `Draft_Video_URL`, n8n uploads the local `Video_Path` to Google Drive with OAuth, shares the link, sends that link to Telegram with approve/reject buttons, and writes the review fields back to the sheet.
- Admin reviews `Draft_Video_URL`, edits `Caption` if needed, then clicks the Telegram approve or reject button.
- Telegram callback calls the runner with `mode = telegram_callback`.
- Rejected rows are set to `Status = Rejected`.
- Approved rows are set to `Status = Approved`.
- After an admin approves from Telegram, the callback branch immediately reads that row, keeps `Status = Approved`, writes a publish-start note, calls `/phase4/publish`, and then writes the final `Status`, `TikTok_Publish_ID`, `Published_URL` when available, and `Note`.
- Manual tick can still publish an already-approved row as a fallback/debug path.

Telegram callback setup:

- Activate `Phase 4 - Review and Publish v2`.
- Point the Telegram bot webhook to the production webhook URL for path `phase4-review-callback`.
- Callback payloads use `phase4|approve|<ID>` and `phase4|reject|<ID>`.

Known notes:

- `TIKTOK_PUBLISHER_MODE=playwright` is the default practical path because TikTok API OAuth/app setup is blocked for this project.
- Browser publishing authenticates in this order: `TIKTOK_SESSION_ID`, cookies file at `TIKTOK_UPLOADER_COOKIES_PATH` / `tiktok_cookies.json`, then `TIKTOK_USERNAME` + `TIKTOK_PASSWORD`.
- Username/password login is the most automated path, but TikTok may still interrupt it with captcha, 2FA, or account checkpoints. A warm session/cookie is usually more reliable.
- `TIKTOK_DRY_RUN` only applies to the old API publisher path.
- Review videos are sent as Google Drive links because Telegram Bot API rejects large direct uploads with `413 Request Entity Too Large`.
- `Published_URL` may remain empty immediately after upload; `TikTok_Publish_ID` is the primary machine confirmation.

## Credentials And Runtime

Required:

- Google Service Account for Google Sheets.
- Threads account for Playwright login.
- Python venv with Playwright installed.
- n8n started from `D:\Project III\start-n8n.ps1`.

Later:

- Gemini API key for Vision/script extraction.
- TTS provider for Vietnamese narration.
- Optional Telegram bot for draft-review notifications.

## Git Guidance

This project is worth putting in git now because Phase 1 and Phase 2 are working and Phase 3 will introduce more moving pieces.

Commit:

- `src/`
- `workflows/`
- `docs/`
- `.env.example`
- `.gitignore`

Do not commit:

- `.env`
- `venv/`
- `runtime/storage/`
- `runtime/data/`
- `runtime/debug/`
- generated screenshots/audio/video
- personal service-account keys

## Directory Layout

```text
D:\Project III\n8n-ai-video\
  workflows\
    01-threads-miner.json
    02-screenshot-extract.json
    03-video-maker.json
  src\
    threads_miner.py
    screenshot_extractor.py
    video_factory.py
    phase4_compact_helper.py
    tiktok_playwright_publisher.py
  runtime\
    assets\
      background.mp4
    data\
      screenshots\
        YYYY-MM-DD\
          <ID>\
            post.png
            comments\
              comment_01.png
      audio\
      visuals\
      videos\
      temp\
      exports\
    storage\
      threads-state.json
    samples\
    outputs\
    debug\
  docs\
  .env
```
