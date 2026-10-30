/* Shared helpers for the ODU Tracker stats API (Vercel serverless).
 *
 * Data store : Upstash Redis (free tier) via its REST API — plain fetch,
 *              zero npm dependencies, works on any Vercel Node runtime.
 * Auth       : PBKDF2-SHA256 (100k iters) over an ADMIN_PASSWORD_HASH env
 *              var — the plaintext password never exists in code or config.
 *              Sessions are HMAC-signed expiring tokens in an HttpOnly,
 *              SameSite=Strict cookie; every comparison is constant-time.
 * Abuse      : login attempts are rate-limited per client IP in Redis.
 */
'use strict';

const crypto = require('crypto');

const REDIS_URL = process.env.UPSTASH_REDIS_REST_URL || '';
const REDIS_TOKEN = process.env.UPSTASH_REDIS_REST_TOKEN || '';
const ADMIN_HASH = process.env.ADMIN_PASSWORD_HASH || '';
const SESSION_SECRET = process.env.SESSION_SECRET || '';
const SESSION_TTL_S = 12 * 3600; // 12 h

/* ---------------------------------------------------------------- redis */
async function redis(cmd, ...args) {
  if (!REDIS_URL || !REDIS_TOKEN) throw new Error('kv not configured');
  const r = await fetch(REDIS_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${REDIS_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify([cmd, ...args]),
  });
  if (!r.ok) throw new Error(`kv ${r.status}`);
  const j = await r.json();
  return j.result;
}

function redisUp() {
  return Boolean(REDIS_URL && REDIS_TOKEN);
}

/* ------------------------------------------------------------- helpers */
function json(res, status, body) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(body));
}

function clientIp(req) {
  // Vercel sets x-forwarded-for; first hop is the real client.
  const fwd = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  return fwd || req.socket?.remoteAddress || 'unknown';
}

function readCookie(req, name) {
  const raw = req.headers.cookie || '';
  for (const part of raw.split(';')) {
    const [k, ...v] = part.trim().split('=');
    if (k === name) return decodeURIComponent(v.join('='));
  }
  return '';
}

/* ---------------------------------------------------------------- auth */
function pbkdf2(password, saltB64, iterations) {
  return new Promise((resolve, reject) => {
    const salt = Buffer.from(saltB64, 'base64');
    crypto.pbkdf2(password, salt, iterations, 32, 'sha256', (err, dk) =>
      err ? reject(err) : resolve(dk));
  });
}

/** Verify a password against the stored `pbkdf2$iters$salt$hash` blob. */
async function verifyPassword(password) {
  if (!ADMIN_HASH || !SESSION_SECRET) return false;
  const parts = ADMIN_HASH.split('$');
  if (parts.length !== 4 || parts[0] !== 'pbkdf2') return false;
  const [, iters, salt, want] = parts;
  let got;
  try {
    got = await pbkdf2(String(password), salt, parseInt(iters, 10) || 100000);
  } catch {
    return false;
  }
  const wantBuf = Buffer.from(want, 'base64');
  return got.length === wantBuf.length && crypto.timingSafeEqual(got, wantBuf);
}

/* Session token = base64url(payload).hmac(payload); payload = expiry unix. */
function signToken(exp) {
  const payload = Buffer.from(JSON.stringify({ exp })).toString('base64url');
  const mac = crypto.createHmac('sha256', SESSION_SECRET).update(payload).digest('base64url');
  return `${payload}.${mac}`;
}

function verifyToken(token) {
  if (!token || !SESSION_SECRET) return false;
  const [payload, mac] = String(token).split('.');
  if (!payload || !mac) return false;
  const want = crypto.createHmac('sha256', SESSION_SECRET).update(payload).digest('base64url');
  const a = Buffer.from(mac), b = Buffer.from(want);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return false;
  try {
    const { exp } = JSON.parse(Buffer.from(payload, 'base64url').toString());
    return typeof exp === 'number' && exp > Math.floor(Date.now() / 1000);
  } catch {
    return false;
  }
}

function sessionCookie(token) {
  return `odu_admin=${encodeURIComponent(token)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${SESSION_TTL_S}`;
}

function clearCookie() {
  return 'odu_admin=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0';
}

/** Handler wrapper: runs fn only with a valid session, else 401. */
function requireAuth(fn) {
  return async (req, res) => {
    if (!verifyToken(readCookie(req, 'odu_admin'))) return json(res, 401, { error: 'unauthorized' });
    return fn(req, res);
  };
}

/** Sliding-window login rate limit: N attempts per IP per window. */
async function loginAllowed(ip, max = 8, windowS = 900) {
  const key = `rl:login:${crypto.createHash('sha256').update(ip).digest('hex').slice(0, 16)}`;
  const n = parseInt(await redis('INCR', key), 10) || 1;
  if (n === 1) await redis('EXPIRE', key, windowS);
  return n <= max;
}

module.exports = {
  redis, redisUp, json, clientIp, readCookie,
  verifyPassword, signToken, sessionCookie, clearCookie,
  requireAuth, loginAllowed, SESSION_TTL_S,
};
