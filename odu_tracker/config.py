r"""Local configuration + SQLite layout bootstrap.

Everything app-related lives under a per-user data dir:
  Linux:   ~/.local/share/odu-tracker    (falls back to ~/.odu-tracker)
  Windows: %APPDATA%\OduTracker
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


def app_dir() -> Path:
    """Resolve (and create) the per-user application data directory."""
    global APP_DIR
    if APP_DIR is not None:
        return APP_DIR
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = Path(base) / "OduTracker"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", "")) if os.environ.get("XDG_DATA_HOME") else None
        if d is None:
            d = Path.home() / ".local" / "share" / "odu-tracker"
    d.mkdir(parents=True, exist_ok=True)
    APP_DIR = d
    return d


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
    "dns_proxy_port": 5353,
    "dns_upstream": "8.8.8.8",
    "nickname": "",                # friendly name for this machine's owner, unused now
}


class Config:
    """Small JSON-backed settings store with an obfuscated password."""

    def __init__(self):
        self._cfg = DEFAULTS.copy()
        self._cfg.update(_load_config_file())
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
