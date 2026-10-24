"""Flask application: dashboard UI + JSON API.

Session handling:
  * the dashboard itself is protected by the router admin password
    (what the user types on first run validates against the real router)
  * the router client keeps its own session with the device
"""
import json
import time

from flask import Flask, jsonify, request, session, send_from_directory

from odu_tracker import store
from odu_tracker.config import Config
from odu_tracker.dns_proxy import start_proxy
from odu_tracker import dns_proxy as _dp
from odu_tracker.poller import Poller
from odu_tracker.router import OduRouter, RouterError, LoginLocked
from odu_tracker.security import normalize_mac
import os

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(cfg: Config, poller: Poller):
    app = Flask("odu_tracker", static_folder=STATIC_DIR)
    app.secret_key = cfg.session_secret
    app.json.sort_keys = False
    from datetime import timedelta
    app.config.update(
        SESSION_COOKIE_NAME="odu_session",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    from odu_tracker.speedtest import SpeedTestRunner
    runner = SpeedTestRunner()

    # ------------------------------------------------------------------ ui
    @app.route("/")
    def index():
        return send_from_directory(os.path.join(STATIC_DIR), "index.html")

    # ------------------------------------------------------------- helpers
    def require_auth(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("auth"):
                return jsonify({"error": "auth_required"}), 401
            return fn(*args, **kwargs)
        return wrapper

    # ---------------------------------------------------------------- auth
    @app.post("/api/login")
    def login():
        pw = (request.json or {}).get("password", "")
        remember = bool((request.json or {}).get("remember", True))
        if not pw:
            return jsonify({"error": "password required"}), 400
        probe = OduRouter(ip=cfg.get("router_ip"),
                          username=cfg.get("router_username") or "admin",
                          password=pw)
        try:
            probe.login(pw)
        except LoginLocked as exc:
            return jsonify({"error": f"router login locked ({exc.seconds_left}s left)"}), 429
        except RouterError as exc:
            return jsonify({"error": str(exc)}), 401
        except Exception:
            return jsonify({"error": f"cannot reach router at {cfg.get('router_ip')}"}), 502
        # success: store, start poller
        cfg.password = pw
        cfg.set("setup_done", True)
        poller.update_router_creds(password=pw)
        poller.start()
        session["auth"] = True
        session.permanent = remember          # persistent cookie => no re-login
        return jsonify({"ok": True})

    @app.post("/api/logout")
    @require_auth
    def logout_ui():
        session.pop("auth", None)
        return jsonify({"ok": True})

    @app.get("/api/meta")
    def meta():
        """Used by the UI before auth: does a password exist?
           Boot with or without."""
        st = poller.status()
        lock = {"attempts_left": None, "lock_secs": None}
        try:
            probe = OduRouter(ip=cfg.get("router_ip"))
            resp = probe.build(232, "GET")   # unauthenticated handshake (safe)
            lock = {
                "attempts_left": int(resp.get("buffer")) if resp.get("buffer") else None,
                "lock_secs": int(resp.get("netx_login_time")) if resp.get("netx_login_time") else None,
            }
        except Exception:
            pass
        return jsonify({
            "setup_done": cfg.get("setup_done") and cfg.has_password,
            "authed": bool(session.get("auth")),
            "router_ip": cfg.get("router_ip"),
            "poll_running": st["running"],
            "router_logged_in": st["logged_in"],
            **lock,
        })

    # -------------------------------------------------------------- status
    @app.get("/api/status")
    @require_auth
    def status():
        st = poller.status()
        raw = store.q("SELECT body FROM raw_cache WHERE name='fast'", fetch="one")
        data = {}
        if raw:
            try:
                data = (json.loads(raw["body"]) or {})
            except json.JSONDecodeError:
                data = {}
        # This firmware returns FLAT payloads (no wrappers)
        sys_status = data.get("sys_status") or {}
        home = data.get("home_info") or {}
        runtime = data.get("runtime") or {}
        flow = data.get("flow") or {}
        battery = data.get("battery") or {}
        network = data.get("network") or {}
        usage_now = store.current_usage()
        day = store.day_start()
        rows = store.q("SELECT ts, used, unit FROM usage_samples WHERE ts>=? ORDER BY ts",
                       (day,))
        day_used = None
        if len(rows) >= 2:
            first, last = rows[0], rows[-1]
            if first["unit"] == last["unit"] and last["used"] >= first["used"]:
                day_used = round(last["used"] - first["used"], 2)

        # usage counted from the user's last manual reset (local baseline)
        base = store.setting_get("usage_baseline")
        since_reset = None
        if base:
            total_mb = None
            if flow.get("mon_download_flow"):
                try:
                    total_mb = float(flow.get("mon_download_flow"))
                except (TypeError, ValueError):
                    pass
            if total_mb is None and usage_now and usage_now.get("unit") == "GB":
                total_mb = usage_now["used"] * 1024.0
            if total_mb is not None:
                since_reset = {
                    "ts": base.get("ts"),
                    "gb": round(max(0.0, total_mb - float(base.get("total_mb") or 0)) / 1024.0, 2),
                }
        return jsonify({
            "now": time.time(),
            "poll": st,
            "sys": {
                "network_type_str": sys_status.get("network_type_str"),
                "signal_lvl": sys_status.get("signal_lvl"),
                "sim_status": sys_status.get("sim_status"),
                "network_operator": sys_status.get("network_operator"),
                "network_status": sys_status.get("network_status"),
                "board_type": sys_status.get("board_type") or runtime.get("board_type"),
                "idu_dev_type": sys_status.get("idu_dev_type"),
                "wired_link_list": sys_status.get("wired_link_list"),
            },
            "signal": {
                "rssi": home.get("RSSI") or home.get("rssi"),
                "rsrp": home.get("RSRP") or home.get("rsrp"),
                "rssi_5g": home.get("RSSI_5G") or home.get("rssi_5g"),
                "rsrp_5g": home.get("RSRP_5G") or home.get("rsrp_5g"),
            },
            "wan": {"wan_ip": home.get("wan_ip"), "uptime": home.get("uptime"),
                    "ssid24": home.get("ssid24"), "ssid58": home.get("ssid58"),
                    "lan_ip": home.get("lanIp"), "device_sn": home.get("device_sn"),
                    "fw": runtime.get("real_fwversion"), "cpuload": runtime.get("cpuload"),
                    "memory": runtime.get("memory")},
            "battery": battery,
            "network": network,
            "flow": {
                "total_mb": flow.get("mon_download_flow"),
                "down_mb": flow.get("dl_mon_flow"),
                "up_mb": flow.get("ul_mon_flow"),
                "limit_size": flow.get("limitSize"),
                "limit_unit": flow.get("flow_limit_unit"),
                "limit_switch": flow.get("limitSwitch"),
            },
            "usage": usage_now,
            "day_usage": day_used,
            "since_reset": since_reset,
            "rates": {"current": poller.current_rate,
                      "avg_hour": poller.avg_rate(3600),
                      "avg_10m": poller.avg_rate(600)},
        })

    # ------------------------------------------------------------ devices
    @app.get("/api/devices")
    @require_auth
    def devices():
        now_ts = store.now()
        online = store.online_macs(window=int(cfg.get("poll_interval", 15)) * 4)
        hosts = store.q(
            "SELECT d.*, "
            "  (SELECT detail FROM events WHERE events.mac=d.mac AND kind='new_device' "
            "   ORDER BY ts ASC LIMIT 1) AS first_event "
            "FROM devices d ORDER BY d.riskscore DESC, d.last_seen DESC")
        out = []
        for h in hosts:
            h["online"] = h["mac"] in online
            h["age_days"] = round((now_ts - h["first_seen"]) / 86400.0, 1)
            h["riskreasons"] = json.loads(h.get("riskreasons") or "[]")
            out.append(h)
        return jsonify({"devices": out, "count": len(out)})

    @app.post("/api/devices/<mac>/trust")
    @require_auth
    def trust(mac, ):
        m = normalize_mac(mac)
        if not m:
            return jsonify({"error": "bad mac"}), 400
        flagged = bool((request.json or {}).get("trusted", True))
        store.set_trust(m, flagged)
        state = "on" if flagged else "off"
        store.exec("INSERT INTO events(ts, mac, kind, level, detail) VALUES(?,?,?,?,?)",
                   (store.now(), m, "trust_change", "info", "trusted=" + state))
        return jsonify({"ok": True})

    @app.post("/api/devices/<mac>/block")
    @require_auth
    def block(mac):
        m = normalize_mac(mac)
        if not m:
            return jsonify({"error": "bad mac"}), 400
        want = bool((request.json or {}).get("blocked", True))
        result = {"db": True}
        if want:
            # try to add to router MAC blacklist or whitelist depending on mode
            resp = _toggle_mac_filter(m, block=True)
            result["router"] = resp
        else:
            resp = _toggle_mac_filter(m, block=False)
            result["router"] = resp
        store.set_blocked(m, want)
        state = "yes" if want else "no"
        store.exec("INSERT INTO events(ts, mac, kind, level, detail) VALUES(?,?,?,?,?)",
                   (store.now(), m, "block_change", "warning", "blocked=" + state))
        return jsonify(result)

    def _toggle_mac_filter(mac, block: bool):
        """Push the MAC exclusion into the router's access-control list."""
        try:
            mode = poller.router.cmd(28, "GET")          # getIPFilterMode
            lists = poller.router.cmd(23, "GET")         # getMACData
            datas = lists.get("data", {}).get("datas") or []
            if isinstance(datas, dict):
                datas = [datas]
            entry = {"mac": mac, "ippro": "IPV4"}
            if block:
                entry.update({"enableRule": True, "protocol": "All", "remark": "placed by odu-tracker"})
                datas.append(entry)
            else:
                datas = [d for d in datas if normalize_mac(str(d.get("mac", ""))) != mac]
            set1 = poller.router.cmd(23, "POST", {"datas": datas})    # setMACData
            apply = poller.router.cmd(20, "POST", {})                  # applyFileter
            return {"ok": True, "set": set1.get("message", ""), "apply": apply.get("message", "")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -------------------------------------------------------------- usage
    @app.get("/api/usage")
    @require_auth
    def usage():
        hours = int(request.args.get("hours", 24))
        rows = store.q("SELECT ts, used, unit FROM usage_samples WHERE ts>=? ORDER BY ts",
                       (store.now() - hours * 3600,))
        # deltas
        series = []
        prev = None
        for r in rows:
            if prev is not None and r["unit"] == prev["unit"] and r["used"] >= prev["used"]:
                series.append({"ts": r["ts"], "delta": round(r["used"] - prev["used"], 3),
                               "cum": r["used"], "unit": r["unit"]})
            prev = r
        return jsonify({"hours": hours, "series": series})

    # ------------------------------------------------------------- events
    @app.get("/api/events")
    @require_auth
    def events():
        kinds = request.args.get("kind")
        rows = store.recent_events(kind=kinds, limit=int(request.args.get("limit", 200)))
        return jsonify({"events": rows})

    @app.get("/api/alerts")
    @require_auth
    def alerts():
        return jsonify({"alerts": store.recent_alerts(200)})

    @app.post("/api/alerts/read")
    @require_auth
    def alerts_read():
        store.exec("UPDATE alerts SET read=1")
        return jsonify({"ok": True})

    # ------------------------------------------------------ sites + ranks
    @app.get("/api/sites")
    @require_auth
    def sites():
        hours = int(request.args.get("hours", 6))
        domain_rows = store.site_summary(hours=hours)
        client_rows = store.site_client_summary(hours=hours)
        # join client_ip -> device names/mac from DHCP inventory
        devs = {d["last_ip"]: d for d in
                store.q("SELECT mac, last_ip, last_hostname, vendor FROM devices")}
        for row in client_rows:
            d = devs.get(row["client_ip"])
            row["hostname"] = d["last_hostname"] if d else "unknown device"
            row["mac"] = d["mac"] if d else ""
        # per-device usage ranking (DNS activity; firmware keeps no per-IP bytes)
        ranking = sorted(client_rows, key=lambda r: (-r["hits"], r["hostname"]))
        raw = store.q("SELECT body FROM raw_cache WHERE name='url_filter'", fetch="one")
        url_rules = []
        if raw:
            try:
                u = json.loads(raw["body"]) or {}
                items = (u.get("data") or {}).get("datas") or []
                for it in items if isinstance(items, list) else []:
                    if isinstance(it, dict):
                        url_rules.append({"url": it.get("url") or it.get("domain") or "",
                                          "enabled": it.get("enable", it.get("enableRule"))})
            except json.JSONDecodeError:
                pass
        return jsonify({"domains": domain_rows, "clients": client_rows,
                        "ranking": ranking,
                        "hours": hours, "url_rules": url_rules})

    # ------------------------------------------------------ raw (advanced)
    @app.get("/api/raw")
    @require_auth
    def raw_dump():
        name = request.args.get("name", "fast")
        row = store.q("SELECT body FROM raw_cache WHERE name=?", (name,), fetch="one")
        return jsonify({"name": name, "body": row.get("body") if row else None})

    # ---------------------------------------------------------- change pw
    @app.post("/api/changepw")
    @require_auth
    def changepw():
        pw = (request.json or {}).get("password", "")
        if not pw:
            return jsonify({"error": "password required"}), 400
        probe = OduRouter(ip=cfg.get("router_ip"),
                          username=cfg.get("router_username") or "admin",
                          password=pw)
        try:
            probe.login(pw)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 401
        poller.update_router_creds(password=pw)
        return jsonify({"ok": True})

    # ------------------------------------------------------- usage baseline
    @app.post("/api/usage/reset")
    @require_auth
    def usage_reset():
        """Reset data-usage counting from now.

        Tries the firmware's own statistics-clear (cmd 337 POST with
        statisticsReset=1, as the vendor UI does); falls back to a local
        baseline if the firmware refuses.
        """
        fw = {"attempted": False, "ok": False, "after_mb": None}
        try:
            resp = poller.router.cmd(337, "POST",
                                     {"statisticsReset": "1", "usedFlow": "0"})
            fw["attempted"] = True
            fw["ok"] = bool(resp.get("success"))
        except Exception as exc:
            fw["error"] = str(exc)
        if fw["attempted"] and fw["ok"]:
            try:
                check = poller.router.cmd(337, "GET")
                fw["after_mb"] = check.get("mon_download_flow")
            except Exception:
                pass
        if not (fw.get("ok") and (fw.get("after_mb") in (None, "0", "0.00") or
                                  _is_small(fw.get("after_mb")))):
            # local baseline fallback / in addition
            total_mb = _current_total_mb()
            ts = time.time()
            store.setting_set("usage_baseline", {"ts": ts, "total_mb": total_mb})
        else:
            store.setting_set("usage_baseline",
                              {"ts": time.time(), "total_mb": 0.0})
        store.add_alert("info", "Usage counter reset",
                        "Counting started from now "
                        + ("(router counters cleared)" if fw.get("ok") else "(local baseline)"))
        return jsonify({"ok": True, "firmware": fw})

    def _is_small(v):
        try:
            return float(v) < 1024   # less than ~1 GB residual
        except (TypeError, ValueError):
            return False

    def _current_total_mb():
        try:
            r = poller.router.cmd(337, "GET")
            return float(r.get("mon_download_flow") or 0)
        except Exception:
            return 0.0

    # ------------------------------------------------ router DNS for sites
    @app.get("/api/dnscfg")
    @require_auth
    def dnscfg_get():
        import socket as _s
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            my_ip = s.getsockname()[0]
            s.close()
        except OSError:
            my_ip = None
        from odu_tracker.dns_proxy import _singleton
        raw = store.q("SELECT body FROM raw_cache WHERE name='fast'", fetch="one")
        dhcp = {}
        if raw:
            try:
                dhcp = (json.loads(raw["body"]).get("dhcp_data") or {})
            except (json.JSONDecodeError, AttributeError):
                dhcp = {}
        return jsonify({
            "enabled": bool(_dp._singleton and _dp._singleton.is_alive()),
            "port": cfg.get("dns_proxy_port"),
            "upstream": cfg.get("dns_upstream"),
            "myIp": my_ip,
            "router_dns_main": dhcp.get("main_dns") or "",
            "router_dns_vice": dhcp.get("vice_dns") or "",
            "dns_pointed_at_app": (my_ip and dhcp.get("main_dns") == my_ip),
        })

    @app.post("/api/dnscfg")
    @require_auth
    def dnscfg_set():
        enabled = bool((request.json or {}).get("enabled"))
        cfg.set("dns_proxy_enabled", enabled)
        if enabled:
            start_proxy(cfg)
            port = cfg.get("dns_proxy_port")
            return jsonify({"ok": True, "enabled": True, "port": port})
        if _dp._singleton:
            _dp._singleton.stop()
        return jsonify({"ok": True, "enabled": False})

    @app.post("/api/dnscfg/router")
    @require_auth
    def dnscfg_router():
        """Point the router's DHCP DNS at this app (per-device site tracking)
        or restore the values the router had before we changed anything."""
        mode = (request.json or {}).get("mode", "app")
        cur = poller.router.cmd(3, "GET")
        import socket as _s
        try:
            s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            my_ip = s.getsockname()[0]
            s.close()
        except OSError:
            return jsonify({"ok": False, "error": "cannot determine this PC's IP"}), 500
        if mode == "app":
            if cur.get("main_dns") == my_ip:
                start_proxy(cfg)
                return jsonify({"ok": True, "mode": mode,
                                "message": f"router DNS already points at {my_ip}"})
            # remember the pre-change values so "restore" is faithful
            if not store.setting_get("dns_original"):
                store.setting_set("dns_original", {
                    "main_dns": cur.get("main_dns") or "",
                    "vice_dns": cur.get("vice_dns") or "",
                })
            new_main, new_vice = my_ip, "8.8.8.8"
            note = (f"router DHCP DNS now prefers this app ({my_ip}) "
                    f"with 8.8.8.8 fallback")
        else:
            orig = store.setting_get("dns_original") or {}
            new_main = orig.get("main_dns") or cfg.get("router_ip")
            new_vice = orig.get("vice_dns") or ""
            note = "router DHCP DNS restored to its previous settings"
        payload = {
            "lanIp": cur.get("lanIp", ""),
            "netMask": cur.get("netMask", ""),
            "dhcpServer": cur.get("dhcpServer", "1"),
            "main_dns": new_main,
            "vice_dns": new_vice,
            "ipBegin": cur.get("ipBegin", ""),
            "ipEnd": cur.get("ipEnd", ""),
            "expireTime": cur.get("expireTime", "24h"),
        }
        resp = poller.router.cmd(3, "POST", payload)
        if mode == "app":
            start_proxy(cfg)
        ok = bool(resp.get("success", bool(resp.get("message") is None)))
        store.add_alert("info" if ok else "warning", "Router DNS updated"
                        if ok else "Router DNS change failed", note)
        return jsonify({"ok": ok, "mode": mode, "router_resp": resp,
                        "message": note})

    # ------------------------------------------------------- speed tests
    @app.post("/api/speedtest/run")
    @require_auth
    def speedtest_run():
        body = request.json or {}
        size_mb = max(1, min(50, int(body.get("size_mb", 6))))
        up_mb = max(0, min(10, int(body.get("up_mb", 1))))
        return jsonify(runner.start(size_mb=size_mb, up_mb=up_mb))

    @app.get("/api/speedtest")
    @require_auth
    def speedtest_status():
        st = runner.status()
        hist = store.speed_history(limit=int(request.args.get("limit", 15)))
        return jsonify({**st, **hist})

    @app.get("/api/rates")
    @require_auth
    def rates():
        minutes = max(5, min(120, int(request.args.get("minutes", 30))))
        return jsonify({"series": poller.rate_series(window_s=minutes * 60)})

    return app
