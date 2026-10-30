/* GET (auth required) -> the admin stats payload.
 *
 * Two sources merged:
 *  1. KV counters  — page views / download clicks, live from beacons.
 *  2. GitHub API   — the REAL per-asset download counts of every release
 *                    (retroactive: covers downloads long before this page
 *                    existed). Cached 10 min in KV to respect rate limits.
 */
'use strict';
const { redis, redisUp, json, requireAuth } = require('./_lib');

const REPO = 'Ademileke12/odu-tracker';

async function ghDownloads() {
  const cache = await redis('GET', 'gh:assets');
  if (cache) return JSON.parse(cache);

  const r = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=30`, {
    headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'odu-stats' },
  });
  if (!r.ok) throw new Error(`github ${r.status}`);
  const releases = await r.json();

  const perAsset = {};
  let total = 0, releases_count = releases.length;
  for (const rel of releases) {
    for (const a of rel.assets || []) {
      perAsset[a.name] = (perAsset[a.name] || 0) + a.download_count;
      total += a.download_count;
    }
  }
  const out = { perAsset, total, releases: releases_count, checkedAt: Date.now() };
  await redis('SET', 'gh:assets', JSON.stringify(out), 'EX', 600);
  return out;
}

async function series(prefix, days = 14) {
  // daily counters t:<prefix>:d:YYYY-MM-DD; missing day = 0
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400e3).toISOString().slice(0, 10);
    const v = parseInt(await redis('GET', `t:${prefix}:d:${d}`), 10) || 0;
    out.push({ day: d, n: v });
  }
  return out;
}

module.exports = requireAuth(async (req, res) => {
  if (req.method !== 'GET') return json(res, 405, { error: 'method' });
  if (!redisUp()) return json(res, 503, { error: 'backend not configured' });

  const views = parseInt(await redis('GET', 't:view:total'), 10) || 0;
  const clicks = parseInt(await redis('GET', 't:click:total'), 10) || 0;

  // per-asset click counters (names the page sends)
  const clickAssets = {};
  for (const a of ['ODU-Tracker-Setup.exe', 'ODU-Tracker-Portable.exe',
                   'odu-tracker-amd64.deb', 'ODU-Tracker.apk']) {
    clickAssets[a] = parseInt(await redis('GET', `t:click:${a}`), 10) || 0;
  }

  let gh = null, ghError = null;
  try { gh = await ghDownloads(); } catch (e) { ghError = String(e.message || e); }

  json(res, 200, {
    views, clicks, clickAssets,
    downloads: gh,                    // { perAsset, total, releases, checkedAt }
    ghError,
    viewSeries: await series('view'),
    clickSeries: await series('click'),
    generatedAt: Date.now(),
  });
});
