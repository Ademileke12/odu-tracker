/* Public trial registry (v1.5.0): POST { d: 'ODU-XXXX-XXXX-XXXX' }
 *                           -> { started_at, expires_at }  (unix SECONDS)
 *
 * The SERVER owns the clock: the first call ever pins the 24h window and
 * every later call - including after a reinstall or an app-data wipe -
 * returns the SAME record, so clearing local config cannot mint a fresh
 * trial. An optional client hint `s` (unix seconds, offline-first
 * bootstrap) is honoured only for brand-new keys and only when it is a
 * sane recent timestamp. Records never expire in Redis: they are the
 * anti-reinstall evidence. Strict device-code format + per-IP rate limit
 * keep the key space from being spammed; if Redis is down this answers
 * 503 and the app falls back to its local window.
 */
'use strict';
const { redis, json, clientIp } = require('./_lib');

const TRIAL_S = 24 * 3600;
const HINT_MAX_AGE_S = 30 * 86400;
const RATE_LIMIT = 60;              // requests / IP / hour
const CODE_RE = /^ODU-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}$/;

module.exports = async (req, res) => {
  if (req.method !== 'POST') return json(res, 405, { error: 'method' });
  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch { body = {}; } }
  body = body || {};
  const d = String(body.d || '').toUpperCase();
  if (!CODE_RE.test(d)) return json(res, 400, { error: 'bad device' });

  // per-IP rate limit (reinstalls only ever need a handful of calls)
  try {
    const ipk = `trl:ip:${clientIp(req)}`;
    const n = await redis('INCR', ipk);
    if (n === 1) await redis('EXPIRE', ipk, 3600);
    if (n > RATE_LIMIT) return json(res, 429, { error: 'rate' });
  } catch {
    return json(res, 503, { error: 'kv' });
  }

  try {
    const key = `trial:${d}`;
    let started = await redis('GET', key);
    if (started == null) {
      // SECONDS everywhere: the Python client compares these against
      // time.time(), so ms here would make the trial never expire.
      let s = Math.floor(Date.now() / 1000);
      const hint = Number(body.s);
      if (Number.isFinite(hint) && hint <= s && s - hint < HINT_MAX_AGE_S) {
        s = Math.floor(hint);         // offline-first bootstrap (recent only)
      }
      const ok = await redis('SET', key, String(s), 'NX');
      if (ok == null) started = await redis('GET', key);   // lost the race
      else started = String(s);
    }
    const st = Number(started);
    if (!Number.isFinite(st)) return json(res, 503, { error: 'kv' });
    json(res, 200, { started_at: st, expires_at: st + TRIAL_S });
  } catch {
    json(res, 503, { error: 'kv' });
  }
};
