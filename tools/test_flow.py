"""Synthetic end-to-end tests - no router contact required.

Mocks the router layer and validates: device ingestion, risk scoring,
usage series, web API with auth, DNS proxy parsing.
"""
import json
import os
import sys
import tempfile
import time
import unittest.mock as mock

# MUST run with an isolated data dir so a mocked login never touches
# the user's real stored password.
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="odu-test-data-")

sys.path.insert(0, ".")

from odu_tracker import store  # noqa: E402
from odu_tracker.config import Config  # noqa: E402
from odu_tracker.poller import Poller, extract_devices, parse_usage  # noqa: E402
from odu_tracker.security import normalize_mac, risk_score, vendor_for  # noqa: E402

init = store.init()

# the 24h trial ships armed against the LIVE endpoint - tests must never
# reach production (or open trial gates that break the paywall asserts)
from odu_tracker import config as _config  # noqa: E402
_config.DEFAULTS["trial_api_url"] = ""
# payment verification likewise ships pointed at the live site endpoint -
# the paywall tests drive the direct path with a fake secret, so disarm
_config.DEFAULTS["paystack_verify_url"] = ""

# start each test run from a clean slate
store.exec("DELETE FROM devices")
store.exec("DELETE FROM events")
store.exec("DELETE FROM alerts")
store.exec("DELETE FROM usage_samples")
store.exec("DELETE FROM sites")


def test_extract_devices():
    resp = {"data": {"dhcp_list_info": [
        {"mac": "AA:BB:CC:11:22:33", "ip": "192.168.0.101", "interface": "wlan1",
         "hostname": "Ade-phone"},
        {"mac": "aabbcc112234", "ip": "192.168.0.102", "interface": "eth",
         "hostname": "Desktop"},
    ]}}
    devs = extract_devices(resp)
    assert len(devs) == 2, devs
    assert devs[0]["mac"] == "AA:BB:CC:11:22:33"
    assert devs[1]["mac"] == "AA:BB:CC:11:22:34"
    print("extract_devices      ok")


def test_parse_usage():
    # real live shape: cmd 337 flat - monthly counters in MB
    u = parse_usage({"success": True, "cmd": 337,
                     "mon_download_flow": "103333.04", "dl_mon_flow": "93989.35",
                     "ul_mon_flow": "9343.69", "limitSize": "100", "flow_limit_unit": "1",
                     "limitSwitch": "0"})
    assert u and abs(u["used"] - 100.91) < 0.05 and u["total"] == 100.0, u
    u2 = parse_usage({"success": False, "cmd": 355, "error": "Non-JSON response"})
    assert u2 is None
    print("parse_usage          ok", u["used"], "GB of", u["total"], "GB")


def test_risk():
    score, level, reasons = risk_score(
        {"mac": "AA:BB:CC:11:22:33", "hostname": "kali.org", "iface": "wlan1"},
        {"is_new": True, "blocked_before": True},
    )
    assert level == "high", (score, level, reasons)
    sc2, lv2, _ = risk_score(
        {"mac": "50:C2:75:00:00:01", "hostname": "Ade-phone", "iface": "wlan1"},
        {"is_new": False, "trusted": True},
    )
    assert lv2 in ("low", "unknown"), (sc2, lv2)
    print("risk_score           ok", "(high:", score, "| low:", sc2, ")")


def test_zte_driver():
    """FibreX / ZTE LuQUID driver: page parsing, snapshot contract, gates.

    Fixtures are shaped from the real F6600P DOM contract (template_ divs,
    MACAddress:/IPAddress: spans, <span class="form-action"> endpoints,
    ajax_response_xml_root XML) - no router contact.
    """
    from odu_tracker.router import (OduRouter, RouterError, open_router,
                                    probe_kind)
    from odu_tracker.router_zte import (ZteRouter, _devices_from_scan,
                                        _devices_from_xml, _field_of,
                                        _parse_page, parse_luquid)

    # ---- field classification: indexed span ids AND bare XML ParaNames
    assert _field_of("MACAddress:3") == ("mac", "3")
    assert _field_of("MACAddress") == ("mac", "")
    assert _field_of("InBytes") == ("bytes", "")
    assert _field_of("HostName_7")[0] == "host"
    assert _field_of("IPAddress")[0] == "ip"
    assert _field_of("Rssi_5G") == (None, None)

    # ---- page -> devices (the dashboard's template-block rendering)
    lan_page = """
    <html><head><title>LAN</title></head><body>
    <div id="lan_info">
      <span class="form-action">/common_page/lan_info_lua.lua</span>
      <div id="template_lan_info_0">
        <span id="MACAddress:0">AA:BB:CC:11:22:33</span>
        <span id="IPAddress:0">192.168.1.50</span>
        <span id="HostName:0">Ade-phone</span>
        <span id="InBytes:0">123456/65432</span>
      </div>
      <div id="template_lan_info_1">
        <span id="MACAddress:1">40:ec:99:aa:bb:01</span>
        <span id="IPAddress:1">192.168.1.51</span>
        <span id="HostName:1">Desktop</span>
        <span id="InBytes:1">1000/2000</span>
      </div>
      <div id="template_lan_info_2">
        <span id="MACAddress:2">not-a-mac</span>
        <span id="IPAddress:2">192.168.1.52</span>
      </div>
    </div></body></html>
    """
    devs = _devices_from_scan(_parse_page(lan_page), "lan")
    assert len(devs) == 2, devs          # the malformed row is dropped
    assert devs[0]["mac"] == "AA:BB:CC:11:22:33"
    assert devs[0]["ip"] == "192.168.1.50"
    assert devs[0]["hostname"] == "Ade-phone"
    assert devs[0]["iface"] == "lan"
    assert devs[0]["rx"] == 123456.0 and devs[0]["tx"] == 65432.0

    # ---- table fallback (some firmwares render DHCP leases as a table)
    tbl_page = """
    <div id="DHCPHostInfo"><table>
    <tr><th>IP</th><th>MAC</th><th>Host</th></tr>
    <tr><td>192.168.1.60</td><td>AA:BB:CC:11:22:44</td><td>Printer</td></tr>
    </table></div>
    """
    tdevs = _devices_from_scan(_parse_page(tbl_page), "")
    assert len(tdevs) == 1, tdevs
    assert tdevs[0]["mac"] == "AA:BB:CC:11:22:44"
    assert tdevs[0]["ip"] == "192.168.1.60"
    assert tdevs[0]["hostname"] == "Printer"

    # ---- XML data endpoint -> devices
    xml = """<ajax_response_xml_root>
    <OBJ_Wlan_ClientStat_ID>
    <Instance>
      <ParaName>MACAddress</ParaName><ParaValue>40:EC:99:AA:BB:02</ParaValue>
      <ParaName>IPAddress</ParaName><ParaValue>192.168.1.70</ParaValue>
      <ParaName>HostName</ParaName><ParaValue>Tablet</ParaValue>
      <ParaName>InBytes</ParaName><ParaValue>5000/6000</ParaValue>
    </Instance>
    </OBJ_Wlan_ClientStat_ID>
    </ajax_response_xml_root>"""
    parsed = parse_luquid(xml)
    assert "OBJ_Wlan_ClientStat_ID" in parsed, parsed
    xdevs = _devices_from_xml(parsed, "wifi")
    assert len(xdevs) == 1, xdevs
    assert xdevs[0]["mac"] == "40:EC:99:AA:BB:02"
    assert xdevs[0]["hostname"] == "Tablet"
    assert xdevs[0]["rx"] == 5000.0 and xdevs[0]["tx"] == 6000.0

    # ---- snapshot_all must satisfy the poller's extraction contract
    class FakeZte(ZteRouter):
        """Driver with its HTTP layer replaced by fixtures."""

        def __init__(self):
            super().__init__(password="x")
            self._pages = {"lan": "/lan", "wlan": "/wlan", "dhcp": "/dhcp",
                           "topology": "/topo", "home": "/home"}
            self._discovered = True

        def _page_endpoints(self, role):
            # non-empty endpoint list is what makes _collect_devices visit
            # the role at all (a None page alone is skipped)
            return {"lan": lan_page, "dhcp": tbl_page}.get(role), \
                ["/ep/" + role]

        def _fetch_endpoint(self, ep, referer_url):
            return xml if referer_url == "/wlan" else None

        def _ensure(self):
            pass

        def _home_info(self):
            return {"data": {"ModelName": "ZXHN F6600P",
                             "SerialNumber": "ZTE123456"}}

    z = FakeZte()
    snap = z.snapshot_all()
    assert set(snap) >= {"all_device", "home_info", "flow_new"}, snap.keys()
    got = extract_devices(snap["all_device"])
    macs = {d["mac"] for d in got}
    assert {"AA:BB:CC:11:22:33", "40:EC:99:AA:BB:01",
            "AA:BB:CC:11:22:44", "40:EC:99:AA:BB:02"} <= macs, macs
    u = parse_usage(snap["flow_new"])
    assert u and u["total"] is None, u      # FibreX is unlimited: no quota
    assert u["used"] >= 0, u
    # bytes counted exactly once: 123456+65432 + 1000+2000 + 5000+6000
    expect_mb = (123456 + 65432 + 1000 + 2000 + 5000 + 6000) / 1048576.0
    assert abs(snap["flow_new"]["mon_download_flow"] - expect_mb) < 0.01, \
        snap["flow_new"]
    # peak-hold: a device leaving must not walk the counter backwards
    z._usage_peak_mb = 99.0
    assert z.snapshot_all()["flow_new"]["mon_download_flow"] >= 99.0
    z.reset_usage()
    assert z.current_usage_mb() == 0.0

    # ---- ZLT-only API is refused loudly, not silently wrong
    for fn in (lambda: z.cmd(3, "GET"), lambda: z.cmd(278, "GET"),
               lambda: z.build(232, "GET")):
        try:
            fn()
            raise AssertionError("ZTE driver must refuse ZLT commands")
        except RouterError:
            pass

    # ---- capability flags drive the gates
    assert z.kind == "zte" and z.features["vouchers"] is False
    assert OduRouter.kind == "zlt" and OduRouter.features["vouchers"] is True

    cfg = Config()
    p = Poller(cfg)
    p.router = z
    assert p.feat("vouchers") is False and p.feat("portal") is False
    res = p.push_dhcp_dns("192.168.1.99", "192.168.1.99")
    assert res["ok"] is False and "ZLT" in res["error"], res
    p.router = OduRouter(password="x")
    assert p.feat("vouchers") is True

    # ---- factory + fingerprint
    assert isinstance(open_router(ip="192.168.1.1", kind="zte"), ZteRouter)
    assert isinstance(open_router(ip="192.168.0.1", kind="zlt"), OduRouter)
    with mock.patch("odu_tracker.router.probe_kind",
                    return_value=(True, "zte")):
        assert isinstance(open_router(ip="192.168.1.1"), ZteRouter)
    with mock.patch("socket.create_connection") as cc, \
            mock.patch("odu_tracker.router.OduRouter.build",
                       side_effect=RuntimeError("not zlt")), \
            mock.patch("odu_tracker.router.requests.get") as rg:
        cc.return_value.close = lambda: None
        rg.return_value.text = '<html>var x = {_sessionTOKEN", "abc123"}</html>'
        assert probe_kind("192.168.1.1") == (True, "zte")
    with mock.patch("socket.create_connection") as cc, \
            mock.patch("odu_tracker.router.OduRouter.build",
                       return_value={"success": "1"}):
        cc.return_value.close = lambda: None
        assert probe_kind("192.168.0.1") == (True, "zlt")
    with mock.patch("socket.create_connection") as cc:
        cc.return_value.close = lambda: None
        assert probe_kind("192.168.1.1", known="zte") == (True, "zte")

    # ---- /api/meta capability flags: an UNKNOWN kind must keep FULL
    # features. Mobile has no "no ODU" gate (desktop does), so sending
    # false/true/false here silently hides Block + Soft-cut from an ODU
    # user whose router has not been fingerprinted yet.
    #
    # trial_api_url must be EMPTY (disarmed), not an unreachable host:
    # refresh() treats any non-empty URL as armed, and on failure
    # trial._bootstrap_local() PERSISTS a 24h trial into the shared config
    # file, which would hand test_base_paywall a live trial and turn its
    # expected 403 into a 200.
    from odu_tracker.web.app import create_app
    mcfg = Config()
    mcfg.set("router_kind", "", persist=False)
    mcfg.set("license_switch_url", "http://127.0.0.1:9/nope", persist=False)
    mcfg.set("trial_api_url", "", persist=False)
    mcfg.set("trial", None, persist=False)     # never leave a minted record
    mc = create_app(mcfg, Poller(mcfg)).test_client()

    def _meta(kind):
        with mock.patch("socket.create_connection") as cc, \
                mock.patch("odu_tracker.web.app.probe_kind",
                           return_value=(True, kind)):
            cc.return_value.close = lambda: None
            return mc.get("/api/meta").get_json()

    full = {"zlt": True, "vouchers": True, "portal": True}
    off = {"zlt": False, "vouchers": False, "portal": False}
    unknown = _meta("")
    assert unknown["features"] == full, unknown["features"]
    assert unknown["router_kind"] == "", unknown["router_kind"]
    assert _meta("zlt")["features"] == full, _meta("zlt")["features"]
    zte = _meta("zte")
    assert zte["features"] == off, zte["features"]
    assert zte["router_kind"] == "zte", zte["router_kind"]

    # ---- discover() must not re-enter itself. A slow GPON ONT needs
    # ~105 s for the 70-page crawl, past the 60-120 s idle session, so the
    # mid-crawl bounce reaches login() -> discover() again. Guarded, that
    # inner call must return at once instead of nesting a second crawl.
    zr = ZteRouter(ip="192.168.1.1")
    crawls = {"n": 0}

    def _nesting_crawl(cap):
        crawls["n"] += 1
        zr.discover()                      # the old code re-entered here
    zr._crawl = _nesting_crawl
    zr.discover()
    assert crawls["n"] == 1, f"discover re-entered {crawls['n']} times"
    assert zr._discovering is False, "guard flag must clear after the crawl"

    # a crawl that dies must leave _discovered False so it retries later
    zr2 = ZteRouter(ip="192.168.1.1")

    def _dies(cap):
        raise RuntimeError("router went away mid-crawl")
    zr2._crawl = _dies
    try:
        zr2.discover()
    except RuntimeError:
        pass
    assert zr2._discovered is False, "a failed crawl must stay retryable"
    assert zr2._discovering is False, "guard flag must clear on failure"

    print("zte driver           ok (page/XML parse, snapshot contract,\n"
          "                            byte accounting, gates, factory,\n"
          "                            meta defaults, no re-entrancy)")


def seed_poller_pass():
    """Run Poller.fast_poll against a fake router; verify Store rows."""
    cfg = Config()
    poller = Poller(cfg)
    from odu_tracker.router import OduRouter
    poller.router = OduRouter(password="x")
    fake = {
        "all_device": {"success": True, "dhcp_list_info": [
            {"mac": "AA:BB:CC:11:22:33", "ip": "192.168.0.101", "interface": "wlan",
             "hostname": "Ade-phone"},
            {"mac": "40:ec:99:aa:bb:01", "ip": "192.168.0.113", "interface": "wlan",
             "hostname": "USB-kali"},
        ]},
        "flow": {"success": True, "mon_download_flow": "10333.00", "limitSize": "100",
                 "flow_limit_unit": "1", "dl_mon_flow": "9000.00", "ul_mon_flow": "1333.00",
                 "limitSwitch": "1"},
        "sys_status": {"success": True, "signal_lvl": "4", "network_type_str": "5G(NSA)",
                       "sim_status": "1", "network_operator": "MTN-NG"},
        "battery": {"success": True},
    }
    with mock.patch.object(type(poller.router), "snapshot_all", return_value=fake):
        poller.fast_poll()
    devs = store.q("SELECT * FROM devices ORDER BY mac")
    assert len(devs) == 2, devs
    kal = [d for d in devs if d["last_hostname"] == "USB-kali"][0]
    assert kal["risklevel"] == "high", (kal["risklevel"], json.loads(kal["riskreasons"]))
    alerts = store.recent_alerts()
    assert any("New device" in a["title"] for a in alerts)
    assert any("Dangerous" in a["title"] or "Suspicious" in a["title"] for a in alerts), alerts
    usage = store.current_usage()
    assert usage and abs(usage["used"] - 10.09) < 0.1, usage
    # second poll with a higher counter -> rate sample should appear
    fake["flow"]["mon_download_flow"] = "10349.00"
    with mock.patch.object(type(poller.router), "snapshot_all", return_value=fake):
        time.sleep(2.2)
        poller.fast_poll()
    assert poller.current_rate and poller.current_rate > 0, poller.current_rate
    print("poller -> store      ok  (devices, alerts, usage %.2f GB, avg %.2f Mbps)"
          % (usage["used"], poller.current_rate))


def test_dns_parser():
    import struct
    from odu_tracker.dns_proxy import build_portal_reply, parse_query_name
    # A query for example.com
    qname = b"\x07example\x03com\x00"
    dom, end = parse_query_name(qname, 0)
    assert dom == "example.com", dom
    # Denied clients: A is the portal, HTTPS/SVCB (65) must not carry a hint.
    def query(qtype):
        return (b"\x12\x34" + struct.pack(">HHHHHH", 0x0100, 1, 0, 0, 0, 0)
                + qname + struct.pack(">HH", qtype, 1))
    a = build_portal_reply(query(1), 1, "192.168.0.50")
    assert a[2:4] == struct.pack(">H", 0x8180)
    assert struct.unpack(">H", a[6:8])[0] == 1          # one answer
    assert a.endswith(bytes(map(int, "192.168.0.50".split("."))))
    https = build_portal_reply(query(65), 65, "192.168.0.50")
    assert struct.unpack(">H", https[6:8])[0] == 0      # no answers, no hint
    assert b"192.168.0.50" not in https and len(https) < len(query(65)) + 16
    print("dns name parser      ok")


def test_web_api():
    cfg = Config()
    from odu_tracker.web.app import create_app
    poller = Poller(cfg)
    app = create_app(cfg, poller)
    c = app.test_client()
    cfg.set("app_license", None)              # v1.5.0: start unpaid
    r = c.get("/api/devices")
    assert r.get_json().get("error") == "auth_required"
    # fake a successful router login by patching OduRouter.login
    with mock.patch("odu_tracker.web.app.OduRouter.login", return_value={"ok": 1}):
        r = c.post("/api/login", json={"password": "anything"})
        assert r.get_json().get("ok"), r.get_json()
    # v1.5.0: signed in, but every data route stays dark until the
    # ₦2,500 unlock is in place (paid_required, not auth_required)
    r = c.get("/api/status")
    assert r.status_code == 403 and r.get_json()["error"] == "paid_required", \
        (r.status_code, r.get_json())
    from odu_tracker import app_license
    r = c.post("/api/applicense/activate",
               json={"code": app_license.expected_code()})
    assert r.get_json().get("paid"), r.get_json()
    r = c.get("/api/status")
    j = r.get_json()
    assert "usage" in j and "sys" in j, j.keys()
    r = c.get("/api/devices")
    j = r.get_json()
    assert j["count"] == 2, j["count"]
    kal = [x for x in j["devices"] if x["last_hostname"] == "USB-kali"][0]
    assert kal["risklevel"] == "high"
    # block/unblock flow (stores only; router part may fail harmlessly)
    with mock.patch("odu_tracker.web.app.OduRouter.cmd",
                    return_value={"message": "", "data": {"datas": []}}):
        r = c.post("/api/devices/AA:BB:CC:11:22:33/block", json={"blocked": True})
        assert r.get_json()["db"] is True, r.get_json()
    r = c.get("/api/usage?hours=2")
    assert "series" in r.get_json()
    r = c.get("/api/events")
    assert r.get_json()["events"]
    r = c.get("/api/sites?hours=6")
    j = r.get_json()
    assert {"domains", "clients", "url_rules"} <= set(j.keys())
    print("web api + auth       ok")


def test_sites_flow():
    ts = store.now()
    store.add_site(ts - 30, "192.168.0.101", None, "www.google.com")
    store.add_site(ts - 20, "192.168.0.101", None, "mail.google.com")
    store.add_site(ts - 10, "192.168.0.113", None, "www.youtube.com")
    s = store.site_summary(hours=1)
    domains = {x["domain"] for x in s}
    assert len(s) == 3 and {"www.google.com", "mail.google.com", "www.youtube.com"} == domains, s
    print("sites storage        ok")


def test_default_deny():
    """New device on the LAN = NO internet until it claims a voucher.

    The product rule under test: with the portal armed, every resolvable
    device WITHOUT an active claim is denied (voucher.deny_checker -> the
    DNS proxy answers every lookup with the claim page), and claiming,
    revoking and exhaustion all flip the verdict the right way.
    """
    from odu_tracker import voucher
    voucher.init()
    mac = "DE:AD:BE:EF:00:01"
    ip = "198.51.100.77"           # TEST-NET-2: never in this box's ARP table
    portal_host = "192.168.0.168"  # exempt: the host serving the claim page
    router_ip = "192.168.0.1"      # exempt: infrastructure
    store.exec("DELETE FROM vouchers")
    store.exec("DELETE FROM voucher_claims")
    store.exec("DELETE FROM voucher_usage")
    store.exec("DELETE FROM devices WHERE mac=?", (mac,))
    voucher.invalidate_deny_cache()
    voucher.invalidate_usage_cache()
    store.exec("INSERT INTO devices(mac, first_seen, last_seen, last_ip) "
               "VALUES(?,?,?,?)", (mac, store.now(), store.now(), ip))
    deny = voucher.deny_checker(exempt_ips=(portal_host, router_ip))
    try:
        # 1. brand-new device, no claim -> DENIED (the default)
        assert deny(ip) is True, "unclaimed device must be denied"
        # 2. portal host / router are never hijacked
        assert deny(portal_host) is False, "portal host must be exempt"
        assert deny(router_ip) is False, "router must be exempt"
        # 3. a device we cannot identify yet fails OPEN (documented: it is
        #    caught on a later query once ARP/poller resolve its MAC)
        assert deny("198.51.100.200") is False, "unknown MAC fails open"
        # 4. claim -> allowed within seconds
        [code] = voucher.generate(1, quota_mb=1024, note="deny-test")
        voucher.claim(code, mac=mac, ip=ip, router=None)
        assert deny(ip) is False, "claimed device must pass"
        # 5. revoke -> straight back to the portal
        voucher.revoke(code)
        voucher.invalidate_deny_cache()
        assert deny(ip) is True, "revoked claim must lose access"
        # 6. claimed but quota burnt -> exhausted -> portal again
        [code2] = voucher.generate(1, quota_mb=1024, note="deny-test")
        voucher.claim(code2, mac=mac, ip=ip, router=None)
        assert deny(ip) is False
        voucher.set_device_used_mb(mac, voucher.device_quota_mb(mac))
        voucher.invalidate_usage_cache()
        assert deny(ip) is True, "exhausted device must be denied"
        print("default-deny policy  ok  (new->portal, claim->pass, "
              "revoke/exhaust->portal)")
    finally:
        store.exec("DELETE FROM vouchers")
        store.exec("DELETE FROM voucher_claims")
        store.exec("DELETE FROM voucher_usage")
        store.exec("DELETE FROM devices WHERE mac=?", (mac,))
        voucher.invalidate_deny_cache()
        voucher.invalidate_usage_cache()


def test_portal_arm_guards():
    """Arming must never claim success it can't honor.

    Regression guards for the "APK portal doesn't activate" incident:
      (a) Android refuses to arm - it cannot bind :53/:80 - instead of
          persisting portal_enforced and pointing the LAN's DNS at a phone
          that serves nothing (total lockout, claim page unreachable);
      (b) a stack that failed to bind refuses to arm and NEVER moves the
          router's DHCP DNS to a dead resolver;
      (c) a healthy stack arms, persists the flag and reports the
          http:// claim-page URL (the portal is HTTP-only - no :443).
    """
    import socket as _s
    from odu_tracker.web import app as webapp

    cfg = Config()
    cfg.set("portal_enforced", False, persist=False)
    poller = Poller(cfg)
    app = webapp.create_app(cfg, poller)
    c = app.test_client()
    with mock.patch("odu_tracker.web.app.OduRouter.login", return_value={"ok": 1}):
        r = c.post("/api/login", json={"password": "x"})
        assert r.get_json().get("ok"), r.get_json()

    pushed = []
    poller.push_dhcp_dns = lambda m, v: pushed.append((m, v)) or {"ok": True}

    # (a) the phone may never arm
    with mock.patch.object(webapp, "on_android", return_value=True):
        r = c.post("/api/portal/enforce", json={"enabled": True})
    j = r.get_json()
    assert r.status_code == 501, (r.status_code, j)
    assert "phone" in j["error"] and "desktop" in j["error"], j
    assert cfg.get("portal_enforced") is False, "android must not persist armed"
    assert pushed == [], "android must not move router DNS"

    # (b) bind failure -> refuse to arm, router DNS untouched
    blocker_u = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    blocker_t = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    blocker_u.bind(("0.0.0.0", 0))
    blocker_t.bind(("0.0.0.0", 0))
    blocker_t.listen(1)
    saved = (cfg.get("dns_proxy_port"), cfg.get("portal_port"))
    try:
        cfg.set("dns_proxy_port", blocker_u.getsockname()[1], persist=False)
        cfg.set("portal_port", blocker_t.getsockname()[1], persist=False)
        r = c.post("/api/portal/enforce", json={"enabled": True})
        j = r.get_json()
        assert r.status_code == 503, (r.status_code, j)
        assert "bind" in (j.get("error") or ""), j
        assert cfg.get("portal_enforced") is False, "bind fail must not persist"
        assert pushed == [], "router DNS must not move to a dead resolver"
    finally:
        blocker_u.close()
        blocker_t.close()
        cfg.set("dns_proxy_port", saved[0], persist=False)
        cfg.set("portal_port", saved[1], persist=False)

    # (c) healthy stack on free high ports (tests never touch :53/:80)
    u = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    t = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    u.bind(("0.0.0.0", 0))
    t.bind(("0.0.0.0", 0))
    u_port, t_port = u.getsockname()[1], t.getsockname()[1]
    u.close()
    t.close()                         # release so the app can bind them
    try:
        cfg.set("dns_proxy_port", u_port, persist=False)
        cfg.set("portal_port", t_port, persist=False)
        r = c.post("/api/portal/enforce", json={"enabled": True})
        j = r.get_json()
        assert r.status_code == 200 and j.get("ok"), (r.status_code, j)
        assert cfg.get("portal_enforced") is True, "healthy arm must persist"
        assert str(j.get("portal_url", "")).startswith("http://"), j
        r = c.post("/api/portal/enforce", json={"enabled": False})
        j = r.get_json()
        assert j.get("ok"), j
        assert cfg.get("portal_enforced") is False, "disarm must clear flag"
    finally:
        cfg.set("dns_proxy_port", saved[0], persist=False)
        cfg.set("portal_port", saved[1], persist=False)
        from odu_tracker import dns_proxy as _dp
        if _dp._singleton:
            _dp._singleton.stop()
    print("portal arm guards    ok  (android=refused, bind-fail=refused, "
          "healthy arm reports http:// URL)")


def test_base_paywall():
    """v1.5.0 - the ₦2,500 unlock: gate, Paystack verify, reinstall code."""
    from odu_tracker import app_license, paystack
    cfg = Config()
    cfg.set("app_license", None)              # start from a clean slate
    from odu_tracker.web.app import create_app
    app = create_app(cfg, Poller(cfg))
    c = app.test_client()

    m = c.get("/api/meta").get_json()
    assert m["paid"] is False and m["price_label"] == "₦2,500", m

    st = c.get("/api/applicense").get_json()
    assert st["paid"] is False and st["unlock_code"] is None, st
    # pre-payment the device code may show, the unlock code must NOT
    assert st["device_code"].startswith("ODU-"), st
    assert st["price_kobo"] == 250000 and st["paystack"]["public_key"], st

    # signed in but unpaid => nothing to read
    with mock.patch("odu_tracker.web.app.OduRouter.login", return_value={"ok": 1}):
        c.post("/api/login", json={"password": "x"})
    r = c.get("/api/devices")
    assert r.status_code == 403 and r.get_json()["error"] == "paid_required", \
        (r.status_code, r.get_json())

    # bad reference / failed charge never unlocks
    assert c.post("/api/applicense/verify",
                  json={"reference": "not a ref!"}).status_code == 400
    with mock.patch("odu_tracker.web.app.paystack.verify",
                    return_value=(False, {}, "payment is failed")):
        r = c.post("/api/applicense/verify", json={"reference": "OT123"})
        assert r.status_code == 400, r.get_json()
    assert app_license.is_paid(cfg) is False

    # a confirmed ₦2,500 charge activates and hands over the code
    info = {"reference": "OT123", "amount": 250000, "currency": "NGN",
            "email": "buyer@example.com"}
    with mock.patch("odu_tracker.web.app.paystack.verify",
                    return_value=(True, info, "")):
        r = c.post("/api/applicense/verify", json={"reference": "OT123"})
    j = r.get_json()
    assert r.status_code == 200 and j["paid"] is True, j
    assert j["unlock_code"] == app_license.expected_code(), j

    # amount/currency enforcement lives in paystack.verify itself
    cfg.set("paystack_secret_key", "sk_test_x", persist=False)
    with mock.patch("odu_tracker.paystack._get", return_value={
            "status": True, "data": {"status": "success", "amount": 10000,
                                     "currency": "NGN",
                                     "customer": {"email": "a@b.c"}}}):
        ok, _, err = paystack.verify(cfg, "REF123")
        assert not ok and "short" in err, (ok, err)
    with mock.patch("odu_tracker.paystack._get", return_value={
            "status": True, "data": {"status": "success", "amount": 250000,
                                     "currency": "NGN",
                                     "customer": {"email": "a@b.c"}}}):
        ok, pinfo, err = paystack.verify(cfg, "REF123")
        assert ok and pinfo["email"] == "a@b.c", (ok, err)

    # reinstall path: another device's code fails, this device's works
    cfg.set("app_license", None)
    r = c.post("/api/applicense/activate", json={"code": "0000-0000-0000-0000"})
    assert r.status_code == 400, r.get_json()
    r = c.post("/api/applicense/activate",
               json={"code": app_license.expected_code()})
    j = r.get_json()
    assert r.status_code == 200 and j["paid"] is True, j
    assert app_license.is_paid(cfg) is True

    # v1.5.0: the base unlock ALSO opens the Vouchers tab - one price,
    # every feature (a separately-sold pre-1.5.0 code would count too)
    j = c.get("/api/license").get_json()
    assert j["locked"] is False and j["kind"] == "included", j
    r = c.get("/api/vouchers")
    assert r.status_code != 403, (r.status_code, r.get_json())

    # force-update gate: remote min_version above the running version
    # flips /api/meta before login/paywall; unreachable switch URL so the
    # background refresher can't race this assertion
    cfg.set("license_switch_url", "http://127.0.0.1:9/nope", persist=False)
    cfg.set("voucher_switch", {"min_version": "99.0.0"}, persist=False)
    m = c.get("/api/meta").get_json()
    assert m["update"]["required"] is True and \
        m["update"]["latest"] == "99.0.0", m["update"]
    j = c.post("/api/updatecheck").get_json()
    assert j["update"]["required"] is True, j          # still forced
    cfg.set("voucher_switch", {"min_version": ""}, persist=False)
    m = c.get("/api/meta").get_json()
    assert m["update"]["required"] is False, m["update"]  # gate lifted
    cfg.set("voucher_switch", None, persist=False)

    print("base paywall         ok  (403 paid_required, verify, reinstall,\n"
          "                            vouchers included, update gate)")


def test_trial():
    """v1.5.0 - the 24h server-backed trial: pin, gate, expiry, anti-wipe."""
    from odu_tracker import app_license
    from odu_tracker import trial as app_trial
    cfg = Config()
    cfg.set("app_license", None)              # unpaid: the trial's world
    cfg.set("trial", None, persist=False)
    # no real network in tests: switch fetch stays unreachable
    cfg.set("license_switch_url", "http://127.0.0.1:9/nope", persist=False)
    from odu_tracker.web.app import create_app
    app = create_app(cfg, Poller(cfg))
    c = app.test_client()

    # disarmed (no endpoint): classic pay-on-first-launch, no record minted
    m = c.get("/api/meta").get_json()
    assert m["trial"]["active"] is False, m["trial"]
    assert not cfg.get("trial"), cfg.get("trial")     # never minted
    with mock.patch("odu_tracker.web.app.OduRouter.login",
                    return_value={"ok": 1}):
        c.post("/api/login", json={"password": "x"})
    r = c.get("/api/devices")
    assert r.status_code == 403 and \
        r.get_json()["error"] == "paid_required", (r.status_code, r.get_json())

    # armed but unreachable: a LOCAL 24h window opens (offline first run)
    cfg.set("trial_api_url", "http://127.0.0.1:9/nope", persist=False)
    app2 = create_app(cfg, Poller(cfg))
    c2 = app2.test_client()
    m = c2.get("/api/meta").get_json()
    t = m["trial"]
    assert t["active"] is True and t["remaining_s"] > 23 * 3600, t
    assert t["source"] == "local" and t["ended"] is False, t
    with mock.patch("odu_tracker.web.app.OduRouter.login",
                    return_value={"ok": 1}):
        c2.post("/api/login", json={"password": "x"})
    r = c2.get("/api/devices")                  # trial passes the paid gate
    assert r.status_code != 403, (r.status_code, r.get_json())
    r = c2.get("/api/vouchers")                 # ...including Vouchers
    assert r.status_code != 403, (r.status_code, r.get_json())

    # window runs out: the gate slams shut, paywall told "ended"
    rec = cfg.get("trial")
    rec["expires_at"] = int(time.time()) - 10
    cfg.set("trial", rec, persist=False)
    r = c2.get("/api/devices")
    j = r.get_json()
    assert r.status_code == 403 and j["error"] == "paid_required", j
    assert j["trial"]["ended"] is True and j["trial"]["active"] is False, j

    # clock wound back (last_seen in the future): permanently banned
    rec["expires_at"] = int(time.time()) + 12 * 3600
    rec["last_seen"] = int(time.time()) + 3600
    cfg.set("trial", rec, persist=False)
    assert app_trial.active(cfg) is False, cfg.get("trial")
    app_trial.refresh(cfg)                      # even offline the ban sticks
    assert cfg.get("trial").get("banned") == 1, cfg.get("trial")
    assert c2.get("/api/devices").status_code == 403

    # anti-reinstall: the SERVER's pinned record survives a local wipe
    cfg.set("trial", None, persist=False)       # "user cleared app data"
    pinned = {"started_at": 1700000000,
              "expires_at": 1700000000 + app_trial.TRIAL_S}
    with mock.patch.object(app_trial, "_post", return_value=pinned):
        app_trial.refresh(cfg)
    got = cfg.get("trial")
    assert got["started_at"] == pinned["started_at"], got
    assert got["source"] == "server" and "banned" not in got, got
    with mock.patch.object(app_trial, "_post", return_value=pinned):
        app_trial.refresh(cfg)                  # same window, every time
    assert cfg.get("trial")["started_at"] == pinned["started_at"]

    # paid users never touch the trial machinery
    cfg.set("app_license", {"code": app_license.expected_code()},
            persist=False)
    assert app_license.is_paid(cfg) is True
    before = dict(cfg.get("trial"))
    assert app_trial.refresh(cfg) is False, cfg.get("trial")
    assert cfg.get("trial") == before, cfg.get("trial")

    print("24h trial            ok  (disarmed, local pin, gate, expiry,\n"
          "                            clock ban, server anti-reinstall)")


if __name__ == "__main__":
    test_extract_devices()
    test_parse_usage()
    test_risk()
    test_zte_driver()
    seed_poller_pass()
    test_dns_parser()
    test_sites_flow()
    test_base_paywall()
    test_trial()
    test_web_api()
    test_default_deny()
    test_portal_arm_guards()
    print("\nALL SYNTHETIC TESTS PASSED")
