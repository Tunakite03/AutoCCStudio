# AutoCC

**Tiếng Việt** | [English](README_EN.md)

AutoCC là MVP local-first để tạo và biên tập phụ đề cho video:

- Import file `.srt` hoặc `.vtt`.
- Nhận dạng bằng `faster-whisper` local hoặc Deepgram Nova-3 cloud.
- Deepgram hỗ trợ diarization âm thanh; AI ngữ cảnh có thể tách thêm lượt thoại bên trong cue bằng xuống dòng, không hiện nhãn `[S1]`/`[S2]`.
- Dịch từng cue qua endpoint LLM tương thích OpenAI (`/chat/completions`), nên có thể dùng Ollama, LM Studio hoặc một API cloud.
- Theo dõi transcription/translation realtime bằng Server-Sent Events (SSE), có heartbeat và tự reconnect.
- Giao diện dạng app dựng phim: xem trước video kèm phụ đề chồng lên hình, timeline kéo thả có dạng sóng audio.
- Màn hình **Dự án** quản lý mọi project trong workspace: ảnh đại diện, tiến độ dịch, dung lượng đĩa, tìm kiếm, lọc và xóa.
- Mở lại project cũ là chạy lại nhận dạng được ngay trên video server đã giữ, không phải tải file lên lần nữa.
- Kéo thân clip để dời cue, kéo mép để co giãn, cắt/gộp cue tại playhead, hít dính vào mép cue và playhead.
- Đang phát mà kéo timeline thì video tạm dừng theo cử chỉ và tự chạy tiếp khi thả chuột.
- Hoàn tác / làm lại nhiều bước cho mọi thao tác sửa cue; một tràng gõ phím gộp thành một bước.
- Cảnh báo tốc độ đọc (CPS) ngay trên clip và trong danh sách, theo ngưỡng 17/21 ký tự mỗi giây.
- Bố cục kéo được: rộng cột trái/phải và cao timeline được nhớ lại giữa các phiên, có giao diện sáng/tối.
- Xuất SRT/VTT hoặc ghép soft subtitle vào MP4 bằng ffmpeg.

## Chạy trên Windows

Yêu cầu Python 3.12. App dùng `ffmpeg` trong `PATH` nếu có; nếu không, nó sẽ thử dùng binary bundled từ `imageio-ffmpeg` cho bước ghép subtitle.

```powershell
cd E:\Project2025\AutoCC
Copy-Item .env.example .env
.\run.ps1
```

Mở [http://127.0.0.1:8000](http://127.0.0.1:8000).

Lần đầu chạy `faster-whisper`, model sẽ được tải về cache của máy. Có thể chọn `tiny`, `base`, `small`, `medium` hoặc `large-v3`; `small` là lựa chọn cân bằng cho CPU.

## Nhận dạng nhiều người bằng Deepgram

Tạo API key trong Deepgram, sau đó cấu hình `.env`:

```dotenv
TRANSCRIPTION_PROVIDER=deepgram
DEEPGRAM_API_KEY=your-deepgram-api-key
DEEPGRAM_MODEL=nova-3
DEEPGRAM_DIARIZE_MODEL=latest
```

Khởi động lại AutoCC. Request gửi video tới API `/v1/listen` với `utterances=true`,
`smart_format=true` và diarizer mới nhất. Nếu để ngôn ngữ là “Tự nhận diện”, app dùng
language detection; chọn ngôn ngữ cụ thể sẽ khóa model theo mã ngôn ngữ đó.

Khi chọn Deepgram trên giao diện, dropdown model đổi sang `nova-3`, `nova-2`,
`nova-2-meeting` hoặc `nova-2-video`. `nova-3` phù hợp nhất cho video thông thường
và nhiều người; hai model Meeting/Video là lựa chọn chuyên biệt cho audio English.
Model được chọn áp dụng riêng cho từng job và được lưu trong metadata của job.

Tùy chọn **AI phân tích lượt thoại** xử lý theo hai tầng. Trước tiên backend dùng
`speaker` và `speaker_confidence` ở từng từ của Deepgram để chèn xuống dòng theo
bằng chứng giọng nói. Sau đó LLM đọc ngữ cảnh hỏi/đáp để bổ sung các ranh giới còn
mơ hồ, kể cả khi Deepgram gộp nhiều người vào một utterance.

Model chỉ được phép chèn thêm ký tự xuống dòng; backend đối chiếu lại toàn bộ chữ và
dấu câu, đồng thời không cho LLM xóa ranh giới đã xác định từ audio. Kết quả LLM được
gắn `cue_id`, chia batch nhỏ và retry riêng cue bị thiếu/sai. Cue vẫn không đạt sau
retry được giữ nguyên thay vì làm hỏng cả batch, và giao diện báo trạng thái partial.
Nút **Phân tích lại lượt thoại** chạy lại riêng bước LLM trên cue hiện tại, không upload
video và không gọi lại Deepgram, nên có thể thử lại sau khi đổi model/prompt mà không
tốn thêm một lượt transcription.

## Dịch bằng LLM

Mặc định app trỏ tới Ollama:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

Nếu dùng endpoint khác, chỉnh `.env`:

```dotenv
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_API_KEY=your-key
LLM_MODEL=your-model
# Không đặt thì dùng lại LLM_MODEL
SPEAKER_ANALYSIS_MODEL=
```

Có thể dịch local bằng Transformers cho model cụ thể, ví dụ English → Vietnamese:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-translation-local.txt
```

```dotenv
TRANSLATION_PROVIDER=transformers
TRANSLATION_MODEL=Helsinki-NLP/opus-mt-en-vi
TRANSFORMERS_TARGET_LANGUAGE=Tiếng Việt
TRANSFORMERS_DEVICE=auto
```

Model local chỉ phù hợp với cặp ngôn ngữ mà model hỗ trợ; với nhiều ngôn ngữ hoặc giữ văn phong dài, dùng LLM-compatible provider sẽ linh hoạt hơn.

Mỗi batch dịch gửi đi một object JSON có khóa là số thứ tự dòng và nhận về đúng bộ khóa
đó, nên model gộp hai dòng ngắn thành một câu cũng không làm lệch cả batch: dòng bị thiếu
được dịch lại riêng, chỉ khi lần lẻ đó cũng hỏng mới báo lỗi. Các xuống dòng giữa người
nói được giữ nguyên khi dịch; timing luôn được giữ ở backend.

Phim là một mạch hội thoại, nên batch không được dịch tách rời: mỗi request kèm 4 dòng
trước (cả bản dịch vừa tạo ra) và 2 dòng sau làm ngữ cảnh chỉ-để-đọc, kèm mã người nói
lấy từ diarization để cùng một nhân vật giữ nguyên giọng điệu và cách xưng hô. Model đồng
thời trả về một bảng thuật ngữ (tên riêng, xưng hô giữa từng cặp nhân vật, từ lặp lại)
được mang sang các batch sau, giới hạn 40 mục để prompt không phình theo độ dài phim. Dòng
phải dịch lại lẻ cũng nhận đúng ngữ cảnh đó — dịch một câu trơ trọi chính là thứ cần
tránh. Không tốn thêm lượt gọi nào, chỉ tốn thêm token đầu vào mỗi request.

### Phong cách dịch

Bản dịch đúng nghĩa vẫn có thể sai vibe: `大哥` nghĩa là "anh cả", nhưng khán giả phim
Trung chờ đợi "đại ca". Ô **Phong cách** trong khối Dịch chọn bộ quy tắc đó, mặc định
`Tự động theo ngôn ngữ nguồn` — tiếng Trung ra preset Hán Việt, tiếng Hàn giữ
oppa/unnie/sunbae, tiếng Nhật giữ senpai và hậu tố -san/-chan, còn lại là trung tính.
Ngoài ra có `GenZ, khẩu ngữ` và `Trang trọng` để chọn tay khi cần.

Mỗi preset gồm hai phần: các quy tắc được chèn thẳng vào prompt, và một bảng thuật ngữ
được **ghim** vào glossary ngay từ batch đầu — model được phép bổ sung thuật ngữ mới
trong lúc dịch nhưng không được sửa hay loại bỏ mục đã ghim.

Ô **Quy tắc riêng** là phần tùy biến của bạn, mỗi dòng một ý:

```text
大哥 → đại ca
陛下 = bệ hạ
Giọng trẻ, tránh từ Hán Việt nặng ở cảnh hiện đại
```

Dòng có `→`, `->`, `=>` hoặc `=` được đọc thành thuật ngữ ghim và **đè lên preset**; dòng
còn lại thành một quy tắc trong prompt. Muốn trộn hai phong cách (ví dụ phim Trung nhưng
lời thoại kiểu GenZ) thì chọn preset gần nhất rồi viết phần còn lại vào đây. Giới hạn 2000
ký tự và 40 thuật ngữ ghim, cắt bớt preset trước khi cắt của bạn. Lựa chọn được lưu theo
project nên mở lại thấy đúng thứ đã dùng.

Preset nằm trong [backend/translation_style.py](backend/translation_style.py) — thêm ngôn
ngữ hoặc thể loại mới chỉ là thêm một mục vào `STYLES` (và `LANGUAGE_STYLES` nếu muốn nó
được chọn tự động).

Provider hosted tính giới hạn theo requests/giây và tokens/phút. Gặp HTTP 429, app chờ
đúng khoảng thời gian ghi trong header `Retry-After` (không có thì backoff nhân đôi, tối
đa một phút) với ngân sách riêng `HTTP_RATE_LIMIT_RETRIES`. Nếu vẫn bị chặn thường xuyên,
giãn nhịp gọi bằng `LLM_MIN_INTERVAL_SECONDS` — với gói free của Mistral (1 request/giây)
đặt `1.1`. Batch nào đã dịch xong vẫn được ghi xuống đĩa trước khi lỗi xảy ra.

## Kiểm tra nhanh

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 -m pytest
py -3.12 -m compileall backend
```

## Phát triển giao diện (Tailwind CSS)

Giao diện sử dụng Tailwind CSS v4 qua binary độc lập `tailwindcss.exe` (không cần Node.js/npm):

- **Khi sửa giao diện (Watch mode):**
  ```powershell
  .\build-css.ps1 -Watch
  ```
- **Khi build nén (Production):**
  ```powershell
  .\build-css.ps1
  ```
- File nguồn: `frontend/input.css` (gồm `@import "tailwindcss";` và `@import "./custom.css";`).
- File đích: `frontend/styles.css` (được load trực tiếp bởi trình duyệt).

## Kiến trúc MVP


```text
frontend/index.html            shell 3 pane: inspector · stage + timeline · danh sách cue
frontend/app.js                entry point: mount lần lượt các feature module
frontend/core/store.js         state tài liệu + event bus (job:loaded, cues:changed, cue:patched…)
frontend/core/router.js        chuyển màn hình theo data-screen + hash URL
frontend/core/api.js           mọi lời gọi backend, nơi duy nhất biết hình dạng URL
frontend/core/dom.js           $, element(), pointer capture
frontend/core/feedback.js      toast, status bar, chỉ báo lưu
frontend/core/format.js        timecode, tốc độ đọc (CPS), tiện ích format
frontend/features/transport.js player, playhead, overlay phụ đề, giữ phát khi kéo
frontend/features/timeline-view.js nối engine timeline vào state
frontend/features/cuelist.js   danh sách cue + quyền sở hữu "cue đang chọn"
frontend/features/inspector.js ô timecode, văn bản, đồng hồ CPS
frontend/features/editing.js   thêm/cắt/gộp/xóa/chỉnh giờ cue
frontend/features/history.js   undo/redo theo snapshot
frontend/features/jobs.js      vòng đời job, SSE, tự lưu
frontend/features/dashboard.js màn hình Dự án: thống kê, lưới project, tìm/lọc/xóa
frontend/features/pipeline.js  sidebar: nguồn, capability, chạy AI, xuất file
frontend/features/shell.js     splitter, theme, thả file toàn cửa sổ
frontend/features/keymap.js    toàn bộ phím tắt
frontend/lib/timeline-engine.js thước, dạng sóng, clip kéo thả, playhead, zoom
backend/app.py            lắp ráp: middleware, ánh xạ lỗi domain → HTTP, mount router + frontend
backend/config.py         settings đọc từ .env + cấu hình logger `autocc.*`
backend/httpclient.py     một đường HTTP duy nhất ra ngoài, có retry/backoff cho lỗi tạm thời
backend/api/system.py     /api/health, /api/capabilities
backend/api/jobs.py       vòng đời job: tạo, sửa cue, xuất file, khởi động việc nền, SSE
backend/api/media.py      stream video (HTTP Range), thumbnail, dạng sóng, ghép phụ đề
backend/jobs/model.py     hình dạng job, danh sách trạng thái, projection ra client
backend/jobs/store.py     sở hữu state job: khóa, ghi atomic, thông báo thay đổi
backend/jobs/runner.py    pool worker giới hạn + JobContext (progress, checkpoint)
backend/jobs/tasks.py     ba luồng việc nền: nhận dạng, phân tích lượt thoại, dịch
backend/media.py          mọi lệnh gọi ffmpeg + probe media
backend/subtitles.py      parser/formatter SRT + VTT
backend/ai.py             adapter faster-whisper, Deepgram + LLM tương thích OpenAI
runtime/<job-id>/         video, subtitle, waveform.json và metadata tạm thời
```

Ba quy tắc giữ cho backend dễ sửa về sau:

1. **Không ai mutate job ngoài `store.edit(job_id)`.** Trước đây worker và request
   handler cùng giữ một dict: worker ghi đè được lên chỉnh sửa của người dùng, xóa
   project xong worker lại tạo lại thư mục, hai writer có thể ghi lệch thứ tự revision.
2. **Vùng khóa phải ngắn.** Worker đọc dữ liệu ra, chạy phần việc dài *không giữ khóa*,
   rồi mở job lại để ghi kết quả — nên `GET /api/jobs/{id}` không bao giờ phải chờ một
   lượt transcription.
3. **Tầng dưới không biết HTTP.** `store` và `jobs/` ném `JobNotFound`/`JobConflict`;
   `app.py` là nơi duy nhất biến chúng thành 404/409.

Việc nền chạy trong pool riêng có giới hạn (`MAX_CONCURRENT_JOBS`), không dùng
`BackgroundTasks` nữa: job thứ ba xếp hàng thay vì tranh CPU với hai job đang chạy, và
mọi lỗi đều rơi vào job dưới dạng `status=error` kèm traceback trong log.

Frontend là ES module thuần, không build step. Quy tắc giữ cho nó dễ mở rộng: **module
không gọi hàm render của module khác** — nó đổi state rồi `emit`, ai quan tâm thì tự vẽ
lại. Mỗi module chỉ sở hữu vùng DOM và trạng thái bật/tắt nút của riêng mình. Thêm một
màn hình mới (ví dụ dashboard quản lý project) là viết thêm một feature module rồi mount
trong `app.js`, không phải sửa module cũ.

- `POST /api/jobs/{id}/transcribe` — nhận dạng lại bằng chính video đã lưu trong `runtime/<job-id>/`, nhận `provider`/`model`/`source_language`/`analyze_speakers` như route upload. Giao diện hỏi xác nhận trước vì cue cũ sẽ bị thay.

Endpoint cho màn hình Dự án:

- `GET /api/jobs` — tóm tắt mọi project trong `runtime/` (số cue, thời lượng, tiến độ dịch, dung lượng, thời điểm sửa), không kèm nội dung cue.
- `DELETE /api/jobs/{id}` — xóa hẳn thư mục project khỏi đĩa.
- `GET /api/jobs/{id}/thumbnail` — ffmpeg trích một khung ở mốc 10% và cache tại `runtime/<job-id>/thumb.jpg`.

Endpoint phục vụ timeline:

- `GET /api/jobs/{id}/video` — stream video có hỗ trợ HTTP Range để tua mượt.
- `GET /api/jobs/{id}/waveform` — ffmpeg giải mã audio thành 20 đỉnh biên độ mỗi giây, cache tại `runtime/<job-id>/waveform.json`. Không có ffmpeg thì timeline vẫn chạy, chỉ thiếu dạng sóng.

### Tiến độ job qua SSE

Mỗi snapshot job có thêm trường `progress`, `null` khi job đứng yên:

```json
{ "phase": "translating", "current": 40, "total": 60, "ratio": 0.6667, "message": "Đã dịch 40/60 dòng" }
```

`phase` là một trong `queued`, `transcribing`, `analyzing`, `translating`. `total` là
`null` khi một bước không tự biết kích thước của mình — Deepgram là một request khép
kín nên chỉ báo `message`, còn faster-whisper báo theo mốc thời gian trên timeline.

Tiến độ được publish qua SSE nhưng **không** ghi xuống đĩa mỗi nhịp; riêng bản dịch thì
mỗi batch xong đều được lưu lại, nên một batch hỏng ở phút thứ 40 không làm mất những gì
đã dịch trước đó — job chuyển sang `error` nhưng vẫn giữ phần đã xong.

## Phím tắt

| Phím | Việc |
| --- | --- |
| `Space` | Phát / dừng |
| `←` `→` | Lùi / tiến 1 frame (giữ `Shift` để nhảy 1 giây) |
| `Ctrl ←` `Ctrl →` | Cue trước / cue sau |
| `Ctrl Z` · `Ctrl Y` | Hoàn tác · làm lại (trong ô nhập chữ, undo của trình duyệt được ưu tiên) |
| `A` · `S` · `G` · `Delete` | Thêm · cắt · gộp · xóa cue |
| `I` · `O` | Lấy điểm vào / ra tại playhead |
| `N` · `F` · `+` `−` | Hít dính · vừa khung · phóng to / thu nhỏ |
| `Ctrl ↵` | Chạy nhận dạng AI |
| `F1` | Bảng phím tắt |

## Giới hạn hiện tại

- Job state được lưu local theo từng thư mục, chưa có user/account hoặc database.
- API không có lớp xác thực. App chỉ bind `127.0.0.1` và CORS giới hạn ở origin localhost; đừng expose ra ngoài mạng khi chưa thêm auth.
- “Ghép vào video” là soft subtitle track trong MP4; chưa có chế độ burn-in chữ vào hình.
- Riêng ghép video vẫn là request đồng bộ (giữ kết nối HTTP suốt lúc render) chứ chưa phải job nền, vì frontend nhận thẳng blob từ response.
- Phân tích lượt thoại gọi LLM tuần tự từng batch nên với transcript rất dài vẫn chậm; đã có progress nhưng chưa có cơ chế hủy giữa chừng.
- Dịch AI cần LLM endpoint đang chạy và có thể cần điều chỉnh prompt/model cho thuật ngữ chuyên ngành.
- Deepgram là dịch vụ cloud: cần API key, có chi phí theo thời lượng và video được gửi ra ngoài máy.

## Deploy

Push lên `main` là tự động deploy lên VM: CI chạy test, build Docker image, đẩy lên GHCR rồi SSH vào VM pull và restart. Các bước cấu hình một lần (SSH key, secrets, NSG) nằm ở [DEPLOY.md](DEPLOY.md).

## License

Dự án được phân phối dưới giấy phép [MIT License](LICENSE).

