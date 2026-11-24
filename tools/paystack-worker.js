// Cloudflare Worker - server-side Paystack verification for ODU Tracker.
//
// WHY: the app verifies a payment before unlocking, but the SECRET key
// (sk_...) must never ship inside a download everyone gets. Deploy this
// ~40-line Worker on Cloudflare's free tier and point the app at it:
//   config:  paystack_verify_url = "https://<your-worker>.workers.dev/verify"
//   (or the PAYSTACK_VERIFY_URL env var when building)
//
// The worker holds PAYSTACK_SECRET_KEY as an encrypted Worker Secret:
//   wrangler secret put PAYSTACK_SECRET_KEY
//
// Contract with odu_tracker/paystack.py:
//   GET  /verify?reference=OTxxxx
//   200  {"ok":true,"amount":150000,"currency":"NGN",
//         "email":"buyer@example.com","paid_at":"...","channel":"card"}
//   4xx  {"ok":false,"error":"..."}
//
// Only `reference` is accepted; it is re-verified against Paystack with
// the secret key here, so a forged reference never unlocks anything.

const PAYSTACK = "https://api.paystack.co";
const EXPECTED_KOBO = 150000; // ₦1,500 - keep in sync with paid_amount_kobo

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname !== "/verify") {
      return json({ ok: false, error: "not found" }, 404);
    }
    if (request.method !== "GET") {
      return json({ ok: false, error: "method not allowed" }, 405);
    }

    const reference = (url.searchParams.get("reference") || "").trim();
    if (!/^[A-Za-z0-9._-]{3,64}$/.test(reference)) {
      return json({ ok: false, error: "bad transaction reference" }, 400);
    }
    const secret = PAYSTACK_SECRET_KEY;
    if (!secret) {
      return json({ ok: false, error: "worker misconfigured" }, 500);
    }

    let res;
    try {
      res = await fetch(`${PAYSTACK}/transaction/verify/${encodeURIComponent(reference)}`, {
        headers: { Authorization: `Bearer ${secret}`, Accept: "application/json" },
      });
    } catch {
      return json({ ok: false, error: "cannot reach Paystack" }, 502);
    }

    let body;
    try {
      body = await res.json();
    } catch {
      return json({ ok: false, error: "bad response from Paystack" }, 502);
    }
    if (!body || !body.status) {
      return json({ ok: false, error: (body && body.message) || "payment not confirmed" }, 400);
    }

    const data = body.data || {};
    if (data.status !== "success") {
      return json({ ok: false, error: `payment is ${data.status || "unsettled"}` }, 400);
    }
    if (data.currency !== "NGN") {
      return json({ ok: false, error: "unexpected currency" }, 400);
    }
    if (Number(data.amount || 0) < EXPECTED_KOBO) {
      return json({ ok: false, error: "payment amount is short" }, 400);
    }

    return json({
      ok: true,
      amount: Number(data.amount || 0),
      currency: data.currency,
      email: (data.customer && data.customer.email) || "",
      paid_at: data.paid_at || null,
      channel: data.channel || null,
    }, 200);
  },
};

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "content-type": "application/json",
      "access-control-allow-origin": "*", // LAN apps call from anywhere
      "cache-control": "no-store",
    },
  });
}
