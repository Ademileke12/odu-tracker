"""Internet speed test through the ODU link.

Uses Cloudflare's free speed endpoints (no key, global anycast, present in
Nigerian IXPs):

  download : GET  https://speed.cloudflare.com/__down?bytes=<N>
  upload   : POST https://speed.cloudflare.com/__up   (body = N bytes)
  latency  : TTFB of GET __down?bytes=0

Everything is user-triggered from the dashboard (NEVER automatic):
it consumes real data on the MTN plan, roughly size_mb (down) + up_mb (up).

Runs in a worker thread; the web layer exposes start()/status().
"""
import os
import random
import threading
import time

import requests

BASE = "https://speed.cloudflare.com"
UA = "ODU-Tracker/0.1 (+local monitoring tool)"


def _measure_latency(samples=3):
    best = None
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            requests.get(f"{BASE}/__down?bytes=0", timeout=6,
                         headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        except requests.RequestException:
            continue
        dt = (time.perf_counter() - t0) * 1000.0
        best = dt if best is None else min(best, dt)
    return best


def _measure_download(size_bytes, budget_s=20.0):
    """Returns (mbps, bytes_read, seconds). Stops at size OR budget seconds."""
    t0 = time.perf_counter()
    got = 0
    ttfb = None
    try:
        with requests.get(f"{BASE}/__down?bytes={size_bytes}", stream=True, timeout=(10, 25),
                          headers={"User-Agent": UA, "Cache-Control": "no-cache"}) as r:
            r.raise_for_status()
            first_chunk = True
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if first_chunk:
                    ttfb = time.perf_counter() - t0
                    first_chunk = False
                got += len(chunk)
                if time.perf_counter() - t0 > budget_s or got >= size_bytes:
                    break
    except requests.RequestException:
        pass
    dt = max(1e-6, time.perf_counter() - t0)
    mbps = (got * 8) / dt / 1e6          # 1 MB = 8 Mbit
    return mbps, got, dt, ttfb


def _measure_upload(size_bytes, budget_s=20.0):
    payload = os.urandom(min(size_bytes, 2 * 1024 * 1024))
    t0 = time.perf_counter()
    sent = 0
    try:
        r = requests.post(f"{BASE}/__up", data=payload, timeout=(10, 30), headers={"User-Agent": UA})
        r.raise_for_status()
        sent = len(payload)
    except requests.RequestException:
        pass
    dt = max(1e-6, time.perf_counter() - t0)
    mbps = (sent * 8) / dt / 1e6
    return mbps, sent, dt


def run_speed_test(size_mb=6, up_mb=1, progress=None):
    """Blocking call: performs the test, returns a result dict."""
    result = {"ts": int(time.time()), "down_mbps": None, "up_mbps": None,
              "ping_ms": None, "bytes_used": size_mb * 1024 * 1024 + up_mb * 1024 * 1024,
              "error": None}

    if callable(progress):
        progress("latency")
    ping = _measure_latency()
    result["ping_ms"] = round(ping, 1) if ping else None

    if callable(progress):
        progress("download")
    down, got, dt, _ = _measure_download(size_mb * 1024 * 1024)
    result["down_mbps"] = round(down, 2) if got else None
    result["bytes_used"] = got + (result["bytes_used"] - size_mb * 1024 * 1024)

    if callable(progress):
        progress("upload")
    up, sent, _ = _measure_upload(up_mb * 1024 * 1024)
    result["up_mbps"] = round(up, 2) if sent else None
    result["bytes_used"] = got + sent

    return result


class SpeedTestRunner:
    """Background runner + status holder (one test at a time)."""

    def __init__(self):
        self.state = {"running": False, "phase": None, "last": None}
        self._lock = threading.Lock()
        self._thread = None

    def start(self, size_mb=6, up_mb=1):
        with self._lock:
            if self.state["running"]:
                return {"ok": False, "error": "test already running"}
            self.state = {"running": True, "phase": "starting", "last": self.state["last"]}

        def work():
            def progress(phase):
                self.state["phase"] = phase
            try:
                res = run_speed_test(size_mb=size_mb, up_mb=up_mb, progress=progress)
            except Exception as exc:   # never blow up the server
                res = {"ts": int(time.time()), "down_mbps": None, "up_mbps": None,
                       "ping_ms": None, "bytes_used": 0, "error": str(exc)}
            with self._lock:
                self.state = {"running": False, "phase": None, "last": res}

        self._thread = threading.Thread(target=work, name="odu-speedtest", daemon=True)
        self._thread.start()
        return {"ok": True}

    def status(self):
        with self._lock:
            return dict(self.state)
