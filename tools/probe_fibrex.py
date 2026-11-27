#!/usr/bin/env python3
"""Fingerprint an MTN FibreX (ZTE ZXHN class) gateway for ODU-Tracker.

WHY THIS EXISTS
---------------
The FibreX gateway does NOT speak the ZLT cgi-bin protocol OduRouter was
reverse-engineered from. ODU-Tracker ships a second driver
(odu_tracker/router_zte.py) for ZTE's "LuQUID" web framework, but every
firmware revision renames its pages slightly - so before we trust the
driver we capture what ONE real device actually serves.

This script is for the person holding the router: it needs no knowledge of
the codebase, it writes one JSON file, and it changes NOTHING on the
router (see SAFETY).

SAFETY
------
* Read-only apart from ONE login POST (the router will not answer data
  pages without a session). The login POST carries only the hashed
  password the firmware itself asks for - it is not a settings write.
* No settings are ever written: no GET/POST to apply/save/set endpoints.
* Login attempts are capped at 2 (the firmware counts wrong passwords
  towards its web-login lockout). Run it with the RIGHT password.
* Nothing leaves the machine: the report is a local JSON file the user
  can read before sending it anywhere.

USAGE
-----
  python3 tools/probe_fibrex.py                      # discover + fingerprint
  python3 tools/probe_fibrex.py --password 'admin'   # also attempt login
  python3 tools/probe_fibrex.py --ip 192.168.1.1
  python3 tools/probe_fibrex.py --out fibrex_report.json

The report is safe to share: it contains router page names, element ids
and (if --password was used) whether the login succeeded - NOT the
password itself.
"""
import argparse
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odu_tracker.router import RouterError            # noqa: E402
from odu_tracker.router_zte import (                  # noqa: E402
    PAGE_URL, ROLE_MARKERS, SEED_PAGES, ZteRouter, _parse_page,
)

# Candidate admin addresses: MTN documents 192.168.x.1 / mtn.broadband.ng,
# ZTE GPON ONTs ship 192.168.1.1, and the older ODU used 192.168.0.1.
CANDIDATE_IPS = (
    "192.168.1.1",
    "192.168.0.1",
    "192.168.100.1",
    "192.168.2.1",
    "192.168.1.254",
    "10.0.0.1",
)


def default_gateway() -> str:
    """This machine's default gateway (Linux /proc route table)."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                raw = f[2]
                return ".".join(str(int(raw[i:i + 2], 16))
                                for i in (6, 4, 2, 0))
    except Exception:
        pass
    return ""


def tcp_open(ip, port=80, timeout=1.5) -> bool:
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def http_get(url, timeout=6, _hops=0):
    """-> (status, text, headers) without raising on HTTP errors.

    Follows redirects and accepts the self-signed certificates LAN gear
    ships with (ZTE gateways commonly answer http:// with 301 -> https://
    and a self-signed cert; refusing it would blind the probe).
    """
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; ODU-Tracker-Probe/1.5)")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.getcode(), r.read().decode("utf-8", "ignore"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        loc = (e.headers or {}).get("Location", "")
        if e.code in (301, 302, 303, 307, 308) and loc and _hops < 4:
            nxt = urllib.parse.urljoin(url, loc)
            return http_get(nxt, timeout=timeout, _hops=_hops + 1)
        return e.code, body, dict(e.headers or {})
    except Exception as exc:
        return 0, f"__ERROR__ {type(exc).__name__}: {exc}", {}


# --------------------------------------------------------------- reporting
def summarize_page(url, html_text):
    """What one page gives the driver: role markers, ids, data endpoints."""
    scan = _parse_page(html_text)
    found_roles = [role for role, marks in ROLE_MARKERS.items()
                   if any(m in html_text for m in marks)]
    ids = sorted({sid for _b, sid, _t in scan.spans if sid})[:400]
    # <span class="form-action"> endpoints (page -> data binding)
    eps = sorted(set(re.findall(
        r'<span[^>]*class="form-action"[^>]*>([^<]+)</span>', html_text)))
    nexts = sorted(set(re.findall(r'nextpage=([A-Za-z0-9_.]+)', html_text)))
    # template blocks the dashboard renders one device per block into
    blocks = sorted({m for m in re.findall(r'id="(template_[A-Za-z0-9_]+)"',
                                           html_text)})
    title = re.search(r"<title[^>]*>([^<]*)</title>", html_text, re.I)
    return {
        "url": url,
        "title": (title.group(1).strip() if title else "")[:80],
        "bytes": len(html_text),
        "roles": found_roles,
        "form_actions": eps[:40],
        "nextpages": nexts[:60],
        "template_blocks": blocks[:60],
        "span_ids": ids,
        "rows": len(scan.rows),
        "has_session_token": "_sessionTOKEN" in html_text,
        "has_luquid_login": bool(re.search(r"logintoken_lua|Frm_Username|"
                                           r"frmLogin|login_sec\.gch",
                                           html_text)),
    }


def login_form_dump(html_text):
    """Extract the login form's action + field names verbatim."""
    forms = re.findall(r"<form[^>]*>", html_text, re.I)
    fields = re.findall(r"<(?:input|select|textarea)[^>]*>", html_text, re.I)
    interesting = [f for f in fields
                   if re.search(r"password|user|token|action|captcha|rand",
                                f, re.I)]
    scripts = sorted(set(re.findall(r'<script[^>]*src="([^"]+)"', html_text,
                                    re.I)))
    t = re.search(r"<title[^>]*>([^<]*)</title>", html_text, re.I)
    return {
        "forms": forms[:10],
        "fields": interesting[:40],
        "scripts": [s for s in scripts
                    if not s.endswith((".png", ".gif"))][:40],
        "title": t.group(1).strip()[:80] if t else "",
        "has_session_token": "_sessionTOKEN" in html_text,
        "has_form_action_spans": "form-action" in html_text,
    }


# -------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fingerprint an MTN FibreX / ZTE gateway (read-only)")
    ap.add_argument("--ip", default=None,
                    help="router IP (default: auto-discover)")
    ap.add_argument("--password", default=None,
                    help="also attempt a login and crawl logged-in pages "
                         "(the firmware locks after repeated bad tries - "
                         "only pass the REAL password)")
    ap.add_argument("--pages", type=int, default=60,
                    help="max pages to crawl after login (default 60)")
    ap.add_argument("--out", default="fibrex_report.json",
                    help="report output path")
    ap.add_argument("--no-crawl", action="store_true",
                    help="skip the post-login page crawl")
    args = ap.parse_args()

    report = {
        "tool": "probe_fibrex",
        "version": 1,
        "notes": [],
        "discovery": {},
        "root": {},
        "login": {},
        "pages": [],
    }

    # ---------------------------------------------------------- discovery
    candidates = []
    gw = default_gateway()
    if gw:
        candidates.append(gw)
    if args.ip:
        candidates.insert(0, args.ip)
    for ip in CANDIDATE_IPS:
        if ip not in candidates:
            candidates.append(ip)

    open_ips = [ip for ip in candidates if tcp_open(ip)]
    report["discovery"] = {
        "default_gateway": gw,
        "tried": candidates,
        "open": open_ips,
    }
    if not open_ips:
        report["notes"].append(
            "no HTTP port found on any candidate - is this machine ON the "
            "router's LAN (Wi-Fi/Ethernet, not a phone hotspot)?")
        Path(args.out).write_text(json.dumps(report, indent=2),
                                  encoding="utf-8")
        print(f"no router reachable; report written to {args.out}")
        return 1

    # prefer a candidate that actually serves an HTML page (several LAN
    # addresses can be "open" - VPN/captive boxes answer too)
    ip = open_ips[0]
    root_code, root, headers = 0, "", {}
    for cand in open_ips:
        c, body, h = http_get(f"http://{cand}/")
        if c and not body.startswith("__ERROR__") and len(body) > 400:
            ip, root_code, root, headers = cand, c, body, h
            break
        if c:
            ip, root_code, root, headers = cand, c, body, h
    if len(open_ips) > 1:
        report["notes"].append(f"several candidates open, using {ip}")
    print(f"router candidate: {ip}")

    # -------------------------------------------------------- root page
    if not root:
        root_code, root, headers = http_get(f"http://{ip}/")
    report["root"] = {
        "url": f"http://{ip}/",
        "status": root_code,
        "server": headers.get("Server", ""),
        "headers": {k: v for k, v in headers.items()
                    if k.lower() in ("server", "www-authenticate",
                                     "set-cookie", "location", "x-frame-options")},
        "bytes": len(root),
        "summary": summarize_page(f"http://{ip}/", root),
        "login_form": login_form_dump(root),
    }
    print(f"  GET / -> {root_code}, {len(root)} bytes, "
          f"sessionTOKEN={'yes' if '_sessionTOKEN' in root else 'no'}")

    # ------------------------------------------------- login + crawl
    router = ZteRouter(ip=ip, timeout=8)
    if args.password is None:
        report["login"]["attempted"] = False
        report["notes"].append("no --password given: login page captured, "
                               "logged-in pages NOT crawled. Re-run with "
                               "--password '<admin password>' for the full "
                               "report.")
    else:
        report["login"]["attempted"] = True
        report["login"]["password_supplied"] = True   # value never recorded
        try:
            router.login(args.password)
            report["login"]["ok"] = True
            print("  login OK - crawling pages ...")
        except RouterError as exc:
            report["login"]["ok"] = False
            report["login"]["error"] = str(exc)
            print(f"  LOGIN FAILED: {exc}", file=sys.stderr)
            Path(args.out).write_text(json.dumps(report, indent=2),
                                      encoding="utf-8")
            print(f"report written to {args.out}")
            return 1

        # seed the same page set the driver uses, then follow menu links
        urls = list(dict.fromkeys(
            [PAGE_URL.format(page=p) for p in SEED_PAGES] + ["/"]))
        seen = set()
        while urls and len(report["pages"]) < args.pages:
            url = urls.pop(0)
            if url in seen:
                continue
            seen.add(url)
            full = url if url.startswith("http") else f"http://{ip}{url}"
            c, body, _h = http_get(full)
            if c != 200 or not body or body.startswith("__ERROR__"):
                continue
            info = summarize_page(full, body)
            if not info["roles"] and not info["form_actions"] \
                    and not info["template_blocks"] and info["bytes"] < 2000:
                continue                       # framework shell, nothing in it
            report["pages"].append(info)
            print(f"    {info['roles'] or '-':<22} {url[:70]}")
            for nxt in info["nextpages"]:
                u = PAGE_URL.format(page=nxt)
                if u not in seen:
                    urls.append(u)

        # what the driver would classify, from what we actually saw
        roles_seen = {}
        for p in report["pages"]:
            for r in p["roles"]:
                roles_seen.setdefault(r, p["url"])
        report["login"]["roles_found"] = roles_seen
        report["login"]["driver_would_use"] = {
            "all_device_pages": [p["url"] for p in report["pages"]
                                 if p["template_blocks"]],
            "data_endpoints": sorted({e for p in report["pages"]
                                      for e in p["form_actions"]})[:80],
        }
        router.logout()

    # ------------------------------------------------------------- verdict
    ids = {i for p in report["pages"] for i in p["span_ids"]}
    report["verdict"] = {
        "recognized_luquid": bool(report["root"]["summary"]["has_session_token"]),
        "device_ids_present": sorted(
            i for i in ids if re.search(
                r"MACAddress|IPAddress|HostName|InBytes|TotalBytesCount|"
                r"OnlineDuration|Rssi", i))[:120],
        "template_device_blocks": sorted(
            {b for p in report["pages"] for b in p["template_blocks"]})[:60],
        "covers": sorted({r for p in report["pages"] for r in p["roles"]}),
    }

    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nreport written to {args.out} "
          f"({len(report['pages'])} pages, "
          f"{len(report['verdict']['device_ids_present'])} device field ids)")
    print("share that file to finish FibreX support")
    return 0


if __name__ == "__main__":
    sys.exit(main())
