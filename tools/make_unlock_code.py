#!/usr/bin/env python3
"""Mint an unlock code for the paid Voucher feature — developer tool.

A paying customer sends you their DEVICE CODE (shown in the locked
Vouchers tab, looks like `ODU-4F2A-91C3-7B60`). You run this from the
repo and give them back the matching UNLOCK CODE. Never ship this flow
as part of the app; the app only verifies codes.

    python3 tools/make_unlock_code.py ODU-4F2A-91C3-7B60
    python3 tools/make_unlock_code.py --here      # this machine (testing)

The secret lives in odu_tracker/license.py (it must, because the app
verifies codes offline). Rotate it there for a future release only if it
ever leaks — rotating invalidates every code issued before the rotation.
"""
import argparse
import hashlib
import hmac
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve()
                       .parents[1]))

from odu_tracker import license as lic


def mint(device_code: str) -> str:
    """Unlock code for a given device code (recomputes it locally)."""
    import re
    dev = str(device_code or "").strip().upper()
    if not re.fullmatch(r"ODU-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}", dev):
        raise SystemExit(f"not a device code: {device_code!r} "
                         "(expected ODU-XXXX-XXXX-XXXX)")
    digest = hmac.new(
        lic._secret(), b"odu-tracker/unlock-v1" + dev.encode("utf-8"),
        hashlib.sha256).digest()
    return lic._format(digest[:8].hex().upper())


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("device_code", nargs="?",
                    help="customer device code, e.g. ODU-4F2A-91C3-7B60")
    ap.add_argument("--here", action="store_true",
                    help="print this machine's device code + unlock code")
    args = ap.parse_args()

    if args.here:
        dev = lic.device_code()
        print(f"device code : {dev}")
        print(f"unlock code : {mint(dev)}")
        return

    if not args.device_code:
        ap.error("pass a customer device code (or --here to test locally)")
    dev = lic.normalize(args.device_code)
    print(f"device code : {lic._format(dev[3:], 'ODU-')}")
    print(f"unlock code : {mint(args.device_code)}")
    print()
    print("Reply template:")
    print(f"  Your Vouchers unlock code is: {mint(args.device_code)}")
    print("  Open the Vouchers tab, type it in, and the feature unlocks")
    print("  permanently on this device. Contact: "
          f"{lic.CONTACT_EMAIL} / TikTok @{lic.CONTACT_TIKTOK}")


if __name__ == "__main__":
    main()
