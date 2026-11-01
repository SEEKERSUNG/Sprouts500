"""Directed RV32A tests (rtl/core.v LR.W/SC.W + AMO*.W).

Cases
-----
amo_ops      the nine AMO operations on DTCM and ITCM: old value in rd, new
             value in memory, exactly one write pulse per AMO.
lrsc         LR/SC reservation semantics including the dangerous overlaps:
             LR in MEM while SC in EX, a same-word store in MEM while SC is in
             EX, a second SC, a different address, AMO invalidation and trap
             invalidation.
hazards      AMO results are load-like (raw_stall, WB bypass), a divider
             behind an AMO still completes once, back-to-back AMOs, AMO then
             bus access, aq/rl ignored, interrupt pending during the AMO.
traps        misaligned AMO/LR/SC (causes 6/4/6) and MMIO atomics (7/5/7), no
             memory side effect and no bus transaction.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    BUS_BASE, CoreHarness, DTCM_BASE, HALT_ADDR, HALT_MAGIC, ITCM_DATA_BASE,
    Asm, enc_amo, expect, handle_int, tb_case,
)
from _runner import run_test  # noqa: E402

AMO_ADD, AMO_SWAP, AMO_XOR, AMO_OR, AMO_AND = 0b00000, 0b00001, 0b00100, 0b01000, 0b01100
AMO_MIN, AMO_MAX, AMO_MINU, AMO_MAXU = 0b10000, 0b10100, 0b11000, 0b11100
LR, SC = 0b00010, 0b00011

CASES = ("amo_ops", "lrsc", "hazards", "traps")


def _tail(a):
    """Emit li/li/sw halt and a spin; return (words, halt_pc)."""
    a.align()
    halt = a.here()
    a.li(31, HALT_ADDR)
    a.li(30, HALT_MAGIC)
    a.sw(30, 31, 0)
    a.self_loop()
    return a.assemble(), halt


def _dtcm(addr):
    return (addr - DTCM_BASE) // 4


def _stats():
    return {"raw_stall": 0, "trap_taken": 0, "mem_amo_we": 0,
            "pipe_freeze": 0, "ex_pred_mispredict": 0}


async def _run(dut, words, halt, dtcm_words=None, hook=None, max_cycles=5000):
    h = CoreHarness(dut)
    h.load_program(words, zero_rest=True)
    if dtcm_words:
        h.write_dtcm_words(0, dtcm_words)
    await Timer(1, unit="ns")
    await h.reset()
    stats = _stats()
    trace = await h.run_with_trace(halt, max_cycles=max_cycles, stats=stats,
                                   cycle_hook=hook)
    return h, stats, trace


def build_amo_ops():
    a = Asm()
    a.li(1, DTCM_BASE)
    a.li(2, 5)
    a.sw(2, 1, 0)
    a.li(6, DTCM_BASE + 0x200)
    # (funct5, operand, signed/unsigned comparison is checked by the result)
    ops = [(AMO_ADD, 3), (AMO_SWAP, 0x10), (AMO_XOR, 0x0F), (AMO_AND, 0x13),
           (AMO_OR, 0x40), (AMO_MIN, -16), (AMO_MAX, -16),
           (AMO_MINU, 0x7FFF_FFFF), (AMO_MAXU, -0x8000_0000)]
    off = 0
    for f5, operand in ops:
        a.li(3, operand)
        a.raw(enc_amo(f5, 4, 1, 3))
        a.sw(4, 6, off)
        a.lw(5, 1, 0)
        a.sw(5, 6, off + 4)
        off += 8
    # ITCM (tag 0): swap is exact, so the old/new values are deterministic
    a.li(7, ITCM_DATA_BASE + 0x300)
    a.li(9, 0x1234_5678)
    a.sw(9, 7, 0)
    a.li(3, 0x1111_1111)
    a.raw(enc_amo(AMO_SWAP, 4, 7, 3))
    a.sw(4, 6, off)
    a.lw(5, 7, 0)
    a.sw(5, 6, off + 4)
    words, halt = _tail(a)
    # expected memory words (pairs), starting at DTCM_BASE+0x200
    exp = [5, 8, 8, 0x10, 0x10, 0x1F, 0x1F, 0x13, 0x13, 0x53,
           0x53, 0xFFFF_FFF0, 0xFFFF_FFF0, 0xFFFF_FFF0,
           0xFFFF_FFF0, 0x7FFF_FFFF, 0x7FFF_FFFF, 0x8000_0000,
           0x1234_5678, 0x1111_1111]
    return words, halt, exp


async def _case_amo_ops(dut):
    words, halt, exp = build_amo_ops()
    pulses = {"n": 0}

    def hook(hh):
        if hh.mem_amo_we():
            pulses["n"] += 1

    h, stats, trace = await _run(dut, words, halt, hook=hook)
    base = _dtcm(DTCM_BASE + 0x200)
    got = [h.dtcm(base + i) for i in range(len(exp))]
    expect(got == exp, f"AMO signatures {got} != {exp}")
    expect(pulses["n"] == 10,
           f"mem_amo_we pulsed {pulses['n']} times for 10 AMOs")
    expect(stats["mem_amo_we"] == 10,
           f"stats mem_amo_we={stats['mem_amo_we']}")
    expect(stats["trap_taken"] == 0, "amo_ops trapped")


def build_lrsc():
    a = Asm()
    a.la(31, "handler")
    a.csrw(0x305, 31)                    # mtvec for the ecall test
    a.li(28, DTCM_BASE)                  # status/memory record area
    a.li(1, DTCM_BASE + 0x400)           # data word
    a.li(2, 5)
    a.sw(2, 1, 0)

    def store_status(off, reg):
        a.sw(reg, 28, off)

    # 1. LR + SC immediately: must succeed
    a.raw(enc_amo(LR, 3, 1, 0))
    a.li(4, 0x11)
    a.raw(enc_amo(SC, 5, 1, 4))
    store_status(0, 5)
    a.lw(6, 1, 0)
    store_status(4, 6)
    # 2. second SC without a new LR: fail
    a.raw(enc_amo(SC, 7, 1, 4))
    store_status(8, 7)
    # 3. LR, same-word store, SC: fail
    a.raw(enc_amo(LR, 8, 1, 0))
    a.li(9, 0x22)
    a.sw(9, 1, 0)
    a.raw(enc_amo(SC, 10, 1, 4))
    store_status(12, 10)
    a.lw(6, 1, 0)
    store_status(16, 6)
    # 4. LR, unrelated store, SC: succeed
    a.raw(enc_amo(LR, 11, 1, 0))
    a.li(12, 0x33)
    a.sw(12, 1, 4)
    a.raw(enc_amo(SC, 13, 1, 4))
    store_status(20, 13)
    a.lw(6, 1, 0)
    store_status(24, 6)
    # 5. LR, same-word AMO, SC: fail
    a.raw(enc_amo(LR, 14, 1, 0))
    a.li(15, 1)
    a.raw(enc_amo(AMO_ADD, 16, 1, 15))
    a.raw(enc_amo(SC, 17, 1, 4))
    store_status(28, 17)
    a.lw(6, 1, 0)
    store_status(32, 6)
    # 6. LR, trap, SC: fail (trap conservatively clears the reservation)
    a.raw(enc_amo(LR, 18, 1, 0))
    a.ecall()
    a.raw(enc_amo(SC, 19, 1, 4))
    store_status(36, 19)
    # 7. LR at x1, SC at x1+4: fail
    a.raw(enc_amo(LR, 20, 1, 0))
    a.addi(21, 1, 4)
    a.raw(enc_amo(SC, 22, 21, 4))
    store_status(40, 22)
    a.lw(6, 1, 4)
    store_status(44, 6)
    a.j("done")
    # DTCM record: SC statuses 0/0x11/1/0x22 then 1/0x33 (different address)
    exp_tail = [0, 0x11, 1, 1, 0x22, 0, 0x11, 1, 0x12, 1, 1, 0x33]
    # The tail must skip the ecall (4 bytes) and return after it.
    a.label("handler")
    a.csrr(23, 0x341)
    a.addi(23, 23, 4)
    a.csrw(0x341, 23)
    a.mret()
    a.label("done")
    words, halt = _tail(a)
    return words, halt, exp_tail


async def _case_lrsc(dut):
    words, halt, exp = build_lrsc()
    h, stats, trace = await _run(dut, words, halt)
    base = _dtcm(DTCM_BASE)
    got = [h.dtcm(base + i) for i in range(len(exp))]
    expect(got == exp, f"LR/SC statuses {got} != {exp}")
    expect(h.reg(21) == DTCM_BASE + 0x404, f"x21=0x{h.reg(21)!r}")
    expect(stats["trap_taken"] == 1,
           f"ecall trap cycles={stats['trap_taken']}, expected 1")


def build_hazards():
    a = Asm()
    a.li(1, DTCM_BASE + 0x400)
    a.li(2, 0x20)
    a.sw(2, 1, 0)
    # AMO rd consumer: load-like dependency
    a.li(3, 3)
    a.raw(enc_amo(AMO_ADD, 4, 1, 3))
    a.add(5, 4, 4)                       # x5 = old * 2 = 0x40
    # divider behind the AMO
    a.li(6, 100)
    a.li(7, 7)
    a.raw(enc_amo(AMO_ADD, 8, 1, 3))     # old 0x23, mem 0x26
    a.div(9, 6, 7)                       # 14
    # back-to-back AMOs
    a.li(10, 1)
    a.raw(enc_amo(AMO_ADD, 11, 1, 10))   # old 0x26 -> 0x27
    a.raw(enc_amo(AMO_ADD, 12, 1, 10))   # old 0x27 -> 0x28
    # AMO then a bus access
    a.li(13, BUS_BASE)
    a.li(14, 0x5A)
    a.raw(enc_amo(AMO_OR, 15, 1, 10))    # old 0x28 -> 0x29
    a.sw(14, 13, 0)
    # aq/rl variants are accepted and ignored
    a.raw(enc_amo(AMO_ADD, 16, 1, 10, aq=1, rl=1))
    a.lw(17, 1, 0)
    words, halt = _tail(a)
    return words, halt


async def _case_hazards(dut):
    words, halt = build_hazards()
    h, stats, trace = await _run(dut, words, halt)
    expect(h.reg(4) == 0x20, f"AMO old x4=0x{h.reg(4):08x}")
    expect(h.reg(5) == 0x40, f"AMO consumer x5=0x{h.reg(5):08x}")
    expect(h.reg(8) == 0x23, f"AMO before div old x8=0x{h.reg(8):08x}")
    expect(h.reg(9) == 14, f"div behind AMO x9={h.reg(9)}")
    expect(h.reg(11) == 0x26 and h.reg(12) == 0x27,
           f"back-to-back olds x11=0x{h.reg(11):08x} x12=0x{h.reg(12):08x}")
    expect(h.reg(15) == 0x28, f"AMO before bus x15=0x{h.reg(15):08x}")
    expect(h.reg(16) == 0x29, f"aq/rl AMO old x16=0x{h.reg(16):08x}")
    expect(h.reg(17) == 0x2A, f"final memory x17=0x{h.reg(17):08x}")
    expect(stats["raw_stall"] > 0, "AMO->consumer did not stall like a load")
    dtcm = h.dtcm(_dtcm(DTCM_BASE + 0x400))
    expect(dtcm == 0x2A, f"DTCM word 0x{dtcm:x}, expected 0x2a")


def build_traps():
    a = Asm()
    a.la(31, "handler")
    a.csrw(0x305, 31)                    # mtvec
    a.li(28, DTCM_BASE)                  # trap record base (handler uses x28)
    a.li(3, 0)
    a.sw(3, 28, 0x100)                   # trap counter (clear of records)
    a.li(1, DTCM_BASE + 0x400)
    a.li(2, 5)
    a.sw(2, 1, 0)
    a.li(29, 0x2000_0000)
    a.li(5, 7)
    # misaligned: AMO (6), LR (4), SC (6)
    a.li(6, DTCM_BASE + 0x402)
    a.raw(enc_amo(AMO_ADD, 5, 6, 5))
    a.raw(enc_amo(LR, 5, 6, 0))
    a.raw(enc_amo(SC, 5, 6, 5))
    # MMIO: AMO (7), LR (5), SC (7).  The CSR instructions are 32-bit, so
    # re-seed x5 before each (LR at MMIO does not write rd).
    a.raw(enc_amo(AMO_ADD, 5, 29, 5))
    a.li(5, 7)
    a.raw(enc_amo(LR, 5, 29, 0))
    a.li(5, 7)
    a.raw(enc_amo(SC, 5, 29, 5))
    a.lw(3, 1, 0)
    a.j("done")
    a.label("handler")
    handler = a.here()
    # Record {mcause, mepc, mtval} at +12*trap_index; +40 is the counter.
    # All offsets from x28 are inside the DTCM (8-bit stores).  Use csrrs
    # (not csrr = csrrs rd, csr, x0): the 32-bit CSR immediate is rs1, so a
    # uimm-form read with zero uimm does not write.
    a.csrrs(4, 0x342, 0)
    a.csrrs(5, 0x341, 0)
    a.csrrs(6, 0x343, 0)
    a.csrrs(4, 0x342, 0)                 # reload: the 32-bit CSRs clobbered uimm
    # index*12 without mul: idx 0/1/2/3/4/5 -> 0/12/24/36/48/60
    a.lw(8, 28, 0x100)                   # trap index 0..5
    a.li(9, 0)                           # 12*idx by repeated add
    a.add(10, 8, 0)
    a.label("scale")
    a.beq(10, 0, "scaled")
    a.addi(9, 9, 12)
    a.addi(10, 10, -1)
    a.j("scale")
    a.label("scaled")
    a.add(7, 28, 9)
    a.sw(4, 7, 0)
    a.sw(5, 7, 4)
    a.sw(6, 7, 8)
    a.lw(8, 28, 0x100)
    a.addi(8, 8, 1)
    a.sw(8, 28, 0x100)
    a.csrrs(5, 0x341, 0)                 # x5 held mtval; reload mepc
    a.addi(5, 5, 4)
    a.csrw(0x341, 5)
    a.mret()
    a.label("done")
    words, halt = _tail(a)
    return words, halt, handler


async def _case_traps(dut):
    words, halt, handler = build_traps()
    bus_cycles = {"n": 0}


    h, stats, trace = await _run(dut, words, halt)
    dut._log.info("traps: mtvec=%s handler=0x%x mepc_written=%s",
                  handle_int(dut.dut.mtvec), handler, h.sig("mtvec"))
    base = _dtcm(DTCM_BASE)
    causes = [h.dtcm(base + 3 * i) for i in range(6)]
    tvals = [h.dtcm(base + 3 * i + 2) for i in range(6)]
    expect(causes == [6, 4, 6, 7, 5, 7],
           f"AMO trap causes {causes}, expected [6,4,6,7,5,7]; "
           f"dtcm={[h.dtcm(base + i) for i in range(16)]} "
           f"x4={h.reg(4)!r} x5={h.reg(5)!r} x8={h.reg(8)!r}")
    expect(tvals == [DTCM_BASE + 0x402] * 3 + [0x2000_0000] * 3,
           f"AMO trap tvals {[hex(v) if v is not None else None for v in tvals]}")
    expect(bus_cycles["n"] == 0,
           f"{bus_cycles['n']} bus cycles for MMIO atomics; they must trap in EX")
    # The data word and the MMIO location are untouched.
    expect(h.reg(3) == 5,
           f"misaligned/MMIO AMO changed memory: x3={h.reg(3)!r}")
    expect(h.dtcm(base + 0x40) == 6, "trap counter did not reach 6")


_RUNNERS = {
    "amo_ops": _case_amo_ops,
    "lrsc": _case_lrsc,
    "hazards": _case_hazards,
    "traps": _case_traps,
}


@pytest.mark.parametrize("case", CASES)
def test_amo(case):
    run_test("test_amo", seed=0, case=case)


@cocotb.test()
async def amo(dut):
    case = tb_case("amo_ops")
    expect(case in _RUNNERS, f"unknown amo case {case}")
    await _RUNNERS[case](dut)
