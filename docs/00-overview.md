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

Phase 3A: Voice Generator
  -> Status: In Progress

Phase 3B: Visual Builder
  -> Status: In Progress

Phase 3C: Merge Final
  -> Status: Done

Phase 4: Publisher
  -> Status: Published / Rejected
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
- Update the row and set `Status = In Progress`.

Future behavior:

- Use Gemini Vision on screenshots for more reliable extraction.
- Capture post + 3-5 comments as separate screenshots.
- Add rejection rules for low-quality, ad-like, or non-story content.

### Phase 3: Video Maker

Files:

- `workflows/03a-voice-generator.json`
- `workflows/03b-visual-builder.json`
- `workflows/03c-merge-final.json`
- `src/video_factory.py`

Current status: split MVP implemented.

Planned behavior:

- `03A` reads rows where `Status = In Progress` and `Audio_Path` is empty, then generates narration audio.
- `03B` reads rows where `Status = In Progress` and `Visual_Video_Path` is empty, then renders the silent visual video.
- `03C` reads rows where `Status = In Progress`, `Audio_Path` exists, and `Visual_Video_Path` exists, then merges them into the final MP4.
- Export audio under `runtime/data/audio/YYYY-MM-DD/<ID>/`.
- Export silent visual video under `runtime/data/visuals/YYYY-MM-DD/<ID>/`.
- Export final MP4 under `runtime/data/videos/YYYY-MM-DD/<ID>/`.
- Update `Video_Path` and set `Status = Done` only after merge.

MVP notes:

- Local rendering should use `runtime/assets/background.mp4`.
- `FPT_TTS_API_KEY` enables FPT.AI Speech TTS.
- `FPT_TTS_VOICE` controls the FPT voice. Current default: `banmai`.
- `FPT_TTS_SPEED` controls FPT speed. Current default: `0`.
- `TTS_VOICE` controls the Edge fallback voice. Current default: `vi-VN-HoaiMyNeural`.
- Optional `SAPI_VOICE` can select an installed Windows offline voice if network TTS is unavailable.
- Final duration follows the generated narration length, with a short tail buffer.
- Add `Audio_Path` and `Visual_Video_Path` columns to the Google Sheet before importing the split Phase 3 workflows.
- Generated media stays in `runtime/` and should not be committed to git.
- The workflow should update the same row by `ID`.

### Phase 4: Publisher

File:

- `workflows/04-auto-publisher.json`

Current status: not started.

Planned behavior:

- Send completed video for human approval.
- Publish to target platforms after approval.
- Update `Published_URL`.
- Set `Status = Published` or `Rejected`.

## Credentials And Runtime

Required:

- Google Service Account for Google Sheets.
- Threads account for Playwright login.
- Python venv with Playwright installed.
- n8n started from `D:\Project III\start-n8n.ps1`.

Later:

- Gemini API key for Vision/script extraction.
- TTS provider for Vietnamese narration.
- Platform APIs for publishing.

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
