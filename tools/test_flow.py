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


if __name__ == "__main__":
    test_extract_devices()
    test_parse_usage()
    test_risk()
    seed_poller_pass()
    test_dns_parser()
    test_sites_flow()
    test_base_paywall()
    test_web_api()
    test_default_deny()
    test_portal_arm_guards()
    print("\nALL SYNTHETIC TESTS PASSED")
