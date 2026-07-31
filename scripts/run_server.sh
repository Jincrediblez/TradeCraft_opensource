#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

export PORT="${PORT:-8888}"

# Port guard: exit silently if port is already in use
if lsof -Pi ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[tradecraft] port $PORT already in use, skipping start"
  exit 0
fi

echo "[tradecraft] starting"
echo "[tradecraft] root=$ROOT_DIR"
echo "[tradecraft] port=$PORT"
echo "[tradecraft] python=$(command -v python3)"
for required_path in "app/main.py" "static/index.html" "data" "cache" "logs"; do
  if [[ -e "$ROOT_DIR/$required_path" ]]; then
    echo "[tradecraft] ok $required_path"
  else
    echo "[tradecraft] missing $required_path"
  fi
done

exec python3 app/main.py
