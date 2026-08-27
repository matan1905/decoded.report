#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

. .venv/bin/activate

echo "Installing requirements..."
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

if [ -f ".env" ]; then
  echo "Loaded .env"
else
  echo "No .env found: run 'cp .env.example .env' to add price/email keys (optional)."
fi

echo "Starting uvicorn on 127.0.0.1:8000"
exec uvicorn app.main:app --host 127.0.0.1 --port 8000