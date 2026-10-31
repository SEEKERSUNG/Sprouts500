"""Stall-machinery invariants (``docs/architecture.md`` §9 items 1, 2, 7).

Every case runs one short program and checks cycle-level signals through
``run_with_trace``'s ``stats``/``cycle_hook``:

* ``load_use``   - a load followed immediately by its consumer: ``raw_stall``
  must hold the consumer in ID while the load crosses EX and MEM, and the
  consumer must then retire exactly once.
* ``hold``       - many load-use chains with instructions queued behind them:
  ``IF_ID_hold`` must rise and the full retirement trace must equal the
  reference model, i.e. ``IF_ID_IR_hold``/``IF_ID_PC_hold`` lose no
  instruction behind a stall.
* ``branch``     - a taken ``bne`` directly behind a load-use stall: the three
  instructions fetched after it must be flushed (never retired), ``id_flush_q``
  must hold ID invalid for the extra cycle after the redirect, and a following
  not-taken branch must still fall through correctly.
* ``jalr``       - ``jalr`` behind a load-use stall jumps to the loaded address
  with bit 0 cleared and its link register (``x1``) equals ``jalr_pc + 4``;
  the target consumes ``x1`` immediately, exercising EX/MEM->ID forwarding
  of the link (a load producer would stall; a link producer must not).
* ``div``        - a divider in EX freezes the pipeline for its latency:
  ``div_busy``/``pipe_freeze`` are counted, the divide retires exactly once,
  and the quotient/remainder plus their immediate consumers are checked.
* ``div_freeze_store`` - a DTCM store sits in MEM while the following div
  freezes it: ``mem_dtcm_we`` must be high for exactly one cycle even though
  the store PC is held in MEM for the whole divide (rtl/core.v:744-745 gates
  TCM writes with ``!pipe_freeze``).
* ``itcm_store`` - the same single-cycle write gate on the ITCM path
  (rtl/core.v:743).
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
    DTCM_BASE, DTCM_WORDS, ITCM_DATA_BASE, Asm, CoreHarness, RV32Interp,
    check_trace, expect, tb_case, tb_seed,
)
from _runner import run_test  # noqa: E402

ADDR = 28
DATA = 27


def _new_dtcm():
    return [0] * DTCM_WORDS


def _init_regs(a):
    """Give every integer register a known zero so the full regfile compare is
    meaningful (untouched architectural registers read X from Icarus)."""
    for r in range(1, 30):
        a.addi(r, 0, 0)


def build_load_use(seed=0):
    dtcm = _new_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(ADDR, DTCM_BASE + 0x40)
    a.lw(5, ADDR, 0)
    consume_pc = a.here()
    a.addi(6, 5, 1)
    a.add(2, 2, 6)
    a.halt()
    return a.assemble(), a.halt_store_pc, {"consume_pc": consume_pc,
                                           "dtcm": dtcm}


def build_hold(seed):
    rng = random.Random(seed * 65599 + 7)
    dtcm = [rng.getrandbits(32) for _ in range(DTCM_WORDS)]
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    for _ in range(30):
        off = rng.randrange(0, DTCM_WORDS) * 4
        a.li(25, DTCM_BASE + off)
        a.lw(5, 25, 0)
        a.addi(6, 5, 0)                     # load-use
        a.add(2, 2, 6)
        a.xor(7, 5, 6)                      # queued behind the consumer
        a.add(2, 2, 7)
        a.nop()
        a.addi(8, 5, 1)                     # load long retired: regfile read
        a.add(2, 2, 8)
    a.halt()
    return a.assemble(), a.halt_store_pc, {"dtcm": dtcm}


def build_branch(seed=0):
    dtcm = _new_dtcm()
    dtcm[0x80 // 4] = 0                  # x6 loads 0, x4 holds 0x27
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(4, 0x27)
    a.li(10, 0)
    a.li(ADDR, DTCM_BASE + 0x80)
    a.lw(6, ADDR, 0)
    branch_pc = a.here()
    a.bne(6, 4, "taken")                # taken
    poison1_pc = a.here()
    a.addi(10, 10, 1)
    poison2_pc = a.here()
    a.addi(10, 10, 2)
    poison3_pc = a.here()
    a.addi(10, 10, 4)
    a.label("taken")
    a.addi(10, 10, 8)                   # only this add reaches x10
    a.addi(7, 6, 1)
    # A not-taken branch must not flush anything.
    a.li(9, 5)
    a.li(11, 5)
    a.bne(9, 11, "never")               # never taken
    a.addi(10, 10, 0x10)
    a.j("after_never")
    a.label("never")
    a.addi(10, 10, 0x20)                # dead path
    a.label("after_never")
    a.halt()
    return a.assemble(), a.halt_store_pc, {
        "branch_pc": branch_pc,
        "poison_pcs": (poison1_pc, poison2_pc, poison3_pc),
        "dtcm": dtcm, "x10": 8 + 0x10, "x7": 1}


def build_jalr(seed=0):
    dtcm = _new_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 0)
    a.li(ADDR, DTCM_BASE + 0x100)
    a.lw(5, ADDR, 0)
    jalr_pc = a.here()
    a.jalr(1, 5, 0)
    skipped_pc = a.here()
    a.li(2, 0xBAD)                      # dead: jalr skips it
    a.label("cont")
    a.li(ADDR, DTCM_BASE + 0x104)
    a.sw(3, ADDR, 0)                    # x3 == link value, checked via ref
    a.lw(7, ADDR, 0)
    a.addi(8, 7, 1)
    a.halt()
    a.label("target")
    target_pc = a.here()
    a.addi(3, 1, 0)                     # immediate consumer of the link
    a.j("cont")
    words = a.assemble()
    expect(target_pc % 2 == 0, "sanity: target must be aligned")
    dtcm[0x100 // 4] = target_pc | 1    # odd address: jalr must clear bit 0
    return words, a.halt_store_pc, {"jalr_pc": jalr_pc,
                                    "skipped_pc": skipped_pc,
                                    "target_pc": target_pc, "dtcm": dtcm,
                                    "x8": None}


def build_div(seed=0):
    dtcm = _new_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(13, 0x1234_5678)
    a.li(14, 0x123)
    div_pc = a.here()
    a.div(10, 13, 14)
    a.addi(11, 10, 1)                   # immediate consumer
    a.rem(15, 13, 14)
    a.add(2, 11, 15)
    a.halt()
    return a.assemble(), a.halt_store_pc, {"div_pc": div_pc, "dtcm": dtcm}


def build_div_freeze_store(seed=0):
    dtcm = _new_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(13, 0x1234)
    a.li(14, 7)
    a.li(2, 0)
    a.li(ADDR, DTCM_BASE + 0x200)
    a.li(DATA, 0x1357_9BDF)
    store_pc = a.here()
    a.sw(DATA, ADDR, 0)                 # in MEM when the div enters EX
    div_pc = a.here()
    a.div(10, 13, 14)
    a.lw(11, ADDR, 0)
    a.add(2, 10, 11)
    a.halt()
    return a.assemble(), a.halt_store_pc, {"store_pc": store_pc,
                                           "div_pc": div_pc, "dtcm": dtcm}


def build_itcm_store(seed=0):
    dtcm = _new_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(ADDR, ITCM_DATA_BASE + 0x40)
    a.li(DATA, 0xC0DE_1234)
    store_pc = a.here()
    a.sw(DATA, ADDR, 0)
    a.lw(11, ADDR, 0)
    a.addi(12, 11, 1)
    a.halt()
    return a.assemble(), a.halt_store_pc, {"store_pc": store_pc,
                                           "dtcm": dtcm}


CASES = ("load_use", "hold", "branch", "jalr", "div", "div_freeze_store",
         "itcm_store")


@pytest.mark.parametrize("case", CASES)
def test_tcm_stall(case):
    seed = tb_seed() if case == "hold" else 0
    run_test("test_tcm_stall", seed=seed, case=case)


@cocotb.test()
async def tcm_stall(dut):
    case = tb_case("load_use")
    builder = {"load_use": build_load_use, "hold": build_hold,
               "branch": build_branch, "jalr": build_jalr, "div": build_div,
               "div_freeze_store": build_div_freeze_store,
               "itcm_store": build_itcm_store}[case]
    seed = tb_seed()
    words, halt_pc, exp = builder(seed)
    dtcm = exp["dtcm"]
    ref = RV32Interp(words, dtcm_words=dtcm).run()

    h = CoreHarness(dut)
    h.load_program(words)
    h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0, "IF_ID_hold": 0, "div_busy": 0, "pipe_freeze": 0,
             "mem_dtcm_we": 0, "mem_itcm_we": 0, "id_flush_q": 0,
             "ex_pred_mispredict": 0, "wb_repeat": 0}
    frozen_mem = {"store_frozen_cycles": 0, "div_busy_mem_overlap": False,
                  "store_mem_we_cycles": 0, "branch_pred": [],
                  "branch_flush": None}

    def cycle_hook(hh):
        if case == "div_freeze_store" and hh.div_busy and hh.MEM_valid:
            frozen_mem["div_busy_mem_overlap"] = True
            if hh.mem_dtcm_we:
                frozen_mem["store_frozen_cycles"] += 1
        if (case == "itcm_store" and hh.mem_itcm_we
                and hh.MEM_PC == exp["store_pc"]):
            frozen_mem["store_mem_we_cycles"] += 1
        if (case == "branch" and hh.EX_valid
                and hh.EX_PC == exp["branch_pc"]):
            frozen_mem["branch_pred"].append(
                (hh.EX_pred_taken, hh.ex_ctrl_actual, hh.ex_pred_mispredict,
                 hh.btb_we))
        # id_flush_q is registered from redirect, so the cycle after the
        # branch's EX mispredict is the one that must hold ID invalid; the
        # branch itself then sits in MEM.
        if (case == "branch" and hh.MEM_valid
                and hh.MEM_PC == exp["branch_pc"]):
            frozen_mem["branch_flush"] = hh.id_flush_q

    trace = await h.run_with_trace(halt_pc, max_cycles=60_000, stats=stats,
                                   cycle_hook=cycle_hook)
    pcs = [pc for pc, _ir in trace]
    check_trace(pcs, ref.trace, label=f"tcm_stall {case} trace")
    h.compare_regs(ref.regs, ignore=(0,), label=f"tcm_stall {case} regfile")
    h.compare_dtcm_words(ref.mem.dtcm_words(),
                         label=f"tcm_stall {case} DTCM")

    dut._log.info("tcm_stall case=%s: %s frozen=%s", case, stats, frozen_mem)
    _case_checks(h, case, exp, stats, pcs, frozen_mem)


def _case_checks(h, case, exp, stats, pcs, frozen_mem):
    if case == "load_use":
        consume = exp["consume_pc"]
        expect(pcs.count(consume) == 1,
               f"consumer 0x{consume:x} retired {pcs.count(consume)} times")
        # Distance-0 load-use: the consumer waits in ID while the load is in
        # EX, then in MEM.  EX/MEM->ID forwarding deliberately excludes load
        # producers, so both cycles remain (measured exactly 2).
        expect(stats["raw_stall"] >= 2,
               f"raw_stall only {stats['raw_stall']} cycle(s) for a load-use "
               f"pair (expected EX and MEM dependency cycles)")
        expect(stats["IF_ID_hold"] >= 1, "IF_ID_hold never asserted")
        expect(h.reg(5) == 0 and h.reg(6) == 1, "consumer got a wrong operand")
        expect(h.reg(2) == 1, "accumulator did not sum the consumer result")
    elif case == "hold":
        # Each of the 30 chains is a distance-0 load-use pair, so forwarding
        # leaves exactly two raw_stall cycles per chain (measured 60).  The
        # old >20 bound predates forwarding but stays as the lower bound.
        expect(stats["raw_stall"] >= 2 * 30,
               f"only {stats['raw_stall']} raw_stall cycles for 30 load-use "
               f"chains (each distance-0 pair needs 2)")
        expect(stats["IF_ID_hold"] >= stats["raw_stall"],
               f"only {stats['IF_ID_hold']} IF_ID_hold cycles for "
               f"{stats['raw_stall']} stall cycles")
    elif case == "branch":
        # The bne consumes the preceding load at distance 0: still two stalls
        # after forwarding (load producers are never forwarded).
        expect(stats["raw_stall"] >= 2,
               f"bne behind a load-use pair stalled only "
               f"{stats['raw_stall']} cycle(s); the branch did not wait for "
               f"its load")
        # The branch is executed once, with the BTB entry still empty: the IF
        # lookup predicts not-taken, the taken branch directs the redirect and
        # trains the entry.  So the flush is still required (a second
        # execution of the same PC would be predicted taken and would not
        # flush).  This pins the cold-mispredict path, not just the flush.
        rec = frozen_mem["branch_pred"]
        expect(len(rec) == 1,
               f"branch case: the bne at 0x{exp['branch_pc']:08x} was seen in "
               f"EX {len(rec)} time(s), expected exactly 1")
        pred, actual, misp, we = rec[0]
        expect(pred == 0 and actual == 1,
               f"branch case: EX_pred_taken={pred} ex_ctrl_actual={actual} for "
               f"the untrained taken bne, expected 0/1")
        expect(misp == 1,
               "branch case: the cold taken bne did not mispredict; the "
               "prediction path is not active")
        expect(we == 1,
               "branch case: the cold taken bne did not train the BTB")
        expect(stats["ex_pred_mispredict"] >= 1,
               "branch case: ex_pred_mispredict never asserted")
        # The redirect is registered into id_flush_q, so the cycle in which
        # the mispredicted branch sits in MEM is exactly the cycle ID must be
        # held invalid (measured 1).
        expect(frozen_mem["branch_flush"] == 1,
               f"branch case: id_flush_q={frozen_mem['branch_flush']!r} in the "
               f"cycle after the taken bne redirect, expected exactly 1")
        # Whole program, measured 3/3: the cold taken bne above, the cold
        # ``jal after_never`` and the halt-spin ``jal x0, 0`` each mispredict
        # once and each registers exactly one id_flush_q cycle.  With no
        # trap/freeze in this program, flush cycles and mispredict cycles
        # must match one for one.
        expect(stats["id_flush_q"] == stats["ex_pred_mispredict"] == 3,
               f"branch case: id_flush_q={stats['id_flush_q']} != "
               f"ex_pred_mispredict={stats['ex_pred_mispredict']} (or either "
               f"!= the measured 3 redirects: the cold bne, the cold jal, and "
               f"the halt-spin jal); the cold taken branch must redirect and "
               f"flush the younger slots exactly once")
        for pc in exp["poison_pcs"]:
            expect(pc not in pcs,
                   f"flushed instruction at 0x{pc:08x} retired")
        expect(h.reg(10) == exp["x10"],
               f"x10=0x{h.reg(10)!r}, expected 0x{exp['x10']:x}: a flushed or "
               f"dead-path add wrote the register")
        expect(h.reg(7) == exp["x7"], f"x7={h.reg(7)!r} != {exp['x7']}")
    elif case == "jalr":
        # jalr consumes the load at distance 0 (2 stalls); the later load-use
        # pair adds 2 more (measured 4, link forwarding contributes none).
        expect(stats["raw_stall"] >= 2,
               f"jalr behind a load-use pair stalled only "
               f"{stats['raw_stall']} cycle(s); the jalr did not wait for "
               f"its loaded target")
        expect(exp["skipped_pc"] not in pcs,
               "instruction after jalr retired (redirect missed)")
        target_pc = exp["target_pc"]
        expect(target_pc in pcs, f"jalr target 0x{target_pc:08x} never retired")
        expect(h.reg(3) != 0 and h.reg(3) % 2 == 0,
               f"x3=0x{h.reg(3)!r}: the link register was not a valid "
               f"return address")
        expect(h.reg(7) == h.reg(3),
               f"x7=0x{h.reg(7)!r} != link x3=0x{h.reg(3)!r} "
               f"(DTCM round-trip of x1)")
        expect(h.reg(8) == h.reg(3) + 1,
               f"x8=0x{h.reg(8)!r} is not link+1")
        # Link value must be jalr_pc + 4, not the PC where it was resolved.
        expect(h.reg(3) == exp["jalr_pc"] + 4,
               f"link x1=0x{h.reg(3)!r} != jalr_pc+4=0x{exp['jalr_pc']+4:08x}")
    elif case == "div":
        expect(stats["div_busy"] >= 30,
               f"div_busy only {stats['div_busy']} cycles")
        expect(stats["pipe_freeze"] >= stats["div_busy"],
               "pipe_freeze shorter than div_busy")
        expect(pcs.count(exp["div_pc"]) == 1,
               f"div retired {pcs.count(exp['div_pc'])} times")
    elif case == "div_freeze_store":
        expect(frozen_mem["div_busy_mem_overlap"],
               "the store never overlapped div_busy in MEM; test setup stale")
        expect(stats["div_busy"] >= 30,
               f"div_busy only {stats['div_busy']} cycles")
        expect(stats["mem_dtcm_we"] == 1,
               f"mem_dtcm_we high {stats['mem_dtcm_we']} cycles "
               f"(halt-mailbox DTCM store included): a TCM store "
               f"re-executed during pipe_freeze (rtl/core.v:744-745); "
               f"during-freeze writes={frozen_mem['store_frozen_cycles']}")
        expect(frozen_mem["store_frozen_cycles"] == 0,
               f"mem_dtcm_we was high for "
               f"{frozen_mem['store_frozen_cycles']} frozen cycle(s)")
        expect(h.dtcm(0x200 // 4) == 0x1357_9BDF,
               "the frozen store lost its data")
    elif case == "itcm_store":
        expect(frozen_mem["store_mem_we_cycles"] == 1,
               f"mem_itcm_we high for the data store on "
               f"{frozen_mem['store_mem_we_cycles']} cycles "
               f"(total mem_itcm_we={stats['mem_itcm_we']}, halt included)")
        expect(h.reg(11) == 0xC0DE_1234, "ITCM store not readable via lw")
        expect(h.reg(12) == 0xC0DE_1235, "ITCM read data/consumer mismatch")
