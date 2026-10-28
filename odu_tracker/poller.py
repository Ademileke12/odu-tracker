"""Background polling service.

Two loops:
  fast (default 15 s): devices, home info, battery, flow counters -> SQLite
  slow (default 300 s): topology, filters, access control, logs -> raw cache

API-independent persistence: parsers are tolerant, mapping into normalized
rows; anything unknown is retained verbatim into raw_cache so the UI can
still display truth.
"""
import json
import threading
import time
import traceback

from . import store
from .config import Config
from .router import OduRouter, RouterError
from .security import normalize_mac, risk_score, vendor_for
from . import voucher

FAST_KEYS = ("all_device", "home_info", "flow_new", "sys_status", "battery", "network")

# Candidate keys where a device list may hide inside responses.
DEVICE_LIST_KEYS = ("dhcp_list_info", "datas", "data_list", "list", "clients")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def extract_devices(resp: dict):
    """Generic device list extraction -> list of normalized dicts."""
    out = []
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        data = resp if isinstance(resp, dict) else {}
    for key in DEVICE_LIST_KEYS:
        items = data.get(key)
        if isinstance(items, list) and items:
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                mac = normalize_mac(raw.get("mac") or raw.get("mac_addr") or
                                    raw.get("MacAddr") or raw.get("sta_mac"))
                ip = raw.get("ip") or raw.get("ip_addr") or raw.get("IpAddr")
                if mac:
                    out.append({
                        "mac": mac,
                        "ip": ip or "",
                        "iface": raw.get("interface") or raw.get("iface") or "",
                        "hostname": raw.get("hostname") or raw.get("name") or "",
                        "raw": raw,
                    })
            # join with device-name table variants that use ip only?
            if out:
                return out
    return out


def parse_usage(resp: dict):
    """Cumulative total-usage extraction (this firmware: FLAT responses).

    cmd 337 gives monthly counters in MB:
        mon_download_flow  (total DL this month, MB)
        dl_mon_flow / ul_mon_flow
        limitSize + flow_limit_unit (unit: '1'=GB) — plan quota
    """
    data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
    if not isinstance(data, dict):
        return None
    used = (data.get("mon_download_flow") or
            data.get("sys_used_flow") or
            data.get("mon_download") or "")
    total = (data.get("limitSize") or "")
    unit = "GB" if str(data.get("flow_limit_unit") or "1") in ("1", "GB") else "MB"
    try:
        # counters are MB; normalize to the display unit (GB default)
        used_mb = float(used)
    except (TypeError, ValueError):
        return None
    used = round(used_mb / 1024.0, 2)
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = None
    return {
        # NB: `used` is ALWAYS GB (MB/1024 above) — never label it with the
        # plan's unit. The plan keeps its own unit for the meter display.
        "used": used,
        "unit": "GB",
        "total": total or None,
        "plan_unit": unit,
        "mb_download": data.get("dl_mon_flow") or "",
        "mb_upload": data.get("ul_mon_flow") or "",
        "limit_switch": str(data.get("limitSwitch") or "0"),
    }


class Poller:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.router = None
        self._stop = threading.Event()
        self._thread = None
        self.last_fast = None
        self.last_slow = None
        self._prev_devices = set()
        self._last_status = {}
        self._fast_ts = 0
        self._slow_ts = 0
        # passive throughput derived from flow-counter deltas
        self._last_flow = None          # (ts, cumulative_mb)
        self.rate_ring = []             # [(ts, mbps)] recent window
        self.current_rate = None        # last interval's Mbps

    # ------------------------------------------------------------- controls
    def start(self):
        if self._thread is None:
            self.router = OduRouter(
                ip=self.cfg.get("router_ip"),
                username=self.cfg.get("router_username") or "admin",
                password=self.cfg.password,
            )
            self._thread = threading.Thread(target=self._run, name="odu-poller", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def status(self):
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "logged_in": self.router.logged_in if self.router else False,
            "last_poll": self._fast_ts,
            "last_slow": self._slow_ts,
        }

    def update_router_creds(self, ip=None, password=None):
        if ip:
            self.cfg.set("router_ip", ip)
        if password:
            self.cfg.password = password
        self.router = OduRouter(
            ip=self.cfg.get("router_ip"),
            username=self.cfg.get("router_username") or "admin",
            password=self.cfg.password,
        )

    # ---------------------------------------------------------- worker loop
    def _run(self):
        while not self._stop.is_set():
            try:
                if not self.router.password:
                    time.sleep(5)
                    continue
                if not self.router.logged_in:
                    try:
                        self.router.login()
                        store.add_alert("success", "Connected to ODU",
                                        f"Logged in as {self.router.username}@{self.router.ip}")
                    except RouterError as exc:
                        store.add_alert("danger", "Login failed", str(exc))
                        time.sleep(30)
                        continue
                # ---- fast poll
                if time.time() - self._fast_ts >= int(self.cfg.get("poll_interval", 15)):
                    self.fast_poll()
                    self._fast_ts = time.time()
                # ---- slow poll
                if time.time() - self._slow_ts >= int(self.cfg.get("slow_interval", 300)):
                    self.slow_poll()
                    self._slow_ts = time.time()
            except Exception:
                traceback.print_exc()
                self.router.logged_in = False
                time.sleep(10)
            time.sleep(1)

    # ------------------------------------------------------------- polling
    def fast_poll(self):
        try:
            snap = self.router.snapshot_all()
        except Exception:
            self.router.logged_in = False
            raise
        now_ts = store.now()
        store.cache_raw("fast", snap)

        devices = extract_devices(snap.get("all_device", {}))
        if not devices:
            devices = extract_devices(snap.get("dhcp_data", {}))
        if not devices:
            devices = extract_devices(snap.get("home_info", {}))

        seen_now = set()
        for dev in devices:
            row_seen = store.upsert_device({**dev, "ts": now_ts})
            if row_seen:
                self._alert_new_device(dev)
            seen_now.add(dev["mac"])
            store.exec("INSERT INTO snapshots(ts, mac, ip, iface) VALUES(?,?,?,?)",
                       (now_ts, dev["mac"], dev.get("ip"), dev.get("iface")))

        # contextual risk update for each visible device
        online = seen_now
        for dev in devices:
            self._update_risk(dev, online)

        # usage samples + passive throughput
        usage = parse_usage(snap.get("flow_new", {})) or parse_usage(snap.get("flow", {}))
        if usage:
            store.add_usage_sample(now_ts, usage["used"], usage["unit"], usage["total"] or None)
            self._check_flow_regression(now_ts, usage)
            self._track_rate(now_ts, usage)
            self._meter_devices(now_ts, usage, seen_now)
        self.last_fast = snap

    def _meter_devices(self, ts, usage, seen_now):
        """Time-share per-device metering from the shared counter (voucher.py).

        Splits this poll's counter growth across online claim-holders; any MAC
        that just crossed its quota is denied on the router (cmd 278) so it
        loses Wi-Fi internet on top of the DNS hijack. Router writes happen in
        the background so the 15 s poll loop never blocks on the ~1.3 s pacing.
        """
        try:
            counter_mb = (usage["used"] or 0) * 1024.0
            newly = voucher.attribute_usage(counter_mb, seen_now)
        except Exception:
            traceback.print_exc()
            return
        for mac in newly:
            store.add_alert("warning", "Device used its data",
                            f"{mac} reached its voucher quota; internet cut")
            threading.Thread(target=self._deny_exhausted, args=(mac,),
                             daemon=True, name=f"deny-{mac[-5:]}").start()

    def _deny_exhausted(self, mac):
        try:
            if self.router is not None and self.router.logged_in:
                voucher.gate_deny(self.router, mac)
        except Exception:
            traceback.print_exc()

    def _check_flow_regression(self, ts, usage):
        """Detect the router's cumulative counter going backwards.

        Happens on ODU reboots/clears (or our own Reset button). Counter-based
        charts would otherwise stick at zero or double-count, and a local
        usage baseline from a previous counter could stay out of reach
        forever. Keep everything anchored to what the router says now.
        """
        total_mb = (usage["used"] or 0) * 1024.0
        if self._last_flow is None:
            return
        prev_ts, prev_mb = self._last_flow
        if total_mb >= prev_mb - 1.0:          # monotonic as expected
            return
        # synthesize a clean restart for the rate ring (no fake Mbps spike)
        self._last_flow = (ts, min(prev_mb, total_mb))
        drop = prev_mb - total_mb
        base = store.setting_get("usage_baseline")
        if base and float(base.get("total_mb") or 0) > total_mb + 1:
            store.setting_set("usage_baseline", {"ts": float(base.get("ts") or ts),
                                                 "total_mb": total_mb})
        store.add_alert("info", "Router usage counter reset",
                        f"Counters dropped {drop / 1024:.2f} GB (reboot/clear); "
                        "tracking re-anchored to the router's new counter")

    def _track_rate(self, ts, usage):
        """Derive average link Mbps from cumulative counter deltas.

        usage['used'] is GB (see parse_usage) — convert to MB, delta/dt * 8
        gives Mbps averaged over the poll interval. Zero extra data cost.
        """
        mb_now = (usage["used"] or 0) * 1024.0
        if self._last_flow is not None:
            t0, mb0 = self._last_flow
            dt = ts - t0
            if dt >= 2 and mb_now >= mb0:
                mbps = (mb_now - mb0) * 8.0 / dt
                self.current_rate = round(mbps, 2)
                self.rate_ring.append((ts, self.current_rate))
                if len(self.rate_ring) > 480:          # ~2h at 15s polls
                    self.rate_ring = self.rate_ring[-480:]
        self._last_flow = (ts, mb_now)

    def avg_rate(self, window_s=3600):
        """Average Mbps over the last window from the rate ring."""
        cutoff = store.now() - window_s
        vals = [m for (t, m) in self.rate_ring if t >= cutoff]
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    def rate_series(self, window_s=3600, limit=240):
        cutoff = store.now() - window_s
        return [{"ts": t, "mbps": m} for (t, m) in self.rate_ring if t >= cutoff][-limit:]

    def slow_poll(self):
        try:
            snap = self.router.slow_snapshot()
        except Exception:
            store.add_alert("warning", "Slow poll failed", "Topology/log fetch error")
            return
        store.cache_raw("slow", snap)
        # URL filter lists -> "websites" tab source A
        urls = snap.get("url_filter") or {}
        self._store_url_filter(urls)
        self.last_slow = snap

    def _store_url_filter(self, url_resp):
        # URL filter entries are *rules* (not visits); keep them cached raw
        # for the Websites tab reference panel.
        store.cache_raw("url_filter", url_resp)

    # ------------------------------------------------------------- behavior
    def _alert_new_device(self, dev):
        vendor = vendor_for(dev["mac"]) or "unknown vendor"
        host = dev.get("hostname") or "(no hostname)"
        store.add_alert("warning", "New device joined",
                        f"{host} [{dev['mac']}] ({vendor}) at {dev.get('ip')}")

    def _update_risk(self, dev, online_now):
        mac = dev["mac"]
        rec = store.q("SELECT * FROM devices WHERE mac=?", (mac,), fetch="one")
        if not rec:
            return
        age_h = max(0.0, (store.now() - rec["first_seen"]) / 3600.0)
        ctx = {
            "is_new": (store.now() - rec["first_seen"]) < 120,
            "blocked_before": bool(rec["blocked"]),
            "trusted": bool(rec["trusted"]),
            "age_hours": age_h,
        }
        score, level, reasons = risk_score(dev, ctx)
        if score != rec["riskscore"] or level != rec["risklevel"]:
            store.exec("UPDATE devices SET riskscore=?, risklevel=?, riskreasons=?, vendor=? WHERE mac=?",
                       (score, level, json.dumps(reasons), vendor_for(mac) or "",
                        mac))
            if level == "high" and rec["risklevel"] != "high":
                store.add_alert("danger", "Dangerous device detected",
                                f"{dev.get('hostname') or mac}: " + "; ".join(reasons))
            elif level == "medium" and rec["risklevel"] in ("unknown", "low"):
                store.add_alert("warning", "Suspicious device flagged",
                                f"{dev.get('hostname') or mac}: " + "; ".join(reasons))
