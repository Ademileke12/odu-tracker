r"""Base app licence — the ₦1,500 one-time unlock (v1.5.0).

Everything except the Voucher add-on sits behind ONE payment: usage,
device radar, threat alerts, speed tests, the guest Wi-Fi gate, on every
platform we ship (Windows / Linux / macOS / Android / Chromebook).

Flow
----
  1. First load shows the paywall (feature slides + Pay stack button).
  2. Paystack Inline collects ₦1,500; the app verifies the reference
     against Paystack (see paystack.py) and, once the transaction is
     confirmed `success` / `NGN` / ≥ 150,000 kobo, activates locally.
  3. Activation stores an unlock code derived from THIS device's
     fingerprint — the same code the customer copies down. Reinstalling
     the app on the same device re-enters that code and unlocks again;
     the code is useless on any other device because verification always
     RECOMPUTES the expected value from the local fingerprint.

The trust model is the same honest trade-off as odu_tracker/license.py
(the voucher add-on): the HMAC secret ships inside the app, so casual
sharing/copying is blocked and a determined reverse-engineer could mint
codes. No server, works offline after the first successful activation.
"""
import hashlib
import hmac
import re
import time

from odu_tracker.license import _format, _secret, device_code

CFG_KEY = "app_license"
PRICE_KOBO = 150000          # ₦1,500 in kobo — Paystack's smallest unit
PRICE_LABEL = "₦1,500"


# --------------------------------------------------------------- code minting
def expected_code() -> str:
    """The unlock code that opens THIS device: XXXX-XXXX-XXXX-XXXX
    (64 bits — and only ever handed out after a confirmed payment)."""
    digest = hmac.new(_secret(), b"odu-tracker/app-unlock-v1" +
                      device_code().encode("utf-8"),
                      hashlib.sha256).digest()
    return _format(digest[:8].hex().upper())


def normalize(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


# ------------------------------------------------------------------- state
def _record(cfg) -> dict:
    rec = cfg.get(CFG_KEY)
    return rec if isinstance(rec, dict) else {}


def is_paid(cfg) -> bool:
    """Recomputed from the device fingerprint on every check — a licence
    copied from another machine, or a stale config file, never validates."""
    code = normalize(_record(cfg).get("code"))
    return bool(code) and hmac.compare_digest(code, normalize(expected_code()))


def status(cfg) -> dict:
    paid = is_paid(cfg)
    rec = _record(cfg)
    return {
        "paid": paid,
        "device_code": device_code(),
        # the unlock code is a secret until they've actually paid — never
        # leak it pre-payment or the paywall is a formality
        "unlock_code": expected_code() if paid else None,
        "kind": rec.get("kind") or None,
        "paid_at": rec.get("paid_at") or None,
        "reference": rec.get("reference") or None,
        "email": rec.get("email") or None,
        "amount_kobo": rec.get("amount_kobo") or None,
        "price_kobo": PRICE_KOBO,
        "price_label": PRICE_LABEL,
    }


# brute-force throttle for the manual activation endpoint
_attempts: list = []


def _rate_ok(limit: int = 10, window_s: int = 300) -> bool:
    now = time.time()
    while _attempts and now - _attempts[0] > window_s:
        _attempts.pop(0)
    if len(_attempts) >= limit:
        return False
    _attempts.append(now)
    return True


def activate(cfg, code: str):
    """Manual activation — the reinstall path: they paste the device code
    they saved, and the local record is rebuilt. Returns (ok, error)."""
    if not _rate_ok():
        return False, "too many attempts — wait a few minutes"
    entered = normalize(code)
    if len(entered) != 16:
        return False, "unlock code must be 16 characters (XXXX-XXXX-XXXX-XXXX)"
    if not hmac.compare_digest(entered, normalize(expected_code())):
        return False, "that code belongs to a different device"
    _record_paid(cfg, code=entered, kind="manual", reference=None, email=None)
    return True, ""


def activate_from_payment(cfg, *, reference: str = "", email: str = "",
                          source: str = "paystack") -> dict:
    """Called only AFTER Paystack said the money landed."""
    _record_paid(cfg, code=expected_code(), kind="paid",
                 reference=reference, email=email, source=source)
    return status(cfg)


def _record_paid(cfg, *, code: str, kind: str, reference, email,
                 source: str = "paystack") -> None:
    cfg.set(CFG_KEY, {
        "code": normalize(code),
        "kind": kind,
        "device": device_code(),
        "paid_at": int(time.time()),
        "reference": str(reference or "")[:80],
        "email": str(email or "")[:120],
        "amount_kobo": PRICE_KOBO,
        "source": source,
    })
