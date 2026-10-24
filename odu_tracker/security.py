"""Device risk classification.

Heuristics, no external calls needed at runtime. MAC vendor lookup uses a
small embedded table for the vendors seen in Nigerian home networks plus a
fallback generic mapping by locally-administered bit / empty prefix.
"""
import json
import random
import re
import struct

MAC_RE_SPACED = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$")
MAC_RE_HEX = re.compile(r"^[0-9A-Fa-f]{12}$")

# Relative OUI prefixes (registry granularity varies; this is a compact map
# of common consumer vendors; unknown-prefix devices lose a little score).
OUI = {
    "54:9F:13": "ZLT/Fiberhome ODU",
    "40:22:FB": "ZLT/ComNet ODU",
    "08:57:AD": "ZTE",
    "F0:A5:72": "Unify/Odido CPE",
    "00:1D:BA": "Samsung",
    "C4:8F:B5": "HP",
    "3C:F9:E6": "Intel CORP",
    "28:6A:BA": "Intel",
    "8C:C6:2C": "TP-Link",
    "E0:28:6D": "Infinix",
    "50:C2:75": "Google/Nest",
    "F8:F5:0C": "Tecno",
    "F0:9F:C2": "Ubiquiti",
    "00:24:E8": "Intel/Apple",
    "FC:75:E6": "Chongqing Fusui (Android TV)",
    "AA:BB:CC": "Generic/local test",
}

SUSPICIOUS_HOSTNAME_PATTERNS = [
    (re.compile(r"^(?:[0-9a-fA-F]{4,12}-[0-9a-fA-F]{4,12}-[0-9a-fA-F]{4,12})"), "random-hex hostname"),
    (re.compile(r"\b(iphone|ipad|macbook|android|television|tv|printer|speaker|echo|nest)\b", re.I), None),
    (re.compile(r"\b(kali|parrot|BackBox|pentest|nmap|wlan0mon|airgeddon|hak5|pineapple|evil-twin)\b", re.I),
     "red-flag hostname"),
]


def normalize_mac(s: str):
    if not s:
        return None
    s = s.strip().upper().replace("-", ":")
    if MAC_RE_SPACED.match(s):
        parts = s.split(":")
        return ":".join(p.zfill(2) for p in parts)
    if MAC_RE_HEX.match(s):
        return ":".join(s[i:i + 2] for i in range(0, 12, 2))
    return None


def vendor_for(mac: str):
    prefix = (mac or "")[:8].upper()
    v = OUI.get(prefix)
    if not v:
        try:
            fb1 = int(mac[0:2], 16) & 0b11
            if fb1:  # locally administered => random/spoofed
                return None
        except ValueError:
            return None
    return v


def is_locally_administered(mac: str):
    """True if the MAC's low 2 bits of the first byte indicate random MAC."""
    try:
        v = int(mac[0:2], 16)
    except (ValueError, TypeError):
        return True
    return bool(v & 0b11)


def looks_suspicious_hostname(host: str):
    if not host:
        return None
    for rx, label in SUSPICIOUS_HOSTNAME_PATTERNS:
        if label and rx.search(host):
            return label
        if not label and rx.search(host):
            return None  # ordinary well-known consumer pattern
    if re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){3,6}", host or ""):
        return "mac-like hostname"
    return None


WEIRD_NAMES = [
    "android-adb", "DESKTOP-", "WLAN-AP", "DIRECT-", "PRO-wired", "Dodora",
]


def risk_score(device: dict, context: dict) -> tuple:
    """Return (score 0..1, level, reasons list).

    Calibrated: modern phones randomize MACs by default, so a randomized MAC
    alone is nearly harmless; real danger = red-flag hostname, prior blocks,
    brand-new stranger devices.
    """
    score = 0.0
    reasons = []

    mac = (device.get('mac') or '').upper()
    host = device.get('hostname') or ""

    if context.get("is_new"):
        score += 0.30
        reasons.append("new device just discovered")

    if context.get("blocked_before"):
        score += 0.45
        reasons.append("was previously blocked")

    vendor = vendor_for(mac) or ""
    if not vendor:
        score += 0.05
        reasons.append("unlisted MAC vendor")

    if is_locally_administered(mac):
        score += 0.10
        reasons.append("randomized MAC (normal on phones)")

    lab = looks_suspicious_hostname(host)
    if lab and lab != "random-hex hostname":
        score += 0.40
        reasons.append(f"hostname: {lab}")

    iface = (device.get("iface") or "")
    if iface and iface.lower() not in ("eth", "lan"):
        score += 0.03  # wireless join slightly more interesting than wired

    if context.get("age_hours", 0) < 24 and not context.get("trusted"):
        score += 0.05

    lvl = "low"
    if score >= 0.55:
        lvl = "high"
    elif score >= 0.30:
        lvl = "medium"
    return round(min(1.0, score), 2), lvl, reasons
