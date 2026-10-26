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
version = 1.2.0

# (list) Application requirements (kivy = splash screen; pyjnius = WebView)
# charset-normalizer is a transitive dep of `requests`; from 3.5.x PyPI also
# ships android-tagged wheels that the p4a build venv's pip refuses
# (kivy/buildozer#2051) — pin the pure-python release so pip takes the
# universal py3-none-any wheel instead.
requirements = python3,kivy,pyjnius,flask,waitress,requests,urllib3,certifi,charset-normalizer==2.1.1

# (str) Presplash of the application
presplash.filename = assets/icon.png

# (str) Icon of the application
icon.filename = assets/icon.png

# (list) Supported orientation — dedicated screen is portrait by default
# (switch to `all` or `landscape` if you wall-mount a tablet sideways)
orientation = portrait

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
