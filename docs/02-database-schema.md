# Google Sheets Database Schema - Threads Edition

Each row represents one Threads post. The row is filled progressively by each pipeline phase.

## Columns

| # | Column Name | Filled By | Type | Description |
|---|---|---|---|---|
| 1 | `ID` | Phase 1 | Text | Unique Threads post ID, used for dedupe and row updates. |
| 2 | `Source_URL` | Phase 1 | URL | Full Threads post URL. |
| 3 | `Source` | Phase 1 | Text | Collection source, for example `auto` or `search:<keyword>`. |
| 4 | `Collected_At` | Phase 1 | DateTime/Text | Collection timestamp from n8n. |
| 5 | `Status` | System | Enum | `Pending` -> `In Progress` -> `Draft` -> `Approved` / `Rejected` -> `Published` / `Failed`. |
| 6 | `Screenshots` | Phase 2 | JSON string | Example: `{"post":"runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png","comments":[]}`. |
| 7 | `Extracted_Content` | Phase 2 | JSON string | Example: `{"post_text":"...","comments":[{"text":"..."}]}`. |
| 8 | `Narrator_Script` | Phase 2 | Text | Text prepared for Vietnamese narration/TTS. |
| 9 | `Audio_Path` | Phase 3A | File path | Local narration audio path. |
| 10 | `Audio_Timing` | Phase 3A | JSON string | Timing manifest per segment/screenshot, example: `[{"image_index":0,"start":0.3,"end":4.7}]`. |
| 11 | `Visual_Video_Path` | Phase 3B | File path | Local silent visual video path before merge. |
| 12 | `Video_Path` | Phase 3C | File path | Local MP4 output path. |
| 13 | `Caption` | Phase 3C / Admin | Text | Final TikTok caption, including hashtags. Phase 3C now asks Gemini to generate this from `Narrator_Script` and `Extracted_Content`, with the runner caption as fallback. |
| 14 | `Draft_Video_URL` | Phase 4 | URL | Signed local runner review link for the generated draft video. |
| 15 | `Draft_Drive_File_ID` | Phase 4 | Text | Legacy Drive file ID column. Local review flow leaves this empty. |
| 16 | `Admin_Decision` | Admin | Text | Empty by default. Set to `approve` or `reject` during review. |
| 17 | `TikTok_Publish_ID` | Phase 4 | Text | Machine confirmation from the TikTok publisher. Browser uploads use a `browser_<timestamp>` value. |
| 18 | `Published_URL` | Phase 4 | URL | Final TikTok URL when available. May stay empty immediately after upload. |
| 19 | `Note` | Any | Text | Debug note, quality note, or error context. Phase 1 now stores source, trend brief, engagement, fit score, AI angle, and reject/keep reason here. |

## Status Flow

```text
Phase 1: Threads Miner
  writes ID, Source_URL, Source, Collected_At, Status, Note
  sets Status = Pending

Phase 2: Screenshot + Extract
  reads Pending rows
  writes Screenshots, Extracted_Content, Narrator_Script, Status, Note
  sets Status = In Progress or Rejected

Phase 3A: Voice Generator
  reads In Progress rows
  writes Audio_Path, Audio_Timing, Note
  keeps Status = In Progress

Phase 3B: Visual Builder
  reads In Progress rows with Audio_Path and Audio_Timing
  writes Visual_Video_Path, Note
  keeps Status = In Progress

Phase 3C: Merge Final
  reads In Progress rows with Audio_Path and Visual_Video_Path
  merges final MP4
  generates fallback Caption in the runner
  rewrites Caption + hashtags through Gemini in the workflow when AI_CAPTION_ENABLED=true
  writes Video_Path, Caption, Status, Note
  sets Status = Draft

Phase 4: Review + Auto Publish
  reads Draft rows without Draft_Video_URL
  creates a signed local review link and writes Draft_Video_URL, Note
  reads Draft rows with Admin_Decision
  sets Status = Approved or Rejected
  reads Approved rows
  calls the TikTok browser/cookie publisher
  writes TikTok_Publish_ID, Published_URL, Status, Note
```

## Notes

- `ID` must remain unique.
- `Status` is intentionally limited to durable row states only. Transient actions such as "publishing started" belong in `Note`, not in `Status`.
- `Screenshots` and `Extracted_Content` are JSON strings because Google Sheets cells are flat.
- `Narrator_Script` should be clean Vietnamese narration text only: no Markdown, no UI labels like `TOPIC` / `COMMENT`, and ideally broken into 2-4 short spoken beats.
- Add the `Audio_Path`, `Audio_Timing`, `Visual_Video_Path`, `Caption`, `Draft_Video_URL`, `Draft_Drive_File_ID`, `Admin_Decision`, `TikTok_Publish_ID`, and `Published_URL` columns before importing the updated Phase 3 and Phase 4 workflows.
- `Note` is currently used by Phase 1 for text preview and by Phase 2 for processing notes. Later improvement: split this into `Preview` and `Note` if the sheet grows.
