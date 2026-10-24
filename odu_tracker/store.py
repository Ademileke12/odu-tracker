"""SQLite persistence: devices, presence samples, usage samples, events, sites."""
import datetime
import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import db_path

_lock = threading.RLock()
_conn: sqlite3.Connection = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
    mac TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    first_ip TEXT, last_ip TEXT,
    first_iface TEXT, last_iface TEXT,
    last_hostname TEXT,
    vendor TEXT,
    trusted INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0,
    blocked_at INTEGER,
    riskscore REAL DEFAULT 0,
    risklevel TEXT DEFAULT 'unknown',
    riskreasons TEXT DEFAULT '[]',
    raw TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mac TEXT, ip TEXT, iface TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_mac ON snapshots(mac);

CREATE TABLE IF NOT EXISTS usage_samples(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    used REAL, unit TEXT,
    total REAL, raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_samples(ts);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    mac TEXT, ip TEXT,
    kind TEXT NOT NULL,
    level TEXT DEFAULT 'info',
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);

CREATE TABLE IF NOT EXISTS sites(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    client_ip TEXT, mac TEXT, domain TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_ts ON sites(ts);
CREATE INDEX IF NOT EXISTS idx_sites_domain ON sites(domain);

CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT, title TEXT, detail TEXT,
    read INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings_data(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS raw_cache(name TEXT PRIMARY KEY, ts INTEGER, body TEXT);

CREATE TABLE IF NOT EXISTS speed_tests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    down_mbps REAL, up_mbps REAL, ping_ms REAL,
    bytes_used INTEGER
);
CREATE INDEX IF NOT EXISTS idx_speed_ts ON speed_tests(ts);
"""


def init():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(db_path()), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        with _lock:
            _conn.executescript(SCHEMA)
            _conn.commit()
    return _conn


def q(sql, args=(), fetch="all"):
    init()
    with _lock:
        cur = _conn.execute(sql, args)
        if fetch == "all":
            return [dict(r) for r in cur.fetchall()]
        if fetch == "one":
            return dict(cur.fetchone() or {})
        return cur.lastrowid


def exec(sql, args=()):
    init()
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur.lastrowid, cur.rowcount


def many_exec(statements):
    """Run a list of (sql, args) inside one transaction."""
    init()
    with _lock:
        for sql, args in statements:
            _conn.execute(sql, args)
        _conn.commit()


# ---------------------------------------------------------------- helpers
def now() -> int:
    return int(time.time())


def day_start(ts=None) -> int:
    d = datetime.datetime.fromtimestamp(ts or now())
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


# ---------------------------------------------------------------- devices
def upsert_device(rec: dict):
    """rec: {mac, ip, iface, hostname, vendor, ts, raw}"""
    mac = rec["mac"]
    existing = q("SELECT * FROM devices WHERE mac=?", (mac,), fetch="one")
    ts = rec.get("ts") or now()
    if not existing:
        exec(
            """INSERT INTO devices(mac, first_seen, last_seen, first_ip, last_ip,
                                  first_iface, last_iface, last_hostname, vendor, raw)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (mac, ts, ts, rec.get("ip"), rec.get("ip"), rec.get("iface"), rec.get("iface"),
             rec.get("hostname"), rec.get("vendor") or "", json.dumps(rec.get("raw") or {})),
        )
        # new-device event
        exec("INSERT INTO events(ts, mac, ip, kind, level, detail) VALUES(?,?,?,?,?,?)",
             (ts, mac, rec.get("ip"), "new_device", "warning",
              f"First seen: {rec.get('hostname') or '(no hostname)'} @ {rec.get('ip')}"))
        return True  # freshly created
    # seen again: update last_* fields
    exec(
        """UPDATE devices SET last_seen=?, last_ip=COALESCE(?,last_ip),
            last_iface=COALESCE(?,last_iface),
            last_hostname=COALESCE(?,last_hostname),
            raw=? WHERE mac=?""",
        (ts, rec.get("ip"), rec.get("iface"), rec.get("hostname"),
         json.dumps(rec.get("raw") or {}), mac),
    )
    ip_changed = existing["last_ip"] != rec.get("ip")
    if ip_changed and rec.get("ip"):
        exec("INSERT INTO events(ts, mac, ip, kind, level, detail) VALUES(?,?,?,?,?,?)",
             (ts, mac, rec.get("ip"), "ip_change", "info",
              f"IP changed {existing['last_ip']} -> {rec.get('ip')}"))
    return False


def mark_offline(mac):
    exec("INSERT INTO events(ts, mac, ip, kind, level, detail) VALUES(?,?,?,?,?,?)",
         (now(), mac, None, "leave", "info", "Device no longer present"))


def set_trust(mac, trusted: bool):
    exec("UPDATE devices SET trusted=? WHERE mac=?", (1 if trusted else 0, mac))


def set_blocked(mac, blocked: bool):
    exec("UPDATE devices SET blocked=?, blocked_at=? WHERE mac=?",
         (1 if blocked else 0, now() if blocked else None, mac))


def online_macs(window=180):
    ts = now() - window
    rows = q("SELECT DISTINCT mac FROM snapshots WHERE ts>=?", (ts,))
    return {r["mac"] for r in rows}


# ---------------------------------------------------------------- alerts
def add_alert(level, title, detail=""):
    exec("INSERT INTO alerts(ts, level, title, detail) VALUES(?,?,?,?)",
         (now(), level, title, detail))


# ---------------------------------------------------------------- usage
def add_usage_sample(ts, used, unit, total):
    exec("INSERT INTO usage_samples(ts, used, unit, total) VALUES(?,?,?,?)",
         (ts, used, unit, total))


def usage_series(hours=24):
    since = now() - hours * 3600
    return q("SELECT ts, used, unit FROM usage_samples WHERE ts>=? ORDER BY ts", (since,))


def current_usage():
    r = q("SELECT ts, used, unit, total FROM usage_samples ORDER BY ts DESC LIMIT 1", fetch="one")
    return r


# ---------------------------------------------------------------- sites (dns)
def add_site(ts, client_ip, mac, domain):
    exec("INSERT INTO sites(ts, client_ip, mac, domain) VALUES(?,?,?,?)",
         (ts, client_ip, mac, domain))


def site_summary(hours=24, limit=200):
    since = now() - hours * 3600
    return q(
        """SELECT domain, COUNT(*) hits, COUNT(DISTINCT client_ip) clients,
                  MIN(ts) first_seen, MAX(ts) last_seen
           FROM sites WHERE ts>=? GROUP BY domain ORDER BY hits DESC LIMIT ?""",
        (since, limit))


def site_client_summary(hours=24, limit=100):
    since = now() - hours * 3600
    return q(
        """SELECT client_ip, COUNT(*) hits, COUNT(DISTINCT domain) domains
           FROM sites WHERE ts>=? GROUP BY client_ip ORDER BY hits DESC LIMIT ?""",
        (since, limit))


# ---------------------------------------------------------------- events
def recent_events(limit=100, kind=None):
    if kind:
        return q("SELECT * FROM events WHERE kind=? ORDER BY ts DESC LIMIT ?", (kind, limit))
    return q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))


def recent_alerts(limit=100):
    return q("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------- speed tests
def add_speed_test(res: dict):
    exec("INSERT INTO speed_tests(ts, down_mbps, up_mbps, ping_ms, bytes_used) VALUES(?,?,?,?,?)",
         (res.get("ts") or now(), res.get("down_mbps"), res.get("up_mbps"),
          res.get("ping_ms"), res.get("bytes_used")))


def speed_history(limit=30):
    rows = q("SELECT ts, down_mbps, up_mbps, ping_ms, bytes_used FROM speed_tests "
             "ORDER BY ts DESC LIMIT ?", (limit,))
    vals = [r["down_mbps"] for r in rows if r["down_mbps"]]
    ups = [r["up_mbps"] for r in rows if r["up_mbps"]]
    return {
        "history": rows,
        "avg_down": round(sum(vals) / len(vals), 2) if vals else None,
        "avg_up": round(sum(ups) / len(ups), 2) if ups else None,
        "count": len(rows),
    }


# ---------------------------------------------------------------- raw cache
def cache_raw(name, body):
    exec("INSERT INTO raw_cache(name, ts, body) VALUES(?,?,?) "
         "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, body=excluded.body",
         (name, now(), json.dumps(body)))
