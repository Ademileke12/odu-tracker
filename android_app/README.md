# ODU Tracker — Android APK

A **dedicated, full-screen Android app**: install it, connect the phone to
the MTN ODU's Wi-Fi, open it — done. It runs the complete tracker engine
(Flask + poller + SQLite, the same code the desktop app uses) *on the phone
itself* and shows the dashboard in a full-screen native WebView. No PC,
no browser, no server needed.

```
┌─────────────────────────────────────────────┐
│           Kivy splash / logo                │   ← while the engine boots
├─────────────────────────────────────────────┤
│  native WebView (full screen, stays awake)  │   ← phone-first dashboard
│  http://127.0.0.1:8770/mobile               │     at /mobile
└─────────────────────────────────────────────┘
        Python engine (this same app process)
        config + SQLite in the app's private dir
```

The WebView loads **`/mobile`** — a phone-first page (big usage gauge,
bottom tabs for Overview / Devices / Alerts) built for a thumb, not the
desktop dashboard squeezed onto a small screen.

## Install (users, not developers)

1. Get `ODU-Tracker.apk` — built by `./build_android.sh` (desktop) or CI
   (`main_app/.github/workflows/workflows.yml`).
2. Copy it to any Android 7+ phone, tap the file, allow
   *Install unknown apps* once, install.
3. Open **ODU Tracker**, then connect the phone to the **ODU Wi-Fi** —
   the app detects the router and shows the sign-in dashboard
   (same screen as the desktop app; the admin password never leaves the
   phone).

Behaviour baked in for a dedicated screen:

* portrait, fullscreen (no status bar)
* screen stays on while the app is open (`WAKE_LOCK` + keep-screen-on)
* the sign-in session persists (30-day cookie in the WebView)

## Build locally

```bash
cd odu-tracker
./tools/build_android.sh            # → android_app/bin/*-release.apk
./tools/build_android.sh --debug    # debug APK for device testing
```

Machine requirements (first build downloads the Android SDK/NDK, ~6 GB):

* Java 17+ (`keytool` in PATH)
* Linux packages: `git zip unzip autoconf libtool pkg-config zlib1g-dev
  libncurses-dev cmake libffi-dev libssl-dev`
* buildozer + cython — the script installs them into the project venv
  automatically

First build takes a while (p4a compiles Python + Kivy from scratch);
subsequent builds are much faster.

### CI build

`.github/workflows/android-apk.yml` builds a **signed release APK** on every
push (release notes attach the APK to tags). No secrets required —
the self-signed keystore below is committed.

## Signing

The repo ships a self-signed release keystore so updates install cleanly
over previous versions (same signature ⇒ no reinstall dance):

* file:      `android_app/odu-tracker-release.keystore`
* alias:     `odu-tracker`
* passwords: `odu-tracker-2026` (store + key)

For wide distribution, replace it with your **private** keystore holding the
same alias/paths and delete the committed one from the repo — everything is
wired through the `P4A_RELEASE_*` environment variables, the same way
python-for-android signs release APKs.

## Data & privacy

* settings, history and alerts live in the app's private dir
  (`/data/data/org.anakincoco.odutracker/files/...`) — wiping app data
  resets the tracker
* the router admin password stays on the phone; every request goes
  directly to `http://192.168.0.1` over your local Wi-Fi

## What works / what's different vs. desktop

| Feature | On Android |
|---|---|
| usage stats, live speeds, charts | ✅ |
| devices, risk scoring, block/unblock | ✅ |
| events/alerts, battery/signal | ✅ |
| speed test | ✅ (uses the phone's connection) |
| **DNS browsing-log proxy** | ❌ desktop-only (needs port 53 privileges) |
| keeping the app alive in background | the engine lives inside the app; Android may pause it when the app is in the background — keep this screen open for continuous logging |

Keep the app out of battery-optimization stubbornness if you rely on
long-horizon logs: *Settings → Apps → ODU Tracker → Battery → Unrestricted*
(most phones keep it running fine as-is though).
