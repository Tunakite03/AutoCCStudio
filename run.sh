#!/usr/bin/env bash
set -e

# Determine project directory
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PORT="${1:-8000}"
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

# 1. Check/copy .env file
if [ ! -f "$PROJECT_ROOT/.env" ]; then
    if [ -f "$PROJECT_ROOT/.env.example" ]; then
        echo "⚙️  .env file not found, creating from .env.example..."
        cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    fi
fi

# 2. Check/create virtual environment
if [ ! -f "$VENV_PYTHON" ]; then
    echo "📦 Creating virtual environment (.venv)..."
    if command -v python3.12 >/dev/null 2>&1; then
        python3.12 -m venv "$VENV_DIR"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m venv "$VENV_DIR"
    else
        echo "❌ Error: python3 is not installed or not in PATH."
        exit 1
    fi
fi

# 3. Ensure pip is installed
if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "📦 Installing pip inside .venv..."
    "$VENV_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

# 4. Install dependencies
echo "📥 Checking and installing requirements..."
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/requirements.txt"

# 5. Start application
echo "🚀 Starting AutoCC on http://127.0.0.1:$PORT..."
exec "$VENV_PYTHON" -m uvicorn backend.app:app --host 127.0.0.1 --port "$PORT"
