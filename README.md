# AutoCC

**Tiếng Việt** | [English](README_EN.md)

AutoCC là ứng dụng local-first để tạo, chỉnh sửa, dịch thuật và lồng tiếng phụ đề video:

- **Import phụ đề:** Hỗ trợ định dạng `.srt` và `.vtt`.
- **Nhận dạng giọng nói (STT):** Nhận dạng offline bằng `faster-whisper` local hoặc cloud qua Deepgram Nova-3.
- **Phân tách người nói (Diarization):** Hỗ trợ diarization âm thanh từ Deepgram; AI ngữ cảnh phân tích hội thoại để tách thêm lượt thoại bên trong cue bằng dấu xuống dòng mà không chèn nhãn `[S1]`/`[S2]` nhân tạo.
- **Dịch thuật AI:** Dịch từng batch qua endpoint LLM tương thích chuẩn OpenAI (`/chat/completions`), kết nối mượt mà với Ollama, LM Studio hoặc bất kỳ endpoint hosted API nào.
- **Phong cách dịch & Thuật ngữ riêng:** Presets phong phú (Hán Việt, Phim Hàn, Anime Nhật, GenZ, Trang trọng), ghim glossary thuật ngữ, hỗ trợ lưu và quản lý Custom Translation Styles.
- **Lồng tiếng tự động (TTS Dubbing):** Đọc bản dịch bằng Microsoft Edge TTS hoặc mock voice; tự động co giãn thời lượng khớp cue qua 3 tầng (Tăng tốc PCM, Tràn khoảng lặng, Rút gọn bằng LLM); trộn audio đè lên video gốc kèm Audio Ducking.
- **Theo dõi tiến độ thời gian thực:** Giám sát tiến trình transcription, translation, diarization và dubbing trực tiếp qua Server-Sent Events (SSE) với cơ chế tự động kết nối lại (auto-reconnect).
- **Giao diện dựng phim chuyên nghiệp (NLE UI):** Màn hình xem trước video kèm phụ đề responsive, timeline tương tác kéo thả mượt mà với hiển thị dạng sóng âm thanh (waveform).
- **Quản lý Dự án (Dashboard):** Màn hình danh sách dự án trực quan: xem ảnh đại diện thumbnail, thời lượng, tiến độ dịch, dung lượng đĩa, tìm kiếm, lọc và xóa dự án.
- **Chạy lại phiên âm tức thời:** Mở lại project cũ và chạy lại nhận dạng ngay trên video đã lưu ở server mà không cần tải lại file.
- **Biên tập Timeline linh hoạt:** Kéo thân clip để dời mốc thời gian, kéo mép để co giãn, cắt/gộp cue tại playhead, hít dính (snapping) thông minh vào mép cue và playhead.
- **Tua mượt mà:** Kéo timeline khi đang phát sẽ tạm dừng video theo cử chỉ và tự động phát tiếp khi thả chuột.
- **Lịch sử Undo / Redo đa cấp:** Hoàn tác và làm lại mọi thao tác chỉnh sửa; các đợt gõ phím nhanh được gộp thông minh thành một bước.
- **Cảnh báo tốc độ đọc (CPS):** Cảnh báo trực quan Characters-Per-Second ngay trên clip timeline và thanh inspector theo ngưỡng 17/21 ký tự/giây.
- **Giao diện tùy biến & Đa ngôn ngữ:** Nhớ kích thước các cột và chiều cao timeline giữa các phiên làm việc, hỗ trợ giao diện Sáng / Tối và song ngữ Tiếng Việt / Tiếng Anh.
- **Xuất file & Muxing:** Xuất file SRT/VTT chuẩn hoặc ghép soft subtitle / audio lồng tiếng vào video MP4 bằng FFmpeg.

---

## Chạy trên Windows

**Yêu cầu:** Python 3.10 trở lên (khuyên dùng Python 3.12). Ứng dụng tự động tìm `ffmpeg` trong `PATH` hệ thống; nếu chưa cài, backend sẽ tự động dùng binary kèm theo từ `imageio-ffmpeg`.

```powershell
cd E:\Project2025\AutoCC
Copy-Item .env.example .env
.\run.ps1
```

Mở trình duyệt tại [http://127.0.0.1:8000](http://127.0.0.1:8000).

> **Lưu ý:** Lần đầu chạy với `faster-whisper`, model sẽ được tải về cache của máy. Bạn có thể chọn `tiny`, `base`, `small`, `medium` hoặc `large-v3`; `small` là lựa chọn cân bằng tối ưu cho CPU.

---

## Nhận dạng nhiều người bằng Deepgram

Tạo API key trên Deepgram, sau đó cấu hình trong `.env`:

```dotenv
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-api-key
DEEPGRAM_MODEL=nova-3
DEEPGRAM_DIARIZE_MODEL=latest
```

Khởi động lại AutoCC. Ứng dụng gửi audio tới API `/v1/listen` với `utterances=true`, `smart_format=true` và diarizer mới nhất. Nếu để ngôn ngữ là “Tự nhận diện”, app dùng tính năng phát hiện ngôn ngữ tự động; chọn ngôn ngữ cụ thể sẽ khóa model theo mã ngôn ngữ đó.

Khi chọn Deepgram trên giao diện, dropdown model sẽ hiển thị `nova-3`, `nova-2`, `nova-2-meeting` hoặc `nova-2-video`. `nova-3` phù hợp nhất cho video thông thường và nhiều người nói.

### AI phân tích lượt thoại hai tầng

1. **Tầng 1 (Acoustic):** Backend sử dụng thông tin `speaker` và `speaker_confidence` ở từng từ của Deepgram để chèn ngắt dòng theo giọng nói.
2. **Tầng 2 (Contextual LLM):** LLM phân tích ngữ cảnh hội thoại (hỏi/đáp, đại từ) để bổ sung ranh giới khi Deepgram gộp nhiều người vào một câu.

Model chỉ được phép chèn thêm ký tự xuống dòng; backend đối chiếu lại toàn bộ nội dung ký tự để đảm bảo không bị mất chữ. Nút **Phân tích lại lượt thoại** cho phép chạy lại riêng bước này trên các cue hiện tại mà không tốn chi phí gọi lại STT.

---

## Dịch thuật bằng AI (LLM)

Mặc định ứng dụng kết nối tới Ollama local:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

Nếu sử dụng endpoint LLM khác (LM Studio, vLLM hoặc Cloud API như OpenAI, DeepSeek, Mistral), cấu hình trong `.env`:

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=[key-1, key-2]
LLM_MODEL=gpt-4o-mini
# Không đặt thì dùng lại LLM_MODEL
SPEAKER_ANALYSIS_MODEL=
```

> **Mẹo xoay vòng API Keys:** Bạn có thể điền nhiều key dạng mảng `[key1, key2]`. Hệ thống sẽ tự động xoay vòng Round-Robin và tự động đưa key vào chế độ Cooldown 60s khi gặp lỗi 429 (Rate Limit).

Có thể dịch offline bằng model Transformers chuyên biệt (ví dụ En $\rightarrow$ Vi):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-translation-local.txt
```

```dotenv
TRANSLATION_PROVIDER=transformers
TRANSLATION_MODEL=Helsinki-NLP/opus-mt-en-vi
TRANSFORMERS_TARGET_LANGUAGE=Tiếng Việt
TRANSFORMERS_DEVICE=auto
```

### Cơ chế Contextual Batching & Khôi phục lỗi JSON

- **Ngữ cảnh liên tục:** Mỗi batch gửi kèm 4 câu trước (gồm cả bản dịch) và 2 câu sau làm ngữ cảnh chỉ đọc, giúp LLM dịch chuẩn danh xưng của nhân vật.
- **Thuật ngữ xuyên suốt (Glossary):** Bảng thuật ngữ nhân vật được tự động tích lũy và truyền sang các batch kế tiếp (tối đa 40 mục).
- **JSON Repair Engine:** Phản hồi từ LLM được đối soát theo đúng mã dòng. Nếu LLM trả sai cấu trúc hoặc gộp dòng, backend tự động cô lập và thử lại câu bị thiếu mà không làm hỏng cả batch.

### Phong cách dịch & Custom Styles

- **Presets:** `Tự động theo ngôn ngữ nguồn` (Tiếng Trung $\rightarrow$ Hán Việt, Tiếng Hàn $\rightarrow$ Oppa/Unnie, Tiếng Nhật $\rightarrow$ Senpai/-san), `Trung tính`, `GenZ / Khẩu ngữ`, `Trang trọng`.
- **Quy tắc riêng & Glossary ghim:** Nhập quy tắc dạng `大哥 → đại ca` hoặc `陛下 = bệ hạ` để ghi đè thuật ngữ.
- **Lưu Preset tùy chỉnh:** Bạn có thể lưu phong cách vừa tạo vào hệ thống (lưu tại `runtime/styles.json`) để tái sử dụng cho các dự án sau.

Preset mặc định nằm trong [backend/domain/translation/style.py](backend/domain/translation/style.py).

---

## Lồng tiếng tự động (TTS Dubbing)

Sau khi có bản dịch, AutoCC tổng hợp giọng đọc cho từng câu, căn khớp thời lượng và ghép thành track audio hoàn chỉnh đè lên video.

Mặc định sử dụng `edge-tts` (Microsoft Neural TTS miễn phí). Cấu hình trong `.env`:

```dotenv
TTS_PROVIDER=edge
TTS_VOICE=vi-VN-HoaiMyNeural
DUB_ORIGINAL_GAIN=0.25
```

Các giọng đọc tiếng Việt chất lượng cao: `vi-VN-HoaiMyNeural` (Nữ) và `vi-VN-NamMinhNeural` (Nam).

### Thuật toán căn khớp thời lượng (Fit Segment) 3 cấp

1. **Đọc nhanh hơn (`retime_pcm`):** Tăng tốc độ đọc lên tới trần `DUB_MAX_SPEEDUP` (mặc định `1.25x`), giữ nguyên cao độ âm thanh.
2. **Tràn sang khoảng lặng (`spill`):** Cho phép câu nói kéo dài sang khoảng lặng phía sau tối đa `DUB_MAX_SPILL_SECONDS` (mặc định `1.2s`).
3. **Rút ngắn câu bằng LLM (`shorten_with_llm`):** Tự động nhờ LLM viết lại câu ngắn hơn theo số ký tự cho phép rồi thu âm lại (tắt được bằng `DUB_SHORTEN_WITH_LLM=false`).

### Cảnh báo lệch giọng khi sửa phụ đề (`dub_stale`)

Mỗi lần lồng tiếng, hệ thống lưu mã băm SHA-256 (`dubbing_fingerprint`) của toàn bộ lời thoại và mốc thời gian. Nếu sau đó bạn chỉnh sửa phụ đề, giao diện sẽ hiện cảnh báo phụ đề đã thay đổi so với bản lồng tiếng cũ.

---

## Kiểm tra nhanh & Chạy Tests

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m pytest
py -3.12 -m compileall backend
```

---

## Phát triển giao diện (Tailwind CSS v4)

Giao diện sử dụng Tailwind CSS v4 standalone binary (không cần cài đặt Node.js/npm):

- **Chế độ phát triển (Watch mode):**
  ```powershell
  .\build-css.ps1 -Watch
  ```
- **Build tối ưu (Production):**
  ```powershell
  .\build-css.ps1
  ```
- File nguồn: `frontend/styles/input.css` (gồm `@import "tailwindcss";` và `@import "./custom.css";`).
- File đích: `frontend/styles.css` (được nạp trực tiếp vào `frontend/index.html`).

---

## Kiến trúc Hệ Thống

Chi tiết kiến trúc chuyên sâu: xem [BACKEND_ARCHITECTURE.md](BACKEND_ARCHITECTURE.md).

```text
frontend/
├── index.html                     # Shell 3 pane: Inspector · Stage + Timeline · Danh sách Cue
├── app.js                         # Entry point: Khởi tạo i18n & mount các feature modules
├── core/                          # Nền tảng Frontend cốt lõi
│   ├── api.js                     # Mọi lời gọi HTTP/SSE tới Backend
│   ├── confirm.js                 # Modal hộp thoại xác nhận tương tác
│   ├── dom.js                     # $, element(), pointer capture utilities
│   ├── feedback.js                # Toast thông báo, status bar, save state indicator
│   ├── format.js                  # Timecode, tốc độ đọc CPS, định dạng văn bản
│   ├── i18n.js                    # Quản lý ngôn ngữ hiển thị (vi/en)
│   ├── icons.js                   # SVG icons template registry
│   ├── router.js                  # Điều hướng màn hình (data-screen + URL hash)
│   └── store.js                   # State tài liệu trung tâm + Event Bus
├── features/                      # Các khối tính năng độc lập
│   ├── cuelist.js                 # Bảng danh sách cue + quản lý cue đang chọn
│   ├── dashboard.js               # Màn hình Dự án: Danh sách project, tìm kiếm, lọc, xóa, metrics
│   ├── editing.js                 # Thao tác thêm/cắt/gộp/xóa/co giãn timing cue
│   ├── history.js                 # Quản lý Undo / Redo đa cấp theo snapshot
│   ├── inspector.js               # Bảng chỉnh sửa timecode, văn bản gốc, bản dịch, thước đo CPS
│   ├── jobs.js                    # Vòng đời job, lắng nghe SSE, cơ chế tự động lưu
│   ├── keymap.js                  # Trình quản lý toàn bộ phím tắt
│   ├── shell.js                   # Splitter kéo đổi layout, giao diện sáng/tối, kéo thả file
│   ├── timeline-view.js           # Kết nối Timeline Canvas Engine vào State
│   ├── transport.js               # Video Player, playhead, subtitle overlay, giữ phát khi kéo
│   └── pipeline/                  # Sidebar quy trình xử lý AI
│       ├── index.js               # Điều phối tab và container pipeline
│       ├── transcribe.js          # Nhận dạng giọng nói (Whisper/Deepgram) & Speaker Diarization
│       ├── translate.js           # Dịch thuật AI, chọn phong cách & ghim glossary
│       ├── dubbing.js             # Lồng tiếng TTS, audio ducking & trình phát nghe thử
│       ├── presets.js             # Quản lý Custom Translation Styles (CRUD)
│       └── export.js              # Xuất file SRT/VTT và muxing video MP4
├── i18n/                          # Từ điển bản địa hóa (vi.js, en.js)
├── lib/
│   └── timeline-engine.js         # Canvas Timeline Engine: thước đo, dạng sóng, clip kéo thả, playhead, zoom
└── styles/
    ├── input.css                  # Tailwind CSS v4 source entry
    └── custom.css                 # Custom scrollbar, animations và CSS design tokens

backend/
├── app.py                         # Khởi tạo FastAPI: SelectiveGZipMiddleware, CORS, exception mapping, static files
├── core/                          # Hạ tầng dùng chung (apikeys, cancellation, config, httpclient, messages)
├── domain/                        # Thuật toán nghiệp vụ thuần Python
│   ├── subtitles/                 # parser.py (SRT/VTT/CJK), layout.py, styles.py (StyleStore JSON)
│   ├── dubbing/                   # aligner.py (fit_segment 3 tầng, fingerprint), audio_dsp.py (PCM thuần)
│   └── translation/               # style.py (Style presets, glossary parser, StyleBrief)
├── infrastructure/                # Tích hợp công cụ & Adapter bên ngoài
│   ├── media/ffmpeg.py            # Wrapper an toàn cho FFmpeg / FFprobe subprocesses
│   └── providers/                 # Transcription, Translation và TTS Provider Protocols & Registry
├── ai/                            # Tầng tương tác AI Pipelines
│   ├── transcription.py           # Faster-Whisper local (CUDA/CPU) + Deepgram cloud
│   ├── translation.py             # Batching, Context injection, JSON repair, Shortening for dubbing
│   ├── diarization.py             # Phân tích lượt nói hội thoại qua LLM
│   ├── llm.py                     # Client OpenAI-compatible chat completions + Transformers local
│   ├── tts.py                     # EdgeTTS synthesis orchestrator
│   └── shared.py                  # AI custom errors & progress types
├── api/                           # Tầng HTTP Routers & Endpoints
│   ├── jobs.py                    # Router facade chính /api/jobs
│   ├── job_lifecycle.py           # Upload, Create, List, Delete, Cues Edit, Download
│   ├── job_operations.py          # Translate, Dub, Analyze Speakers
│   ├── job_events.py              # Server-Sent Events stream endpoint (/api/jobs/{id}/events)
│   ├── job_schemas.py             # Pydantic schemas (CueModel, CuesPayload, DubPayload, TranslatePayload)
│   ├── job_shared.py              # Helpers: claim lock context, save upload, engine resolvers
│   ├── media.py                   # Stream video (HTTP 206 Partial Content), Waveform, Thumbnail, Muxing
│   ├── styles.py                  # CRUD Custom Translation Styles (/api/styles)
│   └── system.py                  # /api/health, /api/capabilities
├── jobs/                          # Tầng Quản Lý Trạng Thái & Thực Thi Nền
│   ├── model.py                   # Cấu trúc Job, Status, Phase, make_progress, clean_cues, public_job
│   ├── store.py                   # JobStore: Thread-safe repository, RLock per-job, atomic JSON persistence
│   ├── runner.py                  # JobRunner: Dedicated ThreadPoolExecutor pool, JobContext, cancellation
│   └── tasks.py                   # 4 Background workflows: transcription, speaker analysis, translation, dubbing
├── runtime/
│   ├── <job_id>/                  # Thư mục lưu trữ độc lập từng project: job.json, video, audio, waveform, dub cache
│   └── styles.json                # Lưu trữ danh sách Custom Translation Styles
```

---

## Phím Tắt Tiêu Biểu

| Phím | Thao Tác |
| :--- | :--- |
| `Space` | Phát / Tạm dừng video |
| `←` `→` | Lùi / Tiến 1 frame (giữ `Shift` để nhảy 1 giây) |
| `Ctrl ←` `Ctrl →` | Nhảy về Cue trước / Cue sau |
| `Ctrl Z` · `Ctrl Y` | Hoàn tác (Undo) · Làm lại (Redo) |
| `A` · `S` · `G` · `Delete` | Thêm · Cắt · Gộp · Xóa cue |
| `I` · `O` | Đặt mốc Vào (In) / Ra (Out) tại vị trí playhead |
| `N` · `F` · `+` `−` | Bật/tắt Hít dính (Snap) · Vừa khung (Fit) · Phóng to / Thu nhỏ Timeline |
| `Ctrl ↵` | Kích hoạt tác vụ AI chính (Phiên âm) |
| `F1` | Mở bảng danh mục toàn bộ phím tắt |

---

## Triển Khai (Deployment)

Dự án hỗ trợ quy trình CI/CD tự động khi push lên nhánh `main`: kiểm tra test, build Docker image, đẩy lên GitHub Packages (GHCR) và tự động deploy lên máy chủ ảo (VM). Xem hướng dẫn chi tiết tại [DEPLOY.md](DEPLOY.md).

---

## Giấy Phép (License)

Dự án được phân phối dưới giấy phép mã nguồn mở [MIT License](LICENSE).
