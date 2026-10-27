"""Captive-portal HTTP server: the voucher-claim page for blocked devices.

Listens on 0.0.0.0:80 (one-time privilege setup: tools/enable_priv_ports.sh).
The DNS proxy points every blocked device's lookups at this machine, so
whatever URL they open lands here.

Probe handling: ANY GET — regardless of Host or path — returns the claim
page with HTTP 200. That is precisely the response Android (generate_204),
iOS (hotspot-detect.html) and Windows (ncsi.txt) treat as "portal detected",
so the device pops its sign-in sheet on its own; no per-probe special cases.

Claim flow (POST /claim):
  ip  -> mac (router cmd 223, falling back to the local cache)
      -> voucher.claim (credits the shared pool, binds the code to the MAC)
      -> deny-cache invalidation (next DNS query passes within seconds)
      -> cmd 278 gate_allow (clears any grace deny-listing)
Rate-limited per client IP: unauthenticated claim attempts are all we get.
"""
import threading
import time

from flask import Flask, jsonify, request

from odu_tracker import store, voucher

# ip -> [attempt ts]; sliding window, unauthenticated endpoint
_RATE = {}
RATE_LIMIT = 10            # attempts...
RATE_WINDOW = 300          # ...per 5 minutes per IP

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ODU WiFi — Voucher</title>
<style>
  body{font-family:system-ui,-apple-system,sans-serif;background:#0b1220;
       color:#e6edf7;margin:0;display:flex;justify-content:center;
       min-height:100vh;align-items:center}
  .card{background:#131c2e;border:1px solid #24314c;border-radius:16px;
        padding:28px 22px;width:100%;max-width:360px;margin:12px;
        box-shadow:0 12px 40px rgba(0,0,0,.45)}
  h1{font-size:20px;margin:0 0 4px;letter-spacing:.5px}
  p.sub{color:#8fa3c4;font-size:13px;margin:0 0 20px;line-height:1.5}
  input{width:100%;box-sizing:border-box;padding:14px;font-size:17px;
        letter-spacing:2px;border-radius:10px;border:1px solid #2c3c5e;
        background:#0e1626;color:#e6edf7;text-align:center;outline:none}
  input:focus{border-color:#4a90ff}
  button{width:100%;margin-top:14px;padding:14px;font-size:16px;font-weight:600;
         border:none;border-radius:10px;background:#2f6fed;color:#fff;cursor:pointer}
  button:disabled{opacity:.55}
  #msg{margin-top:14px;font-size:14px;line-height:1.5;min-height:20px}
  .ok{color:#4ade80}.err{color:#ff8f8f}
  .foot{color:#5b6c8c;font-size:11px;text-align:center;margin-top:18px}
</style></head>
<body><div class="card">
  <h1>&#128246; ODU WiFi</h1>
  <p class="sub">This device needs a voucher to go online.<br>
     Enter your code to get connected.</p>
  <input id="code" placeholder="e.g. tRt4r49ybdj" autocapitalize="off"
         autocorrect="off" spellcheck="false" autocomplete="off">
  <button id="go">Connect</button>
  <div id="msg"></div>
  <div class="foot">No voucher? Ask the network admin to generate one.</div>
</div>
<script>
const $=id=>document.getElementById(id);
$("go").onclick=async()=>{
  const code=$("code").value.trim(), msg=$("msg");
  if(!code){msg.className="err";msg.textContent="Enter a voucher code first.";return}
  $("go").disabled=true; msg.textContent="Checking…"; msg.className="";
  try{
    const r=await fetch("/claim",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code})});
    const d=await r.json().catch(()=>({}));
    if(r.ok&&d.ok){
      msg.className="ok";
      msg.textContent="Connected! "+(d.quota_mb?format(d.quota_mb)+" credited. ":"")
        +"You can close this page and browse normally.";
      $("go").textContent="Connected \\u2713";
      setTimeout(()=>{location.reload()},4000);
    }else{
      msg.className="err"; msg.textContent=d.error||"Claim failed, try again.";
      $("go").disabled=false;
    }
  }catch(e){
    msg.className="err"; msg.textContent="Network error, try again.";
    $("go").disabled=false;
  }
};
$("code").addEventListener("keydown",e=>{if(e.key==="Enter")$("go").click()});
function format(mb){return mb>=1024?(mb/1024).toFixed(0)+" GB":mb+" MB"}
</script>
</body></html>"""


def _rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        _RATE[ip] = hits
        return False
    hits.append(now)
    _RATE[ip] = hits
    if len(_RATE) > 4096:
        _RATE.clear()
    return True


def create_portal_app(cfg, poller):
    app = Flask("odu_portal")
    app.json.sort_keys = False

    @app.get("/")
    @app.get("/<path:path>")           # catch-all: any probe URL, any Host
    def page(path=""):
        return PAGE, 200, {"Content-Type": "text/html; charset=utf-8",
                           "Cache-Control": "no-store"}

    @app.post("/claim")
    def claim():
        ip = request.remote_addr or ""
        if not _rate_ok(ip):
            return jsonify({"error": "too many attempts, wait a few minutes"}), 429
        body = request.json or {}
        code = str(body.get("code") or "").strip()
        if not code:
            return jsonify({"error": "code required"}), 400
        router = getattr(poller, "router", None)
        mac = None
        if router is not None:
            try:
                mac = voucher.mac_for_ip(router, ip)     # live cmd 223
            except Exception:
                mac = None
        mac = mac or voucher.mac_for_ip_local(ip, ttl=5)
        if not mac:
            # poller hasn't seen the device yet — tell it to retry shortly
            return jsonify({"error": "device not identified yet — "
                                     "check Wi-Fi is on and retry in a few "
                                     "seconds"}), 409
        try:
            res = voucher.claim(code, mac=mac, ip=ip)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        voucher.invalidate_deny_cache()       # next DNS query passes now
        gate = {}
        if router is not None:
            try:
                gate = voucher.gate_allow(router, mac)   # clear grace deny
            except Exception as exc:
                gate = {"ok": False, "error": str(exc)[:120]}
        store.add_alert("info", "Voucher claimed via portal",
                        f"{mac} code={res['code']} quota={res['quota_mb']}MB")
        return jsonify({"ok": True, "quota_mb": res["quota_mb"], "gate": gate})

    return app


_singleton = None
_state = {"running": False, "port": 80, "error": None}


def start_portal(cfg, poller):
    """Idempotent; runs the portal on its own waitress thread."""
    global _singleton
    if _singleton and _singleton.is_alive():
        return _state
    port = int(cfg.get("portal_port", 80))
    _state.update({"running": False, "port": port, "error": None})

    def run():
        from waitress import serve
        try:
            serve(create_portal_app(cfg, poller), host="0.0.0.0", port=port,
                  threads=4, clear_untrusted_proxy_headers=True)
        except OSError as exc:
            _state["error"] = (f"cannot bind :{port}: {exc} (privileged port — "
                               f"run tools/enable_priv_ports.sh once)")
        except Exception as exc:
            _state["error"] = str(exc)[:200]

    _singleton = threading.Thread(target=run, daemon=True, name="odu-portal")
    _state["running"] = True
    _singleton.start()
    return _state


def status() -> dict:
    alive = bool(_singleton and _singleton.is_alive() and not _state["error"])
    out = dict(_state)
    out["running"] = alive
    return out
