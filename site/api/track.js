/* Public beacon: page views + download clicks. Called by script.js.
 * Body: { e: 'view' } or { e: 'click', a: '<asset name>' }
 * Counters only — no cookies, no raw IPs (only a salted hash bucket),
 * so this stays lightweight and privacy-sane.
 */
'use strict';
const { redis, json, clientIp } = require('./_lib');
const crypto = require('crypto');

const DAY = 86400;

module.exports = async (req, res) => {
  if (req.method !== 'POST') return json(res, 405, { error: 'method' });
  let body = req.body;
  if (typeof body === 'string') { try { body = JSON.parse(body); } catch { body = {}; } }
  body = body || {};
  const ev = body.e === 'click' ? 'click' : 'view';
  const asset = ev === 'click' ? String(body.a || 'unknown').slice(0, 80) : null;

  // per-visitor bucket: salted ip+ua hash -> unique-visitor dedupe, 1-day window
  const visitor = crypto
    .createHmac('sha256', process.env.SESSION_SECRET || 'dev')
    .update(clientIp(req) + '|' + (req.headers['user-agent'] || ''))
    .digest('hex').slice(0, 16);
  const day = new Date().toISOString().slice(0, 10);

  try {
    const cmds = [
      ['INCR', `t:${ev}:total`],
      ['INCR', `t:${ev}:d:${day}`],
      ['EXPIRE', `t:${ev}:d:${day}`, 90 * DAY],
      ['SADD', `t:${ev}:u:${day}`, visitor],
      ['EXPIRE', `t:${ev}:u:${day}`, 90 * DAY],
      ['INCR', `t:${ev}:v:${visitor}:${day}`],
      ['EXPIRE', `t:${ev}:v:${visitor}:${day}`, 2 * DAY],
    ];
    if (asset) cmds.push(['INCR', `t:click:${asset}`], ['EXPIRE', `t:click:${asset}`, 365 * DAY]);
    for (const c of cmds) await redis(...c);   // sequential: Upstash free tier
  } catch { /* tracking must never break the page */ }
  json(res, 204, {});
};
