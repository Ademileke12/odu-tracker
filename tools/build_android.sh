#!/usr/bin/env bash
# Build the ODU Tracker Android APK (release-signed, sideload-ready).
#
#   ./tools/build_android.sh              # signed release APK
#   ./tools/build_android.sh --debug      # unsigned debug APK (device testing)
#
# Output: android_app/bin/*.apk
#
# Requirements on the build machine:
#   * Java 17+ (keytool comes with it)
#   * roughly 6 GB free disk for the auto-downloaded SDK/NDK + toolchains
#   * (Linux) sudo apt-get install git zip unzip autoconf libtool
#       pkg-config zlib1g-dev libncurses-dev cmake libffi-dev libssl-dev
#   * buildozer + cython installed into the project venv by this script
#
# Signing uses the committed self-signed release keystore
# (android_app/odu-tracker-release.keystore) via the P4A_RELEASE_* env vars
# that buildozer/python-for-android expect. Replace it with your own
# private keystore before distributing wide.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/android_app"

MODE="release"
for arg in "$@"; do
  case "$arg" in
    --debug) MODE="debug" ;;
  esac
done

# ---------------------------------------------------------------- venv/tools
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PY="$ROOT_DIR/.venv/bin/python"
  BUILD0ZER="$ROOT_DIR/.venv/bin/buildozer"
  export PATH="$ROOT_DIR/.venv/bin:$PATH"
else
  PY="python3"
  BUILD0ZER="buildozer"
fi

echo "== checking buildozer/cython"
"$PY" -c "import buildozer" 2>/dev/null || "$PY" -m pip install buildozer
"$PY" -c "import Cython" 2>/dev/null || "$PY" -m pip install cython

if ! command -v keytool >/dev/null 2>&1; then
  echo "ERROR: keytool not found — install a JDK (17+) first." >&2
  exit 1
fi

# ------------------------------------------------------------------ keystore
KS="$APP_DIR/odu-tracker-release.keystore"
# generate only if missing (so you can drop in your own private keystore)
if [ ! -f "$KS" ]; then
  echo "== generating release keystore -> $KS"
  keytool -genkeypair -v -keystore "$KS" -alias odu-tracker \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass odu-tracker-2026 -keypass odu-tracker-2026 \
    -dname "CN=ODU Tracker, OU=MTN ODU Tools, O=anakincoco, L=Lagos, C=NG"
fi

export P4A_RELEASE_KEYSTORE="$KS"
export P4A_RELEASE_KEYSTORE_PASSWD="odu-tracker-2026"
export P4A_RELEASE_KEYALIAS_PASSWD="odu-tracker-2026"
export P4A_RELEASE_KEYALIAS="odu-tracker"

# --------------------------------------------------------------------- build
cd "$APP_DIR"
if [ "$MODE" = "release" ]; then
  "$BUILD0ZER" -v android release
else
  "$BUILD0ZER" -v android debug
fi

echo
echo "== APK ready:"
ls -lh "$APP_DIR/bin/" 2>/dev/null || true
echo
echo "sideload:  adb install -r bin/odu_tracker-*-release.apk   (or copy the"
echo "           APK to the phone and tap to install, Allow unknown apps)"
