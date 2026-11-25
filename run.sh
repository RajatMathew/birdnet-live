#!/usr/bin/env bash
# Start backend (8000) and frontend (5173) together. Ctrl-C stops both.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Python venv lives at the project root.
if [ ! -d "$ROOT/.venv" ]; then
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -r "$ROOT/backend/requirements.txt"
fi

cd "$ROOT/frontend"
[ -d node_modules ] || npm install

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(cd "$ROOT/backend" && "$ROOT/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000) &
(cd "$ROOT/frontend" && npm run dev) &

echo
echo "  → http://localhost:5173"
echo "  → API http://127.0.0.1:8000/api/health"
echo
wait
