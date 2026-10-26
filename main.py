#!/usr/bin/env python3
"""ODU Tracker — Android APK entry point.

buildozer's source.dir is the repo root, so main.py must live HERE and only
delegates to the real Android shell in android_app/. Desktop users don't run
this file — use `python run.py serve` / `python run.py app` instead.

See android_app/main.py for the actual app (Kivy splash + native WebView).
"""
import os
import sys

# make the android_app package importable when running from this root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from android_app.main import run          # noqa: E402

if __name__ == "__main__":
    run()
