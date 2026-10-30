"""ODU Tracker — dedicated Android screen app.

Architecture (all off-line, all local):
    [Python engine]  odu_tracker (Flask + waitress + poller + SQLite)
            |            started as a daemon thread inside this process
            v
      127.0.0.1:8770
            |
    [Kivy splash]  ->  [native Android WebView]   (full screen, stays on)
                       loads the very same dashboard UI the desktop app uses

The WebView is created through pyjnius because Kivy has no built-in WebView
widget. Native view sits on top of the Kivy canvas once the engine answers.
"""
import os
import socket
import sys
import threading
import traceback

# Diagnostics FIRST (stdlib only): every stage below is a breadcrumb that a
# startup crash report will point at. See android_app/diagnostics.py.
try:
    from android_app import diagnostics as diag
except ImportError:                 # running this file directly, not packaged
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from android_app import diagnostics as diag

diag.stage("imports: kivy (window/GL init happens here)")
from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color as GColor, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.utils import platform

diag.stage("imports: odu_tracker engine modules")
from odu_tracker.config import Config
from odu_tracker.poller import Poller
from odu_tracker.store import init as init_db
from odu_tracker.web.app import create_app

diag.stage("imports: app module ready")

SERVER_PORT = 8770
# Phone-first dashboard (/mobile). The desktop UI squeezed into a phone
# WebView was the "badly designed" half of the complaint — the engine
# serves a thumb-sized layout at /mobile with the same APIs.
MOBILE_PATH = "/mobile"
BG = (0.02, 0.027, 0.043)          # #05070b
ACCENT = (1.0, 0.714, 0.196)       # #ffb632
FG = (0.91, 0.92, 0.94)
MUTED = (0.62, 0.68, 0.78)
FAINT = (0.42, 0.47, 0.56)

IS_ANDROID = platform == "android"


# --------------------------------------------------------------------------
# Python engine (Flask + poller + waitress)
# --------------------------------------------------------------------------
_backend_started = False
_backend_lock = threading.Lock()


def _serve(port):
    """Waitress first (production-grade), werkzeug dev server as fallback."""
    def boot():
        try:
            diag.stage("engine: opening database")
            init_db()
            cfg = Config()
            diag.stage("engine: building the flask app")
            poller = Poller(cfg)
            app = create_app(cfg, poller)
            poller.start()
            from odu_tracker import APP_NAME
            print(f"  {APP_NAME} engine running on 127.0.0.1:{port}", flush=True)
            try:
                from waitress import serve as wserve
                diag.stage("engine: serving on 127.0.0.1:8770 (waitress)")
                wserve(app, host="127.0.0.1", port=port, threads=4)
            except Exception:
                diag.stage("engine: serving on 127.0.0.1:8770 (werkzeug)")
                app.run(host="127.0.0.1", port=port, debug=False,
                        use_reloader=False, threaded=True)
        except BaseException:
            # The app is useless without the engine — surface it on screen.
            # (runs on the engine thread: the Kivy loop is already going,
            #  so no nested Kivy fallback — native screen only)
            diag.fatal_exc(detail="the local engine failed to start",
                           allow_kivy_fallback=False)
    return boot


def start_backend(port=SERVER_PORT):
    global _backend_started
    if _backend_started:
        return
    with _backend_lock:
        if _backend_started:
            return
        t = threading.Thread(target=_serve(port), name="odu-engine", daemon=True)
        t.start()
        _backend_started = True


def _server_up(port=SERVER_PORT, timeout=0.6):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Android bridge (pyjnius) — native full-screen WebView
# --------------------------------------------------------------------------
# Post to the Android UI thread via android_app.diagnostics.post_to_ui —
# a hand-rolled pyjnius Runnable with the default ('system') javacontext.
# (An earlier __javacontext__ = "thread" was invalid: pyjnius 1.7.0 accepts
# only 'app' or 'system' and raised on proxy creation.)


def _android_activity():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


# The live WebView (kept so the Android lifecycle can pause/resume it and
# so back-navigation can drive it). A WebView that never gets onPause()/
# onResume() misbehaves badly once the app is backgrounded: timers keep
# burning battery, the render surface goes stale, and on many phones the
# whole activity is killed soon after — the "doesn't stay long enough"
# symptom. See OduTrackerAndroidApp.on_pause/on_resume.
_WEBVIEW = {"wv": None, "lp": None}


def build_webview(url):
    """Create + configure + attach the full-screen WebView (UI thread)."""
    from jnius import autoclass

    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    CookieManager = autoclass("android.webkit.CookieManager")
    Color = autoclass("android.graphics.Color")
    ViewGroupLayoutParams = autoclass("android.view.ViewGroup$LayoutParams")

    MATCH_PARENT = -1      # ViewGroup.LayoutParams.MATCH_PARENT

    wv = WebView(_android_activity())
    wv.setBackgroundColor(Color.rgb(13, 18, 28))   # #0d121c — matches UI dark
    wv.setKeepScreenOn(True)                       # dedicated-screen behaviour

    s = wv.getSettings()
    s.setJavaScriptEnabled(True)
    s.setDomStorageEnabled(True)
    s.setAllowFileAccess(False)
    s.setSupportZoom(False)                        # mobile page scales itself
    s.setBuiltInZoomControls(False)
    s.setMediaPlaybackRequiresUserGesture(False)
    s.setLoadWithOverviewMode(True)
    s.setUseWideViewPort(True)

    wv.setWebViewClient(WebViewClient())
    # NOTE: no custom WebChromeClient — pyjnius can only proxy *interfaces*
    # and android.webkit.WebChromeClient is a class, so the old proxy raised
    # on construction. Default client keeps the WebView alive; the mobile
    # page uses no confirm()/alert(), so nothing needs bridging.

    # keep cookies (the 30-day "keep me signed in" session) for this app only
    CookieManager.getInstance().setAcceptCookie(True)
    try:
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, False)
    except Exception:
        pass

    lp = ViewGroupLayoutParams(MATCH_PARENT, MATCH_PARENT)
    _android_activity().addContentView(wv, lp)
    wv.loadUrl(url)
    _WEBVIEW["wv"], _WEBVIEW["lp"] = wv, lp      # lifecycle handle
    return wv


def _webview_ui(fn):
    """Run fn(wv) on the UI thread if a WebView exists; never raise."""
    wv = _WEBVIEW.get("wv")
    if wv is None:
        return
    try:
        diag.post_to_ui(lambda: fn(wv))
    except BaseException:
        traceback.print_exc()


# --------------------------------------------------------------------------
# Kivy splash / status screen
# --------------------------------------------------------------------------
def _centered(text, size, height, color, **kw):
    """Label with halign=center that works (text_size bound to width)."""
    lbl = Label(text=text, font_size=size, size_hint=(1, None),
                height=height, color=color, halign="center",
                valign="middle", **kw)
    lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
    return lbl


class OduTrackerAndroidApp(App):
    def build(self):
        diag.stage("kivy: build() entered — drawing the splash")
        Window.softinput_mode = "below_target"
        self.root = BoxLayout(orientation="vertical", padding=(dp(36), dp(48)))
        with self.root.canvas.before:
            GColor(*BG, 1)
            self.bg = Rectangle(size=self.root.size, pos=self.root.pos)
        self.root.bind(size=self._resize_bg, pos=self._resize_bg)

        self.logo = _centered("ODU", "44sp", dp(80), ACCENT, bold=True)
        # NB: NOT self.title — App.title is Kivy's window-title StringProperty;
        # assigning a Label raised ValueError inside build() on every device
        # ("big logo, app closes" — died before this splash ever drew).
        self.heading = _centered("TRACKER", "30sp", dp(40), FG, bold=True)
        self.subtitle = _centered("Booting the local engine…", "16sp",
                                  dp(60), MUTED)
        self.progress = ProgressBar(max=100, value=6,
                                    size_hint=(0.72, None),
                                    height=dp(8),
                                    pos_hint={"center_x": 0.5})
        self.tip = _centered("Wi-Fi required: connect this phone to the ODU network.",
                             "13sp", dp(30), FAINT)

        for widget in (self.logo, self.heading, self.subtitle,
                       self.progress, self.tip):
            self.root.add_widget(widget)

        diag.stage("kivy: splash built — launching the engine thread")
        start_backend()

        self._ready = False
        self._pct = 6
        self._elapsed = 0
        Clock.schedule_interval(self._tick, 0.25)
        diag.stage("kivy: splash live — waiting for the engine")
        return self.root

    def _resize_bg(self, _w, _v):
        self.bg.size = self.root.size
        self.bg.pos = self.root.pos

    def _tick(self, dt):
        try:
            return self._tick_inner(dt)
        except BaseException:
            # Kivy's event loop swallows callback errors silently — surface
            # them on screen instead. Loop is running → native screen only.
            diag.fatal_exc(detail="crash inside the splash/progress loop",
                           allow_kivy_fallback=False)
            return False

    def _tick_inner(self, dt):
        if self._ready:
            return False
        self._elapsed += dt
        if _server_up():
            self._install_webview()
            return False
        self._pct = min(int(self._pct + 4), 96)
        self.progress.value = self._pct
        if self._elapsed > 90:
            self.subtitle.text = "Engine still booting — it can take a minute on first start."
            self.tip.text = "If this never finishes, close the app and open it again."
        return True

    def _install_webview(self):
        self._ready = True
        self.progress.value = 100
        self.subtitle.text = "Loading dashboard…"
        url = f"http://127.0.0.1:{SERVER_PORT}{MOBILE_PATH}"
        if IS_ANDROID:
            diag.stage("dashboard: handing off to the native WebView")

            def _do():
                # runs on the Android UI thread — never block it; fatal()
                # knows this (on_ui_thread=True) and only paints.
                try:
                    build_webview(url)
                    diag.stage("dashboard: webview installed — live")
                except BaseException:
                    diag.fatal_exc(detail="creating the native WebView failed",
                                   on_ui_thread=True,
                                   allow_kivy_fallback=False)

            try:
                diag.post_to_ui(_do)
            except BaseException:
                diag.fatal_exc(detail="could not post the WebView to the UI "
                                      "thread (pyjnius/jnius problem)",
                               allow_kivy_fallback=False)
        else:
            # desktop dev preview of the same shell
            import webbrowser
            webbrowser.open(url)
            diag.stage("dashboard: opened in browser (desktop preview)")
            self.subtitle.text = "Dashboard opened in browser (desktop preview)."

    # ------------------------------------------------- Android lifecycle
    # Kivy's App pauses the *Kivy* side only. The native WebView we added
    # on top must be paused/resumed explicitly, and on resume it may sit
    # UNDER the re-attached Kivy surface — hence the re-attach. Without
    # this trio the app "doesn't stay": backgrounding it once was enough
    # for the dashboard to freeze or vanish on many phones.
    def on_pause(self):
        diag.stage("lifecycle: paused (webview paused)")
        _webview_ui(lambda wv: wv.onPause())
        return True                               # keep the process alive

    def on_resume(self):
        diag.stage("lifecycle: resumed (webview resumed + re-attached)")
        wv = _WEBVIEW.get("wv")

        def _do(wv):
            try:
                wv.onResume()
                # p4a re-adds the SDL/GL surface on top after resume —
                # pull the WebView back to the front of the same window.
                parent = wv.getParent()
                if parent is not None:
                    parent.removeView(wv)
                _android_activity().addContentView(wv, _WEBVIEW["lp"])
                wv.bringToFront()
                wv.invalidate()                   # repaint now, not on touch
            except BaseException:
                traceback.print_exc()
        _webview_ui(_do)
        return True

    def on_stop(self):
        diag.stage("lifecycle: stop")


def run():
    diag.stage("kivy: App.run() entering the event loop")
    try:
        OduTrackerAndroidApp().run()
    except BaseException:
        # any exception that escaped the app loop — paint it, don't exit
        # silently (start.c would just logcat + exit(1))
        diag.fatal_exc(detail="the app loop crashed")
        raise
    # App.run() returned: either the user closed it (normal) or the window
    # died before the dashboard ever came up (premature exit — diagnostics
    # decides which and reports the latter on screen).
    diag.report_premature_exit()


if __name__ == "__main__":
    run()
