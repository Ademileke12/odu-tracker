#!/usr/bin/env python3
"""Build & package the ODU Tracker desktop app.

  python tools/build_app.py                 # build onedir bundle
  python tools/build_app.py --deb           # build + create .deb installer
  python tools/build_app.py --portable      # build + dist-agnostic tar.gz
  python tools/build_app.py --skip-build --deb    # re-package only
  python tools/build_app.py --onefile       # single-file executable instead

Result layout for .deb:
  /opt/odu-tracker/app/         PyInstaller bundle
  /usr/bin/odu-tracker          launcher
  /usr/share/applications/      menu entry
  /usr/share/icons/hicolor/…    icon
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "odu_tracker", "web", "static")
ICON = os.path.join(ROOT, "assets", "icon.ico")
ICON_PNG = os.path.join(ROOT, "assets", "icon.png")
APP_VER = "1.1.0"
APP_NAME = "odu-tracker"
BINSRC = os.path.join(ROOT, "dist", "ODU-Tracker")   # onedir result dir
BUNDLE = os.path.join(BINSRC, "ODU-Tracker")         # real executable


def build(onefile=False):
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("pyinstaller missing — run:  pip install pyinstaller")
        return False
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "ODU-Tracker",
        "--add-data", f"{STATIC}{os.pathsep}static",
        "--hidden-import", "waitress",
        "--icon", ICON,
    ]
    try:
        import webview  # noqa: F401
        args += ["--hidden-import", "webview"]
    except ImportError:
        pass
    if onefile:
        args.append("--onefile")
    args.append(os.path.join(ROOT, "app_main.py"))
    r = subprocess.run(args, cwd=ROOT)
    ok = r.returncode == 0
    if ok:
        exe = (os.path.join(ROOT, "dist", "ODU-Tracker.exe")
               if onefile and sys.platform == "win32"
               else (BUNDLE if not (onefile and sys.platform != "win32") else BUNDLE))
        print(f"✓ built → {exe}")
    return ok


def make_deb(deb_dir):
    """Assemble a Debian package containing the onedir bundle."""
    if not (os.path.isdir(BINSRC) and os.path.isfile(BUNDLE)):
        print("bundle missing — build first")
        return None
    pkg = f"{APP_NAME}_{APP_VER}_all"
    root = os.path.join(deb_dir, pkg)
    if os.path.exists(root):
        shutil.rmtree(root)
    # payload
    appdst = os.path.join(root, "opt", APP_NAME, "app")
    os.makedirs(appdst)
    shutil.copytree(BINSRC, appdst, dirs_exist_ok=True)
    os.chmod(BUNDLE, 0o755)
    bin_path = os.path.join(root, "usr", "bin", APP_NAME)
    os.makedirs(os.path.dirname(bin_path), exist_ok=True)
    with open(bin_path, "w") as f:
        f.write(f"""#!/bin/sh
exec /opt/{APP_NAME}/app/ODU-Tracker "$@"
""")
    os.chmod(bin_path, 0o755)
    if os.path.exists(ICON_PNG):
        dst = os.path.join(root, "usr", "share", "icons", "hicolor",
                           "256x256", "apps")
        os.makedirs(dst, exist_ok=True)
        shutil.copy(ICON_PNG, os.path.join(dst, f"{APP_NAME}.png"))
    dst = os.path.join(root, "usr", "share", "applications")
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, f"{APP_NAME}.desktop"), "w") as f:
        f.write("""[Desktop Entry]
Type=Application
Name=ODU Tracker
Comment=MTN ODU data usage, devices & website monitor
Exec=/usr/bin/odu-tracker
TryExec=/opt/odu-tracker/app/ODU-Tracker
Icon=odu-tracker
Categories=Network;Monitor;
StartupWMClass=ODU-Tracker
""")
    meta = os.path.join(root, "DEBIAN")
    os.makedirs(meta, exist_ok=True)
    with open(os.path.join(meta, "control"), "w") as f:
        f.write(f"""Package: {APP_NAME}
Version: {APP_VER}
Section: net
Priority: optional
Architecture: all
Installed-Size: 80000
Depends: chromium | chromium-browser | google-chrome (>=66) | xdg-utils
Maintainer: anakincoco <srimpkoyonko@gmail.com>
Homepage: mailto:srimpkoyonko@gmail.com
Description: MTN ODU (ZLT X17U) data & device tracker
 Full local monitor of data usage, connected devices, security alerts,
 speed tests and per-device website activity. Created by anakincoco.
""")
    with open(os.path.join(meta, "postinst"), "w") as f:
        f.write("""#!/bin/sh
set -e
chmod +x /opt/odu-tracker/app/ODU-Tracker /usr/bin/odu-tracker
exit 0
""")
    os.chmod(os.path.join(meta, "postinst"), 0o755)
    out = os.path.join(deb_dir, f"{pkg}.deb")
    r = subprocess.run(["dpkg-deb", "--build", "--root-owner-group", root, out])
    if r.returncode == 0:
        print(f"✓ .deb → {out}")
        return out
    return None


def make_portable(out_dir):
    """dist-agnostic tarball: unpack anywhere, run ODU-Tracker/ODU-Tracker."""
    if not os.path.isdir(BINSRC):
        return None
    name = f"{APP_NAME}-{APP_VER}-linux-portable"
    stage_root = os.path.join(out_dir, name)
    if os.path.exists(stage_root):
        shutil.rmtree(stage_root)
    shutil.copytree(BINSRC, stage_root, dirs_exist_ok=True)
    with open(os.path.join(stage_root, "START"), "w") as f:
        f.write("""#!/bin/sh
cd "$(dirname "$0")"
./ODU-Tracker "$@"
""")
        os.chmod(os.path.join(stage_root, "START"), 0o755)
    tar = os.path.join(out_dir, name + ".tar.gz")
    shutil.make_archive(tar[:-7], "gztar", out_dir, name)
    print(f"✓ portable → {tar}")
    return tar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true")
    ap.add_argument("--deb", action="store_true")
    ap.add_argument("--portable", action="store_true")
    ap.add_argument("--skip-build", action="store_true")
    a = ap.parse_args()
    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    if not a.skip_build:
        if not build(a.onefile):
            return 2
    if a.deb or not (a.deb or a.portable):
        make_deb(dist)
    if a.portable:
        make_portable(dist)
    return 0


if __name__ == "__main__":
    sys.exit(main())
