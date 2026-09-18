#!/usr/bin/env bash
# Start Blur Bench and open it in the browser.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-7788}"
command -v ffmpeg  >/dev/null || { echo "ffmpeg is not installed. Try: sudo apt install ffmpeg"; exit 1; }
python3 -c "import cv2, flask, numpy" 2>/dev/null || {
  echo "Missing Python packages. Try: pip3 install opencv-python flask numpy"; exit 1; }

python3 app.py --port "$PORT" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT INT TERM
sleep 1.5
(xdg-open "http://127.0.0.1:$PORT/" >/dev/null 2>&1 || true) &
wait $SERVER
