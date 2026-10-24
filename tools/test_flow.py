"""Synthetic end-to-end tests — no router contact required.

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
    from odu_tracker.dns_proxy import parse_query_name
    # A query for example.com
    data = bytes.fromhex("e3f80120000100000000000007657861")  # too short; build manually
    qname = b"\x07example\x03com\x00"
    dom, end = parse_query_name(qname, 0)
    assert dom == "example.com", dom
    print("dns name parser      ok")


def test_web_api():
    cfg = Config()
    from odu_tracker.web.app import create_app
    poller = Poller(cfg)
    app = create_app(cfg, poller)
    c = app.test_client()
    r = c.get("/api/devices")
    assert r.get_json().get("error") == "auth_required"
    # fake a successful router login by patching OduRouter.login
    with mock.patch("odu_tracker.web.app.OduRouter.login", return_value={"ok": 1}):
        r = c.post("/api/login", json={"password": "anything"})
        assert r.get_json().get("ok"), r.get_json()
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


if __name__ == "__main__":
    test_extract_devices()
    test_parse_usage()
    test_risk()
    seed_poller_pass()
    test_dns_parser()
    test_sites_flow()
    test_web_api()
    print("\nALL SYNTHETIC TESTS PASSED")
