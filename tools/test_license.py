"""End-to-end tests for the paid-feature licence gate — no router contact.

Validates: locked paywall state, 403 on voucher APIs, unlock-code flow,
device binding (a foreign code / tampered record re-locks), and the
grandfather rule for installs that already used vouchers.
"""
import os
import sys
import tempfile
import unittest.mock as mock

# MUST run with an isolated data dir (same rule as test_flow.py).
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="odu-test-lic-")

sys.path.insert(0, ".")

from odu_tracker import store  # noqa: E402
from odu_tracker import license as lic  # noqa: E402
from odu_tracker.config import Config  # noqa: E402

store.init()


def _app():
    """A fresh app + authed test client (login mocked like test_flow.py)."""
    from odu_tracker.poller import Poller
    from odu_tracker.web.app import create_app
    cfg = Config()
    app = create_app(cfg, Poller(cfg))
    c = app.test_client()
    with mock.patch("odu_tracker.web.app.OduRouter.login",
                    return_value={"ok": 1}):
        r = c.post("/api/login", json={"password": "anything"})
        assert r.get_json().get("ok"), r.get_json()
    return cfg, c


def test_locked_by_default():
    cfg, c = _app()
    j = c.get("/api/license").get_json()
    assert j["locked"] is True and j["kind"] is None, j
    assert j["device_code"].startswith("ODU-"), j
    assert j["contact_email"] == lic.CONTACT_EMAIL, j
    # voucher APIs gated
    r = c.get("/api/vouchers")
    assert r.status_code == 403 and r.get_json()["error"] == "voucher_locked"
    r = c.post("/api/vouchers", json={"count": 1, "quota_gb": 5})
    assert r.status_code == 403, r.status_code
    r = c.post("/api/voucher/claim", json={"code": "whatever"})
    assert r.status_code == 403, r.status_code
    # unrelated APIs untouched
    r = c.get("/api/events")
    assert r.status_code == 200, r.status_code
    print("locked by default   ok")


def test_unlock_flow():
    cfg, c = _app()
    j = c.get("/api/license").get_json()
    assert j["locked"] is True
    # wrong code refused (bad length and bad value)
    r = c.post("/api/license/unlock", json={"code": "short"})
    assert r.status_code == 400, r.get_json()
    r = c.post("/api/license/unlock",
               json={"code": "AAAA-BBBB-CCCC-DDDD"})
    assert r.status_code == 400 and "invalid" in r.get_json()["error"]
    assert c.get("/api/license").get_json()["locked"] is True
    # the real code (pasted with lowercase + no dashes = paste-friendly)
    good = lic.expected_unlock_code()
    sloppy = good.replace("-", "").lower()
    r = c.post("/api/license/unlock", json={"code": sloppy})
    assert r.get_json().get("locked") is False, r.get_json()
    assert c.get("/api/license").get_json()["kind"] == "paid"
    r = c.get("/api/vouchers")
    assert r.status_code == 200, r.status_code
    assert "vouchers" in r.get_json()
    print("unlock flow         ok")


def test_device_binding():
    """A licence record copied from another device (or tampered) re-locks."""
    cfg, c = _app()
    good = lic.expected_unlock_code()
    c.post("/api/license/unlock", json={"code": good})
    assert lic.is_unlocked(cfg)
    # simulate data-file copy to a different device: patch the fingerprint
    real = lic.device_code
    lic.device_code = lambda: "ODU-FFFF-FFFF-FFFF"
    try:
        assert not lic.is_unlocked(cfg)          # stored code no longer matches
        st = lic.status(cfg)
        assert st["locked"] is True and st["device_code"].startswith("ODU-")
    finally:
        lic.device_code = real
    assert lic.is_unlocked(cfg)                  # back on the real device
    print("device binding      ok")


def test_grandfather():
    """An install that already used vouchers is auto-unlocked, bound to
    this device; a fresh install with empty tables is not."""
    # clear any licence left over by earlier tests in this shared data dir
    Config().set(lic.CFG_KEY, None)
    # prior activity: a generated voucher row in the DB
    store.exec("INSERT INTO vouchers(code, quota_mb, note, created_at) "
               "VALUES(?,?,?,?)", ("test-old-code", 20480, "", store.now()))
    cfg, c = _app()
    j = c.get("/api/license").get_json()
    assert j["locked"] is False and j["kind"] == "grandfathered", j
    assert j["device_code"] == lic.device_code()
    assert c.get("/api/vouchers").status_code == 200
    print("grandfather rule    ok")


def test_switch():
    """Remote kill-switch: pauses even a valid licence; bans specific
    device codes; un-pausing restores access. Fully offline."""
    cfg, c = _app()
    c.post("/api/license/unlock", json={"code": lic.expected_unlock_code()})
    assert lic.is_unlocked(cfg)
    # 1) global pause (cached policy, as after a fetch)
    cfg.set(lic.SWITCH_CFG_KEY,
            {"disabled": True, "banned": [], "message": "maintenance",
             "checked_at": store.now()})
    assert not lic.is_unlocked(cfg)
    j = c.get("/api/license").get_json()
    assert j["locked"] is True and j["paused"] == "maintenance", j
    r = c.post("/api/license/unlock", json={"code": "whatever"})
    assert r.status_code == 400 and "paused" in r.get_json()["error"]
    # 2) un-pause restores
    cfg.set(lic.SWITCH_CFG_KEY,
            {"disabled": False, "banned": [], "message": "",
             "checked_at": store.now()})
    assert lic.is_unlocked(cfg)
    assert c.get("/api/license").get_json()["paused"] is None
    # 3) this device banned by device code
    cfg.set(lic.SWITCH_CFG_KEY,
            {"disabled": False, "banned": [lic.device_code()],
             "message": "", "checked_at": store.now()})
    assert not lic.is_unlocked(cfg)
    # 4) refresh_switch parses a real policy file (file:// URL, offline)
    import tempfile
    pol = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    pol.write('{"disabled": false, "banned": ["ODU-0000-0000-0000"]}')
    pol.close()
    cfg.set("license_switch_url", "file://" + pol.name)
    changed = lic.refresh_switch(cfg)
    assert changed is True, changed         # ban list differs from step 2
    p = lic.switch_policy(cfg)
    assert p["banned"] == ["ODU000000000000"], p
    assert lic.is_unlocked(cfg)             # our device is not in the ban list
    print("kill-switch         ok")


if __name__ == "__main__":
    test_locked_by_default()
    test_unlock_flow()
    test_device_binding()
    test_grandfather()
    test_switch()
    print("all licence tests passed")
