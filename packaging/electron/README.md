# ODU Tracker — dedicated Electron desktop app

A real desktop app users install once and just use. The `.deb` (Linux) and
`.exe` installers (Windows) bundle **both** pieces:

* the **Electron shell** (this folder) — window, icon, taskbar, single
  instance, cookie-persistent sign-in
* the **frozen Python engine** (`odu_tracker.headless` via PyInstaller) —
  the actual tracker: poller, SQLite, Flask dashboard, waitressing on
  `127.0.0.1:<port>`

No Python, Node or browser installation is required on the target machine.

```
Electron window ──▶ loadURL http://127.0.0.1:<port>/
        │ spawns
        └──▶ resources/odu-tracker-server/  (PyInstaller engine, bundled)
```

## Build — Linux (.deb), done for you in this repo

```bash
./tools/build_electron.sh
# → dist/electron/odu-tracker_<version>_amd64.deb
sudo dpkg -i dist/electron/odu-tracker_*.deb
```

Outputs land in `dist/electron/`. `.github/workflows/linux-deb.yml` builds
the same artifact automatically on every push (attach .deb to GitHub
releases).

## Build — Windows (NSIS installer + portable exe)

Run on the **Windows machine** (PyInstaller can't cross-compile from Linux):

```powershell
cd odu-tracker
packaging\windows\build-electron.bat
# → dist\electron\ODU-Tracker-Setup-<version>.exe    (one-click installer)
# → dist\electron\ODU-Tracker-Portable-<version>.exe (single-file, no install)
```

## Behaviour

* the shell asks the OS for a free port in 8770–8777 before starting the
  engine, so it never fights with a console build (`run.py serve`) or a
  browser tab already on the dashboard
* engine logs: `%TMP%/odu-tracker-electron.log` (stdout/stderr captured)
* single-instance: launching a second copy focuses the running window
* same per-user data dir as every other ODU Tracker build (Linux
  `~/.local/share/odu-tracker`, Windows `%APPDATA%\OduTracker`) — settings
  and history are shared with the Python-only builds
* links out (e.g. the router admin page) open in the system browser
* engine crash → up to 3 automatic restarts, then an error dialog

## Layout

| file | role |
|---|---|
| `main.js` | window + engine lifecycle |
| `loading.html` | pre-connect start screen |
| `electron-builder.yml` | deb / NSIS / portable packaging config |
| `build/icon.png` `build/icon.ico` | app icons (from `assets/`) |
| `resources/odu-tracker-server/` | staged engine (created by the build scripts, git-ignored — do **not** edit) |
