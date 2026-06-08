# Quality And Growth Roadmap

This document collects practical upgrades for improving TikTok video quality, retention, and engagement in the current pipeline.

## Current Baseline

- Final videos are rendered at `1080x1920`.
- Phase 3A generates voice and `Audio_Timing`.
- Phase 3B builds visual video from a looping background plus Threads screenshot overlays.
- Phase 3C merges visual + voice, then Gemini finalizes `Caption` and hashtags.
- Phase 4 publishes through browser/cookie upload.

The biggest current quality gap is not resolution alone. It is mostly presentation: static screenshots, weak first 3 seconds, no burned-in subtitles, limited audio polish, and generic background motion.

## Recommended Priority

### 1. Caption And Hook Rewrite

Impact: high  
Effort: low

The Phase 3C AI caption node should produce a caption built for comments, not just summarize the post.

Target caption structure:

```text
[1 emoji] Curiosity hook, no full spoiler.
Specific comment prompt.

#3to4LargeTags #2to3NicheTags
```

Examples:

```text
[shock emoji] Cau chuyen nay cang nghe cang thay sai sai.
Neu la ban trong tinh huong nay, ban se xu ly the nao?

#threads #storytime #chuyencuocsong #tamly #xuhuong
```

Implementation notes:

- Strengthen `Gemini AI Caption Request` prompt in `workflows/03-video-maker.json`.
- Ask for `hook`, `comment_prompt`, and `hashtags`, then compose final caption in `Finalize AI Caption`.
- Avoid generic `Ban nghi sao?`; prefer a content-specific question.
- Use 3-4 broad tags plus 2-3 niche tags.
- Keep `Published_URL` optional; `TikTok_Publish_ID` is the machine confirmation.

### 2. First 3-Second Hook Slide

Impact: very high  
Effort: medium

The first seconds decide whether viewers stay. Add a short intro slide before showing screenshots.

Behavior:

- `0.0s -> 2.0s/2.5s`: large text hook.
- Background can be dark/blurred/gradient over the existing dynamic background.
- Then transition to the screenshot overlay.

Hook extraction ideas:

- Prefer a sentence with `?`.
- Prefer sentences containing high-tension terms like `soc`, `khong ngo`, `plot twist`, `la`, `bat ngo`, `cai nhau`, `chia tay`, `mat tich`, `drama`.
- If no strong hook exists, ask Gemini in the caption node to return `video_hook`.

Implementation notes:

- Add `video_hook` into Phase 3C AI caption output or Phase 2 rewrite output.
- Extend `build_visual_ffmpeg()` to render a `drawtext` hook layer for the first 2.5 seconds.
- Keep text inside TikTok safe zone. Avoid bottom UI area.

### 3. Burned-In Subtitles

Impact: high  
Effort: medium/high

Subtitles make the video easier to watch silently and give the eye something to track.

Behavior:

- 1-2 short lines at a time.
- White text with black stroke or shadow.
- Middle-lower placement, not at the bottom where TikTok UI covers content.
- Sync with `Audio_Timing` or segment timing.

Implementation notes:

- Phase 3A already produces `Audio_Timing`.
- Add subtitle text per timing segment.
- Use ffmpeg `drawtext` or generate transparent subtitle PNGs with Pillow.
- Pillow is usually easier for Vietnamese text rendering and styling.

### 4. Overlay Polish

Impact: medium/high  
Effort: medium

Screenshots currently look too raw/static. Make them feel more native and polished.

Recommended visual treatment:

- Rounded corners.
- Light drop shadow behind each screenshot.
- Fade in/out for each overlay.
- Slow zoom or pan for screenshot overlays.
- Configurable vertical placement by content type:
  - `story`: slightly lower or centered for readability.
  - `discussion`: keep post/comment stack higher if comments matter.

Implementation options:

- Pre-process screenshots with Pillow:
  - round corners
  - add shadow
  - export PNG with alpha
- Then overlay processed PNGs in ffmpeg.
- This is usually simpler than trying to do rounded corners directly with ffmpeg filters.

Implemented:

- Screenshot capture uses a configurable `dark` or `light` Threads color scheme.
- Capture defaults to `2x` device scale for sharper Threads text and icons.
- Overlay cards are centered horizontally and vertically at `82%` of the video width.
- Pillow adds subtle rounded corners and a soft shadow before ffmpeg compositing.
- The related settings are exposed through `THREADS_SCREENSHOT_*` and `OVERLAY_*` environment variables.

Fade expression idea:

```text
alpha='if(lt(t,0.15),t/0.15,if(gt(t,dur-0.15),(dur-t)/0.15,1))'
```

Project hooks:

- `OVERLAY_TOP_RATIO` is currently a single global value.
- `parse_content_mode()` can drive `overlay_top_ratio` by `story` vs `discussion`.

### 5. Background Upgrade

Impact: medium  
Effort: low/medium

The background should be visually active but not distracting.

Good background categories:

- slow gameplay loop
- Minecraft/parkour-style movement
- satisfying kinetic loop
- soft abstract particles
- cooking/sand/cleaning loop

Rules:

- Use licensed or self-generated assets.
- Avoid very high contrast behind text.
- Avoid motion that competes with screenshots.

Implementation options:

- Keep `runtime/assets/background.mp4`, but replace with a better loop.
- Support a folder of backgrounds and rotate by content mode.
- Programmatically create motion from a still image using ffmpeg `zoompan`.

### 6. Encode Settings For TikTok

Impact: medium  
Effort: low

Current visual rendering already uses 1080x1920 and high quality CRF. Final export can be tuned for upload quality.

Recommended final merge settings:

```text
-preset medium or fast
-crf 18
-b:v 8M
-maxrate 10M
-bufsize 20M
-c:a aac
-b:a 192k
-ar 44100
```

Notes:

- `medium` is slower but cleaner.
- `fast` is a reasonable middle ground.
- High bitrate gives TikTok more source quality before recompression.
- Keep `+faststart`.

Implementation notes:

- Update `merge_final()` ffmpeg command.
- Optionally expose:
  - `VIDEO_ENCODE_PRESET`
  - `VIDEO_TARGET_BITRATE`
  - `VIDEO_MAXRATE`
  - `VIDEO_BUFSIZE`
  - `AUDIO_BITRATE`

Implemented:

- Final and visual rendering default to `preset=medium`, `crf=18`, `8M` target bitrate,
  `10M` maxrate, `20M` buffer, and `192k` AAC audio.
- Resolution remains TikTok-native `1080x1920` by default and can be changed with
  `VIDEO_WIDTH` and `VIDEO_HEIGHT`.

### 7. Voice Quality

Impact: high  
Effort: low/medium

The current TTS chain already supports FPT first, then `edge-tts`, `gTTS`, Windows SAPI, and silent fallback.

Recommended near-term setup:

```env
TTS_VOICE=vi-VN-HoaiMyNeural
TTS_DISCUSSION_VOICES=vi-VN-HoaiMyNeural,vi-VN-NamMinhNeural
FPT_TTS_VOICE=banmai
FPT_TTS_SPEED=0
```

Voice strategy:

- Keep `vi-VN-HoaiMyNeural` as default female narrator.
- Use `vi-VN-NamMinhNeural` for comments in `discussion` mode.
- Test `DEFAULT_EDGE_TTS_RATE` from `+12%` to `+15%` or `+18%`.
- Keep first hook sentence slower than the rest if possible.

Script preprocessing:

- Insert light punctuation after 12-15 words if a sentence has no punctuation.
- Normalize slang before TTS.
- Split hook from body:
  - hook at `+0%` or `+6%`
  - body at `+12%` to `+18%`
- Add short pauses between segments.

Implementation notes:

- Add `TTS_HOOK_RATE` and `TTS_BODY_RATE`.
- Add punctuation normalization before segment generation.
- Use existing `discussion_voices` and `content_mode == "discussion"` path.

### 8. Background Music And Ducking

Impact: medium/high  
Effort: medium

Music makes videos feel less empty, but it must not fight the voice.

Recommended behavior:

- Add light background music at about `-25dB` to `-18dB`.
- Duck music while voice is active.
- Keep music optional and configurable.

Implementation notes:

- Add `BACKGROUND_MUSIC_PATH`.
- Add `BACKGROUND_MUSIC_VOLUME`, default around `0.08`.
- Add ffmpeg mix in `merge_final()`:
  - input 0: visual
  - input 1: voice
  - input 2: music
  - loop music to duration
  - lower music volume
  - mix voice + music into final audio

Rights note:

- Do not bake unlicensed trending TikTok sounds into the file.
- If trend sound is needed, add it inside TikTok or extend uploader support only when reliable.

### 9. Timing And Retention Tweaks

Impact: medium  
Effort: low/medium

Recommended defaults:

```env
VISUAL_TIMING_LEAD_SECONDS=0.3
MIN_OVERLAY_SECONDS=2.5
```

Notes:

- Showing an image slightly before the voice helps viewers orient.
- Each screenshot should stay long enough to read.
- Very short segments should be padded or combined.

Implementation notes:

- `VISUAL_TIMING_LEAD_SECONDS` already exists.
- Add `MIN_OVERLAY_SECONDS` to `build_overlay_plan()`.
- Avoid extending video too much; combine tiny segments when possible.

### 10. Cliffhanger Mode

Impact: experimental/high  
Effort: medium/high

This can drive comments but must be used carefully. Overuse will reduce trust.

Behavior:

- For long `story` content, cut just before resolution.
- End with a text card:

```text
Ban doan ket qua the nao?
```

Implementation notes:

- Add `has_cliffhanger` and `cliffhanger_cut_point` to content segmentation.
- Only enable for stories that are naturally long.
- Do not apply to sensitive or serious topics where it feels exploitative.

## Suggested Implementation Order

1. Caption rewrite v2:
   - output `hook`, `comment_prompt`, `hashtags`, optional `video_hook`
   - compose better final caption

2. Encode settings:
   - `preset=fast/medium`
   - video bitrate caps
   - `-b:a 192k`

3. Hook slide:
   - first 2.5 seconds
   - large safe-zone text

4. Overlay polish:
   - Pillow rounded corners + shadow
   - fade in/out
   - content-mode placement

5. Voice rate and multi-voice:
   - `vi-VN-HoaiMyNeural`
   - `vi-VN-NamMinhNeural`
   - faster body rate, slower hook

6. Subtitles:
   - synced to `Audio_Timing`
   - styled for TikTok readability

7. Background music:
   - optional licensed music
   - low volume + ducking

8. Dynamic background rotation:
   - multiple licensed loops
   - choose by content mode

9. Cliffhanger mode:
   - story-only experiment
   - enable after baseline quality is stable

## Quick Win Checklist

- [ ] Strengthen Phase 3C AI caption prompt.
- [x] Add `VIDEO_ENCODE_PRESET=fast` or `medium`.
- [x] Add `AUDIO_BITRATE=192k`.
- [ ] Set `VISUAL_TIMING_LEAD_SECONDS=0.3`.
- [ ] Add `MIN_OVERLAY_SECONDS=2.5`.
- [ ] Set `TTS_DISCUSSION_VOICES=vi-VN-HoaiMyNeural,vi-VN-NamMinhNeural`.
- [ ] Test `DEFAULT_EDGE_TTS_RATE=+15%`.
- [ ] Add hook slide.
- [x] Add rounded screenshot + shadow preprocessing.
- [ ] Add burned-in subtitles.
