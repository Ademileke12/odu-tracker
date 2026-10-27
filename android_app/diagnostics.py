"""Self-diagnosing startup diagnostics for the Android build.

Why this exists
---------------
python-for-android's ``start.c`` runs the entry script with
``PyRun_SimpleFile`` and, on any unhandled exception, only does
``PyErr_Print()`` (→ logcat) + ``exit()``. On the device that looks like:
native presplash ("large logo") appears, then the app closes — with no
visible error at all. This module makes every failure visible *on the
device itself*:

* ``stage()`` records breadcrumb steps so a report says how far boot got.
* Reports are persisted to ``odu-crash.txt`` (app-private dir + the
  externally visible files dir) so they survive the process dying.
* The report embeds the last lines of the app's own logcat — no adb needed.
* ``fatal()`` paints a full-screen **native** error screen (pure pyjnius,
  no Kivy involved — Kivy may be exactly what broke) with a CLOSE button,
  and keeps the process alive until the user has read/screenshot it,
  defeating start.c's immediate ``exit()``.
* If the native screen cannot be drawn, a Kivy error screen is tried
  (only when the Kivy loop is not already running), else the report is
  printed (→ logcat) and persisted.
* ``show_pending_report()`` replays the previous crash once at the next
  launch — covering crashes that happened before any screen was drawn.

Import-safe by design: stdlib only at module import time. jnius, Kivy and
the p4a android package are imported lazily inside the functions that
need them, because they may be the very thing that broke.
"""
import os
import sys
import threading
import time
import traceback

CRASH_FILE = "odu-crash.txt"
SHOWN_FILE = "odu-crash.shown.txt"

CONTACT = "srimpkoyonko@gmail.com"

STAGE = "diagnostics module imported"
_stage_lock = threading.Lock()

# Sticky: once ANY "dashboard:*" stage is recorded, a later clean return from
# App.run() is a NORMAL close, not a premature exit. (main.py overwrites
# STAGE with "entry: app loop returned" before calling
# report_premature_exit(), so checking STAGE there would never match.)
_reached_dashboard = False

_lock = threading.Lock()
_fatal_seen = False            # only the first fatal() draws an error screen

# pyjnius proxies queued for the Android UI thread must be kept alive on the
# Python side until they have run (p4a's android.runnable does the same).
_UI_REFS = []
_uirunnable_cls = None
_click_cls = None


# --------------------------------------------------------------------------
# breadcrumbs
# --------------------------------------------------------------------------
def stage(name):
    """Remember the last startup step reached (also printed → logcat)."""
    global STAGE, _reached_dashboard
    with _stage_lock:
        STAGE = name
        if name.startswith("dashboard:"):
            _reached_dashboard = True
    print(f"[diag] stage: {name}", flush=True)


def is_android():
    return bool(
        os.environ.get("ANDROID_PRIVATE") or os.environ.get("ANDROID_ARGUMENT")
    )


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
def _external_files_dir():
    """App-specific external dir (Android/data/<pkg>/files) — best effort."""
    try:
        from jnius import autoclass

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        d = activity.getExternalFilesDir(None)
        if d is None:
            return None
        return str(d.getAbsolutePath())
    except Exception:
        return None


def _crash_dirs():
    """Writable dirs where a report may be persisted / replayed."""
    dirs = []
    for d in (os.getcwd(), os.environ.get("ANDROID_PRIVATE"),
              _external_files_dir()):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def _write_report(text):
    """Persist the report to every writable location (Android only)."""
    if not is_android():
        return []
    paths = []
    for d in _crash_dirs():
        path = os.path.join(d, CRASH_FILE)
        try:
            with open(path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
            paths.append(path)
        except Exception:
            pass
    return paths


def _take_pending():
    """Return the previous run's report once, then retire its file."""
    for d in _crash_dirs():
        path = os.path.join(d, CRASH_FILE)
        try:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if not text.strip():
                continue
            try:
                os.replace(path, os.path.join(d, SHOWN_FILE))
            except Exception:
                try:
                    os.remove(path)
                except Exception:
                    pass
            return text
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# report content
# --------------------------------------------------------------------------
def device_info():
    try:
        from jnius import autoclass

        Build = autoclass("android.os.Build")
        Version = autoclass("android.os.Build$VERSION")
        return (f"{Build.MANUFACTURER} {Build.MODEL} — Android "
                f"{Version.RELEASE} (API {Version.SDK_INT})")
    except Exception:
        return ""


def logcat_tail(max_lines=300, max_chars=20000):
    """Last lines of this app's own logcat (no adb required).

    Since Jelly Bean an app may read its own UID's log without any
    permission; ``logcat -d`` dumps and exits. Any failure → "".
    """
    proc = None
    try:
        from jnius import autoclass

        runtime = autoclass("java.lang.Runtime").getRuntime()
        proc = runtime.exec(f"logcat -d -v time -t {max_lines}")
        BufferedReader = autoclass("java.io.BufferedReader")
        InputStreamReader = autoclass("java.io.InputStreamReader")
        reader = BufferedReader(InputStreamReader(proc.getInputStream()))
        lines = []
        while len(lines) < max_lines:
            line = reader.readLine()
            if line is None:
                break
            lines.append(line)
        reader.close()
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = "…" + text[-max_chars:]
        return text
    except Exception:
        return ""
    finally:
        if proc is not None:
            try:
                proc.destroy()
            except Exception:
                pass


def compose(title, detail="", exc_text=""):
    """Build the full human-readable report (stage + device + trace + log)."""
    out = [
        title,
        "",
        f"when (UTC): {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}",
        f"last stage: {STAGE}",
    ]
    dev = device_info()
    if dev:
        out.append(f"device: {dev}")
    if detail:
        out += ["", detail]
    if exc_text:
        out += ["", "--- traceback ---", exc_text.rstrip()]
    log = logcat_tail()
    if log:
        out += ["", "--- last app logcat lines ---", log]
    out += ["", f"Screenshot this screen and send it to {CONTACT}"]
    return "\n".join(out)


# --------------------------------------------------------------------------
# pyjnius building blocks (created lazily — jnius may be what broke)
# --------------------------------------------------------------------------
def _get_ui_runnable_cls():
    """A java/lang/Runnable proxy class posting Python callables to the UI."""
    global _uirunnable_cls
    if _uirunnable_cls is None:
        from jnius import PythonJavaClass, java_method

        class _UIRunnable(PythonJavaClass):
            __javainterfaces__ = ["java/lang/Runnable"]

            def __init__(self, fn):
                super().__init__()
                self.fn = fn

            @java_method("()V")
            def run(self):
                try:
                    self.fn()
                except BaseException:
                    traceback.print_exc()
                finally:
                    try:
                        _UI_REFS.remove(self)
                    except ValueError:
                        pass

        _uirunnable_cls = _UIRunnable
    return _uirunnable_cls


def _get_click_cls():
    """A View.OnClickListener proxy class (button taps → Python)."""
    global _click_cls
    if _click_cls is None:
        from jnius import PythonJavaClass, java_method

        class _Click(PythonJavaClass):
            __javainterfaces__ = ["android/view/View$OnClickListener"]

            def __init__(self, on_pressed):
                super().__init__()
                self.on_pressed = on_pressed

            @java_method("(Landroid/view/View;)V")
            def onClick(self, _view):
                try:
                    self.on_pressed()
                except BaseException:
                    traceback.print_exc()

        _click_cls = _Click
    return _click_cls


def _activity():
    from jnius import autoclass

    return autoclass("org.kivy.android.PythonActivity").mActivity


def post_to_ui(fn):
    """Run ``fn`` on Android's UI thread. Raises on failure (caller decides)."""
    runnable = _get_ui_runnable_cls()(fn)
    _UI_REFS.append(runnable)     # keep the proxy alive until it has run
    _activity().runOnUiThread(runnable)


# --------------------------------------------------------------------------
# the native error screen (works even when Kivy/SDL never started)
# --------------------------------------------------------------------------
def native_overlay(text, title, button_label, on_pressed=None,
                   on_ui_thread=False):
    """Full-screen native error screen. Returns ``(drawn, dismissed)``.

    ``dismissed`` is a ``threading.Event`` set when the button is pressed
    (before ``on_pressed`` runs).  Drawing is posted to the Android UI
    thread — unless we are *already* on it (never block that thread: the
    overlay's own button would deadlock).
    """
    if not is_android():
        return False, None

    dismissed = threading.Event()
    built = threading.Event()
    state = {}

    def _pressed():
        dismissed.set()
        if on_pressed is not None:
            on_pressed()

    def _build():
        try:
            activity = _activity()
            density = activity.getResources().getDisplayMetrics().density

            def px(dips):
                return int(dips * density + 0.5)

            from jnius import autoclass

            Color = autoclass("android.graphics.Color")
            Typeface = autoclass("android.graphics.Typeface")
            TextView = autoclass("android.widget.TextView")
            Button = autoclass("android.widget.Button")
            ScrollView = autoclass("android.widget.ScrollView")
            LinearLayout = autoclass("android.widget.LinearLayout")
            ViewGroupLp = autoclass("android.view.ViewGroup$LayoutParams")
            LinearLp = autoclass("android.widget.LinearLayout$LayoutParams")

            root = LinearLayout(activity)
            root.setOrientation(1)                       # VERTICAL
            root.setBackgroundColor(Color.parseColor("#0A0D14"))
            root.setPadding(px(20), px(28), px(20), px(16))

            head = TextView(activity)
            head.setText(title)
            head.setTextColor(Color.parseColor("#FFB632"))
            head.setTextSize(20.0)
            head.setTypeface(Typeface.DEFAULT_BOLD)
            head.setPadding(0, 0, 0, px(10))
            root.addView(head)

            body = TextView(activity)
            body.setText(text)
            body.setTextColor(Color.parseColor("#E8ECF3"))
            body.setTextSize(12.0)
            body.setTypeface(Typeface.MONOSPACE)
            body.setTextIsSelectable(True)               # copy beats screenshot
            scroller = ScrollView(activity)
            scroller.addView(body)
            # vertical LinearLayout: ScrollView grows with weight, button wraps
            root.addView(scroller, LinearLp(-1, 0, 1.0))

            button = Button(activity)
            button.setText(button_label)
            listener = _get_click_cls()(_pressed)
            state["click"] = listener                    # keep proxy alive
            button.setOnClickListener(listener)
            root.addView(button)

            activity.addContentView(root, ViewGroupLp(-1, -1))
            try:
                activity.getWindow().addFlags(128)       # FLAG_KEEP_SCREEN_ON
            except Exception:
                pass
            state["drawn"] = True
        except BaseException:
            traceback.print_exc()
        finally:
            built.set()

    if on_ui_thread:
        _build()
    else:
        try:
            post_to_ui(_build)
            built.wait(15.0)
        except BaseException:
            traceback.print_exc()

    if not state.get("drawn"):
        return False, None
    return True, dismissed


def _kivy_overlay(text, title):
    """Fallback error screen inside Kivy. Blocks until the window is closed.

    Only usable when the Kivy event loop is NOT already running (i.e. from
    the entry script's own exception path).
    """
    from kivy.app import App
    from kivy.metrics import dp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView

    class _ErrorApp(App):
        def build(self):
            root = BoxLayout(orientation="vertical", padding=(dp(14), dp(14)))
            head = Label(text=title, color=(1.0, 0.71, 0.19, 1.0),
                         size_hint=(1, None), height=dp(46),
                         font_size="20sp", bold=True)
            body = Label(text=text, color=(0.9, 0.92, 0.95, 1.0),
                         size_hint_y=None, halign="left", valign="top",
                         font_size="13sp")
            body.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)))
            body.bind(texture_size=lambda inst, size: setattr(inst, "height",
                                                              size[1]))
            scroller = ScrollView()
            scroller.add_widget(body)
            root.add_widget(head)
            root.add_widget(scroller)
            return root

    _ErrorApp().run()
    return True


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def fatal(detail="", exc_text=None, on_ui_thread=False,
          allow_kivy_fallback=True, title="ODU TRACKER — STARTUP ERROR"):
    """Show a blocking on-device error screen and persist the report.

    Returns True when an error screen is up (process stays alive until the
    user taps CLOSE APP, which exits it), False when nothing could be drawn
    (report is then only in logcat + odu-crash.txt).
    """
    global _fatal_seen
    with _lock:
        first = not _fatal_seen
        _fatal_seen = True

    if exc_text is None:
        exc_text = traceback.format_exc()
    heading = title if first else title + "  (already reported)"

    try:
        report = compose(heading, detail, exc_text)
    except BaseException:
        report = f"{heading}\nlast stage: {STAGE}\n\n{exc_text}"

    print(report, flush=True)                       # → androidembed → logcat
    written = _write_report(report)
    if written:
        print("[diag] report saved: " + ", ".join(written), flush=True)

    if not first:
        return False                                # an error screen is up
    if not is_android():
        return False                                # desktop: log only

    drawn, dismissed = native_overlay(
        report, heading, "CLOSE APP",
        on_pressed=lambda: os._exit(0),
        on_ui_thread=on_ui_thread,
    )
    if drawn:
        if not on_ui_thread and dismissed is not None:
            dismissed.wait()                        # keep the process alive
        return True

    if allow_kivy_fallback and not on_ui_thread:
        try:
            return bool(_kivy_overlay(report, heading))
        except BaseException:
            traceback.print_exc()
    return False


def fatal_exc(detail="", **kw):
    """``fatal()`` using the exception currently being handled."""
    return fatal(detail=detail, exc_text=traceback.format_exc(), **kw)


def report_premature_exit():
    """App.run() returned without an exception — boot did not complete."""
    if not is_android() or _fatal_seen:
        return
    if _reached_dashboard:
        return                                # normal close after a live boot
    return fatal(detail=("The app's UI loop ended before the dashboard came up "
                         "(no exception — the Kivy window closed or never "
                         "opened)."),
                 exc_text="no Python exception: App.run() returned cleanly")


def show_pending_report():
    """Replay the previous run's crash report once at startup (blocks)."""
    if not is_android():
        return False
    text = _take_pending()
    if not text:
        return False
    title = "PREVIOUS RUN CRASHED — last report"
    drawn, dismissed = native_overlay(
        text, title, "CONTINUE",
        on_pressed=None,
    )
    if drawn and dismissed is not None:
        dismissed.wait()                            # user taps CONTINUE
        return True
    try:
        return bool(_kivy_overlay(text, title))
    except BaseException:
        print(text, flush=True)
        return False


def install_hooks():
    """Route stray exceptions (main thread + background threads) to fatal()."""
    def _unhandled(exc_type, exc_value, exc_tb):
        fatal(exc_text="".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)))

    def _thread_unhandled(args):
        fatal(exc_text="".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)),
            detail=f"background thread {getattr(args.thread, 'name', '?')!r} "
                   "died with an unhandled exception",
            allow_kivy_fallback=False)

    sys.excepthook = _unhandled
    threading.excepthook = _thread_unhandled
