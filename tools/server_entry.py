#!/usr/bin/env python3
"""PyInstaller entrypoint: headless ODU Tracker server.

Build (see tools/build_electron.sh / packaging/windows/build-electron.bat):

    pyinstaller --name odu-tracker-server --add-data ... \
        --hidden-import waitress tools/server_entry.py

The Electron shell (packaging/electron/) bundles this binary inside its
installers and launches it with  --port N , then opens a window on
http://127.0.0.1:N/.
"""
import sys

from odu_tracker.headless import main

if __name__ == "__main__":
    sys.exit(main())
