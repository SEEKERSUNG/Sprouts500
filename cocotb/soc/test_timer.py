"""Timer peripheral tests (rtl/timer.v).

Covered:
* reset values (docs/registers.md section 8);
* prescaler: the count advances once every PRE+1 clocks;
* one-shot: at CMP the pend flag sets and EN self-clears; count then stays 0;
* periodic: count wraps to 0 and keeps running;
* CTRL W1C: writing 0x0f clears pending but keeps EN/IE/PERIODIC;
* COUNT is read-only (written by nobody but hardware) and never decrements;
* irq == pending && IRQ_EN;
* byte-lane writes to CMP/PRE and the CTRL strobe rules;
* address aliasing (timer decodes addr[3:0], so +0x10 hits CTRL);
* randomized register traffic replayed against a cycle-accurate Python model.

Every access goes through BusMaster on the real `bus`.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc, wait_cycles
from common.misc import make_rng
from common.models import Scoreboard
from common.runner import run_tests, soc_hdl

TIMER = 0x2000_0000
CTRL = TIMER + 0x00
COUNT = TIMER + 0x04
CMP = TIMER + 0x08
PRE = TIMER + 0x0C

EN, IRQ_EN, PEND, PERIODIC = 1, 2, 4, 8


@pytest.mark.cocotb_runner
def test_timer(hdl):
    soc_hdl(hdl)
    run_tests(hdl)


async def _setup(dut):
    bm = BusMaster(dut)
    bm.start()
    await reset_soc(dut, core=False)
    bm.mark_reset()
    dut.ext_master.value = 1
    return bm


async def _check_irq_on_edge(dut, expect: bool) -> None:
    await RisingEdge(dut.clk)
    await ReadOnly()
    got = int(dut.timer_irq_o.value)
    assert got == (1 if expect else 0), f"timer_irq={got} expected {expect}"


async def _idle_clocks(dut, n: int) -> None:
    """Idle the external master for *n* clocks.

    ``wait_cycles`` waits from the current phase, which can share an edge
    with the next BusMaster launch and shorten the idle period; this version
    always consumes the current edge first.
    """
    for _ in range(n):
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)


async def _read_until(dut, bm, addr: int, mask: int, timeout: int = 500):
    for _ in range(timeout):
        val = (await bm.read(addr)).data
        if val & mask:
            return val
    raise AssertionError(f"0x{addr:08x} never had mask 0x{mask:x} set")


@cocotb.test()
async def test_timer_reset_values(dut):
    bm = await _setup(dut)
    assert (await bm.read(CTRL)).data == 0
    assert (await bm.read(COUNT)).data == 0
    assert (await bm.read(CMP)).data == 0xFFFF_FFFF
    assert (await bm.read(PRE)).data == 0
    # undefined offsets read as zero
    assert (await bm.read(TIMER + 0x1C)).data == 0
    await _check_irq_on_edge(dut, False)


@cocotb.test()
async def test_timer_prescaler(dut):
    bm = await _setup(dut)
    for pre in (0, 1, 3, 7):
        await bm.write(CMP, 0xFFFF)
        await bm.write(PRE, pre)
        await bm.write(CTRL, EN)
        r1 = await bm.read(COUNT)
        await _idle_clocks(dut, 4 * (pre + 1))
        r2 = await bm.read(COUNT)
        # COUNT snapshots are taken one tick apart per commit cycle; with a
        # prescaler of pre+1 the count advances once per pre+1 edges.
        elapsed = r2.commit_cycle - r1.commit_cycle
        expected = elapsed / (pre + 1)
        got = r2.data - r1.data
        assert abs(got - expected) <= 1, (pre, r1.data, r2.data, elapsed, expected)
        await bm.write(CTRL, 0)
        # disabled: count snapshots to zero
        assert (await bm.read(COUNT)).data == 0


@cocotb.test()
async def test_timer_one_shot(dut):
    bm = await _setup(dut)
    await bm.write(CMP, 5)
    await bm.write(CTRL, EN)
    # count reaches CMP and stops there, EN clears, pending sets
    for _ in range(200):
        ctrl = (await bm.read(CTRL)).data
        count = (await bm.read(COUNT)).data
        if ctrl & PEND:
            break
    else:
        raise AssertionError("timer never raised pending")
    # COUNT==CMP is visible for only one cycle before EN=0 resets it to 0,
    # so the architectural check is pending set + EN self-cleared.
    assert (ctrl & EN) == 0
    await _check_irq_on_edge(dut, False)      # pending but IE=0
    await wait_cycles(dut, 20)
    assert (await bm.read(COUNT)).data == 0, "one-shot count must stay at 0"
    assert (await bm.read(CTRL)).data & PEND


@cocotb.test()
async def test_timer_w1c_keeps_configuration(dut):
    bm = await _setup(dut)
    await bm.write(CMP, 64)                 # long enough to observe W1C before re-trigger
    await bm.write(CTRL, EN | IRQ_EN | PERIODIC)
    await _read_until(dut, bm, CTRL, PEND)
    await _check_irq_on_edge(dut, True)
    # docs/registers.md: write 0x0f to clear pending without disturbing EN/IE
    await bm.write(CTRL, 0x0F)
    ctrl = (await bm.read(CTRL)).data
    assert ctrl & PEND == 0, hex(ctrl)
    assert ctrl & EN, hex(ctrl)
    assert ctrl & IRQ_EN, hex(ctrl)
    assert ctrl & PERIODIC, hex(ctrl)
    await _check_irq_on_edge(dut, False)


@cocotb.test()
async def test_timer_periodic(dut):
    bm = await _setup(dut)
    await bm.write(PRE, 2)                 # one count every 3 edges
    await bm.write(CMP, 4)
    await bm.write(CTRL, EN | PERIODIC)
    seen = []
    wraps = 0
    for _ in range(60):
        c = (await bm.read(COUNT)).data
        assert 0 <= c <= 4, c
        if seen and c < seen[-1]:
            wraps += 1
        seen.append(c)
    assert len(set(seen)) > 1, seen
    assert wraps >= 1, seen
    ctrl = (await bm.read(CTRL)).data
    assert ctrl & EN and ctrl & PERIODIC and ctrl & PEND, hex(ctrl)


@cocotb.test()
async def test_timer_count_never_decrements(dut):
    bm = await _setup(dut)
    await bm.write(PRE, 2)
    await bm.write(CMP, 0x1000)
    await bm.write(CTRL, EN)
    prev = 0
    for _ in range(60):
        c = (await bm.read(COUNT)).data
        assert c >= prev, (prev, c)
        prev = c
    await bm.write(CTRL, 0)


@cocotb.test()
async def test_timer_count_read_only(dut):
    bm = await _setup(dut)
    await bm.write(PRE, 1)
    await bm.write(CMP, 0x1000)
    await bm.write(CTRL, EN)
    await wait_cycles(dut, 10)
    before = (await bm.read(COUNT)).data
    await bm.write(COUNT, 0xDEAD_BEEF)
    after = (await bm.read(COUNT)).data
    # hardware still counts: the written value must not appear
    assert after != 0xDEAD_BEEF, hex(after)
    assert after >= before, (before, after)
    await bm.write(CTRL, 0)


@cocotb.test()
async def test_timer_cmp_pre_byte_lanes(dut):
    bm = await _setup(dut)
    await bm.write(CMP, 0x1111_1111)
    await bm.write(CMP, 0xAB00_0000, wstrb=0b1000)
    assert (await bm.read(CMP)).data == 0xAB11_1111
    await bm.write(CMP, 0x0000_CD00, wstrb=0b0010)
    assert (await bm.read(CMP)).data == 0xAB11_CD11
    await bm.write(PRE, 0x0000_00FF, wstrb=0b0001)
    assert (await bm.read(PRE)).data == 0xFF
    await bm.write(PRE, 0x7FFF_0000, wstrb=0b1100)
    assert (await bm.read(PRE)).data == 0x7FFF_00FF


@cocotb.test()
async def test_timer_ctrl_strobe_rules(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, EN | PERIODIC)
    # no strobe on byte 0: CTRL bits unchanged
    await bm.write(CTRL, 0x0000_0000, wstrb=0b1110)
    ctrl = (await bm.read(CTRL)).data
    assert ctrl & EN and ctrl & PERIODIC
    # W1C only honoured with wstrb[0]
    await bm.write(CTRL, PERIODIC, wstrb=0b1110)  # bit 2 set, no byte-0 strobe
    await bm.write(CTRL, 0, wstrb=0b0001)
    assert (await bm.read(CTRL)).data == 0


@cocotb.test()
async def test_timer_irq_requires_enable(dut):
    bm = await _setup(dut)
    await bm.write(CMP, 2)
    await bm.write(CTRL, EN)          # no IRQ_EN
    await _read_until(dut, bm, CTRL, PEND)
    await _check_irq_on_edge(dut, False)
    await bm.write(CTRL, IRQ_EN)      # pending already set, IE now on
    await _check_irq_on_edge(dut, True)
    await bm.write(CTRL, 0x0F)
    await _check_irq_on_edge(dut, False)


@cocotb.test()
async def test_timer_addr_alias(dut):
    """timer.v decodes addr[3:0]: base+0x10 is CTRL, base+0x18 is CMP."""
    bm = await _setup(dut)
    await bm.write(TIMER + 0x10, EN | PERIODIC)
    assert (await bm.read(CTRL)).data == (EN | PERIODIC)
    assert (await bm.read(TIMER + 0x10)).data == (EN | PERIODIC)
    await bm.write(TIMER + 0x18, 7)
    assert (await bm.read(CMP)).data == 7
    await bm.write(CTRL, 0)
    await bm.write(CMP, 0xFFFF_FFFF)


@cocotb.test()
async def test_timer_randomized_scoreboard(dut):
    rng = make_rng("timer_randomized")
    bm = await _setup(dut)
    sb = Scoreboard()

    async def wr(addr, data, wstrb=0xF):
        res = await bm.write(addr, data, wstrb)
        sb.write(res.commit_cycle, addr, data, wstrb)
        cocotb.log.info("random op: write addr=0x%08x cycle=%d data=0x%08x "
                        "wstrb=%s", addr, res.commit_cycle, data,
                        format(wstrb, "04b"))

    async def rd(addr):
        res = await bm.read(addr)
        sb.read(res.commit_cycle, addr, res.data)
        cocotb.log.info("random op: read  addr=0x%08x cycle=%d data=0x%08x",
                        addr, res.commit_cycle, res.data)

    wr_choices = [(CTRL, 0b1111), (CTRL, 0b0001), (CMP, 0b1111), (CMP, 0b0100),
                  (CMP, 0b0001), (PRE, 0b1111), (PRE, 0b0001), (PRE, 0b0010)]
    regs = [CTRL, COUNT, CMP, PRE, TIMER + 0x10, TIMER + 0x1C]
    for step in range(120):
        kind = rng.random()
        if kind < 0.45:
            addr, strb = rng.choice(wr_choices)
            await wr(addr, rng.getrandbits(32), strb)
        else:
            await rd(rng.choice(regs))
    await wr(CTRL, 0)
    # idle only after the timer is stopped: with a small CMP a still-running
    # timer can roll over again between the read's commit edge and the
    # Python model's replay, which only sees recorded transactions.
    await _idle_clocks(dut, 5)
    for reg in (CTRL, COUNT, CMP, PRE):
        await rd(reg)
    sb.check()
