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
    url = f"http://0.0.0.0:{args.port}"
    print(f"\n  {APP_NAME} dashboard running: http://localhost:{args.port}\n")
    wserve(app, host=args.bind, port=args.port, threads=8)


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
    ap_probe = sub.add_parser("probe", help="dump raw router JSON")
    ap_probe.add_argument("--fast", action="store_true")
    sub.add_parser("devices", help="list devices as JSON")
    sub.add_parser("login", help="test router authentication")
    args = ap.parse_args()
    init_db()
    {"serve": cmd_serve, "probe": cmd_probe, "devices": cmd_devices, "login": cmd_login}[args.cmd](args)


if __name__ == "__main__":
    main()
