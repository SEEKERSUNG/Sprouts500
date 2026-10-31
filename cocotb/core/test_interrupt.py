"""Directed interrupt tests (level-sensitive external/timer/software inputs).

The IRQ levels are driven by cocotb from the start of the run; the program
installs one common handler that records ``mcause`` and ``mepc`` into DTCM and
increments a trap counter.  ``run_with_trace``'s ``trap_hook`` drops the level
of the source that caused each trap, exactly like a real interrupt controller
would; the priority order of simultaneous sources is therefore observable in
the recorded cause sequence.

Cases
-----
prio          all three sources high, all enabled: causes must be
              0x8000000B (ext), 0x80000003 (soft), 0x80000007 (timer).
disabled      all sources high but mstatus.MIE=0: no trap.
masked_soft   only MSIE enabled while all three are pending: one soft trap.
masked_timer  only MTIE enabled: one timer trap.
masked_ext    only MEIE enabled: one external trap.
vectored      mtvec[1:0]=1, all sources: the vectored entries at
              base+cause*4 jump to the shared handler.
div_before_irq
              timer is raised while a div is in EX; trap_taken must stay low
              during ``div_busy``, the trap lands on the div instruction
              (mepc == div PC), and the div is re-executed after mret.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    DTCM_BASE, Asm, CoreHarness, expect, tb_case,
)
from _runner import run_test  # noqa: E402

CAUSE_SOFT = 0x8000_0003
CAUSE_TIMER = 0x8000_0007
CAUSE_EXT = 0x8000_000B
CAUSE = {"soft": CAUSE_SOFT, "timer": CAUSE_TIMER, "ext": CAUSE_EXT}
MIE_BIT = {"soft": 0x8, "timer": 0x80, "ext": 0x800}
VECTORED_BASE = 0x0000_1000


def _init_state(a):
    a.li(19, 0)                          # trap counter
    a.li(22, DTCM_BASE)                  # cause/mepc record pointer


def _handler(a, label="handler"):
    """Shared handler: DTCM[ptr] = mcause, DTCM[ptr+4] = mepc, ptr += 8."""
    a.label(label)
    a.csrr(20, 0x342)                    # mcause
    a.csrr(21, 0x341)                    # mepc
    a.sw(20, 22, 0)
    a.sw(21, 22, 4)
    a.addi(22, 22, 8)
    a.addi(19, 19, 1)
    a.mret()


def _setup(a, enabled, mie_on=True):
    _init_state(a)
    a.la(5, "handler")
    a.csrw(0x305, 5)                     # direct mode
    a.li(5, 0)
    a.csrw(0x304, 5)                     # mie = 0
    if enabled:
        bits = 0
        for name in enabled:
            bits |= MIE_BIT[name]
        a.li(5, bits)
        a.csrs(0x304, 5)
    a.li(5, 0x8)
    if mie_on:
        a.csrs(0x300, 5)
    else:
        a.csrc(0x300, 5)


def _trap_body(a, n=4):
    for _ in range(n):
        a.addi(7, 7, 1)
        a.addi(8, 8, 1)


def _simple_case(enabled, mie_on=True, n=4):
    a = Asm()
    _setup(a, enabled, mie_on)
    _trap_body(a, n)
    a.halt()
    _handler(a)
    words = a.assemble()
    return words, a.halt_store_pc


def build_prio():
    return _simple_case(["soft", "timer", "ext"])


def build_disabled():
    return _simple_case([], mie_on=False)


def build_masked_soft():
    return _simple_case(["soft"])


def build_masked_timer():
    return _simple_case(["timer"])


def build_masked_ext():
    return _simple_case(["ext"])


def build_vectored():
    a = Asm()
    _init_state(a)
    a.li(5, VECTORED_BASE + 1)           # mtvec base | vectored mode
    a.csrw(0x305, 5)
    a.li(5, 0)
    a.csrw(0x304, 5)
    a.li(5, MIE_BIT["soft"] | MIE_BIT["timer"] | MIE_BIT["ext"])
    a.csrs(0x304, 5)
    a.li(5, 0x8)
    a.csrs(0x300, 5)
    _trap_body(a, 4)
    a.halt()
    # Vector table at VECTORED_BASE + cause*4; each slot jumps to the shared
    # handler that is placed right after the last slot.
    a.org(VECTORED_BASE + 3 * 4)
    a.j("handler")
    a.org(VECTORED_BASE + 7 * 4)
    a.j("handler")
    a.org(VECTORED_BASE + 11 * 4)
    a.j("handler")
    _handler(a)
    words = a.assemble()
    return words, a.halt_store_pc


def build_div_before_irq():
    a = Asm()
    _init_state(a)
    a.la(5, "handler")
    a.csrw(0x305, 5)
    a.li(5, 0)
    a.csrw(0x304, 5)
    a.li(5, MIE_BIT["timer"])
    a.csrs(0x304, 5)
    a.li(5, 0x8)
    a.csrs(0x300, 5)
    # x13 = 4 (source of the div) and x14 = 3 (divisor) are set up early so
    # the only in-flight load feeding EX is the one that loads x12.
    a.li(13, 4)
    a.li(14, 3)
    a.li(15, 0)
    a.li(16, 0)
    a.la(5, "div_op")
    div_pc = a.here()
    a.lw(12, 5, 0)                       # load-use stall: x12 available in EX
    a.addi(15, 15, 1)                    # uses x12
    a.j("after_div")
    a.label("div_op")                    # data word 3 in the ITCM data window
    a.emit(3)
    a.label("after_div")
    div_pc = a.here()
    a.div(10, 13, 14)                    # 4 / 3 = 1
    a.addi(11, 10, 0)                    # uses the div result
    a.addi(16, 16, 1)
    a.halt()
    _handler(a)
    words = a.assemble()
    exp = {"regs": {10: 1, 11: 1, 12: 3, 15: 1, 16: 1, 19: 1},
           "causes": [CAUSE_TIMER], "mepcs": [div_pc], "div_pc": div_pc}
    return words, a.halt_store_pc, exp


CASES = {
    "prio": (build_prio, ["ext", "soft", "timer"],
             [CAUSE_EXT, CAUSE_SOFT, CAUSE_TIMER]),
    "disabled": (build_disabled, [], []),
    "masked_soft": (build_masked_soft, ["soft"], [CAUSE_SOFT]),
    "masked_timer": (build_masked_timer, ["timer"], [CAUSE_TIMER]),
    "masked_ext": (build_masked_ext, ["ext"], [CAUSE_EXT]),
    "vectored": (build_vectored, ["ext", "soft", "timer"],
                 [CAUSE_EXT, CAUSE_SOFT, CAUSE_TIMER]),
}


@pytest.mark.parametrize("case", list(CASES) + ["div_before_irq"])
def test_interrupt(case):
    run_test("test_interrupt", seed=0, case=case)


@cocotb.test()
async def interrupt(dut):
    case = tb_case("prio")
    if case == "div_before_irq":
        await _run_div_case(dut)
        return

    build, order, expected = CASES[case]
    words, halt_pc = build()

    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    await h.reset()
    # All three levels are high from the beginning; the program enables the
    # sources it wants (disabled/masked cases only enable a subset).
    levels = {"soft": 1, "timer": 1, "ext": 1}
    h.set_irqs(**levels)

    idx = 0

    def trap_hook():
        nonlocal idx
        if idx < len(order):
            levels[order[idx]] = 0
            h.set_irqs(**levels)
        idx += 1

    stats = {"trap_taken": 0}
    await h.run_with_trace(halt_pc, max_cycles=20_000, stats=stats,
                           trap_hook=trap_hook)
    expect(stats["trap_taken"] == len(expected),
           f"{case}: {stats['trap_taken']} trap_taken cycles, expected "
           f"{len(expected)} trap(s)")
    expect(h.reg(19) == len(expected),
           f"{case}: x19={h.reg(19)!r}, expected {len(expected)} traps")
    got = [h.dtcm(i) for i in range(0, 2 * len(expected), 2)]
    if expected:
        expect(got == expected,
               f"{case}: DTCM causes {[f'0x{x:08x}' if x is not None else x for x in got]}"
               f" != {[f'0x{x:08x}' for x in expected]}")

    dut._log.info("interrupt case=%s: traps=%d causes=%s", case,
                  stats["trap_taken"], [hex(c) for c in expected])


async def _run_div_case(dut):
    words, halt_pc, exp = build_div_before_irq()
    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    await h.reset()
    h.set_irqs()

    raised = {"done": False, "first_busy": None, "first_trap": None}

    def cycle_hook(hh):
        if hh.div_busy:
            if raised["first_busy"] is None:
                raised["first_busy"] = hh.cycle
            if not raised["done"]:
                raised["done"] = True
                hh.set_irqs(timer=1)

    def trap_hook():
        if raised["first_trap"] is None:
            raised["first_trap"] = h.cycle
        expect(not h.div_busy, "trap_taken asserted while div_busy")
        h.set_irqs()

    stats = {"trap_taken": 0, "div_busy": 0}
    await h.run_with_trace(halt_pc, max_cycles=20_000, stats=stats,
                           cycle_hook=cycle_hook, trap_hook=trap_hook)
    expect(raised["done"], "div_busy never observed; timer IRQ was not raised")
    expect(raised["first_trap"] is not None, "timer interrupt never taken")
    expect(raised["first_trap"] > raised["first_busy"],
           "trap was taken before the divider finished")
    expect(stats["div_busy"] > 20,
           f"div_busy only {stats['div_busy']} cycles (divider did not run)")
    for idx, value in exp["regs"].items():
        expect(h.reg(idx) == value,
               f"div_before_irq: x{idx}={h.reg(idx)!r}, expected 0x{value:08x}")
    expect(h.dtcm(0) == exp["causes"][0],
           f"div_before_irq: mcause {h.dtcm(0)!r} != 0x{exp['causes'][0]:08x}")
    expect(h.dtcm(1) == exp["mepcs"][0],
           f"div_before_irq: mepc 0x{h.dtcm(1) or 0:08x} != "
           f"0x{exp['mepcs'][0]:08x} (div PC, re-executed after mret)")
    dut._log.info("div_before_irq: first div_busy cycle=%d, trap cycle=%d",
                  raised["first_busy"], raised["first_trap"])
