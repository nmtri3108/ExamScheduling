#!/usr/bin/env bash
# Thiết lập ExamScheduling trên macOS / Linux
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 setup.py --run
fi
if command -v python >/dev/null 2>&1; then
  exec python setup.py --run
fi

echo "[LỖI] Không tìm thấy python3. Cài Python 3.10+ (macOS: brew install python@3.12)"
exit 1
