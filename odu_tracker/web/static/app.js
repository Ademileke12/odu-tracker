/* ODU Tracker dashboard logic (vanilla JS, zero dependencies) */
const $ = (id) => document.getElementById(id);
let TAB = "dashboard";
let tick = null;

const TITLES = {
  dashboard: ["Dashboard", "Live view of your MTN ODU"],
  devices: ["Devices", "Everything that has ever joined your network"],
  traffic: ["Traffic & Speed", "Data usage, live throughput and speed tests"],
  websites: ["Websites", "DNS activity and router block lists"],
  alerts: ["Alerts", "Security and activity notifications"],
  settings: ["Settings", "Router connection, proxy and diagnostics"],
};

function fmtTime(ts) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtWhen(ts) {
  if (!ts) return "–";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
function fmtMb(mb) {
  const v = parseFloat(mb);
  if (isNaN(v)) return "–";
  return v >= 1024 ? (v / 1024).toFixed(2) + " GB" : v.toFixed(0) + " MB";
}
function esc(s) {
  return (s != null ? String(s) : "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, ms = 2600) {
  const t = $("toast");
  t.innerHTML = msg; t.style.display = "block";
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.style.display = "none"), ms);
}

/* ------------------------------------------------ tabs */
document.querySelectorAll(".navlink").forEach((el) => {
  el.onclick = () => showTab(el.dataset.tab);
});
function showTab(name) {
  TAB = name;
  document.querySelectorAll(".navlink").forEach(
    (n) => n.classList.toggle("active", n.dataset.tab === name));
  ["dashboard", "devices", "traffic", "websites", "alerts", "settings"].forEach((t) => {
    $("tab-" + t).style.display = t === name ? "" : "none";
  });
  const [t, s2] = TITLES[name] || ["", ""];
  $("pageTitle").textContent = t;
  $("pageSub").textContent = s2;
  history.replaceState(null, "", "#" + name);
  refresh();
}

/* ------------------------------------------------ api + auth */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { showLogin(); throw new Error("auth required"); }
  return r.json();
}
async function doLogin() {
  const pw = $("pw").value;
  if (!pw) { $("loginErr").textContent = "password required"; return; }
  $("loginBtn").textContent = "Connecting…";
  $("loginBtn").disabled = true;
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  }).then((r) => r.json()).catch((e) => ({ error: String(e) }));
  $("loginBtn").textContent = "Connect to router";
  $("loginBtn").disabled = false;
  if (res.error) { $("loginErr").textContent = res.error; return; }
  $("loginErr").textContent = "";
  $("pw").value = "";
  enterMain();
}
function showLogin() {
  $("loginView").style.display = "";
  $("mainView").style.display = "none";
  if (tick) clearInterval(tick);
  bootMeta();
}
async function enterMain() {
  $("loginView").style.display = "none";
  $("mainView").style.display = "";
  if (!tick) tick = setInterval(() => refresh(), 5000);
  const hash = (location.hash || "").replace("#", "");
  if (hash && TITLES[hash]) showTab(hash); else refresh();
}

/* ------------------------------------------------ refresh loop */
async function refresh() {
  if ($("mainView").style.display === "none") return;
  try {
    await Promise.all([refreshMeta(), refreshStatus()]);
    if (TAB === "dashboard") refreshDashboard();
    if (TAB === "devices") refreshDevices();
    if (TAB === "traffic") refreshTraffic();
    if (TAB === "websites") refreshSites();
    if (TAB === "alerts") refreshAlerts(true);
    if (TAB === "settings") refreshSettings();
  } catch (e) { /* handled by api() */ }
}

async function bootMeta() {
  const m = await fetch("/api/meta").then((r) => (r.ok ? r.json() : {}));
  $("routerip").textContent = m.router_ip || "192.168.0.1";
  if (m.attempts_left != null) {
    $("attemptInfo").textContent = m.attempts_left <= 2
      ? `⚠ only ${m.attempts_left} wrong attempt(s) left — enter the exact password`
      : `router accepts ${m.attempts_left} more attempts in this window`;
  }
}

async function refreshMeta() {
  const m = await api("/api/meta");
  $("cfgIp").textContent = m.router_ip;
  const dot = $("pollDot"), txt = $("pollText");
  if (m.poll_running && m.router_logged_in) {
    dot.className = "dot on"; txt.textContent = "LIVE · " + m.router_ip;
  } else if (m.poll_running) {
    dot.className = "dot warn"; txt.textContent = "signing in…";
  } else {
    dot.className = "dot off"; txt.textContent = "polling stopped";
  }
  if (m.attempts_left != null && m.attempts_left <= 2) {
    txt.textContent += ` · ⚠ ${m.attempts_left} attempts left`;
  }
}

/* ------------------------------------------------ status refresh */
let LAST_STATUS = null;
async function refreshStatus() {
  LAST_STATUS = await api("/api/status");
  const s = LAST_STATUS;

  /* month usage */
  const f = s.flow || {};
  const totalGb = f.total_mb != null ? parseFloat(f.total_mb) / 1024 : null;
  $("usageTotal").textContent = totalGb != null ? totalGb.toFixed(2) + " GB" : "–";
  $("headUsage").textContent = totalGb != null ? totalGb.toFixed(1) + " GB" : "–";
  const dn = f.down_mb != null ? parseFloat(f.down_mb) / 1024 : null;
  const up = f.up_mb != null ? parseFloat(f.up_mb) / 1024 : null;
  const quota = f.limit_switch === "1" && f.limit_size
    ? `plan ${(f.limit_size)} GB · **meter on**` : "no quota limit set";
  $("usageSub").innerHTML =
    (dn != null ? `↓ ${dn.toFixed(2)} GB` : "") +
    (up != null ? ` · ↑ ${up.toFixed(2)} GB` : "") + ` · ${quota}`.replace("**meter on**", `<b class="mono">meter on</b>`);
  if (f.limit_switch === "1" && f.limit_size && totalGb != null) {
    $("usageMeter").style.width =
      Math.min(100, (totalGb / parseFloat(f.limit_size)) * 100).toFixed(0) + "%";
  } else if (totalGb != null) {
    $("usageMeter").style.width = Math.min(96, totalGb).toFixed(0) + "%";
  }

  /* since-reset counter */
  const sr = s.since_reset;
  if (sr) {
    $("usageSince").textContent = sr.gb.toFixed(2) + " GB";
    $("sinceSub").textContent = "counting since " + fmtTime(sr.ts) + " — router counter untouched";
  } else {
    const today = s.day_usage;
    $("usageSince").textContent = today != null ? today.toFixed(2) + " GB" : "–";
    $("sinceSub").textContent = "today (tap reset to start a fresh counter)";
  }

  /* rates */
  const r = s.rates || {};
  $("rateNow").textContent = r.current != null ? r.current.toFixed(1) + " Mbps" : "idle";
  $("rateAvg").textContent =
    `avg 10 min: ${r.avg_10m != null ? r.avg_10m + " Mbps" : "–"} ` +
    `· avg 1 h: ${r.avg_hour != null ? r.avg_hour + " Mbps" : "–"}`;
  $("headRate").textContent = r.avg_hour != null ? r.avg_hour + " Mbps" : "–";

  /* signal */
  const sys = s.sys || {};
  const lvl = Math.max(1, +(sys.signal_lvl || 1));
  $("signal").textContent = "▮".repeat(lvl) + "▯".repeat(5 - lvl);
  $("netType").textContent =
    [sys.network_type_str, sys.network_operator,
     `RSRP ${s.signal.rsrp ?? "?"} dBm`].filter(Boolean).join(" · ");

  /* health */
  const rt = s.wan || {};
  $("runtimeKv").innerHTML = [
    ["model", `${sys.board_type || "?"} ${sys.idu_dev_type ? "· " + esc(sys.idu_dev_type) : ""}`],
    ["firmware", rt.fw || "–"],
    ["serial", rt.device_sn || "–"],
    ["cpu load", esc((rt.cpuload || "–").split(",").slice(0, 3).join("  "))],
  ].map((kv) => `<span>${kv[0]}</span><b>${kv[1]}</b>`).join("");
  $("odUptime").textContent = rt.uptime ? "up " + fmtUptime(rt.uptime) : "";
  $("wanKv").innerHTML = [
    ["WAN IP", `<span class="mono">${esc(rt.wan_ip || "–")}</span>`],
    ["Wi-Fi 2.4G", `<span class="mono">${esc(rt.ssid24 || "–")}</span>`],
    ["Wi-Fi 5G", `<span class="mono">${esc(rt.ssid58 || "–")}</span>`],
    ["LAN", `<span class="mono">${esc(rt.lan_ip || "–")}</span>`],
  ].map((kv) => `<span>${kv[0]}</span><b>${kv[1]}</b>`).join("");
}
function fmtUptime(sec) {
  sec = +sec || 0;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
        m = Math.floor((sec % 3600) / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}

/* ------------------------------------------------ usage counter reset */
async function resetUsage() {
  if (!confirm("Start counting data usage from now?\n(The router's own monthly counter is not touched.)")) return;
  const r = await api("/api/usage/reset", { method: "POST", body: "{}" });
  if (r.ok) toast(`<b>Counter started from now</b><br><span class="tagline">baseline set at ${fmtTime(r.ts)}</span>`);
  refreshStatus();
}

/* ------------------------------------------------ dashboard */
async function refreshDashboard() {
  const d = await api("/api/devices");
  $("devCount").textContent = d.devices.filter((x) => x.online).length + "/" + d.count;
  const online = d.devices.filter((x) => x.online);
  $("onlineCount").textContent = `${online.length} online · ${d.count} known`;
  $("noOnline").style.display = online.length ? "none" : "";
  $("onlineCards").innerHTML = online.map((x) => `
    <div class="dev-chip">
      <div class="row"><div class="nm">${esc(x.last_hostname || "(unnamed)")}</div></div>
      <div class="muted">${esc(x.last_ip || "")} · ${esc(x.mac)}</div>
      <div class="row">${riskPill(x.risklevel)}${trustTag(x.trusted, x.blocked)}</div>
    </div>`).join("");
  const ev = await api("/api/events?limit=10");
  $("recentEvents").innerHTML = ev.events.length ? ev.events.slice().reverse().map((e) => `
    <tr><td style="white-space:nowrap">${fmtTime(e.ts)}</td>
    <td><b>${esc(eventLabel(e.kind))}</b></td>
    <td class="right" style="color:var(--muted)">${esc(e.detail || "")}</td></tr>`).join("")
    : `<tr><td colspan="3"><div class="empty">no activity yet</div></td></tr>`;
}
function eventLabel(kind) {
  return { new_device: "New device", leave: "Device left", join: "Joined",
           ip_change: "IP changed", block_change: "Block changed",
           trust_change: "Trust changed" }[kind] || kind;
}
function trustTag(trusted, blocked) {
  if (blocked) return `<span class="badge red">blocked</span>`;
  if (trusted) return `<span class="badge green">trusted</span>`;
  return "";
}

/* ------------------------------------------------ devices */
function riskPill(level) {
  const cls = { high: "tr-high", medium: "tr-medium", low: "tr-low", unknown: "tr-unknown" }[level || "unknown"];
  return `<span class="tr-pill ${cls}">${esc(level || "unknown")} risk</span>`;
}
let DEVICES = null;
async function refreshDevices() {
  const d = await api("/api/devices");
  DEVICES = d;
  const on = d.devices.filter((x) => x.online);
  const flagged = d.devices.filter((x) => ["medium", "high"].includes(x.risklevel)).length;
  const blocked = d.devices.filter((x) => x.blocked).length;
  $("dvTotal").textContent = d.count;
  $("dvOnline").textContent = on.length;
  $("dvFlagged").textContent = flagged;
  $("dvBlocked").textContent = blocked;
  renderDevices();
  if (!$("devFilter").dataset.bound) {
    $("devFilter").dataset.bound = "1";
    $("devFilter").oninput = renderDevices;
  }
}
function renderDevices() {
  const filter = ($("devFilter").value || "").toLowerCase();
  const rows = (DEVICES.devices || []).filter((x) => {
    if (!filter) return true;
    return [x.last_hostname, x.last_ip, x.mac, x.vendor].join(" ").toLowerCase().includes(filter);
  });
  $("devTbody").innerHTML = rows.length ? rows.map((x) => `
    <tr>
      <td><span class="badge ${x.online ? "green" : "gray"}" title="${x.online ? "online" : "offline"}">${x.online ? "up" : "off"}</span></td>
      <td><b>${esc(x.last_hostname || "(unnamed)")}</b></td>
      <td class="mono">${esc(x.last_ip || "–")}</td>
      <td class="mono" style="font-size:.76rem">${esc(x.mac)}</td>
      <td>${esc(x.vendor || "unlisted")}</td>
      <td>${esc(x.last_iface || "–")}</td>
      <td style="color:var(--muted)">${fmtWhen(x.first_seen)}</td>
      <td style="color:var(--muted)">${fmtWhen(x.last_seen)}</td>
      <td>${riskPill(x.risklevel)}</td>
      <td class="right" style="white-space:nowrap">
        <button class="btn xs ${x.blocked ? "" : "danger"}" onclick="toggleBlock('${x.mac}', ${!x.blocked})"
          title="Push/unpush MAC filter on the router">${x.blocked ? "unblock" : "block"}</button>
        <button class="btn xs" onclick="toggleTrust('${x.mac}', ${!x.trusted})"
          title="Mark device as household-trusted">${x.trusted ? "untrust" : "trust"}</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="10"><div class="empty">no devices match the filter</div></td></tr>`;
}
async function toggleTrust(mac, v) {
  await api(`/api/devices/${mac}/trust`, { method: "POST", body: JSON.stringify({ trusted: v }) });
  toast(v ? "Device marked trusted" : "Device untrusted");
  refreshDevices();
}
async function toggleBlock(mac, v) {
  if (v && !confirm(`Block ${mac} on the router via MAC filter?`)) return;
  const r = await api(`/api/devices/${mac}/block`, { method: "POST", body: JSON.stringify({ blocked: v }) });
  if (r.router && r.router.ok === false) toast("Router refused: " + esc(r.router.error || "unknown"), 3500);
  else toast(v ? "<b>Device blocked</b> — MAC filter applied" : "Device unblocked");
  refreshDevices();
}

/* ------------------------------------------------ traffic + speed test */
async function refreshTraffic() {
  const s = LAST_STATUS || {};
  const f = s.flow || {};

  /* hourly buckets */
  const u = await api("/api/usage?hours=24");
  const buckets = new Array(24).fill(0);
  const nowH = Date.now() / 1000;
  u.series.forEach((x) => {
    const h = Math.floor((nowH - x.ts) / 3600);
    if (h >= 0 && h < 24) buckets[23 - h] += Math.max(0, x.delta || 0);
  });
  const unitGB = u.series[0] ? true : false;
  const maxB = Math.max(...buckets, 1e-9);
  $("usageBars").innerHTML = buckets.map((v, i) => {
    const pct = Math.min(100, v > 0 ? 6 + (v / maxB) * 94 : 1.5);
    return `<div class="bar" style="height:${pct}%" title="${v.toFixed(3)} GB — ${23 - i}h ago"></div>`;
  }).join("");
  $("usageHeadRight").textContent = totalFromBucket(buckets) + " GB in the last 24 h";

  $("usageKv").innerHTML = [
    ["month total", f.total_mb != null ? (f.total_mb / 1024).toFixed(2) + " GB" : "–"],
    ["month ↓", f.down_mb != null ? (f.down_mb / 1024).toFixed(2) + " GB" : "–"],
    ["month ↑", f.up_mb != null ? (f.up_mb / 1024).toFixed(2) + " GB" : "–"],
    ["quota", f.limit_switch === "1" ? (f.limit_size || "?") + " GB <span class='tagline'>(meter on)</span>" : "unlimited (meter off)"],
    ["usage since reset", s.since_reset ? s.since_reset.gb.toFixed(2) + " GB" : "not started"],
  ].map((kv) => `<span>${kv[0]}</span><b>${kv[1]}</b>`).join("");

  /* live rate bars */
  const r = s.rates || {};
  $("ratesKv").innerHTML = [
    ["current", r.current != null ? r.current + " Mbps" : "idle — no traffic"],
    ["avg 10 min", r.avg_10m != null ? r.avg_10m + " Mbps" : "–"],
    ["avg 1 hour", r.avg_hour != null ? r.avg_hour + " Mbps" : "–"],
  ].map((kv) => `<span>${kv[0]}</span><b>${esc(kv[1])}</b>`).join("");
  const rr = await api("/api/rates?minutes=30");
  const vals = rr.series.map((x) => x.mbps);
  const rmax = Math.max(...vals, 1e-9);
  $("rateBars").innerHTML = rr.series.length ? rr.series.map((x) => {
    const pct = Math.max(2, (x.mbps / rmax) * 100);
    return `<div class="bar r" style="height:${pct}%" title="${x.mbps} Mbps — ${fmtWhen(x.ts)}"></div>`;
  }).join("") : `<div class="empty" style="width:100%">collecting samples…</div>`;

  await renderSpeedTest();
}
function totalFromBucket(b) { return b.reduce((a, x) => a + x, 0).toFixed(2); }

async function renderSpeedTest() {
  const st = await api("/api/speedtest?limit=12");
  const last = st.last || null;
  if (!st.running) {
    $("stBtn").textContent = "▶  Run test";
    $("stBtn").disabled = false;
    if (last && renderSpeedTest._lastTs !== last.ts) {
      $("stResult").innerHTML = last.down_mbps != null ? [
        ["download", last.down_mbps + " Mbps", "ok"],
        ["upload", last.up_mbps != null ? last.up_mbps + " Mbps" : "–", "info"],
        ["ping", last.ping_ms != null ? (+last.ping_ms).toFixed(0) + " ms" : "–", "warn"],
        ["data used", ((last.bytes_used || 0) / 1048576).toFixed(1) + " MB", "warn"],
      ].map(([lbl, val]) => `<div class="card"><div class="lbl">${lbl}</div><div class="val">${esc(val)}</div></div>`).join("")
        : `<div class="empty" style="grid-column:1/-1">no test yet — pick a size above and run one</div>`;
      renderSpeedTest._lastTs = last.ts;
    } else if (!last) {
      $("stResult").innerHTML = `<div class="empty" style="grid-column:1/-1">no test yet — pick a size above and run one</div>`;
    }
  } else {
    $("stBtn").textContent = "⏳ " + (st.phase || "testing") + "…";
    $("stBtn").disabled = true;
  }
  $("stAvg").textContent = st.avg_down != null
    ? `average of ${st.count}: ↓ ${st.avg_down} Mbps · ↑ ${st.avg_up ?? "–"} Mbps`
    : "no averages yet";
  $("stTbody").innerHTML = (st.history || []).length ? st.history.map((h) => `
    <tr>
      <td style="color:var(--muted);white-space:nowrap">${fmtTime(h.ts)}</td>
      <td class="right"><b style="color:var(--ok)">${h.down_mbps ?? "–"} Mbps</b></td>
      <td class="right">${h.up_mbps ?? "–"} Mbps</td>
      <td class="right">${h.ping_ms != null ? (+h.ping_ms).toFixed(0) + " ms" : "–"}</td>
      <td class="right" style="color:var(--faint)">${((h.bytes_used || 0) / 1048576).toFixed(1)} MB</td>
    </tr>`).join("")
    : `<tr><td colspan="5"><div class="empty">history empty</div></td></tr>`;
}

async function runSpeedTest() {
  const size = +$("stSize").value;
  resetStResult();
  $("stBtn").textContent = "⏳ starting…";
  $("stBtn").disabled = true;
  const r = await api("/api/speedtest/run", { method: "POST", body: JSON.stringify({ size_mb: size, up_mb: 1 }) });
  if (!r.ok) { $("stErr").textContent = r.error || "failed to start"; $("stBtn").disabled = false; return; }
}
function resetStResult() {
  $("stErr").textContent = "";
  $("stResult").innerHTML = `<div class="card"><div class="lbl">phase</div><div class="val">starting…</div></div>
  <div class="empty" style="grid-column:2/-1">measuring through the ODU — this uses real data</div>`;
}

/* ------------------------------------------------ websites */
async function refreshSites() {
  const s2 = await api("/api/sites?hours=6");
  $("sitesTbody").innerHTML = s2.domains.length ? s2.domains.slice(0, 150).map((d) => `
    <tr><td><b>${esc(d.domain)}</b></td><td class="right">${d.hits}</td>
    <td class="right">${d.clients}</td><td style="color:var(--muted)">${fmtWhen(d.last_seen)}</td></tr>`).join("")
    : `<tr><td colspan="4"><div class="empty">no dns activity recorded</div></td></tr>`;
  $("noSites").style.display = s2.domains.length ? "none" : "";
  $("urlRulesTbody").innerHTML = (s2.url_rules || []).slice(0, 80).map((r) => `
    <tr><td class="mono" style="font-size:.78rem">${esc(r.url)}</td>
    <td class="right"><span class="badge ${r.enabled ? "green" : "gray"}">${r.enabled ? "on" : "off"}</span></td></tr>`).join("")
    || `<tr><td colspan="2"><div class="empty">no url rules found</div></td></tr>`;
  $("siteClients").innerHTML = (s2.clients || []).map((c) => `
    <tr><td class="mono">${esc(c.client_ip)}</td><td class="right">${c.hits}</td>
    <td class="right">${c.domains}</td></tr>`).join("")
    || `<tr><td colspan="3"><div class="empty">no client data</div></td></tr>`;
}

/* ------------------------------------------------ alerts */
function levelBadge(level) {
  const cls = { danger: "red", warning: "yellow", success: "green", info: "gray" }[level] || "gray";
  return `<span class="badge ${cls}">${esc(level || "info")}</span>`;
}
async function refreshAlerts(updateBadge) {
  const a = await api("/api/alerts");
  if (updateBadge) {
    const unread = (a.alerts || []).filter((x) => !x.read).length;
    const b = $("alertBadge");
    b.style.display = unread ? "" : "none";
    b.textContent = unread;
  }
  $("alertsTbody").innerHTML = a.alerts.length ? a.alerts.map((x) => `
    <tr ${x.read ? 'style="opacity:.5"' : ""}>
      <td style="white-space:nowrap;color:var(--muted)">${fmtTime(x.ts)}</td>
      <td>${levelBadge(x.level)}</td>
      <td><b>${esc(x.title)}</b></td>
      <td style="color:var(--muted)">${esc(x.detail || "")}</td>
    </tr>`).join("")
    : `<tr><td colspan="4"><div class="empty">no alerts</div></td></tr>`;
}
async function markAlertsRead() {
  await api("/api/alerts/read", { method: "POST" });
  refreshAlerts(true);
}

/* ------------------------------------------------ settings */
async function refreshSettings() {
  const m = await api("/api/meta");
  const st = LAST_STATUS || {};
  const sys = st.sys || {};
  $("settingsRouterKv").innerHTML = [
    ["router ip", `<span class="mono">${esc(m.router_ip)}</span>`],
    ["model", `${esc(sys.board_type || "–")}`],
    ["logged in", m.router_logged_in ? "yes" : "no"],
    ["poll interval", esc(stUsageInterval())],
  ].map((kv) => `<span>${kv[0]}</span><b>${kv[1]}</b>`).join("");
  ["fast", "slow", "url_filter"].forEach((n) => {});
  $("rawLinks").innerHTML = ["fast", "slow", "url_filter"].map((n) =>
    `<button class="btn xs" data-name="${n}" onclick="showRaw('${n}')">${n}</button>`).join("");
  if (!$("dnsEnabled").dataset.bound) {
    $("dnsEnabled").dataset.bound = "1";
    $("dnsEnabled").onchange = saveDns;
  }
  const cfg = await api("/api/dnscfg");
  $("dnsEnabled").checked = !!cfg.enabled;
  $("dnsPort").textContent = cfg.port;
  $("dnsUpstream").textContent = cfg.upstream;
  $("dnsHow").textContent = cfg.myIp || "?";
}
function stUsageInterval() { return "15 s fast · 5 min deep"; }

async function saveDns() {
  const on = $("dnsEnabled").checked;
  const r = await api("/api/dnscfg", { method: "POST", body: JSON.stringify({ enabled: on }) });
  toast(r.enabled ? "<b>DNS proxy running</b> on port " + r.port : "DNS proxy stopped");
}
async function showRaw(name) {
  const r = await api(`/api/raw?name=${name}`);
  try {
    $("rawBody").textContent = JSON.stringify(JSON.parse(r.body || "{}"), null, 2).slice(0, 24000);
  } catch (e) {
    $("rawBody").textContent = r.body || "(empty)";
  }
}
async function changePw() {
  const pw = $("newPw").value;
  if (!pw) { toast("type a password first"); return; }
  const r = await fetch("/api/changepw", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }) });
  const j = await r.json();
  if (j.ok) { toast("password updated"); $("newPw").value = ""; }
  else toast("failed: " + esc(j.error || "?"));
}

/* ------------------------------------------------ boot */
(async function boot() {
  await bootMeta();
  showLogin();
  $("pw").focus();
  $("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
  if (!$("resetUsageBtn").dataset.bound) {
    $("resetUsageBtn").dataset.bound = "1";
    $("resetUsageBtn").onclick = resetUsage;
  }
})();
