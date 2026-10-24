#!/usr/bin/env python3
"""Build the ODU Tracker desktop app into dist/ for this platform.

  python tools/build_app.py              # normal build (one-folder, windowed)
  python tools/build_app.py --onefile    # single executable instead

Prereqs: pip install pyinstaller  (and pywebview for the native window mode).
Static web assets are bundled automatically.
"""
import os
import subprocess
import sys
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "odu_tracker", "web", "static")
ICON = os.path.join(ROOT, "assets", "icon.ico")


def main():
    one = "--onefile" in sys.argv
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("pyinstaller missing — run:  pip install pyinstaller")
        return 2
    sep = os.pathsep
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "ODU-Tracker".replace(" ", "-"),
        "--add-data", f"{STATIC}{sep}static",
        "--hidden-import", "waitress",
    ]
    if os.path.exists(ICON):
        args += ["--icon", ICON]
    if shutil.which("python") == sys.executable or True:
        try:
            import webview  # noqa: F401  (bundled when present)
            args += ["--hidden-import", "webview"]
        except ImportError:
            pass
    if one:
        args.append("--onefile")
    else:
        args += ["--contents-directory", "_internal"]
        if sys.platform == "win32":
            args.append("--console")      # console shows build/runtime errors
    args.append(os.path.join(ROOT, "app_main.py"))
    print("building:",
          f"{'onefile' if one else 'onedir'} app")
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode == 0:
        out = os.path.join(ROOT, "dist",
                           "ODU-Tracker.exe" if one and sys.platform == "win32"
                           else ("ODU-Tracker" if not one else "ODU-Tracker"))
        print(f"\n✓ built → {out}")
        print("  run it directly; use `python run.py autostart` modeled on README"
              " for start-on-login")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
