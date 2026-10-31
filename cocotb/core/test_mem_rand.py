"""Randomized load/store streams against ITCM, DTCM and the reference memory.

All byte lanes (lb/lbu/lh/lhu/lw/sb/sh/sw) are exercised at aligned addresses
inside DTCM and in the ITCM data window.  Each memory operation is preceded by
a ``li`` of its address, so the generated program is position independent and
the same image runs in the reference interpreter.  Some loads are followed
immediately by consumers, which exercises the load-use ``raw_stall`` and the
WB bypass, while the final DTCM/ITCM images act as memory scoreboards.
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
    DTCM_BASE, DTCM_BYTES, DTCM_WORDS, ITCM_DATA_BASE, ITCM_WORDS, Asm,
    CoreHarness, RV32Interp, check_trace, expect, random_operand, tb_seed,
)
from _runner import get_seeds, run_test  # noqa: E402

N_ACCESS = 150
ADDR_REG = 28
ITCM_DATA_WORD = ITCM_DATA_BASE // 4
ITCM_LAST_DATA_BYTE = 0x3FFC          # halt mailbox starts here


def build_case(seed):
    rng = random.Random((seed + 1) * 0x2545_F491 + 101)

    # Memory images (shared by the DUT and the reference model).
    dtcm_words = [rng.getrandbits(32) for _ in range(DTCM_WORDS)]
    itcm_data_words = [rng.getrandbits(32)
                       for _ in range(ITCM_WORDS - ITCM_DATA_WORD - 1)]

    a = Asm()
    for r in range(1, 30):
        a.li(r, random_operand(rng))

    load_ops = [("lb", 1), ("lbu", 1), ("lh", 2), ("lhu", 2), ("lw", 4)]
    store_ops = [("sb", 1), ("sh", 2), ("sw", 4)]

    for _ in range(N_ACCESS):
        if rng.random() < 0.5:
            name, size = rng.choice(load_ops)
        else:
            name, size = rng.choice(store_ops)
        in_dtcm = rng.random() < 0.55
        if in_dtcm:
            base, limit = DTCM_BASE, DTCM_BASE + DTCM_BYTES
        else:
            base, limit = ITCM_DATA_BASE, ITCM_LAST_DATA_BYTE
        n_slots = (limit - base - size) // size + 1
        addr = base + rng.randrange(n_slots) * size
        a.li(ADDR_REG, addr)

        if name in ("lb", "lbu", "lh", "lhu", "lw"):
            rd = rng.randint(1, 27)
            getattr(a, name)(rd, ADDR_REG, 0)
            # Back-to-back load-use: the WB bypass / raw_stall path must give
            # the loaded value without a stale register read.
            if rng.random() < 0.5:
                other = rng.randint(1, 27)
                dst = rng.randint(1, 27)
                if rng.random() < 0.5:
                    a.xor(dst, rd, other)
                else:
                    a.add(dst, other, rd)
                if rng.random() < 0.5:
                    a.sub(rng.randint(1, 27), dst, rd)
        else:
            rs = rng.randint(1, 27)
            getattr(a, name)(rs, ADDR_REG, 0)
            # Store/load alias at the same address with the same size: a later
            # access re-reads the just-written data.
            if rng.random() < 0.25:
                ld = {"sb": "lb", "sh": "lh", "sw": "lw"}[name]
                getattr(a, ld)(rng.randint(1, 27), ADDR_REG, 0)

        # Occasionally recompute the address register from a register (still
        # aligned) so that address setup can stall on an earlier producer.
        if rng.random() < 0.10:
            a.addi(ADDR_REG, ADDR_REG, 0)

    a.halt()
    words = a.assemble()
    expect(len(words) < ITCM_DATA_WORD,
           f"program ({len(words)} words) overlaps the ITCM data window")
    full_image = (words + [0] * (ITCM_DATA_WORD - len(words)) + itcm_data_words)
    return full_image, a.halt_store_pc, dtcm_words


@pytest.mark.parametrize("seed", range(get_seeds()))
def test_mem_rand(seed):
    run_test("test_mem_rand", seed=seed)


@cocotb.test()
async def mem_rand(dut):
    seed = tb_seed()
    image, halt_pc, dtcm_words = build_case(seed)
    ref = RV32Interp(image, dtcm_words=dtcm_words).run()

    h = CoreHarness(dut)
    h.load_program(image)
    h.write_dtcm_words(0, dtcm_words)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0}
    core_trace = await h.run_with_trace(halt_pc, max_cycles=40_000, stats=stats)
    core_pcs = [pc for pc, _ir in core_trace]

    expect(h.itcm(ITCM_WORDS - 1) == 0x5A5A_5A5A, "halt mailbox not written")
    expect(stats["raw_stall"] > 20,
           f"only {stats['raw_stall']} load-use stall cycles: stimulus did not "
           "exercise the hazard path")
    h.compare_regs(ref.regs, ignore=(0,), label=f"mem seed={seed} regfile")
    check_trace(core_pcs, ref.trace, label=f"mem seed={seed} retirement trace")
    h.compare_dtcm_words(ref.mem.dtcm_words(), label=f"mem seed={seed} DTCM")
    h.compare_itcm_words(ref.mem.itcm_words(), label=f"mem seed={seed} ITCM")

    dut._log.info("mem_rand seed=%d: %d words, %d retired, %d ref steps",
                  seed, len(image), len(core_pcs), ref.steps)
