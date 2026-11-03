/* ============================================================
   ODU TRACKER landing, theme toggle, release data, reveal, boost
   ============================================================ */
'use strict';

const REPO = 'Ademileke12/odu-tracker';
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ------------------------------------------------ light / dark toggle */
const modeBtn = document.getElementById('modeBtn');
const modeIco = document.getElementById('modeIco');

function applyMode(dark) {
  document.body.classList.toggle('dark', dark);
  if (modeIco) modeIco.textContent = dark ? '☀' : '☾';
  try { localStorage.setItem('odu-mode', dark ? 'dark' : 'light'); } catch { /* private mode */ }
}

let storedDark = false;
try { storedDark = localStorage.getItem('odu-mode') === 'dark'; } catch { /* ignore */ }
applyMode(storedDark);

modeBtn?.addEventListener('click', () => {
  applyMode(!document.body.classList.contains('dark'));
});

/* ------------------------------------------------ light ads
   Paste your Google AdSense publisher id below (Vercel settings can also
   hold it, but this is the one switch). Empty string = the slot on the
   page stays hidden and ZERO ad code is ever loaded.
   1. AD_CLIENT: your id from AdSense, like "ca-pub-1234567890123456"
   2. optionally AD_SLOT: the id of one responsive display unit you create
      in AdSense. If you leave it empty, only the page-wide "Auto ads"
      toggle in AdSense will place anything.
   3. name site/ads.txt.example to ads.txt with your id, see that file.   */
const AD_CLIENT = '';
const AD_SLOT = '';
(function ads() {
  const slot = document.getElementById('adSlot');
  if (!slot || !AD_CLIENT) return;
  const s = document.createElement('script');
  s.async = true;
  s.crossOrigin = 'anonymous';
  s.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client='
    + encodeURIComponent(AD_CLIENT);
  document.head.appendChild(s);
  if (!AD_SLOT) return;                     // auto ads only
  slot.classList.add('on');
  slot.setAttribute('aria-hidden', 'false');
  const ins = document.createElement('ins');
  ins.className = 'adsbygoogle';
  ins.style.display = 'block';
  ins.setAttribute('data-ad-client', AD_CLIENT);
  ins.setAttribute('data-ad-slot', AD_SLOT);
  ins.setAttribute('data-ad-format', 'auto');
  ins.setAttribute('data-full-width-responsive', 'true');
  slot.appendChild(ins);
  try { (window.adsbygoogle = window.adsbygoogle || []).push({}); } catch { /* retry noop */ }
})();

/* ------------------------------------------------ release announcement
   Dismissal is stored PER RELEASE (keyed on data-rel), when v1.4.0
   ships the strip comes back on its own. */
const newsbar = document.getElementById('newsbar');
if (newsbar) {
  const rel = newsbar.dataset.rel || 'v1';
  let gone = '';
  try { gone = localStorage.getItem('odu-news-dismissed') || ''; } catch { /* ignore */ }
  if (gone === rel) newsbar.hidden = true;
  document.getElementById('newsbarX')?.addEventListener('click', () => {
    newsbar.hidden = true;
    try { localStorage.setItem('odu-news-dismissed', rel); } catch { /* private mode */ }
  });
}

/* ------------------------------------------------ reveal on scroll */
const io = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
  }
}, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
document.querySelectorAll('.reveal').forEach((el) => io.observe(el));

/* ------------------------------------------------ release data */
const API = `https://api.github.com/repos/${REPO}/releases/latest`;
const SIZES = new Map();

function pretty(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(0) + ' MB';
  return (bytes / 1024).toFixed(0) + ' KB';
}

fetch(API, { headers: { Accept: 'application/vnd.github+json' } })
  .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
  .then((rel) => {
    const ver = (rel.tag_name || '').replace(/^v/, '');
    if (ver) {
      const v = document.getElementById('vertext');
      if (v) v.textContent = 'v' + ver;
      const fv = document.querySelector('.footer-ver');
      if (fv) fv.textContent = 'v' + ver;
    }
    for (const a of rel.assets || []) SIZES.set(a.name, a.size);
    document.querySelectorAll('.dl-card').forEach((card) => {
      const holder = card.querySelector('.dl-size');
      const asset = holder?.dataset.asset;
      // external mirrors (data-external) aren't in the GitHub release, skip them
      const external = card.querySelector('.dl-link')?.dataset.external === 'true';
      const size = !external && asset && SIZES.get(asset);
      if (size && holder) holder.textContent = pretty(size);
    });
  })
  .catch((code) => {
    /* 404 is the ONLY case where the direct URLs genuinely can't resolve 
       no release published yet. Anything else (API rate limit, ad blocker,
       network drop) leaves the buttons untouched: they point at
       …/releases/latest/download/<asset>, permanent direct-download URLs
       that work whenever a release exists. Never reroute on a failed check. */
    if (code !== 404) return;

    document.querySelectorAll('.dl-link').forEach((a) => {
      if (a.dataset.external === 'true') return;
      a.href = 'https://github.com/' + REPO + '/releases';
      const holder = a.querySelector('.dl-size');
      if (holder) holder.textContent = 'no release yet';
    });
    const note = document.getElementById('dlFallback');
    if (note) note.hidden = false;
  });

/* ------------------------------------------------ stats beacon
   Counters land in the KV store behind /api/track (admin page reads them).
   sendBeacon survives navigation, so a download click is counted even
   though the browser immediately leaves for GitHub. Failures are silent 
   analytics must never break the page. */
function beacon(payload) {
  try {
    const blob = new Blob([JSON.stringify(payload)], { type: 'application/json' });
    navigator.sendBeacon?.('/api/track', blob) ||
      fetch('/api/track', { method: 'POST', body: blob, keepalive: true }).catch(() => {});
  } catch { /* ignore */ }
}

beacon({ e: 'view' });

document.querySelectorAll('.dl-link').forEach((a) => {
  a.addEventListener('click', () => beacon({ e: 'click', a: a.dataset.asset || 'unknown' }));
});

/* ------------------------------------------------ copy donation account */
const copyAcct = document.getElementById('copyAcct');
copyAcct?.addEventListener('click', async () => {
  const num = (document.getElementById('supportAcct')?.textContent || '').trim();
  if (!num) return;
  try {
    await navigator.clipboard.writeText(num);
  } catch {
    const area = document.createElement('textarea');
    area.value = num;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); } catch { /* ignore */ }
    area.remove();
  }
  const label = copyAcct.textContent;
  copyAcct.textContent = 'Copied';
  copyAcct.classList.add('copied');
  setTimeout(() => {
    copyAcct.textContent = label;
    copyAcct.classList.remove('copied');
  }, 1600);
});

/* ------------------------------------------------ konami boost */
const KONAMI = ['ArrowUp','ArrowUp','ArrowDown','ArrowDown',
                'ArrowLeft','ArrowRight','ArrowLeft','ArrowRight','b','a'];
let kIdx = 0;

addEventListener('keydown', (e) => {
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  kIdx = key === KONAMI[kIdx] ? kIdx + 1 : (key === KONAMI[0] ? 1 : 0);
  if (kIdx === KONAMI.length) {
    kIdx = 0;
    engageBoost();
  }
});

let boostTimer = null;
function engageBoost() {
  document.body.classList.add('boost');
  const flash = document.getElementById('turbo');
  flash?.classList.add('on');
  clearTimeout(boostTimer);
  boostTimer = setTimeout(disengageBoost, 5000);
}
function disengageBoost() {
  document.body.classList.remove('boost');
  document.getElementById('turbo')?.classList.remove('on');
}
