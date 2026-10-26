#!/usr/bin/env bash
# Build the dedicated ODU Tracker desktop app for Linux (Electron + .deb).
#
#   ./tools/build_electron.sh              # python engine -> stage -> .deb
#   ./tools/build_electron.sh --skip-server    # reuse the staged engine
#
# Output:  dist/electron/odu-tracker_<version>_amd64.deb
#
# The .deb bundles BOTH the Electron shell and the frozen Python engine
# (waitress + poller + SQLite), so `sudo dpkg -i` gives users a complete,
# self-contained app — no Python/Node required on the target machine.
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
#else
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

echo "== electron-builder (Linux .deb)"
npx electron-builder --linux deb --publish never

echo
echo "== done. artifacts:"
ls -lh "$ROOT_DIR/dist/electron/" 2>/dev/null || true
echo
echo "install with:  sudo dpkg -i dist/electron/*.deb"
