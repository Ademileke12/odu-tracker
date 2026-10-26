#!/usr/bin/env bash
# Clone (or reuse) python-for-android and patch it for 16 KB ELF alignment.
#
# Android 16/17 devices with 16 KB kernels enforce page-size checks on shared
# libraries. Stock p4a (NDK r28 defaults max-page-size but not
# common-page-size) produces libraries that fail the strictest variants of
# those checks, so we build from a patched clone instead.
#
# buildozer finds it via buildozer.spec:  p4a.source_dir = ~/p4a-16k
# (which makes buildozer use the directory AS-IS: no re-clone, no pull).
#
# Idempotent: safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P4A_HOME="${HOME}/p4a-16k"
URL='https://github.com/kivy/python-for-android.git'
BRANCH=develop

if [ -d "${P4A_HOME}/.git" ]; then
  echo "== p4a seed: reusing existing clone at ${P4A_HOME}"
else
  rm -rf "${P4A_HOME}"
  git clone --depth 1 -b "${BRANCH}" "${URL}" "${P4A_HOME}"
fi

python3 "${HERE}/patch_p4a_16kb.py" "${P4A_HOME}/pythonforandroid/archs.py"
echo "== p4a seeded at ${P4A_HOME} (16 KB ELF alignment patch active)"
