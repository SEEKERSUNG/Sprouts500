"""Independent Python models of the timer and GPIO peripherals.

These models are written from the RTL (rtl/timer.v, rtl/gpio.v), not from the
test expectations, and are replayed edge by edge against the recorded bus
traffic.  Cycle convention (shared with ``BusMaster``):

* cycle 1 is the first rising edge after the SoC reset was released;
* a read committed at cycle *k* sees the register state *before* edge *k*,
  i.e. the state after ``k-1`` ticks;
* a write committed at cycle *k* takes effect *at* edge *k*.

Tests record every write/read with its ``commit_cycle`` and then call
``check()``; any mismatch reports the cycle, address, expected/observed data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.misc import byte_mask

MASK32 = 0xFFFF_FFFF

TIMER_CTRL = 0x0
TIMER_COUNT = 0x4
TIMER_CMP = 0x8
TIMER_PRE = 0xC

GPIO_DIR = 0x0
GPIO_OUT = 0x4
GPIO_IN = 0x8
GPIO_IE = 0xC
GPIO_PEND = 0x10


@dataclass
class BusOp:
    cycle: int
    write: bool
    addr: int
    data: int = 0
    wstrb: int = 0xF
    rdata: int | None = None


class TimerModel:
    """Cycle-accurate model of rtl/timer.v (including W1C and alias decode)."""

    def __init__(self) -> None:
        self.writes: dict[int, tuple[int, int, int]] = {}
        self.reads: list[BusOp] = []

    # -- traffic recording -------------------------------------------------
    def note_write(self, cycle: int, addr: int, data: int, wstrb: int) -> None:
        if cycle in self.writes:
            raise AssertionError(f"two timer writes at cycle {cycle}")
        self.writes[cycle] = (addr, data & MASK32, wstrb)

    def note_read(self, cycle: int, addr: int, rdata: int) -> None:
        self.reads.append(BusOp(cycle, False, addr, rdata=rdata))

    # -- state -------------------------------------------------------------
    def reset_state(self) -> None:
        self.ctrl = 0          # bits 0/1/3 modelled, bit 2 is the pending flag
        self.count = 0
        self.cmp = MASK32
        self.pre = 0
        self.pending = 0
        self.pre_cnt = 0

    def _tick(self, write: tuple[int, int, int] | None) -> None:
        old = (self.ctrl, self.count, self.cmp, self.pre,
               self.pending, self.pre_cnt)
        ctrl, count, cmp, pre, pending, pre_cnt = old
        addr = data = wstrb = None
        if write is not None:
            addr, data, wstrb = write
            addr &= 0xF

        # register writes (RTL lines 47-59)
        write_ctrl = write is not None and addr == TIMER_CTRL and (wstrb & 1)
        if write_ctrl:
            ctrl = (ctrl & ~0b1011) | (data & 0b1011)
            if data & 0b0100:
                pending = 0
        if write is not None and addr == TIMER_CMP:
            cmp = (cmp & ~byte_mask(wstrb)) | (data & byte_mask(wstrb))
        if write is not None and addr == TIMER_PRE:
            pre = (pre & ~byte_mask(wstrb)) | (data & byte_mask(wstrb))

        # counter / prescaler (RTL lines 62-80).  timer.v assigns ctrl/cmp/pre
        # with nonblocking assignments in the same always block, so the
        # counter sees the *old* values on the edge of a register write.  In
        # particular a PRE write that lands on the same edge as a count
        # update must not suppress that update.
        old_ctrl, old_cmp, old_pre = old[0], old[2], old[3]
        if old_ctrl & 1:
            if pre_cnt >= old_pre:
                pre_cnt = 0
                if count >= old_cmp:
                    pending = 1
                    if ctrl & 0b1000:
                        count = 0
                    else:
                        ctrl &= ~1
                else:
                    count = (count + 1) & MASK32
            else:
                pre_cnt = (pre_cnt + 1) & MASK32
        else:
            pre_cnt = 0
            count = 0

        # a CTRL W1C write wins over the automatic pending set (lines 82-84)
        if write_ctrl and (data & 0b0100):
            pending = 0

        self.ctrl, self.count, self.cmp, self.pre = ctrl, count, cmp, pre
        self.pending, self.pre_cnt = pending, pre_cnt

    def state_after(self, ticks: int) -> None:
        self.reset_state()
        for c in range(1, ticks + 1):
            self._tick(self.writes.get(c))

    def rdata(self, addr: int) -> int:
        reg = addr & 0xF
        if reg == TIMER_CTRL:
            return ((self.ctrl & 1) | ((self.ctrl >> 1) & 1) << 1
                    | (self.pending & 1) << 2 | ((self.ctrl >> 3) & 1) << 3)
        if reg == TIMER_COUNT:
            return self.count
        if reg == TIMER_CMP:
            return self.cmp
        if reg == TIMER_PRE:
            return self.pre
        return 0

    def expected_read(self, cycle: int, addr: int) -> int:
        self.state_after(cycle - 1)
        return self.rdata(addr)

    # -- scoreboard --------------------------------------------------------
    def check(self) -> list[str]:
        errors = []
        for op in self.reads:
            got = op.rdata
            exp = self.expected_read(op.cycle, op.addr)
            if got != exp:
                errors.append(
                    f"timer read cycle={op.cycle} addr=0x{op.addr:08x}: "
                    f"expected 0x{exp:08x}, got {got if got is None else f'0x{got:08x}'}"
                )
        return errors


class GpioModel:
    """Cycle-accurate model of rtl/gpio.v register + edge-detect behaviour.

    ``note_input(cycle, value)`` schedules the pad value sampled at edge
    *cycle*; the value stays until the next scheduled change.  Tests drive the
    pins right after an edge, so the next edge number is ``bm.cycles + 1``
    (``BusMaster.cycles`` is derived from simulation time, so this is exact).
    """

    def __init__(self) -> None:
        self.writes: dict[int, tuple[int, int, int]] = {}
        self.reads: list[BusOp] = []
        self.inputs: dict[int, int] = {}

    def note_write(self, cycle: int, addr: int, data: int, wstrb: int) -> None:
        if cycle in self.writes:
            raise AssertionError(f"two gpio writes at cycle {cycle}")
        self.writes[cycle] = (addr, data & MASK32, wstrb)

    def note_read(self, cycle: int, addr: int, rdata: int) -> None:
        self.reads.append(BusOp(cycle, False, addr, rdata=rdata))

    def note_input(self, cycle: int, value: int) -> None:
        self.inputs[cycle] = value & MASK32

    def input_at(self, cycle: int) -> int:
        value = 0
        for c in sorted(c for c in self.inputs if c <= cycle):
            value = self.inputs[c]
        return value

    def reset_state(self) -> None:
        self.dir = 0
        self.out = 0
        self.ie = 0
        self.pend = 0
        self.in_s0 = 0
        self.in_s1 = 0
        self.last_input = 0

    def _tick(self, cycle: int, write: tuple[int, int, int] | None) -> None:
        old = (self.dir, self.out, self.ie, self.pend, self.in_s0, self.in_s1)
        dir_, out_, ie_, pend_, in0, in1 = old
        addr = data = wstrb = None
        if write is not None:
            addr, data, wstrb = write
            addr &= 0xFF
            mask = byte_mask(wstrb)
            if addr == GPIO_DIR:
                dir_ = (dir_ & ~mask) | (data & mask)
            elif addr == GPIO_OUT:
                out_ = (out_ & ~mask) | (data & mask)
            elif addr == GPIO_IE:
                ie_ = (ie_ & ~mask) | (data & mask)

        # The testbench pad is out when DIR=1, otherwise the external drive
        # (gpio_ext_en drives zeros when the test has not enabled the external
        # world, which is the GpioModel default).  Register values here are
        # the post-edge-(cycle-1) state; gpio.v samples gpio_i into in_s0 and
        # compares the old in_s0/in_s1, so the pad of this edge also decides
        # whether in_edge fires.
        new_in0 = (out_ & dir_) | (self.input_at(cycle) & ~dir_)
        in_edge = new_in0 ^ in0
        w1c = 0
        if write is not None and addr == GPIO_PEND:
            w1c = data & byte_mask(wstrb)
        pend_ = (pend_ | (in_edge & ie_)) & ~w1c

        self.dir, self.out, self.ie, self.pend = dir_, out_, ie_, pend_
        self.in_s0, self.in_s1 = new_in0, in0
        self.last_input = new_in0

    def state_after(self, ticks: int) -> None:
        self.reset_state()
        for c in range(1, ticks + 1):
            self._tick(c, self.writes.get(c))

    def rdata(self, addr: int) -> int:
        reg = addr & 0xFF
        if reg == GPIO_DIR:
            return self.dir
        if reg == GPIO_OUT:
            return self.out
        if reg == GPIO_IN:
            return self.in_s1
        if reg == GPIO_IE:
            return self.ie
        if reg == GPIO_PEND:
            return self.pend
        return 0

    def expected_read(self, cycle: int, addr: int) -> int:
        self.state_after(cycle - 1)
        return self.rdata(addr)

    def check(self) -> list[str]:
        errors = []
        for op in self.reads:
            got = op.rdata
            exp = self.expected_read(op.cycle, op.addr)
            if got != exp:
                errors.append(
                    f"gpio read cycle={op.cycle} addr=0x{op.addr:08x}: "
                    f"expected 0x{exp:08x}, got {got if got is None else f'0x{got:08x}'}"
                )
        return errors


class Scoreboard:
    """Collects timer/gpio traffic and checks it with both models."""

    def __init__(self) -> None:
        self.timer = TimerModel()
        self.gpio = GpioModel()
        self.errors: list[str] = []

    @staticmethod
    def _owner(addr: int) -> str | None:
        if addr & 0xFFFF_F000 == 0x2000_0000:
            return "timer"
        if addr & 0xFFFF_F000 == 0x2000_1000:
            return "gpio"
        return None

    def write(self, commit_cycle: int, addr: int, data: int, wstrb: int) -> None:
        owner = self._owner(addr)
        if owner == "timer":
            self.timer.note_write(commit_cycle, addr, data, wstrb)
        elif owner == "gpio":
            self.gpio.note_write(commit_cycle, addr, data, wstrb)

    def read(self, commit_cycle: int, addr: int, rdata: int) -> None:
        if rdata is None:
            self.errors.append(
                f"read cycle={commit_cycle} addr=0x{addr:08x} returned X/Z"
            )
            return
        owner = self._owner(addr)
        if owner == "timer":
            self.timer.note_read(commit_cycle, addr, rdata)
        elif owner == "gpio":
            self.gpio.note_read(commit_cycle, addr, rdata)

    def check(self) -> None:
        errors = self.timer.check() + self.gpio.check() + self.errors
        if errors:
            raise AssertionError("scoreboard mismatch:\n  " + "\n  ".join(errors))
