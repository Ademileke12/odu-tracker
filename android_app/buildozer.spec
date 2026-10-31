[app]

# (str) Title of your application
title = ODU Tracker

# (str) Package name
package.name = odutracker

# (str) Package domain (reversed-style app identifier)
package.domain = org.anakincoco

# (str) The app icon; the repo-wide backend is included so main.py can
# import odu_tracker + android_app
source.dir = ..

# (list) Source files to include
source.include_exts = py,png,jpg,kv,atlas,html,css,js,json,webmanifest

# (list) List of directory to exclude (whole-repo source; keep the APK lean)
source.exclude_dirs = .buildozer,build,dist,node_modules,.venv,venv,__pycache__

# (list) Exclusions via patterns
source.exclude_patterns = packaging/*,tools/*,site/*,dist/*,build/*,*.md,*.spec,*.bat,*.sh,*.desktop,*.iss,ODU-Tracker.spec,win.txt,requirements.txt,.git*

# (str) Application versioning
version = 1.3.1

# (int) versionCode — Android refuses to install an APK whose code is LOWER
# than what's already on the device, so this must only ever go up
# (1.3.0 shipped without this line and got buildozer's default of 1).
version.code = 10301

# (list) Application requirements (kivy = splash screen; pyjnius = WebView)
# charset-normalizer is a transitive dep of `requests`; from 3.5.x PyPI also
# ships android-tagged wheels that the p4a build venv's pip refuses
# (kivy/buildozer#2051) — pin the pure-python release so pip takes the
# universal py3-none-any wheel instead.
requirements = python3,kivy,pyjnius,flask,waitress,requests,urllib3,certifi,charset-normalizer==2.1.1

# (str) Presplash of the application — full-screen 1080x1920 composition
# (tools/make_icon.py). The old 256x256 icon used here was upscaled to the
# whole display: "large logo, not scaled". Resolves relative to the build
# cwd (android_app/), same place packaging expects icon.png.
presplash.filename = assets/presplash.png

# (str) Icon of the application — 1024x1024 source (launcher mipmaps)
icon.filename = assets/icon.png

# (list) Supported orientation — dedicated screen is portrait by default
# (switch to `all` or `landscape` if you wall-mount a tablet sideways)
orientation = portrait

# p4a 2026.5.9 (latest PyPI release) still ships the pip-install stage that
# breaks itself on fresh runs — its `pip install -U pip` inside the build
# venv leaves a mixed pip state and dies with
#   ImportError: cannot import name 'BuildDependencyInstallError'
# (kivy/python-for-android#3339 / #3364). The fixes — PR #3360 (skip the
# venv self-upgrade) and PR #3366 (drop venv creation, custom pip) — are
# merged on the develop branch only.
p4a.branch = develop

# 16 KB ELF alignment, part 1 (linker flags): Android 16/17 devices with
# 16 KB kernels enforce page-size checks on shared libraries. Stock p4a does
# not link with -Wl,-z,common-page-size=16384, which is required for the
# strictest published check ("Check the RELRO security flag",
# developer.android.com/guide/practices/page-sizes). tools/seed_p4a.sh
# clones p4a develop into ~/p4a-16k and applies the flags
# (tools/patch_p4a_16kb.py); p4a.source_dir makes buildozer use that
# patched checkout AS-IS (no re-clone / no pull over it).
p4a.source_dir = ~/p4a-16k

# (bool) Keep the screen on while the app runs (dedicated monitoring screen)
android.wakelock = True

# (bool) Fullscreen (hides the status bar)
fullscreen = 1

# (string) Presplash background color — matches the dark theme
android.presplash_color = #05070B

# (list) Permissions
android.permissions = INTERNET, ACCESS_NETWORK_STATE, ACCESS_WIFI_STATE, WAKE_LOCK

# The engine + dashboard talk plain http to 127.0.0.1 and to the router
# (192.168.0.1) — allow cleartext app-wide via a manifest application attr.
android.extra_manifest_application_arguments = ./src/android/extra_manifest_application_arguments.xml

# (bool) Allow backup: settings + tiny sqlite history move via adb backup
android.allow_backup = True

# (int) Target/minimum Android API — leave defaults (API 36 / min 24, Android 7+)

# (str) Android NDK version to use — leave empty for buildozer default
#android.ndk = 28c

# (str) Android archs to build for
android.archs = arm64-v8a, armeabi-v7a

# (bool) Automatically accept SDK license for automation
android.accept_sdk_license = True

# (str) Release packaging format: users sideload the APK directly
android.release_artifact = apk
android.debug_artifact = apk

# NOTE: release *signing* uses environment variables (buildozer >= 1.5):
#   P4A_RELEASE_KEYSTORE=odu-tracker-release.keystore
#   P4A_RELEASE_KEYSTORE_PASSWD=odu-tracker-2026
#   P4A_RELEASE_KEYALIAS_PASSWD=odu-tracker-2026
#   P4A_RELEASE_KEYALIAS=odu-tracker
# tools/build_android.sh and .github/workflows/android-apk.yml set them.

[buildozer]
# (int) Log level (0 = error only, 1 = info, 2 = debug with command output)
log_level = 1

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1

# (str) Path to build artifact storage (relative to this spec file)
build_dir = ./.buildozer

# (str) Path to build output (i.e. .apk, .aab) storage
bin_dir = ./bin
