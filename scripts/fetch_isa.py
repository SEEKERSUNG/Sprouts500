#!/usr/bin/env python3
"""Fetch the riscv-arch-test 2.x RV32I/RV32M sources + reference signatures.

Files are placed under tests/isa/.  The default source is raw.githubusercontent.com;
set ISA_MIRROR to a proxy prefix if GitHub is slow, e.g.
  ISA_MIRROR=https://gh-proxy.com/https://raw.githubusercontent.com/riscv/riscv-arch-test/old-framework-2.x/
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "tests" / "isa"
DEFAULT_MIRROR = "https://raw.githubusercontent.com/riscv/riscv-arch-test/old-framework-2.x/"
# Optional mirror prefix, e.g. ISA_MIRROR=https://gh-proxy.com/https://raw.githubusercontent.com/riscv/riscv-arch-test/old-framework-2.x/
MIRROR = os.environ.get("ISA_MIRROR", DEFAULT_MIRROR)
if not MIRROR.endswith("/"):
    MIRROR += "/"

I_TESTS = """add-01 addi-01 and-01 andi-01 auipc-01 beq-01 bge-01 bgeu-01 blt-01
bltu-01 bne-01 jal-01 jalr-01 lb-align-01 lbu-align-01 lh-align-01 lhu-align-01
lui-01 lw-align-01 or-01 ori-01 sb-align-01 sh-align-01 sll-01 slli-01 slt-01
slti-01 sltiu-01 sltu-01 sra-01 srai-01 srl-01 srli-01 sub-01 sw-align-01
xor-01 xori-01 fence-01""".split()

M_TESTS = """div-01 divu-01 mul-01 mulh-01 mulhsu-01 mulhu-01 rem-01 remu-01""".split()

ENV_FILES = ["riscv-test-suite/env/arch_test.h", "riscv-test-suite/env/encoding.h"]


def fetch(rel: str, dest: Path, timeout: int = 90) -> bool:
    if dest.exists() and dest.stat().st_size > 100:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = MIRROR + rel
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sprouts500-fetch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if r.status != 200 or len(data) < 20:
                raise RuntimeError(f"bad response {r.status}, {len(data)} bytes")
            dest.write_bytes(data)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt == 3:
                print(f"FAIL {rel}: {exc}")
                return False
            time.sleep(1.5 * (attempt + 1))
    return False


def main() -> int:
    jobs: list[tuple[str, Path]] = []
    for rel in ENV_FILES:
        jobs.append((rel, DEST / rel.replace("riscv-test-suite/", "")))

    for suite, tests in (("I", I_TESTS), ("M", M_TESTS)):
        for name in tests:
            base = f"riscv-test-suite/rv32i_m/{suite}"
            jobs.append((f"{base}/src/{name}.S",
                         DEST / "rv32i_m" / suite / "src" / f"{name}.S"))
            jobs.append((f"{base}/references/{name}.reference_output",
                         DEST / "rv32i_m" / suite / "references" / f"{name}.reference_output"))

    ok = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fetch, rel, dest): (rel, dest) for rel, dest in jobs}
        for fut in concurrent.futures.as_completed(futures):
            rel, _ = futures[fut]
            if fut.result():
                ok += 1
                print(f"ok   {rel}")
            else:
                print(f"FAIL {rel}")

    print(f"\n{ok}/{len(jobs)} files present")
    return 0 if ok == len(jobs) else 1


if __name__ == "__main__":
    sys.exit(main())
