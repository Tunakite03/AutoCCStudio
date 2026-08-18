# Đóng góp cho AutoCC

Cảm ơn bạn đã quan tâm đóng góp cho AutoCC. Tài liệu này mô tả cách setup môi
trường dev, quy ước code, và quy trình gửi thay đổi.

## Setup môi trường

Yêu cầu Python 3.12.

```powershell
git clone https://github.com/Tunakite03/AutoCCStudio.git
cd AutoCCStudio
Copy-Item .env.example .env
.\run.ps1
```

`run.ps1` tự tạo virtualenv tại `.venv`, cài `requirements.txt` và chạy
`uvicorn`. App sẽ ở [http://127.0.0.1:8000](http://127.0.0.1:8000).

Nếu chỉnh CSS (`frontend/input.css`), biên dịch lại bằng Tailwind CLI đã
bundle sẵn trong repo:

```powershell
.\build-css.ps1          # build một lần, minify
.\build-css.ps1 -Watch   # watch mode khi đang sửa
```

`frontend/styles.css` là file build ra — đừng sửa tay, và đừng commit nếu
không đi kèm thay đổi tương ứng ở `input.css`.

## Chạy test & lint

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

CI (`.github/workflows/ci.yml`) chạy `python -m compileall backend`,
`pytest` và `ruff` trên mọi PR — đảm bảo cả hai lệnh trên chạy sạch trước khi
mở PR. Test nằm trong `tests/`, đặt tên theo module đang test
(`test_subtitles.py` test `backend/subtitles.py`, v.v.).

Nếu sửa JS/HTML/CSS trong `frontend/`, chạy Prettier (`.prettierrc`) trước
khi commit để giữ format nhất quán:

```bash
npx prettier --write frontend/
```

## Quy ước code

- Python: theo cấu hình `ruff` trong `pyproject.toml` (pycodestyle, pyflakes,
  isort, pyupgrade, bugbear). Line length không giới hạn cứng (`E501` tắt) vì
  formatter lo phần đó, nhưng ưu tiên ngắn gọn.
- JS/HTML/CSS: 2 spaces, double quotes, semicolon — theo `.prettierrc` và
  `.editorconfig`. Không dùng framework/bundler; `frontend/` là JS thuần chạy
  thẳng trên trình duyệt.
- Comment chỉ giải thích *tại sao*, không lặp lại *cái gì* code đã nói rõ —
  xem ví dụ trong `build-css.ps1` hoặc `.github/workflows/ci.yml`.
- Không thêm abstraction/tuỳ chọn cấu hình cho nhu cầu chưa xuất hiện. Sửa
  bug thì chỉ sửa bug đó, không tiện tay refactor xung quanh.
- Test mới cho behavior mới, đặc biệt với logic dễ vỡ âm thầm (timing cue,
  batch dịch, SSE reconnect) — xem `tests/test_subtitles.py` và
  `tests/test_frontend_sse.py` làm ví dụ về style.

## Gửi thay đổi

1. Tạo branch từ `main`.
2. Đảm bảo `pytest` và `ruff check .` chạy sạch.
3. Mở Pull Request vào `main`, mô tả ngắn gọn *tại sao* cần thay đổi này (bug
   gì, tính năng gì) hơn là liệt kê lại diff.
4. Merge vào `main` sẽ tự động build image và deploy lên VM production (xem
   `DEPLOY.md`) — vì vậy PR vào `main` cần đã test kỹ, tránh commit
   work-in-progress trực tiếp lên branch này.

## Báo lỗi / đề xuất tính năng

Mở issue trên GitHub, mô tả rõ bước tái hiện (với bug) hoặc use case cụ thể
(với đề xuất tính năng). Với bug liên quan tới nhận dạng giọng nói hoặc dịch,
kèm theo provider đang dùng (`faster-whisper`/Deepgram,
Ollama/LLM-compatible endpoint) giúp debug nhanh hơn nhiều.
