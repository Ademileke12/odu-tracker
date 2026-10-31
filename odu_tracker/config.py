r"""Local configuration + SQLite layout bootstrap.

Everything app-related lives under a per-user data dir:
  Linux:   ~/.local/share/odu-tracker    (falls back to ~/.odu-tracker)
  Windows: %APPDATA%\OduTracker
  Android: <app-private files dir>/odu-tracker   (see on_android())

Android gotcha (the v1.3.0 boot crash): the platform exports HOME=/data —
that's a mount POINT, not a writable directory for an app UID — so the
historic `Path.home()/.local/share/...` fallback resolved to
/data/.local/share/odu-tracker, mkdir() failed, and the engine died before
the dashboard ever answered. p4a exports ANDROID_PRIVATE (the app's own
files dir) for every process; that is the only trustworthy base there.
"""
import base64
import binascii
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

APP_DIR = None


def on_android() -> bool:
    """True inside a python-for-android app process.

    p4a's bootstrap exports ANDROID_ARGUMENT/ANDROID_PRIVATE for every app
    process — that's a more reliable signal than sys.platform, which is
    plain "linux" on devices.
    """
    return bool(os.environ.get("ANDROID_ARGUMENT")
                or os.environ.get("ANDROID_PRIVATE")
                or os.environ.get("ANDROID_APP_PATHS"))


def _try_dir(d: Path):
    """mkdir -p + a writability probe; None when the dir is unusable."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".writable-probe"
        probe.touch()
        probe.unlink()
        return d
    except OSError:
        return None


def app_dir() -> Path:
    """Resolve (and create) the per-user application data directory.

    Candidates are tried in order and the first WRITABLE one wins — an
    unwritable home (Android's /data, a read-only NFS home, ...) must
    degrade to the next location, never crash the app.
    """
    global APP_DIR
    if APP_DIR is not None:
        return APP_DIR
    candidates = []
    if on_android():
        # App-private storage, the only place an app UID may always write.
        priv = os.environ.get("ANDROID_PRIVATE") or os.environ.get("ANDROID_ARGUMENT")
        if priv:
            candidates.append(Path(priv) / "odu-tracker")
        # Last resort: whatever working dir the bootstrap left us in.
        candidates.append(Path.cwd() / "odu-tracker")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        candidates.append(Path(base) / "OduTracker")
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            candidates.append(Path(xdg) / "odu-tracker")
        candidates.append(Path.home() / ".local" / "share" / "odu-tracker")
    tried = []
    for cand in candidates:
        d = _try_dir(cand)
        if d is not None:
            APP_DIR = d
            return d
        tried.append(str(cand))
    raise OSError("no writable app data dir (tried: "
                  + ", ".join(tried) + ")")


CONFIG_PATH = None
KEY_PATH = None
DB_PATH = None


def _paths():
    root = app_dir()
    return root / "config.json", root / ".key", root / "data.db"


def _load_config_file() -> dict:
    cfg_path, _, _ = _paths()
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_config_file(cfg: dict):
    cfg_path, _, _ = _paths()
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------- secret key
def _machine_key() -> bytes:
    """A stable per-machine secret used to obfuscate the stored password."""
    _, key_path, _ = _paths()
    if key_path.exists():
        try:
            return base64.b64decode(key_path.read_bytes(), validate=True)
        except (binascii.Error, OSError):
            pass
    key = secrets.token_bytes(32)
    key_path.write_bytes(base64.b64encode(key))
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


# ------------------------------------------------------------------ settings
DEFAULTS = {
    "router_ip": "192.168.0.1",
    "router_username": "admin",
    "poll_interval": 15,           # seconds between fast polls
    "slow_interval": 300,          # seconds between slow polls (topology/logs)
    "dns_proxy_enabled": False,
    "dns_proxy_port": 53,            # real LAN DNS interception (was 5353)
    "dns_upstream": "8.8.8.8",
    "portal_port": 80,               # captive-portal claim page
    "portal_enforced": False,        # hijack only when the admin arms it
    "nickname": "",                # friendly name for this machine's owner, unused now
}


class Config:
    """Small JSON-backed settings store with an obfuscated password."""

    def __init__(self):
        self._cfg = DEFAULTS.copy()
        loaded = _load_config_file()
        # migrate: DNS moved 5353 -> 53 (real interception); an explicitly
        # stored 5353 is the old default, not a user choice.
        if int(loaded.get("dns_proxy_port") or 0) == 5353:
            loaded["dns_proxy_port"] = 53
        self._cfg.update(loaded)
        self._cfg.setdefault("setup_done", False)

    # -- generic keys ------------------------------------------------------
    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def set(self, key, value, persist=True):
        self._cfg[key] = value
        if persist:
            self._save()

    def _save(self):
        cfg = {k: v for k, v in self._cfg.items()}
        _save_config_file(cfg)

    # -- password (obfuscated, not cryptographically strong - local only) --
    @property
    def password(self) -> str:
        enc = self._cfg.get("__pw__")
        if not enc:
            return ""
        key = _machine_key()
        try:
            return _xor(base64.b64decode(enc), key).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""

    @password.setter
    def password(self, value: str):
        if value:
            enc = base64.b64encode(_xor(value.encode("utf-8"), _machine_key())).decode()
            self._cfg["__pw__"] = enc
        else:
            self._cfg["__pw__"] = ""
        self._save()

    @property
    def has_password(self) -> bool:
        return bool(self._cfg.get("__pw__"))

    # -- app UI session secret (python-side signing key) --------------------
    @property
    def session_secret(self) -> str:
        s = self._cfg.get("__session_secret__")
        if not s:
            s = secrets.token_hex(32)
            self._cfg["__session_secret__"] = s
            self._save()
        return s


def db_path() -> Path:
    _, _, p = _paths()
    return p
