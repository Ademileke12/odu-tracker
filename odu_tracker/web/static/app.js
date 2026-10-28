/* ================================================================
   ODU TRACKER · UI v3  — vanilla JS, no dependencies
   charts: bar / line / area / donut (+ toast, theme, live pollers)
   ================================================================ */
"use strict";

/* ---------------------------------------------------------------- utils */
const $  = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

const fmtGB    = mb => mb == null ? "—" : (mb / 1024).toFixed(mb >= 10240 ? 1 : 2) + " GB";
const fmtG     = g  => g == null ? "—" : (+g >= 100 ? (+g).toFixed(1) : (+g).toFixed(2)) + " GB";
const fmtMbp   = v  => (+v || 0) >= 100 ? (+v).toFixed(0) : (+v || 0).toFixed(2);
const fmtT     = ts => ts ? new Date(ts * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) : "—";
const fmtDT    = ts => ts ? new Date(ts * 1000).toLocaleString([], {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"}) : "—";
const ago      = ts => {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return `${s | 0}s ago`;
  if (s < 3600) return `${(s / 60) | 0}m ago`;
  if (s < 86400) return `${(s / 3600) | 0}h ago`;
  return `${(s / 86400) | 0}d ago`;
};

let toastTmr = null;
function toast(msg, ms = 3500) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.display = "block";
  clearTimeout(toastTmr);
  toastTmr = setTimeout(() => el.style.display = "none", ms);
}

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, opts));
  if (r.status === 401) { showAuth(); throw new Error("signed out"); }
  let body = null;
  try { body = await r.json(); } catch { /* no body */ }
  if (!r.ok) throw Object.assign(new Error((body && body.error) || `HTTP ${r.status}`), {status: r.status, body});
  return body;
}

let deferredInstallPrompt = null;

async function registerPwa() {
  if (!("serviceWorker" in navigator)) return;
  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch (err) {
    console.warn("Service worker registration failed:", err);
  }
}

function initPwaInstall() {
  const installBtn = document.getElementById("installPwaBtn");
  if (!installBtn) return;

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstallPrompt = event;
    installBtn.hidden = false;
  });

  installBtn.addEventListener("click", async () => {
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    installBtn.hidden = true;
  });
}

/* ---------------------------------------------------------------- theme */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  $("#themeLabel").textContent = t === "dark" ? "Dark" : "Light";
  $("#themeIco").innerHTML = t === "dark"
    ? '<path d="M20 14A8.5 8.5 0 0 1 10 4a8.5 8.5 0 1 0 10 10z"/>'
    : '<circle cx="12" cy="12" r="4.4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6"/>';
  localStorage.setItem("odu.theme", t);
}
function initTheme() {
  const saved = localStorage.getItem("odu.theme");
  applyTheme(saved || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  $("#themeBtn").onclick = () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}

function initOfflineState() {
  const syncStatus = () => {
    const online = navigator.onLine;
    const connDot = document.getElementById("connDot");
    const connText = document.getElementById("connText");
    if (!connDot || !connText) return;
    connDot.className = online ? "dot on" : "dot warn";
    connText.textContent = online ? "router connected" : "offline — waiting for network";
    if (!online) toast("Offline — the dashboard is waiting for the local ODU network.", 3000);
  };
  window.addEventListener("online", syncStatus);
  window.addEventListener("offline", syncStatus);
  syncStatus();
}

/* ---------------------------------------------------------------- charts
   chart(boxElement, cfg)
   cfg: {type:"bar"|"line"|"area"|"donut", series:[{ts,v}]
         parts:[{label,value}], color, fill, unit, height, fmt}
   ---------------------------------------------------------------- */
const tip = () => $("#chartTip");

function showTip(evt, box, text) {
  const t = tip(), r = box.getBoundingClientRect();
  t.innerHTML = text;
  t.style.display = "block";
  const x = evt.clientX - r.left + 14, y = evt.clientY - r.top - 34;
  t.style.left = Math.max(2, Math.min(x, r.width - t.offsetWidth - 4)) + "px";
  t.style.top  = Math.max(2, y) + "px";
}
function hideTip() { tip().style.display = "none"; }

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

function chart(box, cfg) {
  box.innerHTML = "";
  hideTip();
  const H = cfg.height || 200, W = 760;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  box.appendChild(svg);
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, at) => {
    const e = document.createElementNS(NS, n);
    for (const k in at) e.setAttribute(k, at[k]);
    return e;
  };
  const uid = "g" + (chart.uid = (chart.uid || 0) + 1);
  const gold = cfg.color || "var(--accent)";
  if (cfg.type === "donut") { donut(box, cfg, NS, el, uid); return; }
  const cfg_fill_color = cfg.color || (cfg.type === "bar" ? "#ffb632" : "#6fa7ff");
  const defs = el("defs", {});
  defs.innerHTML =
    `<linearGradient id="${uid}b" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="${cfg_fill_color}" stop-opacity=".95"/>
       <stop offset="1" stop-color="${cfg_fill_color}" stop-opacity=".28"/>
     </linearGradient>`;
  svg.appendChild(defs);

  const series = (cfg.series || []).map(p => ({...p, v: Math.max(0, +p.v || 0)}));
  if (!series.length) {
    box.innerHTML = '<div class="empty">no data yet — the poller is filling samples</div>';
    return;
  }
  const padL = 46, padR = 10, padT = 12, padB = 24;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = niceMax(Math.max(...series.map(p => p.v)) * 1.15);
  const fmt = cfg.fmt || (v => v.toFixed(2) + " " + (cfg.unit || ""));
  // grid + y labels
  for (let i = 0; i <= 4; i++) {
    const y = padT + ih - ih * i / 4;
    svg.appendChild(el("line", {x1: padL, x2: W - padR, y1: y, y2: y,
      stroke: "var(--grid-line)", "stroke-width": 1}));
    const t = el("text", {x: padL - 8, y: y + 3.5, "text-anchor": "end",
      fill: "var(--faint)", "font-size": 10});
    t.textContent = fmt(max * i / 4);
    svg.appendChild(t);
  }
  const xLabel = (i, t) => {
    const x = padL + iw * (i + .5) / series.length;
    const e = el("text", {x, y: H - 8, "text-anchor": "middle",
      fill: "var(--faint)", "font-size": 10});
    e.textContent = t;
    svg.appendChild(e);
  };
  const xAt = (i) => padL + iw * (i + .5) / series.length;

  if (cfg.type === "bar") {
    const gap = iw / series.length;
    const bw = Math.min(46, Math.max(3, gap * 0.62));
    series.forEach((p, i) => {
      const h = Math.max(1.5, ih * p.v / max);
      const r = el("rect", {x: xAt(i) - bw / 2, y: padT + ih - h, width: bw, height: h,
        rx: Math.min(5, bw / 3), fill: `url(#${uid}b)`, class: "cbar"});
      r.style.transition = "opacity .15s";
      const tipTxt = `<b>${fmt(p.v)}</b> · ${p.label}`;
      r.addEventListener("mousemove", e => { r.style.opacity = 0.75; showTip(e, box, tipTxt); });
      r.addEventListener("mouseleave", () => { r.style.opacity = 1; hideTip(); });
      svg.appendChild(r);
      if (series.length <= 14 || i % Math.ceil(series.length / 9) === 0) xLabel(i, p.label);
    });
  } else {                                    // line / area
    const pts = series.map((p, i) => [padL + iw * (series.length === 1 ? .5 : i / (series.length - 1)),
                                      padT + ih - Math.max(1, ih * p.v / max)]);
    let d = "M" + pts.map(p => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L");
    if (cfg.type === "area") {
      const area = d + ` L${pts[pts.length - 1][0].toFixed(1)} ${padT + ih} L${pts[0][0].toFixed(1)} ${padT + ih} Z`;
      svg.appendChild(el("path", {d: area, fill: `url(#${uid}b)`, stroke: "none"}));
    }
    svg.appendChild(el("path", {d, fill: "none", stroke: cfg_fill_color,
      "stroke-width": 2.4, "stroke-linecap": "round", "stroke-linejoin": "round"}));
    if (series.length <= 40) pts.forEach((p, i) => {
      const c = el("circle", {cx: p[0], cy: p[1], r: 3, fill: "var(--panel)",
        stroke: cfg_fill_color, "stroke-width": 2});
      c.addEventListener("mousemove", e => showTip(e, box, `<b>${fmt(series[i].v)}</b> · ${series[i].label}`));
      c.addEventListener("mouseleave", hideTip);
      svg.appendChild(c);
    });
    const step = Math.max(1, Math.ceil(series.length / 8));
    series.forEach((p, i) => { if (i % step === 0 || i === series.length - 1) xLabel(i, p.label); });
  }
}

function donut(box, cfg, NS, el, uid) {
  const parts = (cfg.parts || []).filter(p => p.value > 0);
  if (!parts.length) {
    box.innerHTML = '<div class="empty">no usage yet in this window</div>';
    return;
  }
  const total = parts.reduce((a, p) => a + p.value, 0);
  const H = cfg.height || 200, R = 64, cx = 110, cy = H / 2, C = 2 * Math.PI * R;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${cx * 2 + 330} ${H}`);
  svg.setAttribute("width", "100%");
  box.appendChild(svg);
  const colors = ["var(--accent)", "#6fa7ff", "#3fd68e", "#b18cff", "#ff7b72"];
  let off = 0;
  parts.forEach((p, i) => {
    const frac = p.value / total;
    const ring = el("circle", {cx, cy, r: R, fill: "none",
      stroke: colors[i % colors.length], "stroke-width": 21,
      "stroke-dasharray": `${Math.max(0.5, frac * C - 2)} ${C}`, "stroke-dashoffset": -off + C / 4,
      "stroke-linecap": "butt"});
    ring.addEventListener("mousemove", e =>
      showTip(e, box, `<b>${((frac * 100)).toFixed(1)}%</b> · ${p.label} (${fmt(p.value)})`));
    ring.addEventListener("mouseleave", hideTip);
    svg.appendChild(ring);
    off += frac * C;
  });
  const center = el("text", {x: cx, y: cy - 2, "text-anchor": "middle", fill: "var(--text)",
    "font-size": 20, "font-weight": 700});
  center.textContent = cfg.fmt ? cfg.fmt(total) : total.toFixed(1);
  svg.appendChild(center);
  const cl = el("text", {x: cx, y: cy + 16, "text-anchor": "middle", fill: "var(--faint)", "font-size": 10});
  cl.textContent = cfg.center || "total";
  svg.appendChild(cl);
  // legend
  let ly = cy - parts.length * 11 + 22;
  parts.forEach((p, i) => {
    const col = colors[i % colors.length];
    svg.appendChild(el("rect", {x: 262, y: ly - 8, width: 11, height: 11, rx: 3, fill: col}));
    const t = el("text", {x: 282, y: ly + 1, fill: "var(--text)", "font-size": 12.5});
    t.textContent = p.label;
    const v = el("text", {x: cx * 2 + 318, y: ly + 1, fill: "var(--faint)", "font-size": 12, "text-anchor": "end"});
    v.textContent = cfg.fmt ? cfg.fmt(p.value) : p.value;
    svg.appendChild(t); svg.appendChild(v);
    ly += 23;
  });
}

/* leaderboard (horizontal HTML bars) for per-device activity */
function leaderboard(box, rows, valFmt) {
  if (!rows.length) { box.innerHTML = '<div class="empty">no activity recorded yet</div>'; return; }
  const max = Math.max(...rows.map(r => r.value), 1);
  box.innerHTML = rows.map(r =>
    `<div class="row"><div class="nm">${esc(r.name)}<small>${esc(r.sub || "")}</small></div>` +
    `<div class="bar-track"><div class="bar-fill" style="width:${(r.value / max * 100).toFixed(1)}%"></div></div>` +
    `<div class="val">${valFmt(r.value)}</div></div>`).join("");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- state */
const S = {
  usageType: localStorage.getItem("odu.usageType") || "bar",
  ratesType: localStorage.getItem("odu.ratesType") || "area",
  usageHours: 24, ratesMinutes: 30,
  lastStatus: null, devices: [],
  tmrs: {},
};

/* ---------------------------------------------------------------- auth */
function showAuth() {
  $("#authView").hidden = false;
  $("#mainView").hidden = true;
}

const SPLASH_MIN_MS = 1500;

async function boot() {
  initTheme();
  initOfflineState();
  registerPwa();
  initPwaInstall();
  const t0 = Date.now();
  try {
    const meta = await api("/api/meta");
    const wait = Math.max(0, SPLASH_MIN_MS - (Date.now() - t0));
    setTimeout(() => route(meta), wait);
  } catch (e) {
    $("#splashStatus").textContent = "tracker service unreachable…";
    setTimeout(boot, 2500);
  }
}

function fadeSplash() {
  const s = $("#splashView");
  s.classList.add("fade");
  setTimeout(() => { s.hidden = true; }, 420);
}

function showNoOdu() {
  fadeSplash();
  $("#noOduView").hidden = false;
  startOduWatch();
}

let oduWatchTmr = null;
function startOduWatch() {
  if (oduWatchTmr) clearInterval(oduWatchTmr);
  oduWatchTmr = setInterval(async () => {
    try {
      const c = await api("/api/check");
      if (c.odu) { clearInterval(oduWatchTmr); oduWatchTmr = null; $("#noOduView").hidden = true; boot(); }
    } catch { /* keep watching */ }
  }, 4000);
}
$("#noOduRetry").onclick = async () => {
  $("#noOduStatus").textContent = "checking…";
  try {
    const c = await api("/api/check");
    if (c.odu) {
      $("#noOduView").hidden = true;
      if (oduWatchTmr) { clearInterval(oduWatchTmr); oduWatchTmr = null; }
      boot();
    } else {
      $("#noOduStatus").textContent =
        `still nothing at ${c.ip || "192.168.0.1"} — are you on the ODU Wi-Fi?`;
      setTimeout(() => $("#noOduStatus").textContent =
        "Waiting for the ODU network… rechecking every 4 s", 3000);
    }
  } catch (e) {
    $("#noOduStatus").textContent = "tracker backend unreachable";
  }
};

async function route(meta) {
  if (!meta.reachable || !meta.odu) { showNoOdu(); return; }
  fadeSplash();
  if (meta.authed) { enterMain(); return; }
  showAuth();
  $("#authTitle").textContent = meta.setup_done ? "Welcome back" : "Set up your tracker";
  $("#authSub").textContent = meta.setup_done
    ? "Sign in with your ODU admin password — one time, then this PC is remembered for 30 days."
    : "Create the link to your router: enter the ODU admin password (it is stored only on this PC).";
  const lockWarn = meta.lock_secs && meta.lock_secs > 3600
    ? `⚠ Router login locked ${(meta.lock_secs / 3600).toFixed(1)} h remaining`
    : (meta.lock_secs ? `Router login cooldown — try again in ${meta.lock_secs}s` : "");
  const lowWarn = meta.attempts_left != null && meta.attempts_left <= 2
    ? `⚠ Only ${meta.attempts_left} login attempt${meta.attempts_left === 1 ? "" : "s"} left before a 24h router lock — only type the correct password`
    : "";
  const warn = lockWarn || lowWarn;
  if (warn) {
    const c = $("#authAttempts");
    c.hidden = false; c.classList.add("warn");
    c.textContent = warn;
  }
  $("#brandIp").textContent = `Router ${meta.router_ip} · local only · nothing leaves your LAN`;
}

$("#authForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const btn = $("#authBtn");
  btn.disabled = true; btn.textContent = "Signing in…";
  $("#authErr").textContent = "";
  try {
    await api("/api/login", {method: "POST", body: JSON.stringify({
      password: $("#authPass").value,
      remember: $("#authRemember").checked,
    })});
    enterMain();
  } catch (e) {
    $("#authErr").textContent = e.message.replace("password required", "Enter a password");
  } finally {
    btn.disabled = false; btn.textContent = "Sign in →";
  }
});
$("#eyeBtn").onclick = () => {
  const p = $("#authPass");
  p.type = p.type === "password" ? "text" : "password";
};

function enterMain() {
  $("#authView").hidden = true;
  $("#mainView").hidden = false;
  bindNav();
  refreshAll();
  startTimers();
}

/* ---------------------------------------------------------------- nav */
let navBound = false;
function bindNav() {
  if (navBound) return;
  navBound = true;
  $$("[data-tabs] .navlink").forEach(el => el.onclick = () => go(el.dataset.tab));
  document.addEventListener("click", e => {          // inline links jump to tabs
    const l = e.target.closest("a.link[data-tab]");
    if (l) go(l.dataset.tab);
  });
}
function go(tab) {
  $$("[data-tabs] .navlink").forEach(el => el.classList.toggle("active", el.dataset.tab === tab));
  $$("main [data-view]").forEach(el => el.hidden = el.dataset.view !== tab);
  const titles = {
    dashboard: ["Dashboard", "Live view of your ODU"],
    devices:   ["Devices", "Every device this ODU has seen"],
    websites:  ["Websites", "Per-device browsing activity (DNS log)"],
    vouchers:  ["Vouchers", "Codes, shared data pool & device gating"],
    speed:     ["Speed test", "Real throughput over your MTN line"],
    alerts:    ["Alerts", "New devices, blocks and router events"],
    settings:  ["Settings", "Counter resets, DNS log, raw data"],
  };
  const t = titles[tab] || ["ODU Tracker", ""];
  $("#pageTitle").textContent = t[0];
  $("#pageSub").textContent = t[1];
  loadTab(tab);
}
function loadTab(tab) {
  if (tab === "devices") pollDevices();
  if (tab === "websites") { pollSites(); pollDnscfg(); }
  if (tab === "vouchers") pollVouchers();
  if (tab === "speed") { pollSpeed(); pollAlertBadge(); }
  if (tab === "alerts") pollAlerts();
  if (tab === "settings") { pollDnscfg(); renderResetNote(); }
}

/* ---------------------------------------------------------------- timers */
function startTimers() {
  const every = (ms, fn, key) => {
    if (S.tmrs[key]) clearInterval(S.tmrs[key]);
    S.tmrs[key] = setInterval(() => { if (!document.hidden) fn(); }, ms);
  };
  every(10000, pollStatus, "status");
  every(20000, pollUsage, "usage");
  every(30000, pollDevices, "devices");
  every(30000, pollSites, "sites");
  every(30000, () => { if (visibleTabIs("vouchers")) pollVouchers(); }, "vouchers");
  every(20000, pollAlertBadge, "alerts");
  every(5000, () => { if (visibleTabIs("dashboard")) pollRates(); }, "rates");
  every(5000, () => { if (speedRunning()) pollSpeed(true); }, "speed");
}
const visibleTabIs = t => !$$(`main [data-view="${t}"]`)[0].hidden;

/* ---------------------------------------------------------------- status */
async function pollStatus() {
  try {
    const d = await api("/api/status");
    S.lastStatus = d;
    const st = d.flow || {};
    const u = d.usage || {};
    // KPIs
    $("#kpiMonth").textContent = d.rates ? fmtGB(st.total_mb) : "—";
    const limitGB = st.limit_switch === "1" || st.limit_switch === 1
      ? parseFloat(st.limit_size) : null;
    const usedGB = st.total_mb != null ? st.total_mb / 1024 : null;
    if (limitGB > 0 && usedGB != null) {
      const pct = Math.min(100, usedGB / limitGB * 100);
      $("#kpiMonthMeter > div").style.width = pct.toFixed(1) + "%";
      $("#kpiMonthSub").textContent = `${pct.toFixed(0)}% of ${parseFloat(limitGB).toFixed(0)} GB plan · down ${fmtGB(st.down_mb)} / up ${fmtGB(st.up_mb)}`;
    } else {
      $("#kpiMonthMeter > div").style.width = "0%";
      $("#kpiMonthSub").textContent = `down ${fmtGB(st.down_mb)} · up ${fmtGB(st.up_mb)} (meter off)`;
    }
    $("#kpiReset").textContent = d.since_reset ? fmtG(d.since_reset.gb) : "—";
    $("#kpiResetSub").textContent = d.since_reset ? "since " + fmtDT(d.since_reset.ts) : "no reset yet";
    $("#kpiToday").textContent = d.day_usage != null ? fmtG(d.day_usage) : "—";
    $("#kpiRateNow").textContent = fmtMbp(d.rates?.current) + " Mbps";
    $("#kpiRateSub").textContent = `1-h avg ${fmtMbp(d.rates?.avg_hour)} · 10-m ${fmtMbp(d.rates?.avg_10m)}`;
    // chips
    const lvl = d.sys?.signal_lvl, op = d.sys?.network_operator;
    $("#chipSignal").textContent = lvl != null ? `▮ ${lvl}/5` : "—";
    $("#chipNet").textContent = `${d.sys?.network_type_str || "—"} · ${op || ""}`;
    const ok = d.poll?.logged_in && !d.poll?.error;
    $("#chipStatus").innerHTML = ok
      ? '<span class="chip-ic ok">✓</span><span>Live</span>'
      : `<span class="chip-ic warn">•</span><span>Link down</span>`;
    $("#chipMonth").textContent = fmtGB(st.total_mb);
    // system kv
    const net = d.network || {};
    const rows = [
      ["Board", `${d.sys?.board_type || "—"}`],
      ["IDU", d.sys?.idu_dev_type || "—"],
      ["Firmware", d.wan?.fw || "—"],
      ["WAN IP", d.wan?.wan_ip || "—"],
      ["Uptime", (d.wan?.uptime != null && d.wan.uptime !== "") ? Math.round(d.wan.uptime / 3600) + " h" : "—"],
      ["SSID", `${d.wan?.ssid24 || ""}${d.wan?.ssid58 ? " · " + d.wan.ssid58 : ""}`],
      ["RSRP", d.signal?.rsrp != null ? d.signal.rsrp + " dBm" : (d.signal?.rsrp_5g != null ? d.signal.rsrp_5g + " dBm (5G)" : "—")],
      ["Battery", d.battery && d.battery.battery_capacity ? d.battery.battery_capacity + "%" : "—"],
    ].concat(net.temp != null ? [["Temp", net.temp + " °C"]] : []);
    $("#sysKv").innerHTML = rows.map(r => `<span>${esc(r[0])}</span><b>${esc(r[1])}</b>`).join("");
    // conn dot
    $("#connDot").className = "dot " + (ok ? "on" : "off");
    $("#connText").textContent = ok ? "router connected" : "router offline";
  } catch { /* transient */ }
}

/* ---------------------------------------------------------------- usage & rates */
async function pollUsage() {
  try {
    const monthMode = S.usageHours === "month";
    const d = await api(monthMode ? "/api/usage?month=1"
                                  : `/api/usage?hours=${S.usageHours}`);
    const rows = d.series || [];
    const total = rows.reduce((a, r) => a + (r.delta || 0), 0);
    const cfgType = {bar: "bar", line: "line", area: "area", donut: "donut"}[S.usageType];
    if (cfgType === "donut") {
      const st = S.lastStatus?.flow || {};
      const dl = +st.down_mb || 0, ul = +st.up_mb || 0;
      chart($("#usageChart"), {type: "donut", height: 210,
        parts: [
          {label: "Download (month)", value: dl},
          {label: "Upload (month)", value: ul},
        ].filter(p => p.value > 0),
        fmt: v => fmtGB(v), center: monthMode ? "month total" : "month"});
    } else if (monthMode) {
      const labeled = rows.map(r => ({ts: r.ts, v: r.delta,
        label: new Date(r.ts * 1000).toLocaleDateString([], {day: "numeric", month: "short"})}));
      $("#usageNote").textContent =
        `${fmtG(total)} this month${d.first ? ` · recording since ${fmtDT(d.first)}` : ""}` +
        (rows.length ? "" : " — no samples recorded yet");
      chart($("#usageChart"), {type: cfgType, series: labeled, unit: "GB",
        height: 210, color: "#3fd68e", fmt: v => v.toFixed(v >= 10 ? 1 : 2) + " GB"});
    } else {
      $("#usageNote").textContent =
        `${fmtG(total)} used in the last ${S.usageHours} h · sampled every 15 s`;
      chart($("#usageChart"), {type: cfgType, series: bucketize(rows, S.usageHours),
        unit: "GB", height: 210, fmt: v => v.toFixed(v >= 10 ? 1 : 2) + " GB"});
    }
  } catch { /* transient */ }
}
/* aggregate usage deltas into labelled buckets based on window size */
function bucketize(rows, hours) {
  if (!rows.length) return [];
  const per = hours <= 6 ? 900 : hours <= 24 ? 3600 : hours <= 72 ? 10800 : 21600;
  const map = new Map();
  for (const r of rows) {
    const k = Math.floor(r.ts / per) * per;
    map.set(k, (map.get(k) || 0) + (r.delta || 0));
  }
  const out = [];
  for (const [ts, v] of map) {
    const d = new Date(ts * 1000);
    out.push({ts, v,
      label: hours >= 72
        ? d.toLocaleDateString([], {day: "2-digit", month: "short"})
        : d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})});
  }
  return out.sort((a, b) => a.ts - b.ts);
}

async function pollRates() {
  try {
    const d = await api(`/api/rates?minutes=${S.ratesMinutes}`);
    const rows = (d.series || []).map(r => ({ts: r.ts, v: r.mbps || 0, label: fmtT(r.ts)}));
    $("#ratesNote").textContent = rows.length
      ? `peak ${fmtMbp(Math.max(...rows.map(r => r.v)))} Mbps · avg ${fmtMbp(rows.reduce((a, r) => a + r.v, 0) / rows.length)} Mbps`
      : "waiting for throughput samples…";
    chart($("#ratesChart"), {type: S.ratesType, series: rows.slice(), unit: "Mbps",
      height: 200, color: "#6fa7ff", fmt: v => v.toFixed(2)});
  } catch { /* transient */ }
}

/* ---------------------------------------------------------------- devices */
async function pollDevices() {
  try {
    const d = await api("/api/devices");
    S.devices = d.devices || [];
    $("#devOnlineCount").textContent = `${S.devices.filter(x => x.online).length} online / ${S.devices.length} known`;
    // dashboard chips
    const online = S.devices.filter(x => x.online);
    $("#devChips").innerHTML = online.map(x =>
      `<span class="chip"><b style="max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.last_hostname || "device")}</b>${esc(x.last_ip || "")}</span>`).join("")
      || '<span class="empty" style="padding:6px">none right now</span>';
    renderDevTable();
    // leaderboard driven by the sites/rank cache (refreshed by its own timer)
    renderRank();
  } catch { /* transient */ }
}
function renderDevTable() {
  const q = ($("#devSearch").value || "").toLowerCase();
  const rows = S.devices.filter(x =>
    !q || `${x.last_hostname} ${x.last_ip} ${x.mac} ${x.vendor}`.toLowerCase().includes(q));
  $("#devTable tbody").innerHTML = rows.map(x => {
    const lvl = (x.risklevel || "unknown").toLowerCase();
    const badge = x.online ? '<span class="badge green">online</span>'
      : '<span class="badge gray">offline</span>';
    const flags = [];
    if (x.trusted) flags.push('<span class="badge green">trusted</span>');
    if (x.blocked) flags.push('<span class="badge red">blocked</span>');
    if (!x.trusted && x.age_days < 7) flags.push('<span class="badge yellow">new</span>');
    return `<tr>
      <td><b>${esc(x.last_hostname || "unknown")}</b><br><span class="tagline">${esc(x.vendor || (x.mac.startsWith("2E") || x.mac.startsWith("62") ? "randomized MAC" : "unknown vendor"))}</span></td>
      <td class="mono">${esc(x.last_ip || "—")}</td>
      <td class="mono" style="font-size:.72rem">${esc(x.mac)}</td>
      <td>${badge}<div style="margin-top:4px">${flags.join(" ")}</div></td>
      <td><span class="tr-pill tr-${lvl}" title="score ${(+x.riskscore).toFixed(2)}">${lvl}</span></td>
      <td>${fmtDT(x.first_seen)}</td>
      <td class="right">
        <button class="btn xs" data-act="trust" data-mac="${esc(x.mac)}">${x.trusted ? "Untrust" : "Trust"}</button>
        <button class="btn xs ${x.blocked ? "" : "danger"}" data-act="block" data-mac="${esc(x.mac)}">${x.blocked ? "Unblock" : "Block"}</button>
      </td></tr>`;
  }).join("") || '<tr><td colspan="7"><div class="empty">no devices matched</div></td></tr>';
}
$("#devSearch").addEventListener("input", renderDevTable);
document.addEventListener("click", async e => {
  const b = e.target.closest('button[data-act="trust"],button[data-act="block"]');
  if (!b) return;
  const {act, mac} = b.dataset;
  try {
    if (act === "trust") {
      const dev = S.devices.find(x => x.mac === mac);
      await api(`/api/devices/${mac}/trust`, {method: "POST",
        body: JSON.stringify({trusted: !(dev && dev.trusted)})});
      toast("Trust updated");
    } else {
      const dev = S.devices.find(x => x.mac === mac);
      await api(`/api/devices/${mac}/block`, {method: "POST",
        body: JSON.stringify({blocked: !(dev && dev.blocked)})});
      toast(dev && dev.blocked ? "Device unblocked on router" : "Device blocked on router ⛔");
    }
    pollDevices();
  } catch (err) { toast("Failed: " + err.message); }
});

/* ---------------------------------------------------------------- vouchers */
function fillClaimDevices() {
  const sel = $("#vClaimDev");
  if (!sel) return;
  const keep = sel.value;
  const devs = S.devices || [];
  sel.innerHTML = '<option value="">auto (this device)</option>' +
    devs.map(x => `<option value="${esc(x.mac)}">${esc(x.last_hostname || x.mac)} · ${esc(x.last_ip || "")}</option>`).join("");
  if (keep) sel.value = keep;
}

async function pollVouchers() {
  try {
    if (!S.devices) {
      try { const dd = await api("/api/devices"); S.devices = dd.devices || []; } catch { /* transient */ }
    }
    fillClaimDevices();
    const d = await api("/api/vouchers?gate=1");
    renderVoucherPool(d.pool || {});
    renderVoucherGate(d.gate);
    renderVoucherWps(d.wps);
    renderVoucherTable(d.vouchers || []);
    try { renderVoucherUsage(await api("/api/voucher/usage")); } catch { /* transient */ }
    try { renderPortal(await api("/api/dnscfg")); } catch { /* transient */ }
  } catch { /* transient */ }
}

function renderVoucherUsage(u) {
  const tb = $("#vUsageTable tbody");
  if (!tb) return;
  const rows = (u && u.devices) || [];
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="5"><div class="empty">no claimed devices yet</div></td></tr>';
    return;
  }
  const fmtGB = mb => (mb / 1024).toFixed(mb >= 10240 ? 0 : 1) + " GB";
  tb.innerHTML = rows.map(r => {
    const pct = r.quota_mb ? Math.min(100, Math.round(100 * r.used_mb / r.quota_mb)) : 0;
    const badge = r.exhausted
      ? '<span class="badge red">cut off</span>'
      : (pct >= 80 ? `<span class="badge yellow">${pct}%</span>`
                   : `<span class="badge green">${pct}%</span>`);
    const dev = (S.devices || []).find(x => x.mac.toUpperCase() === r.mac.toUpperCase());
    const name = dev ? (dev.hostname || dev.last_hostname || r.mac) : r.mac;
    return `<tr><td class="mono">${esc(name)}</td>` +
      `<td class="right">${fmtGB(r.used_mb)}</td>` +
      `<td class="right">${fmtGB(r.quota_mb)}</td>` +
      `<td class="right">${fmtGB(r.remaining_mb)}</td>` +
      `<td>${badge}</td></tr>`;
  }).join("");
}

function renderPortal(p) {
  const el = $("#vPortal");
  if (!p) { el.innerHTML = '<span class="tagline">not loaded</span>'; return; }
  const parts = [];
  parts.push(p.enforced
    ? '<span class="badge green">ARMED</span>'
    : '<span class="badge yellow">disarmed</span>');
  const dns = p.bindError
    ? `<b>DNS :${p.port}</b> <span class="badge red">${esc(p.bindError)}</span>`
    : (p.enabled
        ? `<b>DNS :${p.port}</b> <span class="badge green">up</span>`
        : `<b>DNS :${p.port}</b> <span class="tagline">off</span>`);
  parts.push(dns);
  const pt = p.portal || {};
  parts.push(pt.error
    ? `<b>portal :${pt.port}</b> <span class="badge red">${esc(pt.error)}</span>`
    : (pt.running
        ? `<b>portal :${pt.port}</b> <span class="badge green">up</span>`
        : '<span class="tagline">portal down</span>'));
  if (p.myIp) parts.push(`<span class="tagline">this PC ${esc(p.myIp)}</span>`);
  if (p.hijacked) parts.push(`<span class="tagline">${p.hijacked} hijacked</span>`);
  if (!p.dns_pointed_at_app)
    parts.push('<span class="badge yellow">router DNS not pointing here</span>');
  el.innerHTML = parts.join(" &nbsp; ");
}

function renderVoucherWps(w) {
  const el = $("#vWps");
  if (!w) { el.innerHTML = '<span class="tagline">not loaded</span>'; return; }
  const parts = ["2.4g", "5g"].map(b => {
    const s = w[b] || {};
    if (s.error) return `<b>${b}</b> error: ${esc(s.error)}`;
    return `<b>${b}</b> ` + (s.on
      ? '<span class="badge yellow">ON — gate not enforced</span>'
      : '<span class="badge green">off</span>');
  });
  el.innerHTML = parts.join(" &nbsp; ");
}

async function voucherAction(path, body, label) {
  const out = $("#vEnfOut");
  out.hidden = false;
  out.textContent = label + ": sending…";
  try {
    const d = await api(path, body ? {method: "POST", body: JSON.stringify(body)} : {method: "POST"});
    out.textContent = label + ": " + JSON.stringify(d);
    toast(label + " OK");
  } catch (err) {
    out.textContent = label + " FAILED: " + err.message;
    toast(label + " failed: " + err.message);
  }
  pollVouchers();
}

$("#btnVWps").addEventListener("click", () =>
  voucherAction("/api/voucher/wps", null, "WPS off"));
// Two-step arm/confirm: JS confirm() is unreliable on Android WebView
// (may return false/undefined without showing anything), so the button
// itself arms first and executes on a second click within 4 s.
function armConfirm(btn, armLabel, execLabel, fn) {
  if (btn.dataset.armed === "1") {
    btn.dataset.armed = "";
    btn.textContent = execLabel;
    btn.classList.remove("danger");
    fn();
    return;
  }
  btn.dataset.armed = "1";
  btn.textContent = armLabel;
  btn.classList.add("danger");
  setTimeout(() => {
    if (btn.dataset.armed === "1") {
      btn.dataset.armed = "";
      btn.textContent = execLabel;
      btn.classList.remove("danger");
    }
  }, 4000);
}

$("#btnVCapOn").addEventListener("click", () =>
  armConfirm($("#btnVCapOn"), "Sure? cuts everyone at 0", "Enable data cap", () =>
    voucherAction("/api/voucher/pool", {enable: true}, "Enable cap")));
$("#btnVCapOff").addEventListener("click", () =>
  voucherAction("/api/voucher/pool", {enable: false}, "Disable cap"));

async function portalAction(path, body, label) {
  const out = $("#vPortalOut");
  out.hidden = false;
  out.textContent = label + ": sending…";
  try {
    const d = await api(path, body ? {method: "POST", body: JSON.stringify(body)} : {method: "POST"});
    out.textContent = label + ": " + JSON.stringify(d);
    toast(label + " OK");
  } catch (err) {
    out.textContent = label + " FAILED: " + err.message;
    toast(label + " failed: " + err.message);
  }
  pollVouchers();
}
$("#btnPortalOn").addEventListener("click", () =>
  armConfirm($("#btnPortalOn"), "Sure? unclaimed devices lose internet",
    "Arm portal", () =>
      portalAction("/api/portal/enforce", {enabled: true}, "Arm portal")));
$("#btnPortalOff").addEventListener("click", () =>
  portalAction("/api/portal/enforce", {enabled: false}, "Disarm portal"));

function renderVoucherPool(p) {
  $("#vpPurchased").textContent = fmtGB(p.purchased_mb);
  $("#vpUsed").textContent = fmtGB(p.used_total_mb);
  $("#vpRemaining").textContent = p.remaining_mb == null ? "—" : fmtGB(p.remaining_mb);
  $("#vpRemainingSub").textContent = p.planned_limit_mb == null
    ? "redeem a code to anchor the pool"
    : `cap ${fmtGB(p.planned_limit_mb)} = baseline ${fmtGB(p.baseline_mb)} + purchased ${fmtGB(p.purchased_mb)} + grace ${fmtGB(p.grace_mb)}`;
  const r = p.router || {};
  const cap = $("#vpCap"), capSub = $("#vpCapSub");
  if (r.error) { cap.textContent = "—"; capSub.textContent = "router: " + r.error; }
  else if (r.limitSwitch == null) { cap.textContent = "—"; capSub.textContent = "router unreachable"; }
  else if (r.limitSwitch === "1") {
    cap.textContent = r.limitSize;
    capSub.textContent = `ON · unit ${r.flow_limit_unit || "?"} · billing day ${r.startDate || "?"}`;
  } else {
    cap.textContent = "off";
    capSub.textContent = `limitSwitch=0 · billing day ${r.startDate || "?"}`;
  }
}

function renderVoucherGate(g) {
  const el = $("#vGate");
  if (!g) { el.innerHTML = '<div class="empty">gate state not loaded</div>'; return; }
  el.innerHTML = ["2.4g", "5g"].map(b => {
    const s = g[b] || {};
    if (s.error) return `<div style="margin:6px 0"><b>${b}</b> — error: ${esc(s.error)}</div>`;
    const mode = s.mode || "close";
    const badge = mode === "close" ? '<span class="badge gray">filter off</span>'
      : mode === "deny" ? '<span class="badge red">deny list</span>'
      : '<span class="badge green">allow list</span>';
    const macs = (s.macs || []).map(m => `<span class="mono" style="font-size:.72rem">${esc(m)}</span>`).join("  ")
      || '<span class="tagline">empty</span>';
    return `<div style="display:flex;gap:10px;align-items:center;margin:6px 0;flex-wrap:wrap"><b style="width:44px">${b}</b>${badge} ${macs}</div>`;
  }).join("");
}

function renderVoucherTable(list) {
  $("#vCountTag").textContent = `${list.length} code(s)`;
  $("#vTable tbody").innerHTML = list.map(v => {
    const badge = v.status === "claimed" ? '<span class="badge green">claimed</span>'
      : v.status === "revoked" ? '<span class="badge gray">revoked</span>'
      : '<span class="badge yellow">active</span>';
    const dev = v.mac ? `<span class="mono" style="font-size:.72rem">${esc(v.mac)}</span>` : "—";
    const act = v.status === "active"
      ? `<button class="btn xs danger" data-vrevoke="${esc(v.code)}">Revoke</button>` : "";
    return `<tr>
      <td class="mono"><b>${esc(v.code)}</b></td>
      <td class="right">${fmtGB(v.quota_mb)}</td>
      <td>${badge}</td>
      <td>${dev}</td>
      <td>${v.claimed_at ? fmtDT(v.claimed_at) : "—"}</td>
      <td>${esc(v.note || "")}</td>
      <td class="right">${act}</td></tr>`;
  }).join("") || '<tr><td colspan="7"><div class="empty">no codes yet — generate some above</div></td></tr>';
}

$("#btnVGen").addEventListener("click", async () => {
  try {
    const d = await api("/api/vouchers", {method: "POST", body: JSON.stringify({
      count: +$("#vCount").value || 1,
      quota_gb: +$("#vQuota").value || 20,
      note: $("#vNote").value})});
    const out = $("#vCodesOut");
    out.hidden = false;
    out.innerHTML = `<b>${d.codes.length} code(s) · ${fmtGB(d.quota_mb)} each</b><br>` +
      d.codes.map(c => `<span class="mono">${esc(c)}</span>`).join(" · ") +
      ` <button class="btn xs" id="btnVCopy">Copy</button>`;
    const copyBtn = $("#btnVCopy");
    if (copyBtn) copyBtn.onclick = () => {
      navigator.clipboard.writeText(d.codes.join("\n")).then(() => toast("Codes copied"));
    };
    toast(`Generated ${d.codes.length} code(s)`);
    pollVouchers();
  } catch (err) { toast("Failed: " + err.message); }
});

$("#btnVClaim").addEventListener("click", async () => {
  const code = $("#vClaimCode").value.trim();
  if (!code) { toast("Enter a code"); return; }
  try {
    const d = await api("/api/voucher/claim", {method: "POST",
      body: JSON.stringify({code, mac: $("#vClaimDev").value || null})});
    toast(`Redeemed → ${fmtGB(d.claim.quota_mb)} pooled for ${d.claim.mac}`);
    $("#vClaimCode").value = "";
    pollVouchers();
  } catch (err) { toast("Failed: " + err.message); }
});

document.addEventListener("click", async e => {
  const b = e.target.closest("button[data-vrevoke]");
  if (!b) return;
  try {
    await api(`/api/vouchers/${b.dataset.vrevoke}/revoke`, {method: "POST"});
    toast("Code revoked");
    pollVouchers();
  } catch (err) { toast("Failed: " + err.message); }
});

/* rank cache fed from /api/sites.clients (device names joined server-side) */
let RANK = [];
async function pollSites() {
  try {
    const d = await api(`/api/sites?hours=${$("#sitesHours").value}`);
    RANK = d.ranking || [];
    if (visibleTabIs("devices")) renderRank();
    renderSites(d);
  } catch { /* transient */ }
}
function renderRank() {
  const rows = RANK.slice(0, 6).map(r => ({
    name: r.hostname || "unknown",
    sub: r.client_ip,
    value: r.hits,
  }));
  leaderboard($("#devRankList"), rows, v => v + " lookups");
}
function renderSites(d) {
  const c = d.clients || [], dom = d.domains || [];
  $("#siteClientTable tbody").innerHTML = c.map(r =>
    `<tr><td><b>${esc(r.hostname || "unknown device")}</b><br><span class="tagline mono">${esc(r.client_ip)}</span></td>
     <td class="right"><b>${r.hits}</b></td><td class="right">${r.domains}</td></tr>`)
    .join("") || '<tr><td colspan="3"><div class="empty">No lookups logged yet in this window</div></td></tr>';
  $("#siteDomTable tbody").innerHTML = dom.map(r =>
    `<tr><td class="mono">${esc(r.domain)}</td><td class="right"><b>${r.hits}</b></td>
     <td class="right">${r.clients}</td><td class="right tagline">${ago(r.last_seen)}</td></tr>`)
    .join("") || '<tr><td colspan="4"><div class="empty">No domains yet — see the notice above ⤴</div></td></tr>';
  const rules = d.url_rules || [];
  $("#siteRules").className = rules.length ? "" : "empty";
  $("#siteRules").innerHTML = rules.length
    ? rules.map(r => `<span class="badge ${r.enabled ? "red" : "gray"}" style="margin:2px">${esc(r.url)}${r.enabled ? "" : " (off)"}</span>`).join(" ")
    : "router URL filter has no entries";
}

/* ---------------------------------------------------------------- dns cfg */
async function pollDnscfg() {
  try {
    const d = await api("/api/dnscfg");
    $("#dnsEnable").checked = !!d.enabled;
    $("#dnsPort").textContent = d.port || 5353;
    const pointed = !!d.dns_pointed_at_app;
    const idx = pointed ? "✓" : "•";
    $("#dnsStatus").innerHTML =
      `This PC on the LAN: <b class="mono">${esc(d.myIp || "?")}</b><br>` +
      `Router DHCP DNS: <b class="mono">${esc(d.router_dns_main || "?")}</b>` +
      (d.router_dns_vice ? ` (backup ${esc(d.router_dns_vice)})` : "") + "<br>" +
      (pointed
        ? `<span class="badge green">${idx} pointed at this app</span> — devices pick it up as DHCP leases renew (toggle Wi-Fi on a device for an instant refresh).`
        : `<span class="badge yellow">${idx} router DNS is ${d.router_dns_main || "not set"}</span> — per-device site tracking needs the one-click connect.`);
    $("#btnDnsRestore").disabled = !pointed && !d.router_dns_main;
  } catch { /* transient */ }
}
$("#dnsEnable").addEventListener("change", async e => {
  try {
    await api("/api/dnscfg", {method: "POST", body: JSON.stringify({enabled: e.target.checked})});
    toast(e.target.checked ? "DNS logging running" : "DNS logging stopped");
  } catch (err) { toast("Failed: " + err.message); }
  pollDnscfg();
});
async function dnsRouter(mode) {
  const btn = mode === "app" ? $("#btnDnsPoint") : $("#btnDnsRestore");
  btn.disabled = true;
  btn.classList.add("primary");
  try {
    const d = await api("/api/dnscfg/router", {method: "POST", body: JSON.stringify({mode})});
    if (d.ok) toast(d.message || "Router DNS updated ✓", 5000);
    else toast("Router refused: " + JSON.stringify(d.router_resp?.message || d.error || ""), 6000);
  } catch (err) { toast("Failed: " + err.message, 6000); }
  btn.classList.remove("primary");
  btn.disabled = false;
  pollDnscfg();
}
$("#btnDnsPoint").onclick = () => dnsRouter("app");
$("#btnDnsRestore").onclick = () => dnsRouter("default");

/* ---------------------------------------------------------------- speed */
const speedRunning = () => !!(S.speedState && S.speedState.running);
let SPEED = {};
async function pollSpeed(silent) {
  try {
    const d = await api("/api/speedtest");
    S.speedState = d; SPEED = d;
    const st = d.running ? d.phase : (d.last ? "done" : "idle");
    $("#stStatus").textContent = d.running ? `⏳ ${d.phase} — uses your data while running` :
      d.last ? `last test ${fmtDT(d.last.ts)}` : "idle";
    $("#stBar").style.width = d.running ? "50%" : d.last ? "100%" : "0%";
    if (d.last && !d.running) {
      $("#stDownVal").textContent = fmtMbp(d.last.down_mbps);
      $("#stUpVal").textContent = d.last.up_mbps ? fmtMbp(d.last.up_mbps) : "skipped";
      $("#stPing").textContent = d.last.ping_ms != null ? Math.round(d.last.ping_ms) + " ms" : "—";
    }
    $("#stAvg").textContent = d.avg_down ? fmtMbp(d.avg_down) + " Mbps" : "—";
    $("#stAvgUp").textContent = d.avg_up ? "avg up " + fmtMbp(d.avg_up) + " Mbps" : `${d.count || 0} tests stored`;
    $("#stHistory tbody").innerHTML = (d.history || []).map(r =>
      `<tr><td>${fmtDT(r.ts)}</td><td class="right"><b>${fmtMbp(r.down_mbps)}</b></td>
       <td class="right">${r.up_mbps ? fmtMbp(r.up_mbps) : "—"}</td>
       <td class="right">${r.ping_ms != null ? Math.round(r.ping_ms) : "—"}</td>
       <td class="right tagline">${r.bytes_used ? fmtGB(r.bytes_used / 1024) : "—"}</td></tr>`)
      .join("") || '<tr><td colspan="5"><div class="empty">no tests yet — press ▶ Run test</div></td></tr>';
  } catch (e) { if (!silent) console.warn(e); }
}
$("#btnSpeed").onclick = async () => {
  const btn = $("#btnSpeed");
  btn.disabled = true; btn.textContent = "Starting…";
  try {
    await api("/api/speedtest/run", {method: "POST", body: JSON.stringify({
      size_mb: +$("#stDown").value, up_mb: +$("#stUp").value})});
    toast("Speed test running — data is being consumed");
    pollSpeed(true);
  } catch (err) { toast("Failed: " + err.message); }
  btn.disabled = false; btn.textContent = "▶ Run test";
};

/* ---------------------------------------------------------------- alerts */
async function pollAlerts() {
  try {
    const d = await api("/api/alerts");
    const a = d.alerts || [];
    $("#alertsList").innerHTML = a.map(x =>
      `<div style="display:flex;gap:12px;align-items:flex-start;padding:13px 4px;border-bottom:1px solid var(--line);
        ${x.read ? "" : "background:var(--panel2)"}">
        <span class="badge ${x.level === "warning" ? "yellow" : x.level === "danger" ? "red" : "gray"}">${esc(x.level || "info")}</span>
        <div style="flex:1"><b style="font-size:.9rem">${esc(x.title)}</b>
          <div class="tagline" style="margin-top:2px">${esc(x.detail || "")}</div></div>
        <span class="tagline" style="white-space:nowrap">${ago(x.ts)}</span></div>`)
      .join("") || '<div class="empty">no alerts — all quiet 🎉</div>';
    pollAlertBadge();
  } catch { /* transient */ }
}
async function pollAlertBadge() {
  try {
    const d = await api("/api/alerts");
    const unread = (d.alerts || []).filter(x => !x.read).length;
    const b = $("#navAlertBadge");
    b.hidden = !unread;
    b.textContent = unread > 99 ? "99+" : unread;
  } catch { /* transient */ }
}
$("#btnAlertsRead").onclick = async () => {
  await api("/api/alerts/read", {method: "POST"});
  pollAlerts();
  pollAlertBadge();
};

/* ---------------------------------------------------------------- settings */
function renderResetNote() {
  const s = S.lastStatus;
  $("#resetNote").textContent = s?.since_reset
    ? `Last reset ${fmtDT(s.since_reset.ts)} · used since: ${fmtG(s.since_reset.gb)}`
    : "Never reset in this app";
}
$("#resetBtn").onclick = async () => {
  const b = $("#resetBtn");
  if (!confirm("Reset the usage counter so it counts from now?\nThe router's own statistics are cleared when the firmware allows it.")) return;
  b.disabled = true; b.textContent = "Resetting…";
  try {
    const d = await api("/api/usage/reset", {method: "POST"});
    toast(d.firmware?.ok
      ? "✓ Router counters cleared — counting from 0 now"
      : "Reset baseline saved — counting from now", 5000);
    renderResetNote(); pollStatus(); pollUsage();
  } catch (err) { toast("Failed: " + err.message); }
  b.disabled = false; b.textContent = "↺ Reset usage from now";
};
$("#btnChangePw").onclick = async () => {
  const pw = $("#newPw").value;
  if (!pw) { $("#pwErr").textContent = "Type the new router password first"; return; }
  $("#pwErr").textContent = "";
  try {
    await api("/api/changepw", {method: "POST", body: JSON.stringify({password: pw})});
    $("#newPw").value = "";
    toast("Router password updated ✓");
  } catch (err) { $("#pwErr").textContent = err.message; }
};
$$(".eye").forEach(b => b.onclick = () => {
  const inp = b.parentElement.querySelector("input");
  inp.type = inp.type === "password" ? "text" : "password";
});
$("#btnRawLoad").onclick = async () => {
  try {
    const d = await api(`/api/raw?name=${$("#rawName").value}`);
    let body = null;
    try { body = JSON.stringify(JSON.parse(d.body), null, 1).replace(/\{"cmd"/g, "\n{\"cmd\""); }
    catch { body = d.body; }
    $("#rawPre").textContent = body || "(empty)";
  } catch (err) { $("#rawPre").textContent = "load failed: " + err.message; }
};
$("#btnLogout").onclick = async () => {
  try { await api("/api/logout", {method: "POST"}); } catch {}
  location.reload();
};

/* seg control helpers */
function autoType(group, type) {
  const seg = $(`.seg[data-charts="${group}"]`);
  if (!seg) return;
  $$("button", seg).forEach(b => b.classList.toggle("active", b.dataset.ctype === type));
  if (group === "usage") { S.usageType = type; localStorage.setItem("odu.usageType", type); }
  else { S.ratesType = type; localStorage.setItem("odu.ratesType", type); }
}
$$(".seg[data-charts]").forEach(seg => {
  seg.querySelectorAll("button").forEach(b => b.onclick = () => {
    seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    if (seg.dataset.charts === "usage") {
      S.usageType = b.dataset.ctype;
      localStorage.setItem("odu.usageType", S.usageType);
      pollUsage();
    } else {
      S.ratesType = b.dataset.ctype;
      localStorage.setItem("odu.ratesType", S.ratesType);
      pollRates();
    }
  });
});
// restore chosen chart types from storage
$$('[data-charts="usage"] button').forEach(b => b.classList.toggle("active", b.dataset.ctype === S.usageType));
$$('[data-charts="rates"] button').forEach(b => b.classList.toggle("active", b.dataset.ctype === S.ratesType));
$("#usageRange").onchange = e => {
  S.usageHours = e.target.value;
  if (S.usageHours === "month") autoType("usage", "line");   // month reads best as a line
  pollUsage();
};
$("#ratesRange").onchange = e => { S.ratesMinutes = +e.target.value; pollRates(); };
$("#sitesHours").onchange = () => pollSites();

/* ---------------------------------------------------------------- refresh all & boot */
function refreshAll() {
  pollStatus(); pollUsage(); pollRates(); pollDevices(); pollSites(); pollDnscfg();
  pollAlertBadge(); pollSpeed(true); renderResetNote();
}
boot();
