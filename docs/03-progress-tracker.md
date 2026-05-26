# Progress Tracker - AI Video Automation

Project is now tracking the Threads-based pipeline. Older Reddit RSS / generic script-writer workflows are kept in the repo for reference only.

Project structure was cleaned up on 2026-04-23: Python scripts now live in `src/`, while local runtime files live in `runtime/`.

## Current Pipeline

- [x] **Phase 1: Threads Miner**
  - [x] Workflow: `workflows/01-threads-miner.json`
  - [x] Script: `src/threads_miner.py`
  - [x] Reads Threads credentials from `.env`.
  - [x] Reuses Playwright session from `runtime/storage/threads-state.json`.
  - [x] Scrapes candidate Threads post URLs and writes new rows to Google Sheet tab `Threads`.
  - [x] Sets `Status = Pending`.
  - [x] Deduplicates by `ID` before appending.
  - [x] Confirmed working in n8n on 2026-04-23.

- [x] **Phase 2: Screenshot + Extract MVP**
  - [x] Workflow: `workflows/02-screenshot-extract.json`
  - [x] Script: `src/screenshot_extractor.py`
  - [x] Read rows with `Status = Pending`.
  - [x] Open each `Source_URL` with Playwright.
  - [x] Save isolated post screenshot under `runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png`.
  - [x] Save isolated comment screenshots under `runtime/data/screenshots/YYYY-MM-DD/<ID>/comments/comment_XX.png`.
  - [x] Extract visible text from the post page DOM.
- [x] Generate temporary `Narrator_Script`.
- [x] Update row with `Screenshots`, `Extracted_Content`, `Narrator_Script`.
- [x] Added `AI Rewrite Script` node in `workflows/02-screenshot-extract.json` so the saved narration script can be rewritten by Gemini before Phase 3 TTS.
  - [x] Set `Status = In Progress`.
  - [x] Switched screenshot isolation to Threads DOM blocks using `div[data-pressable-container="true"]` + Playwright bounding boxes.
  - [x] Confirmed full n8n run after import.

- [x] **Phase 3 Refactor: Multi-Lane Workflow**
  - [x] Script: `src/video_factory.py`
  - [x] Added mode `voice` for narration generation.
  - [x] Added mode `visual` for silent gameplay + screenshot render.
  - [x] Added mode `merge` for final audio/video mux.
  - [x] Consolidated Phase 3 into one workflow: `workflows/03-video-maker.json`
  - [x] Updated merge output to create a TikTok `Caption` and set `Status = Draft`.
  - [ ] Add `Audio_Path`, `Visual_Video_Path`, `Caption`, `Admin_Decision`, and `TikTok_Publish_ID` columns in Google Sheet.
  - [ ] Import the updated Phase 3 workflow into n8n.
  - [ ] Confirm successful end-to-end Phase 3 run in n8n.

- [x] **Phase 4: Admin Review + Manual Upload MVP**
  - [x] Workflow: `workflows/04-auto-publisher.json`
  - [x] Convert `Draft + Admin_Decision=approve` to `Ready To Upload`.
  - [x] Convert `Draft + Admin_Decision=reject` to `Rejected`.
  - [x] Confirm manual TikTok upload when `Published_URL` is filled in the sheet.
  - [x] Update `Published_URL`, `Status`, and `Note`.
  - [ ] Import the Phase 4 workflow into n8n.
  - [ ] Add final manual-upload helper columns if desired (`Uploaded_At`, `Upload_Note`).
  - [ ] Confirm successful end-to-end manual upload flow.

## Improvement Notes

### Phase 1 Follow-Ups

- Threads Explore currently redirects to `threads.com/explore` and returns a "page gone" screen in Playwright. The miner now falls back to the logged-in home feed. Later improvement: find the current Explore route or use a search/topic feed.
- `Note` currently stores a raw text preview that may include UI text and engagement numbers. Later improvement: parse author, post body, likes, comments, reposts, and timestamp into separate fields or a structured JSON blob.
- Language detection currently uses Vietnamese diacritics. Later improvement: use Gemini or a lightweight language detector so non-diacritic Vietnamese is not missed.
- Ranking is not yet true "viral ranking"; it collects visible feed candidates. Later improvement: sort/filter by engagement metrics.
- Phase 1 now estimates engagement from visible numeric clusters and filters by `MIN_ENGAGEMENT_SCORE`. Later improvement: parse exact like/comment/repost/share labels from a more stable source.
- Session handling is basic. `THREADS_FORCE_LOGIN=true` can refresh login manually. Later improvement: detect invalid sessions automatically and retry login once.
- n8n Code node requires `NODE_FUNCTION_ALLOW_BUILTIN=*` because it calls Python via Node `child_process`. Later improvement: move Python calls to a local HTTP microservice or restore Execute Command if the n8n install supports it.
- `Collected_At` depends on n8n timezone. Verify n8n timezone if timestamps look off.

### Phase 2 Planned Improvements

- DOM extraction may still include some Threads UI/navigation text. Later improvement: use Gemini Vision or tighter selectors to extract only post/comment content.
- Screenshot goal: `post.png` must contain only the original post card, including account name, post content, timestamp, and engagement counts. `comment_XX.png` must contain only one comment each.
- Screenshot isolation now avoids full-page visual crop / pixel-line detection and instead clips exact DOM bounding boxes. The post clip trims the `Top / View activity` discussion toolbar when Threads includes it inside the same post container.
- Current post/comment selection is still heuristic because Threads DOM selectors are unstable. Verify screenshots visually after each run and tune selectors if needed.
- Add quality gates before moving to `In Progress`: reject ads, shops, low-context posts, or posts with insufficient Vietnamese content.
- Add a `Rejected` status with a useful `Note` when screenshot/extract fails.
- The sample Phase 2 test post was shop/product content; later workflow should filter commercial posts if the desired channel format is story/commentary.

### Project / Git Follow-Ups

- A `.gitignore` was added for source-control safety. Keep `.env`, `venv/`, Threads session state, screenshots, videos, debug dumps, and local runtime outputs out of git.
- Recommended git strategy: commit code, workflows, docs, `.env.example`, and small config files. Do not commit personal credentials, Google service-account JSON, cookies/session files, generated screenshots, generated audio, or rendered videos.
- Later improvement: split runtime assets into `runtime/assets/source/` and `runtime/assets/licensed/` so background video/audio licensing is traceable.

### Phase 3 Planned Improvements

- Voice, visual build, and merge now run as separate lanes inside one workflow so TTS/network delays do not block gameplay render and merge debugging.
- TTS now tries FPT.AI Speech first using `FPT_TTS_API_KEY`, `FPT_TTS_VOICE=banmai`, and `FPT_TTS_SPEED=0`; then `edge-tts`, gTTS, offline Windows SAPI, and silent audio if all providers fail.
- Screenshot timeline is sequential: original post first, then each comment screenshot one-by-one.
- Visual timing now prefers the real `Audio_Path` duration when available; otherwise it falls back to a text-length estimate.
- Each screenshot slot is timed sequentially with a bounded first-post window so the post cannot dominate nearly the whole video before comments appear.
- Screenshot overlays now scale to full video width and sit higher on screen instead of vertically centered.
- `03B` renders a silent visual video with `preset=veryfast` and `fps=24` to reduce task-runner timeout risk.
- `03C` muxes audio + visual, creates `Caption`, and sets `Status = Draft` so admin approval can happen before publishing.
- Phase 2 can optionally rewrite the temporary narration script through an AI provider before TTS when `SCRIPT_REWRITE_ENABLED=true`.
- Later improvement: use a paid Vietnamese TTS provider with better voice consistency and fewer network failures.
- Later improvement: animate post/comment screenshots with smoother timed reveals, scale transitions, and safe-area checks.
- Later improvement: add captions/subtitles synced to narration.
- Later improvement: add video quality gates: min/max duration, readable screenshot scale, safe margins, and non-empty audio.
