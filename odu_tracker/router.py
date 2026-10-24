"""Client for the MTN ODU (ZLT X28 / ZLT T300 class) web API.

Reverse-engineered from the device's bundled Vue SPA:
  endpoint : POST http://<ip>/cgi-bin/http.cgi
  body     : JSON  {"cmd": <int>, "method": "GET"|"POST", "sessionId": <str>,
                    "token": <str> (only for method=POST)}
Login handshake (exactly as the web UI does):
  1. cmd 232 GET            -> pre-login "token" (also reports lock state)
  2. cmd 100 POST           -> {username:"admin", passwd: sha256(token+password),
                               isAutoUpgrade:"1", isCheckPasswd:"1"}
                               response carries the session token ("sessionId")
  3. cmd 233 GET (resetLoginTime) -> refreshed "token" for subsequent POSTs
All POST requests include the latest "token"; each POST response is followed by
a token-refresh call (that is what the SPA's axios interceptor does).
"""
import hashlib
import json
import secrets
import threading
import time
import uuid

import requests

# --- command ids -------------------------------------------------------------
CMD_WWANUP_HTTPD = 104
CMD_GET_BASIC = 80           # unauthenticated device info
CMD_LOGIN = 100
CMD_LOGOUT = 101
CMD_SYS_STATUS = 113
CMD_DHCP_FILTER_DATA = 3
CMD_DEVICE_TIME = 11
CMD_GET_SMS_SETTING = 16
CMD_SYSTEM_LOG = 17
CMD_PORT_FILTER = 21
CMD_MAC_FILTER_DATA = 23
CMD_GET_IP_BAND = 24
CMD_URL_FILTER = 26
CMD_PORT_FORWARD = 27
CMD_IP_FILTER_MODE = 28
CMD_URL_FILTER_MODE = 29
CMD_IPPORT_DATA = 32
CMD_BASIC_INFO = 80
CMD_ARP_BIND = 83
CMD_RESET_DEVICE = 112
CMD_RESERVED_IP = 115
CMD_GET_IPINFO = 133
CMD_PIN_STATUS = 134
CMD_STATIC_ROUTE = 164
CMD_GET_RUNTIME = 207
CMD_DHCP_INFO = 208
CMD_GET_NETWORK = 218
CMD_ALL_DEVICE = 223
CMD_WIFI_NUM_A = 224
CMD_WIFI_NUM_B = 225
CMD_GET_ACCESS = 230             # alias in app.js (230/231 switch)
CMD_GET_ACCESS_CTRL = 345        # LAN/WAN access control lists
CMD_LOGIN_LOCK = 232
CMD_RESET_LOGIN_TIME = 233
CMD_TELNET = 234
CMD_MODULE_LOG = 236
CMD_ADB = 237
CMD_EXPORT_LOG = 241
CMD_SIM_SWITCH = 246
CMD_ESIM_DATA = 247
CMD_APN_LIST = 248
CMD_TIME_RESTART = 263
CMD_DDOS = 275
CMD_TOPOLOGY = 315
CMD_LOG_INFO = 322
CMD_ABNORM_REBOOT = 328
CMD_UPNP = 333
CMD_FLOW = 337               # monthly flow + limits
CMD_FLOW_NEW = 355           # newer variant (sys_used_flow / usr_total_flow)
CMD_PARENT_RIGHT = 385
CMD_PARENT_SET = 391
CMD_PARENT_CTRL = 397
CMD_HOME_INFO = 402
CMD_WIFI_ADV = 410
CMD_POWER = 411
CMD_PROFILE_LIST = 542
CMD_SYS_MODE = 548
CMD_BATTERY = 1005


class RouterError(Exception):
    pass


class NotAuthorized(RouterError):
    pass


class LoginLocked(RouterError):
    def __init__(self, seconds_left=None):
        self.seconds_left = seconds_left or 0
        msg = f"Router login locked for {seconds_left}s" if seconds_left else "Router login locked"
        super().__init__(msg)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _ota_session_id() -> str:
    # The SPA builds sessionId = md5(random)+md5(random)
    return _md5(str(uuid.uuid4())) + _md5(str(uuid.uuid4()))


class OduRouter:
    """Thread-safe client for one router. Re-logins automatically on NO_AUTH."""

    def __init__(self, ip="192.168.0.1", username="admin", password="", timeout=10):
        self.ip = ip
        self.username = username
        self.password = password
        self.base = f"http://{ip}/cgi-bin/http.cgi"
        self.timeout = timeout
        self.session_id = ""
        self.token = ""
        self.user_level = ""
        self._lock = threading.RLock()
        self._http = requests.Session()
        self._http.headers.update({"Content-Type": "application/json"})
        self.logged_in = False

    # ------------------------------------------------------------- transport
    def _post(self, payload: dict):
        with self._lock:
            if payload.get("method") == "POST":
                if self.token:
                    payload["token" if "token" not in payload else "token"] = self.token
            body = json.dumps(payload, ensure_ascii=False)
            resp = self._http.post(self.base, data=body, timeout=self.timeout)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                raise RouterError(f"Non-JSON response: {resp.text[:120]!r}")
            # SPA behavior: after every POST (except cmd 233) refresh the token.
            if (
                payload.get("method") == "POST"
                and payload.get("cmd") not in (CMD_RESET_LOGIN_TIME, CMD_LOGIN)
                and data.get("sessionId") is not None
            ):
                self._maybe_refresh_token(data.get("sessionId") or self.session_id)
            return data

    def _maybe_refresh_token(self, sid: str):
        try:
            r = self._http.post(
                self.base,
                data=json.dumps({"cmd": CMD_RESET_LOGIN_TIME, "method": "GET", "sessionId": sid}),
                timeout=self.timeout,
            )
            d = r.json()
            tok = d.get("token")
            if tok:
                self.token = tok
        except Exception:
            pass

    # --------------------------------------------------------------- auth
    def login(self, password: str = None) -> dict:
        """Full login handshake; returns the final cmd-100 dict."""
        password = password if password is not None else self.password
        with self._lock:
            # 1) wake httpd + fetch pre-login token
            self._silent(self.build(CMD_WWANUP_HTTPD, "GET"))
            lock = self.build(CMD_LOGIN_LOCK, "GET")
            token = lock.get("token") or ""
            if not token:
                raise RouterError("Router did not provide a login token (cmd 232)")
            netx = lock.get("netx_login_time")
            if lock.get("login_locked") == "1" or (netx and int(netx) > 0 and lock.get("login_locked")):
                pass  # keep soft: SPA warns only
            # 2) login POST (session id is client-generated)
            sid = _ota_session_id()
            passwd_hash = hashlib.sha256((token + password).encode()).hexdigest()
            payload = {
                "sessionId": sid,
                "username": self.username,
                "passwd": passwd_hash,
                "isAutoUpgrade": "1",
                "isCheckPasswd": "1",
                "method": "POST",
                "cmd": CMD_LOGIN,
            }
            data = self._post(payload)
            if data.get("login_fail") == "fail":
                times = data.get("login_times")
                raise RouterError(f"Wrong password (attempts left: {times})")
            if data.get("login_fail2") == "fail" or data.get("login_locked"):
                raise LoginLocked(data.get("login_time"))
            new_sid = data.get("sessionId") or sid
            self.session_id = new_sid
            self.user_level = str(data.get("user_level") or "")
            self.logged_in = True
            # keep token: refresh once like dashboard does
            self._maybe_refresh_token(new_sid)
            return data

    def build(self, cmd: int, method: str) -> dict:
        """Build + send a command; the raw dict is returned."""
        if method == "POST":
            data = {"cmd": cmd, "method": "POST", "sessionId": self.session_id}
            if self.token:
                data["token"] = self.token
            return self._post(data)
        data = {"cmd": cmd, "method": "GET", "sessionId": self.session_id}
        return self._post(data)

    def cmd(self, cmd: int, method="GET", extra=None, _retried=False):
        """Send a command; auto re-login when authorization lapses."""
        payload = {"cmd": cmd, "method": method, "sessionId": self.session_id or ""}
        if extra:
            payload.update(extra)
        data = self._post(payload)
        if data.get("message") == "NO_AUTH":
            try:
                self.login()
            except Exception as exc:  # surface a clean error to callers
                raise NotAuthorized(f"Re-login failed: {exc}") from exc
            if _retried:
                return data
            payload = dict(payload, sessionId=self.session_id, token=self.token)
            return self.cmd(cmd, method, extra, _retried=True)
        return data

    def logout(self):
        try:
            if self.logged_in:
                self.build(CMD_LOGOUT, "POST")
        finally:
            self.logged_in = False
            self.session_id = ""

    # --------------------------------------------------------- convenience
    def _silent(self, *args, **kwargs):
        try:
            return self.build(*args, **kwargs)
        except Exception:
            return {}

    # -- raw fetchers used by the poller (all tolerant of failures) ---------
    def snapshot_all(self):      # one merged pass of everything we care about
        wanted = {
            "home_info": CMD_HOME_INFO,
            "all_device": CMD_ALL_DEVICE,
            "dhcp_info": CMD_DHCP_INFO,
            "dhcp_data": CMD_DHCP_FILTER_DATA,
            "sys_status": CMD_SYS_STATUS,
            "network": CMD_GET_NETWORK,
            "battery": CMD_BATTERY,
            "flow": CMD_FLOW,
            "flow_new": CMD_FLOW_NEW,
            "runtime": CMD_GET_RUNTIME,
        }
        out = {}
        for name, c in wanted.items():
            try:
                out[name] = self.cmd(c, "GET")
            except Exception as exc:
                out[name] = {"success": False, "error": str(exc)}
        return out

    def slow_snapshot(self):      # heavier data, polled less often
        wanted = {
            "topology": CMD_TOPOLOGY,
            "log": CMD_SYSTEM_LOG,
            "module_log": CMD_MODULE_LOG,
            "access": CMD_GET_ACCESS_CTRL,
            "url_filter": CMD_URL_FILTER,
            "ip_filter_mode": CMD_IP_FILTER_MODE,
            "mac_filter": CMD_MAC_FILTER_DATA,
            "parent_control": CMD_PARENT_CTRL,
        }
        out = {}
        for name, c in wanted.items():
            try:
                out[name] = self.cmd(c, "GET")
            except Exception as exc:
                out[name] = {"success": False, "error": str(exc)}
        return out
