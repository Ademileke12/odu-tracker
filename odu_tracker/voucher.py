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
                 pool engine first reads the router -- nothing before that
                 counts. Re-anchored automatically when the router counter
                 goes backwards (billing-period reset).
  purchased_mb = sum of quota_mb over CLAIMED, non-revoked, UNEXPIRED vouchers
                 (see validity_days()).
  grace_mb     = starter allowance (default 20 GB) so arming the cap does
                 not instantly cut off a household with zero redemptions.
  planned cap  = baseline_mb + purchased_mb + grace_mb
                 -> written to cmd 337 limitSize (GB) whenever the cap is
                 enabled and the stored value no longer matches (cap_stale):
                 claims raise it, expiry/revocation lowers it, all writes go
                 through pool_refresh_async (background, coalesced, backed
                 off) or an explicit pool_set.

VALIDITY
--------
  A redeemed voucher credits the pool for validity_days() days (default 30,
  0 = never). After that the claim stops counting toward purchased_mb, the
  device loses its "claimed" status (an armed DNS portal serves it the claim
  page again), and the router cap is re-synced down by the stale check above.
"""
import secrets
import threading
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

# Starter allowance the pool carries on top of claimed vouchers, so that
# arming the router cap (cmd 337) does not instantly cut the household off
# when nothing has been redeemed yet. Included in the planned cap:
#   planned = baseline + purchased + grace
GRACE_DEFAULT_MB = 20480          # 20 GB

# How long a redeemed voucher keeps crediting the pool / holding device
# access. Admin-adjustable per pool (voucher_pool.validity_days); 0 = never.
VALIDITY_DEFAULT_DAYS = 30

# Minimum gap between background cap re-write attempts when the previous
# attempt failed (router down / write refused) — stops a stale pool from
# hammering an unreachable router every dashboard poll.
REFRESH_BACKOFF_S = 60.0

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
CREATE TABLE IF NOT EXISTS voucher_usage(
    mac TEXT PRIMARY KEY,
    used_mb REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
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
    cutoff = _claim_cutoff()
    out = []
    for r in rows:
        if r["revoked"]:
            status = "revoked"
        elif r["claim_mac"] and cutoff and (r["claimed_at"] or 0) < cutoff:
            status = "expired"
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


# ------------------------------------------------------------- validity
def validity_days() -> int:
    """Days a redeemed voucher keeps crediting the pool / device access.

    0 = never expires. Admin-adjustable via set_validity_days().
    """
    st = _pool()
    v = st.get("validity_days")
    return VALIDITY_DEFAULT_DAYS if v is None else int(v)


def set_validity_days(days: int) -> int:
    st = _pool()
    st["validity_days"] = max(0, int(days))
    _save_pool(st)
    return st["validity_days"]


def _claim_cutoff() -> int:
    """Unix ts before which a claim counts as expired (0 = no expiry)."""
    d = validity_days()
    return 0 if d <= 0 else store.now() - d * 86400


def purchased_mb() -> int:
    cutoff = _claim_cutoff()
    sql = ("SELECT COALESCE(SUM(v.quota_mb),0) AS s FROM vouchers v "
           "JOIN voucher_claims c ON c.code=v.code WHERE v.revoked=0")
    args = ()
    if cutoff:
        sql += " AND c.claimed_at >= ?"
        args = (cutoff,)
    r = store.q(sql, args, fetch="one")
    return int(r["s"] if r else 0)


def grace_mb() -> int:
    """Starter allowance (see GRACE_DEFAULT_MB); admin-adjustable per pool."""
    st = _pool()
    g = st.get("grace_mb")
    return GRACE_DEFAULT_MB if g is None else int(g)


# ------------------------------------------------------------ pool state
def _pool() -> dict:
    return store.setting_get(POOL_KEY) or {}


def _save_pool(st: dict):
    st["updated_at"] = store.now()
    store.setting_set(POOL_KEY, st)


def claim(code: str, mac: str = None, ip: str = None, router=None) -> dict:
    """Redeem a code: binds it to a device MAC and credits the pool.

    `router` (optional) enables the background cap re-write so the redeemed
    quota is actually usable at the router immediately — see the pool model
    note at the end of this function.
    """
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
    # The claim credits our books, but cmd 337 still enforces the OLD cap —
    # redeemed data is unusable until limitSize is rewritten. Do it now in the
    # background (pool_refresh_async is a no-op while the cap is disabled).
    pool_refresh_async(router, reason="claim")
    return {"code": code, "mac": m, "quota_mb": row["quota_mb"]}


def pool_status(router=None, auto_resync=True) -> dict:
    """Read-only pool view: what the router reports + our accounting.

    When the router counter goes backwards (billing-period reset) while the
    cap is enabled, the stale limitSize would be far too generous for the new
    period, so the cap is re-written to the fresh planned value automatically.
    Pass auto_resync=False to suppress that write (pool_set does, to avoid
    recursion).
    """
    st = _pool()
    purchased = purchased_mb()
    grace = grace_mb()
    used_total = None
    router_state = None
    reset_detected = False
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
                elif (st.get("baseline_mb") is not None
                      and used_total is not None
                      and used_total < st["baseline_mb"]):
                    # billing-period reset: the router counter went backwards.
                    # Re-anchor so the next cap write isn't anchored to last
                    # period's usage.
                    st["baseline_mb"] = used_total
                    _save_pool(st)
                    reset_detected = True
        except Exception as exc:
            router_state = {"error": str(exc)[:160]}
    baseline = st.get("baseline_mb")
    planned_limit = (baseline + purchased + grace) if baseline is not None else None
    remaining = None
    if planned_limit is not None and used_total is not None:
        remaining = planned_limit - used_total
    if reset_detected and auto_resync and st.get("enabled"):
        try:
            pool_set(router, True, router_state=router_state)
        except Exception:
            pass
    elif (auto_resync and router is not None and st.get("enabled")
          and cap_stale()):
        # expiry / revocation moved the planned pool without any router event
        # (no reset detected) -> lower limitSize in the background too
        pool_refresh_async(router, reason="expiry/revocation")
    return {
        "baseline_mb": baseline,
        "purchased_mb": purchased,
        "grace_mb": grace,
        "used_total_mb": used_total,
        "planned_limit_mb": planned_limit,
        "remaining_mb": remaining,
        "enabled": bool(st.get("enabled")),
        "reset_detected": reset_detected,
        "validity_days": validity_days(),
        "cap_stale": cap_stale(),
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


def _write_ok(resp) -> bool:
    """Success shapes seen on this firmware: cmd 278/337 -> success:true with
    message "" (some builds answer message "Succeed!"); cmd 132 -> "0"."""
    if not isinstance(resp, dict):
        return False
    if resp.get("message") == "0":                       # cmd 132 shape
        return True
    if resp.get("message") == "Succeed!":                # cmd 337 shape
        return True
    return resp.get("success") in (True, "1") and \
        resp.get("message", "") == ""


def _post_checked(router, cmd: int, payload: dict) -> dict:
    """POST with the CSRF dance proven against this firmware (see module doc).

    A write that times out at the HTTP layer may still have been APPLIED
    (seen live with cmd 132: the radio stack restarts and the response never
    arrives). In that case return {"applied_unknown": True} instead of
    raising, so callers verify by read-back rather than retry a write that
    already landed.
    """
    token = ""
    try:
        token = router.cmd(233, "GET").get("token") or ""
    except Exception:
        pass
    body = dict(payload)
    if token:
        body["token"] = token
    try:
        resp = router.cmd(cmd, "POST", extra=body)
    except Exception as exc:
        return {"applied_unknown": True, "error": str(exc)[:160]}
    if isinstance(resp, dict) and resp.get("message") == "Invalid CSRF Token":
        # fallback 1: token echoed by this command's own GET response
        extra = {"subcmd": payload["subcmd"]} if "subcmd" in payload else None
        try:
            echo = router.cmd(cmd, "GET", extra=extra)
            token2 = (echo or {}).get("token") or ""
        except Exception:
            token2 = ""
        if token2:
            body = dict(payload, token=token2)
            try:
                resp = router.cmd(cmd, "POST", extra=body)
            except Exception as exc:
                return {"applied_unknown": True, "error": str(exc)[:160]}
        # fallback 2: a fresh login mints a fresh single-use token
        if (isinstance(resp, dict)
                and resp.get("message") == "Invalid CSRF Token"):
            try:
                router.login()
                token3 = router.cmd(233, "GET").get("token") or ""
            except Exception:
                token3 = ""
            if token3:
                body = dict(payload, token=token3)
                try:
                    resp = router.cmd(cmd, "POST", extra=body)
                except Exception as exc:
                    return {"applied_unknown": True, "error": str(exc)[:160]}
    return resp


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


# ------------------------------------------------- pool cap (cmd 337)
def pool_set(router, enable: bool, router_state: dict = None) -> dict:
    """Write the SIM data cap into cmd 337 (setFlow).

    enable=True  -> limitSwitch "1", limitSize = planned cap in GB
                    (baseline + purchased + grace; see module doc).
    enable=False -> limitSwitch "0", limitSize untouched.

    limitSize is in GB when flow_limit_unit is "1" (MB="0", TB="2") — the
    firmware computes remaining = limitSize * 1024^unit − mon_download_flow(MB).
    """
    st = _pool()
    status = pool_status(router, auto_resync=False)
    planned_mb = status.get("planned_limit_mb")
    if enable and planned_mb is None:
        return {"ok": False, "error": "no pool baseline yet (no router reading)"}
    current = router_state or status.get("router") or {}
    payload = {
        "limitSwitch": "1" if enable else "0",
        "limitSize": str(current.get("limitSize") or "0"),
        "startDate": str(current.get("startDate") or "1"),
        "flow_limit_unit": str(current.get("flow_limit_unit") or "1"),
    }
    if enable:
        payload["limitSize"] = f"{planned_mb / 1024:.2f}"      # MB -> GB
    resp = _post_checked(router, 337, payload)
    ok = _write_ok(resp)
    applied_unknown = isinstance(resp, dict) and resp.get("applied_unknown")
    # always verify by read-back (a timed-out write may still have landed)
    back = {}
    if ok or applied_unknown:
        try:
            time.sleep(2.0)
            back = router.cmd(337, "GET") or {}
        except Exception:
            pass
    verified = (str(back.get("limitSwitch")) == payload["limitSwitch"]
                and (not enable
                     or str(back.get("limitSize")) == payload["limitSize"]))
    ok = ok or verified
    if ok:
        st["enabled"] = bool(enable)
        st["cap_limit_mb"] = planned_mb if enable else None
        _save_pool(st)
    return {"ok": ok,
            "limitSwitch": payload["limitSwitch"],
            "limitSize": payload["limitSize"],
            "verified": verified,
            "message": (resp or {}).get("message", "") if isinstance(resp, dict)
            else str(resp)[:120]}


# ------------------------------------------- cap auto-sync (background thread)
# cmd 337's limitSize must track the accounting pool:
#   * a claim RAISES it     -> otherwise redeemed data is unusable
#   * expiry/revocation LOWERS it -> otherwise expired data stays granted
# Syncs are coalesced (one writer), backed off after failures, and gated by
# cap_stale() so a healthy pool costs zero router traffic.
_refresh_lock = threading.Lock()
_refresh_state = {"thread": None, "pending": False,
                  "last_fail": 0.0, "fail_logged": False}


def cap_stale() -> bool:
    """DB-only: does the stored cap no longer match the planned pool?

    True when the cap is enabled and planned (baseline + purchased + grace)
    moved away from cap_limit_mb — i.e. a claim, expiry or revocation
    happened since the last cmd 337 write. Never touches the router.
    """
    st = _pool()
    if not st.get("enabled") or st.get("baseline_mb") is None:
        return False
    stored = st.get("cap_limit_mb")
    if stored is None:
        return False
    planned = st["baseline_mb"] + purchased_mb() + grace_mb()
    return abs(planned - float(stored)) > 0.5      # MB; > rounding noise


def pool_refresh_async(router, reason: str = "") -> dict:
    """Rewrite cmd 337 limitSize to the current planned cap in a background
    thread. Safe to call after every claim/route: no-op when the cap is
    disabled or the pool is already in sync, coalesced into one writer, and
    backed off (REFRESH_BACKOFF_S) after a failed attempt.

    Returns {"ok": True, "thread": t} when a write pass was started/queued.
    """
    if router is None:
        return {"ok": False, "skipped": "no router"}
    if not _pool().get("enabled"):
        return {"ok": False, "skipped": "cap disabled"}
    with _refresh_lock:
        if (_refresh_state["last_fail"]
                and time.time() - _refresh_state["last_fail"]
                < REFRESH_BACKOFF_S):
            return {"ok": False, "skipped": "backoff"}
        if _refresh_state["thread"] is not None:
            _refresh_state["pending"] = True       # piggy-back on the running pass
            return {"ok": True, "queued": True}
        if not cap_stale():
            return {"ok": True, "synced": False}   # nothing to write
        th = threading.Thread(target=_refresh_worker, args=(router, reason),
                              daemon=True, name="cap-sync")
        _refresh_state["thread"] = th
        th.start()
        return {"ok": True, "queued": True, "thread": th}


def _refresh_worker(router, reason: str):
    try:
        while True:
            with _refresh_lock:
                _refresh_state["pending"] = False
            wrote = False
            res = {"ok": False, "error": "skipped"}
            try:
                if cap_stale():
                    res = pool_set(router, True)   # recomputes planned itself
                    wrote = True
                else:
                    res = {"ok": True}
            except Exception as exc:
                res = {"ok": False, "error": str(exc)[:160]}
            ok = bool(res.get("ok"))
            with _refresh_lock:
                _refresh_state["last_fail"] = 0.0 if ok else time.time()
                failed_new = (not ok) and not _refresh_state["fail_logged"]
                if ok:
                    _refresh_state["fail_logged"] = False
                elif not _refresh_state["fail_logged"]:
                    _refresh_state["fail_logged"] = True
            if wrote or failed_new:
                # one event per real write, plus a single warning for a
                # failing streak (the backoff stops 60 s spam)
                try:
                    store.exec(
                        "INSERT INTO events(ts, mac, kind, level, detail) "
                        "VALUES(?,?,?,?,?)",
                        (store.now(), "", "voucher_pool",
                         "info" if ok else "warning",
                         f"cap sync ({reason or 'auto'}): "
                         f"limitSize={res.get('limitSize', '?')} "
                         f"{'ok' if ok else 'FAILED ' + str(res.get('error') or res.get('message') or '')}"))
                except Exception:
                    pass
            with _refresh_lock:
                if _refresh_state["pending"]:
                    continue                        # a newer claim arrived mid-write
                _refresh_state["thread"] = None
                return
    except BaseException:
        with _refresh_lock:
            _refresh_state["thread"] = None
        raise


# -------------------------------------------- per-device usage (time-share)
# The firmware has ONE shared counter (mon_download_flow) and no per-device
# byte meter (verified live). To still cut a device off when it burns through
# ITS allocation, we estimate usage: on every poll the counter's growth since
# the last poll is split evenly among the claim-holder devices that are online
# right now (time-share split). Accumulated per-MAC usage is compared against
# that MAC's active-claim quota; at/over quota the device is exhausted ->
# denied (cmd 278 + DNS hijack to the portal) until it claims more.
#
# This is an ESTIMATE, not a firmware reading — it is the closest a shared
# counter allows. The cmd 337 pool cap remains the hard, exact backstop.
# Per-poll floor before a batch of accumulated growth is billed to devices.
# Readings are quantized (~0.01 GB = 10.24 MB steps), so this must stay above
# the quantization step; growth BELOW the floor is CARRIED in pool state
# (usage_pending_mb), never dropped — a hard cutoff here previously discarded
# ~71% of all counter growth (measured live) because steady household traffic
# rarely moves >64 MB inside one 15 s poll.
USAGE_TOLERANCE_MB = 64.0

_exhaust_cache = {"ts": 0.0, "macs": None}     # exhausted MAC set, 5 s TTL


def device_quota_mb(mac: str) -> int:
    """Total MB this device currently holds (sum of its unexpired claims)."""
    m = normalize_mac(mac or "")
    cutoff = _claim_cutoff()
    sql = ("SELECT COALESCE(SUM(v.quota_mb),0) AS s FROM vouchers v "
           "JOIN voucher_claims c ON c.code=v.code "
           "WHERE c.mac=? AND v.revoked=0")
    args = (m,)
    if cutoff:
        sql += " AND c.claimed_at >= ?"
        args = (m, cutoff)
    r = store.q(sql, args, fetch="one")
    return int(r["s"] if r else 0)


def device_used_mb(mac: str) -> float:
    m = normalize_mac(mac or "")
    r = store.q("SELECT used_mb FROM voucher_usage WHERE mac=?", (m,),
                fetch="one")
    return float(r["used_mb"]) if r else 0.0


def set_device_used_mb(mac: str, used_mb: float):
    m = normalize_mac(mac or "")
    if not m:
        return
    store.exec("INSERT INTO voucher_usage(mac, used_mb, updated_at) "
               "VALUES(?,?,?) ON CONFLICT(mac) DO UPDATE SET "
               "used_mb=excluded.used_mb, updated_at=excluded.updated_at",
               (m, max(0.0, float(used_mb)), store.now()))


def exhausted_macs(ttl: float = 5.0) -> set:
    """MACs whose estimated usage has reached their active-claim quota."""
    now = time.time()
    if _exhaust_cache["macs"] is not None and now - _exhaust_cache["ts"] < ttl:
        return _exhaust_cache["macs"]
    out = set()
    rows = store.q("SELECT mac, used_mb FROM voucher_usage")
    for r in rows:
        m = normalize_mac(r["mac"])
        if not m:
            continue
        q = device_quota_mb(m)
        if q > 0 and float(r["used_mb"]) >= q - USAGE_TOLERANCE_MB:
            out.add(m)
    _exhaust_cache["macs"] = out
    _exhaust_cache["ts"] = now
    return out


def invalidate_usage_cache():
    _exhaust_cache["macs"] = None
    _exhaust_cache["ts"] = 0.0


def attribute_usage(counter_mb: float, online_macs) -> list:
    """Split the counter's growth since last poll among online claim-holders.

    `counter_mb` = mon_download_flow now (MB). `online_macs` = MACs seen online
    this poll. Returns MACs that JUST crossed their quota this call (so the
    caller can deny them). Purely local (no router HTTP).

    Sub-tolerance growth is CARRIED in pool state (`usage_pending_mb`) instead
    of being dropped: the readings are quantized to ~10 MB steps and a hard
    per-poll cutoff silently discarded most steady traffic. The accumulator
    also cancels that rounding noise (zero-mean) instead of truncating it.
    Pending growth is always settled with the set of claim-holders it
    accumulated for (they were online while it accrued), even if they have
    since gone offline or the sharer set changed.
    """
    st = _pool()
    prev = st.get("usage_counter_mb")
    st["usage_counter_mb"] = float(counter_mb)
    # counter went backwards (ODU reboot / firmware clear): re-anchor the
    # delta and drop any pending carry — but do NOT refund device usage.
    # The data was really consumed; zeroing here let every exhausted device
    # roam free the moment the router rebooted (live incident 2026-10-30).
    # A fresh billing period is an explicit admin action: reset_all_usage().
    if prev is not None and float(counter_mb) < float(prev) - 1.0:
        st["usage_pending_mb"] = 0.0
        st["usage_pending_set"] = ""
        _save_pool(st)
        return []
    if prev is None:
        st["usage_pending_mb"] = 0.0          # first reading: just anchor
        st["usage_pending_set"] = ""
        _save_pool(st)
        return []
    delta = float(counter_mb) - float(prev)   # > -1.0 here (reset handled)
    active = active_claim_macs()
    online = {normalize_mac(x) for x in (online_macs or [])} - {None, ""}
    sharers = online & active                 # claim-holders online now
    key = ",".join(sorted(sharers))
    old_key = st.get("usage_pending_set") or ""
    pending_old = float(st.get("usage_pending_mb") or 0.0)
    newly = set()
    if old_key and old_key != key and pending_old > 0.0:
        # sharer set changed: settle the carry with the set that earned it
        newly |= _distribute(old_key.split(","), pending_old)
        pending_old = 0.0
    if not sharers:
        # nobody to bill for this window (unclaimed / unknown devices online)
        st["usage_pending_mb"] = 0.0
        st["usage_pending_set"] = ""
        _save_pool(st)
        return sorted(newly)
    pending = (pending_old if old_key == key else 0.0) + delta
    if pending < 0.0:
        pending = 0.0                         # downward quantization jitter
    if pending < USAGE_TOLERANCE_MB:
        st["usage_pending_mb"] = pending
        st["usage_pending_set"] = key
        _save_pool(st)
        return sorted(newly)
    st["usage_pending_mb"] = 0.0
    st["usage_pending_set"] = key
    _save_pool(st)
    newly |= _distribute(sorted(sharers), pending)
    return sorted(newly)


def _distribute(macs, total_mb: float) -> set:
    """Split total_mb evenly over macs; return MACs newly at/over quota."""
    macs = [m for m in macs if m]
    if not macs or total_mb <= 0.0:
        return set()
    share = total_mb / len(macs)
    before = exhausted_macs()
    for m in macs:
        set_device_used_mb(m, device_used_mb(m) + share)
    invalidate_usage_cache()
    return exhausted_macs() - before


def reset_all_usage():
    """Explicit admin 'new billing period' — zero every device's usage.

    The ONLY path that forgives consumed data. A router reboot / counter
    clear is NOT this: attribute_usage just re-anchors its delta there and
    keeps per-device usage intact (a reboot must never hand out free GB).
    """
    st = _pool()
    st["usage_pending_mb"] = 0.0
    st["usage_pending_set"] = ""
    _save_pool(st)
    store.exec("UPDATE voucher_usage SET used_mb=0")
    invalidate_usage_cache()


def credit_device(mac: str, mb: float):
    """A new claim adds to a device's pool; if that lifts it out of exhaustion,
    restore access (gate_allow + DNS cache). Used by the claim routes."""
    invalidate_usage_cache()
    m = normalize_mac(mac or "")
    if m and m in exhausted_macs():
        # quota grew (new claim) -> no longer exhausted; clear any prior deny
        try:
            invalidate_deny_cache()
        except Exception:
            pass
    return {"mac": m, "quota_mb": device_quota_mb(m),
            "used_mb": device_used_mb(m)}


# ------------------------------------------------------- WPS off (cmd 132)
# The firmware disables the cmd 278 Wi-Fi filter while WPS/MESH are on
# (its own UI warns about this). Enforcement requires WPS off on both bands.
WPS_BANDS = {"2.4g": (0, "wlan2g_wps_switch"), "5g": (1, "wlan5g_wps_switch")}


def wps_status(router) -> dict:
    out = {}
    for band, (sub, key) in WPS_BANDS.items():
        try:
            resp = router.cmd(132, "GET", extra={"subcmd": sub})
            out[band] = {"on": str(resp.get(key, "")) == "1"}
        except Exception as exc:
            out[band] = {"error": str(exc)[:160]}
    return out


def wps_off(router) -> dict:
    """Turn WPS off on both bands (cmd 132). Success shape: message "0".

    A POST timeout is not failure here — seen live, applying the change can
    drop the HTTP response — so the result is always decided by read-back.
    """
    out = {}
    for band, (sub, key) in WPS_BANDS.items():
        payload = {key: "0", "subcmd": sub}
        resp = _post_checked(router, 132, payload)
        ok = _write_ok(resp)
        applied_unknown = isinstance(resp, dict) and resp.get("applied_unknown")
        # always verify by read-back
        on = None
        try:
            time.sleep(3.0 if (ok or applied_unknown) else 1.0)
            back = router.cmd(132, "GET", extra={"subcmd": sub})
            on = str(back.get(key, "")) == "1"
        except Exception:
            pass
        verified_off = (on is False)
        out[band] = {"ok": ok or verified_off, "verified_off": verified_off,
                     "still_on": on,
                     "message": (resp or {}).get("message", "") if isinstance(resp, dict)
                     else str(resp)[:120]}
    return out


# ------------------------------------------------------- device resolution
# ---------------------------------------------- portal deny policy (DNS path)
# The DNS proxy calls this on EVERY query, so it must be purely local:
# no router HTTP (1.3 s pacing) — devices.last_ip (poller, ~15 s fresh) plus
# the kernel ARP table. Fail-open on unresolvable MACs: a device we cannot
# identify yet simply is not hijacked (it gets caught on a later query once
# the poller or ARP has its MAC), so an unresolvable state can never black-
# hole a paying customer.
_deny_cache = {"ts": 0.0, "macs": None}      # active-claim MAC set, 5 s TTL
_ip_cache = {}                                # ip -> (mac|None, ts), 30 s TTL


def active_claim_macs(ttl: float = 5.0) -> set:
    """MACs holding a claim on a non-revoked, unexpired voucher. Small TTL so
    a fresh portal claim stops being hijacked within seconds."""
    now = time.time()
    if _deny_cache["macs"] is not None and now - _deny_cache["ts"] < ttl:
        return _deny_cache["macs"]
    cutoff = _claim_cutoff()
    sql = ("SELECT c.mac FROM voucher_claims c "
           "JOIN vouchers v ON v.code=c.code WHERE v.revoked=0")
    args = ()
    if cutoff:
        sql += " AND c.claimed_at >= ?"
        args = (cutoff,)
    rows = store.q(sql, args)
    macs = {normalize_mac(r["mac"]) for r in rows} - {None, ""}
    _deny_cache["macs"] = macs
    _deny_cache["ts"] = now
    return macs


def invalidate_deny_cache():
    """Call after a claim so the device passes immediately."""
    _deny_cache["macs"] = None
    _deny_cache["ts"] = 0.0
    _ip_cache.clear()


def mac_for_ip_local(ip: str, ttl: float = 30.0) -> str:
    """Fast offline ip -> mac (devices cache, then ARP table). None-cached."""
    now = time.time()
    hit = _ip_cache.get(ip)
    if hit and now - hit[1] < ttl:
        return hit[0]
    mac = None
    try:
        r = store.q("SELECT mac FROM devices WHERE last_ip=? "
                    "ORDER BY last_seen DESC LIMIT 1", (ip,), fetch="one")
        if r:
            mac = normalize_mac(r["mac"])
    except Exception:
        pass
    if not mac:
        try:
            with open("/proc/net/arp") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == ip and parts[2] == "0x2":
                        mac = normalize_mac(parts[3])
                        if mac:
                            break
        except OSError:
            pass
    _ip_cache[ip] = (mac, now)
    if len(_ip_cache) > 2048:                 # bounded
        _ip_cache.clear()
    return mac


def deny_checker(exempt_ips=()):
    """Build the predicate the DNS proxy uses: True = hijack this client.

    Denied = resolvable device with NO active voucher claim, OR one whose
    estimated usage has reached its quota (exhausted — see attribute_usage).
    Exempt: the portal host itself and the router (never hijack infra).
    """
    exempt = {str(x) for x in exempt_ips if x}

    def deny(ip: str) -> bool:
        if not ip or ip in exempt or ip.startswith("127."):
            return False
        mac = mac_for_ip_local(ip)
        if not mac:
            return False                    # unknown -> fail open (see above)
        if mac in exhausted_macs():
            return True                     # used its allocation -> portal
        return mac not in active_claim_macs()
    return deny


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
