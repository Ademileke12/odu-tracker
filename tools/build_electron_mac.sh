#!/usr/bin/env bash
# Build the ODU Tracker desktop app for macOS - Apple Silicon (.dmg).
#
#   ./tools/build_electron_mac.sh              # python engine -> stage -> .dmg
#   ./tools/build_electron_mac.sh --skip-server # reuse the staged engine
#
# Output:  dist/electron/ODU-Tracker-arm64.dmg
#
# MUST run on macOS (arm64): PyInstaller cannot cross-compile, so this is
# what CI's macos-15 runner executes. The .app is NOT code-signed (no
# Apple Developer certificate yet) - users right-click → Open on first
# launch, or run:
#   xattr -com.apple.quarantine -d "/Applications/ODU Tracker.app"
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ELECTRON_DIR="$ROOT_DIR/packaging/electron"
RESOURCE_DIR="$ELECTRON_DIR/resources/odu-tracker-server"
SKIP_SERVER=0
for arg in "$@"; do
  case "$arg" in
    --skip-server) SKIP_SERVER=1 ;;
  esac
done
cd "$ROOT_DIR"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "ERROR: this script builds a macOS .dmg and must run on macOS." >&2
  exit 1
fi

PY="python3"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi
echo "== using interpreter: $PY"

# ---------------------------------------------------------- 1. python engine
if [ "$SKIP_SERVER" -eq 0 ]; then
  if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
    echo "== installing PyInstaller into venv"
    "$PY" -m pip install pyinstaller
  fi

  echo "== building headless engine (PyInstaller)"
  "$PY" -m PyInstaller \
    --noconfirm --clean \
    --name odu-tracker-server \
    --add-data "odu_tracker/web/static:static" \
    --hidden-import waitress \
    tools/server_entry.py
fi

STAGE="$ELECTRON_DIR/resources/odu-tracker-server"
if [ -f "$ROOT_DIR/dist/odu-tracker-server/odu-tracker-server" ]; then
  echo "== staging engine -> $STAGE"
  rm -rf "$ELECTRON_DIR/resources"
  mkdir -p "$STAGE"
  cp -r "$ROOT_DIR/dist/odu-tracker-server/." "$STAGE/"
  chmod +x "$STAGE/odu-tracker-server" || true
elif [ ! -d "$STAGE" ]; then
  echo "ERROR: no engine bundle found (dist/odu-tracker-server/ or staged resources)" >&2
  exit 1
fi

# --------------------------------------------------- 2. electron + packaging
cd "$ELECTRON_DIR"
if [ ! -d node_modules ]; then
  echo "== npm install (electron + electron-builder)"
  npm install --no-audit --no-fund
fi

echo "== electron-builder (macOS .dmg, Apple Silicon)"
npx electron-builder --mac dmg --publish never

echo
echo "== done. artifacts:"
ls -lh "$ROOT_DIR/dist/electron/"*.dmg 2>/dev/null || true
echo
echo "install: open the .dmg, drag ODU Tracker to Applications"
echo "first launch: right-click the app -> Open (unsigned build)"
