#!/usr/bin/env python3
"""Patch python-for-android so EVERY library it builds gets 16 KB ELF alignment.

Android 16/17 devices with 16 KB kernels (and Play's 16 KB checks) require
shared libraries linked with:

    -Wl,-z,max-page-size=16384
    -Wl,-z,common-page-size=16384

NDK r28+ sets max-page-size by default but NOT common-page-size, so p4a's
-stock- builds ship libraries whose GNU_RELRO ends are only 4/8/12 KB-aligned.
See developer.android.com/guide/practices/page-sizes ("Check the RELRO
security flag").

Usage:
    python3 tools/patch_p4a_16kb.py <path-to-pythonforandroid/archs.py>

Fails loudly (exit 1) when upstream p4a changes the expected code, so CI
never silently ships an unpatched build. Safe to run repeatedly (idempotent).
"""
import pathlib
import sys

# --- patch 1: generic link flags every autotools/make recipe inherits
OLD_LDFLAGS = "common_ldflags = ['-L{ctx_libs_dir}']"
NEW_LDFLAGS = (
    "common_ldflags = ["
    "'-L{ctx_libs_dir}', "
    "'-Wl,-z,max-page-size=16384', "
    "'-Wl,-z,common-page-size=16384'"
    "]"
)

# --- patch 2: LDSHARED/LDCXXSHARED flags (python extension links, etc.)
OLD_LDSHARED = (
    "    common_ldshared = [\n"
    "        '-pthread',\n"
    "        '-shared',\n"
    "        '-Wl,-O1',\n"
    "        '-Wl,-Bsymbolic-functions',\n"
    "    ]"
)
NEW_LDSHARED = (
    "    common_ldshared = [\n"
    "        '-pthread',\n"
    "        '-shared',\n"
    "        '-Wl,-O1',\n"
    "        '-Wl,-Bsymbolic-functions',\n"
    "        '-Wl,-z,max-page-size=16384',\n"
    "        '-Wl,-z,common-page-size=16384',\n"
    "    ]"
)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = pathlib.Path(sys.argv[1])
    if not target.is_file():
        print(f"patch_p4a_16kb: file not found: {target}", file=sys.stderr)
        return 1
    text = target.read_text()

    if "max-page-size=16384" in text and "common-page-size=16384" in text:
        print("patch_p4a_16kb: already patched — nothing to do")
        return 0

    changed = 0
    if OLD_LDFLAGS in text:
        text = text.replace(OLD_LDFLAGS, NEW_LDFLAGS)
        changed += 1
    if OLD_LDSHARED in text:
        text = text.replace(OLD_LDSHARED, NEW_LDSHARED)
        changed += 1

    # both blocks must be present in one pass, or upstream changed layout
    if changed < 2:
        print(
            f"patch_p4a_16kb: FATAL — only {changed}/2 patterns matched; "
            "python-for-android changed its layout. Update the patch patterns.",
            file=sys.stderr,
        )
        return 1

    if text.count("max-page-size=16384") < 2 or text.count("common-page-size=16384") < 2:
        print(
            "patch_p4a_16kb: FATAL — expected 2 flag sites per flag not produced.",
            file=sys.stderr,
        )
        return 1

    target.write_text(text)
    print(
        "patch_p4a_16kb: OK — archs.py now links every library with 16 KB "
        "ELF alignment (max-page-size + common-page-size)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
