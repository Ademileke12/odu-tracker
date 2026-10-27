#!/usr/bin/env python3
"""ODU Tracker — Android APK entry point.

buildozer's source.dir is the repo root, so main.py must live HERE and only
delegates to the real Android shell in android_app/. Desktop users don't run
this file — use `python run.py serve` / `python run.py app` instead.

See android_app/main.py for the actual app (Kivy splash + native WebView).

Self-diagnosing entry: python-for-android's start.c answers any unhandled
exception with PyErr_Print() (logcat only) + exit(), which on a device looks
like "large logo appears, app closes" with nothing to read. So EVERYTHING is
wrapped: failures are caught here, persisted to odu-crash.txt and PAINTED on
screen by android_app/diagnostics.py (pure pyjnius — works even when Kivy
itself is what broke), and the process is kept alive until dismissed.
"""
import os
import sys

# make the android_app package importable when running from this root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from android_app import diagnostics as diag    # noqa: E402  (stdlib-only module)

diag.stage("entry: python interpreter up")
diag.install_hooks()


def _main():
    diag.stage("entry: replaying previous crash report (if any)")
    diag.show_pending_report()

    diag.stage("entry: importing app module (kivy window + engine imports)")
    from android_app.main import run

    diag.stage("entry: starting the app")
    run()

    diag.stage("entry: app loop returned")
    diag.report_premature_exit()


if __name__ == "__main__":
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        diag.fatal_exc(detail="the app failed during startup "
                              "(traceback below; stage in header)")
        raise                      # only reached when no screen could be drawn
