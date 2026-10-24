/* ODU Tracker dashboard logic (vanilla JS, zero dependencies) */
const $ = (id) => document.getElementById(id);
let TAB = "dashboard";
let tick = null;

function fmtTime(ts) {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit"
  });
}
function fmtWhen(ts) {
  if (!ts) return "–";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}
function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg; t.style.display = "block";
  setTimeout(() => (t.style.display = "none"), ms);
}

/* ------------------------------------------------ tabs */
document.querySelectorAll("nav .navlink").forEach((el) => {
  el.onclick = () => showTab(el.dataset.tab);
});
function showTab(name) {
  TAB = name;
  document.querySelectorAll("nav .navlink").forEach(
    (n) => n.classList.toggle("active", n.dataset.tab === name));
  ["dashboard", "devices", "traffic", "websites", "alerts", "settings"].forEach((t) => {
    $("tab-" + t).style.display = t === name ? "" : "none";
  });
  refresh();
}

/* ------------------------------------------------ auth */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (r.status === 401) { showLogin(); throw new Error("auth required"); }
  return r.json();
}
async function doLogin() {
  const pw = $("pw").value;
  if (!pw) { $("loginErr").textContent = "password required"; return; }
  $("loginBtn").textContent = "connecting…";
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  }).then((r) => r.json()).catch((e) => ({ error: String(e) }));
  $("loginBtn") && ($("loginBtn").textContent = "Sign in");
  if (res.error) { $("loginErr").textContent = res.error; return; }
  $("loginErr").textContent = "";
  enterMain();
}
function showLogin() {
  $("loginView").style.display = "";
  $("mainView").style.display = "none";
  if (tick) clearInterval(tick);
}
async function enterMain() {
  $("loginView").style.display = "none";
  $("mainView").style.display = "";
  if (!tick) tick = setInterval(() => refresh(), 5000);
  refresh();
}

/* ------------------------------------------------ refresh loops */
async function refresh() {
  if ($("mainView").style.display === "none") return;
  try {
    await Promise.all([refreshMeta(), refreshStatus()]);
    if (TAB === "dashboard") await refreshDashboard();
    if (TAB === "devices") await refreshDevices();
    if (TAB === "traffic") await refreshTraffic();
    if (TAB === "websites") await refreshSites();
    if (TAB === "alerts") await refreshAlerts();
    if (TAB === "settings") await refreshSettings();
  } catch (e) { /* auth handled in api() */ }
}

async function refreshMeta() {
  const m = await api("/api/meta");
  $("cfgIp").textContent = m.router_ip;
  const dot = $("pollDot"), txt = $("pollText");
  if (m.poll_running && m.router_logged_in) {
    dot.className = "dot on"; txt.textContent = "live · " + m.router_ip;
  } else if (m.poll_running) {
    dot.className = "dot warn"; txt.textContent = "logging in…";
  } else {
    dot.className = "dot off"; txt.textContent = "polling stopped";
  }
}

function fmtUptime(sec) {
  sec = +sec || 0;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return (d ? d + "d " : "") + h + "h " + m + "m";
}

let LAST_STATUS = null;
async function refreshStatus() {
  LAST_STATUS = await api("/api/status");
  const s = LAST_STATUS;

  // ---- usage (monthly totals from router flow counters)
  const f = s.flow || {};
  const totalGb = f.total_mb != null ? (parseFloat(f.total_mb) / 1024) : null;
  $("usageTotal").textContent = totalGb != null ? totalGb.toFixed(2) + " GB" : "–";
  const downGb = f.down_mb != null ? (parseFloat(f.down_mb) / 1024) : null;
  const upGb = f.up_mb != null ? (parseFloat(f.up_mb) / 1024) : null;
  $("usageSub").textContent =
    `${downGb != null ? "↓ " + downGb.toFixed(2) + " GB" : ""} ${upGb != null ? "↑ " + upGb.toFixed(2) + " GB" : ""}` +
    (f.limit_switch === "1" && f.limit_size ? ` · plan ${f.limit_size} GB` : " · no quota limit set");
  if (f.limit_switch === "1" && f.limit_size && totalGb != null) {
    $("usageMeter").style.width = Math.min(100, (totalGb / parseFloat(f.limit_size)) * 100).toFixed(0) + "%";
  } else {
    $("usageMeter").style.width = "0%";
  }
  const today = s.day_usage;
  $("usageToday").textContent = today != null ? today.toFixed(2) + " GB" : "collecting…";

  // ---- signal / network
  const sys = s.sys || {};
  const lvl = Math.max(1, +(sys.signal_lvl || 1));
  $("signal").textContent = "▮".repeat(lvl) + "▯".repeat(5 - lvl) + " (" + lvl + "/5)";
  $("netType").textContent = [sys.network_type_str, sys.network_operator, `RSRP ${s.signal.rsrp ?? "?"} dBm`]
    .filter(Boolean).join(" · ");

  // ---- rates (passive)
  const r = s.rates || {};
  $("rateNow").textContent = r.current != null ? r.current + " Mbps" : "idle";
  $("rateAvg").textContent = `avg 10m: ${r.avg_10m ?? "–"} Mbps · avg 1h: ${r.avg_hour ?? "–"} Mbps`;

  // ---- runtime / health
  const w = s.wan || {};
  $("runtimeKv").innerHTML = [
    ["device", `${sys.board_type || "?"} · ${sys.idu_dev_type || ""}`],
    ["firmware", w.fw || "–"],
    ["serial", w.device_sn || "–"],
    ["cpu load", (w.cpuload || "–").split(",").slice(0, 2).join(" / ") || "–"],
  ].map((kv) => `<span>${kv[0]}</span><span><b>${esc(kv[1])}</b></span>`).join("");
  $("wanKv").innerHTML = [
    ["WAN IP", w.wan_ip || "– (network up when data flows)"],
    ["uptime", fmtUptime(w.uptime)],
    ["Wi-Fi 2.4G", w.ssid24 || "–"],
    ["Wi-Fi 5G", w.ssid58 || "–"],
    ["LAN", w.lan_ip || "–"],
  ].map((kv) => `<span>${kv[0]}</span><span><b>${esc(kv[1])}</b></span>`).join("");
}

async function refreshDashboard() {
  const d = await api("/api/devices");
  $("devCount").textContent = d.count + " known devices";
  const online = d.devices.filter((x) => x.online);
  $("noOnline").style.display = online.length ? "none" : "";
  $("onlineCards").innerHTML = online.map((x) => `
    <div class="card" style="background:var(--panel2)">
      <div style="font-weight:600; font-size:.9rem">${esc(x.last_hostname || "(unnamed)")}</div>
      <div class="sub">${esc(x.last_ip || "")}</div>
      <div style="margin-top:8px">${riskBadge(x.risklevel)} <span class="badge ${x.online ? "green" : "gray"}">${x.online ? "online" : "offline"}</span></div>
    </div>`).join("");
  const ev = await api("/api/events?limit=12");
  $("recentEvents").innerHTML = ev.events.length ? ev.events.slice().reverse().map((e) => `
    <tr><td>${fmtTime(e.ts)}</td><td>${esc(e.kind)}</td><td>${esc(e.detail || "")}</td></tr>`).join("")
    : `<tr><td colspan="3"><div class="empty">no activity yet</div></td></tr>`;
}

/* ------------------------------------------------ devices */
function esc(s) {
  return (s != null ? String(s) : "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
function riskBadge(level) {
  const cls = { high: "red", medium: "yellow", low: "green", unknown: "gray" }[level || "unknown"];
  const label = level && level !== "unknown" ? level.toUpperCase() : "?";
  return `<span class="badge ${cls}" title="${(level || 'unknown')} risk">${label}</span>`;
}
let DEVICES = null;
async function refreshDevices() {
  const d = await api("/api/devices");
  DEVICES = d;
  $("devTotals").textContent = `${d.devices.filter(x=>x.online).length}/${d.count} online`;
  const filter = ($("devFilter").value || "").toLowerCase();
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
      <td><span class="badge ${x.online ? "green" : "gray"}">${x.online ? "●" : "○"}</span></td>
      <td style="font-weight:600">${esc(x.last_hostname || "(unnamed)")} ${x.trusted ? `<span class="badge gray">trusted</span>` : ""}${x.blocked ? `<span class="badge red">blocked</span>` : ""}</td>
      <td>${esc(x.last_ip || "–")}</td>
      <td style="font-family:monospace; font-size:.78rem">${esc(x.mac)}</td>
      <td>${esc(x.vendor || "unknown")}</td>
      <td>${esc(x.last_iface || "–")}</td>
      <td>${fmtWhen(x.first_seen)}</td>
      <td>${fmtWhen(x.last_seen)}</td>
      <td>${riskBadge(x.risklevel)}<div class="tagline">${esc((x.riskreasons || []).join("; "))}</div></td>
      <td>
        <button class="btn small" onclick="toggleTrust('${x.mac}', ${!x.trusted})">${x.trusted ? "untrust" : "trust"}</button><br>
        <button class="btn small ${x.blocked ? "" : "danger"}" style="margin-top:4px" onclick="toggleBlock('${x.mac}', ${!x.blocked})">${x.blocked ? "unblock" : "block"}</button>
      </td>
    </tr>`).join("")
    : `<tr><td colspan="10"><div class="empty">no devices match</div></td></tr>`;
}
async function toggleTrust(mac, v) {
  await api(`/api/devices/${mac}/trust`, { method: "POST", body: JSON.stringify({ trusted: v }) });
  toast(v ? "Device trusted" : "Device untrusted");
  refreshDevices();
}
async function toggleBlock(mac, v) {
  if (v && !confirm("Block this device on the router (MAC filter)?")) return;
  const r = await api(`/api/devices/${mac}/block`, { method: "POST", body: JSON.stringify({ blocked: v }) });
  if (r.router && r.router.ok === false) toast("Router refused: " + (r.router.error || "unknown"), 3500);
  else toast(v ? "Blocked on router" : "Unblocked", 2600);
  refreshDevices();
}

/* ------------------------------------------------ traffic */
async function refreshTraffic() {
  const u = await api("/api/usage?hours=24");
  // hourly buckets
  const buckets = new Array(24).fill(0);
  const nowH = Date.now() / 1000;
  u.series.forEach((s2) => {
    const h = Math.floor((nowH - s2.ts) / 3600);
    if (h >= 0 && h < 24) buckets[23 - h] += Math.max(0, s2.delta || 0);
  });
  const maxB = Math.max(...buckets, 1e-6);
  $("usageBars").innerHTML = buckets.map((v) => {
    const pct = Math.min(100, v > 0 ? 6 + (v / maxB) * 94 : 1);
    return `<div class="bar" style="height:${pct}%" title="${v.toFixed(2)} GB in hour ${23 - buckets.indexOf(v)}h ago"></div>`;
  }).join("");

  const s = LAST_STATUS || {};
  const f = s.flow || {};
  $("usageKv").innerHTML = [
    ["month total", f.total_mb != null ? (f.total_mb / 1024).toFixed(2) + " GB" : "–"],
    ["month ↓ down", f.down_mb != null ? (f.down_mb / 1024).toFixed(2) + " GB" : "–"],
    ["month ↑ up", f.up_mb != null ? (f.up_mb / 1024).toFixed(2) + " GB" : "–"],
    ["quota", f.limit_switch === "1" ? (f.limit_size || "?") + " GB" : "unlimited (off)"],
    ["samples", u.series.length ? u.series.length : "collecting…"],
  ].map((kv) => `<span>${kv[0]}</span><span><b>${esc(kv[1])}</b></span>`).join("");

  // live rates
  const r = s.rates || {};
  $("ratesKv").innerHTML = [
    ["current interval", r.current != null ? r.current + " Mbps" : "idle / no traffic"],
    ["avg last 10 min", r.avg_10m != null ? r.avg_10m + " Mbps" : "–"],
    ["avg last hour", r.avg_hour != null ? r.avg_hour + " Mbps" : "–"],
  ].map((kv) => `<span>${kv[0]}</span><span><b>${esc(kv[1])}</b></span>`).join("");
  const rr = await api("/api/rates?minutes=30");
  const rateVals = rr.series.map((x) => x.mbps);
  const rateMax = Math.max(...rateVals, 1e-6);
  $("rateBars").innerHTML = rr.series.map((x) => {
    const pct = Math.max(2, (x.mbps / rateMax) * 100);
    return `<div class="bar" style="height:${pct}%" title="${x.mbps} Mbps"></div>`;
  }).join("") || `<div class="empty" style="width:100%">waiting for samples…</div>`;

  // speed test state + history
  await renderSpeedTest();
}

async function renderSpeedTest() {
  const st = await api("/api/speedtest?limit=12");
  if (st.running) {
    $("stBtn").textContent = "⏳ testing (" + (st.phase || "…") + ")";
    $("stBtn").disabled = true;
  } else if (st.running === false && st.down_mbps === null && st.error) {
    $("stErr").textContent = st.error;
  }
  const last = st.last || {};
  if (!st.running && (!st.last || last.ts !== (renderSpeedTest._lastTs))) {
    $("stResult").innerHTML = last.down_mbps != null ? [
      ["download", last.down_mbps + " Mbps"],
      ["upload", last.up_mbps != null ? last.up_mbps + " Mbps" : "–"],
      ["ping", last.ping_ms != null ? last.ping_ms.toFixed(0) + " ms" : "–"],
      ["data used", ((last.bytes_used || 0) / 1024 / 1024).toFixed(1) + " MB"],
    ].map((kv) => `<div class="card" style="background:var(--panel2)">
        <div class="tagline">${kv[0]}</div><div class="big" style="font-size:1.2rem">${esc(kv[1])}</div></div>`).join("")
      : "";
    renderSpeedTest._lastTs = last.ts;
  }
  const avgDown = st.avg_down != null ? st.avg_down + " Mbps" : "–";
  const avgUp = st.avg_up != null ? st.avg_up + " Mbps" : "–";
  $("stTbody").innerHTML = (st.history || []).length ? st.history.map((h) => `
    <tr><td>${fmtTime(h.ts)}</td><td style="font-weight:700; color:var(--accent2)">${h.down_mbps ?? "–"} Mbps</td>
    <td>${h.up_mbps ?? "–"} Mbps</td><td>${h.ping_ms != null ? (+h.ping_ms).toFixed(0) + " ms" : "–"}</td></tr>`).join("")
    + `<tr style="background:#101c13"><td><b>average (${st.count})</b></td><td><b>${esc(avgDown)}</b></td><td><b>${esc(avgUp)}</b></td><td></td></tr>`
    : `<tr><td colspan="4"><div class="empty">no speed tests yet — click Run speed test</div></td></tr>`;
}

async function runSpeedTest() {
  const size = +$("stSize").value;
  $("stErr").textContent = "";
  $("stBtn").textContent = "⏳ starting…";
  const r = await api("/api/speedtest/run", { method: "POST", body: JSON.stringify({ size_mb: size, up_mb: 1 }) });
  if (!r.ok) { $("stErr").textContent = r.error || "failed to start"; }
  else {
    const poll = setInterval(async () => {
      const st = await api("/api/speedtest?limit=12");
      if (st.running) {
        $("stBtn").textContent = "⏳ testing (" + (st.phase || "…") + ")";
        $("stBtn").disabled = true;
      } else {
        clearInterval(poll);
        $("stBtn").textContent = "▶ Run speed test";
        $("stBtn").disabled = false;
        renderSpeedTest._lastTs = null;   // force re-render
        toast(st.last && st.last.error ? "test failed: " + st.last.error : "speed test done", 3000);
      }
    }, 1500);
  }
}

/* ------------------------------------------------ websites */
async function refreshSites() {
  const s2 = await api(`/api/sites?hours=6`);
  $("sitesTbody").innerHTML = s2.domains.length ? s2.domains.slice(0, 120).map((d) => `
    <tr><td style="font-weight:600">${esc(d.domain)}</td><td>${d.hits}</td>
    <td>${d.clients}</td><td>${fmtTime(d.first_seen)}</td><td>${fmtWhen(d.last_seen)}</td></tr>`).join("")
    : `<tr><td colspan="5"><div class="empty">no dns data yet</div></td></tr>`;
  $("noSites").style.display = s2.domains.length ? "none" : "";
  $("urlRulesTbody").innerHTML = (s2.url_rules || []).slice(0, 60).map((r) => `
    <tr><td>${esc(r.url)}</td><td>${r.enabled ? "enabled" : "off"}</td></tr>`).join("")
    || `<tr><td colspan="2"><div class="empty">no URL rules found</div></td></tr>`;
  $("siteClients").innerHTML = (s2.clients || []).map((c) => `
    <tr><td>${esc(c.client_ip)}</td><td>${c.hits}</td><td>${c.domains}</td></tr>`).join("")
    || `<tr><td colspan="3"><div class="empty">no client data</div></td></tr>`;
}

/* ------------------------------------------------ alerts */
async function refreshAlerts() {
  const a = await api("/api/alerts");
  $("alertsTbody").innerHTML = a.alerts.length ? a.alerts.map((x) => `
    <tr ${x.read ? 'style="opacity:.55"' : ""}>
      <td>${fmtTime(x.ts)}</td>
      <td>${levelBadge(x.level)}</td>
      <td style="font-weight:600">${esc(x.title)}</td>
      <td>${esc(x.detail || "")}</td>
    </tr>`).join("")
    : `<tr><td colspan="4"><div class="empty">no alerts</div></td></tr>`;
}
function levelBadge(level) {
  const cls = { danger: "red", warning: "yellow", success: "green", info: "gray" }[level] || "gray";
  return `<span class="badge ${cls}">${esc(level || "info")}</span>`;
}
async function markAlertsRead() {
  await api("/api/alerts/read", { method: "POST" });
  refreshAlerts();
}

/* ------------------------------------------------ settings */
async function refreshSettings() {
  const m = await api("/api/meta");
  const raws = [];
  ["fast", "slow", "url_filter"].forEach((n) => raws.push(n));
  $("rawLinks").innerHTML = raws.map((n) =>
    `<button class="btn small" data-name="${n}" onclick="showRaw('${n}')">${n}</button>`).join("");
  $("dnsEnabled").onchange && ($("dnsEnabled").onchange = null);
  const cfg = await api("/api/dnscfg");
  $("dnsEnabled").checked = !!cfg.enabled;
  $("dnsPort").textContent = cfg.port;
  $("dnsUpstream").textContent = cfg.upstream;
  $("dnsHow").innerHTML = `this PC's LAN IP (likely): <b>${esc(cfg.myIp || "?")}</b>`;
}
async function saveDns() {
  const on = $("dnsEnabled").checked;
  const r = await api("/api/dnscfg", { method: "POST", body: JSON.stringify({ enabled: on }) });
  toast(r.enabled ? "DNS proxy running on port " + r.port : "DNS proxy stopped");
}
async function showRaw(name) {
  const r = await api(`/api/raw?name=${name}`);
  $("rawBody").textContent = JSON.stringify(JSON.parse(r.body || "{}"), null, 2).slice(0, 20000);
}
async function changePw() {
  const pw = $("newPw").value;
  if (!pw) { toast("type a password first"); return; }
  const r = await fetch("/api/changepw", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }) });
  const j = await r.json();
  if (j.ok) { toast("password updated"); $("newPw").value = ""; }
  else toast("failed: " + (j.error || "?"));
}

/* ------------------------------------------------ boot */
(async function boot() {
  const m = await fetch("/api/meta").then((r) => r.json());
  $("routerip").textContent = m.router_ip || "192.168.0.1";
  // show router login-attempts budget so nobody wastes tries
  if (m.attempts_left != null) {
    const warn = m.attempts_left <= 2;
    $("attemptInfo").innerHTML = warn
      ? `<span style="color:var(--danger)">⚠ only ${m.attempts_left} wrong password attempt(s) left — use the exact admin password</span>`
      : `router attempts available: ${m.attempts_left}`;
  }
  showLogin();
  $("pw").focus();
})();
