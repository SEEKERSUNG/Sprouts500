"""Driver/monitor for the Sprouts500 simple bus.

The bus starts a transaction on the rising edge of ``req`` (``req && !req_q``)
and strobes the selected peripheral for exactly one clock.  It holds the read
value while ``req`` stays high, so the master may keep ``req`` asserted while a
slow slave has ``ready`` low.

``BusMaster`` therefore drives a one-cycle ``req`` pulse for a normal access,
and a held ``req`` (``read_held`` / ``write_held``) to exercise that path.
``drive_ready`` controls the external-master ready input, which the testbench
ANDs with the fake slave's ready, so tests can stall transactions.

Timing returned to callers is in *bus cycles*: cycle 1 is the first rising edge
after the SoC reset was released.  This is the same clock the timer and GPIO
models use, so a scoreboard can replay a write at exactly that edge.
"""
from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import ReadOnly, RisingEdge
from cocotb.utils import get_sim_time

from common.clock_reset import CLK_PERIOD_NS, CycleCounter


@dataclass
class BusResult:
    data: int
    """Read data (undefined/ignored for writes)."""
    commit_cycle: int
    """
    Bus cycle of the transaction edge, where cycle 1 is the first rising edge
    after the reset was released.  A write is sampled by the peripheral and a
    read captures its data on exactly this edge.  The value is derived from
    simulation time, so it does not depend on coroutine scheduling.
    """
    wait_cycles: int
    """Extra cycles spent waiting for ``ready``."""


class BusMaster:
    def __init__(self, dut, max_wait: int = 20000) -> None:
        self.dut = dut
        self.clk = dut.clk
        self.max_wait = max_wait
        self.cycle = CycleCounter(dut)
        # Simulation time (ps) of the reset-release edge.  Cycle numbers are
        # derived from it instead of the CycleCounter task: the task resumes
        # in an arbitrary order relative to the caller, which made
        # commit_cycle one edge early or late depending on scheduling.
        self._origin_ps: int | None = None
        self._period_ps = CLK_PERIOD_NS * 1000

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the cycle counter (one background task per simulation)."""
        self.cycle.start()

    def mark_reset(self) -> None:
        """Call right after ``reset_soc`` returned; defines bus cycle 0."""
        self._origin_ps = int(get_sim_time(unit="ps"))
        self.cycle.mark()

    def _edge_index(self) -> int:
        """Rising-edge number since reset release (the release edge is 0)."""
        if self._origin_ps is None:
            return 0
        now = int(get_sim_time(unit="ps"))
        return int(round((now - self._origin_ps) / self._period_ps))

    @property
    def cycles(self) -> int:
        """Current rising-edge index; deterministic, unlike the task count."""
        return self._edge_index()

    def drive_ready(self, level: bool) -> None:
        """Drive the external master's ready input (False stalls the bus)."""
        self.dut.ext_ready.value = 1 if level else 0

    # -- transactions ------------------------------------------------------
    async def _transaction(
        self,
        addr: int,
        data: int,
        we: bool,
        wstrb: int,
        extra_hold: int = 0,
    ) -> BusResult:
        dut = self.dut
        # Let one whole clock elapse after the caller's current phase.  This
        # works whether the caller is in ReadOnly or the write phase, and it
        # guarantees req was low for at least one full cycle (req_q cleared).
        await RisingEdge(self.clk)
        dut.m_req.value = 1
        dut.m_we.value = 1 if we else 0
        dut.m_addr.value = addr & 0xFFFF_FFFF
        dut.m_wdata.value = data & 0xFFFF_FFFF
        dut.m_wstrb.value = wstrb & 0xF

        # The edge that the peripheral samples; data/commit timing is relative
        # to this edge even if ready is still low.
        await RisingEdge(self.clk)
        commit_cycle = self._edge_index()
        await ReadOnly()
        waited = 0
        data_out = int(dut.m_rdata.value)
        while not int(dut.m_ready.value):
            waited += 1
            if waited > self.max_wait:
                raise TimeoutError(
                    f"bus access addr=0x{addr:08x} never became ready "
                    f"({waited} cycles)"
                )
            await RisingEdge(self.clk)
            await ReadOnly()
            data_out = int(dut.m_rdata.value)
        # Keep req asserted for N more cycles when the test wants to observe
        # held-read behaviour (the value must not change and no side effect may
        # repeat).
        for _ in range(extra_hold):
            await RisingEdge(self.clk)
            await ReadOnly()
            data_out = int(dut.m_rdata.value)

        # Deassert after an edge (writes during ReadOnly are not allowed).
        await RisingEdge(self.clk)
        dut.m_req.value = 0
        dut.m_we.value = 0
        dut.m_wstrb.value = 0
        await ReadOnly()
        return BusResult(data_out, commit_cycle, waited)

    async def write(
        self, addr: int, data: int, wstrb: int = 0xF, extra_hold: int = 0
    ) -> BusResult:
        return await self._transaction(addr, data, True, wstrb, extra_hold)

    async def read(self, addr: int) -> BusResult:
        return await self._transaction(addr, 0, False, 0xF)

    async def read_held(self, addr: int, hold_cycles: int = 2) -> BusResult:
        """Read while keeping ``req`` high for ``hold_cycles`` extra cycles."""
        return await self._transaction(addr, 0, False, 0xF, hold_cycles)

    async def write_held(self, addr: int, data: int, wstrb: int = 0xF,
                         hold_cycles: int = 2) -> BusResult:
        """Write while keeping ``req`` high (checks no duplicate write)."""
        return await self._transaction(addr, data, True, wstrb, hold_cycles)
