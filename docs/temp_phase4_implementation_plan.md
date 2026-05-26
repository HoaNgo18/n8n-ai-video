# Phase 4 Implementation Plan

## Goal

Extend the current pipeline so Phase 3 produces a reviewable TikTok draft, then Phase 4 lets an admin approve or reject the draft before publishing to TikTok.

End-to-end target flow:

```text
Pending
  -> In Progress
  -> Draft
  -> Approved / Rejected
  -> Published / Publish Failed
```

## Final Google Sheet Columns

Keep the Sheet small and operational. These are the intended columns:

```text
ID
Source_URL
Collected_At
Status
Screenshots
Extracted_Content
Narrator_Script
Audio_Path
Visual_Video_Path
Video_Path
Caption
Admin_Decision
TikTok_Publish_ID
Published_URL
Note
```

Allowed `Status` values:

```text
Pending
In Progress
Draft
Approved
Rejected
Published
Publish Failed
```

`Admin_Decision` should be empty by default. Admin sets it to:

```text
approve
reject
```

## Phase 3 Changes

Current Phase 3 creates `Audio_Path`, `Visual_Video_Path`, and `Video_Path`.

Required changes:

1. Add caption generation after final merge.
2. Write the final TikTok caption to `Caption`.
3. Include hashtags directly inside `Caption`; do not create a separate `Hashtags` column.
4. Set `Status = Draft` after `Video_Path` and `Caption` are ready.
5. Keep `Note` useful for admin review, for example:

```text
Phase 3C: draft ready duration=42.3s
```

Suggested caption source:

- Prefer `Narrator_Script` plus `Extracted_Content`.
- Generate a short Vietnamese caption with 3-6 hashtags.
- Keep total caption length safely below TikTok's limit.
- Avoid misleading claims, spammy hashtags, or duplicated tags.

Suggested MVP caption style:

```text
Cau chuyen nay dang gay tranh luan tren Threads. Ban nghi sao?

#threads #chuyencuocsong #tamly #xuhuong
```

Implementation options:

- Simple MVP: deterministic caption template in n8n Code node.
- Better MVP: Gemini rewrite node creates the caption from `Narrator_Script`.

Recommended first implementation: deterministic caption template so the approval and publishing flow can be tested without adding another AI dependency.

## Phase 4 Workflow

Create or replace:

```text
workflows/04-auto-publisher.json
```

The workflow should be split into three logical lanes.

### Lane A: Admin Decision Sync

Purpose: convert admin Sheet decisions into pipeline statuses.

Steps:

1. Read rows where `Status = Draft`.
2. If `Admin_Decision = reject`, update:

```text
Status = Rejected
Note = Phase 4: rejected by admin
```

3. If `Admin_Decision = approve`, update:

```text
Status = Approved
Note = Phase 4: approved by admin
```

4. If `Admin_Decision` is empty, leave the row as `Draft`.

MVP admin review process:

- Admin opens the Sheet.
- Admin checks `Video_Path` locally.
- Admin edits `Caption` if needed.
- Admin sets `Admin_Decision` to `approve` or `reject`.

### Lane B: TikTok Publisher

Purpose: publish approved videos to TikTok.

Steps:

1. Read rows where:

```text
Status = Approved
Video_Path is not empty
Caption is not empty
Published_URL is empty
```

2. Validate local video file exists.
3. Call TikTok Content Posting API.
4. Save the returned publish identifier to `TikTok_Publish_ID`.
5. If publish succeeds, update:

```text
Status = Published
Published_URL = <TikTok URL if available>
Note = Phase 4: published to TikTok
```

6. If publish fails, update:

```text
Status = Publish Failed
Note = Phase 4 publish failed: <short error>
```

### Lane C: Retry Support

Purpose: make failures easy to retry without adding more columns.

Retry rule:

- If a row is `Publish Failed`, admin can fix `Caption`, credentials, or file path, then set `Status = Approved` again.
- Phase 4 should pick it up on the next run.

## TikTok Publishing Design

Use TikTok Content Posting API with local file upload.

Preferred mode:

```text
video.publish
```

This is the direct publish flow. It requires the TikTok app to be allowed to use the `video.publish` scope.

Fallback mode:

```text
video.upload
```

This uploads into TikTok inbox/draft for manual completion inside TikTok. Use this only if direct publish is blocked by app approval or scope limitations.

Important API note:

- TikTok direct posting may be restricted to private/self-only until the developer app passes TikTok audit.
- Public direct publishing requires the correct app configuration, user authorization, and approved scope.

## Credentials Needed

### Already Needed

Google Sheets:

```text
GOOGLE_SERVICE_ACCOUNT_JSON or n8n Google Sheets credential
GOOGLE_SHEET_ID
GOOGLE_SHEET_TAB=Threads
```

### TikTok Required

TikTok Developer App:

```text
TIKTOK_CLIENT_KEY
TIKTOK_CLIENT_SECRET
TIKTOK_REDIRECT_URI
TIKTOK_ACCESS_TOKEN
TIKTOK_REFRESH_TOKEN
TIKTOK_PRIVACY_LEVEL
TIKTOK_BRAND_CONTENT
TIKTOK_BRAND_ORGANIC
TIKTOK_IS_AIGC
```

Required TikTok scope for direct publish:

```text
video.publish
```

Optional fallback scope:

```text
video.upload
```

Also required:

- Content Posting API enabled for the TikTok app.
- The target TikTok account has authorized the app.
- App audit/approval if public direct publishing is required.
- The requested `TIKTOK_PRIVACY_LEVEL` is available in TikTok creator info.

### Optional Later

Admin notification:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

or:

```text
SLACK_BOT_TOKEN
SLACK_CHANNEL_ID
```

External video preview hosting:

```text
Google Drive credential
```

or:

```text
S3/R2 bucket credential
```

These are not required for the first Sheet-based MVP.

## Implementation Checklist

### Step 1: Sheet Update

- [x] Add `Caption`.
- [x] Add `Admin_Decision`.
- [x] Add `TikTok_Publish_ID`.
- [ ] Confirm final column order.
- [ ] Confirm valid statuses are exactly:

```text
Pending
In Progress
Draft
Approved
Rejected
Published
Publish Failed
```

### Step 2: Phase 3 Draft Output

- [x] Update `src/video_factory.py` merge mode to return `Status = Draft` instead of `Done`.
- [x] Add caption generation output to merge mode.
- [x] Update `workflows/03-video-maker.json` so it writes `Caption`.
- [x] Update workflow row filters so Phase 3 merge still targets only ready `In Progress` rows.
- [ ] Run Phase 3 on one test row and confirm:

```text
Video_Path exists
Caption is filled
Status = Draft
```

### Step 3: Admin Review MVP

- [x] Document admin review rule in docs.
- [ ] Admin manually reviews `Video_Path`.
- [ ] Admin edits `Caption` if needed.
- [ ] Admin sets `Admin_Decision = approve` or `reject`.

### Step 4: Phase 4 Workflow

- [x] Build `workflows/04-auto-publisher.json`.
- [x] Add lane for `Draft + reject -> Rejected`.
- [x] Add lane for `Draft + approve -> Approved`.
- [x] Add lane for `Approved -> TikTok publish`.
- [x] Add error handling so failed publish updates `Status = Publish Failed`.
- [x] Ensure the workflow updates rows by `ID`.

### Step 5: TikTok Token Setup

- [ ] Create TikTok Developer app.
- [ ] Enable Content Posting API.
- [ ] Configure OAuth redirect URI.
- [ ] Authorize target TikTok account.
- [ ] Store access token and refresh token securely.
- [ ] Confirm `video.publish` is granted.
- [ ] Confirm whether app is restricted to private/self-only posting before audit.
- [ ] Confirm the requested `TIKTOK_PRIVACY_LEVEL` is returned by TikTok creator info.

### Step 6: End-to-End Test

- [ ] Run Phase 1 to create a `Pending` row.
- [ ] Run Phase 2 to move it to `In Progress`.
- [ ] Run Phase 3 to create `Draft`.
- [ ] Approve the row in Sheet.
- [ ] Run Phase 4.
- [ ] Confirm final state is one of:

```text
Published
Publish Failed
```

- [ ] If published, verify `TikTok_Publish_ID` and `Published_URL` or `Note`.

## Open Decisions

1. Whether Phase 4 should direct publish with `video.publish` only, or support fallback to `video.upload`.
2. Whether admin review stays Sheet-only for MVP or gets a Telegram/Slack notification.
3. Whether `Published_URL` can be obtained immediately from TikTok or should be filled later after status polling/manual verification.
4. Whether captions should be deterministic at first or generated by Gemini.

Recommended MVP choices:

```text
Direct publish first
Sheet-only admin approval
Deterministic caption template
Store TikTok_Publish_ID even if Published_URL is not immediately available
```
