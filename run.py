#!/usr/bin/env python3
"""ODU Tracker entrypoint.

Commands:
  python run.py serve [--port 8770]        start dashboard + poller
  python run.py probe                      login (prompt) and dump raw JSONs
  python run.py devices                    print devices as JSON
  python run.py login                      test router auth only
"""
import argparse
import json
import os
import sys
import threading

from odu_tracker import APP_NAME
from odu_tracker.config import Config
from odu_tracker.store import init as init_db


def build_default(cfg=None):
    cfg = cfg or Config()
    init_db()
    from odu_tracker.poller import Poller
    p = Poller(cfg)
    return cfg, p


def cmd_serve(args):
    from odu_tracker.web.app import create_app
    from waitress import serve as wserve

    cfg, poller = build_default()
    app = create_app(cfg, poller)
    poller.start()
    if getattr(args, "open", False):
        threading.Timer(1.5, lambda: _open_browser(args.port)).start()
    url = f"http://{args.bind}:{args.port}"
    print(f"\n  {APP_NAME} dashboard running: http://localhost:{args.port}\n")
    wserve(app, host=args.bind, port=args.port, threads=8)


def _open_browser(port):
    import webbrowser
    webbrowser.open(f"http://localhost:{port}")


def cmd_app(args):
    """Desktop app mode: server in background thread + native window."""
    from odu_tracker.launcher import desktop_main
    desktop_main(port=args.port)


def _startup_parts():
    """(exe, script_or_None) used for autostart entries (frozen or dev)."""
    import os
    import sys
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if sys.platform == "win32" and exe.lower().endswith("python.exe"):
            pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pw):
                exe = pw
        return exe, None
    exe = sys.executable
    if sys.platform == "win32":
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pw):
            exe = pw
    return exe, os.path.join(os.getcwd(), "run.py")


def _startup_target():
    """Command line for autostarting this program (cmd-file quoting)."""
    import os
    exe, script = _startup_parts()
    if script:
        return f'"{exe}" "{script}" app'
    return f'"{exe}" app'


AUTOSTART_LNX = os.path.expanduser("~/.config/autostart/odu-tracker.desktop")
def _autostart_win():
    import os
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu",
                           "Programs", "Startup")
    return os.path.join(startup, "ODU Tracker (autostart).cmd")


def cmd_autostart(args):
    """Install or remove the auto-start-on-login shortcut."""
    import os
    win = sys.platform == "win32"
    target = _autostart_win() if win else AUTOSTART_LNX
    if args.remove:
        if os.path.exists(target):
            os.remove(target)
            print("autostart removed:", target)
        else:
            print("nothing to remove")
        return
    if win:
        with open(target, "w", encoding="utf-8-sig") as f:
            f.write(f"@echo off\r\nstart \"\" {_startup_target()}\r\n")
    else:
        exe, script = _startup_parts()
        exec_line = f"Exec='{exe}' '{script}' app --port 8770" if script \
            else f"Exec='{exe}' --port 8770"
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=ODU Tracker\n"
                    f"{exec_line}\n"
                    "X-GNOME-Autostart-enabled=true\nHidden=false\n"
                    "Comment=MTN ODU data & device tracker\n")
    print("autostart installed:", target)


def _login_or_prompt(cfg):
    from odu_tracker.router import OduRouter, RouterError
    while True:
        pw = cfg.password
        if not pw:
            import getpass
            pw = getpass.getpass(f"Router admin password ({cfg.get('router_ip')}): ")
        r = OduRouter(ip=cfg.get("router_ip"), username=cfg.get("router_username") or "admin", password=pw)
        try:
            r.login(pw)
            if not cfg.has_password:
                cfg.password = pw
            return r
        except RouterError as exc:
            print(f"  !! {exc}")
            pw = None
            retry = input("  try again? [Y/n] ").strip().lower()
            if retry == "n":
                sys.exit(2)


def cmd_probe(args):
    cfg, _ = build_default()
    r = _login_or_prompt(cfg)
    print("=== fast snapshot ===")
    out = r.snapshot_all()
    print(json.dumps(out, indent=2)[:24000])
    if not args.fast:
        print("\n=== slow snapshot ===")
        print(json.dumps(r.slow_snapshot(), indent=2)[:24000])
    r.logout()


def cmd_devices(args):
    cfg, _ = build_default()
    r = _login_or_prompt(cfg)
    outs = {}
    for name, cmd in (("all_device", 223), ("dhcp_info", 208), ("dhcp_data", 3)):
        try:
            outs[name] = r.cmd(cmd, "GET")
        except Exception as exc:
            outs[name] = {"error": str(exc)}
    print(json.dumps(outs, indent=2))
    r.logout()


def cmd_login(args):
    cfg, _ = build_default()
    r = _login_or_prompt(cfg)
    print("login OK; sessionId len:", len(r.session_id), "user_level:", r.user_level)


def main():
    ap = argparse.ArgumentParser("odu-tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_serve = sub.add_parser("serve", help="start dashboard + poller")
    ap_serve.add_argument("--port", type=int, default=8770)
    ap_serve.add_argument("--bind", default="127.0.0.1")
    ap_serve.add_argument("--open", action="store_true",
                          help="open the dashboard in the browser on start")
    ap_app = sub.add_parser("app", help="desktop app window")
    ap_app.add_argument("--port", type=int, default=8770)
    ap_auto = sub.add_parser("autostart", help="install/remove login autostart")
    ap_auto.add_argument("--remove", action="store_true")
    ap_probe = sub.add_parser("probe", help="dump raw router JSON")
    ap_probe.add_argument("--fast", action="store_true")
    sub.add_parser("devices", help="list devices as JSON")
    sub.add_parser("login", help="test router authentication")
    args = ap.parse_args()
    init_db()
    {"serve": cmd_serve, "probe": cmd_probe, "devices": cmd_devices,
     "login": cmd_login, "app": cmd_app, "autostart": cmd_autostart}[args.cmd](args)


if __name__ == "__main__":
    main()
