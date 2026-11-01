#!/usr/bin/env python3
"""Compile and run the riscv-arch-test 2.x RV32I/RV32M/RV32C/RV32A/RV32F suites on Sprouts500.

For each test:
  - assemble/link with the RISC-V bare-metal GCC
  - objcopy to a flat binary, pack into a 32-bit hex file
  - run sim/tb_core.v under Icarus Verilog
  - compare the signature dump against the checked-in reference output

Usage:
    python3 scripts/run_isa.py              # all tests
    python3 scripts/run_isa.py jalr-01      # one test
    python3 scripts/run_isa.py I/beq-01 M/div-01 C/cadd-01 F/fadd_b1-01

A bare test name selects it in every suite; a suite letter (I/M/C/A/F) selects
the whole suite.  F needs a core with the RV32F extension.
"""
from __future__ import annotations

import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISA = ROOT / "tests" / "isa"
BUILD = ROOT / "build" / "isa"
LOGS = ROOT / "logs" / "isa"
TB = ROOT / "sim" / "tb_core.v"
CORE = ROOT / "rtl" / "core.v"
FPU = ROOT / "rtl" / "fp_unit.v"
LINK = ROOT / "sw" / "isa" / "link.ld"
MODEL_INC = ROOT / "sw" / "isa"
ENV_INC = ISA / "env"

SUITES = ("I", "M", "C", "A", "F")
MAX_CYCLES = 2_000_000
JOBS = max(1, min(8, (os.cpu_count() or 4)))

PREFIX = os.environ.get("RISCV_PREFIX", "riscv64-unknown-elf-")


def find_tool(name: str) -> str:
    for cand in (PREFIX + name, name):
        p = shutil.which(cand)
        if p:
            return p
    raise SystemExit(
        f"missing tool '{PREFIX}{name}' (or '{name}').\n"
        "Run: bash scripts/setup_ubuntu.sh"
    )


GCC = find_tool("gcc")
OBJCOPY = find_tool("objcopy")
NM = find_tool("nm")
IVERILOG = find_tool("iverilog")
VVP = find_tool("vvp")


def run(cmd: list[str], timeout: int = 180, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def pack_hex(binary: bytes, out: Path) -> int:
    if len(binary) % 4:
        binary += b"\x00" * (4 - len(binary) % 4)
    with out.open("w", encoding="ascii") as f:
        for i in range(0, len(binary), 4):
            f.write("%08x\n" % int.from_bytes(binary[i:i + 4], "little"))
    return len(binary) // 4


def ref_words(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()]


def run_one(suite: str, name: str) -> dict:
    src = ISA / "rv32i_m" / suite / "src" / f"{name}.S"
    ref = ISA / "rv32i_m" / suite / "references" / f"{name}.reference_output"
    elf = BUILD / f"{name}.elf"
    binf = BUILD / f"{name}.bin"
    hexf = BUILD / f"{name}.hex"
    logf = LOGS / f"{name}.log"

    # C expands in IF to canonical 32-bit IR; -mabi=ilp32 applies to all.
    MARCH = {"I": "rv32i", "M": "rv32im", "C": "rv32ic_zicsr", "A": "rv32ia_zicsr",
             "F": "rv32if_zicsr"}
    march = MARCH[suite]
    # FLEN selects flw/fsw (FLREG/FSREG) and FREGWIDTH in arch_test.h.  It must
    # be defined before arch_test.h, so pass it on the command line.
    flen = ["-DFLEN=32"] if suite == "F" else []
    cc = [
        GCC, f"-march={march}", "-mabi=ilp32", "-DXLEN=32", *flen,
        "-nostdlib", "-nostartfiles", "-static", "-mcmodel=medany",
        "-I", str(MODEL_INC), "-I", str(ENV_INC),
        "-T", str(LINK), str(src), "-o", str(elf),
    ]
    r = run(cc)
    if r.returncode != 0:
        logf.write_text(r.stdout)
        return {"name": name, "suite": suite, "ok": False, "stage": "compile",
                "detail": r.stdout[-800:]}

    r = run([OBJCOPY, "-O", "binary", str(elf), str(binf)])
    if r.returncode != 0:
        logf.write_text(r.stdout)
        return {"name": name, "suite": suite, "ok": False, "stage": "objcopy",
                "detail": r.stdout[-800:]}

    r = run([NM, "-n", str(elf)])
    m = re.search(r"^([0-9a-fA-F]+)\s+\S+\s+begin_signature\s*$", r.stdout, re.M)
    if not m:
        logf.write_text(r.stdout)
        return {"name": name, "suite": suite, "ok": False, "stage": "nm",
                "detail": "begin_signature not found"}
    sig_addr = int(m.group(1), 16)

    refs = ref_words(ref)
    sig_len = len(refs)
    pack_hex(binf.read_bytes(), hexf)

    r = run([
        VVP, str(BUILD / "tb_core.vvp"),
        f"+ITCM_HEX={hexf.as_posix()}",
        f"+SIG_ADDR={sig_addr:x}",
        f"+SIG_LEN={sig_len}",
        f"+REF_FILE={ref.as_posix()}",
        f"+MAX_CYCLES={MAX_CYCLES}",
    ], timeout=600)
    out = r.stdout
    logf.write_text(
        f"suite={suite} test={name} sig_addr=0x{sig_addr:x} sig_len={sig_len}\n"
        + out
    )
    passed = "TEST_PASS" in out
    cm = re.search(r"cycles=(\d+)", out)
    return {
        "name": name, "suite": suite, "ok": passed,
        "stage": "pass" if passed else "run",
        "cycles": int(cm.group(1)) if cm else None,
        "detail": "" if passed else out[-1200:],
    }


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    r = run([IVERILOG, "-g2012", "-o", str(BUILD / "tb_core.vvp"),
             str(TB), str(CORE), str(FPU)])
    if r.returncode != 0:
        print("testbench compile failed:")
        print(r.stdout)
        return 1

    only = set(sys.argv[1:])
    tests: list[tuple[str, str]] = []
    for suite in SUITES:
        d = ISA / "rv32i_m" / suite / "src"
        for p in sorted(d.glob("*.S")):
            if only and (p.stem not in only) and (f"{suite}/{p.stem}" not in only) \
                    and (suite not in only):
                continue
            tests.append((suite, p.stem))

    if not tests:
        print("no tests found; run: python3 scripts/fetch_isa.py")
        return 1

    print(f"running {len(tests)} tests with {JOBS} jobs...")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=JOBS) as ex:
        futures = {ex.submit(run_one, s, n): (s, n) for s, n in tests}
        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                s, n = futures[fut]
                res = {"name": n, "suite": s, "ok": False, "stage": "exception",
                       "detail": str(exc)}
            results.append(res)
            tag = "PASS" if res["ok"] else "FAIL"
            cyc = f" cycles={res['cycles']}" if res.get("cycles") is not None else ""
            print(f"[{tag}] {res['suite']}/{res['name']}{cyc}")

    results.sort(key=lambda x: (x["suite"], x["name"]))
    passed = sum(1 for x in results if x["ok"])
    print(f"\n{passed}/{len(results)} passed")
    for res in results:
        if not res["ok"]:
            print(f"\n--- {res['suite']}/{res['name']} ({res['stage']}) ---")
            print(res.get("detail", ""))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
