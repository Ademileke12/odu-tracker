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
from odu_tracker.poller import Poller
from odu_tracker.router import OduRouter, RouterError, LoginLocked
from odu_tracker.security import normalize_mac
import os

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def create_app(cfg: Config, poller: Poller):
    app = Flask("odu_tracker", static_folder=STATIC_DIR)
    app.secret_key = cfg.session_secret
    app.json.sort_keys = False
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
        session.permanent = True
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
                "rssi": home.get("RSSI"), "rsrp": home.get("RSRP"),
                "rssi_5g": home.get("RSSI_5G"), "rsrp_5g": home.get("RSRP_5G"),
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

    # -------------------------------------------------------------- sites
    @app.get("/api/sites")
    @require_auth
    def sites():
        hours = int(request.args.get("hours", 6))
        domain_rows = store.site_summary(hours=hours)
        clients = store.site_client_summary(hours=hours)
        # Pull URL filter list for reference of "blocked domains"
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
        return jsonify({"domains": domain_rows, "clients": clients,
                        "hours": hours, "url_rules": url_rules})

    # ------------------------------------------------------ raw (advanced)
    @app.get("/api/raw")
    @require_auth
    def raw_dump():
        name = request.args.get("name", "fast")
        row = store.q("SELECT body FROM raw_cache WHERE name=?", (name,), fetch="one")
        return jsonify({"name": name, "body": row.get("body") if row else None})

    # ------------------------------------------------------- dns proxy cfg
    @app.get("/api/dnscfg")
    @require_auth
    def dnscfg_get():
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            my_ip = s.getsockname()[0]
            s.close()
        except OSError:
            my_ip = None
        from odu_tracker import dns_proxy as _dp
        return jsonify({
            "enabled": bool(_dp._singleton and _dp._singleton.is_alive()),
            "port": cfg.get("dns_proxy_port"),
            "upstream": cfg.get("dns_upstream"),
            "myIp": my_ip,
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
        """Start counting usage from now (local baseline; the router's own
        monthly counter can't be wiped from its web API)."""
        total_mb = None
        try:
            resp = poller.router.cmd(337, "GET")
            total_mb = float(resp.get("mon_download_flow") or 0)
        except Exception:
            pass
        if total_mb is None:   # fall back to last cached value
            raw = store.q("SELECT body FROM raw_cache WHERE name='fast'", fetch="one")
            try:
                fast = json.loads(raw["body"]) if raw else {}
                total_mb = float((fast.get("flow") or {}).get("mon_download_flow") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                total_mb = 0.0
        ts = time.time()
        store.setting_set("usage_baseline", {"ts": ts, "total_mb": total_mb})
        store.add_alert("info", "Usage counter reset",
                        f"Counting from now (baseline {round(total_mb / 1024, 2)} GB)")
        return jsonify({"ok": True, "ts": ts, "total_mb": total_mb})

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
