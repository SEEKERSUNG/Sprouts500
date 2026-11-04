"""I2C master peripheral tests (rtl/i2c.v).

A clocked, independent 7-bit slave/EEPROM model watches and drives the real
open-drain pads. Coverage includes register semantics, addressed writes and
reads, repeated START, sequential reads, ACK/NACK recovery, completion IRQ,
SCL stretching, invalid/busy commands, byte lanes and randomized transfers.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc
from common.i2c_bfm import (
    CMD_NACK,
    CMD_READ,
    CMD_START,
    CMD_STOP,
    CMD_WRITE,
    CTRL_EN,
    CTRL_IRQ,
    I2C_BASE,
    I2C_CLKDIV,
    I2C_CMD,
    I2C_CTRL,
    I2C_RXDATA,
    I2C_STATUS,
    I2C_TXDATA,
    STATUS_ACTIVE,
    STATUS_BUSY,
    STATUS_DONE,
    STATUS_NACK,
    STATUS_SCL,
    STATUS_SDA,
    I2CSlave,
    i2c_command,
    i2c_read,
    i2c_stop,
    i2c_write,
)
from common.misc import make_rng
from common.runner import run_tests, soc_hdl


@pytest.mark.cocotb_runner
def test_i2c(hdl):
    soc_hdl(hdl)
    run_tests(hdl)


async def _setup(dut, div: int = 3):
    bm = BusMaster(dut, max_wait=40000)
    bm.start()
    await reset_soc(dut, core=False)
    bm.mark_reset()
    dut.ext_master.value = 1
    slave = I2CSlave(dut)
    await bm.write(I2C_CLKDIV, div)
    await bm.write(I2C_CTRL, CTRL_EN)
    return bm, slave


async def _irq(dut) -> int:
    await RisingEdge(dut.clk)
    await ReadOnly()
    return int(dut.i2c_irq_o.value)


@cocotb.test()
async def test_i2c_reset_values(dut):
    bm = BusMaster(dut)
    bm.start()
    await reset_soc(dut, core=False)
    bm.mark_reset()
    dut.ext_master.value = 1
    assert (await bm.read(I2C_CTRL)).data == 0
    assert (await bm.read(I2C_STATUS)).data & 0xF == 0
    assert (await bm.read(I2C_CLKDIV)).data == 499
    assert (await bm.read(I2C_TXDATA)).data == 0
    assert (await bm.read(I2C_RXDATA)).data == 0
    assert (await bm.read(I2C_CMD)).data == 0
    status = (await bm.read(I2C_STATUS)).data
    assert status & STATUS_SCL and status & STATUS_SDA
    assert await _irq(dut) == 0


@cocotb.test()
async def test_i2c_multibyte_write(dut):
    bm, slave = await _setup(dut)
    statuses = await i2c_write(dut, bm, 0x50, 0x20, b"\xA5\x3C")
    assert all((s & STATUS_DONE) and not (s & STATUS_NACK) for s in statuses), (
        [hex(s) for s in statuses], slave.received, slave.starts, slave.stops)
    assert not (statuses[-1] & (STATUS_BUSY | STATUS_ACTIVE))
    assert slave.received == [0xA0, 0x20, 0xA5, 0x3C]
    assert slave.mem[0x20:0x23] == b"\xA5\x3C\x00"
    assert slave.starts == 1 and slave.stops == 1
    assert slave.scl_rises == 4 * 9 + 1  # ninth-bit clocks + STOP SCL release


@cocotb.test()
async def test_i2c_repeated_start_and_sequential_read(dut):
    bm, slave = await _setup(dut)
    slave.mem[0x30:0x34] = b"\x96\x5A\xFF\x00"
    data, statuses = await i2c_read(dut, bm, 0x50, 0x30, 4)
    assert data == b"\x96\x5A\xFF\x00"
    assert all(not (s & STATUS_NACK) for s in statuses)
    assert slave.received == [0xA0, 0x30, 0xA1]
    assert slave.transmitted == list(data)
    assert slave.starts == 2 and slave.stops == 1
    assert slave.scl_rises == (3 + 4) * 9 + 2  # repeated START + STOP releases
    assert not (statuses[-1] & STATUS_ACTIVE)


@cocotb.test()
async def test_i2c_address_nack_and_stop_recovery(dut):
    bm, slave = await _setup(dut)
    slave.ack_address = False
    status = await i2c_command(dut, bm, 0xA0, CMD_START | CMD_WRITE)
    assert status & STATUS_DONE and status & STATUS_NACK
    assert status & STATUS_ACTIVE and not (status & STATUS_BUSY)
    status = await i2c_stop(dut, bm)
    assert status & STATUS_DONE and not (status & STATUS_ACTIVE)
    assert slave.stops == 1

    # A NACK does not wedge the engine; the next addressed transfer succeeds.
    slave.ack_address = True
    statuses = await i2c_write(dut, bm, 0x50, 0x10, b"\x42")
    assert all(not (s & STATUS_NACK) for s in statuses)
    assert slave.mem[0x10] == 0x42


@cocotb.test()
async def test_i2c_data_nack_and_status_w1c(dut):
    bm, slave = await _setup(dut)
    slave.ack_data = False
    await i2c_command(dut, bm, 0xA0, CMD_START | CMD_WRITE)
    status = await i2c_command(dut, bm, 0x55, CMD_WRITE | CMD_STOP)
    assert status & STATUS_NACK and status & STATUS_DONE
    assert not (status & STATUS_ACTIVE)
    assert slave.mem[0] == 0
    await bm.write(I2C_STATUS, STATUS_NACK | STATUS_DONE)
    status = (await bm.read(I2C_STATUS)).data
    assert not (status & (STATUS_NACK | STATUS_DONE))


@cocotb.test()
async def test_i2c_done_irq_and_disable(dut):
    bm, slave = await _setup(dut)
    await i2c_write(dut, bm, 0x50, 0x01, b"\x99")
    await bm.write(I2C_CTRL, CTRL_EN | CTRL_IRQ)
    assert await _irq(dut) == 1
    await bm.write(I2C_STATUS, STATUS_DONE)
    assert await _irq(dut) == 0
    assert not ((await bm.read(I2C_STATUS)).data & STATUS_DONE)

    await bm.write(I2C_CTRL, 0)
    assert (await bm.read(I2C_STATUS)).data & 0xF == 0
    status = (await bm.read(I2C_STATUS)).data
    assert status & STATUS_SCL and status & STATUS_SDA


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    bm, slave = await _setup(dut, div=4)
    t0 = bm.cycles
    await i2c_write(dut, bm, 0x50, 0x22, b"\x11")
    baseline = bm.cycles - t0

    slave.stretch_next(60)
    t0 = bm.cycles
    statuses = await i2c_write(dut, bm, 0x50, 0x22, b"\x22")
    stretched = bm.cycles - t0
    assert all(not (s & STATUS_NACK) for s in statuses)
    assert stretched >= baseline + 40, (baseline, stretched)
    assert slave.mem[0x22] == 0x22


@cocotb.test()
async def test_i2c_invalid_and_busy_commands_ignored(dut):
    bm, slave = await _setup(dut, div=20)
    await bm.write(I2C_CTRL, 0)
    await bm.write(I2C_CMD, CMD_START | CMD_WRITE)
    assert (await bm.read(I2C_STATUS)).data & 0xF == 0
    assert slave.starts == 0

    await bm.write(I2C_CTRL, CTRL_EN)
    await bm.write(I2C_CMD, CMD_READ | CMD_WRITE)
    await bm.write(I2C_CMD, CMD_START | CMD_STOP)
    assert not ((await bm.read(I2C_STATUS)).data & STATUS_BUSY)
    assert slave.starts == 0

    await bm.write(I2C_TXDATA, 0xA0)
    await bm.write(I2C_CMD, CMD_START | CMD_WRITE)
    assert (await bm.read(I2C_STATUS)).data & STATUS_BUSY
    await bm.write(I2C_TXDATA, 0x55)
    await bm.write(I2C_CMD, CMD_WRITE | CMD_STOP)  # ignored while busy
    while not ((await bm.read(I2C_STATUS)).data & STATUS_DONE):
        pass
    assert slave.received == [0xA0]
    assert not ((await bm.read(I2C_STATUS)).data & STATUS_NACK)
    await i2c_stop(dut, bm)


@cocotb.test()
async def test_i2c_byte_lanes_alias_and_start_only(dut):
    bm, slave = await _setup(dut)
    await bm.write(I2C_CLKDIV, 0x07, wstrb=0b0001)
    assert (await bm.read(I2C_CLKDIV)).data == 0x07
    await bm.write(I2C_CLKDIV, 0x1200, wstrb=0b0010)
    assert (await bm.read(I2C_CLKDIV)).data == 0x1207
    await bm.write(I2C_CLKDIV, 0xAB00_0000, wstrb=0b1000)
    assert (await bm.read(I2C_CLKDIV)).data == 0xAB00_1207

    await bm.write(I2C_BASE + 0x10C, 0x5A)
    assert (await bm.read(I2C_TXDATA)).data == 0x5A
    await bm.write(I2C_CLKDIV, 3)
    status = await i2c_command(dut, bm, 0, CMD_START)
    assert status & STATUS_DONE and status & STATUS_ACTIVE
    assert slave.starts == 1
    status = await i2c_stop(dut, bm)
    assert not (status & STATUS_ACTIVE)
    assert slave.stops == 1


@cocotb.test()
async def test_i2c_randomized_eeprom_roundtrip(dut):
    rng = make_rng("i2c_eeprom")
    bm, slave = await _setup(dut)
    for index in range(10):
        pointer = rng.randrange(0, 252)
        length = rng.randrange(1, 5)
        payload = bytes(rng.getrandbits(8) for _ in range(length))
        div = rng.randrange(2, 7)
        dut._log.info(
            f"random I2C {index}: ptr=0x{pointer:02x} len={length} "
            f"data={payload.hex()} div={div}")
        writes = await i2c_write(dut, bm, 0x50, pointer, payload, div=div)
        assert all(not (s & STATUS_NACK) for s in writes)
        data, reads = await i2c_read(dut, bm, 0x50, pointer, length, div=div)
        assert data == payload, (pointer, payload, data)
        assert all(not (s & STATUS_NACK) for s in reads)
