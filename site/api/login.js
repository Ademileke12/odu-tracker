/* POST { password } -> sets signed HttpOnly session cookie.
 * Rate-limited per IP; constant-time verification; generic error text
 * (no oracle on whether the account exists / password length).
 */
'use strict';
const {
  redisUp, json, clientIp, verifyPassword,
  signToken, sessionCookie, loginAllowed, SESSION_TTL_S,
} = require('./_lib');

module.exports = async (req, res) => {
  if (req.method !== 'POST') return json(res, 405, { error: 'method' });
  if (!redisUp() || !process.env.ADMIN_PASSWORD_HASH)
    return json(res, 503, { error: 'backend not configured' });

  const ip = clientIp(req);
  if (!(await loginAllowed(ip))) return json(res, 429, { error: 'too many attempts, try later' });

  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch { body = {}; } }
  const ok = await verifyPassword((body || {}).password || '');
  if (!ok) return json(res, 401, { error: 'wrong password' });

  const token = signToken(Math.floor(Date.now() / 1000) + SESSION_TTL_S);
  res.setHeader('Set-Cookie', sessionCookie(token));
  json(res, 200, { ok: true });
};
