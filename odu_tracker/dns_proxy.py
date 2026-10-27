"""DNS proxy: LAN-wide query logging + captive-portal hijack.

Two roles, one listener:

  1. Logging: every LAN client's DNS lookup flows through this app, giving a
     real per-device "visited websites" view without touching the firmware.
  2. Hijack (captive portal): clients whose device has NO active voucher
     claim get DNS answers pointing at this machine instead of the real
     site — their HTTP traffic lands on the voucher-claim portal (portal.py).
     Answered clients are forwarded upstream untouched.

  UDP :53 (was 5353): receive query -> [log] -> deny? answer with portal IP
                       : forward to upstream -> reply.
Non-address types (MX/TXT/SRV...) are always forwarded transparently.
AAAA queries from denied clients get an empty NOERROR answer so only the
IPv4 path (-> portal) is used.

Binding port 53 needs an unprivileged-port raise or a file capability —
bind failures are recorded in DnsProxy.error and surfaced in the UI rather
than failing silently (see /api/portal/status).
"""
import socket
import struct
import threading
import time

from . import store

# reverse / local zones are infrastructure, not "websites"
IGNORE_SUFFIX = (".in-addr.arpa", ".ip6.arpa", ".local", ".lan", ".arpa", ".home")


def parse_query_name(data: bytes, offset: int):
    """Extract the question name at offset (skips compression pointers)."""
    labels = []
    end = offset
    for _ in range(64):
        if end >= len(data):
            break
        l = data[end]
        if l == 0:
            end += 1
            break
        if l & 0xC0:
            end += 2
            break
        labels.append(data[end + 1:end + 1 + l].decode("ascii", "replace"))
        end += 1 + l
    return ".".join(labels), end


def build_portal_reply(query: bytes, qtype: int, portal_ip: str, ttl: int = 60):
    """Synthesize a DNS response for `query`.

    A (1)    -> one answer record with portal_ip (hijack)
    AAAA (28) -> NOERROR with no answers, so clients fall back to IPv4/portal
    """
    if len(query) < 12:
        return None
    _, qend = parse_query_name(query, 12)
    if qend + 4 > len(query):
        return None
    question = query[12:qend + 4]          # name + qtype + qclass
    if qtype == 1:
        answer = (b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, ttl, 4)
                  + socket.inet_aton(portal_ip))
        ancount = 1
    elif qtype == 28:
        answer = b""
        ancount = 0
    else:
        return None
    header = (query[0:2]                       # transaction id
              + struct.pack(">H", 0x8180)      # QR=1 RD=1 RA=1 NOERROR
              + struct.pack(">HHHH", 1, ancount, 0, 0))
    return header + question + answer


def _worker(sock, data, addr, upstream, upstream_port):
    """Per-query forwarder thread — LAN clients never wait on us."""
    reply = None
    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.settimeout(3.0)
    try:
        up.sendto(data, (upstream, upstream_port))
        reply, _ = up.recvfrom(4096)
    except OSError:
        reply = None
    finally:
        up.close()
    if reply:
        try:
            sock.sendto(reply, addr)
        except OSError:
            pass
    else:
        # SERVFAIL so clients fail fast to their secondary DNS
        try:
            if len(data) > 12:
                flags = struct.unpack(">H", data[2:4])[0]
                resp = data[:2] + struct.pack(">H", (flags | 0x0002) & 0xFFDF
                                              | 0x0002 | (flags & 0x0080))
                resp += data[4:12] + b"\x00\x00\x00\x00"
                sock.sendto(resp, addr)
        except OSError:
            pass


class DnsProxy(threading.Thread):
    A_OR_AAAA = (1, 28)

    def __init__(self, port=53, upstream="8.8.8.8", deny=None, portal_ip=None):
        """deny: optional callable(client_ip) -> bool; True = hijack this client.
        portal_ip: the A record handed to denied clients (this machine)."""
        super().__init__(daemon=True, name="odu-dns")
        self.port = port
        self.upstream = upstream
        self.upstream_port = 53
        self.deny = deny
        self.portal_ip = portal_ip
        self._stop_evt = threading.Event()
        self.sock = None
        self.logged = 0
        self.hijacked = 0
        self.error = None       # bind/other failure, surfaced in the UI

    def stop(self):
        self._stop_evt.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", self.port))
        except OSError as exc:
            # port 53/80 need privileges (net.ipv4.ip_unprivileged_port_start)
            self.error = (f"cannot bind UDP {self.port}: {exc} "
                          f"(privileged port — run tools/enable_priv_ports.sh once)")
            return
        while not self._stop_evt.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(data, addr),
                             daemon=True).start()

    def _denied(self, client_ip: str) -> bool:
        if not self.deny or not self.portal_ip:
            return False
        try:
            return bool(self.deny(client_ip))
        except Exception:
            return False

    def handle(self, data: bytes, addr):
        try:
            if len(data) < 12:
                return
            qd = struct.unpack(">H", data[4:6])[0]
            if not qd:
                return
            domain, qend = parse_query_name(data, 12)
            qtype = (struct.unpack(">H", data[qend:qend + 2])[0]
                     if len(data) >= qend + 2 else 0)
            dom = (domain or "").lower()
            interesting = (dom and qtype in self.A_OR_AAAA
                           and not dom.endswith(IGNORE_SUFFIX)
                           and not dom.startswith(("192.168.", "10.", "172.16.")))
            if interesting:
                store.add_site(int(time.time()), addr[0], None, dom)
                self.logged += 1
                if self._denied(addr[0]):
                    reply = build_portal_reply(data, qtype, self.portal_ip)
                    if reply:
                        self.hijacked += 1
                        try:
                            self.sock.sendto(reply, addr)
                        except OSError:
                            pass
                        return
            _worker(self.sock, data, addr, self.upstream, self.upstream_port)
        except Exception:
            pass


_singleton = None


def start_proxy(cfg, deny=None, portal_ip=None) -> "DnsProxy":
    global _singleton
    if _singleton and _singleton.is_alive():
        return _singleton
    _singleton = DnsProxy(port=int(cfg.get("dns_proxy_port", 53)),
                          upstream=cfg.get("dns_upstream", "8.8.8.8"),
                          deny=deny, portal_ip=portal_ip)
    _singleton.start()
    return _singleton
