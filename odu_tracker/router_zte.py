"""ZTE LuQUID web-GUI client - MTN FibreX (ZXHN F6600P class) routers.

The FibreX gateway does NOT speak the ZLT cgi-bin protocol OduRouter was
reverse-engineered from, so it gets its own client. Everything here is
READ-ONLY: this module never changes a router setting.

Protocol (from public, live-verified documentation of ZTE's "LuQUID"
framework, Server: ZTE web server 1.0 ZTE corp 2015):
  login   GET  /                        -> page embeds _sessionTOKEN", "<tok>"
          GET  /function_module/login_module/login_page/logintoken_lua.lua
                                         -> one-time token (XML-wrapped)
          POST /  Username=<u>, Password=sha256(password+token),
                  action=login, _sessionTOKEN=<tok>
          -> router answers with a SID cookie (+ _TESTCOOKIESUPPORT)
  pages   GET  /getpage.lua?pid=1002&nextpage=<Page>_t.lp
  data    page HTML embeds its data endpoints in <span class="form-action">;
          each answers XML  <ajax_response_xml_root><OBJ_x_ID><Instance>
          <ParaName>k</ParaName><ParaValue>v</ParaValue> ...
  bound   data endpoints only answer for the page visited LAST (anything
          else -> SessionTimeout), so every fetch is visit-then-get.
  idle    sessions die in 1-2 minutes: proactive re-login at 85 s plus
          reactive re-login whenever a response looks logged out.

Discovery is model-independent: after login the client crawls the menu for
`nextpage=` links and classifies pages by element id (lan_info,
Wlan_ClientStat, DHCPHostInfo, ...), so GPON page names differing from the
DSL gateway this protocol was documented on do not break it.

Interface parity with OduRouter: ip/username/password attrs, logged_in
(plain attribute - the poller assigns False on errors), login()/logout(),
snapshot_all()/slow_snapshot(), cmd() raising for ZLT-only features, plus
`features` flags the poller and web app gate on.
"""
import hashlib
import http.cookiejar
import html as html_mod
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .router import RouterError

TOKEN_PAGE = "/function_module/login_module/login_page/logintoken_lua.lua"
PAGE_URL = "/getpage.lua?pid=1002&nextpage={page}.lp"
SESSION_TOKEN_RE = re.compile(r'_sessionTOKEN["\']?\s*,\s*["\']([0-9A-Za-z]+)["\']')
FORM_ACTION_RE = re.compile(r'<span[^>]*class="form-action"[^>]*>([^<]+)</span>')
NEXT_PAGE_RE = re.compile(r'nextpage=([A-Za-z0-9_.]+)')
HREF_RE = re.compile(r'href="([^"#]+)"')
# "we got bounced to the login form" markers (framework JS is always
# present, the login form itself is the reliable signal)
LOGGED_OUT_MARKERS = ("Frm_Username", "frmLogin", "login_sec.gch")
# element ids that identify a page's role (from the F6600P/F672Y dashboard)
ROLE_MARKERS = {
    "lan": ('id="lan_info"',),
    "wlan": ('id="Wlan_ClientStat"', 'id="WLANStatus"'),
    "dhcp": ('id="DHCPHostInfo"',),
    "home": ('id="home_adev_area"',),
    "topology": ('id="master-area"',),
    "urlfilter": ('id="URLFilter"', 'id="url_filter"', 'ParentCtl'),
}
# page-name seeds (documented ZTE names) - the crawl discovers the rest
SEED_PAGES = (
    "ManagDiag_StatusManag_t", "Status_DSLWAN_t", "Status_DSLWAN_statis_t",
    "Status_LAN_t", "Status_DHCP_t", "Status_arp_t", "Localnet_WlanBasicAd_t",
    "Localnet_WLANStationInfo_t", "Internet_LAN_Conf_t",
    "Internet_ParentCtl_URLFilter_t", "ManagDiag_SystemLog_t",
)
SEED_URLS = [PAGE_URL.format(page=p) for p in SEED_PAGES] + ["/"]

# field classification for span ids / XML ParaNames:
#   MACAddress:3, MAC_3, HostName:3, IPAddr_3, ... and the bare XML
#   ParaName form (MACAddress / InBytes with no index at all).
_FIELD_RE = re.compile(
    r"^(?P<field>MACAddress|MACAddr|IPAddress|IPAddr|HostName|Hostname|"
    r"TotalBytesCount|InBytes|OnlineDuration|MAC|IP|Host)"
    r"(?P<sep>[:_]?)(?P<idx>.*)$")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_INFO_IDS_RE = re.compile(
    r"(Model|Software|Firmware|Serial|Uptime|OnlineDuration|Version|Product)",
    re.I)


def _norm_mac(v):
    # uppercase, like security.normalize_mac everywhere else - a lowercase
    # MAC here would be a second identity for the same device in the DB
    v = (v or "").strip().upper().replace("-", ":")
    return v if _MAC_RE.match(v) else ""


# ------------------------------------------------------------------ parsing
class _PageScan(HTMLParser):
    """Collects spans (with their nearest template_ div block) and table
    rows from one page - the F6600P dashboard renders every value in a
    <span id="Field:idx"> inside a <div id="template_..."> item."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._div_stack = []
        self._span = None          # (id, [text parts], block)
        self._row = None
        self._cell = None
        self.spans = []            # (block, span_id, text)
        self.rows = []             # [[cell text, ...], ...]

    def _block(self):
        for d in reversed(self._div_stack):
            if d.startswith("template_"):
                return d
        return ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div":
            self._div_stack.append(a.get("id") or "")
        elif tag == "span":
            self._span = [a.get("id") or "", [], self._block()]
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "div" and self._div_stack:
            self._div_stack.pop()
        elif tag == "span" and self._span:
            sid, parts, block = self._span
            self.spans.append((block, sid, "".join(parts).strip()))
            self._span = None
        elif tag == "tr" and self._row is not None:
            if any(c.strip() for c in self._row):
                self.rows.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append("".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._span is not None:
            self._span[1].append(data)
        if self._cell is not None:
            self._cell.append(data)


def _parse_page(html_text):
    scan = _PageScan()
    try:
        scan.feed(html_text)
        scan.close()
    except Exception:
        pass
    return scan


def _field_of(span_id):
    """('MACAddress:3') -> ('mac', '3') / ('InBytes_2') -> ('bytes', '2')."""
    m = _FIELD_RE.match(span_id or "")
    if not m:
        return None, None
    raw = m.group("field")
    idx = m.group("idx")
    low = raw.lower()
    if low in ("macaddress", "macaddr", "mac"):
        return "mac", idx
    if low in ("ipaddress", "ipaddr", "ip"):
        return "ip", idx
    if low in ("hostname", "host"):
        return "host", idx
    if low in ("inbytes", "totalbytescount"):
        return "bytes", idx
    return None, None


def _bytes_value(text):
    """'123456/65432' -> (rx, tx) ints; tolerates unit suffixes."""
    if not text or "/" not in text:
        return None
    a, _, b = text.partition("/")
    def num(s):
        s = re.sub(r"[^0-9.]", "", s.strip())
        try:
            return float(s)
        except ValueError:
            return None
    rx, tx = num(a), num(b)
    if rx is None or tx is None:
        return None
    return rx, tx


def _devices_from_scan(scan, iface):
    """Template-block pairing: each div[id^=template_] on a client page is
    one device; grab whichever mac/ip/host/bytes spans live inside it."""
    out = []
    blocks = {}
    for block, sid, text in scan.spans:
        if not block:
            continue
        field, _ = _field_of(sid)
        if not field:
            continue
        blocks.setdefault(block, {})[field] = (sid, text)
    for block, fields in blocks.items():
        mac = ""
        for key in ("mac",):
            if key in fields:
                mac = _norm_mac(fields[key][1])
                if mac:
                    break
        if not mac:
            continue
        ip = ""
        if "ip" in fields:
            m = _IPV4_RE.search(fields["ip"][1])
            ip = m.group(0) if m else ""
        host = fields.get("host", ("", ""))[1].strip()
        dev = {"mac": mac, "ip": ip, "hostname": host, "iface": iface,
               "raw": {"block": block}}
        if "bytes" in fields:
            bv = _bytes_value(fields["bytes"][1])
            if bv:
                dev["rx"], dev["tx"] = bv
        out.append(dev)
    # table fallback (DHCP leases page is a plain table on some firmwares)
    for row in scan.rows:
        mac = ""
        ip = ""
        host = ""
        for cell in row:
            cm = _MAC_RE.match(cell.strip())
            if cm and not mac:
                mac = cell.strip().upper()
                continue
            im = _IPV4_RE.fullmatch(cell.strip())
            if im and not ip:
                ip = cell.strip()
                continue
            if (cell.strip() and not host and not cm and not im
                    and len(cell.strip()) < 64):
                host = cell.strip()
        if mac and not any(d["mac"] == mac for d in out):
            out.append({"mac": mac, "ip": ip, "hostname": host,
                        "iface": iface, "raw": {"table": True}})
    return out


def parse_luquid(xml_text):
    """<ajax_response_xml_root><OBJ_x_ID><Instance><ParaName>... -> dict."""
    out = {}
    for g in re.finditer(r"<(OBJ_\w+)>(.*?)</\1>", xml_text, re.S):
        obj, body = g.group(1), g.group(2)
        insts = []
        for im in re.finditer(r"<Instance>(.*?)</Instance>", body, re.S):
            pairs = re.findall(
                r"<ParaName>([^<]+)</ParaName><ParaValue>(.*?)</ParaValue>",
                im.group(1), re.S)
            insts.append({k: html_mod.unescape(v) for k, v in pairs})
        out.setdefault(obj, []).extend(insts)
    return out


def _devices_from_xml(parsed, iface):
    out = []
    for obj, insts in parsed.items():
        for inst in insts:
            mac = ""
            ip = ""
            host = ""
            rx = tx = None
            for k, v in inst.items():
                field, _ = _field_of(k)
                if field == "mac" and not mac:
                    mac = _norm_mac(v)
                elif field == "ip" and not ip:
                    m = _IPV4_RE.search(v or "")
                    ip = m.group(0) if m else ""
                elif field == "host" and not host:
                    host = (v or "").strip()
                elif field == "bytes" and rx is None:
                    bv = _bytes_value(v)
                    if bv:
                        rx, tx = bv
            if not mac:
                continue
            dev = {"mac": mac, "ip": ip, "hostname": host, "iface": iface,
                   "raw": {"obj": obj}}
            if rx is not None:
                dev["rx"], dev["tx"] = rx, tx
            out.append(dev)
    return out


# ------------------------------------------------------------------- client
class ZteRouter:
    """Read-only client for one ZTE LuQUID router (FibreX class)."""

    kind = "zte"
    features = {"zlt": False, "vouchers": False, "portal": False}

    def __init__(self, ip="192.168.1.1", username="admin", password="",
                 timeout=8):
        self.ip = ip
        self.username = username
        self.password = password
        self.base = f"http://{ip}"
        self.timeout = timeout
        self.logged_in = False
        self._sid = None
        self._login_ts = 0.0
        self._last_page = None
        self._lock = threading.RLock()
        self._cj = http.cookiejar.CookieJar()
        # ProxyHandler({}) -> direct LAN connection, ignore system proxy
        self._op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cj))
        self._op.addheaders = [
            ("User-Agent", "Mozilla/5.0 (compatible; ODU-Tracker/1.5)"),
            ("X-Requested-With", "XMLHttpRequest"),
        ]
        # discovered page map: role -> url ; endpoint map: url -> [eps]
        self._pages = {}
        self._discovered = False
        self._discovering = False
        self._usage_peak_mb = 0.0

    # ------------------------------------------------------------- transport
    def _req(self, path, data=None, referer=None, retries=2):
        url = path if path.startswith("http") else self.base + path
        body = (urllib.parse.urlencode(data).encode() if data else None)
        delay = 0.4
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body)
                if referer:
                    req.add_header("Referer", referer)
                if body:
                    req.add_header(
                        "Content-Type", "application/x-www-form-urlencoded")
                with self._op.open(req, timeout=self.timeout) as resp:
                    return resp.getcode(), resp.read().decode("utf-8", "ignore")
            except Exception as exc:  # noqa: BLE001 - report last error
                last = exc
                if attempt + 1 < retries:
                    time.sleep(delay)
                    delay *= 2
        raise RouterError(f"router unreachable: {type(last).__name__}: {last}")

    def _sid_cookie(self):
        for ck in self._cj:
            if ck.name == "SID":
                return ck.value
        return None

    @staticmethod
    def _looks_logged_out(text):
        return any(m in text for m in LOGGED_OUT_MARKERS)

    # ---------------------------------------------------------------- auth
    def login(self, password=None):
        """Establish a SID session. Auth failures raise immediately (the
        firmware counts wrong passwords towards its lockout)."""
        if password is not None:
            self.password = password
        with self._lock:
            self._login_luquid()

    def _login_luquid(self):
        last_err = None
        for attempt in range(2):          # transport-level retries only
            try:
                code, root = self._req("/", retries=1)
                if code != 200 or not root:
                    raise RouterError("router root page unreachable")
                m = SESSION_TOKEN_RE.search(root)
                if not m:
                    if _looks_logged_out(root) or "password" in root.lower():
                        # login page without LuQUID plumbing: fingerprint it
                        raise RouterError(
                            "unrecognized ZTE login page (no _sessionTOKEN) "
                            f"- title={self._title(root)!r} "
                            f"form={FORM_ACTION_RE.search(root) and 'yes' or 'no'}")
                    raise RouterError("no _sessionTOKEN on router root page")
                session_token = m.group(1)
                code, tok_xml = self._req(TOKEN_PAGE, referer=self.base + "/",
                                          retries=1)
                token = re.sub(r"<[^>]+>", "", tok_xml or "").strip()
                if not token:
                    raise RouterError("router did not provide a login token")
                sha = hashlib.sha256(
                    (self.password + token).encode()).hexdigest()
                code, body = self._req("/", referer=self.base + "/", retries=1,
                                       data={
                                           "Username": self.username,
                                           "Password": sha,
                                           "action": "login",
                                           "_sessionTOKEN": session_token,
                                       })
                sid = self._sid_cookie()
                if not sid:
                    # POST answered but no SID -> credentials rejected:
                    # fail NOW, never burn more attempts at the firmware.
                    raise RouterError(
                        "wrong router password (rejected by the router)")
                # prove the session with a real page
                code, page = self._req(
                    PAGE_URL.format(page="ManagDiag_StatusManag_t"),
                    referer=self.base + "/", retries=1)
                if self._looks_logged_out(page):
                    raise RouterError("router session not established")
                self._sid = sid
                self._login_ts = time.time()
                self._last_page = "ManagDiag_StatusManag_t"
                self.logged_in = True
                if not self._discovered:
                    try:
                        self.discover()
                    except Exception:
                        pass      # discovery failure degrades, never blocks
                return
            except RouterError as exc:
                if "wrong router password" in str(exc) or \
                        "unrecognized ZTE login page" in str(exc) or \
                        "no _sessionTOKEN" in str(exc):
                    self.logged_in = False
                    raise              # auth/fingerprint: no blind retries
                last_err = exc
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            time.sleep(0.5)
        self.logged_in = False
        raise RouterError(f"login failed: {last_err}")

    @staticmethod
    def _title(text):
        m = re.search(r"<title[^>]*>([^<]*)</title>", text or "", re.I)
        return (m.group(1).strip() if m else "")[:60]

    def logout(self):
        try:
            self._req("/", retries=1)
        except Exception:
            pass
        finally:
            self.logged_in = False
            self._sid = None
            self._cj.clear()

    def _ensure(self):
        if not self.logged_in or not self._sid:
            self.login()
            return
        # proactive re-login before the 1-2 min idle kill
        if time.time() - self._login_ts >= 85:
            self.login()

    # ----------------------------------------------------------- discovery
    def discover(self, cap=70):
        """Crawl the menu once; classify pages by element-id markers.

        Re-entrancy guard: the crawl is up to 70 sequential page fetches,
        and a slow GPON ONT (~1.5 s/page = 105 s) outlives the 1-2 min idle
        session. The bounce then reaches login(), which calls back into
        discover() because `_discovered` only flips at the END of a crawl -
        so the crawl would nest, restarting from the seed pages each time.
        `_discovered` still marks COMPLETION only, so a crawl that fails
        outright is retried on the next login rather than being skipped.
        """
        if self._discovering:
            return
        self._discovering = True
        try:
            self._crawl(cap)
        finally:
            self._discovering = False

    def _crawl(self, cap):
        seen = set()
        queue = list(SEED_URLS)
        found = 0
        while queue and found < cap:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                code, page = self._req(url, referer=self.base + "/", retries=1)
            except Exception:
                continue
            if code != 200 or not page:
                continue
            if self._looks_logged_out(page):
                self.login()
                try:
                    code, page = self._req(url, referer=self.base + "/",
                                           retries=1)
                except Exception:
                    continue
            found += 1
            for role, markers in ROLE_MARKERS.items():
                if role not in self._pages and any(mk in page for mk in markers):
                    self._pages[role] = url
            for link in NEXT_PAGE_RE.findall(page):
                u = PAGE_URL.format(page=link)
                if u not in seen:
                    queue.append(u)
            for href in HREF_RE.findall(page):
                href = href.strip()
                if not href or href.startswith(("javascript:", "http://",
                                                "https://", "#", "mailto:")):
                    continue
                if re.search(r"\.(css|js|png|gif|jpg|svg|ico|woff2?)(\?|$)",
                             href, re.I):
                    continue
                u = href if href.startswith("/") else "/" + href
                if u not in seen and ("getpage" in u or ".lp" in u
                                      or ".lua" in u or ".html" in u
                                      or "nextpage" in u):
                    queue.append(u)
            time.sleep(0.03)
        self._discovered = True

    def _page_html(self, role):
        self._ensure()
        url = self._pages.get(role)
        if not url:
            if not self._discovered:
                self.discover()
            url = self._pages.get(role)
        if not url:
            return None
        code, page = self._req(url, referer=self.base + "/", retries=2)
        if self._looks_logged_out(page):
            self.login()
            code, page = self._req(url, referer=self.base + "/", retries=2)
        self._last_page = url
        return page

    def _page_endpoints(self, role):
        page = self._page_html(role)
        if not page:
            return None, []
        eps = []
        for m in FORM_ACTION_RE.finditer(page):
            ep = m.group(1).strip()
            if ep and ep not in eps:
                eps.append(ep)
        return page, eps

    def _fetch_endpoint(self, ep, referer_url):
        """Data endpoints answer only for the bound page (SessionTimeout)."""
        url = ep if ep.startswith("/") else "/" + ep
        code, body = self._req(url, referer=self.base + referer_url, retries=1)
        if "SessionTimeout" in body or self._looks_logged_out(body):
            self.login()
            self._req(referer_url, referer=self.base + "/", retries=1)
            code, body = self._req(url, referer=self.base + referer_url,
                                   retries=1)
        if "ajax_response_xml_root" not in body:
            return None
        return body

    # ------------------------------------------------------------- snapshots
    def _collect_devices(self):
        """Merge client lists from every discovered page -> (devices, bytes)."""
        merged = {}
        total_bytes = 0.0
        for role, iface in (("lan", "lan"), ("wlan", "wifi"),
                            ("dhcp", ""), ("topology", "")):
            try:
                page, eps = self._page_endpoints(role)
            except Exception:
                continue
            if not page and not eps:
                continue
            got = []
            if page:
                got += _devices_from_scan(_parse_page(page), iface)
            for ep in eps:
                xml = self._fetch_endpoint(ep, self._pages.get(role, "/"))
                if xml:
                    parsed = parse_luquid(xml)
                    got += _devices_from_xml(parsed, iface)
                    # Rows WITHOUT a mac never become devices (global
                    # counters) - their bytes count here exactly once.
                    # Rows WITH a mac are summed below via dev["rx"]/["tx"],
                    # so adding them here too would double the total.
                    for insts in parsed.values():
                        for inst in insts:
                            if any(_field_of(k)[0] == "mac" and _norm_mac(v)
                                   for k, v in inst.items()):
                                continue
                            for k, v in inst.items():
                                if _field_of(k)[0] == "bytes":
                                    bv = _bytes_value(v)
                                    if bv:
                                        total_bytes += bv[0] + bv[1]
            for dev in got:
                rx = dev.pop("rx", None)
                tx = dev.pop("tx", None)
                if rx is not None:
                    total_bytes += rx + (tx or 0)
                cur = merged.get(dev["mac"])
                if not cur:
                    merged[dev["mac"]] = dev
                else:
                    if not cur.get("ip") and dev.get("ip"):
                        cur["ip"] = dev["ip"]
                    if not cur.get("hostname") and dev.get("hostname"):
                        cur["hostname"] = dev["hostname"]
                    if not cur.get("iface") and dev.get("iface"):
                        cur["iface"] = dev["iface"]
        return list(merged.values()), total_bytes

    def _home_info(self):
        try:
            page, eps = self._page_endpoints("home")
        except Exception:
            return {}
        info = {}
        if page:
            scan = _parse_page(page)
            for _block, sid, text in scan.spans:
                if _INFO_IDS_RE.search(sid or "") and text:
                    info[sid.split(":")[0].split("_")[0]] = text[:80]
        for ep in (eps or []):
            xml = self._fetch_endpoint(ep, self._pages.get("home", "/"))
            if xml:
                for insts in parse_luquid(xml).values():
                    for inst in insts:
                        for k, v in inst.items():
                            if _INFO_IDS_RE.search(k) and v:
                                info.setdefault(k, str(v)[:80])
        return {"data": info} if info else {}

    def _usage_mb(self, counted_bytes):
        """Cumulative MB with peak-hold: client counters drop when a device
        leaves, a real counter reset must not fire regression alerts on it."""
        mb = counted_bytes / (1024.0 * 1024.0)
        if mb < self._usage_peak_mb:
            mb = self._usage_peak_mb
        else:
            self._usage_peak_mb = mb
        return round(mb, 2)

    def current_usage_mb(self):
        return self._usage_peak_mb

    def reset_usage(self):
        self._usage_peak_mb = 0.0

    def snapshot_all(self):
        self._ensure()
        devices, total_bytes = self._collect_devices()
        usage_mb = self._usage_mb(total_bytes)
        out = {
            "all_device": {"data": {"clients": devices}} if devices else {},
            "home_info": self._home_info(),
            # mon_download_flow is MB (see poller.parse_usage); no plan
            # quota on unlimited FibreX -> limitSize stays empty (= None)
            "flow_new": {"mon_download_flow": usage_mb, "limitSize": ""},
            "sys_status": {},
            "network": {},
            "dhcp_data": {},
        }
        return out

    def slow_snapshot(self):
        self._ensure()
        out = {k: {} for k in ("topology", "log", "module_log", "access",
                               "url_filter", "ip_filter_mode", "mac_filter",
                               "parent_control")}
        try:
            page, eps = self._page_endpoints("urlfilter")
            if page:
                data = {}
                for ep in eps:
                    xml = self._fetch_endpoint(
                        ep, self._pages.get("urlfilter", "/"))
                    if xml:
                        data[ep] = parse_luquid(xml)
                if not data:
                    data = {"page": _parse_page(page).rows}
                out["url_filter"] = data
        except Exception:
            pass
        try:
            page, eps = self._page_endpoints("topology")
            if page:
                out["topology"] = {"rows": _parse_page(page).rows}
        except Exception:
            pass
        return out

    # ---------------------------------------------------------- ZLT-only API
    def cmd(self, cmd, method="GET", extra=None, _retried=False):
        raise RouterError(
            "feature not available on this router (ZTE/FibreX) - "
            "an MTN ODU (ZLT) is required")

    def build(self, cmd, method="GET"):
        raise RouterError(
            "feature not available on this router (ZTE/FibreX)")
