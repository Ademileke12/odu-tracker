"""Optional DNS logging proxy.

Enable it, then set the router's DHCP DNS to this PC's LAN IP (or set it on
each client). Every DNS query made by a LAN client is logged here, giving a
"visited websites" timeline per device without touching the router firmware.

Runs a tiny UDP listener that:
  receives a DNS query      -> log (ts, client_ip, domain)
  forwards to upstream DNS  -> replies to the client normally.
"""
import socket
import struct
import threading
import time

from . import store


def parse_query_name(data: bytes, offset: int):
    """Extract the question name at offset, carefully skipping compression."""
    labels = []
    jumped = False
    end = offset
    for _ in range(64):
        l = data[end]
        if l == 0:
            end += 1
            break
        if l & 0xC0 == 0xC0:
            end += 2
            jumped = True
            break
        labels.append(data[end + 1:end + 1 + l].decode("ascii", "replace"))
        end += 1 + l
    return ".".join(labels), end


class DnsProxy(threading.Thread):
    def __init__(self, port=5353, upstream="8.8.8.8"):
        super().__init__(daemon=True, name="odu-dnslog")
        self.port = port
        self.upstream = upstream
        self._stop = threading.Event()
        self.sock = None
        self.qtype_names = {1: "A", 28: "AAAA", 5: "CNAME", 2: "NS", 16: "TXT"}
        self._skipped = 0

    def stop(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        try:
            self.sock.bind(("0.0.0.0", self.port))
        except OSError:
            return
        sessions = {}
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.handle(data, addr, sessions)
            except Exception:
                pass

    def handle(self, data: bytes, addr, sessions):
        if len(data) < 12:
            return
        tid, flags, qd = struct.unpack(">HHH", data[:6])
        if not qd:
            return
        domain, qend = parse_query_name(data, 12)
        qtype = struct.unpack(">H", data[qend:qend + 2])[0] if len(data) >= qend + 2 else 0
        if not domain:
            return
        # Only log A/AAAA lookups (site visits), skip reverse lookups
        if qtype not in (1, 28):
            self._skipped += 1
            kind = self.qtype_names.get(qtype, f"t{qtype}")
            if kind in ("TXT", "NS", "CNAME"):
                return
        store.add_site(int(time.time()), addr[0], None, domain.lower())
        upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        upstream.settimeout(3.0)
        try:
            upstream.sendto(data, (self.upstream, 53))
            resp, _ = upstream.recvfrom(4096)
            self.sock.sendto(resp, addr)
        finally:
            upstream.close()


_singleton = None


def start_proxy(cfg) -> "DnsProxy":
    global _singleton
    if _singleton and _singleton.is_alive():
        return _singleton
    _singleton = DnsProxy(port=int(cfg.get("dns_proxy_port", 5353)),
                          upstream=cfg.get("dns_upstream", "8.8.8.8"))
    _singleton.start()
    return _singleton
