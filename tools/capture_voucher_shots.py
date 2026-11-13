#!/usr/bin/env python3
"""Capture the REAL voucher UI for the landing page (site/assets).

Runs the actual engine on an isolated data dir with no router, injects
fake auth + a fake "ODU reachable" answer (so the dashboards render),
seeds demo voucher data, and screenshots the phone + desktop views with
headless Chrome. Each capture runs in its own process so the engine's
cached data-dir never leaks between shots.

    .venv/bin/python tools/capture_voucher_shots.py

Produces:
    site/assets/voucher-paywall.png          phone, locked tab
    site/assets/voucher-unlocked.png         phone, unlocked tab
    site/assets/voucher-paywall-desktop.png  desktop, locked tab
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "site" / "assets"
CHROME = shutil.which("google-chrome") or shutil.which("chromium")

SHOTS = [
    # label, unlocked, url path, w, h, output name
    ("phone locked",   False, "/mobile?tab=vch", 520, 1126,
     "voucher-paywall.png"),
    ("phone unlocked", True,  "/mobile?tab=vch", 520, 1126,
     "voucher-unlocked.png"),
    ("desktop locked", False, "/?tab=vouchers",  1600, 1000,
     "voucher-paywall-desktop.png"),
]


def serve(port: int, unlocked: bool) -> None:
    """Child process: boot the real engine and serve until killed."""
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from odu_tracker import store, voucher
    from odu_tracker.config import Config
    from odu_tracker.poller import Poller
    from odu_tracker.web.app import create_app

    store.init()
    cfg = Config()
    if unlocked:
        voucher.init()      # voucher tables live here, not in store.init()
        now = store.now()
        rows = [("guest pool", 20480, now - 3600 * 26, True),
                ("", 10240, now - 3600 * 20, False),
                ("family", 51200, now - 3600 * 8, False)]
        for note, mb, created, claimed in rows:
            voucher.generate(1, mb, note)   # canonical insert path
            real = store.q("SELECT code FROM vouchers ORDER BY rowid DESC "
                           "LIMIT 1", fetch="one")["code"]
            store.exec("UPDATE vouchers SET note=?, created_at=? "
                       "WHERE code=?", (note, created, real))
            if claimed:
                store.exec("INSERT INTO voucher_claims(code, mac, ip, "
                           "claimed_at) VALUES(?,?,?,?)",
                           (real, "AA:BB:CC:11:22:33", "192.168.0.101",
                            created + 600))
        for mac, ip, host, seen in (
                ("AA:BB:CC:11:22:33", "192.168.0.101", "Ade-phone", now - 900),
                ("AA:BB:CC:44:55:66", "192.168.0.102", "Laptop", now - 300)):
            store.exec("INSERT OR IGNORE INTO devices(mac, first_seen, "
                       "last_seen, last_ip, last_hostname) VALUES(?,?,?,?,?)",
                       (mac, seen, seen, ip, host))
        # a paid licence so the feature renders unlocked
        from odu_tracker import license as lic
        cfg.set("voucher_license",
                {"code": lic.expected_unlock_code(), "kind": "paid",
                 "device": lic.device_code(), "at": now})
        cfg.set("setup_done", True)

    app = create_app(cfg, Poller(cfg))

    @app.before_request
    def _fake_env():
        """Signed in + a fake 'ODU on this network' answer so both UIs
        render their dashboards without real hardware."""
        from flask import jsonify, request, session
        session["auth"] = True
        if request.path == "/api/meta":
            return jsonify({"setup_done": True, "authed": True,
                            "router_ip": "192.168.0.1", "poll_running": True,
                            "router_logged_in": True, "reachable": True,
                            "odu": True,
                            "version": __import__("odu_tracker").__version__,
                            "attempts_left": None, "lock_secs": None})
        if request.path == "/api/check":
            return jsonify({"reachable": True, "odu": True,
                            "ip": "192.168.0.1"})
        return None

    from werkzeug.serving import make_server
    make_server("127.0.0.1", port, app, threaded=True).serve_forever()


def wait_ready(port: int, timeout: float = 30.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/license", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("engine did not come up")


def shot(url: str, out: Path, w: int, h: int) -> None:
    tmp = out.with_suffix(".raw.png")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--window-size=%d,%d" % (w, h),
                    "--virtual-time-budget=12000", "--screenshot=%s" % tmp,
                    url], check=True, capture_output=True, timeout=180)
    from PIL import Image
    img = Image.open(tmp)
    img = img.resize((w, h), Image.LANCZOS)   # 2x capture -> display size
    img.save(out, optimize=True)
    tmp.unlink()
    print(f"  {out.name}  {out.stat().st_size // 1024} KB  ({w}x{h})")


def main() -> None:
    if not CHROME:
        raise SystemExit("google-chrome / chromium not found")
    ASSETS.mkdir(parents=True, exist_ok=True)
    for label, unlocked, path, w, h, name in SHOTS:
        print(f"capturing {label} …")
        port, data = _free_port(), Path(tempfile.mkdtemp(prefix="odu-shot-"))
        env = dict(os.environ, XDG_DATA_HOME=str(data))
        child = subprocess.Popen(
            [sys.executable, __file__, "--serve", str(port),
             "unlocked" if unlocked else "locked"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready(port)
            shot(f"http://127.0.0.1:{port}{path}", ASSETS / name, w, h)
        finally:
            child.terminate()
            child.wait(timeout=10)
            shutil.rmtree(data, ignore_errors=True)
    print("done →", ASSETS)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), sys.argv[3] == "unlocked")
    else:
        main()
