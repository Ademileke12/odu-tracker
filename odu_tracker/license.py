r"""Offline access licence for the paid Voucher feature.

The Voucher tab (data codes, shared SIM pool, captive-portal gating) is a
paid feature. There is no payment system — the buyer chats with the author
(email / TikTok), pays directly, and receives an UNLOCK CODE that opens the
feature on their device:

  1. The app derives a DEVICE CODE from a stable per-device identity
     (Android ANDROID_ID / Windows MachineGuid / Linux machine-id).
     It survives reinstalls on the same device, and it changes on any
     other device — so a code is useless outside the one device.
  2. The buyer sends the device code to the author, who runs
     `tools/make_unlock_code.py` to mint the matching unlock code.
  3. The unlock code is verified OFFLINE (HMAC-SHA256 with a secret
     embedded in the app, obfuscated). No server, works on every
     platform the engine runs on (Windows / Linux / Android).

Anti-copy guarantees:
  * one code works on exactly one device (bound to its fingerprint);
  * copying the app or its data files to another device does NOT carry
    the unlock — the code is re-verified against the device fingerprint
    on every check, never trusted from storage;
  * entering a *different* device's code fails, verification always
    recomputes the expected code for the local device.

Trade-off (honest): the verification secret ships inside the app, so a
determined reverse-engineer could extract it and mint codes. Casual
sharing/copying — the realistic threat for direct sales — is fully
blocked. Rotate the secret for future releases if it ever leaks.
"""
import hashlib
import hmac
import json
import os
import re
import socket
import time
import urllib.request
import uuid
from pathlib import Path

from odu_tracker.config import on_android

CONTACT_EMAIL = "srimpkoyonko@gmail.com"
CONTACT_TIKTOK = "leke_abudu"

CFG_KEY = "voucher_license"

# Remote kill-switch (policy file deployed with the site). Lets the author
# pause the voucher feature or ban leaked device codes even on installs
# that are already out there. The app re-checks it at boot + every 30 min
# and keeps the LAST KNOWN policy when offline (sticky by design).
SWITCH_URL = ("https://raw.githubusercontent.com/Ademileke12/odu-tracker/"
              "main/site/voucher-switch.json")
SWITCH_CFG_KEY = "voucher_switch"

# Obfuscated HMAC secret (blob ^ mask). Rotate for a future release only
# if it leaks — rotating invalidates every unlock code issued before it.
_SECRET_BLOB = bytes.fromhex(
    "a268f4d9d01d54d37ab4c8362dfaabb296801d763d57662a643486bc0ef86199")
_SECRET_MASK = bytes.fromhex(
    "1e3eac4406507f7d5099b5576312bf29c014a53c7cacdffcffaa4e0023a55723")


def _secret() -> bytes:
    """The HMAC key, assembled from its two obfuscated halves."""
    return bytes(a ^ b for a, b in zip(_SECRET_BLOB, _SECRET_MASK))


def _version() -> str:
    try:
        from odu_tracker import __version__
        return __version__
    except Exception:
        return "?"


# ------------------------------------------------------------- device identity
def _device_identity() -> str:
    """A stable per-device string. Same device => same string, even after
    an app reinstall (that is the support-friendly half of the trade-off:
    a customer may reinstall, but not copy to another device)."""
    # 1) Android: ANDROID_ID — per device + app signing key, survives
    #    reinstalls, changes on factory reset.
    if on_android():
        try:
            from jnius import autoclass
            Activity = autoclass("org.kivy.android.PythonActivity")
            resolver = Activity.mActivity.getContentResolver()
            Secure = autoclass("android.provider.Settings$Secure")
            aid = Secure.getString(resolver, Secure.ANDROID_ID)
            if aid:
                return "android:" + aid
        except Exception:
            pass
    # 2) Windows: MachineGuid (per Windows installation)
    if os.name == "nt":
        try:
            import winreg
            try:
                access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
            except AttributeError:
                access = winreg.KEY_READ
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography",
                                0, access) as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
                if guid:
                    return "win:" + str(guid)
        except OSError:
            pass
    # 3) Linux: machine-id
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            s = Path(p).read_text(encoding="utf-8").strip()
            if s:
                return "linux:" + s
        except OSError:
            pass
    # 4) Fallback: hostname + first NIC MAC (best effort)
    return "fallback:%s:%s" % (socket.gethostname().lower(),
                               uuid.UUID(int=uuid.getnode()).hex[:12])


def _format(hexd: str, prefix: str = "") -> str:
    return prefix + "-".join(hexd[i:i + 4] for i in range(0, len(hexd), 4))


def device_code() -> str:
    """What the buyer sees and sends: ODU-XXXX-XXXX-XXXX (48 bits)."""
    digest = hmac.new(_secret(), b"odu-tracker/device-v1" +
                      _device_identity().encode("utf-8"),
                      hashlib.sha256).digest()
    return _format(digest[:6].hex().upper(), "ODU-")


def expected_unlock_code() -> str:
    """The unlock code that opens this device: XXXX-XXXX-XXXX-XXXX
    (64 bits — brute-forcing it offline is not practical)."""
    digest = hmac.new(_secret(), b"odu-tracker/unlock-v1" +
                      device_code().encode("utf-8"),
                      hashlib.sha256).digest()
    return _format(digest[:8].hex().upper())


def normalize(code: str) -> str:
    """Uppercase, dashes/spaces removed — paste-friendly."""
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


# ----------------------------------------------------------------- unlock state
def switch_policy(cfg) -> dict:
    """Last known remote policy ({} = never fetched / not in effect)."""
    p = cfg.get(SWITCH_CFG_KEY)
    return p if isinstance(p, dict) else {}


def switch_blocks(cfg):
    """None when the feature is allowed, else the user-facing reason."""
    p = switch_policy(cfg)
    if not p:
        return None
    banned = p.get("banned") or []
    if p.get("disabled") or normalize(device_code()) in [normalize(b) for b in banned]:
        return (str(p.get("message") or "").strip()
                or "access is paused — contact the author")
    return None


def refresh_switch(cfg, url=None, timeout=6.0):
    """Fetch the remote policy. Returns True when the applied policy
    CHANGED, False when unchanged, None when unreachable (last known
    policy stays in effect — sticky)."""
    url = url or cfg.get("license_switch_url") or SWITCH_URL
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "odu-tracker/" + _version()})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        pol = {
            "disabled": bool(data.get("disabled")),
            "banned": sorted({normalize(b) for b in (data.get("banned") or [])
                              if normalize(b)}),
            "message": str(data.get("message") or "")[:200],
            "checked_at": int(time.time()),
        }
    except Exception:
        return None
    old = switch_policy(cfg)
    changed = (old.get("disabled") != pol["disabled"]
               or old.get("banned") != pol["banned"])
    cfg.set(SWITCH_CFG_KEY, pol)
    return changed


def _record(cfg) -> dict:
    rec = cfg.get(CFG_KEY)
    return rec if isinstance(rec, dict) else {}


def is_unlocked(cfg) -> bool:
    """Recomputed from the DEVICE FINGERPRINT every time — a licence
    copied from another device or from an old data dir never validates.
    The remote kill-switch can pause even a valid local licence."""
    if switch_blocks(cfg) is not None:
        return False
    rec = _record(cfg)
    if rec.get("kind") == "grandfathered":
        return rec.get("device") == device_code()
    code = normalize(rec.get("code"))
    return bool(code) and hmac.compare_digest(
        code, normalize(expected_unlock_code()))


def status(cfg) -> dict:
    rec = _record(cfg)
    paused = switch_blocks(cfg)
    return {
        "locked": not is_unlocked(cfg),
        "kind": rec.get("kind") or None,
        "device_code": device_code(),
        "contact_email": CONTACT_EMAIL,
        "contact_tiktok": CONTACT_TIKTOK,
        "paused": paused,
    }


# brute-force throttle for the unlock endpoint (per app process)
_attempts: list = []


def _rate_ok() -> bool:
    now = time.time()
    while _attempts and now - _attempts[0] > 300:
        _attempts.pop(0)
    if len(_attempts) >= 10:
        return False
    _attempts.append(now)
    return True


def unlock(cfg, code: str):
    """Try to unlock with a customer-entered unlock code.
    Returns (ok: bool, error_message: str)."""
    paused = switch_blocks(cfg)
    if paused is not None:
        return False, f"voucher feature is paused remotely ({paused})"
    entered = normalize(code)
    if len(entered) != 16:
        return False, "unlock code must be 16 characters"
    if not _rate_ok():
        return False, "too many attempts — wait a few minutes"
    if not hmac.compare_digest(entered, normalize(expected_unlock_code())):
        return False, "invalid code for this device"
    cfg.set(CFG_KEY, {"code": entered, "kind": "paid",
                      "device": device_code(), "at": int(time.time())})
    return True, ""


def init(cfg, store) -> None:
    """Called once at engine start. Grandfathers installs that already
    used the voucher feature before it became paid (their local database
    shows prior voucher activity), bound to this device's fingerprint."""
    start_switch_refresher(cfg, store)
    if is_unlocked(cfg):
        return
    used = False
    try:
        for sql in ("SELECT 1 FROM vouchers LIMIT 1",
                    "SELECT 1 FROM voucher_claims LIMIT 1",
                    "SELECT 1 FROM voucher_usage LIMIT 1"):
            if store.q(sql, fetch="one"):
                used = True
                break
    except Exception:
        pass                      # fresh install: tables may not exist yet
    if not used and cfg.get("portal_enforced"):
        used = True               # portal armed => they ran the feature
    if used:
        cfg.set(CFG_KEY, {"kind": "grandfathered", "device": device_code(),
                          "at": int(time.time())})


def _log_switch_change(store, blocked) -> None:
    try:
        store.exec("INSERT INTO events(ts, kind, level, detail) "
                   "VALUES(?,?,?,?)",
                   (store.now(), "voucher_switch",
                    "warning" if blocked else "info",
                    "remote voucher switch: " +
                    ("PAUSED" if blocked else "active")))
    except Exception:
        pass


def start_switch_refresher(cfg, store=None, interval_s: int = 1800) -> None:
    """Background daemon: fetch the remote policy now, then every
    `interval_s`. Offline failures are silent (last policy stays in
    effect); a policy CHANGE is logged to the event feed."""
    import threading

    def loop():
        while True:
            try:
                changed = refresh_switch(cfg)
                if changed and store is not None:
                    _log_switch_change(store, switch_blocks(cfg) is not None)
            except Exception:
                pass
            time.sleep(interval_s)

    threading.Thread(target=loop, daemon=True,
                     name="voucher-switch").start()
