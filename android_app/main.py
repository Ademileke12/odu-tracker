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
import socket
import threading
import traceback

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color as GColor, Rectangle
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.utils import platform

from odu_tracker.config import Config
from odu_tracker.poller import Poller
from odu_tracker.store import init as init_db
from odu_tracker.web.app import create_app

SERVER_PORT = 8770
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
        init_db()
        cfg = Config()
        poller = Poller(cfg)
        app = create_app(cfg, poller)
        poller.start()
        from odu_tracker import APP_NAME
        print(f"  {APP_NAME} engine running on 127.0.0.1:{port}", flush=True)
        try:
            from waitress import serve as wserve
            wserve(app, host="127.0.0.1", port=port, threads=4)
        except Exception:
            app.run(host="127.0.0.1", port=port, debug=False,
                    use_reloader=False, threaded=True)
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
def _android_activity():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonActivity").mActivity


class _Runnable:
    """java/lang/Runnable for activity.runOnUiThread()."""

    _pyjnius_ready = False

    def __new__(cls, fn):
        from jnius import PythonJavaClass, java_method

        if not cls._pyjnius_ready:
            class _R(PythonJavaClass):
                __javainterfaces__ = ["java/lang/Runnable"]
                __javacontext__ = "thread"

                @java_method("()V")
                def run(self):
                    try:
                        self.fn()
                    except Exception:
                        traceback.print_exc()

            cls._cls = _R
            cls._pyjnius_ready = True
        instance = cls._cls()
        instance.fn = fn
        return instance


def run_on_ui_thread(fn):
    activity = _android_activity()
    activity.runOnUiThread(_Runnable(fn))


class _ChromeClient:
    """Default-ish WebChromeClient so JS alert()/file pickers work."""

    _pyjnius_ready = False

    def __new__(cls):
        from jnius import PythonJavaClass

        if not cls._pyjnius_ready:
            class _C(PythonJavaClass):
                __javaclass__ = "android/webkit/WebChromeClient"

            cls._cls = _C
            cls._pyjnius_ready = True
        return cls._cls()


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
    s.setSupportZoom(True)
    s.setBuiltInZoomControls(True)
    s.setDisplayZoomControls(False)
    s.setMediaPlaybackRequiresUserGesture(False)
    s.setLoadWithOverviewMode(False)
    s.setUseWideViewPort(False)

    wv.setWebViewClient(WebViewClient())
    wv.setWebChromeClient(_ChromeClient())

    # keep cookies (the 30-day "keep me signed in" session) for this app only
    CookieManager.getInstance().setAcceptCookie(True)
    try:
        CookieManager.getInstance().setAcceptThirdPartyCookies(wv, False)
    except Exception:
        pass

    lp = ViewGroupLayoutParams(MATCH_PARENT, MATCH_PARENT)
    _android_activity().addContentView(wv, lp)
    wv.loadUrl(url)
    return wv


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
        Window.softinput_mode = "below_target"
        self.root = BoxLayout(orientation="vertical", padding=(dp(36), dp(48)))
        with self.root.canvas.before:
            GColor(*BG, 1)
            self.bg = Rectangle(size=self.root.size, pos=self.root.pos)
        self.root.bind(size=self._resize_bg, pos=self._resize_bg)

        self.logo = _centered("ODU", "44sp", dp(80), ACCENT, bold=True)
        self.title = _centered("TRACKER", "30sp", dp(40), FG, bold=True)
        self.subtitle = _centered("Booting the local engine…", "16sp",
                                  dp(60), MUTED)
        self.progress = ProgressBar(max=100, value=6,
                                    size_hint=(0.72, None),
                                    height=dp(8),
                                    pos_hint={"center_x": 0.5})
        self.tip = _centered("Wi-Fi required: connect this phone to the ODU network.",
                             "13sp", dp(30), FAINT)

        for widget in (self.logo, self.title, self.subtitle, self.progress, self.tip):
            self.root.add_widget(widget)

        start_backend()

        self._ready = False
        self._pct = 6
        self._elapsed = 0
        Clock.schedule_interval(self._tick, 0.25)
        return self.root

    def _resize_bg(self, _w, _v):
        self.bg.size = self.root.size
        self.bg.pos = self.root.pos

    def _tick(self, dt):
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
        url = f"http://127.0.0.1:{SERVER_PORT}/"
        try:
            if IS_ANDROID:
                run_on_ui_thread(lambda: build_webview(url))
            else:
                # desktop dev preview of the same shell
                import webbrowser
                webbrowser.open(url)
                self.subtitle.text = "Dashboard opened in browser (desktop preview)."
        except Exception:
            traceback.print_exc()
            self.subtitle.text = "WebView failed — tell: srimpkoyonko@gmail.com"


def run():
    OduTrackerAndroidApp().run()


if __name__ == "__main__":
    run()
