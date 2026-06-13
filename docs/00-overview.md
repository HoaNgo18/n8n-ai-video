# Project III / n8n AI Video - Overview And Operations

Tài liệu này là bản tổng hợp chính cho project `n8n-ai-video`: project dùng những công cụ nào, gọi API nào, chạy bằng lệnh nào, dữ liệu đi qua các phase ra sao, và các file cấu hình quan trọng nằm ở đâu.

Không ghi API key thật vào docs. Key thật nằm trong `.env`, Google credential JSON, hoặc credential store của n8n.

## 1. Mục tiêu

Project tự động biến bài đăng Threads tiếng Việt thành video ngắn dạng TikTok/Reels/Shorts:

```text
Threads post
  -> mine bài phù hợp
  -> screenshot post + comment
  -> trích text và viết narration script
  -> tạo voice AI
  -> dựng screenshot trên video nền
  -> merge audio + visual
  -> gửi draft cho admin duyệt
  -> publish TikTok khi được approve
```

Output chính là video dọc `1080x1920`, lưu trong:

```text
runtime/data/videos/YYYY-MM-DD/<ID>/final.mp4
```

## 2. Kiến trúc tổng quan

Project có 2 service Docker chính:

```text
n8n
  - Image: n8nio/n8n:2.17.8
  - URL: http://localhost:5678
  - Chạy automation workflow
  - Đọc/ghi Google Sheet
  - Gọi Gemini node
  - Gọi runner API qua HTTP

n8n-runner
  - FastAPI app trong runner/app.py
  - URL nội bộ/host: http://localhost:8000
  - n8n gọi bằng: http://host.docker.internal:8000/...
  - Chạy Python scripts trong src/
  - Dùng Playwright, ffmpeg, VieNeu, yt-dlp...
```

File Docker:

```text
docker-compose.yml
runner/Dockerfile
runner/requirements.txt
```

Các volume quan trọng:

```text
./:/workspace
./runtime:/home/node/.n8n-files/runtime
../project-iii-492308-fb6a394893e0.json:/workspace/google-service-account.json:ro
E:/n8n-ai-video-cache:/models
```

`/workspace` trong container chính là folder project `n8n-ai-video`.

## 3. Thư mục chính

```text
n8n-ai-video/
  docs/
    00-overview.md              Tài liệu tổng hợp này
    01-gemini-prompts.md        Ghi chú prompt Gemini
    02-database-schema.md       Schema Google Sheet
    03-progress-tracker.md      Tiến độ/lịch sử thay đổi
    04-docker-runtime.md        Ghi chú Docker runtime
    05-quality-growth-roadmap.md Roadmap chất lượng

  workflows/
    01-threads-miner.json
    02-screenshot-extract.json
    03-video-maker.json
    04-review&publish.json

  runner/
    app.py                      FastAPI endpoints cho n8n
    Dockerfile
    requirements.txt

  src/
    threads_miner.py            Phase 1
    screenshot_extractor.py     Phase 2
    video_factory.py            Phase 3
    phase4_compact_helper.py    Phase 4 helper
    draft_review_helper.py      Local signed review link helper
    manual_upload_helper.py     Manual export helper
    tiktok_playwright_publisher.py
    tiktok_publisher.py
    tiktok_auth.py

  runtime/
    assets/                     Video nền, voice sample, asset dùng lại
    data/                       Screenshot, audio, visual, final video
    debug/                      Debug HTML/PNG/log khi lỗi
    storage/                    Browser session/state
    cache/                      Cache nhỏ
    samples/                    Sample input/output
    outputs/                    Output test/dev cũ

  .env                          Cấu hình thật, không commit
  .env.example                  Mẫu cấu hình, có thể commit
  docker-compose.yml
  README.md
```

## 4. Các workflow n8n

### Phase 1 - Threads Miner

File:

```text
workflows/01-threads-miner.json
src/threads_miner.py
```

Chức năng:

- Dùng Playwright mở Threads.
- Dùng session trong `runtime/storage/threads-state.json`.
- Mine candidate posts từ Threads feed.
- Lọc bài tiếng Việt, lọc engagement, lọc content fit.
- n8n dùng Gemini classifier để quyết định bài có đáng làm video không.
- Ghi row mới vào Google Sheet tab `Threads`.
- Set `Status = Pending`.

Endpoint runner:

```text
POST http://host.docker.internal:8000/phase1/threads-miner
```

Biến `.env` liên quan:

```env
THREADS_USERNAME=
THREADS_PASSWORD=
THREADS_STORAGE_STATE=runtime/storage/threads-state.json
THREADS_DEBUG_DIR=runtime/debug
THREADS_FORCE_LOGIN=false
MAX_POSTS=15
SCROLL_COUNT=20
MIN_ENGAGEMENT_SCORE=1000
MIN_STRONGEST_METRIC_SCORE=1000
CANDIDATE_LIMIT=75
MIN_CONTENT_FIT_SCORE=2
THREADS_CLASSIFIER_ENABLED=true
THREADS_CLASSIFIER_MODEL=gemini-2.5-flash-lite
```

### Phase 2 - Screenshot + Extract

File:

```text
workflows/02-screenshot-extract.json
src/screenshot_extractor.py
```

Chức năng:

- Đọc row `Status = Pending`.
- Dùng Playwright Chromium mở `Source_URL`.
- Screenshot bài post chính.
- Screenshot comment/continuation phù hợp.
- Trích visible text từ page.
- Dọn text, bỏ UI noise, bỏ nested media card trong post.
- n8n dùng Gemini rewrite để tạo `Narrator_Script` tự nhiên hơn.
- Set `Status = In Progress`.

Endpoint runner:

```text
POST http://host.docker.internal:8000/phase2/screenshot-extract
```

Output:

```text
runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png
runtime/data/screenshots/YYYY-MM-DD/<ID>/comments/comment_01.png
```

Biến `.env` liên quan:

```env
SCREENSHOTS_DIR=runtime/data/screenshots
THREADS_SCREENSHOT_THEME=dark
THREADS_SCREENSHOT_DPR=2
SCRIPT_REWRITE_ENABLED=true
SCRIPT_REWRITE_MODEL=gemini-2.5-flash
SCRIPT_REWRITE_BASE_URL=https://generativelanguage.googleapis.com/v1beta
GEMINI_API_KEY=
```

Chạy thủ công trong runner:

```powershell
docker compose exec -T runner python src/screenshot_extractor.py --id "POST_ID" --url "https://www.threads.com/@user/post/POST_ID"
```

### Phase 3 - Video Maker

File:

```text
workflows/03-video-maker.json
src/video_factory.py
```

Phase 3 chia thành 3 lane:

```text
3A Voice Generator
  input: Narrator_Script, Extracted_Content
  output: Audio_Path, Audio_Timing

3B Visual Builder
  input: Screenshots, Audio_Path, Audio_Timing
  output: Visual_Video_Path

3C Merge Final
  input: Audio_Path, Visual_Video_Path
  output: Video_Path, Caption, Status = Draft
```

Endpoints runner:

```text
POST http://host.docker.internal:8000/phase3/voice
POST http://host.docker.internal:8000/phase3/visual
POST http://host.docker.internal:8000/phase3/merge
```

Output:

```text
runtime/data/audio/YYYY-MM-DD/<ID>/narration.wav
runtime/data/audio/YYYY-MM-DD/<ID>/audio_timing_debug.json
runtime/data/visuals/YYYY-MM-DD/<ID>/visual.mp4
runtime/data/visuals/YYYY-MM-DD/<ID>/overlays/overlay_XX.png
runtime/data/videos/YYYY-MM-DD/<ID>/final.mp4
```

Biến `.env` liên quan:

```env
AUDIO_DIR=runtime/data/audio
VISUALS_DIR=runtime/data/visuals
VIDEOS_DIR=runtime/data/videos
TEMP_DIR=runtime/data/temp

VIDEO_WIDTH=1080
VIDEO_HEIGHT=1920
OVERLAY_WIDTH_RATIO=0.98
OVERLAY_TOP_RATIO=0.24
OVERLAY_CORNER_RADIUS_RATIO=0.035
OVERLAY_SHADOW_OPACITY=105
OVERLAY_ANIMATION=fade_slide
OVERLAY_ANIMATION_SECONDS=0.35
OVERLAY_SLIDE_PIXELS=56
VISUAL_TIMING_LEAD_SECONDS=0.0

VIDEO_ENCODE_PRESET=medium
VIDEO_CRF=16
VIDEO_TARGET_BITRATE=14M
VIDEO_MAXRATE=18M
VIDEO_BUFSIZE=36M
AUDIO_BITRATE=192k
```

### Phase 4 - Review + Publish

File:

```text
workflows/04-review&publish.json
src/phase4_compact_helper.py
src/draft_review_helper.py
src/tiktok_playwright_publisher.py
src/tiktok_publisher.py
```

Chức năng:

- Đọc row `Status = Draft`.
- Tạo signed review link từ runner, không upload lên Drive.
- Gửi link review cho admin.
- Gửi Telegram message có nút approve/reject.
- Webhook Telegram callback nhận quyết định admin.
- Nếu approve, publish bằng TikTok browser uploader.
- Ghi `TikTok_Publish_ID`, `Published_URL`, `Status`, `Note`.

Endpoint runner chính:

```text
POST http://host.docker.internal:8000/phase4/compact
POST http://host.docker.internal:8000/phase4/publish
POST http://host.docker.internal:8000/phase4/draft-review
POST http://host.docker.internal:8000/phase4/manual-upload
POST http://host.docker.internal:8000/phase4/telegram-callback
```

Biến `.env` liên quan:

```env
TELEGRAM__PHASE4_BOT_TOKEN=
TELEGRAM_PHASE1_BOT_TOKEN=
TELEGRAM_CHAT_ID=

REVIEW_PUBLIC_BASE_URL=https://example.ngrok-free.app
REVIEW_TOKEN_SECRET=
N8N_PHASE4_CALLBACK_URL=http://n8n:5678/webhook/phase4-review-callback

TIKTOK_PUBLISHER_MODE=playwright
TIKTOK_SESSION_ID=
TIKTOK_UPLOADER_COOKIES_PATH=tiktok_cookies.json
TIKTOK_USERNAME=
TIKTOK_PASSWORD=
TIKTOK_UPLOAD_BROWSER=chromium
TIKTOK_UPLOAD_HEADLESS=true
TIKTOK_UPLOAD_RETRIES=1
```

## 5. Google Sheet

Tab:

```text
Threads
```

Các cột chính:

```text
ID
Source_URL
Collected_At
Status
Screenshots
Extracted_Content
Narrator_Script
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
```

Status flow:

```text
Pending
  -> In Progress
  -> Draft
  -> Approved / Rejected
  -> Published / Failed
```

Xem chi tiết schema ở:

```text
docs/02-database-schema.md
```

## 6. AI/API đang dùng

### Gemini

Dùng trong n8n qua node:

```text
@n8n/n8n-nodes-langchain.googleGemini
```

Vai trò:

- Phase 1: classify candidate Threads post.
- Phase 2: rewrite/normalize narrator script.
- Phase 3C: tạo TikTok caption + hashtag.

Biến `.env`:

```env
GEMINI_API_KEY=
THREADS_CLASSIFIER_MODEL=gemini-2.5-flash-lite
SCRIPT_REWRITE_MODEL=gemini-2.5-flash
SCRIPT_REWRITE_BASE_URL=https://generativelanguage.googleapis.com/v1beta
AI_CAPTION_MODEL=gemini-2.5-flash-lite
AI_CAPTION_ENABLED=true
```

Docs prompt riêng:

```text
docs/01-gemini-prompts.md
```

### Google Sheets

Dùng để lưu state toàn pipeline.

Nơi dùng:

- n8n Google Sheets node.
- `src/phase4_compact_helper.py` khi cần helper đọc/ghi sheet.

Credential:

```text
Google service account JSON
/workspace/google-service-account.json trong runner
```

Biến `.env`:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=/workspace/google-service-account.json
GOOGLE_SHEET_ID=
GOOGLE_SHEET_TAB=Threads
```

### Local Review Server

Dùng để cho admin xem file MP4 gốc trực tiếp từ runner, tránh Drive preview bị nén/xấu và tránh mất thời gian upload file lớn.

Nơi dùng:

- `runner/app.py`: phục vụ `/review/<ID>`, `/review/<ID>/video`, `/review/<ID>/download`.
- `src/draft_review_helper.py`: tạo signed review link.
- `src/local_review.py`: ký và xác thực token.

Biến `.env`:

```env
REVIEW_PUBLIC_BASE_URL=https://example.ngrok-free.app
REVIEW_TOKEN_SECRET=
N8N_PHASE4_CALLBACK_URL=http://n8n:5678/webhook/phase4-review-callback
```

### Telegram Bot API

Dùng để gửi review link và nhận approve/reject callback.

Nơi dùng:

```text
src/phase4_compact_helper.py
workflows/04-review&publish.json
```

API gọi:

```text
https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/sendMessage
https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/sendVideo
https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/answerCallbackQuery
https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/editMessageText
```

Không ghi token thật vào docs.

### TikTok

Có 2 hướng trong code:

```text
src/tiktok_playwright_publisher.py
  - Đường thực tế đang dùng
  - Dùng tiktok-uploader + browser/cookie/session

src/tiktok_publisher.py
  - Đường TikTok Content Posting API
  - Dùng khi OAuth/app setup đầy đủ
```

Biến `.env` cho browser uploader:

```env
TIKTOK_PUBLISHER_MODE=playwright
TIKTOK_SESSION_ID=
TIKTOK_UPLOADER_COOKIES_PATH=tiktok_cookies.json
TIKTOK_USERNAME=
TIKTOK_PASSWORD=
```

Biến `.env` cho TikTok API path:

```env
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=
TIKTOK_ACCESS_TOKEN=
TIKTOK_REFRESH_TOKEN=
TIKTOK_PRIVACY_LEVEL=SELF_ONLY
```

## 7. Screenshot dùng gì

Screenshot dùng:

```text
Playwright
Chromium trong Docker
```

Dependency:

```text
playwright
chromium
fonts-noto-core
fonts-noto-cjk
fonts-noto-color-emoji
```

File code:

```text
src/threads_miner.py
src/screenshot_extractor.py
```

Selector chính:

```text
div[data-pressable-container="true"]
```

Output screenshot:

```text
runtime/data/screenshots/YYYY-MM-DD/<ID>/post.png
runtime/data/screenshots/YYYY-MM-DD/<ID>/comments/comment_XX.png
```

Lệnh test Phase 2:

```powershell
docker compose exec -T runner python src/screenshot_extractor.py --id "POST_ID" --url "https://www.threads.com/@user/post/POST_ID"
```

Debug khi lỗi:

```text
runtime/debug/screenshot_extract_failure_<ID>.html
runtime/debug/screenshot_extract_failure_<ID>.png
```

## 8. Video nền

Folder video nền:

```text
runtime/assets/backgrounds/
```

Định dạng hỗ trợ:

```text
.mp4
.mov
.mkv
.webm
```

Chọn 1 video nền cố định trong `.env`:

```env
BACKGROUND_VIDEO_PATH=runtime/assets/backgrounds/my_background.mp4
BACKGROUND_VIDEO_DIR=
```

Chọn tự động trong folder:

```env
BACKGROUND_VIDEO_PATH=runtime/assets/background.mp4
BACKGROUND_VIDEO_DIR=runtime/assets/backgrounds
BACKGROUND_VIDEO_PICK=hash
```

Mode chọn nền:

```text
hash   - cùng post ID sẽ luôn chọn cùng một nền
random - mỗi lần render chọn ngẫu nhiên
first  - lấy file đầu tiên theo alphabet
```

Code chọn nền:

```text
src/video_factory.py
choose_background_video()
```

## 9. Tải video YouTube làm nền

Tool:

```text
yt-dlp
```

Dependency:

```text
runner/requirements.txt -> yt-dlp
```

Lệnh tải trong Docker runner:

```powershell
docker compose exec -T runner python -m yt_dlp -f "bestvideo[height<=1080][ext=mp4]+bestaudio/best" --merge-output-format mp4 -o "/workspace/runtime/assets/backgrounds/%(title).120s.%(ext)s" "https://www.youtube.com/watch?v=VIDEO_ID"
```

Lệnh này nên chạy ở folder có `docker-compose.yml`:

```text
D:\Project III\n8n-ai-video
```

Nếu muốn tải bằng Python local thay vì Docker:

```powershell
python -m yt_dlp -f "bestvideo[height<=1080][ext=mp4]+bestaudio/best" --merge-output-format mp4 -o "runtime/assets/backgrounds/%(title).120s.%(ext)s" "https://www.youtube.com/watch?v=VIDEO_ID"
```

Sau khi tải xong, set `.env` nếu muốn dùng file đó cố định:

```env
BACKGROUND_VIDEO_PATH=runtime/assets/backgrounds/TEN_FILE.mp4
BACKGROUND_VIDEO_DIR=
```

## 10. Voice AI / TTS

File code chính:

```text
src/video_factory.py
```

TTS hiện tại chỉ dùng VieNeu, cấu hình bằng `.env`:

```env
TTS_ENGINE_ORDER=vieneu
```

Nếu `.env` còn sót engine khác trong `TTS_ENGINE_ORDER`, runner sẽ ignore và vẫn chỉ gọi VieNeu.

Ví dụ:

```env
TTS_ENGINE_ORDER=vieneu
```

Engine hợp lệ:

```text
vieneu
```

### VieNeu

Dependency:

```text
vieneu==3.0.4
```

Biến `.env`:

```env
VIENEU_TTS_ENABLED=true
VIENEU_MODE=v3turbo
VIENEU_BACKBONE_REPO=
VIENEU_BACKBONE_DEVICE=
VIENEU_CODEC_REPO=
VIENEU_CODEC_DEVICE=
VIENEU_VOICE_REF=
VIENEU_VOICE_REF_TEXT=
```

VieNeu hỗ trợ voice registry trong project này. Mỗi item có thể là preset voice
hoặc file audio mẫu:

```env
TTS_AUTHOR_VOICES=preset:voice_1,preset:voice_2,preset:voice_3,preset:voice_4
TTS_AUTHOR_VOICES=ref:runtime/assets/voices/a.wav,ref:runtime/assets/voices/b.wav
```

Khi `TTS_ENGINE_ORDER=vieneu`, dạng `preset:` và `ref:` được dùng cho VieNeu.

VieNeu vẫn có thể dùng một global voice reference:

```env
VIENEU_VOICE_REF=runtime/assets/voices/my_voice.wav
```

Ý nghĩa:

```text
Bạn cung cấp file audio mẫu -> VieNeu encode chất giọng -> đọc text mới bằng giọng gần giống mẫu.
```

Khuyến nghị file mẫu:

```text
10-30 giây hoặc hơn
1 người nói
ít noise
không nhạc nền
có quyền sử dụng giọng
```

Model/cache được đẩy sang ổ E trong Docker compose:

```env
HF_HOME=/models/huggingface
HUGGINGFACE_HUB_CACHE=/models/huggingface/hub
```

Host mount:

```text
E:/n8n-ai-video-cache:/models
```

Trong Docker Linux path này thường không phải lựa chọn chính.

## 11. Dựng video dùng gì

Tool chính:

```text
ffmpeg
imageio-ffmpeg
Pillow
```

Nơi dùng:

```text
src/video_factory.py
```

Visual builder:

- Lấy background video.
- Loop background nếu video nền ngắn.
- Style screenshot thành card PNG bằng Pillow.
- Overlay screenshot theo `Audio_Timing`.
- Render silent `visual.mp4`.

Merge:

- Input 1: `visual.mp4`
- Input 2: `narration.wav`
- Output: `final.mp4`

Các setting quan trọng:

```env
VIDEO_WIDTH=1080
VIDEO_HEIGHT=1920
VIDEO_CRF=16
VIDEO_TARGET_BITRATE=14M
VIDEO_MAXRATE=18M
VIDEO_BUFSIZE=36M
AUDIO_BITRATE=192k
```

## 12. Runner API

File:

```text
runner/app.py
```

Health:

```text
GET /health
```

Endpoints:

```text
POST /phase1/threads-miner
POST /phase2/screenshot-extract
POST /phase3/voice
POST /phase3/visual
POST /phase3/merge
POST /phase4/draft-review
POST /phase4/manual-upload
POST /phase4/compact
POST /phase4/publish
POST /phase4/telegram-callback
GET /review/<ID>
GET /review/<ID>/video
GET /review/<ID>/download
```

Health check:

```powershell
docker compose exec -T runner python -c "import requests; print(requests.get('http://localhost:8000/health').text)"
```

Hoặc từ host:

```powershell
curl http://localhost:8000/health
```

## 13. Lệnh Docker/n8n thường dùng

Chạy stack:

```powershell
docker compose up -d
```

Build lại runner:

```powershell
docker compose build runner
docker compose up -d runner
```

Build lại toàn bộ:

```powershell
docker compose up -d --build
```

Xem logs runner:

```powershell
docker compose logs -f runner
```

Xem logs n8n:

```powershell
docker compose logs -f n8n
```

Vào shell runner:

```powershell
docker compose exec runner bash
```

Mở n8n:

```text
http://localhost:5678
```

Import/update workflows từ:

```text
workflows/01-threads-miner.json
workflows/02-screenshot-extract.json
workflows/03-video-maker.json
workflows/04-review&publish.json
```

## 14. Phase 4 review với ngrok

Chỉ cần mở một tunnel vào runner port `8000`. Runner vừa phục vụ review video, vừa nhận Telegram callback rồi forward nội bộ sang n8n.

```powershell
ngrok http 8000
```

Ví dụ URL:

```text
https://example.ngrok-free.app
```

Set `.env`:

```env
REVIEW_PUBLIC_BASE_URL=https://example.ngrok-free.app
N8N_PHASE4_CALLBACK_URL=http://n8n:5678/webhook/phase4-review-callback
```

Restart runner sau khi đổi `.env`:

```powershell
docker compose up -d runner
```

Set Telegram webhook vào runner:

```powershell
curl "https://api.telegram.org/bot<TELEGRAM__PHASE4_BOT_TOKEN>/setWebhook?url=https://example.ngrok-free.app/phase4/telegram-callback"
curl "https://api.telegram.org/bot<TELEGRAM_PHASE1_BOT_TOKEN>/setWebhook?url=https://example.ngrok-free.app/phase1/telegram-search-callback"
```

Trong n8n test mode, tạm đổi `N8N_PHASE4_CALLBACK_URL` sang URL `/webhook-test/...` nội bộ tương ứng. Khi workflow active, đổi lại `/webhook/...`.

Test tạo review link thủ công:

```powershell
docker compose exec -T runner python src/draft_review_helper.py --id "POST_ID" --video-path "runtime/data/videos/YYYY-MM-DD/POST_ID/final.mp4" --caption "test caption"
```

## 15. Runtime folder nào cần giữ

Nên giữ:

```text
runtime/assets/       Video nền, voice sample, asset dùng lại
runtime/storage/      Threads browser session
runtime/data/         Dữ liệu phase output hiện tại
runtime/debug/        Debug khi crawler/screenshot lỗi
runtime/cache/        Cache nhỏ
runtime/samples/      Sample dev
runtime/outputs/      Output test/dev cũ nếu còn cần
```

Có thể dọn khi cần:

```text
runtime/data/temp/<old-id>/
runtime/data/visuals/<date>/<debug-or-test-id>/
runtime/data/audio/<date>/*smoke*/
runtime/data/screenshots/<date>/*check*/
runtime/debug/old_failure_files
```

Không nên xóa nếu Google Sheet còn trỏ tới path đó:

```text
runtime/data/screenshots/
runtime/data/audio/
runtime/data/visuals/
runtime/data/videos/
```

Vì các cột `Screenshots`, `Audio_Path`, `Visual_Video_Path`, `Video_Path`, `Draft_Video_URL` có thể đang phụ thuộc vào các file này.

## 16. Secrets và file không nên commit

Không commit:

```text
.env
project-iii-*.json
google-service-account.json
tiktok_cookies.json
runtime/data/
runtime/storage/
runtime/debug/
runtime/assets/backgrounds/*.mp4
runtime/assets/voices/*.wav
```

Có thể commit:

```text
src/
runner/
workflows/
docs/
.env.example
docker-compose.yml
README.md
```

## 17. Cleanup runtime định kỳ

Script:

```text
scripts/cleanup_runtime.py
```

Dry-run, chỉ in danh sách file cũ có thể xóa:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14
```

Xóa thật sau khi đã xem dry-run:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14 --apply
```

Nếu muốn scan cả final video cũ:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 30 --include-videos
```

Nếu runner chưa có `GOOGLE_SHEET_ID` hoặc service-account file, chỉ dọn temp bằng chế độ không đọc Sheet:

```powershell
docker compose exec -T runner python scripts/cleanup_runtime.py --days 14 --without-sheet
```

Nguyên tắc an toàn:

- Script đọc Google Sheet bằng `GOOGLE_SHEET_ID`, `GOOGLE_SHEET_TAB`, `GOOGLE_SERVICE_ACCOUNT_FILE`.
- Script gom các path `runtime/...` còn nằm trong Sheet.
- Chỉ file cũ hơn `--days` và không còn được Sheet trỏ tới mới được đưa vào danh sách xóa.
- Mặc định là dry-run. Phải thêm `--apply` mới xóa thật.
- Sau khi xóa file, script chỉ xóa folder rỗng nếu folder đó không chứa path còn được Sheet tham chiếu.
- `--without-sheet` chỉ scan `TEMP_DIR`, không đụng `audio`, `visuals`, `screenshots`, hoặc `videos`.

## 18. Quality check sau khi dựng video

Script:

```text
scripts/quality_check.py
```

Check video mới nhất:

```powershell
docker compose exec -T runner python scripts/quality_check.py
```

Check theo post ID:

```powershell
docker compose exec -T runner python scripts/quality_check.py --id "POST_ID"
```

Script kiểm tra:

- `final.mp4` có video stream.
- `final.mp4` có audio stream.
- Kích thước đúng theo `VIDEO_WIDTH` / `VIDEO_HEIGHT`.
- Duration của final không lệch quá xa visual/audio.
- `audio_timing_debug.json` không có segment âm, ngược thời gian, hoặc vượt duration audio.

Nên chạy script này sau mỗi lần chỉnh:

- `VISUAL_TIMING_LEAD_SECONDS`
- `OVERLAY_WIDTH_RATIO`
- screenshot crop/extract
- caption/merge logic
- video encode settings

## 19. Error handling runner/n8n

Runner API nằm ở:

```text
runner/app.py
```

Các endpoint Phase 2/3 hiện đã được bọc fail-soft:

```text
POST /phase2/screenshot-extract
POST /phase3/voice
POST /phase3/visual
POST /phase3/merge
```

Nếu script Python bên dưới lỗi, endpoint trả JSON dạng:

```json
{
  "ID": "POST_ID",
  "Status": "Failed",
  "Note": "Phase ... failed: lỗi cụ thể..."
}
```

Nhờ vậy các Google Sheets update node hiện có vẫn có dữ liệu để ghi `Status = Failed` và `Note = lỗi cụ thể`, thay vì workflow chỉ đỏ node giữa chừng mà Sheet không biết chuyện gì xảy ra.

Phase 1 chưa có row cụ thể để update nếu miner fail. Khi Phase 1 fail thì vẫn nên xem:

```powershell
docker compose logs -f runner
docker compose logs -f n8n
```

## 20. Ghi chú hiện tại

- n8n workflow là orchestration chính.
- Python runner xử lý việc nặng: browser, screenshot, TTS, ffmpeg, upload/publish helper.
- Gemini đang dùng ở n8n, không gọi trực tiếp từ Python chính.
- Screenshot dùng Playwright Chromium.
- YouTube download dùng `yt-dlp`.
- Voice chính đang ưu tiên VieNeu nếu `VIENEU_TTS_ENABLED=true`.
- Video nền có thể set cố định bằng `BACKGROUND_VIDEO_PATH` và để trống `BACKGROUND_VIDEO_DIR`.
- Generated media trong `runtime/` là dữ liệu runtime, không phải source code.
