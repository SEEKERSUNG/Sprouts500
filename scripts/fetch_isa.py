#!/usr/bin/env python3
"""Fetch the riscv-arch-test 2.x RV32I/RV32M/RV32C/RV32F sources + reference signatures.

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

C_TESTS = """cadd-01 caddi-01 caddi16sp-01 caddi4spn-01 cand-01 candi-01 cbeqz-01
cbnez-01 cebreak-01 cj-01 cjal-01 cjalr-01 cjr-01 cli-01 clui-01 clw-01 clwsp-01
cmv-01 cnop-01 cor-01 cslli-01 csrai-01 csrli-01 csub-01 csw-01 cswsp-01
cxor-01""".split()

# RV32F is fetched from upstream like I/M/C.  These tests are large (~126 MiB of
# assembly in total, several sources >18 MiB); the fetch is still incremental.
F_TESTS = """fadd_b1-01 fadd_b10-01 fadd_b11-01 fadd_b12-01 fadd_b13-01 fadd_b2-01 fadd_b3-01
fadd_b4-01 fadd_b5-01 fadd_b7-01 fadd_b8-01 fclass_b1-01 fcvt.s.w_b25-01
fcvt.s.w_b26-01 fcvt.s.wu_b25-01 fcvt.s.wu_b26-01 fcvt.w.s_b1-01 fcvt.w.s_b22-01
fcvt.w.s_b23-01 fcvt.w.s_b24-01 fcvt.w.s_b27-01 fcvt.w.s_b28-01 fcvt.w.s_b29-01
fcvt.wu.s_b1-01 fcvt.wu.s_b22-01 fcvt.wu.s_b23-01 fcvt.wu.s_b24-01
fcvt.wu.s_b27-01 fcvt.wu.s_b28-01 fcvt.wu.s_b29-01 fdiv_b1-01 fdiv_b2-01
fdiv_b20-01 fdiv_b21-01 fdiv_b3-01 fdiv_b4-01 fdiv_b5-01 fdiv_b6-01 fdiv_b7-01
fdiv_b8-01 fdiv_b9-01 feq_b1-01 feq_b19-01 fle_b1-01 fle_b19-01 flt_b1-01
flt_b19-01 flw-align-01 fmadd_b1-01 fmadd_b14-01 fmadd_b15-01 fmadd_b16-01
fmadd_b17-01 fmadd_b18-01 fmadd_b2-01 fmadd_b3-01 fmadd_b4-01 fmadd_b5-01
fmadd_b6-01 fmadd_b7-01 fmadd_b8-01 fmax_b1-01 fmax_b19-01 fmin_b1-01
fmin_b19-01 fmsub_b1-01 fmsub_b14-01 fmsub_b15-01 fmsub_b16-01 fmsub_b17-01
fmsub_b18-01 fmsub_b2-01 fmsub_b3-01 fmsub_b4-01 fmsub_b5-01 fmsub_b6-01
fmsub_b7-01 fmsub_b8-01 fmul_b1-01 fmul_b2-01 fmul_b3-01 fmul_b4-01 fmul_b5-01
fmul_b6-01 fmul_b7-01 fmul_b8-01 fmul_b9-01 fmv.w.x_b25-01 fmv.w.x_b26-01
fmv.x.w_b1-01 fmv.x.w_b22-01 fmv.x.w_b23-01 fmv.x.w_b24-01 fmv.x.w_b27-01
fmv.x.w_b28-01 fmv.x.w_b29-01 fnmadd_b1-01 fnmadd_b14-01 fnmadd_b15-01
fnmadd_b16-01 fnmadd_b17-01 fnmadd_b18-01 fnmadd_b2-01 fnmadd_b3-01 fnmadd_b4-01
fnmadd_b5-01 fnmadd_b6-01 fnmadd_b7-01 fnmadd_b8-01 fnmsub_b1-01 fnmsub_b14-01
fnmsub_b15-01 fnmsub_b16-01 fnmsub_b17-01 fnmsub_b18-01 fnmsub_b2-01
fnmsub_b3-01 fnmsub_b4-01 fnmsub_b5-01 fnmsub_b6-01 fnmsub_b7-01 fnmsub_b8-01
fsgnj_b1-01 fsgnjn_b1-01 fsgnjx_b1-01 fsqrt_b1-01 fsqrt_b2-01 fsqrt_b20-01
fsqrt_b3-01 fsqrt_b4-01 fsqrt_b5-01 fsqrt_b7-01 fsqrt_b8-01 fsqrt_b9-01
fsub_b1-01 fsub_b10-01 fsub_b11-01 fsub_b12-01 fsub_b13-01 fsub_b2-01 fsub_b3-01
fsub_b4-01 fsub_b5-01 fsub_b7-01 fsub_b8-01 fsw-align-01""".split()

# RV32A has no suite in the pinned old-framework tree; tests/isa/rv32i_m/A is
# project-owned and is not fetched (and must not be deleted) by this script.
# F (like I/M/C) is upstream-fetched; D is intentionally not fetched.

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

    for suite, tests in (("I", I_TESTS), ("M", M_TESTS), ("C", C_TESTS),
                         ("F", F_TESTS)):
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
