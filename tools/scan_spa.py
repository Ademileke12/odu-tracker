#!/usr/bin/env python3
"""Map the ODU's firmware capability by reading its own web UI (SPA).

The router serves a Vue admin SPA. That SPA's JavaScript is the firmware's API
manual in code: every command id and response field the firmware supports is
referenced there. This tool downloads the SPA and extracts:

  * every `cmd` numeric id it uses (the full command surface)
  * every usage/accounting-related field name (to hunt a per-device counter)
  * any feature keywords (voucher, portal, radius, quota, limit, parent...)

ZERO RISK: it only fetches static files (HTML/JS/CSS) over HTTP. No login, no
POST, no state change, no lockout. It can never trip the 3-attempt login lock
because it does not log in at all.

USAGE
-----
  python3 tools/scan_spa.py                      # http://192.168.0.1/
  python3 tools/scan_spa.py --ip 192.168.0.1
  python3 tools/scan_spa.py --dump spa_raw/      # also save every fetched file
  python3 tools/scan_spa.py --no-chunks          # core bundles only (faster)

OUTPUT
------
  stdout  capability report: command ids, usage fields, feature keywords
"""
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin

import requests

# field-name hints that would indicate per-device accounting
USAGE_FIELD = re.compile(
    r"\b(sta_\w+|station_\w+|dev_\w*flow\w*|per[_-]?dev\w*|"
    r"\w*(rx|tx)\w*_(flow|bytes|rate|packets?)\w*|"
    r"\w*_(rx|tx)\w*|\bflow_\w+\b|\busage\w*\b|\bbytes?\w*\b|"
    r"\btraf\w+\b|\bpackets?\b|\boctets?\b)", re.I)

# high-value feature keywords (voucher/hotspot/quota capability)
FEATURE = re.compile(
    r"\b(voucher|captive|portal|radius|chilli|hotspot|wifidog|quota|"
    r"prepaid|credit|billing|accounting|limit_?flow|flow_?limit|"
    r"data_?plan|parent\w*|time_?limit|mac_?filter|access_?ctrl)", re.I)

# a cmd id may appear as: cmd: 337 / cmd:337 / "cmd":337 / cmd = 337 / .cmd(337)
CMD_PAT = re.compile(
    r"""["']?cmd["']?\s*[:=]\s*["']?(\d{1,4})|\.cmd\(\s*(\d{1,4})|cmd=(\d{1,4})""")
CMD_NAME = re.compile(
    r"""["']?cmd["']?\s*[:=]\s*["']?(\d{1,4})["']?[^,}\n]{0,40}"""
    r"""["']?(?:name|type|desc)["']?\s*[:=]\s*["']([^"']+)""")


def fetch(base, path):
    url = urljoin(base, path)
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 0:
            return r.content
    except requests.RequestException:
        pass
    return None


def discover_assets(base):
    """Find SPA entry points and their JS/CSS bundles."""
    found = {}
    html = fetch(base, "/")
    if html:
        found["/index.html"] = html
        text = html.decode("utf-8", "ignore")
        # src="...js" / href="...css"
        for m in re.finditer(r"""(?:src|href)\s*=\s*["']([^"']+\.(?:js|css))["']""", text):
            found[m.group(1)] = None  # placeholder; fetched below
    # common SPA locations even if index parsing missed them
    for guess in ("/app.js", "/index.html", "/main.js", "/assets/index.js"):
        if guess not in found:
            found[guess] = None
    out = {}
    for path in found:
        data = fetch(base, path)
        if data:
            out[path] = data
    # follow any chunk imports referenced inside fetched JS (webpack/vite chunks)
    for path, data in list(out.items()):
        if not path.endswith(".js"):
            continue
        text = data.decode("utf-8", "ignore")
        for m in re.finditer(r"""["'](\.?/?[\w./-]+\.js)["']""", text):
            chunk = m.group(1)
            if chunk not in out:
                cdata = fetch(base, chunk.lstrip("/"))
                if cdata:
                    out[chunk.lstrip("/")] = cdata
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read ODU firmware capability from its SPA")
    ap.add_argument("--ip", default="192.168.0.1")
    ap.add_argument("--port", default="80")
    ap.add_argument("--dump", metavar="DIR", default=None,
                    help="also write every fetched file into DIR")
    ap.add_argument("--no-chunks", action="store_true",
                    help="skip webpack lazy-route chunks (js/chunk-*.js)")
    args = ap.parse_args()

    base = f"http://{args.ip}:{args.port}/"
    print(f"Fetching SPA from {base} (static files only, no login) ...")
    assets = discover_assets(base)
    if not assets:
        print("No SPA files reachable. Is the router IP correct and reachable?",
              file=sys.stderr)
        return 1

    # ---- lazy route chunks ---------------------------------------------
    # Vue routes load pages on demand as `n.e("chunk-XXXXXXXX")` -> js/chunk-XXXXXXXX.js
    # (filename has no ".js" inside the reference, so discover_assets misses them).
    # These chunks hold the real pages (filterRule/MAC filter, parentControl...)
    # and are the bulk of the API surface. Static GET only, like everything here.
    if not args.no_chunks:
        ids = set()
        for data in assets.values():
            ids.update(re.findall(rb"chunk-[0-9a-f]{8}", data))
        got = 0
        for cid in sorted(ids):
            path = f"js/{cid.decode('ascii')}.js"
            cdata = fetch(base, path)
            if cdata:
                assets[path] = cdata
                got += 1
        if ids:
            print(f"  -> {got}/{len(ids)} lazy route chunks fetched")

    total = 0
    for path, data in assets.items():
        print(f"  {path}  ({len(data)} bytes)")
        total += len(data)

    if args.dump:
        d = Path(args.dump)
        d.mkdir(parents=True, exist_ok=True)
        for path, data in assets.items():
            if path.startswith("js/chunk-"):
                sub = d / "chunks"
                sub.mkdir(parents=True, exist_ok=True)
                (sub / path.split("/")[-1]).write_bytes(data)
                continue
            safe = path.strip("/").replace("/", "__") or "index.html"
            (d / safe).write_bytes(data)
        print(f"  -> dumped to {d}/")

    # ---- scan -----------------------------------------------------------
    cmd_ids = defaultdict(set)      # id -> set(files)
    usage_fields = defaultdict(set)  # field -> files
    features = defaultdict(set)     # keyword -> files
    for path, data in assets.items():
        text = data.decode("utf-8", "ignore")
        for m in CMD_PAT.finditer(text):
            cid = next(g for g in m.groups() if g)
            cmd_ids[int(cid)].add(path)
        for m in USAGE_FIELD.finditer(text):
            usage_fields[m.group(0).lower()].add(path)
        for m in FEATURE.finditer(text):
            features[m.group(0).lower()].add(path)

    print("\n" + "=" * 66)
    print(f"COMMAND SURFACE: {len(cmd_ids)} distinct cmd ids referenced")
    print("  known-relevant ids marked with *")
    known = {3, 20, 21, 23, 26, 27, 28, 29, 32, 80, 113, 208, 218, 223,
             230, 315, 337, 345, 355, 385, 391, 397}
    for cid in sorted(cmd_ids):
        mark = "*" if cid in known else " "
        print(f"  {mark} {cid}")

    print("\n" + "=" * 66)
    if usage_fields:
        print(f"USAGE/ACCOUNTING FIELD NAMES ({len(usage_fields)}):")
        for f in sorted(usage_fields):
            print(f"   {f}")
        # highlight ones that look device-attached
        dev = [f for f in usage_fields
               if re.search(r"(sta|station|dev|mac|host)", f, re.I)]
        print("\n  device-attached candidates (per-device counter signal):")
        if dev:
            for f in sorted(dev):
                print(f"   -> {f}")
        else:
            print("   (none found — supports 'no per-device counter' hypothesis)")
    else:
        print("USAGE/ACCOUNTING FIELDS: none referenced in SPA")

    print("\n" + "=" * 66)
    if features:
        print("FEATURE KEYWORDS (voucher/hotspot/quota capability signal):")
        for f in sorted(features):
            print(f"   {f}")
    else:
        print("FEATURE KEYWORDS: none — no voucher/portal/quota UI in this firmware")

    print("\n" + "=" * 66)
    print("INTERPRETATION")
    print(" * device-attached usage fields  -> per-device quota likely possible")
    print(" * voucher/portal/radius keywords -> native hotspot support (rare on CPE)")
    print(" * neither                        -> on/off MAC gating only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
