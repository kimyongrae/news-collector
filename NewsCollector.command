#!/bin/zsh
set -e

cd "$(dirname "$0")"

HOST="${NEWS_COLLECTOR_HOST:-127.0.0.1}"
PORT="${NEWS_COLLECTOR_PORT:-8768}"
URL="http://${HOST}:${PORT}"

echo "Starting NewsCollector UI..."
echo "URL: ${URL}"
echo

if python3 - <<PY
import socket
host = "${HOST}"
port = int("${PORT}")
s = socket.socket()
s.settimeout(0.25)
try:
    s.connect((host, port))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
then
  echo "NewsCollector UI is already running at ${URL}"
  open "${URL}" >/dev/null 2>&1
  exit 0
fi

(sleep 1.2 && open "${URL}") >/dev/null 2>&1 &

NEWS_COLLECTOR_HOST="${HOST}" NEWS_COLLECTOR_PORT="${PORT}" python3 run.py
