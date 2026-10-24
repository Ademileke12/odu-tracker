"""Shared launcher used by run.py (dev) and the frozen desktop app.

desktop_main(): start the dashboard + poller in a background thread, then
open the desktop window. Logging always goes to a rotating file inside the
data dir when running frozen (no console).
"""
import logging
import logging.handlers
import socket
import threading
import time
import traceback


def _setup_logging():
    try:
        from odu_tracker.config import Config
        logfile = Config().path.parent / "odu-tracker.log"
        handler = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        logging.basicConfig(handlers=[handler],
                            level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except Exception:
        logging.basicConfig(level=logging.INFO)


def _free_port(start=8770):
    """First free port in [start, start+5]."""
    for p in range(start, start + 6):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            continue
        finally:
            try:
                s.close()
            except OSError:
                pass
    return start


def start_dashboard(port=8770):
    """Boot config + DB + poller + waitress in a daemon thread.

    Port is auto-bumped to the next free one when taken. Returns
    (cfg, poller, thread, final_port).
    """
    from odu_tracker.config import Config
    from odu_tracker.store import init as init_db
    from odu_tracker.poller import Poller
    from odu_tracker.web.app import create_app
    from odu_tracker import APP_NAME

    init_db()
    cfg = Config()
    poller = Poller(cfg)
    app = create_app(cfg, poller)
    poller.start()

    final = _free_port(port)
    from waitress import serve as wserve

    def run():
        try:
            wserve(app, host="127.0.0.1", port=final, threads=8)
        except Exception:
            traceback.print_exc()
            logging.exception("waitress failed")

    t = threading.Thread(target=run, daemon=True, name="waitress")
    t.start()
    print(f"  {APP_NAME} dashboard running: http://localhost:{final}")
    return cfg, poller, t, final


def desktop_main(port=8770):
    _setup_logging()
    from odu_tracker.desktop import open_window

    _cfg, _poller, _t, target = start_dashboard(port=port)
    url = f"http://127.0.0.1:{target}"
    time.sleep(1.2)                       # let waitress bind
    mode = "browser"
    try:
        mode = open_window(url)
    except Exception:
        traceback.print_exc()
        import webbrowser
        webbrowser.open(url)
    logging.info("desktop window mode=%s", mode)
    if mode != "webview":
        # Keep the tracker alive for window-less openers (app window,
        # browser tab). Closing the tab won't stop usage tracking; quit
        # via the app console or `python run.py autostart --remove`.
        threading.Event().wait()
