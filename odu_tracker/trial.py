"""24-hour free trial (v1.5.0) - server-backed so a reinstall cannot restart it.

Record flow:
  1. On every app start (first /api/meta of the process) we POST the device
     code to the site's ``/api/trial`` endpoint (Upstash Redis on Vercel).
     The SERVER owns ``started_at``: the first call ever pins the 24h window,
     later calls - including after a reinstall or app-data wipe - return the
     SAME record.
  2. The answer is mirrored into config so gating stays offline-friendly:
     ``active()`` is a pure local read on every gated request (no network
     in the request path).
  3. Endpoint unreachable => the last local window stays sticky (same
     degradation as the voucher switch). Once the endpoint answers again,
     the server record wins. With NO endpoint configured the feature is
     simply disarmed: no record is ever created and the classic
     pay-on-first-launch behaviour applies (also what the test suite sees).

Clock rollback: ``last_seen`` is written on every refresh. A ``now`` more
than SLACK_S behind it means the system clock was wound back to stretch
the trial - the record is permanently banned (paywall). Small NTP steps
inside SLACK_S are tolerated; deleting config never mints a fresh trial
because the server record outranks any local absence.
"""
import json
import time
import urllib.request

from odu_tracker import app_license
from odu_tracker.license import device_code

CFG_KEY = "trial"
URL_CFG = "trial_api_url"
TRIAL_S = 24 * 3600          # 24 hours, the whole product unlocked
SLACK_S = 600                # clock-rollback tolerance (10 min)
HINT_MAX_AGE_S = 30 * 86400  # server honours a local start hint only within 30 days


def _now() -> int:
    return int(time.time())


def armed(cfg) -> bool:
    """True when an endpoint is configured - the feature's master switch."""
    return bool(str(cfg.get(URL_CFG) or "").strip())


def has_record(cfg) -> bool:
    rec = cfg.get(CFG_KEY)
    return isinstance(rec, dict) and bool(rec.get("expires_at"))


def active(cfg) -> bool:
    """Local, cheap, no network: called on every gated request."""
    rec = cfg.get(CFG_KEY)
    if not isinstance(rec, dict) or not rec.get("expires_at"):
        return False                     # no record = no trial (delete != free)
    if rec.get("banned"):
        return False                     # clock tamper, permanent
    now = _now()
    last = int(rec.get("last_seen") or 0)
    if last and now < last - SLACK_S:
        return False                     # clock wound back this session
    return now < int(rec["expires_at"])


def state(cfg) -> dict:
    rec = cfg.get(CFG_KEY)
    rec = rec if isinstance(rec, dict) else {}
    exp = int(rec.get("expires_at") or 0)
    on = active(cfg)
    return {
        "active": on,
        "started_at": int(rec.get("started_at") or 0) or None,
        "expires_at": exp or None,
        "remaining_s": max(0, exp - _now()) if exp else None,
        # a window that ran out (not "never had one") - drives the
        # "your trial has ended" line on the paywall
        "ended": bool(exp) and not on,
        "source": rec.get("source") or None,
    }


def _post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "odu-tracker/trial"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _bootstrap_local(cfg, rec: dict, now: int, tampered: bool) -> bool:
    """Endpoint unreachable: keep the existing window sticky, or start a
    local one for a brand-new (offline) install. Only runs when armed()."""
    started = int(rec.get("started_at") or 0)
    if not started or started > now + SLACK_S:
        started = now                   # fresh install, or future-dated junk
    new = {
        "started_at": started,
        "expires_at": started + TRIAL_S,
        "last_seen": now,
        "checked_at": now,
        "source": "local",
    }
    if tampered or rec.get("banned"):
        new["banned"] = 1
    cfg.set(CFG_KEY, new)
    return True


def refresh(cfg, url=None, timeout=6.0) -> bool:
    """Fetch/pin the trial record. Returns True when stored state changed.
    Never raises: offline, unconfigured and misconfigured all degrade."""
    if app_license.is_paid(cfg):
        return False                      # paid users are past the trial
    url = (url or cfg.get(URL_CFG) or "").strip()
    if not url:
        return False                      # disarmed: never mint a record
    now = _now()
    rec = cfg.get(CFG_KEY)
    rec = rec if isinstance(rec, dict) else {}
    last = int(rec.get("last_seen") or 0)
    tampered = bool(rec.get("banned")) or bool(last and now < last - SLACK_S)

    payload = {"d": device_code()}
    hint = int(rec.get("started_at") or 0)
    if 0 < hint <= now:
        payload["s"] = hint               # offline-first bootstrap hint
    try:
        data = _post(url, payload, timeout)
        started = int(data.get("started_at") or 0)
        expires = int(data.get("expires_at") or 0)
        if not started or not expires or expires <= started:
            raise ValueError("bad trial payload")
    except Exception:
        return _bootstrap_local(cfg, rec, now, tampered)

    new = {
        "started_at": started,
        "expires_at": expires,
        "last_seen": now,
        "checked_at": now,
        "source": "server",
    }
    if tampered:
        new["banned"] = 1                 # sticky: server answers never un-ban
    changed = any(new.get(k) != rec.get(k)
                  for k in ("started_at", "expires_at", "banned"))
    cfg.set(CFG_KEY, new)
    return changed
