#!/usr/bin/env bash
# Build a standalone Dizzy's Web Panel executable with PyInstaller.
# Result: dist/dizzys-web-panel  (dist\dizzys-web-panel.exe on Windows).
set -euo pipefail
cd "$(dirname "$0")"

BUILD_VENV="${BUILD_VENV:-.venv-build}"
python_bin="$BUILD_VENV/bin/python"

if [ ! -x "$python_bin" ]; then
  echo "==> Creating build venv ($BUILD_VENV)"
  python3 -m venv "$BUILD_VENV"
fi

echo "==> Installing build dependencies"
"$python_bin" -m pip install --quiet --upgrade pip
"$python_bin" -m pip install --quiet -r requirements-build.txt

echo "==> Building executable (this can take a minute)"
"$python_bin" -m PyInstaller --noconfirm --clean dizzys_web_panel.spec

echo
echo "==> Done:"
ls -lh dist/
