"""Directed/random RAW hazards and the stall machinery.

Generates producer/consumer pairs at every pipeline distance:

* TCM load-use at distance 0 (producer in EX when the consumer is in ID)
  -> one ``raw_stall``; at distance 1 the MEM load forwards directly, and at
  distance >= 2 the value is read through the WB bypass,
* ALU/mul/LUI/CSR/JAL-link/div producers at EX/MEM distance -> forwarded by
  ``ex_fwd_ok``/``mem_fwd_ok`` with no stall (rtl/core.v:806-831), except a
  divisor still running (``div_busy`` freezes ID),
* x0 writes followed immediately by x0 readers (x0 check precedes EX/MEM/WB
  forwarding in ``id_rs1_data``/``id_rs2_data``).

The retirement trace must match the reference interpreter, which would diverge
immediately if ``IF_ID_IR_hold``/``IF_ID_PC_hold`` lost or duplicated the
instruction that waits behind a stall.  ``IF_ID_hold`` and ``raw_stall``
activity is counted to prove the load-use stall path was actually taken.
``raw_stall`` is load-use only: forwarding removed the ALU/mul stalls, and a
divider is covered by ``div_busy`` (a raw_stall level, like pipe_freeze),
so ``raw_stall`` must be at least one cycle per immediately adjacent TCM
load-use pair.

Measured seeds 0/1/2: raw_stall 174/226/341, IF_ID_hold 636/490/836,
div_busy 594/462/792.  The thresholds below use a static lower bound computed
from the assembled image, so they do not depend on those numbers.
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
    DTCM_BASE, DTCM_WORDS, Asm, CoreHarness, RV32Interp, check_trace, expect,
    random_operand, tb_seed,
)
from _runner import get_seeds, run_test  # noqa: E402

N_PAIR = 120
ADDR_REG = 28


def count_load_uses(words):
    """Count immediately adjacent TCM load/consumer pairs that must stall once.

    Distance-1 consumers take the MEM forwarding value without a stall.  Decode
    just enough to keep this bound independent of the RTL stall logic.
    """
    # Decode just enough: opcode 0x03 = load (rd bits 11:7), ALU-immediate
    # 0x13 / register 0x33 / load 0x03 read rs1 bits 19:15, store 0x23 and
    # branch 0x63 read rs1 and rs2 bits 24:20.
    def rd(w):
        return (w >> 7) & 0x1F

    def uses(w, r):
        op = w & 0x7F
        rs1 = (w >> 15) & 0x1F
        rs2 = (w >> 20) & 0x1F
        if op in (0x13, 0x33, 0x03):
            return rs1 == r != 0
        if op in (0x23, 0x63):
            return r != 0 and (rs1 == r or rs2 == r)
        return False

    n = 0
    for i, w in enumerate(words):
        if w & 0x7F == 0x03:
            r = rd(w)
            if i + 1 < len(words) and uses(words[i + 1], r):
                n += 1
    return n


def build_case(seed):
    rng = random.Random((seed + 1) * 0xC2B2_AE35 + 59)
    dtcm_words = [rng.getrandbits(32) for _ in range(DTCM_WORDS)]

    a = Asm()
    for r in range(1, 30):
        a.li(r, random_operand(rng))

    # x0 must be read as zero even when the immediately preceding instruction
    # writes rd=x0 (the x0 test must come before the WB bypass).
    a.addi(0, 0, 0x123)
    a.add(1, 0, 0)
    a.addi(0, 0, -1)
    a.add(2, 0, 1)
    a.addi(0, 0, 0x7FF)
    a.addi(3, 0, 0)

    for _ in range(N_PAIR):
        kind = rng.random()
        rd = rng.randint(1, 27)
        if kind < 0.35:
            addr = DTCM_BASE + rng.randrange(DTCM_WORDS) * 4
            a.li(ADDR_REG, addr)
            a.lw(rd, ADDR_REG, 0)
        elif kind < 0.50:
            a.div(rd, rng.randint(1, 27), rng.randint(1, 27))
        elif kind < 0.62:
            a.mul(rd, rng.randint(1, 27), rng.randint(1, 27))
        elif kind < 0.72:
            a.lui(rd, rng.getrandbits(20))
        else:
            a.addi(rd, rng.randint(0, 27), rng.randint(-2048, 2047))

        # Distance 0..3 from producer to consumer.
        for _ in range(rng.choice([0, 0, 1, 2, 3])):
            a.nop()
        other = rng.randint(1, 27)
        dst = rng.randint(1, 27)
        a.add(dst, rd, other)
        if rng.random() < 0.6:
            a.xor(rng.randint(1, 27), dst, rd)
        if rng.random() < 0.25:
            a.slli(rng.randint(1, 27), rd, rng.randrange(32))

    a.halt()
    return a.assemble(), a.halt_store_pc, dtcm_words


@pytest.mark.parametrize("seed", range(get_seeds()))
def test_hazard(seed):
    run_test("test_hazard", seed=seed)


@cocotb.test()
async def hazard(dut):
    seed = tb_seed()
    words, halt_pc, dtcm_words = build_case(seed)
    load_uses = count_load_uses(words)
    expect(load_uses > 0, "sanity: random program contains no load-use pair")
    ref = RV32Interp(words, dtcm_words=dtcm_words).run()

    h = CoreHarness(dut)
    h.load_program(words)
    h.write_dtcm_words(0, dtcm_words)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0, "IF_ID_hold": 0, "div_busy": 0,
             "pipe_freeze": 0, "wb_repeat": 0}
    unjustified = {"n": 0, "cycle": None}
    load_stalls = {"n": 0}

    def cycle_hook(hh):
        # With forwarding, a raw_stall must come from a load producer in EX or
        # MEM (rd != 0), or from a divider still running (div_busy, which also
        # makes raw_stall high because ID is frozen).  Anything else would be
        # a stall the forwarding path failed to remove.  Non-divider stalls
        # are the load-use stalls exercised by count_load_uses().
        if not hh.raw_stall or hh.div_busy:
            return
        load_ex = hh.EX_valid and hh.EX_mem_read and hh.EX_rd != 0
        load_mem = hh.MEM_valid and hh.MEM_mem_read and hh.MEM_rd != 0
        if load_ex or load_mem:
            load_stalls["n"] += 1
        else:
            unjustified["n"] += 1
            unjustified["cycle"] = (hh.ID_PC, hh.EX_PC, hh.MEM_PC)

    core_trace = await h.run_with_trace(halt_pc, max_cycles=60_000, stats=stats,
                                       cycle_hook=cycle_hook)
    core_pcs = [pc for pc, _ir in core_trace]

    expect(h.itcm(4095) == 0x5A5A_5A5A, "halt mailbox not written")
    h.compare_regs(ref.regs, ignore=(0,), label=f"hazard seed={seed} regfile")
    check_trace(core_pcs, ref.trace, label=f"hazard seed={seed} trace")
    h.compare_dtcm_words(ref.mem.dtcm_words(), label=f"hazard seed={seed} DTCM")

    # Ordinary TCM loads forward from MEM: distance-0 pairs stall once in EX,
    # distance-1 pairs no longer contribute a mandatory stall.  Require one
    # cycle per immediately adjacent pair found in the assembled image.
    expect(load_stalls["n"] >= load_uses,
           f"only {load_stalls['n']} load-use stall cycles for {load_uses} "
           f"load-use pairs; forwarding must not have swallowed load stalls")
    expect(unjustified["n"] == 0,
           f"{unjustified['n']} raw_stall cycle(s) with no load in EX/MEM and "
           f"no div_busy, first at ID/EX/MEM PC 0x{unjustified['cycle']}: a "
           f"non-load producer stalled ID (forwarding path broken)")
    expect(stats["IF_ID_hold"] >= stats["raw_stall"],
           f"IF_ID_hold {stats['IF_ID_hold']} < raw_stall "
           f"{stats['raw_stall']}: the IF/ID hold path did not follow the stall")
    expect(stats["div_busy"] > 0, "no divider busy cycles")
    expect(stats["pipe_freeze"] > 0, "no pipeline freeze cycles")
    expect(stats["wb_repeat"] > 0,
           "WB never observed held across a freeze (div didn't overlap WB)")

    dut._log.info("hazard seed=%d: raw_stall=%d load_stalls=%d load_uses=%d "
                  "hold=%d div_busy=%d freeze=%d wb_repeat=%d retired=%d",
                  seed, stats["raw_stall"], load_stalls["n"], load_uses,
                  stats["IF_ID_hold"], stats["div_busy"],
                  stats["pipe_freeze"], stats["wb_repeat"], len(core_pcs))
