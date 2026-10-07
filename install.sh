#!/usr/bin/env bash
# One-shot setup (Linux / macOS): venv + playwright + chromium (+ system deps).
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: python3 not found. Install Python 3.10+ first (https://www.python.org/downloads/)." >&2
  exit 1
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "error: Python 3.10+ required (found $("$PYTHON" --version 2>&1))." >&2
  exit 1
}

echo "==> Creating virtual environment (.venv)"
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing playwright"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Downloading Chromium (Playwright build)"
playwright install chromium

if [ "$(uname -s)" = "Linux" ]; then
  echo "==> Installing Chromium system libraries"
  playwright install-deps chromium || \
    echo "note: 'playwright install-deps' failed - run it manually with sudo if Chromium won't start."
fi

echo
echo "Setup complete. Next steps:"
echo "  1. Put your liked_videos.json (JSON array of video URLs) in this folder."
echo "  2. Run:  source .venv/bin/activate && python migrate_liked_videos.py"
echo "     (or directly: .venv/bin/python migrate_liked_videos.py)"
