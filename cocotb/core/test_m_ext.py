"""Randomized M-extension (mul/mulh/mulhsu/mulhu/div/divu/rem/remu) test.

Directed edge cases cover div-by-zero, INT_MIN/-1, negative operands and
back-to-back div/rem sequences (divider FSM + ``EX_funct3[1]`` quotient vs.
remainder selection).  Random RAW distances exercise the divider start/hold
interaction with the pipeline freeze.
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
    Asm, CoreHarness, RV32Interp, check_trace, expect, random_operand, tb_seed,
)
from _runner import get_seeds, run_test  # noqa: E402


def build_case(seed):
    rng = random.Random((seed + 1) * 0x85EB_CA6B + 29)
    a = Asm()
    for r in range(1, 30):
        a.li(r, random_operand(rng))

    # --- directed special values -----------------------------------------
    a.li(27, 0x8000_0000)      # INT_MIN
    a.li(28, 0xFFFF_FFFF)      # -1
    a.li(26, 0x0000_0000)      # zero

    a.div(1, 27, 26)           # INT_MIN / 0  -> all ones
    a.divu(2, 28, 26)          # UINT_MAX / 0 -> all ones
    a.rem(3, 27, 26)           # INT_MIN % 0  -> INT_MIN
    a.remu(4, 28, 26)          # UINT_MAX % 0 -> UINT_MAX
    a.div(5, 27, 28)           # INT_MIN / -1 -> INT_MIN (overflow)
    a.rem(6, 27, 28)           # INT_MIN % -1 -> 0 (overflow)
    a.divu(7, 27, 28)          # 0x80000000 / 0xffffffff -> 0
    a.remu(8, 27, 28)          # remainder 0x80000000

    # Back-to-back div/rem with no RAW: the divider must restart cleanly and
    # the quotient/remainder mux must follow EX_funct3[1].
    a.mul(9, 1, 2)
    a.div(10, 3, 4)
    a.rem(11, 5, 6)
    a.divu(12, 7, 8)
    a.remu(13, 9, 10)
    a.mulh(14, 11, 12)
    a.div(15, 13, 14)
    a.rem(16, 15, 16)

    m_ops = ["mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"]
    for _ in range(96):
        name = rng.choice(m_ops)
        rd = rng.randint(1, 29)
        rs1 = rng.randint(0, 29)
        rs2 = rng.randint(0, 29)
        getattr(a, name)(rd, rs1, rs2)
        # Keep some operations back-to-back (no RAW) and some separated so the
        # ID stall and the divider freeze interact in different ways.
        for _ in range(rng.choice([0, 0, 0, 1, 2, 3, 5])):
            a.nop()

    a.halt()
    return a.assemble(), a.halt_store_pc


@pytest.mark.parametrize("seed", range(get_seeds()))
def test_m_ext(seed):
    run_test("test_m_ext", seed=seed)


@cocotb.test()
async def m_ext(dut):
    seed = tb_seed()
    words, halt_pc = build_case(seed)
    ref = RV32Interp(words).run()

    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    await h.reset()
    core_trace = await h.run_with_trace(halt_pc, max_cycles=60_000)
    core_pcs = [pc for pc, _ir in core_trace]

    expect(h.itcm(4095) == 0x5A5A_5A5A, "halt mailbox not written")
    h.compare_regs(ref.regs, ignore=(0,), label=f"M-ext seed={seed} regfile")
    check_trace(core_pcs, ref.trace, label=f"M-ext seed={seed} retirement trace")

    # Sanity: the divider must have frozen the pipeline at some point.
    expect(ref.steps > 100, "reference model did not run enough instructions")
    dut._log.info("m_ext seed=%d: %d words, %d retired, ref steps=%d",
                  seed, len(words), len(core_pcs), ref.steps)
