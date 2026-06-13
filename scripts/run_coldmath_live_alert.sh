#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR/.."

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

exec "${PYTHON:-/opt/homebrew/bin/python3}" coldmath_live_alert.py --no-email
