"""Background polling service.

Two loops:
  fast (default 15 s): devices, home info, battery, flow counters -> SQLite
  slow (default 300 s): topology, filters, access control, logs -> raw cache

API-independent persistence: parsers are tolerant, mapping into normalized
rows; anything unknown is retained verbatim into raw_cache so the UI can
still display truth.
"""
import json
import socket
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
        # portal-aware soft cut: was the hijack path fully up last poll?
        self._soft_prev = False

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

        # portal-aware soft cut: the moment the hijack path is fully up,
        # re-admit devices still held by a stale cmd278 deny so they can
        # rejoin the SSID and reach the claim page (self-service recovery)
        soft = self._portal_soft_cut()
        if soft and not self._soft_prev:
            threading.Thread(target=self._lift_soft_denies, daemon=True,
                             name="soft-lift").start()
        self._soft_prev = soft

        # a router reboot silently reverts its DHCP DNS to its own IP,
        # which KILLS the hijack (and with it the soft cut) while every
        # status flag still says "enforced". self-heal it.
        if self.cfg.get("portal_enforced"):
            self._repoint_dns_if_drifted()
        self.last_fast = snap

    def _meter_devices(self, ts, usage, seen_now):
        """Time-share per-device metering from the shared counter (voucher.py).

        Splits this poll's counter growth among online claim-holders. A device
        that crosses its quota is ALWAYS held by the captive portal (soft cut,
        product decision 2026-10-29): it keeps its Wi-Fi association and every
        lookup lands on the voucher claim page, so the holder can redeem a new
        code by themselves within seconds. The router kick (cmd 278) was
        retired from this path — verified live it DEAUTHENTICATES the client
        completely (the phone could not even rejoin the SSID), which destroyed
        the self-service recovery loop. cmd 278 remains available for the
        manual Devices-tab block only.
        """
        try:
            counter_mb = (usage["used"] or 0) * 1024.0
            newly = voucher.attribute_usage(counter_mb, seen_now)
        except Exception:
            traceback.print_exc()
            return
        if not newly:
            return
        held = self._portal_soft_cut()
        for mac in newly:
            if held:
                store.add_alert("warning", "Device used its data",
                                f"{mac} reached its voucher quota; redirected "
                                "to the voucher claim page (soft cut)")
            else:
                # No enforcement possible without the hijack path: say so
                # loudly instead of silently letting the device keep browsing.
                store.add_alert("danger", "Device used its data",
                                f"{mac} reached its voucher quota — captive "
                                "portal not armed, so it still has internet. "
                                "Arm it in the Vouchers tab to hold it at the "
                                "claim page.")

    # ------------------------------------------- portal-aware soft cut policy
    def _portal_soft_cut(self) -> bool:
        """True when the captive portal currently HOLDS an exhausted device
        (the hijack path is verifiably up), False when the soft cut is armed
        in policy but not actually enforcing.

        Soft cut is ALWAYS the exhaustion policy (see _meter_devices); this
        check only decides which alert fires and drives the re-admission
        transition in fast_poll. All links must hold:
          * portal_enforced armed by the admin,
          * claim page running,
          * DNS proxy running without error,
          * router DHCP DNS handing out this machine — otherwise the hijack
            reaches nobody and the device would roam free.
        """
        try:
            if not self.cfg.get("portal_enforced"):
                return False
            from odu_tracker import dns_proxy as _dp
            from odu_tracker import portal as _portal
            if not _portal.status().get("running"):
                return False
            dp = _dp._singleton
            if not (dp and dp.is_alive() and not getattr(dp, "error", None)):
                return False
            return self._dns_pointed_at_self()
        except Exception:
            return False

    def _dns_pointed_at_self(self) -> bool:
        """Router DHCP main DNS == this machine's LAN IP (read from the fresh
        cmd-3 snapshot the poller caches in raw_cache['fast'])."""
        try:
            raw = store.q("SELECT body FROM raw_cache WHERE name='fast'",
                          fetch="one")
            if not raw:
                return False
            dhcp = json.loads(raw["body"]).get("dhcp_data") or {}
            main = str(dhcp.get("main_dns") or "")
            if not main:
                return False
            me = self._my_lan_ip()
            return bool(me) and main == me
        except Exception:
            return False

    def _repoint_dns_if_drifted(self, cooldown_s=600):
        """Self-heal: re-apply the DHCP DNS pointing after it drifts.

        Verified live incident 2026-10-30: the ODU reverts its cmd-3 DNS
        fields to the defaults on reboot, so the hijack silently reached
        zero devices while `portal_enforced` stayed true — every exhausted
        device roamed free. The admin only ever clicks "point DNS at this
        app" once; a router reboot must not undo enforcement forever after.
        So while the portal is armed, drift is repaired automatically,
        rate-limited so a flaky router can't be hammered with cmd-3 posts.
        """
        now = time.time()
        if now - getattr(self, "_dns_heal_ts", 0) < cooldown_s:
            return
        if self._dns_pointed_at_self():
            return
        if self.router is None or not self.router.logged_in:
            return
        self._dns_heal_ts = now
        threading.Thread(target=self._repoint_dns, daemon=True,
                         name="dns-heal").start()

    def _repoint_dns(self):
        """Push cmd 3 with this machine as main DNS (8.8.8.8 fallback),
        preserving every other DHCP field the router reported."""
        try:
            me = self._my_lan_ip()
            if not me:
                return
            cur = self.router.cmd(3, "GET") or {}
            payload = {
                "lanIp": cur.get("lanIp", ""),
                "netMask": cur.get("netMask", ""),
                "dhcpServer": cur.get("dhcpServer", "1"),
                "main_dns": me,
                "vice_dns": "8.8.8.8",
                "ipBegin": cur.get("ipBegin", ""),
                "ipEnd": cur.get("ipEnd", ""),
                "expireTime": cur.get("expireTime", "24h"),
            }
            resp = self.router.cmd(3, "POST", payload) or {}
            ok = bool(resp.get("success"))
            store.add_alert(
                "info" if ok else "danger",
                "Router DNS self-healed" if ok else "Router DNS self-heal FAILED",
                f"router DNS had drifted (reboot/reset?) — re-pointed at "
                f"{me} automatically (ok={ok})")
        except Exception:
            traceback.print_exc()

    def _my_lan_ip(self) -> str:
        """This machine's LAN IP towards the router (UDP connect — no packets)."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((self.cfg.get("router_ip") or "192.168.0.1", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except OSError:
            return ""

    def _lift_soft_denies(self):
        """Portal just armed -> self-service recovery must work NOW: lift the
        cmd 278 deny of every device that isn't manually blocked (Devices
        tab). gate_deny has only two callers — the exhaustion path and the
        manual block — so "denied minus manual" is exactly ours to lift; entries
        written from the vendor UI are left untouched when they match a manual
        block, otherwise they are re-admitted too (the admin just armed the
        portal: holding MACs off the SSID has no recovery path left).
        """
        try:
            if self.router is None or not self.router.logged_in:
                return
            manual = {r["mac"] for r in
                      store.q("SELECT mac FROM devices WHERE blocked=1")}
            st = voucher.gate_status(self.router)
            denied = set()
            for band in (st or {}).values():
                if isinstance(band, dict) and band.get("mode") == "deny":
                    denied |= set(band.get("macs") or [])
            for mac in sorted(denied - manual):
                res = voucher.gate_allow(self.router, mac)
                store.add_alert("info", "Portal arm: device re-admitted",
                                f"{mac} unblocked so it can rejoin and redeem "
                                f"a voucher (ok={bool((res or {}).get('ok'))})")
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
