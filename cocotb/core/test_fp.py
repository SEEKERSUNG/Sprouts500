"""Directed RV32F integration tests (rtl/fp_unit.v + core plumbing).

Cases
-----
hazards    FLW -> FADD/FMUL chains, FP WB bypass and the FP scoreboard: the
           result path and back-to-back FP dependencies must not lose data.
fflags     fdiv 1/3 accrues NX, an exact fadd adds nothing, and writing fflags
           clears the accrued bits.
fs_off     an FP instruction with mstatus.FS==Off traps as illegal (cause 2),
           with the trapping PC in mepc.

The arithmetic itself is covered exhaustively by the 146 riscv-arch-test F
tests; these cases exercise the core's FP register file, hazards, CSR and FS
logic.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    DTCM_BASE, HALT_ADDR, HALT_MAGIC, Asm, expect, tb_case,
)
from _runner import run_test  # noqa: E402

CASES = ("hazards", "fflags", "fs_off")


def _tail(a):
    a.align()
    halt = a.here()
    a.li(31, HALT_ADDR)
    a.li(30, HALT_MAGIC)
    a.sw(30, 31, 0)
    a.self_loop()
    return a.assemble(), halt


def _flw(rd, rs1, imm):
    return (imm & 0xFFF) << 20 | (rs1 << 15) | (0b010 << 12) | (rd << 7) | 0x07


def _fsw(rs2, rs1, imm):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (0b010 << 12) | \
           ((imm & 0x1F) << 7) | 0x27


def _fop(f7, rd, rs1, rs2, rm=0):
    return (f7 << 25) | (rs2 << 20) | (rs1 << 15) | (rm << 12) | (rd << 7) | 0x53


def _fmv_x_w(rd, fs):
    return (0x70 << 25) | (fs << 15) | (0 << 12) | (rd << 7) | 0x53


def _fmv_w_x(fd, rs):
    return (0x78 << 25) | (rs << 15) | (0 << 12) | (fd << 7) | 0x53


async def _run(dut, words, halt, max_cycles=2000):
    from _helpers import CoreHarness
    h = CoreHarness(dut)
    h.load_program(words, zero_rest=True)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0, "trap_taken": 0}
    await h.run_with_trace(halt, max_cycles=max_cycles, stats=stats)
    return h


def build_hazards():
    a = Asm()
    a.li(6, 0x2000)
    a.csrs(0x300, 6)                    # mstatus.FS = Initial
    a.li(1, DTCM_BASE)
    a.li(2, 0x3F80_0000)                # 1.0
    a.sw(2, 1, 0)
    a.li(2, 0x4000_0000)                # 2.0
    a.sw(2, 1, 4)
    a.raw(_flw(1, 1, 0))
    a.raw(_flw(2, 1, 4))
    a.raw(_fop(0x00, 3, 1, 2))          # f3 = f1 + f2 = 3.0
    a.raw(_fmv_x_w(5, 3))
    a.sw(5, 1, 8)
    a.raw(_fop(0x08, 4, 3, 2))          # f4 = f3 * f2 = 6.0
    a.raw(_fmv_x_w(6, 4))
    a.sw(6, 1, 12)
    a.raw(_fop(0x08, 7, 4, 3))          # f7 = f4 * f3 = 18.0 (FP RAW)
    a.raw(_fmv_x_w(8, 7))
    a.sw(8, 1, 16)
    words, halt = _tail(a)
    return words, halt


async def _case_hazards(dut):
    words, halt = build_hazards()
    h = await _run(dut, words, halt)
    base = 0
    got = [h.dtcm(base + i) for i in range(5)]
    expect(got[2] == 0x4040_0000, f"fadd result 0x{got[2]:08x}")
    expect(got[3] == 0x40C0_0000, f"fmul result 0x{got[3]:08x}")
    expect(got[4] == 0x4190_0000, f"FP RAW result 0x{got[4]:08x}")


def build_fflags():
    a = Asm()
    a.li(6, 0x2000)
    a.csrs(0x300, 6)
    a.li(1, DTCM_BASE)
    a.li(2, 0x3F80_0000)
    a.sw(2, 1, 0)
    a.li(2, 0x4040_0000)                # 3.0
    a.sw(2, 1, 4)
    a.raw(_flw(1, 1, 0))
    a.raw(_flw(2, 1, 4))
    a.raw(_fop(0x0C, 3, 1, 2))          # 1/3: NX
    a.csrr(5, 0x001)
    a.sw(5, 1, 8)
    a.raw(_fop(0x00, 4, 1, 2))          # exact 4.0: no new flags
    a.csrr(6, 0x001)
    a.sw(6, 1, 12)
    a.csrw(0x001, 0)                    # clear fflags
    a.csrr(7, 0x001)
    a.sw(7, 1, 16)
    words, halt = _tail(a)
    return words, halt


async def _case_fflags(dut):
    words, halt = build_fflags()
    h = await _run(dut, words, halt)
    expect(h.dtcm(2) == 1, f"fflags after fdiv = {h.dtcm(2)}")
    expect(h.dtcm(3) == 1, f"fflags accumulated = {h.dtcm(3)}")
    expect(h.dtcm(4) == 0, f"fflags not cleared: {h.dtcm(4)}")


def build_fs_off():
    a = Asm()
    a.li(28, DTCM_BASE)
    a.la(30, "handler")
    a.csrw(0x305, 30)
    a.raw(_fop(0x00, 1, 1, 1))          # FS is Off at reset: cause 2
    a.li(3, 0x1234)
    a.sw(3, 28, 4)
    a.j("done")
    a.label("handler")
    a.csrr(5, 0x342)
    a.sw(5, 28, 0)
    a.csrr(6, 0x341)
    a.addi(6, 6, 4)
    a.csrw(0x341, 6)
    a.mret()
    a.label("done")
    words, halt = _tail(a)
    return words, halt


async def _case_fs_off(dut):
    words, halt = build_fs_off()
    h = await _run(dut, words, halt)
    expect(h.dtcm(0) == 2, f"FS=Off FP instruction mcause={h.dtcm(0)}")
    expect(h.dtcm(1) == 0x1234, f"execution did not resume: {h.dtcm(1):x}")


_RUNNERS = {"hazards": _case_hazards, "fflags": _case_fflags,
            "fs_off": _case_fs_off}


@pytest.mark.parametrize("case", CASES)
def test_fp(case):
    run_test("test_fp", seed=0, case=case)


@cocotb.test()
async def fp(dut):
    case = tb_case("hazards")
    expect(case in _RUNNERS, f"unknown fp case {case}")
    await _RUNNERS[case](dut)
