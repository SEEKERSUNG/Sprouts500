"""Clock, reset and cycle-count helpers shared by the SoC tests.

The SoC runs at 50 MHz (20 ns period); reset is held for 16 cycles like
``sim/tb_app.v``.  ``start_clock`` remembers the running Clock so a second
call in the same test (or after a reset) does not start a duplicate driver.
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

CLK_PERIOD_NS = 20
RESET_CYCLES = 16


def start_clock(dut, period_ns: int = CLK_PERIOD_NS) -> None:
    """Start a 50 MHz clock on ``dut.clk`` unless one is already running.

    cocotb 2 cancels tasks spawned by a test when the test ends, so the clock
    must be restarted by the next test in the same simulation.  The stored
    task handle tells us whether the previous clock is still alive.
    """
    task = getattr(dut, "_sprouts_clock_task", None)
    if task is not None and not task.done():
        return
    dut._sprouts_clock = Clock(dut.clk, period_ns, unit="ns")
    dut._sprouts_clock_task = cocotb.start_soon(dut._sprouts_clock.start())


def _set_defaults(dut) -> None:
    """Drive every TB input that is not owned by a helper to a safe level.

    ``uart_rx`` and ``spi_miso`` idle high/low respectively; ``ext_master``
    keeps the core on the bus; ready/stall/hold are inactive; the external
    GPIO world is disconnected (``gpio_ext_en`` = 0).
    """
    dut.rstn.value = 0
    dut.core_rstn.value = 0
    dut.ext_master.value = 0
    dut.hold.value = 0
    dut.core_stall.value = 0
    dut.ext_ready.value = 1
    dut.gpio_ext_val.value = 0
    dut.gpio_ext_en.value = 1          # external world drives zeros everywhere
    dut.uart_rx.value = 1
    dut.uart_loopback.value = 0
    dut.spi_miso.value = 0
    dut.spi_loopback.value = 0
    dut.m_req.value = 0
    dut.m_we.value = 0
    dut.m_addr.value = 0
    dut.m_wdata.value = 0
    dut.m_wstrb.value = 0


async def reset_soc(
    dut,
    cycles: int = RESET_CYCLES,
    *,
    core: bool = True,
    start_clk: bool = True,
) -> None:
    """Apply a reset pulse and release; returns right after the release edge.

    ``core=False`` keeps the core in reset while the bus/peripherals run, so
    bus-level tests can drive ``m_*`` without any core traffic.  The caller
    owns the clock: ``reset_soc`` starts it unless ``start_clk=False``.
    """
    if start_clk:
        start_clock(dut)
    _set_defaults(dut)
    for _ in range(cycles):
        await RisingEdge(dut.clk)
    # Release both resets on the same edge: the core must not run for a few
    # clocks while the peripherals are still held in reset.
    dut.rstn.value = 1
    dut.core_rstn.value = 1 if core else 0


class CycleCounter:
    """Counts rising edges of ``dut.clk`` in a background coroutine.

    ``mark()`` records the counter value after reset; ``cycles`` is the number
    of rising edges since then.  The increment happens in a separate
    coroutine, so callers cannot rely on ``cycles`` having advanced in the
    same simulation step as their own ``RisingEdge``; read it only at the
    start of a new step (i.e. after an ``await ReadOnly()``, as tests do).
    """

    def __init__(self, dut) -> None:
        self.dut = dut
        self.edge = 0
        self.origin = 0

    async def _run(self) -> None:
        while True:
            await RisingEdge(self.dut.clk)
            self.edge += 1

    def start(self) -> None:
        cocotb.start_soon(self._run())

    def mark(self) -> None:
        self.origin = self.edge

    @property
    def cycles(self) -> int:
        return self.edge - self.origin


async def wait_cycles(dut, n: int) -> None:
    """Wait *n* rising edges."""
    for _ in range(n):
        await RisingEdge(dut.clk)
