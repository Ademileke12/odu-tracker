"""Desktop window for the dashboard — Linux & Windows.

Preference order:
  1. pywebview (if installed)   -> true native web view window
  2. Chromium-family --app mode -> Edge/Chrome window without browser UI,
     using a dedicated profile so the user's normal browser is untouched
  3. default system browser

Nothing here ever touches the router's own admin page; we only render our
own local dashboard. The URL is bound to 127.0.0.1.
"""
import os
import shutil
import subprocess
import sys


def chromium_app_candidates():
    """Local app-mode launchers we can find on this machine."""
    cands = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        cands += [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        ]
    else:
        cands += ["microsoft-edge", "google-chrome", "google-chrome-stable",
                  "chromium", "chromium-browser", "brave-browser"]
    out = []
    for c in cands:
        if os.path.isfile(c) or shutil.which(c):
            out.append(c)
    return out


def _app_window_via_chromium(url, profile_name="ODUTrackerApp"):
    cands = chromium_app_candidates()
    if not cands:
        return False
    if sys.platform == "win32":
        profile = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                               "ODUTracker", "app-window-profile")
    else:
        profile = os.path.expanduser("~/.local/share/odu-tracker/app-window-profile")
    args = [cands[0], f"--app={url}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=TranslateUI", "--window-size=1280,860"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def open_window(url, title="ODU Tracker"):
    """Blocking when a native webview is available; non-blocking app window
    otherwise. Returns the mode used: 'webview' | 'chromium' | 'browser'."""
    try:
        import webview                      # pywebview — best experience
    except ImportError:
        webview = None
    if webview is not None:
        webview.create_window(title, url, width=1280, height=860,
                              min_size=(960, 640))
        webview.start()
        return "webview"
    if _app_window_via_chromium(url):
        return "chromium"
    import webbrowser
    webbrowser.open(url)
    return "browser"
