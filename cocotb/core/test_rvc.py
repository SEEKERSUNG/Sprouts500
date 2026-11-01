"""Directed RV32C tests (rtl/core.v RVC front end).

The compressed expander is a pure function; these tests drive complete
programs and check architectural state plus a few microarchitectural
properties (straddle bubble, canonical IR at retirement, BTB lane separation,
compressed link values, illegal-compressed mtval/mepc).

Cases
-----
alu_mem        every integer/memory compressed form, mixed with 32-bit setup,
               results checked in registers and DTCM.
straddle       a 32-bit instruction starting at PC[1]=1 (assembled from the
               next word) executes exactly once with one IF bubble.
control        C.J/C.JAL/C.JR/C.JALR/C.BEQZ/C.BNEZ, PC+2 links, a 2-byte
               target, and a JALR target with bit 1 set.
illegal_hint   HINTs are no-ops; reserved compressed encodings trap with
               mcause=2, mtval=the zero-extended halfword, mepc=the 2-byte PC;
               C.EBREAK sets mtval=PC; CSR mepc writes mask bit 0 only.
btb_lanes      compressed and 32-bit instructions in the two lanes of the same
               word get distinct BTB entries and redirect correctly.
predicted      a compressed taken branch trained in the BTB is predicted with
               no mispredict after the cold one.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    CE_EBREAK, CE_NOP, CoreHarness, DTCM_BASE, HALT_ADDR, HALT_MAGIC, Asm,
    enc_i, ce_add, ce_addi,
    ce_addi16sp, ce_addi4spn, ce_andi, ce_and, ce_bnez, ce_j, ce_jal,
    ce_jalr, ce_jr, ce_li, ce_lui, ce_lw, ce_lwsp, ce_mv, ce_or, ce_slli,
    ce_srai, ce_srli, ce_sub, ce_sw, ce_swsp, ce_xor, expect, tb_case,
)
from _runner import run_test  # noqa: E402
from rvc_ref import rvc_expand  # noqa: E402

CASES = ("alu_mem", "straddle", "control", "illegal_hint", "btb_lanes",
         "predicted")


def _stats():
    keys = ("raw_stall", "id_flush_q", "trap_taken", "ex_pred_mispredict",
            "btb_we", "pipe_freeze")
    return {k: 0 for k in keys}


async def _run(dut, words, halt_pc, dtcm=None, hook=None, max_cycles=5000):
    h = CoreHarness(dut)
    h.load_program(words, zero_rest=True)
    if dtcm:
        h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    await h.reset()
    stats = _stats()
    trace = await h.run_with_trace(halt_pc, max_cycles=max_cycles,
                                   stats=stats, cycle_hook=hook)
    return h, stats, trace


def _word(addr):
    return addr // 4


def _dtcm_idx(addr):
    return (addr - DTCM_BASE) // 4


def _finish_tail(a):
    """Body words + explicit halt tail; returns (words, halt_pc)."""
    a.align()
    halt = a.here()
    a.li(31, HALT_ADDR)
    a.li(30, HALT_MAGIC)
    a.sw(30, 31, 0)               # run stops when this retires
    return a.assemble(), halt


def _patch16(words, pc, half):
    """Patch a little-endian 16-bit field at byte address ``pc``."""
    data = bytearray()
    for w in words:
        data += int(w).to_bytes(4, "little")
    data[pc:pc + 2] = int(half).to_bytes(2, "little")
    return [int.from_bytes(data[i:i + 4], "little")
            for i in range(0, len(data), 4)]


def build_alu_mem():
    a = Asm()
    a.li(2, DTCM_BASE)
    a.cword([ce_addi16sp(0x40)])          # x2 = DTCM_BASE + 0x40
    a.li(8, 0x1234)
    a.cword([ce_andi(8, 0x0F), ce_slli(8, 4), ce_srli(8, 2),
             ce_srai(8, 1), ce_addi(8, -4)])
    a.li(9, 6)
    a.cword([ce_add(8, 9), ce_sub(8, 9)])
    a.li(10, 0x00FF)
    a.li(11, 0x0F0F)
    a.cword([ce_and(10, 11), ce_or(10, 11), ce_xor(10, 11)])
    a.cword([ce_mv(12, 11)])
    a.cword([ce_li(13, 5), ce_li(14, -1), ce_lui(15, 9)])
    a.align()
    a.li(9, DTCM_BASE)
    a.cword([ce_sw(8, 9, 0), ce_sw(10, 9, 4), ce_sw(12, 9, 8),
             ce_sw(14, 9, 16)])
    a.cword([ce_sw(15, 9, 20), ce_swsp(11, 8)])   # sp+8 = DTCM_BASE+0x48
    a.cword([ce_lwsp(16, 8)])             # x16 = mem[sp+8] = 0x0F0F
    a.align()                             # C.LW/C.ADDI4SPN need word base
    a.cword([ce_addi4spn(8, 0x20)])       # x8 = x2 + 0x20 (lane 2)
    a.cword([ce_lw(10, 9, 0), ce_j(8)])   # C.LW, then skip the odd lane
    a.cword([ce_addi(13, 0x7F)])          # never decoded (odd lane)
    a.align()
    a.cword([ce_addi(7, 0x40)])
    return _finish_tail(a)


def build_straddle():
    a = Asm()
    a.cword([CE_NOP])                     # PC 0, lane 0
    a.raw32_unaligned(enc_i(0x13, 1, 0, 0, 5))   # PC 2: straddling ADDI
    a.cword([ce_addi(1, 1)])              # PC 6
    return _finish_tail(a)


def build_control():
    a = Asm()
    a.li(2, 0)
    a.li(8, 0)                            # loop accumulator
    a.li(11, 3)                           # loop counter (rs1' must be x8-x15)
    a.label("loop")
    loop = a.here()
    a.cword([ce_addi(8, 1), ce_addi(11, -1)])
    bp = a.here()
    a.cword([ce_bnez(11, loop - bp)])
    a.li(4, 0)                            # seed: the next li must be skipped
    cj_pc = a.here()
    a.cword([ce_j(0)])                    # offset patched to "keep"
    a.li(4, 0x0BAD)                       # skipped
    a.label("keep")
    keep = a.here()
    a.li(5, 0x600D)
    call1 = a.here()
    a.cword([ce_jal(0)])                  # offset patched to sub1
    after1 = a.here()
    a.cword([ce_mv(13, 10)])
    call2 = a.here()
    a.la(7, "sub2")
    a.cword([ce_jalr(7)])
    after2 = a.here()
    a.cword([ce_mv(14, 11)])
    a.align()
    a.la(12, "lane1")
    a.jalr(0, 12, 0)
    a.cword([CE_NOP])
    a.label("lane1")
    lane1 = a.here()
    a.cword([ce_addi(2, 1), ce_j(0)])     # C.J offset patched to "done"
    a.label("sub1")
    a.align()                             # C.JR returns here
    sub1 = a.here()
    a.addi(10, 1, 0)                      # capture C.JAL link
    a.cword([ce_jr(1)])
    a.label("sub2")
    a.align()                             # C.JR returns here
    sub2 = a.here()
    a.addi(11, 1, 0)                      # capture C.JALR link
    a.cword([ce_jr(1)])
    a.label("done")
    done_pc = a.here()
    a.align()
    words, halt = _finish_tail(a)
    words = _patch16(words, cj_pc, ce_j(keep - cj_pc))
    words = _patch16(words, call1, ce_jal(sub1 - call1))
    words = _patch16(words, lane1 + 2, ce_j(done_pc - (lane1 + 2)))
    return words, {"loop": loop, "call1": call1, "after1": after1,
                   "after2": after2, "sub1": sub1, "sub2": sub2,
                   "lane1": lane1, "halt": halt}


def build_illegal_hint():
    a = Asm()
    a.li(28, DTCM_BASE)                   # handler record pointer
    a.la(31, "handler")
    a.csrw(0x305, 31)
    a.li(1, 0x55)
    a.cword([ce_li(0, 7), ce_addi(0, 5), ce_slli(0, 1)])   # HINTs
    bad = a.here()
    bad_half = ce_slli(1, 0x21)           # shamt[5]=1: reserved in RV32
    a.cword([bad_half])
    resume1 = a.here()
    a.addi(2, 0, 0x22)
    eb = a.here()
    a.cword([CE_EBREAK])
    resume2 = a.here()
    a.align()
    a.addi(7, 0, 0x33)
    a.align()
    a.li(8, 0x0000_FFFF)
    a.csrw(0x341, 8)
    a.csrr(9, 0x341)
    a.j("done")
    a.label("handler")
    a.csrr(29, 0x342)
    a.sw(29, 28, 0)
    a.csrr(29, 0x341)
    a.sw(29, 28, 4)
    a.csrr(29, 0x343)
    a.sw(29, 28, 8)
    a.addi(28, 28, 12)
    a.csrr(29, 0x341)                     # reload mepc (x29 held mtval)
    a.addi(29, 29, 2)                     # both faults are compressed
    a.csrw(0x341, 29)
    a.mret()
    a.label("done")
    a.align()
    words, halt = _finish_tail(a)
    return words, halt, {"bad": bad, "bad_half": bad_half,
                                    "resume1": resume1, "eb": eb,
                                    "resume2": resume2}


def build_btb_lanes():
    a = Asm()
    a.li(2, 0)
    lane0 = a.here()
    a.cword([ce_j(2)])                    # lane 0 -> lane 1 of the same word
    lane1 = a.here()
    a.jal_unaligned(0, "exit")            # 32-bit JAL at lane 1 (straddles)
    a.cword([CE_NOP])                     # not executed
    a.label("exit")
    exit_pc = a.here()
    a.addi(2, 2, 1)
    words, halt = _finish_tail(a)
    return words, halt, lane0, lane1, exit_pc


def build_predicted():
    a = Asm()
    a.li(8, 0)                            # loop accumulator
    a.li(11, 4)                           # loop counter (rs1' must be x8-x15)
    a.label("head")
    head = a.here()
    a.cword([ce_addi(8, 1), ce_addi(11, -1)])
    bp = a.here()
    a.cword([ce_bnez(11, head - bp)])
    words, halt = _finish_tail(a)
    return words, halt, head, bp


async def _case_alu_mem(dut):
    words, halt = build_alu_mem()
    h, stats, trace = await _run(dut, words, halt)
    got_reg = {r: h.reg(r) for r in (8, 10, 11, 12, 13, 14, 15, 16)}
    base = _dtcm_idx(DTCM_BASE)
    got = [(base + i, h.dtcm(base + i)) for i in range(8)]
    expect(h.reg(8) == DTCM_BASE + 0x60, f"addi4spn x8=0x{h.reg(8):08x}")
    expect(h.reg(10) == 4, f"c.lw x10=0x{h.reg(10):08x}")
    expect(h.reg(16) == 0x0F0F, f"c.lwsp x16={h.reg(16)!r}")
    expect(got == [(base, 4), (base + 1, 0), (base + 2, 0x0F0F),
                   (base + 3, None), (base + 4, 0xFFFF_FFFF),
                   (base + 5, 0x9000), (base + 6, None), (base + 7, None)],
           f"DTCM results {got} regs={got_reg}")
    expect(h.dtcm(_dtcm_idx(DTCM_BASE + 0x48)) == 0x0F0F,
           "c.swsp did not store to sp+8")


async def _case_straddle(dut):
    words, halt = build_straddle()
    pending = {"n": 0}

    def hook(hh):
        if hh.if_straddle_pending():
            pending["n"] += 1

    h, stats, trace = await _run(dut, words, halt, hook=hook)
    expect(h.reg(1) == 6, f"straddled addi x1={h.reg(1)}")
    expect(pending["n"] == 0,
           "the two-word IF window must present a lane-1 32-bit instruction "
           "without a straddle-assembly bubble")
    irs = [(pc, ir) for pc, ir in trace]
    expect((2, 0x00500093) in irs, f"canonical IR at PC 2 missing: {irs[:6]}")
    expect((6, 0x00108093) in irs, f"compressed IR at PC 6 missing: {irs[:6]}")


async def _case_control(dut):
    words, info = build_control()
    h, stats, trace = await _run(dut, words, info["halt"])
    dbg = {r: h.reg(r) for r in (2, 4, 5, 8, 10, 11, 13, 14, 12)}
    expect(h.reg(8) == 3, f"loop: x8={h.reg(8)!r}, expected 3")
    expect(h.reg(2) == 1, f"lane1: x2={h.reg(2)!r}, expected 1")
    expect(h.reg(4) == 0, f"C.J did not skip; x4=0x{h.reg(4)!r} regs={dbg}")
    expect(h.reg(5) == 0x600D, f"x5=0x{h.reg(5)!r} regs={dbg}")
    expect(h.reg(10) == info["after1"],
           f"C.JAL link 0x{h.reg(10):08x} != 0x{info['after1']:08x}")
    expect(h.reg(11) == info["after2"],
           f"C.JALR link 0x{h.reg(11):08x} != 0x{info['after2']:08x}")
    expect(h.reg(13) == info["after1"], f"x13=0x{h.reg(13)!r} regs={dbg}")
    expect(h.reg(14) == info["after2"], f"x14=0x{h.reg(14)!r} regs={dbg}")
    irs = dict((pc, ir) for pc, ir in trace)
    exp_ir = rvc_expand(ce_addi(11, -1))
    expect(irs.get(info["loop"] + 2) == exp_ir,
           f"compressed canonical IR at 0x{info['loop'] + 2:x} is "
           f"{irs.get(info['loop'] + 2)!r}, expected 0x{exp_ir:08x}; "
           f"retired={[(hex(p), hex(i)) for p, i in trace]}")
    expect(stats["trap_taken"] == 0, "control: unexpected trap")


async def _case_illegal_hint(dut):
    words, halt, info = build_illegal_hint()
    h, stats, trace = await _run(dut, words, halt)
    expect(h.reg(2) == 0x22, f"resume after illegal failed: x2={h.reg(2)}")
    expect(h.reg(7) == 0x33, f"resume after ebreak failed: x7={h.reg(7)}")
    expect(h.reg(1) == 0x55,
           f"HINTs modified state: x1={h.reg(1)!r}")
    expect(h.reg(9) == 0x0000_FFFE,
           f"mepc CSR write mask: x9=0x{h.reg(9):08x}, expected 0x0000fffe")
    base = _dtcm_idx(DTCM_BASE)
    mcause, mepc, mtval = h.dtcm(base), h.dtcm(base + 1), h.dtcm(base + 2)
    expect(mcause == 2, f"illegal mcause={mcause}")
    expect(mepc == info["bad"],
           f"illegal mepc=0x{mepc:08x}, expected 0x{info['bad']:08x}")
    expect(mtval == info["bad_half"],
           f"illegal mtval=0x{mtval:08x}, expected 0x{info['bad_half']:08x}")
    mcause, mepc, mtval = h.dtcm(base + 3), h.dtcm(base + 4), h.dtcm(base + 5)
    expect(mcause == 3, f"ebreak mcause={mcause}")
    expect(mepc == info["eb"],
           f"ebreak mepc=0x{mepc:08x}, expected 0x{info['eb']:08x}")
    expect(mtval == info["eb"],
           f"ebreak mtval=0x{mtval:08x}, expected 0x{info['eb']:08x}")
    expect(stats["trap_taken"] == 2, f"trap cycles={stats['trap_taken']}")


async def _case_btb_lanes(dut):
    words, halt, lane0, lane1, exit_pc = build_btb_lanes()
    h, stats, trace = await _run(dut, words, halt)
    expect(h.reg(2) == 1, f"x2={h.reg(2)}")
    e0 = h.btb_find(lane0)
    e1 = h.btb_find(lane1)
    expect(e0[0] and e1[0], f"BTB entries invalid: lane0={e0} lane1={e1}")
    expect(e0[1] == lane1, f"lane0 target 0x{e0[1]:08x} != 0x{lane1:08x}")
    expect(e1[1] == exit_pc, f"lane1 target 0x{e1[1]:08x} != 0x{exit_pc:08x}")
    expect(e0 != e1, "the two lanes aliased into one BTB entry")
    expect(stats["trap_taken"] == 0, "lane test trapped")


async def _case_predicted(dut):
    words, halt, head, bp = build_predicted()
    mis = []

    def hook(hh):
        if hh.EX_valid and hh.EX_PC == bp and hh.sig("ex_pred_mispredict"):
            mis.append(hh.cycle)

    h, stats, trace = await _run(dut, words, halt, hook=hook)
    expect(h.reg(8) == 4, f"x8={h.reg(8)!r}, expected 4")
    expect(len(mis) == 2, f"compressed loop mispredicts={len(mis)}, expected "
                          f"the cold entry and the loop exit")
    hit, target, cnt = h.btb_find(bp)
    expect(hit and target == head and cnt == 2,
           f"BTB entry {h.btb_find(bp)}, expected target 0x{head:08x} cnt=2 "
           f"after the loop-exit decrement")


_RUNNERS = {
    "alu_mem": _case_alu_mem,
    "straddle": _case_straddle,
    "control": _case_control,
    "illegal_hint": _case_illegal_hint,
    "btb_lanes": _case_btb_lanes,
    "predicted": _case_predicted,
}


@pytest.mark.parametrize("case", CASES)
def test_rvc(case):
    run_test("test_rvc", seed=0, case=case)


@cocotb.test()
async def rvc(dut):
    case = tb_case("alu_mem")
    expect(case in _RUNNERS, f"unknown rvc case {case}")
    await _RUNNERS[case](dut)
