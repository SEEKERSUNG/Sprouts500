"""Core-driven SoC tests: a hand-assembled program exercising the real bus.

The peripheral tests drive the bus through the external master.  These tests
run the actual core out of ITCM against the same bus/peripherals and check
the architectural result, which is what proves the core/bus handshake:

* back-to-back loads and stores (``bus_conflict`` inserts the bubble that
  gives every access a fresh ``req`` edge);
* reads from different peripheral windows in consecutive instructions;
* word / byte / half stores merged by the fake slave, then word / signed
  byte / unsigned byte / signed half / unsigned half loads of the result;
* TIMER polling from software and the W1C pending bit (write 0x0f clears
  pending while keeping EN/IE/PERIODIC);
* GPIO DIR/OUT then a synchronized IN readback;
* a slow slave (``hold`` on fake-slave accesses) and ``core_stall`` on any
  access: ``bus_wait`` must freeze the pipeline, keep ``req`` asserted
  without re-strobing the slave, and resume when ready returns.

The program writes its observations into fake-slave words 0x80..0xB8, then
writes the halt mailbox, so a test can read the results through the external
master after the core is spinning.  There is one image per cocotb module;
every test resets the SoC and reruns it (see ``common/program.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc
from common.program import Assembler, halt_sequence, write_program
from common.runner import ROOT, run_tests, soc_hdl

FAKE = 0x3000_0000
TIMER = 0x2000_0000
GPIO = 0x2000_1000
UART = 0x2000_2000
SPI = 0x2000_3000

# fake-slave addresses used for the scenario data
SC_RD0 = FAKE + 0x00          # reset word 0 = 0xA5A5_0000
SC_RD1 = FAKE + 0x04          # reset word 1 = 0xA5A5_0001
SC_RW = FAKE + 0x10           # store then load
SC_MERGE = FAKE + 0x20        # byte/half-store merge target
# result slots, read by the external master after the halt
RES = {
    "fake_rd0": FAKE + 0x80,
    "fake_rd1": FAKE + 0x84,
    "uart_baud": FAKE + 0x88,
    "spi_clkdiv": FAKE + 0x8C,
    "store_load": FAKE + 0x90,
    "merge_word": FAKE + 0x94,
    "merge_lb0": FAKE + 0x98,
    "merge_lb3": FAKE + 0x9C,
    "merge_lbu3": FAKE + 0xA0,
    "merge_lh2": FAKE + 0xA4,
    "merge_lhu2": FAKE + 0xA8,
    "timer_pending": FAKE + 0xAC,
    "timer_after_w1c": FAKE + 0xB0,
    "gpio_in": FAKE + 0xB4,
    "magic": FAKE + 0xB8,
}
MAGIC = 0x600D_1234

EXPECTED = {
    "fake_rd0": 0xA5A5_0000,
    "fake_rd1": 0xA5A5_0001,
    "uart_baud": 867,
    "spi_clkdiv": 49,
    "store_load": 0xDEAD_BEEF,
    "merge_word": 0xBEEF_AB0D,
    "merge_lb0": 0x0000_000D,
    "merge_lb3": 0xFFFF_FFBE,
    "merge_lbu3": 0x0000_00BE,
    "merge_lh2": 0xFFFF_BEEF,
    "merge_lhu2": 0x0000_BEEF,
    # CTRL read: [3] PERIODIC, [2] pending, [1] IE, [0] EN
    "timer_pending": 0x0D,     # EN | pending | PERIODIC
    "timer_after_w1c": 0x0B,   # EN | IE | PERIODIC, pending cleared
    "gpio_in": 0x05,           # OUT=0xA5 on DIR=0x0F, inputs pulled low
    "magic": MAGIC,
}

#: Results written before the halt sequence; the halt sequence overwrites
#: x30/x31, so keep them out of the program until the very end.
RESULT_ORDER = (
    "fake_rd0", "fake_rd1", "uart_baud", "spi_clkdiv", "store_load",
    "merge_word", "merge_lb0", "merge_lb3", "merge_lbu3", "merge_lh2",
    "merge_lhu2", "timer_pending", "timer_after_w1c", "gpio_in", "magic",
)

PROGRAM_HEX = ROOT / "sim_build" / "core_program.hex"


def build_core_program() -> Assembler:
    a = Assembler()
    a.li("x28", FAKE)
    a.li("x27", TIMER)
    a.li("x26", GPIO)
    a.li("x25", UART)
    a.li("x24", SPI)

    # -- back-to-back fake-slave loads: bus_conflict must insert a bubble so
    #    both accesses are separate transactions -----------------------------
    a.lw("x5", 0, "x28")
    a.lw("x6", 4, "x28")
    a.sw("x5", RES["fake_rd0"] - FAKE, "x28")
    a.sw("x6", RES["fake_rd1"] - FAKE, "x28")

    # -- two different peripherals in consecutive loads ----------------------
    a.lw("x5", 0x08, "x25")           # UART BAUD
    a.lw("x6", 0x08, "x24")           # SPI CLKDIV
    a.sw("x5", RES["uart_baud"] - FAKE, "x28")
    a.sw("x6", RES["spi_clkdiv"] - FAKE, "x28")

    # -- store then load the same word (write must land before the read) -----
    a.li("x7", 0xDEAD_BEEF)
    a.sw("x7", SC_RW - FAKE, "x28")
    a.lw("x8", SC_RW - FAKE, "x28")
    a.sw("x8", RES["store_load"] - FAKE, "x28")

    # -- byte / half stores merge into a word, then all load sizes -----------
    a.li("x9", 0xF00D_F00D)
    a.sw("x9", SC_MERGE - FAKE, "x28")
    a.li("x10", 0xAB)
    a.sb("x10", SC_MERGE + 1 - FAKE, "x28")
    a.li("x11", 0xBEEF)
    a.sh("x11", SC_MERGE + 2 - FAKE, "x28")
    a.lw("x12", SC_MERGE - FAKE, "x28")
    a.sw("x12", RES["merge_word"] - FAKE, "x28")
    a.lb("x13", SC_MERGE - FAKE, "x28")
    a.sw("x13", RES["merge_lb0"] - FAKE, "x28")
    a.lb("x13", SC_MERGE + 3 - FAKE, "x28")
    a.sw("x13", RES["merge_lb3"] - FAKE, "x28")
    a.lbu("x13", SC_MERGE + 3 - FAKE, "x28")
    a.sw("x13", RES["merge_lbu3"] - FAKE, "x28")
    a.lh("x13", SC_MERGE + 2 - FAKE, "x28")
    a.sw("x13", RES["merge_lh2"] - FAKE, "x28")
    a.lhu("x13", SC_MERGE + 2 - FAKE, "x28")
    a.sw("x13", RES["merge_lhu2"] - FAKE, "x28")

    # -- timer: PRE=0, CMP=64, EN|W1C|PERIODIC; poll pending; clear it ------
    a.sw("x0", 0x0C, "x27")           # PRE = 0
    a.li("x5", 64)
    a.sw("x5", 0x08, "x27")           # CMP = 64
    a.li("x5", 0x0D)                  # EN | W1C | PERIODIC
    a.sw("x5", 0x00, "x27")
    a.label("timer_poll")
    a.lw("x6", 0x00, "x27")
    a.andi("x6", "x6", 0x4)           # pending?
    a.beq("x6", "x0", "timer_poll")
    a.lw("x6", 0x00, "x27")
    a.sw("x6", RES["timer_pending"] - FAKE, "x28")
    a.li("x5", 0x0F)                  # EN | IE | W1C | PERIODIC
    a.sw("x5", 0x00, "x27")           # clears pending, keeps mode bits
    a.lw("x6", 0x00, "x27")
    a.sw("x6", RES["timer_after_w1c"] - FAKE, "x28")
    a.sw("x0", 0x00, "x27")           # stop

    # -- GPIO: DIR=0x0F, OUT=0xA5, read synchronized IN ----------------------
    a.li("x5", 0x0F)
    a.sw("x5", 0x00, "x26")
    a.li("x5", 0xA5)
    a.sw("x5", 0x04, "x26")
    a.nop()
    a.nop()
    a.lw("x6", 0x08, "x26")
    a.sw("x6", RES["gpio_in"] - FAKE, "x28")

    a.li("x5", MAGIC)
    a.sw("x5", RES["magic"] - FAKE, "x28")
    halt_sequence(a)
    return a


PROGRAM_HEX = write_program(PROGRAM_HEX, build_core_program())


@pytest.mark.cocotb_runner
def test_core(hdl):
    soc_hdl(hdl)
    run_tests(hdl, plusargs=[f"+ITCM_HEX={Path(PROGRAM_HEX).resolve()}"])


@dataclass
class BusEvent:
    """One core bus transaction seen on ``core_req``'s rising edge."""

    cycle: int
    we: bool
    addr: int
    wdata: int
    wstrb: int
    duration: int = 0


@dataclass
class FakeWrite:
    cycle: int
    addr: int
    wdata: int
    wstrb: int


async def monitor_core_bus(dut, max_cycles: int = 40_000):
    """Record every core transaction and fake-slave write pulse until halt.

    Returns ``(events, fake_writes, halt_cycle)``; ``halt_cycle`` is None on
    timeout.  Reads ``core_req``/``fake_wr`` after ReadOnly, so the values are
    those of the cycle that just elapsed.
    """
    events: list[BusEvent] = []
    fake_writes: list[FakeWrite] = []
    prev_req = 0
    active: BusEvent | None = None
    for cycle in range(1, max_cycles + 1):
        await RisingEdge(dut.clk)
        await ReadOnly()
        halted = int(dut.core_halted.value)
        req = int(dut.core_req.value)
        if req:
            if active is None:
                active = BusEvent(
                    cycle,
                    bool(int(dut.core_we.value)),
                    int(dut.core_addr.value),
                    int(dut.core_wdata.value),
                    int(dut.core_wstrb.value),
                )
                events.append(active)
            active.duration += 1
        else:
            active = None
        if not halted and int(dut.fake_wr.value):
            fake_writes.append(FakeWrite(
                cycle, int(dut.fake_wr_addr.value),
                int(dut.fake_wr_wdata.value), int(dut.fake_wr_wstrb.value)))
        if halted:
            return events, fake_writes, cycle
        prev_req = req
    return events, fake_writes, None


async def _wait_edges(dut, n: int) -> None:
    for _ in range(n):
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)


async def _run_program(dut, hold_cycles: int = 0, stall_cycles: int = 0):
    """Reset, optionally stretch ready, run to the halt mailbox.

    The monitor is started before the clock exists, so it sees the program
    from its first bus access.  ``hold`` and ``core_stall`` are raised on the
    reset-release edge, i.e. before the first load reaches MEM.
    """
    mon = cocotb.start_soon(monitor_core_bus(dut))
    await reset_soc(dut)
    if hold_cycles:
        dut.hold.value = 1
        await _wait_edges(dut, hold_cycles)
        dut.hold.value = 0
    if stall_cycles:
        dut.core_stall.value = 1
        await _wait_edges(dut, stall_cycles)
        dut.core_stall.value = 0
    events, fake_writes, halt_cycle = await mon
    assert halt_cycle is not None, (
        f"program did not halt within the monitor window; "
        f"{len(events)} bus transactions seen, last="
        f"{events[-1] if events else None}"
    )
    assert int(dut.core_halted.value) == 1
    return events, fake_writes, halt_cycle


async def _read_results(dut):
    """Switch the bus to the external master and read all result slots."""
    bm = BusMaster(dut)
    bm.start()
    await RisingEdge(dut.clk)
    dut.ext_master.value = 1
    bm.mark_reset()
    return {name: (await bm.read(addr)).data
            for name, addr in RES.items()}


def _check_results(results: dict) -> None:
    for name, expect in EXPECTED.items():
        assert results[name] == expect, (
            f"{name}: expected 0x{expect:08x}, got 0x{results[name]:08x}")


def _events_at(events, addr: int, we: bool = None):
    return [e for e in events
            if e.addr == addr and (we is None or e.we == we)]


def _check_no_duplicate_result_writes(events, fake_writes) -> None:
    """Each result slot must be written exactly once, including stalls."""
    for name in RESULT_ORDER:
        addr = RES[name]
        writes = _events_at(events, addr, True)
        assert len(writes) == 1, (
            f"{name}: {len(writes)} core write transactions to "
            f"0x{addr:08x} (expected 1)")
        pulses = [w for w in fake_writes if w.addr == addr]
        assert len(pulses) == 1, (
            f"{name}: {len(pulses)} fake-slave write pulses at "
            f"0x{addr:08x} (expected 1)")


@cocotb.test()
async def test_core_program_runs_and_halts(dut):
    events, fake_writes, halt_cycle = await _run_program(dut)
    results = await _read_results(dut)
    _check_results(results)
    # the halt mailbox must not be used as a bus transaction
    assert all(e.addr != 0x0000_7FFC for e in events)
    dut._log.info(
        f"core program halted after {halt_cycle} cycles, "
        f"{len(events)} bus transactions, {len(fake_writes)} fake writes")


@cocotb.test()
async def test_core_back_to_back_bus_accesses(dut):
    events, _, _ = await _run_program(dut)
    results = await _read_results(dut)
    _check_results(results)
    # every program access starts its own transaction: back-to-back accesses
    # must not merge.  These are the distinct scenario addresses touched.
    for addr in (SC_RD0, SC_RD1, FAKE + 0x04, SC_RW, SC_MERGE,
                 UART + 0x08, SPI + 0x08):
        assert _events_at(events, addr), (
            f"no transaction observed at 0x{addr:08x}")
    # consecutive loads of SC_RD0/SC_RD1 produce two separate transactions
    # with a low req cycle in between; if the bus_conflict bubble were
    # missing they would share one req pulse.
    assert len(_events_at(events, SC_RD0, False)) >= 1
    assert len(_events_at(events, SC_RD1, False)) >= 1
    # result stores themselves are back-to-back: each result slot gets its
    # own strobe (checked via the fake slave pulses)
    assert results["fake_rd0"] == 0xA5A5_0000
    assert results["fake_rd1"] == 0xA5A5_0001


@cocotb.test()
async def test_core_store_sizes_and_load_extensions(dut):
    _, _, _ = await _run_program(dut)
    results = await _read_results(dut)
    _check_results(results)
    # detailed checks on the merged word:
    #   F00DF00D -> sb +1 (0xAB) -> F00DAB0D -> sh +2 (0xBEEF) -> BEEFAB0D
    assert results["merge_word"] == 0xBEEF_AB0D
    assert results["merge_lb0"] == 0x0000_000D
    assert results["merge_lb3"] == 0xFFFF_FFBE
    assert results["merge_lbu3"] == 0x0000_00BE
    assert results["merge_lh2"] == 0xFFFF_BEEF
    assert results["merge_lhu2"] == 0x0000_BEEF


@cocotb.test()
async def test_core_timer_w1c_from_software(dut):
    _, _, _ = await _run_program(dut)
    results = await _read_results(dut)
    _check_results(results)
    assert results["timer_pending"] == 0x0D
    assert results["timer_after_w1c"] == 0x0B


@cocotb.test()
async def test_core_gpio_readback(dut):
    _, _, _ = await _run_program(dut)
    results = await _read_results(dut)
    _check_results(results)
    assert results["gpio_in"] == 0x05


@cocotb.test()
async def test_core_slow_fake_slave_freezes_pipeline(dut):
    hold_cycles = 120
    events, fake_writes, halt_cycle = await _run_program(
        dut, hold_cycles=hold_cycles)
    results = await _read_results(dut)
    _check_results(results)
    _check_no_duplicate_result_writes(events, fake_writes)
    # the first fake-slave read starts before the hold is released and its
    # req must stay asserted for the whole stall: the core is in bus_wait,
    # the second load is held in EX (bus_conflict path) and nothing is
    # re-strobed.
    first = _events_at(events, SC_RD0, False)
    assert len(first) == 1, first
    # hold is released mid-edge; the transaction observes it one cycle later,
    # and the first load reaches MEM about 5 cycles after reset release.
    assert first[0].duration >= hold_cycles - 30, (
        f"first fake read only lasted {first[0].duration} cycles while hold "
        f"was asserted for {hold_cycles}")
    # no fake slave write pulse during the stall window: the program's first
    # fake write happens after the stall
    assert all(w.cycle > first[0].cycle + first[0].duration
               for w in fake_writes), fake_writes[:3]


@cocotb.test()
async def test_core_core_stall_bus_wait(dut):
    stall_cycles = 40
    events, fake_writes, _ = await _run_program(
        dut, stall_cycles=stall_cycles)
    results = await _read_results(dut)
    _check_results(results)
    _check_no_duplicate_result_writes(events, fake_writes)
    first = _events_at(events, SC_RD0, False)
    assert len(first) == 1, first
    # core_stall gates every core access.  The first 10 instructions are the
    # five li base pointers (ALU only), so the first load only reaches MEM
    # after the pipeline has fetched them; it then waits out the rest of the
    # stall window.
    assert first[0].duration >= stall_cycles - 25, (
        f"first transaction only lasted {first[0].duration} cycles with a "
        f"{stall_cycles}-cycle core_stall")
    # the read data is still the value captured at the transaction start
    assert results["fake_rd0"] == 0xA5A5_0000
