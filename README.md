# ⚡ ODU Tracker

A cross-platform (Linux / Windows) companion app for the **MTN Broadband 5G ODU**
(`ZLT X28`-class router, admin panel at `http://192.168.0.1/`).

It logs into the router's *undocumented* web API (reverse-engineered from its
Vue SPA) and turns it into a real monitoring tool:

| Feature | Source |
|---|---|
| 📊 **Data-usage stats** — month total/↓/↑, today, hourly charts, plan meter | `cmd 337` flow counters (MB → GB) |
| 📶 **Live throughput** — current + 10 min + 1 h **average speed** derived from router counters (zero data cost) | counter deltas per poll |
| 🚀 **Speed test** — on-demand internet speed test (down/up/ping) with history + averages, size-selectable to protect your data plan | Cloudflare speed endpoints |
| 📱 **Connected devices** — name, IP, MAC, vendor, WiFi/LAN, online state | `cmd 223` DHCP list (+ `208`/`3`) |
| 🆕 **New-device detection** — first-seen alerting with history | SQLite presence tracking |
| 🚨 **Dangerous devices** — live risk scoring (unknown vendor, random MAC, flagged hostname, previously blocked…) | heuristic engine |
| 🔒 **Block/unblock a device** — pushes to the router MAC filter | `cmd 23` + `cmd 20` |
| 🌐 **Websites** — optional local DNS logging proxy (per-device browsing timeline) + router URL block list | `cmd 26` + local DNS proxy |
| 🔋 **Battery / signal / network status** (the ODU has an internal battery) | `cmd 113`/`cmd 1005`/`cmd 218` |
| 🕓 **Event + alert history** — joins, leaves, IP changes, block actions | full local timeline |

Everything is stored **locally** (SQLite). The admin password never leaves your PC.

---

## ⚠️ Read first — login attempts

The firmware only allows **3 wrong login attempts** before locking the
web login for ~26 hours. `run.py` and the dashboard always use *one* clean
attempt with the password you type — do **not** repeatedly test wrong passwords.

---

## Quick start (Linux)

```bash
cd odu-tracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py serve              # dashboard at http://127.0.0.1:8770
```

Open **http://localhost:8770**, type your ODU admin password once → the app
logs in, starts polling, and the dashboard fills up.

First run password is stored (obfuscated) in `~/.local/share/odu-tracker/config.json`.

### CLI

```bash
.venv/bin/python run.py login               # validate credentials only
.venv/bin/python run.py probe --fast        # dump the raw JSON of every endpoint
.venv/bin/python run.py devices             # print device lists as JSON
```

### Run as a service (optional, Linux)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/odu-tracker.service <<EOF
[Unit]
Description=ODU Tracker daemon
[Service]
ExecStart=%h/odu-tracker/.venv/bin/python %h/odu-tracker/run.py serve --port 8770
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now odu-tracker
journalctl --user -u odu-tracker -f
```

## Quick start (Windows)

```powershell
cd odu-tracker
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py serve
```

Data is stored in `%APPDATA%\OduTracker`. For a launcher, create
`odu-tracker.bat`:

```bat
@echo off
cd /d %~dp0
start "" http://localhost:8770
.venv\Scripts\python run.py serve --port 8770
```

---

## The dashboard

Tabs:

* **Dashboard** — usage totals + today + plan meter, signal (`5G(NSA)`,
  RSRP, operator), live link throughput, ODU health (uptime/CPU/firmware),
  WAN/SSID info, online-device cards, recent activity feed.
* **Devices** — every device ever seen: status dot, name, IP, MAC, vendor,
  interface, age, **risk badge with reasons**, trust/block buttons.
* **Traffic** — hourly bar chart (24 h), live throughput + **average speeds**
  (from router counters, costs no data), **speed test runner** with
  down/up/ping results, history and averages, usage breakdown.
* **Websites** — per-device browsing timeline via the DNS proxy, URL block
  list from the router, top browsing clients.
* **Alerts** — security + activity alerts (new/join/leave/IP change/danger).
* **Settings** — change stored password, run the DNS proxy, inspect raw
  router JSON (`fast` / `slow` / `url_filter`).

## "Visited websites" — how it works

Router firmware does **not** expose a browsing history; that is normal for
this hardware class. Two sources instead:

1. **Router URL block list** (always): `Websites → Router URL block list`.
2. **Local DNS proxy** (optional, per-device): enable in *Settings*, then set
   the **router DHCP DNS to this PC's LAN IP** (shown in the settings card).
   Every DNS lookup of every LAN client is then logged locally and shown as
   per-device domain timeline. To route *all* clients, your PC must stay on.

Risk scoring details (no network calls): unknown MAC vendor prefix, randomized
MACs, suspicious hostnames (`kali`, `nmap`, hex-blobs…), first-time join,
previous blocks, wireless joins. Levels: `low / medium / high`.

---

## API map (reverse-engineered)

`POST /cgi-bin/http.cgi` with JSON `{cmd, method, sessionId[, token]}`.

| cmd | meaning | notes |
|---|---|---|
| 232 | login handshake | returns pre-login token + lock state |
| 100 | login | `passwd = sha256(token + password)`, username always `admin` |
| 233 | keep-alive / token refresh | called after every successful POST |
| 101 | logout | |
| 402 | home info list | includes `dhcp_list_info` |
| 223 | all devices (`dhcp_list_info`) | |
| 208 / 3 | DHCP info / DHCP range | |
| 337 / 355 | data-usage totals & plan | `sys_used_flow`, `usr_total_flow` |
| 113 / 218 / 1005 / 411 | sys status / network / battery / power | |
| 315 | mesh topology | `topology information` tree |
| 26 / 29 | URL filter list / mode | |
| 23 / 20 / 28 | MAC filter write / apply / mode | used by **block device** |
| 17 / 236 / 241 | system log / module log / export | cached raw |

Login-lock and password hashing were validated live against the device.

## Project layout

```
odu-tracker/
├── run.py                    # CLI entry (serve / probe / devices / login)
├── odu_tracker/
│   ├── config.py             # settings store (~/.local/share/odu-tracker)│   ├── router.py             # ZLT X28 API client (login + 60 cmds)
│   ├── store.py              # SQLite persistence layer
│   ├── security.py           # risk scoring + OUI vendor tags
│   ├── poller.py             # fast/slow poll loops + event engine
│   ├── dns_proxy.py          # optional DNS logging proxy
│   └── web/                  # Flask dashboard (single-file UI)
└── tools/test_flow.py        # synthetic end-to-end tests (routerless)
```

## Troubleshooting

* *Dashboard login says wrong password* — the password is what you use at
  `http://192.168.0.1`. Remember: 1 failed try = 1 of 3 attempts consumed.
* *Login locked* — wait the lockout (~26 h) or reboot the ODU (often clears it).
* *No devices appear* — Settings → ensure `router_ip` matches your gateway;
  some builds expose the device list only under `home_info` (auto-handled).
* *Usage chart empty* — flows need a few minutes of sampling to build deltas.

## What's new — UI v3

- **Persistent sign-in**: the dashboard session survives refreshes and reboots
  (30-day cookie, "Keep me signed in" on the login screen).
- **Light & dark themes** with a polished split-screen login, custom ODU logo
  icon, Plus Jakarta Sans / Space Grotesk / JetBrains Mono typography, and
  generous spacing.
- **Charts your way**: usage and throughput cards have Bar / Line / Area /
  Donut switchers (each choice is remembered per browser).
- **Per-device data ranking**: ranked by DNS lookups through the app
  (this firmware exposes no per-device byte counters — a router-provided
  byte field appears automatically if the ODU starts filling it).
- **Real "visited websites"**: Settings → Website log — one click
  *Connect router DNS → this PC* re-programs the ODU's DHCP-DNS (cmd 3 POST)
  to hand out this app's IP, with 8.8.8.8 as fallback; *Restore router
  default* puts back exactly what was configured before (saved on first
  change). Devices pick new DNS on lease renewal (toggle Wi-Fi to refresh).
- **Reset usage from now** now clears the router's own statistics through the
  firmware path (`cmd 337 POST statisticsReset=1`) and keeps a local baseline
  as fallback.
- DNS proxy hardened: per-query worker threads, all record types forwarded
  (only A/AAAA logged), reverse/local zones filtered.

## Desktop app (Linux & Windows)

Dev mode already behaves like an app:

    python run.py app          # native window (pywebview if installed)
    python run.py serve --open # console + browser
    python run.py autostart    # start on login (installs removal: --remove)

Packaged builds:

    pip install -r requirements.txt
    python tools/make_icon.py            # creates assets/icon.ico (Pillow)
    python tools/build_app.py            # → dist/ODU-Tracker/  (launcher dir)
    python tools/build_app.py --onefile  # → dist/ODU-Tracker.exe

The frozen app opens its own window (pywebview → Edge/Chrome "--app" profile →
browser fallback), logs to ~/.local/share/odu-tracker/odu-tracker.log, and
auto-bumps the port if 8770 is busy. Run EITHER the packaged app OR the
console server (`run.py serve`) — not both at once, so the router isn't
polled twice.

## Co-existence with the router admin page (http://192.168.0.1)

- The tracker talks to the same login API the vendor UI uses, with its own
  session; it never changes the admin password unless you explicitly ask.
- The dashboard Website-log feature points the router's **DHCP-DNS** at this
  PC — that only re-routes client DNS, not the admin panel, which is opened
  by IP and stays fully reachable while this app runs (and DNS falls back to
  8.8.8.8 when this PC is offline, so the LAN never goes blind).
- Usage tracking is read-only (cmd 337 counters); a full statistics-reset
  only happens when you press Reset in this app or clear it in the admin UI.
- The router's wrong-attempt counter is precious: the app stores your
  password locally and silently re-logs in as needed — never type a wrong
  password anywhere.
