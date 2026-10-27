#!/usr/bin/env python3
"""Hard pre-release audit of an ODU-Tracker APK.

Verifies, with only unzip-grade tooling (zipfile + readelf):

  1. AndroidManifest-parsing prerequisites:
     - resources.arsc is STORED (mandatory for targetSdk >= 30 installs)
  2. 16 KB ELF page-size compliance for EVERY native library, per
     developer.android.com/guide/practices/page-sizes:
       - every PT_LOAD segment: p_align >= 0x4000 (16 KB) and
         p_vaddr % p_align == p_offset % p_align (mapping congruence)
       - GNU_RELRO: either the end (vaddr + memsz) is 16 KB aligned, or the
         RELRO spans its entire LOAD segment (Google's documented
         "still compliant" case)
       - ENFORCED for 64-bit ABIs only (arm64-v8a, x86_64). Google's rule
         explicitly exempts 32-bit ABIs (armeabi-v7a, x86): 16 KB-page
         devices run 64-bit user-space only, and NDK prebuilts such as
         libc++_shared/libomp cannot be relinked. 32-bit libs at
         p_align 0x1000 are informational, not failures.
  3. Signing:
       - v2 APK signing block present (magic "APK Sig Block 42")
       - v1 JAR signature files present (META-INF/CERT.SF + *.RSA) —
         some OEM "install unknown apps" impls silently refuse v2-only APKs
         with a bare "App not installed"
  4. App payload (assets/private.tar):
       - entry point + kivy shell + android_app/diagnostics (on-device crash
         reporting) + flask app + dashboard static are present
       - presplash/icon PNGs >= 1000px (a 256px source was upscaled to the
         whole display: "large logo, not scaled")

Prints a table + a PASS/FAIL summary. Exit 1 on any failure so CI stops.
"""
import glob
import io
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

PAGE = 0x4000  # 16 KB

BODY_CHECKS = ("resources.arsc",)


def elf_segments(path):
    """Return (loads, relros) for a shared object, from `readelf -lW`.

    loads: list of (offset, vaddr, memsz, align)
    relros: list of (offset, vaddr, memsz)
    """
    out = subprocess.run(
        ["readelf", "-lW", path], capture_output=True, text=True
    ).stdout
    loads, relros = [], []
    for line in out.splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "LOAD" and len(t) >= 8:
            loads.append((int(t[1], 16), int(t[2], 16), int(t[5], 16), int(t[-1], 16)))
        elif t[0] == "GNU_RELRO" and len(t) >= 7:
            # GNU_RELRO off vaddr paddr filesz memsz flags align… — memsz is t[5]
            relros.append((int(t[1], 16), int(t[2], 16), int(t[5], 16)))
    return loads, relros


def audit_elf(libpath, relname, problems):
    is_64 = relname.split("/")[1] in ("arm64-v8a", "x86_64")
    with open(libpath, "rb") as fh:
        if fh.read(4) != b"\x7fELF":
            # p4a's libpybundle.so is a gzip data blob (python code bundle),
            # never mapped as ELF; the loader ignores it. Informational only.
            print(f"    {relname}: not an ELF (data blob, e.g. p4a pybundle) — informational")
            return False, "not-ELF(data-blob)"

    loads, relros = elf_segments(libpath)
    bad = False

    for off, va, msz, al in loads:
        if al < PAGE:
            if is_64:
                print(f"    {relname}: LOAD p_align {al:#x} < 16 KB (4 KB-pages build)")
                problems.append(f"{relname}: PT_LOAD p_align {al:#x} < 16 KB")
                bad = True
            else:
                # Google's 16 KB requirement governs 64-bit ABIs only;
                # 32-bit libs (armeabi-v7a/x86) at 0x1000 are compliant.
                print(
                    f"    {relname}: LOAD p_align {al:#x} — 32-bit ABI, "
                    "exempt from 16 KB rule (informational)"
                )
        if va % al != off % al:
            print(
                f"    {relname}: LOAD incongruent — va {va:#x} vs off {off:#x} (align {al:#x})"
            )
            problems.append(f"{relname}: PT_LOAD incongruent (va {va:#x} off {off:#x})")
            bad = True

    for _off, rva, rmsz in relros:
        end = rva + rmsz
        container = next(
            (ld for ld in loads if ld[1] <= rva and end <= ld[1] + ld[2]), None
        )
        if container is None:
            continue  # RELRO not inside any LOAD — exotic; leave to loader validity
        end_of_load = container[1] + container[2]
        shares = end < end_of_load
        if shares and end % PAGE != 0:
            if not is_64:
                print(
                    f"    {relname}: RELRO end {end:#x} (32-bit ABI exempt) — informational"
                )
                continue
            print(
                f"    {relname}: RELRO shares LOAD with writable-data and ends "
                f"{end:#x} ({end % PAGE:#X} into a 16 KB page)"
            )
            problems.append(
                f"{relname}: RELRO end {end:#x} not 16 KB aligned (shares LOAD)"
            )
            bad = True

    relro_state = "own-LOAD(compliant)" if not bad else "FAIL"
    return bad, relro_state


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    apk = sys.argv[1]
    for tool in ("unzip", "readelf"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            print(f"verify_apk: required tool missing: {tool}", file=sys.stderr)
            return 1

    problems = []

    with zipfile.ZipFile(apk) as z:
        names = z.namelist()

        # --- 1. body prerequisites -------------------------------------
        arsc = next((i for i in z.infolist() if i.filename == "resources.arsc"), None)
        if arsc is None:
            problems.append("resources.arsc missing from APK")
        elif arsc.compress_type != zipfile.ZIP_STORED:
            problems.append(
                f"resources.arsc is COMPRESSED (type {arsc.compress_type}); "
                "targetSdk>=30 install sessions refuse compressed arsc"
            )
        else:
            print("body: resources.arsc STORED ✔")

        # --- 2. ELF audit per ABI ---------------------------------------
        elf_fail, elf_ok = 0, 0
        extracted = set()
        with tempfile.TemporaryDirectory() as td:
            for info in z.infolist():
                if not info.filename.startswith("lib/") or not info.filename.endswith(".so"):
                    continue
                parts = info.filename.split("/")
                abi = parts[1] if len(parts) == 3 else None
                if abi not in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64"):
                    continue
                relname = info.filename
                target = os.path.join(td, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(z.read(info.filename))
                extracted.add(abi)
                bad, _ = audit_elf(target, relname, problems)
                if bad:
                    elf_fail += 1
                else:
                    elf_ok += 1
            print(f"ELF: {elf_ok} libs compliant, {elf_fail} failing")
            if not extracted:
                problems.append("no native libraries extracted for audit")

        # --- 3. signatures ----------------------------------------------
        signed_v2 = False
        with open(apk, "rb") as fh:
            fh.seek(max(0, os.path.getsize(apk) - 0x40000))
            tail = fh.read()
            signed_v2 = b"APK Sig Block 42" in tail
        if signed_v2:
            print("signing: v2 APK signing block present ✔")
        else:
            problems.append("v2 APK signing block MISSING — installs are refused on Android 7+")
        v1_files = [
            n
            for n in names
            if n.startswith("META-INF/") and (n.endswith(".SF") or n.endswith(".RSA") or n.endswith(".DSA"))
        ]
        if v1_files:
            print(f"signing: v1 JAR signature present ({', '.join(sorted(v1_files))}) ✔")
        else:
            problems.append(
                "v1 JAR signature MISSING (no META-INF/CERT.*) — "
                "some OEM installers silently refuse v2-only APKs with 'App not installed'"
            )

        # --- 4. app payload + brand assets -------------------------------
        # assets/private.tar IS the app: if the entry point or the
        # crash-reporting module goes missing, the APK installs and then
        # "shows a big logo and closes" with nothing to diagnose.
        try:
            priv = z.read("assets/private.tar")
        except KeyError:
            problems.append("assets/private.tar missing — no app code packaged")
        else:
            with tarfile.open(fileobj=io.BytesIO(priv)) as tf:
                members = set(tf.getnames())

                def has(base):
                    return base in members or base[:-1] in members  # .py fallback

                required = (
                    "main.pyc",                    # p4a entry point
                    "android_app/main.pyc",        # kivy shell
                    "android_app/diagnostics.pyc",  # on-device crash reporting
                    "odu_tracker/web/app.pyc",
                    "odu_tracker/web/static/index.html",
                )
                missing = [r for r in required if not has(r)]
                if missing:
                    problems.append(
                        "private.tar is missing: " + ", ".join(missing)
                    )
                else:
                    print(f"payload: private.tar has entry + diagnostics "
                          f"({len(members)} members) ✔")

                def png_size(member):
                    data = tf.extractfile(member).read()
                    if data[:8] != b"\x89PNG\r\n\x1a\n":
                        return None
                    return struct.unpack(">II", data[16:24])

                # Brand assets: the old 256px presplash was upscaled to the
                # whole display ("large logo, not scaled") — keep it big.
                for member, need in (
                    ("android_app/assets/presplash.png", 1000),
                    ("android_app/assets/icon.png", 1000),
                ):
                    if member not in members:
                        problems.append(f"{member} missing from private.tar")
                        continue
                    size = png_size(member)
                    if size is None or size[0] < need or size[1] < need:
                        problems.append(
                            f"{member} is {size} — must be >= {need}px "
                            "(blurry upscaled launch screen otherwise)"
                        )
                    else:
                        print(f"asset: {member} {size[0]}x{size[1]} ✔")

    print()
    if problems:
        print(f"verify_apk: FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"   ✗ {p}")
        return 1
    print("verify_apk: PASS — APK passes all structural, ELF-alignment and signature checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
