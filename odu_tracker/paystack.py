r"""Paystack verification client for the ₦1,500 base unlock.

Paystack Inline (js.paystack.co) collects the money inside the app's
first-load paywall; this module CONFIRMS it before any licence is
written. A payment is accepted only when the transaction is

    status  == "success"
    amount  >= 150,000 kobo   (₦1,500)
    currency == "NGN"

so a ₦100 test charge, a failed card charge or a refunded payment can
never unlock the app.

Where the keys live
-------------------
  * PUBLIC key (pk_...) is safe to ship — it only opens the checkout
    popup. Override with the `paystack_public_key` config key.
  * SECRET key (sk_...) must never ship in an app everyone downloads:
    it can list every customer's transactions. Two supported setups:
      1. RECOMMENDED: deploy `tools/paystack-worker.js` (a ~40-line
         Cloudflare Worker, free tier) and set `paystack_verify_url`
         to it — the app then asks the worker to verify, and the
         secret key stays on the worker.
      2. Direct: set `paystack_secret_key` (config) or the
         PAYSTACK_SECRET_KEY env var in a build only you distribute
         to buyers you trust.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from odu_tracker.app_license import PRICE_KOBO

DIRECT_BASE = "https://api.paystack.co"
# Paystack TEST public key — safe to ship (it can only open the checkout
# popup on test cards). Swap for the pk_live_... key before selling for
# real, or override at runtime with the `paystack_public_key` config key.
DEFAULT_PUBLIC_KEY = "pk_test_632321e234398026f1e8b4ace852c17fde34b9fc"
USER_AGENT = "odu-tracker-pay/1.0"

_REF_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


def public_key(cfg) -> str:
    return (cfg.get("paystack_public_key")
            or os.environ.get("PAYSTACK_PUBLIC_KEY")
            or DEFAULT_PUBLIC_KEY)


def verify_url(cfg) -> str:
    """Worker endpoint when configured, else '' (verify directly)."""
    return str(cfg.get("paystack_verify_url")
               or os.environ.get("PAYSTACK_VERIFY_URL") or "").strip()


def secret_key(cfg) -> str:
    return str(cfg.get("paystack_secret_key")
               or os.environ.get("PAYSTACK_SECRET_KEY") or "").strip()


def configured(cfg) -> dict:
    """What the paywall needs to know to render."""
    return {
        "public_key": public_key(cfg),
        "mode": "worker" if verify_url(cfg) else (
            "direct" if secret_key(cfg) else "unconfigured"),
        "price_kobo": PRICE_KOBO,
    }


def _get(url: str, headers: dict, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def verify(cfg, reference: str):
    """Confirm a Paystack transaction. Returns (ok, info, error)."""
    reference = str(reference or "").strip()
    if not _REF_RE.match(reference):
        return False, {}, "bad transaction reference"
    price = int(cfg.get("paid_amount_kobo") or PRICE_KOBO)

    try:
        if verify_url(cfg):
            sep = "&" if "?" in verify_url(cfg) else "?"
            payload = _get(f"{verify_url(cfg)}{sep}reference="
                           + urllib.parse.quote(reference),
                           {"Accept": "application/json"})
            # worker contract: {"ok": true, "amount": 150000,
            #                   "currency": "NGN", "email": "..."}
            if not payload.get("ok"):
                return False, {}, str(payload.get("error")
                                      or "payment not confirmed")
            amount = int(payload.get("amount") or 0)
            currency = str(payload.get("currency") or "")
            email = str(payload.get("email") or "")
            paid_at = payload.get("paid_at")
            channel = payload.get("channel")
        else:
            secret = secret_key(cfg)
            if not secret:
                return False, {}, ("payment verification is not configured "
                                   "on this build")
            payload = _get(DIRECT_BASE + "/transaction/verify/"
                           + urllib.parse.quote(reference),
                           {"Authorization": "Bearer " + secret,
                            "Accept": "application/json"})
            if not payload.get("status"):
                return False, {}, str(payload.get("message")
                                      or "payment not confirmed")
            data = payload.get("data") or {}
            if str(data.get("status") or "") != "success":
                return False, {}, (f"payment is {data.get('status') or 'unsettled'}"
                                   " — try again in a moment")
            amount = int(data.get("amount") or 0)
            currency = str(data.get("currency") or "")
            email = str((data.get("customer") or {}).get("email") or "")
            paid_at = data.get("paid_at")
            channel = data.get("channel")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:
            detail = ""
        return False, {}, (f"Paystack error {exc.code}"
                           + (f": {detail}" if detail else ""))
    except Exception as exc:                      # noqa: BLE001 — offline, DNS…
        return False, {}, f"cannot reach Paystack ({type(exc).__name__})"

    if currency != "NGN":
        return False, {}, f"unexpected currency {currency or '?'}"
    if amount < price:
        return False, {}, ("payment amount is short — "
                           f"expected ₦{price // 100:,}")
    return True, {"reference": reference, "amount": amount,
                  "currency": currency, "email": email,
                  "paid_at": paid_at, "channel": channel}, ""
