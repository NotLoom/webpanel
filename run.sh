#!/usr/bin/env bash
# Run Dizzy's Web Panel inside the project's virtualenv.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No .venv found. Set it up with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

exec .venv/bin/python dizzys_web_panel.py "$@"
