/* ============================================================
   ODU TRACKER landing — theme toggle, release data, reveal, boost
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
      // external mirrors (data-external) aren't in the GitHub release — skip them
      const external = card.querySelector('.dl-link')?.dataset.external === 'true';
      const size = !external && asset && SIZES.get(asset);
      if (size && holder) holder.textContent = pretty(size);
    });
  })
  .catch((code) => {
    /* 404 is the ONLY case where the direct URLs genuinely can't resolve —
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
