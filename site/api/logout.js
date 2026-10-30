/* POST -> clears the admin session cookie. */
'use strict';
const { json, clearCookie } = require('./_lib');

module.exports = async (req, res) => {
  if (req.method !== 'POST') return json(res, 405, { error: 'method' });
  res.setHeader('Set-Cookie', clearCookie());
  json(res, 200, { ok: true });
};
