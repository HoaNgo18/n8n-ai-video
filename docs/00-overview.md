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
  -> Status: Ready To Upload / Rejected / Uploaded
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
- Sorts qualified posts by estimated engagement before writing to the sheet.
- Writes new rows into Google Sheet tab `Threads`.
- Sets `Status = Pending`.

Known notes:

- Threads Explore currently fails in headless browser, so the miner falls back to the home feed.
- `Note` stores a raw text preview for now.
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

- Lane A reads rows where `Status = In Progress` and `Audio_Path` is empty, then generates narration audio.
- Lane B reads rows where `Status = In Progress` and `Visual_Video_Path` is empty, then renders the silent visual video.
- Lane C reads rows where `Status = In Progress`, `Audio_Path` exists, and `Visual_Video_Path` exists, then merges them into the final MP4.
- Export audio under `runtime/data/audio/YYYY-MM-DD/<ID>/`.
- Export silent visual video under `runtime/data/visuals/YYYY-MM-DD/<ID>/`.
- Export final MP4 under `runtime/data/videos/YYYY-MM-DD/<ID>/`.
- Update `Video_Path`, generate `Caption`, and set `Status = Draft` only after merge.

MVP notes:

- Local rendering should use `runtime/assets/background.mp4`.
- `FPT_TTS_API_KEY` enables FPT.AI Speech TTS.
- `FPT_TTS_VOICE` controls the FPT voice. Current default: `banmai`.
- `FPT_TTS_SPEED` controls FPT speed. Current default: `0`.
- `TTS_VOICE` controls the Edge fallback voice. Current default: `vi-VN-HoaiMyNeural`.
- `SCRIPT_REWRITE_ENABLED` toggles optional AI rewrite for `Narrator_Script`.
- `GEMINI_API_KEY` is the preferred key for the Phase 2 rewrite node.
- `SCRIPT_REWRITE_MODEL` and `SCRIPT_REWRITE_BASE_URL` configure the Gemini rewrite request.
- Optional `SAPI_VOICE` can select an installed Windows offline voice if network TTS is unavailable.
- Final duration follows the generated narration length, with a short tail buffer.
- Add `Audio_Path`, `Visual_Video_Path`, `Caption`, `Admin_Decision`, and `TikTok_Publish_ID` columns to the Google Sheet before importing the updated Phase 3 and Phase 4 workflows.
- Generated media stays in `runtime/` and should not be committed to git.
- The workflow should update the same row by `ID`.

### Phase 4: Publisher

File:

- `workflows/04-auto-publisher.json`

Current status: Sheet approval + manual TikTok upload handoff implemented in workflow JSON.

MVP behavior:

- Read completed drafts where `Status = Draft`.
- Admin reviews local `Video_Path`, edits `Caption` if needed, then sets `Admin_Decision = approve` or `reject`.
- Set rejected rows to `Status = Rejected`.
- Set approved rows to `Status = Ready To Upload`.
- Keep `Video_Path` and `Caption` ready for manual TikTok upload.
- After the upload is done manually, fill `Published_URL` in the sheet.
- The workflow converts `Ready To Upload + Published_URL` to `Status = Uploaded`.

Known notes:

- Phase 4 no longer depends on TikTok API production approval.
- The final TikTok upload step is intentionally manual to avoid OAuth/app-review blockers for internal use.
- `Published_URL` is now the main confirmation field for a completed upload.

## Credentials And Runtime

Required:

- Google Service Account for Google Sheets.
- Threads account for Playwright login.
- Python venv with Playwright installed.
- n8n started from `D:\Project III\start-n8n.ps1`.

Later:

- Gemini API key for Vision/script extraction.
- TTS provider for Vietnamese narration.
- Optional notification channel for manual-upload reminders.

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
    04-auto-publisher.json
  src\
    threads_miner.py
    screenshot_extractor.py
    video_factory.py
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
