"""Voucher engine: shared SIM data pool + per-device MAC gating.

FIRMWARE REALITY (ZLT X17U / W154R PLUS, fw 4.2.3, verified live 2026-10-27)
-----------------------------------------------------------------------------
* There is NO per-device byte counter anywhere in the API. Live tests:
  cmd 223 `flow` stayed "0" for every device while the global counter moved
  +21 MB; per-station rx/tx rates are always "0"; no page displays per-device
  usage. A "20 GB voucher" therefore CANNOT be metered per device.
* Quotas are POOLED: purchased GB raise the router's single SIM cap
  (cmd 337: limitSwitch + limitSize against mon_download_flow). Every active
  voucher holder shares the pool.
* Per-device enforcement is on/off only, via the Wi-Fi black/white list
  (cmd 278): modes allow/deny/close, subcmd "1"=2.4G, "0"=5G, 32 entries per
  band. Write path proven live (write -> read-back -> restore).
* cmd 278/23 writes need a CSRF token. Working rule, established by test:
  first try the token from cmd 233 GET; if the write comes back
  "Invalid CSRF Token", take the token echoed in the command's own GET
  response and retry once with it (fresh cmd 233 tokens are rejected after
  the first accepted write).
* Mode caveats: cmd 278 is Wi-Fi only (wired clients bypass it) and the
  firmware disables it while WPS/MESH are on. Whitelist ("allow") mode would
  also cut unclaimed devices off from the portal itself, so enforcement uses
  deny entries per device + the global cap for exhaustion.

POOL MODEL
----------
  baseline_mb  = mon_download_flow (MB this billing period) captured when the
                 first voucher is claimed -- nothing before that counts.
  purchased_mb = sum of quota_mb over CLAIMED, non-revoked vouchers.
  planned cap  = baseline_mb + purchased_mb  -> becomes cmd 337 limitSize
                 when the pool is enabled (explicit action, not automatic).
"""
import secrets
import time

from . import store
from .security import normalize_mac

# cmd 278 subcmd per band (from the firmware's own radio labels:
# label:"1" -> "2.4G", label:"0" -> 5G)
BANDS = {"2.4g": "1", "5g": "0"}

# unambiguous alphabet (no 0/O/1/l/I) -- matches the admin-code style
_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 11

POOL_KEY = "voucher_pool"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vouchers(
    code TEXT PRIMARY KEY,
    quota_mb INTEGER NOT NULL,
    note TEXT,
    created_at INTEGER NOT NULL,
    revoked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS voucher_claims(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    mac TEXT NOT NULL,
    ip TEXT,
    claimed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vclaim_mac ON voucher_claims(mac);
"""


def init():
    """Idempotent; call once after store.init()."""
    for stmt in _SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            store.exec(stmt)


# --------------------------------------------------------------- codes
def random_code(length: int = CODE_LEN) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def generate(count: int = 1, quota_mb: int = 20480, note: str = "") -> list:
    """Create `count` fresh voucher codes (quota in MB; 20480 = 20 GB)."""
    count = max(1, min(100, int(count)))
    quota_mb = max(1, int(quota_mb))
    note = (note or "")[:120]
    out = []
    for _ in range(count):
        for _attempt in range(20):
            code = random_code()
            try:
                store.exec(
                    "INSERT INTO vouchers(code, quota_mb, note, created_at) "
                    "VALUES(?,?,?,?)",
                    (code, quota_mb, note, store.now()))
                out.append(code)
                break
            except Exception:
                continue  # collision, regenerate
    return out


def revoke(code: str) -> bool:
    _last, rowcount = store.exec(
        "UPDATE vouchers SET revoked=1 WHERE code=? AND revoked=0", (code,))
    return bool(rowcount)


def list_vouchers(limit: int = 200) -> list:
    rows = store.q(
        """SELECT v.code, v.quota_mb, v.note, v.created_at, v.revoked,
                  c.mac AS claim_mac, c.ip AS claim_ip, c.claimed_at
           FROM vouchers v LEFT JOIN voucher_claims c ON c.code = v.code
           ORDER BY v.created_at DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        if r["revoked"]:
            status = "revoked"
        elif r["claim_mac"]:
            status = "claimed"
        else:
            status = "active"
        out.append({
            "code": r["code"], "quota_mb": r["quota_mb"], "note": r["note"],
            "created_at": r["created_at"], "status": status,
            "mac": r["claim_mac"], "ip": r["claim_ip"],
            "claimed_at": r["claimed_at"],
        })
    return out


def purchased_mb() -> int:
    r = store.q("SELECT COALESCE(SUM(v.quota_mb),0) AS s FROM vouchers v "
                "JOIN voucher_claims c ON c.code=v.code WHERE v.revoked=0",
                fetch="one")
    return int(r["s"] if r else 0)


# ------------------------------------------------------------ pool state
def _pool() -> dict:
    return store.setting_get(POOL_KEY) or {}


def _save_pool(st: dict):
    st["updated_at"] = store.now()
    store.setting_set(POOL_KEY, st)


def claim(code: str, mac: str = None, ip: str = None) -> dict:
    """Redeem a code: binds it to a device MAC and credits the pool."""
    code = (code or "").strip()
    row = store.q("SELECT * FROM vouchers WHERE code=?", (code,), fetch="one")
    if not row:
        raise ValueError("unknown code")
    if row["revoked"]:
        raise ValueError("code revoked")
    if store.q("SELECT 1 FROM voucher_claims WHERE code=?", (code,),
               fetch="one"):
        raise ValueError("code already redeemed")
    m = normalize_mac(mac or "")
    if not m:
        raise ValueError("no device MAC for this claim")
    store.exec("INSERT INTO voucher_claims(code, mac, ip, claimed_at) "
               "VALUES(?,?,?,?)", (code, m, ip or "", store.now()))
    # NOTE: the pool baseline (mon_download_flow at engine start) is anchored
    # by pool_status() on its first successful router read -- usage before the
    # engine ever looked is never charged to the pool.
    store.exec("INSERT INTO events(ts, mac, kind, level, detail) "
               "VALUES(?,?,?,?,?)",
               (store.now(), m, "voucher_claim", "info",
                f"code={code} quota_mb={row['quota_mb']}"))
    return {"code": code, "mac": m, "quota_mb": row["quota_mb"]}


def pool_status(router=None) -> dict:
    """Read-only pool view: what the router reports + our accounting."""
    st = _pool()
    purchased = purchased_mb()
    used_total = None
    router_state = None
    if router is not None:
        try:
            if not router.logged_in:
                router.login()
            resp = router.cmd(337, "GET")           # getFlow (read-only)
            if isinstance(resp, dict) and resp.get("success") in (True, "1"):
                used_total = float(resp.get("mon_download_flow") or 0)
                router_state = {
                    "limitSwitch": resp.get("limitSwitch"),
                    "limitSize": resp.get("limitSize"),
                    "startDate": resp.get("startDate"),
                    "flow_limit_unit": resp.get("flow_limit_unit"),
                }
                # anchor/refresh baseline now that we have a reading
                if st.get("baseline_mb") is None and used_total is not None:
                    st["baseline_mb"] = used_total
                    _save_pool(st)
        except Exception as exc:
            router_state = {"error": str(exc)[:160]}
    baseline = st.get("baseline_mb")
    planned_limit = (baseline + purchased) if baseline is not None else None
    remaining = None
    if planned_limit is not None and used_total is not None:
        remaining = planned_limit - used_total
    return {
        "baseline_mb": baseline,
        "purchased_mb": purchased,
        "used_total_mb": used_total,
        "planned_limit_mb": planned_limit,
        "remaining_mb": remaining,
        "enabled": bool(st.get("enabled")),
        "router": router_state,
    }


# ------------------------------------------------------------- gate (cmd 278)
def gate_read(router, subcmd: str):
    """-> (mode, [macs]) for one band. Missing datas = filter off."""
    resp = router.cmd(278, "GET", extra={"subcmd": subcmd})
    datas = resp.get("datas") if isinstance(resp, dict) else None
    if not isinstance(datas, dict):
        return "close", []
    mode = datas.get("macfilter") or "close"
    macs = []
    for e in datas.get("maclist") or []:
        m = normalize_mac(str(e.get("mac", "")))
        if m:
            macs.append(m)
    return mode, macs


def _post_checked(router, cmd: int, payload: dict) -> dict:
    """POST with the CSRF dance proven against this firmware (see module doc)."""
    token = ""
    try:
        token = router.cmd(233, "GET").get("token") or ""
    except Exception:
        pass
    body = dict(payload)
    if token:
        body["token"] = token
    resp = router.cmd(cmd, "POST", extra=body)
    if isinstance(resp, dict) and resp.get("message") == "Invalid CSRF Token":
        # fallback: token echoed by this command's own GET response
        extra = {"subcmd": payload["subcmd"]} if "subcmd" in payload else None
        try:
            echo = router.cmd(cmd, "GET", extra=extra)
            token2 = (echo or {}).get("token") or ""
        except Exception:
            token2 = ""
        if token2:
            body = dict(payload, token=token2)
            resp = router.cmd(cmd, "POST", extra=body)
    return resp


def _write_ok(resp) -> bool:
    return (isinstance(resp, dict) and resp.get("success") in (True, "1")
            and resp.get("message", "") == "")


def gate_write(router, subcmd: str, macs: list, mode: str) -> dict:
    """Set one band's list. mode: allow | deny | close."""
    payload = {
        "subcmd": subcmd,
        "success": True,
        "datas": {"maclist": [{"mac": m.upper()} for m in macs],
                  "macfilter": mode},
    }
    resp = _post_checked(router, 278, payload)
    ok = _write_ok(resp)
    return {"ok": ok,
            "message": (resp or {}).get("message", "") if isinstance(resp, dict)
            else str(resp)[:120]}


def _both_bands(fn):
    results = {}
    for band, sub in BANDS.items():
        results[band] = fn(sub)
    return results


def gate_deny(router, mac: str) -> dict:
    """Block a device on both bands (adds to the deny list)."""
    m = normalize_mac(mac or "")
    if not m:
        return {"ok": False, "error": "bad mac"}

    def one(sub):
        mode, macs = gate_read(router, sub)
        if mode == "allow":
            # whitelist mode: absent == already blocked; don't corrupt it
            return {"ok": True, "mode": "allow", "note": "already excluded"}
        if m in macs and mode == "deny":
            return {"ok": True, "mode": mode, "note": "already listed"}
        macs = [x for x in macs if x != m] + [m]
        r = gate_write(router, sub, macs, "deny")
        r["mode"] = "deny"
        return r
    return _both_bands(one)


def gate_allow(router, mac: str) -> dict:
    """Un-block a device on both bands (removes it; empty list -> close)."""
    m = normalize_mac(mac or "")
    if not m:
        return {"ok": False, "error": "bad mac"}

    def one(sub):
        mode, macs = gate_read(router, sub)
        if m not in macs:
            return {"ok": True, "mode": mode, "note": "not listed"}
        macs = [x for x in macs if x != m]
        new_mode = "close" if not macs else mode
        r = gate_write(router, sub, macs, new_mode)
        r["mode"] = new_mode
        return r
    return _both_bands(one)


def gate_status(router) -> dict:
    out = {}
    for band, sub in BANDS.items():
        try:
            mode, macs = gate_read(router, sub)
            out[band] = {"mode": mode, "macs": macs}
        except Exception as exc:
            out[band] = {"error": str(exc)[:160]}
    return out


# ------------------------------------------------------- device resolution
def mac_for_ip(router, ip: str) -> str:
    """Live DHCP table lookup (cmd 223) for the claimant's MAC."""
    if not ip:
        return None
    try:
        if router is not None and not router.logged_in:
            router.login()
        resp = router.cmd(223, "GET")
        for e in (resp.get("dhcp_list_info") or []):
            if e.get("ip") == ip:
                m = normalize_mac(str(e.get("mac", "")))
                if m:
                    return m
    except Exception:
        pass
    # fallback: tracker's own device cache
    try:
        r = store.q("SELECT mac FROM devices WHERE last_ip=? "
                    "ORDER BY last_seen DESC LIMIT 1", (ip,), fetch="one")
        if r:
            return normalize_mac(r["mac"])
    except Exception:
        pass
    return None
