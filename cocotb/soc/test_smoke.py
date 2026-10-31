"""Infrastructure smoke tests: clock/reset, external bus master, fake slave.

These validate the testbench plumbing (external-master mux, fake slave
decode, ready/hold semantics) before the peripheral suites rely on it.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import RESET_CYCLES, reset_soc, start_clock
from common.runner import run_tests, soc_hdl

TIMER_BASE = 0x2000_0000
FAKE_BASE = 0x3000_0000


@pytest.mark.cocotb_runner
def test_soc_smoke(hdl):
    soc_hdl(hdl)
    run_tests(hdl)


async def _bus_setup(dut):
    bm = BusMaster(dut)
    bm.start()
    await reset_soc(dut, core=False)
    bm.mark_reset()
    dut.ext_master.value = 1
    return bm


@cocotb.test()
async def test_clock_and_reset_release(dut):
    start_clock(dut)
    await reset_soc(dut, RESET_CYCLES, core=False)
    await ReadOnly()
    assert int(dut.rstn.value) == 1
    assert int(dut.core_rstn.value) == 0


@cocotb.test()
async def test_fake_slave_reset_values_and_aliasing(dut):
    bm = await _bus_setup(dut)
    r0 = await bm.read(FAKE_BASE + 0x00)
    assert r0.data == 0xA5A5_0000
    r7 = await bm.read(FAKE_BASE + 0x1C)
    assert r7.data == 0xA5A5_0007
    w4 = await bm.read(FAKE_BASE + 0x10)
    assert w4.data == 0xA5A5_0004
    # addr[7:2] decode: aliases every 256 bytes
    alias = await bm.read(FAKE_BASE + 0x100 + 0x10)
    assert alias.data == 0xA5A5_0004
    assert alias.commit_cycle > w4.commit_cycle


@cocotb.test()
async def test_fake_slave_wstrb_writes(dut):
    bm = await _bus_setup(dut)
    await bm.write(FAKE_BASE + 0x20, 0x11223344, wstrb=0b0001)
    v = await bm.read(FAKE_BASE + 0x20)
    # word 8 resets to 0xA5A5_0008: replacing byte 0 keeps 0xA5A5_00xx
    assert v.data == 0xA5A5_0044, hex(v.data)
    await bm.write(FAKE_BASE + 0x20, 0xAA000000, wstrb=0b1000)
    v = await bm.read(FAKE_BASE + 0x20)
    assert v.data == 0xAAA5_0044, hex(v.data)
    await bm.write(FAKE_BASE + 0x20, 0xDEADBEEF)
    v = await bm.read(FAKE_BASE + 0x20)
    assert v.data == 0xDEAD_BEEF


@cocotb.test()
async def test_hold_stretches_ready(dut):
    bm = await _bus_setup(dut)
    # ready held low while `hold` is high
    await RisingEdge(dut.clk)
    dut.hold.value = 1
    task = cocotb.start_soon(bm.read(FAKE_BASE + 0x04))
    for _ in range(3):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.m_ready.value) == 0
    await RisingEdge(dut.clk)      # leave ReadOnly before driving
    dut.hold.value = 0
    res = await task
    assert res.data == 0xA5A5_0001
    assert res.wait_cycles >= 2, res


@cocotb.test()
async def test_peripheral_access_ignores_hold(dut):
    bm = await _bus_setup(dut)
    dut.hold.value = 1        # hold only applies to the fake slave window
    res = await bm.read(TIMER_BASE + 0x08)
    assert res.data == 0xFFFF_FFFF, hex(res.data)
    assert res.wait_cycles == 0


@cocotb.test()
async def test_ext_ready_stalls_any_access(dut):
    bm = await _bus_setup(dut)
    await RisingEdge(dut.clk)
    bm.drive_ready(False)
    task = cocotb.start_soon(bm.read(TIMER_BASE + 0x00))
    for _ in range(4):
        await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.m_ready.value) == 0
    await RisingEdge(dut.clk)
    bm.drive_ready(True)
    res = await task
    assert res.data == 0
    assert res.wait_cycles >= 3, res


@cocotb.test()
async def test_core_held_in_reset_sees_no_bus_traffic(dut):
    bm = await _bus_setup(dut)
    seen = 0
    for _ in range(8):
        await RisingEdge(dut.clk)
        await ReadOnly()
        seen += int(dut.core_req.value)
    assert seen == 0
    assert int(dut.ext_master.value) == 1
