"""Directed/random RAW hazards and the stall machinery.

Generates producer/consumer pairs at every pipeline distance:

* load-use (producer in MEM when the consumer is in ID) -> ``raw_stall``,
* ALU/mul/div producers at EX/MEM/WB distance (WB bypass),
* x0 writes followed immediately by x0 readers (x0 check precedes the WB
  bypass in ``id_rs1_data``/``id_rs2_data``).

The retirement trace must match the reference interpreter, which would diverge
immediately if ``IF_ID_IR_hold``/``IF_ID_PC_hold`` lost or duplicated the
instruction that waits behind a stall.  ``IF_ID_hold`` and ``raw_stall``
activity is counted to prove the stall path was actually taken.
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
    ref = RV32Interp(words, dtcm_words=dtcm_words).run()

    h = CoreHarness(dut)
    h.load_program(words)
    h.write_dtcm_words(0, dtcm_words)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0, "IF_ID_hold": 0, "div_busy": 0,
             "pipe_freeze": 0, "wb_repeat": 0}
    core_trace = await h.run_with_trace(halt_pc, max_cycles=60_000, stats=stats)
    core_pcs = [pc for pc, _ir in core_trace]

    expect(h.itcm(4095) == 0x5A5A_5A5A, "halt mailbox not written")
    h.compare_regs(ref.regs, ignore=(0,), label=f"hazard seed={seed} regfile")
    check_trace(core_pcs, ref.trace, label=f"hazard seed={seed} trace")
    h.compare_dtcm_words(ref.mem.dtcm_words(), label=f"hazard seed={seed} DTCM")

    expect(stats["raw_stall"] > 40,
           f"only {stats['raw_stall']} raw_stall cycles")
    expect(stats["IF_ID_hold"] > 40,
           f"only {stats['IF_ID_hold']} IF_ID_hold cycles")
    expect(stats["div_busy"] > 0, "no divider busy cycles")
    expect(stats["pipe_freeze"] > 0, "no pipeline freeze cycles")
    expect(stats["wb_repeat"] > 0,
           "WB never observed held across a freeze (div didn't overlap WB)")

    dut._log.info("hazard seed=%d: raw_stall=%d hold=%d div_busy=%d freeze=%d "
                  "wb_repeat=%d retired=%d",
                  seed, stats["raw_stall"], stats["IF_ID_hold"],
                  stats["div_busy"], stats["pipe_freeze"], stats["wb_repeat"],
                  len(core_pcs))
