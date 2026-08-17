FROM python:3.12-slim

# Cài đặt ffmpeg và các công cụ hệ thống cần thiết
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY pyproject.toml .

# Tạo thư mục runtime để lưu trữ jobs và media
RUN mkdir -p runtime

EXPOSE 8000

# Chạy FastAPI qua Uvicorn
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
