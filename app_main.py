#!/usr/bin/env python3
"""PyInstaller entry point for the ODU Tracker desktop app (Linux/Windows)."""
import sys

port = 8770
argv = sys.argv[1:]
if "--port" in argv:
    try:
        port = int(argv[argv.index("--port") + 1])
    except (IndexError, ValueError):
        pass
from odu_tracker.launcher import desktop_main

desktop_main(port=port)
