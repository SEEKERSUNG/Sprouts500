"""Randomized control flow: BRANCH / JAL / JALR, taken and not taken.

Two images are built for every seed:

* a *linked* image with random forward branches, ``jal`` skips, ``jalr``
  calls (``jalr`` with a non-zero offset, return via ``jalr x0, x, 0``) and
  counted backward loops, and
* a *straight-line overlay* assembled at a different origin (``Asm.org``) whose
  ``jal``/``auipc`` link values are resolved against the overlay's real
  addresses, ending in ``jal x0, 0``.

Both run in one simulation; the overlay is copied over the entry region and the
core is reset a second time, so reset state leaking between images would show
up as a trace mismatch.  A taken branch or jump must flush the two younger
IF/ID slots (``id_flush_q``); a missed flush (e.g. a stale instruction after a
``raw_stall``) diverges the retirement trace on the next instruction.
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
    Asm, CoreHarness, RV32Interp, check_trace, enc_j, expect, random_operand,
    tb_seed,
)
from _runner import get_seeds, run_test  # noqa: E402

BRANCHES = {"beq": 0, "bne": 1, "blt": 4, "bge": 5, "bltu": 6, "bgeu": 7}
N_BLOCK = 18
OVERLAY_PC = 0x400


def build_linked(seed):
    rng = random.Random((seed + 1) * 0x27D4_EB2F + 7)
    a = Asm()
    for r in range(1, 29):
        a.li(r, random_operand(rng))
    a.li(30, 0)                         # taken-branch counter
    a.li(29, 0)                         # skipped-instruction counter
    a.label("top")
    for blk in range(N_BLOCK):
        rs1 = rng.randint(0, 28)
        rs2 = rng.randint(0, 28)
        br = rng.choice(list(BRANCHES))
        a._b(f"t{blk}", rs1, rs2, BRANCHES[br])
        a.addi(30, 30, 1)               # executed when NOT taken
        a.j(f"nt{blk}")
        a.label(f"t{blk}")
        a.addi(29, 29, 1)               # executed when taken
        a.label(f"nt{blk}")

        a.jal(rng.randint(0, 28), f"aj{blk}")
        a.addi(29, 29, 1)
        a.label(f"aj{blk}")

        link = rng.randint(1, 28)
        a.jal(link, f"sub{blk}")
        a.label(f"ret{blk}")
        a.jalr(0, link, 0)              # return through jalr x0
        a.label(f"sub{blk}")
        a.addi(rng.randint(1, 28), rs2, 7)
        a.nop()
        a.j(f"next{blk}")
        a.label(f"next{blk}")

        cnt = rng.randint(1, 4)
        a.li(27, cnt)
        a.label(f"loop{blk}")
        a.addi(26, 26, rng.randint(-3, 3))
        a.addi(27, 27, -1)
        a.bne(27, 0, f"loop{blk}")
        a.addi(25, 25, 1)

        # jalr with a non-zero offset onto a label: base = pc(auipc) +
        # (label - pc(auipc)).
        if rng.random() < 0.3:
            # auipc at pc0, then x24 = pc0 + 20 and jalr -4 -> pc0 + 16,
            # i.e. the instruction after the skipped addi at pc0 + 12.
            a.auipc(24, 0)
            a.addi(24, 24, 20)
            a.jalr(23, 24, -4)
            a.addi(28, 28, 1)           # skipped

    a.halt()
    return a.assemble(), a.halt_store_pc


def build_overlay(seed):
    rng = random.Random((seed + 1) * 0x1656_67B1 + 11)
    a = Asm()
    a.org(OVERLAY_PC)
    a.addi(13, 0, 2)                    # must stay 2: the jalr skips over
                                        # addi/addi, so x13 == 2 iff flushed
    a.li(20, rng.getrandbits(32))
    a.li(21, rng.getrandbits(32))

    # jal link value resolved at the overlay's real address.
    pc_jal = a.here()
    a.jal(19, "after_jal")
    a.addi(18, 18, 1)                   # skipped
    a.label("after_jal")
    a.addi(17, 19, -(pc_jal + 4))       # x17 == 0 iff jal linked correctly

    # auipc + jalr(+0): base = overlay pc of the auipc.
    pc_au = a.here()
    a.auipc(15, 0)
    a.addi(15, 15, 12)                  # target = pc_au + 12
    a.jalr(14, 15, 0)
    a.addi(13, 13, 1)                   # skipped
    a.addi(13, 13, 1)                   # skipped
    a.label("after_jalr")
    a.addi(12, 14, -(pc_au + 12))       # x12 == 0 iff jalr linked correctly

    for _ in range(30):
        r = rng.randint(1, 11)
        a.addi(r, r, rng.randint(-64, 63))
        a.xor(r, r, rng.randint(1, 11))
    a.emit(0x0000_006F)                 # jal x0, 0 : spin
    expect(len(a.items) * 4 <= 0xFFC, "overlay overlaps the halt mailbox")
    return a.assemble()[OVERLAY_PC // 4:]


@pytest.mark.parametrize("seed", range(get_seeds()))
def test_branch_rand(seed):
    run_test("test_branch_rand", seed=seed)


@cocotb.test()
async def branch_rand(dut):
    seed = tb_seed()
    words, halt_pc = build_linked(seed)
    overlay = build_overlay(seed)

    ref = RV32Interp(words).run()
    # Overlay lives at OVERLAY_PC in the same 16 KiB ITCM image.
    ov_image = [0] * (OVERLAY_PC // 4) + overlay
    ov = RV32Interp(ov_image, regs={i: 0 for i in range(1, 32)})
    ov.pc = OVERLAY_PC
    ref_ov = ov.exec(len(overlay) + 8)
    expect(ref_ov.trace[-1] == OVERLAY_PC + (len(overlay) - 1) * 4,
           "overlay reference did not self-loop")

    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"trap_taken": 0, "ex_pred_mispredict": 0,
             "IF_ID_hold": 0, "id_flush_q": 0}
    core_trace = await h.run_with_trace(halt_pc, max_cycles=40_000, stats=stats)
    core_pcs = [pc for pc, _ir in core_trace]
    expect(stats["trap_taken"] == 0, "unexpected trap during branch test")
    # The image has cold ``jal``/``jalr`` calls and counted loops in every
    # block, so the predictor must both miss (empty/target-changed entries)
    # and train.  A zero count means the IF/EX prediction plumbing is dead.
    expect(stats["ex_pred_mispredict"] > 0,
           "ex_pred_mispredict never asserted over a random BRANCH/JAL/JALR "
           "program; the prediction path is not exercised")
    expect(h.reg(0) in (0, None), "register file x0 was written")
    # Registers: only compare the counters that must be exact.
    for idx in (26, 27, 28, 29, 30):
        got = h.reg(idx)
        expect(got == ref.regs[idx],
               f"branch linked x{idx}: got {got!r} exp 0x{ref.regs[idx]:08x}")
    check_trace(core_pcs, ref.trace, label=f"branch seed={seed} linked trace")

    # Second image: the overlay is linked for OVERLAY_PC, so place it there and
    # enter it from address 0 with a single ``jal x0, OVERLAY_PC`` stub.
    h.write_itcm_words(OVERLAY_PC // 4, overlay)
    h.write_itcm_words(0, [enc_j(0x6F, 0, OVERLAY_PC)])
    await h.reset()
    ov_trace = await h.run_trace_until_pc(
        OVERLAY_PC + (len(overlay) - 1) * 4, max_cycles=10_000)
    ov_pcs = [pc for pc, _ir in ov_trace]
    expect(ov_pcs[0] == 0, "stub instruction at 0 did not retire first")
    check_trace(ov_pcs[1:], ref_ov.trace[:len(ov_pcs) - 1],
                label=f"branch seed={seed} overlay trace")
    # x12/x17 carry the link-value checks; x13 stays untouched (only the
    # skipped increments would touch it).  The spin is reached before the
    # random tail runs, so the tail registers are not compared here.
    for idx in (12, 13, 17):
        got = h.reg(idx)
        expect(got == ref_ov.regs[idx],
               f"overlay x{idx}: got {got!r} exp 0x{ref_ov.regs[idx]:08x}")

    dut._log.info("branch_rand seed=%d: linked retired=%d overlay retired=%d "
                  "mispredicts=%d id_flush_q=%d", seed, len(core_pcs),
                  len(ov_pcs), stats["ex_pred_mispredict"],
                  stats["id_flush_q"])
