/* ============================================================
   ODU TRACKER landing — speed-lines, release data, turbo mode
   ============================================================ */
'use strict';

const REPO = 'Ademileke12/odu-tracker';
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ------------------------------------------------ speed-line canvas */
const canvas = document.getElementById('speedlines');
const ctx = canvas.getContext('2d');
let W = 0, H = 0, turbo = false, turboUntil = 0;

function resize() {
  const dpr = Math.min(devicePixelRatio || 1, 2);
  W = canvas.width = innerWidth * dpr;
  H = canvas.height = innerHeight * dpr;
  canvas.style.width = innerWidth + 'px';
  canvas.style.height = innerHeight + 'px';
}
resize();
addEventListener('resize', resize);

const HUES = ['#ffb632', '#ffe08a', '#22d3ee', '#7dd3fc', '#ff5470'];
const lines = [];
function spawn() {
  const z = Math.random();                    // depth 0 (far) .. 1 (near)
  return {
    x: W * (1 + Math.random() * 0.3),
    y: Math.random() * H,
    len: 40 + z * 260,
    speed: (110 + z * 620) * dprSpeed(),
    width: 0.6 + z * 1.8,
    hue: HUES[(Math.random() * HUES.length) | 0],
    alpha: 0.05 + z * 0.3,
  };
}
function dprSpeed() { return (W / innerWidth) * (turbo ? 3.4 : 1); }
for (let i = 0; i < 60; i++) lines.push(spawn());

function tick() {
  if (!reduceMotion) {
    ctx.clearRect(0, 0, W, H);
    for (const l of lines) {
      ctx.globalAlpha = l.alpha;
      ctx.strokeStyle = l.hue;
      ctx.lineWidth = l.width;
      ctx.beginPath();
      ctx.moveTo(l.x, l.y);
      ctx.lineTo(l.x + l.len, l.y);
      ctx.stroke();
      l.x -= l.speed;
      if (l.x + l.len < 0) Object.assign(l, spawn());
    }
    ctx.globalAlpha = 1;
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

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
      document.getElementById('vertext').textContent = 'v' + ver;
      const fv = document.querySelector('.footer-ver');
      if (fv) fv.textContent = 'v' + ver;
      document.querySelectorAll('.btn-sub').forEach((el) => {
        if (el.textContent.includes('v1.2.0')) el.textContent = 'free · v' + ver;
      });
    }
    for (const a of rel.assets || []) SIZES.set(a.name, a.size);
    document.querySelectorAll('[data-asset]').forEach((el) => {
      if (el.classList.contains('dl-size') || el.classList.contains('dl-link')) {
        const size = SIZES.get(el.dataset.asset);
        if (size) {
          const holder = el.closest('.dl-card')?.querySelector('.dl-size');
          if (holder) holder.textContent = pretty(size);
        }
      }
    });
  })
  .catch(() => { /* static fallbacks already in place */ });

/* hide direct-download buttons that 404 (no release yet) → keep href anyway;
   browsers land on GitHub's release page via the /latest/download URL only
   after assets exist, so also probe cheaply once */
Promise.resolve().then(async () => {
  await new Promise((r) => setTimeout(r, 1200));
  if (SIZES.size === 0) {
    document.querySelectorAll('.dl-link').forEach((a) => {
      a.href = 'https://github.com/' + REPO + '/releases';
      const holder = a.querySelector('.dl-size');
      if (holder) holder.textContent = 'see releases';
    });
  }
});

/* ------------------------------------------------ konami turbo */
const KONAMI = ['ArrowUp','ArrowUp','ArrowDown','ArrowDown',
                'ArrowLeft','ArrowRight','ArrowLeft','ArrowRight','b','a'];
let kIdx = 0;
addEventListener('keydown', (e) => {
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  kIdx = key === KONAMI[kIdx] ? kIdx + 1 : (key === KONAMI[0] ? 1 : 0);
  if (kIdx === KONAMI.length) {
    kIdx = 0;
    engageTurbo();
  }
});

let turboTimer = null;
function engageTurbo() {
  turbo = true;
  document.body.classList.add('turbo');
  const flash = document.getElementById('turbo');
  flash.classList.add('on');
  clearTimeout(turboTimer);
  turboTimer = setTimeout(disengageTurbo, 5000);
}
function disengageTurbo() {
  turbo = false;
  document.body.classList.remove('turbo');
  document.getElementById('turbo').classList.remove('on');
}
