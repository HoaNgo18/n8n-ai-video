# Google Sheets Database Schema - Threads Edition

Each row represents one Threads post. The row is filled progressively by each pipeline phase.

## Columns

| # | Column Name | Filled By | Type | Description |
|---|---|---|---|---|
| 1 | `ID` | Phase 1 | Text | Unique Threads post ID, used for dedupe and row updates. |
| 2 | `Source_URL` | Phase 1 | URL | Full Threads post URL. |
| 3 | `Collected_At` | Phase 1 | DateTime/Text | Collection timestamp from n8n. |
| 4 | `Status` | System | Enum | `Pending` -> `In Progress` -> `Done` -> `Published` / `Rejected`. |
| 5 | `Screenshots` | Phase 2 | JSON string | Example: `{"post":"runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png","comments":[]}`. |
| 6 | `Extracted_Content` | Phase 2 | JSON string | Example: `{"post_text":"...","comments":[{"text":"..."}]}`. |
| 7 | `Narrator_Script` | Phase 2 | Text | Text prepared for Vietnamese narration/TTS. |
| 8 | `Audio_Path` | Phase 3A | File path | Local narration audio path. |
| 9 | `Visual_Video_Path` | Phase 3B | File path | Local silent visual video path before merge. |
| 10 | `Video_Path` | Phase 3C | File path | Local MP4 output path. |
| 11 | `Published_URL` | Phase 4 | URL | Final platform URL after publishing. |
| 12 | `Note` | Any | Text | Debug note, quality note, or error context. |

## Status Flow

```text
Phase 1: Threads Miner
  writes ID, Source_URL, Collected_At, Status, Note
  sets Status = Pending

Phase 2: Screenshot + Extract
  reads Pending rows
  writes Screenshots, Extracted_Content, Narrator_Script, Status, Note
  sets Status = In Progress

Phase 3A: Voice Generator
  reads In Progress rows
  writes Audio_Path, Note
  keeps Status = In Progress

Phase 3B: Visual Builder
  reads In Progress rows
  writes Visual_Video_Path, Note
  keeps Status = In Progress

Phase 3C: Merge Final
  reads In Progress rows with Audio_Path and Visual_Video_Path
  writes Video_Path, Status, Note
  sets Status = Done

Phase 4: Publisher
  reads Done rows
  writes Published_URL, Status, Note
  sets Status = Published or Rejected
```

## Notes

- `ID` must remain unique.
- `Screenshots` and `Extracted_Content` are JSON strings because Google Sheets cells are flat.
- `Narrator_Script` should eventually be cleaned text only: no Markdown and minimal UI noise.
- Add the `Audio_Path` and `Visual_Video_Path` columns to the Google Sheet before importing the split Phase 3 workflows.
- `Note` is currently used by Phase 1 for text preview and by Phase 2 for processing notes. Later improvement: split this into `Preview` and `Note` if the sheet grows.
