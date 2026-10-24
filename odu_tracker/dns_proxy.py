"""Optional DNS logging proxy.

Enable it (one click after enabling router DNS auto-config) and every LAN
client's DNS lookup flows through this app — giving a real per-device
"visited websites" view without touching the firmware.

  UDP 5353: receive query -> log (ts, client_ip, domain) for A/AAAA lookups
            -> forward to upstream -> reply.
Non-address types (MX/TXT/SRV...) are forwarded transparently, never blocked.
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

    def __init__(self, port=5353, upstream="8.8.8.8"):
        super().__init__(daemon=True, name="odu-dnslog")
        self.port = port
        self.upstream = upstream
        self.upstream_port = 53
        self._stop_evt = threading.Event()
        self.sock = None
        self.logged = 0

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
        except OSError:
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
            if dom and qtype in self.A_OR_AAAA and not dom.endswith(IGNORE_SUFFIX) \
               and not dom.startswith(("192.168.", "10.", "172.16.")):
                store.add_site(int(time.time()), addr[0], None, dom)
                self.logged += 1
            _worker(self.sock, data, addr, self.upstream, self.upstream_port)
        except Exception:
            pass


_singleton = None


def start_proxy(cfg) -> "DnsProxy":
    global _singleton
    if _singleton and _singleton.is_alive():
        return _singleton
    _singleton = DnsProxy(port=int(cfg.get("dns_proxy_port", 5353)),
                          upstream=cfg.get("dns_upstream", "8.8.8.8"))
    _singleton.start()
    return _singleton
