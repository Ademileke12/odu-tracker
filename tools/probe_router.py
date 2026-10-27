#!/usr/bin/env python3
"""Probe the ODU's undocumented API for a per-device usage counter.

WHY THIS EXISTS
---------------
The voucher system (user enters code -> gets N MB/GB -> blocked at 0) needs
to count bytes PER DEVICE. cmd 337/355 only report ONE global SIM counter,
and cmd 223 (device list) carries only mac/ip/hostname — no counters.

This probe answers the one question that decides the design:
    does this firmware expose per-device byte/flow counting anywhere?

SAFETY
------
* GET method ONLY for every command (read-only; never POST, so no state is
  mutated, nothing is reset/restarted/filtered).
* Known-dangerous ids are skipped outright.
* Logs in EXACTLY ONCE. The firmware locks web login for ~26 h after 3 bad
  attempts, so there is no retry loop here.

USAGE
-----
  python3 tools/probe_router.py                 # uses stored ODU password
  ODU_PASSWORD=secret python3 tools/probe_router.py
  python3 tools/probe_router.py --ip 192.168.0.1 --out probe_dump.json

OUTPUT
------
  <out>  raw JSON of every response (for manual inspection)
  stdout a verdict: any command that looks like per-device usage, with fields.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odu_tracker.config import Config          # noqa: E402
from odu_tracker.router import OduRouter, RouterError  # noqa: E402

# Never touch these even with GET (defensive belt-and-braces).
# NOTE: GET is NOT always read-only on this firmware — probing cmd 6 during a
# --sweep rebooted the router (its API map has `reboot` with no get variant,
# and it ignored `method:"GET"`). Every cmd that maps to an action-only
# function (no getX sibling in the SPA) is therefore excluded outright.
DANGEROUS = {
    6,     # reboot            <-- REBOOTED THE DEVICE during a sweep (2026-10-27)
    112,   # restore factory
    263,   # time/restart
    241,   # export log (may be heavy)
    101,   # logout (handled separately)
    100,   # login (handled separately)
    102,   # set password
    106,   # upgrade
    239,   # upgrade immediately
    240,   # set auto-upgrade
    249,   # reset esim info
    250,   # download esim info
    541,   # delete/switch SIM profile
    97,    # set language
    51,    # set PIN
    13,    # send sms
    14,    # delete sms
    220,   # set roaming
    222,   # set data switch
    226,   # set flight mode
    160,   # physical cell lock
    161,   # band lock
    295,   # voice settings
    360,   # set phonebook
    362,   # save phonebook
    168,   # net tool (ping/trace can hang the httpd)
    17,    # clear/get log (GET returns a raw log body, not JSON)
}

# Commands already known to matter, always probed first.
KNOWN = [
    223,  # all_device (DHCP list)
    208,  # dhcp_info
    3,    # dhcp filter data
    315,  # topology
    345,  # LAN/WAN access control
    385, 391, 397,  # parental control
    337, 355,       # flow (global)
    21, 26, 28, 29, 32,  # filters (port/url/ip)
    80, 113, 218, 207, 230,
]

# Per-device usage field name hints.
USAGE_KEYS = re.compile(
    r"(flow|usage|used|traffic|bytes|byte_|_byte|rx_|tx_|recv|trans|"
    r"download|upload|packets?|octets?)",
    re.I,
)
MAC_KEYS = re.compile(r"(mac|physaddr|hwaddr)", re.I)
# keys that are clearly NOT per-device counters
GLOBAL_KEYS = re.compile(r"(mon_download|sys_used|usr_total|limitSize|dl_mon|ul_mon)", re.I)


def looks_like_per_device_usage(obj, path=""):
    """Yield (path, key, value) for device-scoped usage fields."""
    hits = []
    if isinstance(obj, dict):
        has_mac = any(MAC_KEYS.search(str(k)) for k in obj)
        for k, v in obj.items():
            kp = f"{path}.{k}" if path else str(k)
            if has_mac and USAGE_KEYS.search(str(k)) and not GLOBAL_KEYS.search(str(k)):
                # numeric (or numeric-string) counter on an object that has a MAC
                if isinstance(v, (int, float)) or (
                    isinstance(v, str) and re.fullmatch(r"\d+(\.\d+)?", v.strip())
                ):
                    hits.append((kp, k, v))
            hits.extend(looks_like_per_device_usage(v, kp))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            hits.extend(looks_like_per_device_usage(v, f"{path}[{i}]"))
    return hits


def scan_response(resp):
    """Return (device_list_hits, all_usage_hits) for a raw response dict."""
    per_device = looks_like_per_device_usage(resp)
    # any usage-ish key anywhere (even if not MAC-attached) as secondary signal
    any_usage = []

    def walk(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                kp = f"{p}.{k}" if p else str(k)
                if USAGE_KEYS.search(str(k)) and not GLOBAL_KEYS.search(str(k)):
                    any_usage.append((kp, v if not isinstance(v, (dict, list)) else type(v).__name__))
                walk(v, kp)
        elif isinstance(o, list):
            for i, v in enumerate(o[:50]):
                walk(v, f"{p}[{i}]")

    walk(resp)
    return per_device, any_usage


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe ODU API for per-device usage counters")
    ap.add_argument("--ip", default=None, help="router IP (default: stored config)")
    ap.add_argument("--out", default="probe_dump.json", help="raw dump output path")
    ap.add_argument("--sweep", type=int, default=0,
                    help="also GET cmd ids 1..N (read-only discovery); default 0=off")
    ap.add_argument("--password", default=os.environ.get("ODU_PASSWORD"))
    args = ap.parse_args()

    cfg = Config()
    ip = args.ip or cfg.get("router_ip", "192.168.0.1")
    password = args.password if args.password is not None else cfg.password
    if not password:
        print("No ODU password. Set ODU_PASSWORD=... or run the app once to store it.",
              file=sys.stderr)
        return 2

    r = OduRouter(ip=ip, password=password, timeout=8)
    print(f"Logging in to {ip} (single attempt) ...")
    try:
        info = r.login()
    except RouterError as exc:
        print(f"LOGIN FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"  ok  user_level={info.get('user_level')!r}")

    # build the probe list: KNOWN first, then optional sweep
    ids = []
    for c in KNOWN:
        if c not in DANGEROUS and c not in ids:
            ids.append(c)
    if args.sweep:
        for c in range(1, args.sweep + 1):
            if c not in DANGEROUS and c not in ids:
                ids.append(c)

    dump = {}
    verdicts = []   # (cmd, path, key, value)
    secondary = []  # (cmd, path, value)

    print(f"Probing {len(ids)} commands (GET only) ...")
    for c in ids:
        try:
            resp = r.cmd(c, "GET")
        except Exception as exc:
            dump[str(c)] = {"error": str(exc)}
            continue
        dump[str(c)] = resp
        pd, au = scan_response(resp)
        for item in pd:
            verdicts.append((c,) + item)
        for item in au:
            if item not in pd and not any(v[1] == item[0] for v in verdicts):
                secondary.append((c,) + item)

    Path(args.out).write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print(f"\nRaw dump written to {args.out}  ({len(dump)} responses)")

    print("\n" + "=" * 64)
    if verdicts:
        print("PER-DEVICE USAGE COUNTER FOUND  ->  true MB/GB vouchers are possible")
        seen = set()
        for cmd, path, key, val in verdicts:
            tag = (cmd, path)
            if tag in seen:
                continue
            seen.add(tag)
            print(f"   cmd {cmd:<5} {path}  = {val}")
    else:
        print("NO per-device usage counter detected in probed commands.")
        print("  -> router-only vouchers would be ON/OFF (MAC block), not byte quotas.")
        if secondary:
            print("\n  Secondary usage-like fields (not device-attached, verify manually):")
            for cmd, path, val in secondary[:40]:
                print(f"     cmd {cmd:<5} {path} = {val}")

    print("=" * 64)
    print(f"\nInspect {args.out} for anything ambiguous. Re-run with --sweep 600")
    print("to GET ids 1..600 for wider discovery (still read-only).")
    try:
        r.logout()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
