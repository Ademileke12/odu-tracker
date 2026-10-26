"""Headless ODU Tracker server — no window, no browser tab.

Used as the payload of the dedicated Electron desktop shell
(packaging/electron/) but works standalone too:

    python -m odu_tracker.headless --port 8770

Behaviour:
  * binds 127.0.0.1 only (the dashboard is a local surface; the app itself
    reaches the router over the LAN)
  * the port must be free exactly as requested — if not, exits with code 3
    so a shell (Electron) can retry with the next port itself
  * logs to the usual rotating file in the data dir (frozen builds have
    no console to print to)
"""
import argparse
import logging
import socket
import sys


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser("odu-tracker-headless")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args(argv)

    from odu_tracker import APP_NAME
    from odu_tracker.launcher import _setup_logging
    _setup_logging()
    log = logging.getLogger("odutracker.headless")

    if not _port_free(args.port):
        print(f"PORT_BUSY:{args.port}", flush=True)
        log.error("port %s busy — refusing to double-poll the router", args.port)
        return 3

    from odu_tracker.config import Config
    from odu_tracker.store import init as init_db
    from odu_tracker.poller import Poller
    from odu_tracker.web.app import create_app
    from waitress import serve as wserve

    init_db()
    cfg = Config()
    poller = Poller(cfg)
    app = create_app(cfg, poller)
    poller.start()

    url = f"http://{args.bind}:{args.port}"
    print(f"{APP_NAME} engine ready: {url}", flush=True)
    log.info("headless server on %s", url)

    try:
        wserve(app, host=args.bind, port=args.port, threads=8)
    except KeyboardInterrupt:
        log.info("headless server stopped (ctrl-c)")
    except OSError as exc:
        log.error("server failed on %s: %s", url, exc)
        print(f"SERVER_ERROR:{exc}", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
