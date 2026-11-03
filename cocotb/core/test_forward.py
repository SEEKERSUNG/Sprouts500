"""Directed EX/MEM->ID forwarding tests (rtl/core.v ``ex_fwd_ok``/``mem_fwd_ok``).

Before forwarding every RAW dependency at pipeline distance 0/1 stalled ID until
the producer reached WB.  With EX/MEM->ID forwarding the operand mux reads the
producer result directly out of EX or MEM.  Ordinary TCM loads are read at the
EX->MEM edge and also forward from MEM; bus loads and AMO/LR/SC still wait for
``bus_rdata_q``/WB:

    priority: x0 (hardwired) > EX > MEM > WB bypass > regfile
    EX  forwarded   : EX_valid && EX_rd_write_req && !EX_mem_read &&
                      !(divider in EX and not yet DIV_DONE)
    MEM forwarded   : ALU results plus ordinary ITCM/DTCM loads
    load-use stalls : 1 cycle at distance 0, 0 at distance 1 on TCM;
                      bus loads and AMO/LR/SC retain the WB-based stalls.

Each case builds a short program, runs it against the reference interpreter
(retirement trace + regfile + ITCM/DTCM/bus images) and checks cycle-level
behaviour through ``run_with_trace``'s ``stats``/``cycle_hook``.

Stall accounting is exact and per instruction: when a retirement is observed,
the ``raw_stall`` cycles recorded since the previous retirement are the cycles
in which the retired instruction waited in ID.  ``RetireTrace`` keeps those
per-PC.  Because a non-divider stall can only be a load-use stall, every stall
window that this suite does not expect is a test failure, and every expected
stall must name a load producer that is really a load in the image.  Divider
freeze cycles (``div_busy``) are reported separately: a distance-0 divider
consumer sits in ID for the whole divide, which is a ``pipe_freeze``, not a
forwarding failure.

Cases
-----
alu_ex        distance-0 ALU/mul/LUI/shift producers used on rs1, rs2 and both:
              zero raw_stall and the forwards are observed in ID.
alu_mem       producer one slot ahead (in MEM while the consumer is in ID):
              zero raw_stall, MEM forward observed.
precedence    EX beats MEM on the same rd, and an ordinary TCM load forwards
              from MEM while bus/atomic producers keep their WB-based stalls.
loaduse_depth exactly 1/0/0 consecutive raw_stall cycles at distance 0/1/2.
x0            producers writing x0 never leak into a consumer reading x0.
div           div/rem consumed immediately and at distance 1: forwarded at
              DIV_DONE, only divider-freeze stalls in the windows.
csr           csrr -> consumer, csrrs with a forwarded rs1, csrw with a mul
              producer operand.
jal_link      the first target instructions of ``jal x1`` read the link.
store_data    sb/sh/sw data from EX and MEM distance into DTCM, ITCM and the
              bus: exactly one store per access and the right data.
branch_ops    beq/bne/blt/bgeu operands from EX/MEM distance: no stall, the
              direction follows the forwarded operands.
trap_tval     misaligned lw/sw address from the immediately preceding addi
              (distance 0/1): cause 4/6, mtval = forwarded address, destination
              not written, memory unchanged.
trap_cancel   a faulting load followed by consumers of its rd: the consumers
              never retire and their destinations are not written.

CSR and trap programs contain SYSTEM instructions, which the reference
interpreter intentionally does not model (same policy as ``test_csr_trap.py``),
so those cases use hand-built retirement traces plus directed CSR/memory
checks instead of the interpreter.
"""

import pathlib
import random
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    BUS_BASE, DTCM_BASE, DTCM_WORDS, ITCM_DATA_BASE, ITCM_WORDS, Asm,
    BusSlave, CoreHarness, RV32Interp, check_trace, expect, tb_case,
)
from _runner import run_test  # noqa: E402

DTCM_SCRATCH = DTCM_BASE + 0x300     # store-data case scratch area
BUS_SLOT = BUS_BASE + 0x40           # store-data case bus slot
TARGET_OFF = 0x141                   # misaligned target address offset


# ---------------------------------------------------------------------------
# retirement trace with exact per-instruction stall accounting
# ---------------------------------------------------------------------------
class RetireTrace:
    """Retirement trace + per-instruction stall records.

    ``cycle_hook`` is called after every sampled cycle, before the retirement
    sample.  A ``raw_stall`` cycle is attributed to the instruction held in ID
    in that cycle (``ID_PC``): that is exactly the instruction whose operands
    are being waited on, so no retirement-window bookkeeping is involved
    (stalls happen while the producer is still in EX/MEM, i.e. before the
    producer itself retires).  ``div_busy`` is recorded per stall cycle so
    divider freeze cycles can be told apart from load-use stalls, and the
    EX/MEM PC of the cycle is kept so the responsible producer can be checked.
    """

    def __init__(self):
        self.retired = []            # PCs in retirement order
        self.stall_cycles = {}       # ID_PC -> [(div_busy, EX_PC, MEM_PC)]
        self._pc = None

    def hook(self, h):
        if h.raw_stall:
            self.stall_cycles.setdefault(h.ID_PC, []).append(
                (bool(h.div_busy),
                 h.EX_PC if h.EX_valid else None,
                 h.MEM_PC if h.MEM_valid else None))
        if h.WB_valid and h.WB_PC is not None and h.WB_PC != self._pc:
            self.retired.append(h.WB_PC)
            self._pc = h.WB_PC
        elif not h.WB_valid:
            self._pc = None

    def stalls_at(self, pc):
        """All raw_stall cycles in which ``pc`` was held in ID."""
        return self.stall_cycles.get(pc, [])

    def load_stalls_at(self, pc):
        """raw_stall cycles in ID for ``pc`` that are not divider freeze."""
        return [s for s in self.stalls_at(pc) if not s[0]]

    def retired_pcs(self):
        return list(self.retired)

    def total_stalls(self):
        return sum(len(v) for v in self.stall_cycles.values())


def _init_regs(a):
    """Give every integer register a known zero (untouched regs read X)."""
    for r in range(1, 30):
        a.addi(r, 0, 0)


def _zero_dtcm():
    return [0] * DTCM_WORDS


def _is_load(word):
    return (word & 0x7F) == 0x03


def _straight_trace(halt_pc):
    return list(range(0, halt_pc + 4, 4))


# ---------------------------------------------------------------------------
# run + compare framework
# ---------------------------------------------------------------------------
async def run_case(dut, words, exp, label, use_ref=True, dtcm=None,
                   bus_init=None, waits=(0, 0), max_cycles=60_000):
    """Run ``words`` on the DUT and compare against the oracle.

    ``exp`` keys consumed here: ``halt_pc``, ``producers`` (consumer PC ->
    list of load producer PCs, one per expected non-divider stall cycle),
    ``fwd`` (consumer PC -> list of forward expectations), ``regs`` (directed
    register checks) and, for ``use_ref=False``, ``expect_trace`` /
    ``regs_full``.  Returns a dict with the harness, trace, stats, reference
    and bus slave.
    """
    if dtcm is None:
        dtcm = exp.get("dtcm")
    if dtcm is None:
        dtcm = _zero_dtcm()
    # The inferred ITCM reads X for words that were never written; give the DUT
    # and the reference model the same fully defined image (program + zeros).
    image = list(words) + [0] * (ITCM_WORDS - len(words))
    slave = None
    ref = None
    if use_ref:
        ref = RV32Interp(image, dtcm_words=dtcm)
        if bus_init is not None:
            ref.mem.load_bus_bytes(bus_init)
        ref = ref.run()
    if bus_init is not None:
        slave = BusSlave(dut, dut.clk, size=0x1000, seed=0,
                         wait_range=waits, init_bytes=bus_init)

    h = CoreHarness(dut)
    h.load_program(image, zero_rest=True)
    h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    if slave is not None:
        slave.start()
    await h.reset()

    stats = {"raw_stall": 0, "IF_ID_hold": 0, "trap_taken": 0,
             "mem_dtcm_we": 0, "mem_itcm_we": 0}
    tr = RetireTrace()
    observed = {}
    fwd_specs = exp.get("fwd", {})

    def cycle_hook(hh):
        tr.hook(hh)
        # Under EX/MEM->ID forwarding the only reasons left for raw_stall are
        # a load producer in EX/MEM (its data is not available yet) or the
        # iterative divider still running in EX.
        if hh.raw_stall:
            justified = (
                (hh.EX_valid and hh.EX_mem_read and (hh.EX_rd or 0) != 0) or
                (hh.MEM_valid and hh.MEM_mem_read and (hh.MEM_rd or 0) != 0) or
                bool(hh.div_busy))
            expect(justified,
                   f"{label}: raw_stall with no load producer in EX/MEM and "
                   f"no divider busy (EX_PC=0x{hh.EX_PC or 0:08x}, "
                   f"MEM_valid={hh.MEM_valid}, EX_mem_read={hh.EX_mem_read}, "
                   f"MEM_mem_read={hh.MEM_mem_read})")
        if hh.ID_valid and hh.ID_PC in fwd_specs:
            for spec in fwd_specs[hh.ID_PC]:
                src = spec["src"]
                sel = hh.ex_fwd_ok if src == "EX" else hh.mem_fwd_ok
                rd_sig = hh.EX_rd if src == "EX" else hh.MEM_rd
                val = hh.ex_result if src == "EX" else hh.MEM_result
                if not sel:
                    continue
                if spec.get("rd") is not None and rd_sig != spec["rd"]:
                    continue
                if "rs1" in spec:
                    expect(hh.id_rs1_data == spec["rs1"],
                           f"{label}: consumer 0x{hh.ID_PC:08x} rs1="
                           f"0x{hh.id_rs1_data:08x}, expected 0x{spec['rs1']:08x}"
                           f" ({src} forward)")
                if "rs2" in spec:
                    expect(hh.id_rs2_data == spec["rs2"],
                           f"{label}: consumer 0x{hh.ID_PC:08x} rs2="
                           f"0x{hh.id_rs2_data:08x}, expected 0x{spec['rs2']:08x}"
                           f" ({src} forward)")
                if "value" in spec:
                    expect(val == spec["value"],
                           f"{label}: {src} forward 0x{val:08x} != "
                           f"0x{spec['value']:08x} for consumer "
                           f"0x{hh.ID_PC:08x}")
                if spec.get("nonzero"):
                    expect(val not in (None, 0),
                           f"{label}: {src} producer 0x{hh.ID_PC:08x} carried "
                           f"zero; the x0 check would be vacuous")
                observed.setdefault(hh.ID_PC, set()).add(src)

    trace = await h.run_with_trace(exp["halt_pc"], max_cycles=max_cycles,
                                   stats=stats, cycle_hook=cycle_hook)
    if slave is not None:
        slave.stop()
    pcs = [pc for pc, _ir in trace]
    exp["image"] = words

    if use_ref:
        check_trace(pcs, ref.trace, label=f"{label} trace")
        h.compare_regs(ref.regs, ignore=(0,), label=f"{label} regfile")
        h.compare_dtcm_words(ref.mem.dtcm_words(), label=f"{label} DTCM")
        # The DUT and the reference model got the same fully defined image
        # (program + zeros), so the final ITCM images are directly comparable.
        h.compare_itcm_words(ref.mem.itcm_words(), label=f"{label} ITCM")
        if slave is not None:
            expect(bytes(slave.mem) == bytes(ref.mem.bus),
                   f"{label}: bus image differs from the reference model")
    else:
        expect("expect_trace" in exp,
               f"{label}: no oracle trace supplied for a CSR/trap case")
        check_trace(pcs, exp["expect_trace"], label=f"{label} expected trace")
        if "regs_full" in exp:
            h.compare_regs(exp["regs_full"], ignore=(0, 30, 31),
                           label=f"{label} regfile")

    for idx, value in exp.get("regs", {}).items():
        got = h.reg(idx)
        expect(got == value,
               f"{label}: x{idx}=0x{got!r}, expected 0x{value:08x}")

    # Exact per-instruction stall counts.  A raw_stall cycle belongs to the
    # instruction held in ID during that cycle (``producers`` names the load
    # producer expected to be in EX or MEM for each such cycle).  An
    # unexpected non-divider stall anywhere is a failure.
    producers = exp.get("producers", {})
    for pc, want in producers.items():
        got = tr.load_stalls_at(pc)
        rec = [(div, ex_pc, mem_pc) for div, ex_pc, mem_pc in got]
        expect(len(got) == len(want),
               f"{label}: 0x{pc:08x} was held in ID for {len(got)} non-divider "
               f"stall cycle(s), expected {len(want)} (stall record: {rec})")
        for prod_pc in want:
            expect(_is_load(words[prod_pc // 4]),
                   f"{label}: 0x{pc:08x} expected to stall on 0x{prod_pc:08x}, "
                   f"which is not a load")
        for (div, ex_pc, mem_pc), prod_pc in zip(got, want):
            expect(ex_pc == prod_pc or mem_pc == prod_pc,
                   f"{label}: 0x{pc:08x} stall cycle saw EX=0x{ex_pc} MEM="
                   f"0x{mem_pc}, expected the load 0x{prod_pc:08x} in EX/MEM")
    # Some windows (e.g. behind a slow bus access) legitimately hold an
    # instruction in ID for a data-dependent number of cycles; those only
    # require that every stall cycle really has the named producer in EX/MEM.
    for pc, prod_pc in exp.get("producer_stall_pcs", {}).items():
        recs = tr.stalls_at(pc)
        expect(recs,
               f"{label}: 0x{pc:08x} was never held in ID behind producer "
               f"0x{prod_pc:08x}")
        for div, ex_pc, mem_pc in recs:
            expect(not div and (ex_pc == prod_pc or mem_pc == prod_pc),
                   f"{label}: 0x{pc:08x} stall cycle saw EX=0x{ex_pc} "
                   f"MEM=0x{mem_pc}, expected producer 0x{prod_pc:08x}")
    cancelled = set(exp.get("cancelled_pcs", ()))
    loose = set(exp.get("producer_stall_pcs", {}))
    for pc, records in tr.stall_cycles.items():
        if pc in producers:
            continue
        if pc in loose:
            continue
        if pc in cancelled:
            # Instructions flushed on their way to EX: a load-use hold on a
            # doomed instruction is harmless as long as it never retires
            # (checked by the case) and the hold itself is a load load-use.
            expect(all(not r[0] and _is_load(words[(r[1] or r[2]) // 4])
                       for r in records),
                   f"{label}: cancelled instruction 0x{pc:08x} stalled with "
                   f"record {records}")
            continue
        unexpected = [r for r in records if not r[0]]
        expect(not unexpected,
               f"{label}: 0x{pc:08x} was held in ID for {len(unexpected)} "
               f"non-divider stall cycle(s) that the case does not expect")

    # Every intended forward must actually have been selected at least once,
    # otherwise the case silently failed to exercise the path.
    for pc, specs in fwd_specs.items():
        for spec in specs:
            expect(spec["src"] in observed.get(pc, set()),
                   f"{label}: consumer 0x{pc:08x} was never fed by the "
                   f"{spec['src']} forward (observed "
                   f"{sorted(observed.get(pc, ()))})")

    expect(stats["trap_taken"] == exp.get("traps", 0),
           f"{label}: {stats['trap_taken']} trap cycle(s), expected "
           f"{exp.get('traps', 0)}")
    dut._log.info("%s: retired=%d raw_stall=%d IF_ID_hold=%d traps=%d "
                  "dtcm_we=%d itcm_we=%d", label, len(pcs), stats["raw_stall"],
                  stats["IF_ID_hold"], stats["trap_taken"],
                  stats["mem_dtcm_we"], stats["mem_itcm_we"])
    return {"h": h, "tr": tr, "stats": stats, "ref": ref, "slave": slave,
            "pcs": pcs, "observed": observed}


# ---------------------------------------------------------------------------
# builders: pure forwarding cases
# ---------------------------------------------------------------------------
def build_alu_ex(seed=0):
    dtcm = [0] * DTCM_WORDS
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 0x1234_5678)
    a.li(4, 5)
    a.li(25, 0x00FF_00FF)
    fwd = {}

    # rs1 operand forwarded from EX (addi producer, distance 0).
    p = a.here()
    a.addi(5, 3, 7)                      # x5 = 0x1234_567F
    c = a.here()
    a.add(5, 5, 4)                       # x5 = 0x1234_5684
    fwd[c] = [{"src": "EX", "rd": 5, "rs1": 0x1234_567F, "rs2": 5,
               "value": 0x1234_567F}]
    a.add(2, 2, 5)

    # rs2 operand forwarded from EX (add producer, distance 0).
    p = a.here()
    a.add(6, 3, 4)                       # x6 = 0x1234_567D
    c = a.here()
    a.xor(6, 5, 6)                       # 0x1234_5684 ^ 0x1234_567D = 0xF9
    fwd[c] = [{"src": "EX", "rd": 6, "rs2": 0x1234_567D,
               "value": 0x1234_567D}]
    a.add(2, 2, 6)

    # Both operands are the producer rd (LUI, distance 0).
    p = a.here()
    a.lui(7, 0xABCDE)                    # x7 = 0xABCD_E000
    c = a.here()
    a.add(7, 7, 7)                       # x7 = 0x579B_C000
    fwd[c] = [{"src": "EX", "rd": 7, "rs1": 0xABCD_E000,
               "rs2": 0xABCD_E000, "value": 0xABCD_E000}]
    a.add(2, 2, 7)

    # Shift producer, rs1 forwarded from EX.
    p = a.here()
    a.slli(8, 3, 8)                      # x8 = 0x3456_7800
    c = a.here()
    a.or_(8, 8, 25)                      # x8 = 0x3456_78FF
    fwd[c] = [{"src": "EX", "rd": 8, "rs1": 0x3456_7800,
               "value": 0x3456_7800}]
    a.add(2, 2, 8)

    # Multiply producer (EX mul result, no divider freeze).
    p = a.here()
    a.mul(9, 3, 3)
    mul_val = (0x1234_5678 * 0x1234_5678) & 0xFFFFFFFF
    c = a.here()
    a.srli(9, 9, 4)
    fwd[c] = [{"src": "EX", "rd": 9, "rs1": mul_val, "value": mul_val}]
    a.add(2, 2, 9)
    a.halt()

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {6: 0xF9, 7: (0xABCD_E000 * 2) & 0xFFFFFFFF,
                    8: 0x3456_7800 | 0x00FF_00FF, 9: mul_val >> 4}}
    return a.assemble(), exp


def build_alu_mem(seed=0):
    dtcm = [0] * DTCM_WORDS
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 0x0F0F_0F0F)
    a.li(4, 0x00FF_00FF)
    fwd = {}

    # Producer one slot ahead: in MEM while the consumer is in ID.
    p = a.here()
    a.addi(5, 3, 0x11)                   # x5 = 0x0F0F_0F20
    a.addi(6, 3, 0)                      # filler, different rd
    c = a.here()
    a.add(6, 5, 4)                       # 0x0F0F_0F20 + 0x00FF_00FF
    fwd[c] = [{"src": "MEM", "rd": 5, "rs1": 0x0F0F_0F20,
               "rs2": 0x00FF_00FF, "value": 0x0F0F_0F20}]
    a.add(2, 2, 6)

    # MEM forward of a LUI result, consumer uses it on both operands.
    p = a.here()
    a.lui(7, 0x77777)                    # x7 = 0x7777_7000
    a.addi(8, 3, 1)
    c = a.here()
    a.xor(8, 7, 7)                       # 0
    fwd[c] = [{"src": "MEM", "rd": 7, "rs1": 0x7777_7000,
               "rs2": 0x7777_7000, "value": 0x7777_7000}]
    a.add(2, 2, 8)
    a.halt()

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {6: (0x0F0F_0F20 + 0x00FF_00FF) & 0xFFFFFFFF, 8: 0}}
    return a.assemble(), exp


def build_precedence(seed=0):
    dtcm = _zero_dtcm()
    # The fault handlers write DTCM+0x200/0x260, so keep the data lower.
    dtcm[0x40 // 4] = 0xDEAD_BEEF
    dtcm[0x44 // 4] = 0xCAFE_0001
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(10, DTCM_BASE + 0x40)
    fwd = {}
    producers = {}

    # Load first, ALU second: both write x5.  EX (the younger addi) must win
    # over MEM (the load); the intervening ALU slot also lets the TCM load
    # forward from MEM without a stall.
    l = a.here()
    a.lw(5, 10, 0)                       # x5 = 0xDEAD_BEEF
    p_ex = a.here()
    a.addi(5, 0, 9)
    c = a.here()
    a.add(6, 5, 0)
    fwd[c] = [{"src": "EX", "rd": 5, "rs1": 9, "value": 9}]
    # The load and the addi share x5: the addi in EX already carries the
    # right value, and the TCM load can forward from MEM without a stall.
    producers[c] = []

    # ALU first, load second: the load value must win (it is younger).  A TCM
    # load forwards from MEM, leaving one EX-stage dependency cycle.
    p_alu = a.here()
    a.addi(5, 0, 1)
    l = a.here()
    a.lw(5, 10, 4)                       # x5 = 0xCAFE_0001
    c2 = a.here()
    a.add(7, 5, 0)
    producers[c2] = [l]

    a.add(2, 2, 6)
    a.add(2, 2, 7)
    a.halt()

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": producers,
           "regs": {6: 9, 7: 0xCAFE_0001}, "dtcm": dtcm,
           "precedence": (c, p_ex, p_alu, c2)}
    return a.assemble(), exp


def build_loaduse_depth(seed=0):
    dtcm = _zero_dtcm()
    for i in range(3):
        dtcm[0x80 // 4 + i] = 0x1000_0000 + i
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(10, DTCM_BASE + 0x80)
    producers = {}
    pcs = {}

    # distance 0: exactly one raw_stall cycle (the EX-stage dependency).
    l = a.here()
    a.lw(5, 10, 0)
    c = a.here()
    a.addi(6, 5, 1)
    producers[c] = [l]
    pcs["d0"] = (l, c)

    # distance 1: the MEM load forwards directly, so no stall.
    l = a.here()
    a.lw(7, 10, 4)
    a.nop()
    c = a.here()
    a.addi(8, 7, 1)
    producers[c] = []
    pcs["d1"] = (l, c)

    # distance 2: no stall (WB bypass only).
    l = a.here()
    a.lw(9, 10, 8)
    a.nop()
    a.nop()
    c = a.here()
    a.addi(11, 9, 1)
    pcs["d2"] = (l, c)

    a.add(2, 2, 6)
    a.add(2, 2, 8)
    a.add(2, 2, 11)
    a.halt()
    exp = {"halt_pc": a.halt_store_pc, "fwd": {}, "producers": producers,
           "regs": {6: 0x1000_0001, 8: 0x1000_0002, 11: 0x1000_0003},
           "dtcm": dtcm, "depth_pcs": pcs}
    return a.assemble(), exp


def build_x0(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 0x55)
    a.li(4, 0xAA)
    fwd = {}

    # EX producer writes x0 with a non-zero result: the mux masks it.
    p = a.here()
    a.addi(0, 3, 5)
    c = a.here()
    a.add(6, 0, 0)
    fwd[c] = [{"src": "EX", "rd": 0, "rs1": 0, "rs2": 0, "nonzero": True}]
    # One slot later the x0 producer is in MEM; rs2 must come from the regfile.
    c = a.here()
    a.add(7, 0, 3)
    fwd[c] = [{"src": "MEM", "rd": 0, "rs1": 0, "rs2": 0x55,
               "nonzero": True}]

    # LUI x0, then a shift reading x0 twice.
    p = a.here()
    a.lui(0, 0xFFFFF)
    c = a.here()
    a.sll(8, 0, 0)
    fwd[c] = [{"src": "EX", "rd": 0, "rs1": 0, "rs2": 0, "nonzero": True}]

    # Producer with rd != 0 next to a consumer whose other operand is x0.
    a.addi(3, 0, 0x42)
    p = a.here()
    a.addi(12, 3, 1)                     # x12 = 0x43
    a.nop()
    c = a.here()
    a.add(13, 12, 0)                     # x13 = 0x43
    fwd[c] = [{"src": "MEM", "rd": 12, "rs1": 0x43, "rs2": 0,
               "value": 0x43}]
    a.add(2, 2, 13)
    a.halt()

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {6: 0, 7: 0x55, 8: 0, 13: 0x43}}
    return a.assemble(), exp


def build_div(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(13, 0x1234_5678)
    a.li(14, 0x123)
    a.li(15, -7)
    a.li(16, 2)
    fwd = {}

    # Div result consumed immediately: the consumer waits in ID while the
    # divider runs (div_busy freeze) and gets the result at DIV_DONE.
    div_pc = a.here()
    a.div(10, 13, 14)
    c0 = a.here()
    a.addi(11, 10, 1)
    fwd[c0] = [{"src": "EX", "rd": 10}]

    # Remainder at distance 1 (producer in MEM).
    rem_pc = a.here()
    a.rem(12, 13, 14)
    a.nop()
    c1 = a.here()
    a.add(17, 12, 0)
    fwd[c1] = [{"src": "MEM", "rd": 12}]

    # Negative signed divide at distance 1, producer on rs2.
    divn_pc = a.here()
    a.div(18, 15, 16)
    a.nop()
    c2 = a.here()
    a.sub(19, 2, 18)
    fwd[c2] = [{"src": "MEM", "rd": 18}]

    # distance 2: WB bypass, no stall and no EX/MEM forward needed.
    remu_pc = a.here()
    a.remu(20, 13, 14)
    a.nop()
    a.nop()
    c3 = a.here()
    a.add(21, 20, 0)
    a.add(2, 2, 21)
    a.halt()

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "pcs": {"div": div_pc, "rem": rem_pc, "divn": divn_pc,
                   "remu": remu_pc, "c0": c0, "c1": c1, "c2": c2, "c3": c3}}
    return a.assemble(), exp


def build_csr(seed=0):
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 0x0123_4000)                 # lui + addi
    fwd = {}
    regs = {}

    # csrw with a forwarded operand, then csrr -> consumer at EX distance.
    p = a.here()
    a.addi(5, 3, 0x567)                  # x5 = 0x0123_4567
    a.csrw(0x340, 5)                     # mscratch = 0x0123_4567
    l = a.here()
    a.csrr(6, 0x340)
    c = a.here()
    a.addi(7, 6, 1)                      # csrr result forwarded out of EX
    fwd[c] = [{"src": "EX", "rd": 6, "rs1": 0x0123_4567,
               "value": 0x0123_4567}]

    # csrrs with a forwarded rs1 operand.
    p = a.here()
    a.addi(8, 0, 0x0F0)
    c = a.here()
    a.csrrs(9, 0x340, 8)                 # x9 = old 0x0123_4567
    fwd[c] = [{"src": "EX", "rd": 8, "rs1": 0x0F0, "value": 0x0F0}]
    a.csrr(10, 0x340)                    # 0x0123_45F7
    c = a.here()
    a.add(11, 9, 10)                     # EX forward x10, MEM forward x9
    fwd[c] = [{"src": "EX", "rd": 10, "rs1": 0x0123_4567,
               "rs2": 0x0123_45F7, "value": 0x0123_45F7},
              {"src": "MEM", "rd": 9, "rs1": 0x0123_4567,
               "rs2": 0x0123_45F7, "value": 0x0123_4567}]

    # csrw with a mul producer operand; mepc masks the low two bits.
    p = a.here()
    a.mul(12, 8, 8)                      # 0xE100
    c = a.here()
    a.csrw(0x341, 12)
    fwd[c] = [{"src": "EX", "rd": 12, "rs1": 0xE100, "value": 0xE100}]
    a.csrr(13, 0x341)
    a.halt()
    # mepc is WARL: the low two bits are always zero.
    mepc_val = 0xE100
    regs_full = {3: 0x0123_4000, 5: 0x0123_4567, 6: 0x0123_4567,
                 7: 0x0123_4568, 8: 0x0F0, 9: 0x0123_4567,
                 10: 0x0123_45F7, 11: 0x0246_8B5E, 12: 0xE100,
                 13: mepc_val}

    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {},
           "expect_trace": _straight_trace(a.halt_store_pc),
           "regs_full": regs_full,
           "csrs": {"mscratch": 0x0123_45F7, "mepc": mepc_val}}
    return a.assemble(), exp


def build_jal_link(seed=0):
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    fwd = {}
    jal_pc = a.here()
    link = jal_pc + 4
    a.jal(1, "target")
    a.li(2, 0xBAD)                       # dead: jal skips it
    a.label("target")
    t0_pc = a.here()
    a.addi(5, 1, 0)                      # first target instruction reads x1
    t1_pc = a.here()
    fwd[t1_pc] = [{"src": "EX", "rd": 5, "rs1": link, "value": link}]
    a.add(6, 1, 5)                       # second one reads x1 and t0
    a.add(2, 5, 6)
    a.halt()
    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {5: link, 6: 2 * link, 2: 3 * link},
           "link_pcs": (jal_pc, t0_pc, t1_pc)}
    return a.assemble(), exp


def build_store_data(seed=0):
    rng = random.Random(seed * 13 + 5)
    v_sw = rng.getrandbits(32)
    v_sh = rng.getrandbits(16) | 0x8000
    v_itcm = rng.getrandbits(32)
    v_bus = rng.getrandbits(32)
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(10, DTCM_SCRATCH)
    a.li(11, DTCM_SCRATCH + 8)
    a.li(12, DTCM_SCRATCH + 12)
    a.li(22, DTCM_SCRATCH + 16)
    a.li(20, ITCM_DATA_BASE + 0x80)
    a.li(21, BUS_SLOT)
    producers = {}
    stores = {}

    # DTCM sw: data produced by an addi at distance 0 (EX forward).
    a.li(3, v_sw)
    stores["dtcm_sw"] = a.here()
    a.sw(3, 10, 0)
    l = a.here()
    a.lw(4, 10, 0)
    c = a.here()
    a.add(5, 5, 4)
    producers[c] = [l]

    # DTCM sh: data produced one slot ahead (MEM forward).
    a.li(6, v_sh)                        # addi (16-bit value > 2047)
    a.nop()
    stores["dtcm_sh"] = a.here()
    a.sh(6, 11, 0)
    l = a.here()
    a.lhu(7, 11, 0)
    c = a.here()
    a.add(5, 5, 7)
    producers[c] = [l]

    # DTCM sb: data produced by mul at distance 0 (EX forward).
    a.mul(8, 6, 6)
    mul_byte = (v_sh * v_sh) & 0xFF
    stores["dtcm_sb"] = a.here()
    a.sb(8, 22, 0)
    l = a.here()
    a.lbu(9, 22, 0)
    c = a.here()
    a.add(5, 5, 9)
    producers[c] = [l]

    # ITCM data window sw at distance 0 (EX forward).
    a.li(14, v_itcm)
    stores["itcm_sw"] = a.here()
    a.sw(14, 20, 0)
    l = a.here()
    a.lw(15, 20, 0)
    c = a.here()
    a.add(5, 5, 15)
    producers[c] = [l]

    # Bus sw at distance 1 (MEM forward), while MEM waits on the slave.
    a.li(16, v_bus)
    a.nop()
    stores["bus_sw"] = a.here()
    a.sw(16, 21, 0)
    l = a.here()
    a.lw(17, 21, 0)
    c = a.here()
    a.add(5, 5, 17)
    # The bus read freezes MEM for a slave-dependent number of cycles, so the
    # consumer's ID hold is data-dependent; only require that the load is the
    # producer behind every one of those cycles.
    loose_stall = {c: l}

    a.halt()
    bus_init = bytes(rng.getrandbits(8) for _ in range(0x100))
    exp = {"halt_pc": a.halt_store_pc, "fwd": {}, "producers": producers,
           "producer_stall_pcs": loose_stall, "stores": stores,
           "regs": {4: v_sw, 7: v_sh, 9: mul_byte, 15: v_itcm, 17: v_bus,
                    5: (v_sw + v_sh + mul_byte + v_itcm + v_bus) & 0xFFFFFFFF},
           "dtcm_slots": {"sw": (DTCM_SCRATCH - DTCM_BASE) // 4,
                          "sh": (DTCM_SCRATCH + 8 - DTCM_BASE) // 4,
                          "sb": (DTCM_SCRATCH + 16 - DTCM_BASE) // 4},
           "values": {"sw": v_sw, "sh": v_sh, "sb": mul_byte,
                      "itcm": v_itcm, "bus": v_bus},
           "n_dtcm_stores": 3, "n_itcm_stores": 2}
    return a.assemble(), exp, bus_init


def build_branch_ops(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(20, 0x10)
    fwd = {}
    branch_pcs = []
    x2 = 0

    def block(rs1_val, rs2_val, op, filler, taken):
        nonlocal x2
        a.li(5, rs1_val)
        a.li(6, rs2_val)
        if filler:
            a.nop()
        br = a.here()
        branch_pcs.append(br)
        label = f"B{len(branch_pcs)}"
        getattr(a, op)(5, 6, label)
        a.addi(2, 2, 0 if taken else 1)
        a.j(f"E{label}")
        a.label(label)
        a.addi(2, 2, 0x10)
        a.label(f"E{label}")
        x2 += 0x10 if taken else 1
        return br

    # EX-distance operands (both producers a single slot ahead of the branch).
    b_eq = block(0x11, 0x11, "beq", False, True)
    fwd[b_eq] = [{"src": "EX", "rd": 6, "rs1": 0x11, "rs2": 0x11,
                  "value": 0x11},
                 {"src": "MEM", "rd": 5, "rs1": 0x11, "value": 0x11}]
    block(0x11, 0x12, "bne", False, True)
    block(-1, 1, "blt", False, True)          # signed: -1 < 1
    block(0xFFFF_FFFF, 1, "bgeu", False, True)  # unsigned: max >= 1
    block(0x11, 0x12, "beq", False, False)
    block(1, 0xFFFF_FFFF, "bgeu", False, False)
    block(1, -1, "blt", False, False)         # signed: 1 < -1 is false
    # MEM-distance operands (one filler instruction between).
    block(0x11, 0x11, "beq", True, True)
    b_ne_m = block(0x11, 0x11, "bne", True, False)
    fwd[b_ne_m] = [{"src": "MEM", "rd": 6, "rs1": 0x11, "rs2": 0x11,
                    "value": 0x11}]

    a.halt()
    exp = {"halt_pc": a.halt_store_pc, "fwd": fwd, "producers": {},
           "regs": {2: x2}, "branch_pcs": branch_pcs}
    return a.assemble(), exp


# ---------------------------------------------------------------------------
# builders: CSR / trap cases (no reference interpreter)
# ---------------------------------------------------------------------------
def _trap_tval_program(kind):
    """One misaligned access whose address comes from the preceding addi.

    ``kind``: ``load`` (lw, cause 4, distance 0), ``load_d1`` (same at
    distance 1) or ``store`` (sw, cause 6, distance 0).
    """
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(10, 0)
    a.la(5, "handler")
    a.csrw(0x305, 5)
    a.li(28, DTCM_BASE + 0x40)
    if kind == "store":
        a.li(6, 0xBEEF)
    p = a.here()
    a.addi(28, 28, 0x101)                # DTCM + 0x141, misaligned
    if kind == "load_d1":
        a.nop()
    fault_pc = a.here()
    if kind == "store":
        a.sw(6, 28, 0)
        cause = 6
    else:
        a.lw(5, 28, 0)
        cause = 4
    after_pc = a.here()
    a.addi(10, 10, 0x123)
    a.halt()

    a.label("handler")
    h_start = a.here()
    a.csrr(20, 0x342)                    # mcause
    a.addi(21, 20, 0)                    # EX forward of mcause
    a.csrr(22, 0x343)                    # mtval
    a.add(23, 22, 0)                     # EX forward of mtval
    a.csrr(24, 0x341)                    # mepc at trap time
    a.li(11, DTCM_BASE + 0x200)
    a.sw(20, 11, 0)
    a.sw(21, 11, 4)
    a.sw(22, 11, 8)
    a.sw(23, 11, 12)
    a.csrr(25, 0x341)
    a.addi(25, 25, 4)                    # skip the faulting instruction
    a.csrw(0x341, 25)
    a.mret()
    h_end = a.here()

    tval = DTCM_BASE + TARGET_OFF
    trace = (list(range(0, fault_pc, 4)) +
             list(range(h_start, h_end, 4)) +
             list(range(after_pc, a.halt_store_pc + 4, 4)))
    exp = {"halt_pc": a.halt_store_pc,
           "fwd": {fault_pc: [{"src": "EX", "rd": 28}]}
                  if kind != "load_d1" else
                  {fault_pc: [{"src": "MEM", "rd": 28}]},
           "producers": {}, "regs": {},
           "expect_trace": trace,
           "trap": {"kind": kind, "fault_pc": fault_pc, "cause": cause,
                    "tval": tval, "after_pc": after_pc, "handler": h_start,
                    "dtcm_base": 0x200},
           "traps": 1}
    return a.assemble(), exp


def build_trap_cancel(seed=0):
    """Faulting load + two consumers of its rd, both flushed by the trap."""
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(10, 0)
    a.la(5, "handler")
    a.csrw(0x305, 5)
    a.li(28, DTCM_BASE + 0x40)
    p = a.here()
    a.addi(28, 28, 0x101)
    fault_pc = a.here()
    a.lw(7, 28, 0)                       # faults: misaligned, cause 4
    con1 = a.here()
    a.addi(8, 7, 1)                      # must never retire
    con2 = a.here()
    a.add(9, 7, 0)                       # must never retire
    resume_pc = a.here()
    a.addi(10, 10, 0x456)
    a.halt()

    a.label("handler")
    h_start = a.here()
    a.csrr(20, 0x342)
    a.csrr(22, 0x343)
    a.li(11, DTCM_BASE + 0x260)
    a.sw(20, 11, 0)
    a.sw(22, 11, 4)
    a.csrr(25, 0x341)
    a.addi(25, 25, 12)                   # skip load + both consumers
    a.csrw(0x341, 25)
    a.mret()
    h_end = a.here()

    tval = DTCM_BASE + TARGET_OFF
    trace = (list(range(0, fault_pc, 4)) +
             list(range(h_start, h_end, 4)) +
             list(range(resume_pc, a.halt_store_pc + 4, 4)))
    exp = {"halt_pc": a.halt_store_pc,
           "fwd": {fault_pc: [{"src": "EX", "rd": 28}]},
           # The first consumer is in ID in the cycle the faulting load is in
           # EX, so the load-use detector holds it once before the redirect.
           "producers": {}, "regs": {},
           "cancelled_pcs": (con1, con2),
           "expect_trace": trace,
           "trap": {"kind": "cancel", "fault_pc": fault_pc, "cause": 4,
                    "tval": tval, "resume": resume_pc, "dtcm_base": 0x260,
                    "consumers": (con1, con2), "handler": h_start},
           "traps": 1}
    return a.assemble(), exp


# ---------------------------------------------------------------------------
# cocotb entry points
# ---------------------------------------------------------------------------
CASES = ("alu_ex", "alu_mem", "precedence", "loaduse_depth", "x0", "div",
         "csr", "jal_link", "store_data", "branch_ops", "trap_tval",
         "trap_cancel")


@pytest.mark.parametrize("case", CASES)
def test_forward(case):
    run_test("test_forward", seed=0, case=case)


@cocotb.test()
async def fwd(dut):
    case = tb_case("alu_ex")
    expect(case in CASES, f"unknown forward case {case}")
    await _RUNNERS[case](dut, case)


async def _run_plain(dut, case, builder):
    words, exp = builder(0)
    res = await run_case(dut, words, exp, f"forward {case}")
    _check_common(dut, case, res, exp)
    return res, exp


async def _run_alu_ex(dut, case):
    res, exp = await _run_plain(dut, case, build_alu_ex)
    expect(res["stats"]["raw_stall"] == 0,
           f"alu_ex: {res['stats']['raw_stall']} raw_stall cycles for "
           f"ALU/mul producers (forwarding should cover them)")
    dut._log.info("alu_ex: x5=0x%08x x6=0x%08x x7=0x%08x x8=0x%08x "
                  "x9=0x%08x", *[res["h"].reg(i) or 0 for i in (5, 6, 7, 8, 9)])


async def _run_alu_mem(dut, case):
    res, exp = await _run_plain(dut, case, build_alu_mem)
    expect(res["stats"]["raw_stall"] == 0,
           f"alu_mem: {res['stats']['raw_stall']} raw_stall cycles")
    dut._log.info("alu_mem: x6=0x%08x x8=0x%08x", res["h"].reg(6) or 0,
                  res["h"].reg(8) or 0)


async def _run_precedence(dut, case):
    res, exp = await _run_plain(dut, case, build_precedence)
    h, tr = res["h"], res["tr"]
    c_ex, _p_ex, _p_alu, c_mem = exp["precedence"]
    expect(h.reg(6) == 9,
           f"precedence: x6=0x{h.reg(6)!r}, expected 9 (younger EX producer "
           f"must beat the load in MEM)")
    expect(len(tr.load_stalls_at(c_ex)) == 0,
           f"precedence: the first consumer was held in ID "
           f"{len(tr.load_stalls_at(c_ex))} cycle(s); with an ALU instruction "
           f"between the load and the consumer the TCM load forwards from MEM "
           f"({tr.load_stalls_at(c_ex)})")
    expect(h.reg(7) == 0xCAFE_0001,
           f"precedence: x7=0x{h.reg(7)!r}, expected the younger load value")
    expect(len(tr.load_stalls_at(c_mem)) == 1,
           "precedence: the load-over-ALU consumer did not stall once in EX")
    dut._log.info("precedence: x6=9 (EX>MEM), x7=0x%08x (load via WB), "
                  "stalls first=%d second=%d", h.reg(7),
                  len(tr.load_stalls_at(c_ex)), len(tr.load_stalls_at(c_mem)))


async def _run_loaduse_depth(dut, case):
    res, exp = await _run_plain(dut, case, build_loaduse_depth)
    tr = res["tr"]
    want = {"d0": 1, "d1": 0, "d2": 0}
    got = {}
    for key, (_l, cons) in exp["depth_pcs"].items():
        got[key] = len(tr.load_stalls_at(cons))
        expect(got[key] == want[key],
               f"loaduse_depth: {key} consumer stalled {got[key]} cycle(s), "
               f"expected {want[key]} (all cycles {len(tr.stalls_at(cons))})")
    expect(res["stats"]["raw_stall"] == 1,
           f"loaduse_depth: raw_stall={res['stats']['raw_stall']}, expected 1")
    expect(res["h"].reg(6) == 0x1000_0001 and res["h"].reg(8) == 0x1000_0002
           and res["h"].reg(11) == 0x1000_0003, "loaduse_depth: wrong values")
    dut._log.info("loaduse_depth: stalls d0=%d d1=%d d2=%d", got["d0"],
                  got["d1"], got["d2"])


async def _run_x0(dut, case):
    # The x0 producer must not write DTCM with a zero word (the case is really
    # about the regfile); build_x0's DTCM is unused.
    await _run_plain(dut, case, build_x0)


async def _run_div(dut, case):
    words, exp = build_div(0)
    res = await run_case(dut, words, exp, "forward div")
    _check_common(dut, "div", res, exp)
    h, tr, stats = res["h"], res["tr"], res["stats"]
    pcs = exp["pcs"]
    for name in ("div", "rem", "divn", "remu"):
        expect(pcs[name] in tr.retired_pcs(),
               f"div: {name} at 0x{pcs[name]:08x} did not retire exactly once")
    # The distance-0 consumer sits in ID through the divider freeze; none of
    # its stall cycles may be a load-use stall.
    c0_stalls = tr.stalls_at(pcs["c0"])
    expect(len(tr.load_stalls_at(pcs["c0"])) == 0,
           f"div: the immediate consumer had "
           f"{len(tr.load_stalls_at(pcs['c0']))} non-divider stall cycle(s)")
    expect(len(c0_stalls) > 0,
           "div: the immediate consumer was never frozen in ID during the "
           "divide (div_busy/raw_stall interaction untested)")
    for name in ("c1", "c2", "c3"):
        expect(len(tr.stalls_at(pcs[name])) == 0,
               f"div: consumer {name} at 0x{pcs[name]:08x} stalled "
               f"{len(tr.stalls_at(pcs[name]))} cycle(s) although the "
               f"producer had left EX")
    expect(stats["raw_stall"] > 0,
           "div: no raw_stall cycles at all; the divider freeze window was "
           "not exercised")
    dut._log.info("div: x10=%d x11=%d x12=%d x17=%d x19=%d x21=%d "
                  "c0 stalls=%d (all divider), c1/c2/c3 stalls=0",
                  *[h.reg(i) or 0 for i in (10, 11, 12, 17, 19, 21)],
                  len(c0_stalls))


async def _run_csr(dut, case):
    words, exp = build_csr(0)
    res = await run_case(dut, words, exp, "forward csr", use_ref=False)
    _check_common(dut, "csr", res, exp)
    h = res["h"]
    for name, value in exp["csrs"].items():
        got = getattr(h, name)
        expect(got == value, f"csr: {name}=0x{got!r}, expected 0x{value:08x}")
    expect(h.mscratch == 0x0123_45F7,
           f"csr: mscratch=0x{h.mscratch!r}")
    dut._log.info("csr: x6=0x%08x x7=0x%08x x9=0x%08x x10=0x%08x "
                  "x11=0x%08x x12=0x%08x x13=0x%08x mscratch=0x%08x",
                  *[h.reg(i) or 0 for i in (6, 7, 9, 10, 11, 12, 13)],
                  h.mscratch or 0)


async def _run_jal_link(dut, case):
    res, exp = await _run_plain(dut, case, build_jal_link)
    h, tr = res["h"], res["tr"]
    jal_pc, t0_pc, t1_pc = exp["link_pcs"]
    link = jal_pc + 4
    expect(h.reg(5) == link, f"jal_link: x5=0x{h.reg(5)!r} != 0x{link:08x}")
    expect(h.reg(6) == 2 * link, f"jal_link: x6=0x{h.reg(6)!r}")
    expect(len(tr.stalls_at(t0_pc)) == 0,
           f"jal_link: first target instruction stalled "
           f"{len(tr.stalls_at(t0_pc))} cycle(s) on the link")
    expect(len(tr.stalls_at(t1_pc)) == 0,
           f"jal_link: second target instruction stalled "
           f"{len(tr.stalls_at(t1_pc))} cycle(s) on the link")
    dut._log.info("jal_link: link=0x%08x, no stalls after the redirect", link)


async def _run_store_data(dut, case):
    words, exp, bus_init = build_store_data(0)
    # One wait cycle on every access: the bus store's consumer is held in ID
    # for two cycles (one bus_wait + one load-use), so the data-forward path is
    # checked in a cycle where the consumer really waits.
    res = await run_case(dut, words, exp, "forward store_data",
                         bus_init=bus_init, waits=(1, 1))
    _check_common(dut, "store_data", res, exp)
    h, stats, slave = res["h"], res["stats"], res["slave"]
    slots = exp["dtcm_slots"]
    vals = exp["values"]
    expect(h.dtcm(slots["sw"]) == vals["sw"],
           f"store_data: DTCM sw got 0x{h.dtcm(slots['sw'])!r}, expected "
           f"0x{vals['sw']:08x}")
    expect(h.dtcm(slots["sh"]) & 0xFFFF == vals["sh"],
           f"store_data: DTCM sh halfword got 0x{h.dtcm(slots['sh'])!r}, "
           f"expected 0x{vals['sh']:04x}")
    expect(h.dtcm(slots["sb"]) & 0xFF == vals["sb"],
           f"store_data: DTCM sb byte got 0x{h.dtcm(slots['sb'])!r}, "
           f"expected 0x{vals['sb']:02x}")
    expect(h.itcm((ITCM_DATA_BASE + 0x80) // 4) == vals["itcm"],
           "store_data: ITCM store data wrong")
    expect(len(slave.writes) == 1,
           f"store_data: {len(slave.writes)} bus writes, expected exactly one")
    expect(slave.writes[0].wdata == vals["bus"] and
           slave.writes[0].wstrb == 0xF and
           slave.writes[0].addr == BUS_SLOT,
           f"store_data: bus write {slave.writes[0]} != data 0x{vals['bus']:08x}"
           f" at 0x{BUS_SLOT:08x}")
    expect(stats["mem_dtcm_we"] == exp["n_dtcm_stores"],
           f"store_data: mem_dtcm_we high {stats['mem_dtcm_we']} cycles, "
           f"expected {exp['n_dtcm_stores']} (one per DTCM store)")
    expect(stats["mem_itcm_we"] == exp["n_itcm_stores"],
           f"store_data: mem_itcm_we high {stats['mem_itcm_we']} cycles, "
           f"expected {exp['n_itcm_stores']} (ITCM store + halt mailbox)")
    dut._log.info("store_data: dtcm sw=0x%08x sh=0x%04x sb=0x%02x "
                  "itcm=0x%08x bus=0x%08x writes=%d",
                  h.dtcm(slots["sw"]), h.dtcm(slots["sh"]) & 0xFFFF,
                  h.dtcm(slots["sb"]) & 0xFF,
                  h.itcm((ITCM_DATA_BASE + 0x80) // 4),
                  slave.writes[0].wdata, len(slave.writes))


async def _run_branch_ops(dut, case):
    res, exp = await _run_plain(dut, case, build_branch_ops)
    h, tr = res["h"], res["tr"]
    expect(h.reg(2) == exp["regs"][2],
           f"branch_ops: x2=0x{h.reg(2)!r}, expected 0x{exp['regs'][2]:x} "
           f"(wrong branch direction)")
    for br in exp["branch_pcs"]:
        expect(len(tr.stalls_at(br)) == 0,
               f"branch_ops: branch at 0x{br:08x} stalled "
               f"{len(tr.stalls_at(br))} cycle(s) on a non-load producer")
    expect(res["stats"]["raw_stall"] == 0,
           f"branch_ops: {res['stats']['raw_stall']} raw_stall cycles")
    dut._log.info("branch_ops: x2=0x%x over %d branches, no stalls",
                  h.reg(2), len(exp["branch_pcs"]))


async def _run_trap_tval(dut, case):
    # One program per kind, each needing its own reset: run sequentially.
    for kind in ("load", "load_d1", "store"):
        words, exp = _trap_tval_program(kind)
        res = await run_case(dut, words, exp, f"forward trap_tval {kind}",
                             use_ref=False, max_cycles=20_000)
        _check_trap(dut, kind, res, exp)
        exp["after_pc_check"] = True


async def _run_trap_cancel(dut, case):
    words, exp = build_trap_cancel(0)
    res = await run_case(dut, words, exp, "forward trap_cancel",
                         use_ref=False, max_cycles=20_000)
    _check_trap(dut, "cancel", res, exp)
    h, tr = res["h"], res["tr"]
    t = exp["trap"]
    executed = tr.retired_pcs()
    for pc in t["consumers"]:
        expect(pc not in executed,
               f"trap_cancel: consumer 0x{pc:08x} retired after the faulting "
               f"load")
    expect(h.reg(7) == 0,
           f"trap_cancel: x7=0x{h.reg(7)!r}: the faulting load wrote its rd")
    expect(h.reg(8) == 0 and h.reg(9) == 0,
           f"trap_cancel: x8=0x{h.reg(8)!r} x9=0x{h.reg(9)!r}: a cancelled "
           f"consumer wrote its destination")
    expect(h.reg(10) == 0x456,
           f"trap_cancel: x10=0x{h.reg(10)!r}, the handler did not resume at "
           f"0x{t['resume']:08x}")
    dut._log.info("trap_cancel: fault 0x%08x flushed consumers %s, resumed "
                  "0x%08x", t["fault_pc"],
                  [f"0x{p:08x}" for p in t["consumers"]], t["resume"])


def _check_trap(dut, kind, res, exp):
    h, tr, stats = res["h"], res["tr"], res["stats"]
    t = exp["trap"]
    expect(stats["trap_taken"] == 1,
           f"trap {kind}: {stats['trap_taken']} trap cycle(s), expected 1")
    expect(h.mcause == t["cause"],
           f"trap {kind}: mcause=0x{h.mcause!r}, expected {t['cause']}")
    expect(h.mtval == t["tval"],
           f"trap {kind}: mtval=0x{h.mtval!r}, expected 0x{t['tval']:08x} "
           f"(the address forwarded from the preceding addi)")
    w = t["dtcm_base"] // 4
    # trap_cancel's handler records mcause then mtval; the tval program's
    # handler also copies both through EX-forwarding consumers.
    if kind == "cancel":
        expect(h.dtcm(w) == t["cause"] and h.dtcm(w + 1) == t["tval"],
               f"trap {kind}: handler stored 0x{h.dtcm(w)!r}/"
               f"0x{h.dtcm(w + 1)!r}, expected mcause={t['cause']} and "
               f"mtval=0x{t['tval']:08x}")
    else:
        expect(h.dtcm(w) == t["cause"] and h.dtcm(w + 1) == t["cause"],
               f"trap {kind}: handler mcause values "
               f"0x{h.dtcm(w)!r}/0x{h.dtcm(w + 1)!r}, expected {t['cause']}")
        expect(h.dtcm(w + 2) == t["tval"] and h.dtcm(w + 3) == t["tval"],
               f"trap {kind}: mtval consumer values "
               f"0x{h.dtcm(w + 2)!r}/0x{h.dtcm(w + 3)!r}, expected "
               f"0x{t['tval']:08x}")
    expect(h.reg(20) == t["cause"],
           f"trap {kind}: x20=0x{h.reg(20)!r}, expected mcause {t['cause']}")
    expect(h.reg(22) == t["tval"],
           f"trap {kind}: x22=0x{h.reg(22)!r}, expected mtval 0x{t['tval']:08x}")
    if kind != "cancel":
        # The tval handler also copies both CSRs through EX-forwarding addi
        # consumers; trap_cancel's handler has no such consumers.
        expect(h.reg(21) == t["cause"] and h.reg(23) == t["tval"],
               f"trap {kind}: x21/x23=0x{h.reg(21)!r}/0x{h.reg(23)!r}")
    expect(tr.retired_pcs().count(t["fault_pc"]) == 0,
           f"trap {kind}: the faulting instruction retired")
    if kind in ("load", "load_d1", "store"):
        expect(h.reg(5) == t["handler"],
               f"trap {kind}: x5=0x{h.reg(5)!r}: the faulting instruction "
               f"wrote its destination (handler should stay 0x"
               f"{t['handler']:08x})")
        expect(h.reg(10) == 0x123,
               f"trap {kind}: x10=0x{h.reg(10)!r}: execution did not resume "
               f"after the faulting instruction")
        expect(h.reg(28) == t["tval"], f"trap {kind}: x28 wrong")
        expect(h.dtcm((DTCM_BASE + 0x140 - DTCM_BASE) // 4) == 0,
               f"trap {kind}: the misaligned access modified DTCM")
    dut._log.info("trap %s: cause=%d mtval=0x%08x fault_pc=0x%08x", kind,
                  t["cause"], t["tval"], t["fault_pc"])


def _check_common(dut, case, res, exp):
    """Checks that apply to every case (called after run_case)."""
    expect(res["stats"]["trap_taken"] == exp.get("traps", 0),
           f"{case}: unexpected traps")
    expect(res["pcs"][-1] == exp["halt_pc"],
           f"{case}: last retired PC 0x{res['pcs'][-1]:08x} is not the halt "
           f"store 0x{exp['halt_pc']:08x}")


_RUNNERS = {
    "alu_ex": _run_alu_ex, "alu_mem": _run_alu_mem,
    "precedence": _run_precedence, "loaduse_depth": _run_loaduse_depth,
    "x0": _run_x0, "div": _run_div, "csr": _run_csr,
    "jal_link": _run_jal_link, "store_data": _run_store_data,
    "branch_ops": _run_branch_ops, "trap_tval": _run_trap_tval,
    "trap_cancel": _run_trap_cancel,
}
