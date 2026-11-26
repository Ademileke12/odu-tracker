/* Paystack payment verification for the ₦2,500 lifetime unlock (v1.5.0).
 *
 * The paywall opens Paystack Inline with the PUBLIC key baked into the
 * app; before any licence is written the app asks THIS endpoint to
 * re-check the transaction with the SECRET key, which lives only here as
 * the PAYSTACK_SECRET_KEY Vercel env var - never in the repo and never
 * inside a downloaded build. A forged reference therefore unlocks
 * nothing: every answer is re-derived from Paystack itself.
 *
 * Contract with odu_tracker/paystack.py (worker branch):
 *   GET  /api/verify?reference=OTxxxx
 *   200  {"ok":true,"amount":250000,"currency":"NGN",
 *         "email":"buyer@example.com","paid_at":"...","channel":"card"}
 *   200  {"ok":false,"error":"human-readable reason"}
 * Business outcomes answer 200 on purpose: the app surfaces `error`
 * straight to the buyer, while a non-2xx would arrive as an opaque
 * "Paystack error NNN". Only malformed requests get 4xx/5xx.
 *
 * Enforced here AND again in paystack.verify (defence in depth):
 * status == success, currency == NGN, amount >= 250000 kobo.
 */
'use strict';
const { redis, json, clientIp } = require('./_lib');

const PAYSTACK = 'https://api.paystack.co';
const EXPECTED_KOBO = 250000;   // ₦2,500 - keep in sync with paid_amount_kobo
const REF_RE = /^[A-Za-z0-9._-]{3,64}$/;
const RATE_LIMIT = 60;          // verify calls / IP / hour

module.exports = async (req, res) => {
  if (req.method !== 'GET') {
    return json(res, 405, { ok: false, error: 'method not allowed' });
  }
  const reference = new URL(req.url, 'http://localhost').searchParams.get('reference') || '';
  if (!REF_RE.test(reference)) {
    return json(res, 400, { ok: false, error: 'bad transaction reference' });
  }

  const secret = process.env.PAYSTACK_SECRET_KEY || '';
  if (!secret) {
    return json(res, 200, {
      ok: false,
      error: 'payment verification is not configured on the server',
    });
  }

  // Best-effort abuse brake. A real payment must never fail because KV
  // is down, so a rate-limit error means "no limit this time" - the
  // worst an attacker gains is free Paystack verify calls.
  try {
    const ipk = `vfy:ip:${clientIp(req)}`;
    const n = await redis('INCR', ipk);
    if (n === 1) await redis('EXPIRE', ipk, 3600);
    if (n > RATE_LIMIT) {
      return json(res, 429, {
        ok: false,
        error: 'too many attempts - wait a minute and retry',
      });
    }
  } catch { /* fail open */ }

  let upstream;
  try {
    upstream = await fetch(`${PAYSTACK}/transaction/verify/${encodeURIComponent(reference)}`, {
      headers: { Authorization: `Bearer ${secret}`, Accept: 'application/json' },
    });
  } catch {
    return json(res, 200, {
      ok: false,
      error: 'cannot reach Paystack - check your connection and retry',
    });
  }

  let body;
  try {
    body = await upstream.json();
  } catch {
    return json(res, 200, {
      ok: false,
      error: 'bad response from Paystack - retry in a moment',
    });
  }
  if (!body || !body.status) {
    return json(res, 200, {
      ok: false,
      error: (body && body.message) || 'payment not confirmed',
    });
  }

  const data = body.data || {};
  if (data.status !== 'success') {
    return json(res, 200, {
      ok: false,
      error: `payment is ${data.status || 'unsettled'} - try again in a moment`,
    });
  }
  if (data.currency !== 'NGN') {
    return json(res, 200, { ok: false, error: 'unexpected currency' });
  }
  if (Number(data.amount || 0) < EXPECTED_KOBO) {
    return json(res, 200, { ok: false, error: 'payment amount is short' });
  }

  return json(res, 200, {
    ok: true,
    amount: Number(data.amount || 0),
    currency: data.currency,
    email: (data.customer && data.customer.email) || '',
    paid_at: data.paid_at || null,
    channel: data.channel || null,
  });
};
