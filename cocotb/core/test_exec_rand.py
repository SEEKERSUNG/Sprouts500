"""Randomized ALU / shift / compare instruction streams vs. a reference model.

Targets: every base-integer ALU path (OP and OP-IMM), LUI/AUIPC, WB->ID
bypass, x0-before-bypass, and the IF_ID_*_hold / id_flush_q invariant (a lost
or duplicated instruction makes the retirement trace diverge).
Memory, branches and M-extension are covered by other tests.
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

N_INST = 180


def build_case(seed):
    rng = random.Random((seed + 1) * 0x9E37_79B1 + 17)
    a = Asm()
    for r in range(1, 30):
        a.li(r, random_operand(rng))

    # x0 is hardwired; an instruction writing rd=0 must not leak through the
    # WB bypass into a consumer reading x0.
    a.addi(0, 0, 0x7FF)
    a.add(1, 0, 0)          # x1 := x0 + x0 == 0
    a.addi(0, 0, -1)
    a.add(2, 0, 1)          # still 0 (x1 may have been rewritten below)
    a.addi(0, 0, 0x123)
    a.addi(3, 0, 0)

    r_ops = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or_", "and_"]
    i_ops = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
    shift_ops = ["slli", "srli", "srai"]
    imm_pool = [-2048, -2047, -1, 0, 1, 2, 3, 0x7FF, 0x800, 0x123, 0x555,
                0xAAA, 0x7FE, 0x001]

    for _ in range(N_INST):
        kind = rng.random()
        rd = rng.randint(1, 29)
        if rng.random() < 0.30:
            rs1 = rd
        else:
            rs1 = rng.randint(0, 29)
        if rng.random() < 0.30:
            rs2 = rd
        else:
            rs2 = rng.randint(0, 29)
        if kind < 0.42:
            name = rng.choice(r_ops)
            getattr(a, name)(rd, rs1, rs2)
        elif kind < 0.62:
            name = rng.choice(i_ops)
            if rng.random() < 0.8:
                imm = rng.choice(imm_pool)
            else:
                imm = rng.randint(-2048, 2047)
            getattr(a, name)(rd, rs1, imm)
        elif kind < 0.72:
            name = rng.choice(shift_ops)
            getattr(a, name)(rd, rs1, rng.randrange(32))
        elif kind < 0.86:
            a.lui(rd, rng.getrandbits(20))
        else:
            a.auipc(rd, rng.getrandbits(20))

    a.halt()
    return a.assemble(), a.halt_store_pc


@pytest.mark.parametrize("seed", range(get_seeds()))
def test_exec_rand(seed):
    run_test("test_exec_rand", seed=seed)


@cocotb.test()
async def exec_rand(dut):
    seed = tb_seed()
    words, halt_pc = build_case(seed)
    ref = RV32Interp(words).run()

    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    await h.reset()
    core_trace = await h.run_with_trace(halt_pc, max_cycles=20_000)
    core_pcs = [pc for pc, _ir in core_trace]

    expect(h.itcm(4095) == 0x5A5A_5A5A, "halt mailbox not written")
    h.compare_regs(ref.regs, ignore=(0,), label=f"exec seed={seed} regfile")
    check_trace(core_pcs, ref.trace, label=f"exec seed={seed} retirement trace")

    # x0 must still read as zero in the core (the register file itself is not
    # reset, so only the read path can be checked).
    expect(h.reg(0) is None or h.reg(0) == 0,
           "register file x0 was written by the core")

    dut._log.info("exec_rand seed=%d: %d instructions, %d retired, %d ref steps",
                  seed, len(words), len(core_pcs), ref.steps)
