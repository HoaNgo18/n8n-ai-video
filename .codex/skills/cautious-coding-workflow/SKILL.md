---
name: cautious-coding-workflow
description: Apply a cautious coding workflow for implementation and fixes in the n8n-ai-video pipeline. Use for surgical edits, phase-aware debugging, n8n workflow changes, screenshot/TTS/timing issues, and verification in a sandbox that often lacks live Threads, Gemini, Google Sheets, TikTok, or VieNeu access.
argument-hint: Task goal and constraints
user-invocable: true
---

# Cautious Coding Workflow For n8n-ai-video

## Purpose
- Maximize agent effectiveness on this repo by forcing phase awareness, minimal edits, and proof-oriented verification.
- Prevent common regressions in this project: broken n8n JSON, stdout/stderr contract violations, screenshot-script desync, truncated audio/video, and over-broad fixes across unrelated phases.

## Repo Snapshot
- Project type: automated Threads-to-short-video pipeline.
- Main phases:
  - Phase 1: mining/filtering in `src/threads_miner.py`, `src/trend_signal.py`, `workflows/01-threads-miner.json`
  - Phase 2: capture/extract in `src/screenshot_extractor.py`, `workflows/02-screenshot-extract.json`
  - Phase 3: voice/visual/merge in `src/video_factory.py`, `workflows/03-video-maker.json`
  - Phase 4: review/publish in `src/phase4_compact_helper.py`, publisher helpers, `workflows/04-review&publish.json`
- Runtime outputs live under `runtime/` and are disposable.
- Runner API lives in `runner/` and is mounted into Docker as `/workspace`.

## Non-Negotiable Project Rules
- Python phase scripts must print exactly one JSON payload to `stdout` and send logs/debug text to `stderr`.
- Prefer existing helpers and conventions over new abstractions.
- New behavior should be env-driven when it is truly tunable; avoid hardcoding knobs unless the value is intrinsic.
- Keep fixes phase-local unless the bug clearly crosses a handoff boundary.
- Never casually refactor workflow exports. n8n JSON is fragile.

## Must-Know Conventions
- `.env` is loaded with `load_dotenv(PROJECT_ROOT / ".env", override=True)`.
- CLI payloads from n8n may be JSON-wrapped strings; reuse `decode_cli_text()` style handling.
- Vietnamese text normalization already exists in repo helpers; reuse existing matching/cleanup logic before inventing new regex pipelines.
- For debugging, MCP n8n tools may be used when they materially shorten the path to the failing node, execution, payload, or workflow state.
- For workflow edits, modify only the local JSON exports in `workflows/`. Do not treat live n8n as the source of truth for editing; the user will handle importing/uploading to n8n.
- For workflow JSON:
  - preserve node ids
  - preserve node names if referenced by connections
  - keep valid JSON import structure
  - treat embedded `jsCode` and prompt strings as code, not plain text
- For media pipeline bugs, always reason across all three artifacts together:
  - `Screenshots`
  - `Extracted_Content`
  - `Narrator_Script` / `Audio_Timing`

## High-Risk Areas
- `src/screenshot_extractor.py`
  - post/comment isolation
  - grouped comment capture
  - text cleanup that can leak headers, topic banners, UI fragments
  - image-to-text ordering and `image_index`
- `src/video_factory.py`
  - TTS shorthand normalization
  - segment limiting/truncation
  - audio timing manifests
  - overlay plan generation
  - final duration matching audio
- `workflows/*.json`
  - JSON validity
  - embedded prompt/code escaping
  - field mapping between phases

## When To Use
- Bug fixes in any phase
- Requests that say "rà soát nhanh", "sửa tối thiểu", "đừng đụng rộng"
- Screenshot/comment grouping issues
- Script cleanup or TTS pronunciation problems
- Audio/video duration mismatch
- n8n Code node or Gemini prompt tuning
- Any task where overengineering would hurt more than help

## Default Mindset
- Be conservative.
- Touch the fewest files possible.
- Prefer minimal, local changes over broad cleanup or speculative improvement.
- Keep working context tight: load only the files, nodes, prompts, and runtime artifacts needed to prove and fix the reported issue.
- Prove the bug path before editing when local evidence exists.
- Prefer fixing the source of truth instead of patching downstream symptoms.
- If the problem crosses phases, change the earliest safe handoff point.

## n8n Debug vs Edit Rule
- Debugging:
  - Allowed to inspect executions, workflow state, node payloads, and related metadata through MCP n8n tools when available.
  - Use MCP n8n to confirm what happened in a real run when local files alone are insufficient.
- Editing:
  - Edit workflow JSON only in local repo files under `workflows/`.
  - Do not rely on live n8n edits as the implementation path.
  - Assume the user will import/upload the updated workflow manually after review.

## Phase-Aware Debugging Heuristics

### Phase 1
- If wrong posts are entering the pipeline, inspect:
  - miner scoring/filters
  - Gemini keep/reject prompt
  - Sheets append/dedupe mapping
- Do not jump to Phase 2/3 unless the issue is clearly downstream.

### Phase 2
- If script contains UI noise, topic headers, handles, or timestamps, fix extraction/cleanup first.
- If screenshots and narration do not align, inspect `segments`, `comments`, `continuations`, and `image_index` generation before touching video rendering.
- If comment crops look wrong, inspect grouping and bounding-rect logic, not just overlay rendering.

### Phase 3
- If TTS says the wrong thing, inspect shorthand normalization and the actual text fed into segment audio generation.
- If video ends too early, inspect segment limits, audio duration, timing manifest, and merge duration.
- If overlays do not match spoken lines, inspect `select_audio_segments`, `select_overlay_text_blocks`, and timing/image alignment together.

### Phase 4
- If draft/review/publish is wrong, confirm the row fields generated by earlier phases first.
- Avoid blaming publisher logic when the sheet row is already malformed.

## Decision Gates

### 1. Clarity Gate
- If the user request is ambiguous in a way that could cause broad or destructive edits, ask a focused question.
- Otherwise make a narrow assumption and proceed.

### 2. Scope Gate
- Fix only what is necessary for the reported outcome.
- Avoid "cleanup while here" changes.
- Do not edit docs, README, `.env.example`, or unrelated workflows unless they are directly part of the requested outcome.
- If a workflow change is needed, edit only the specific local export file and only the touched node payload/code/prompt needed for the fix.

### 3. Earliest-Fix Gate
- Prefer the earliest reliable point in the pipeline that can solve the issue.
- Example:
  - leaked topic banner -> Phase 2 cleanup
  - `wtf` pronunciation -> Phase 3 text normalization
  - video cuts off early -> Phase 3 segment/timing/merge logic

### 4. Verification Gate
- Every code change must get the narrowest realistic verification available in sandbox.
- At minimum run one of:
  - `python` function snippet
  - `py_compile`
  - `python -m json.tool` for workflow JSON
  - dry-run/mock command if the file supports it
- If real external access is unavailable, say so explicitly and validate logic locally.

## Required Workflow
1. Read the smallest set of files needed to understand the bug path.
2. Identify the exact handoff where bad data first appears.
3. State a short plan tied to that handoff.
4. Edit minimally.
5. Run targeted verification.
6. Report only what changed, what was verified, and any remaining unverified edge.

## Preferred Verification By File Type
- Python source:
  - `py_compile`
  - direct function snippet with representative Vietnamese examples
- Screenshot/extract logic:
  - inspect existing runtime artifacts under `runtime/data/...`
  - test helper functions on sample extracted text or mock blocks
- Video/timing logic:
  - test `select_audio_segments`, timing builders, overlay planners with synthetic `Extracted_Content`
  - compare expected segment count vs image count
- n8n workflow JSON:
  - `python -m json.tool workflows/<file>.json`
  - inspect embedded `jsCode` string around edited node

## Strong Preferences For This Repo
- Prefer `rg` for search.
- Prefer `apply_patch` for edits.
- Preserve existing naming and file layout.
- Keep context usage efficient: avoid reading whole large workflow exports or source files when targeted search and narrow excerpts are enough.
- Keep Vietnamese examples in tests/snippets when the bug involves Vietnamese text.
- Use real artifacts already present in `runtime/` when debugging alignment issues.
- If a current runtime artifact proves the bug, reference that instead of guessing.

## Anti-Patterns
- Editing multiple phases without proving the issue crosses them.
- Adding a new helper/module when an existing function can be extended.
- Breaking the one-JSON-to-stdout contract.
- Touching n8n node ids or connection structure casually.
- Fixing desync by only masking it visually downstream.
- Leaving hard caps that silently truncate script/audio/image coverage.
- Ending after code edits without any verification.

## Completion Criteria
- The fix is traceable to the user-reported symptom.
- The touched phase/file choice is justified by the data flow.
- Verification was run and is relevant.
- Any unverified part depends only on missing external systems, not on skipped local checks.
