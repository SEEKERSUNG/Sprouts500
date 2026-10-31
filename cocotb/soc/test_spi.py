"""SPI peripheral tests (rtl/spi.v).

Covered:
* reset values (docs/registers.md section 8), idle pins and CS polarity;
* CS register semantics: bit n drives cs_n[n] low, only wstrb[0] is honoured,
  and CS keeps its value across transfers;
* loopback in all four modes (CPOL/CPHA 0..3) and both bit orders;
* SCLK half-period timing from CLKDIV and the number of clock pulses;
* BUSY during the transfer, DONE at the end, and RXDATA read clearing DONE;
* IRQ = DONE && IRQ_EN;
* TXDATA writes ignored while disabled or busy;
* CLKDIV byte-lane writes, address aliasing (addr[7:0] decode) and STATUS
  being read-only;
* randomized loopback transfers across modes/bit orders.

The TB connects spi_miso to spi_mosi when ``spi_loopback`` is set, so a
transfer is checked against the byte that was shifted out.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc
from common.misc import make_rng
from common.runner import run_tests, soc_hdl
from common.spi_bfm import (
    CTRL_CPHA,
    CTRL_CPOL,
    CTRL_EN,
    CTRL_IRQ,
    CTRL_LSB,
    SPI_BASE,
    SPI_CLKDIV,
    SPI_CS,
    SPI_CTRL,
    SPI_RXDATA,
    SPI_STATUS,
    SPI_TXDATA,
    sample_sclk,
    spi_loopback_transfer,
    spi_wait_done,
)


@pytest.mark.cocotb_runner
def test_spi(hdl):
    soc_hdl(hdl)
    run_tests(hdl)


async def _setup(dut):
    bm = BusMaster(dut)
    bm.start()
    await reset_soc(dut, core=False)
    bm.mark_reset()
    dut.ext_master.value = 1
    return bm


async def _idle(dut, n: int) -> None:
    for _ in range(n):
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)


async def _irq(dut):
    await RisingEdge(dut.clk)
    await ReadOnly()
    return int(dut.spi_irq_o.value)


async def _pins(dut):
    await RisingEdge(dut.clk)
    await ReadOnly()
    return (int(dut.spi_sclk.value), int(dut.spi_mosi.value),
            int(dut.spi_cs_n.value))


@cocotb.test()
async def test_spi_reset_values(dut):
    bm = await _setup(dut)
    assert (await bm.read(SPI_CTRL)).data == 0
    assert (await bm.read(SPI_STATUS)).data == 0
    assert (await bm.read(SPI_CLKDIV)).data == 24
    assert (await bm.read(SPI_RXDATA)).data == 0
    assert (await bm.read(SPI_TXDATA)).data == 0        # write-only
    assert (await bm.read(SPI_CS)).data == 0
    assert (await bm.read(SPI_BASE + 0x18)).data == 0
    assert await _pins(dut) == (0, 0, 0xF)
    assert await _irq(dut) == 0


@cocotb.test()
async def test_spi_cs_register(dut):
    bm = await _setup(dut)
    await bm.write(SPI_CS, 0x1)
    assert (await bm.read(SPI_CS)).data == 0x1
    assert (await _pins(dut))[2] == 0xE          # cs_n[0] asserted low
    await bm.write(SPI_CS, 0xA)
    assert (await _pins(dut))[2] == 0x5          # ~0xA
    # only wstrb[0] matters for CS
    await bm.write(SPI_CS, 0xF, wstrb=0b1110)
    assert (await bm.read(SPI_CS)).data == 0xA
    await bm.write(SPI_CS, 0)
    assert (await _pins(dut))[2] == 0xF


async def _first_bad_mode(dut, bm, tx, clk_div):
    """Return (cpol, cpha, lsb, rx) for the first failing loopback mode."""
    for cpol in (0, 1):
        for cpha in (0, 1):
            for lsb in (False, True):
                rx, _, _ = await spi_loopback_transfer(
                    dut, bm, tx, cpol, cpha, lsb, clk_div)
                if rx != tx:
                    return cpol, cpha, lsb, rx
    return None


# rtl/spi.v bug: in loopback (MISO == MOSI) a transfer with CLKDIV == 1
# samples the previous MOSI bit.  The transmit shifter itself is correct --
# every transition seen on spi_mosi is the configured TX byte -- but the
# receive side of spi.v samples miso_s1 one clock after the bit boundary:
#
#   spi.v: `miso_s1 <= miso_s0;`  (first edge, line 120)  and
#          `rx_shift[...] <= miso_s1;` in the step counter block (line 144)
#
# so rx_shift captures the value miso_s1 held *before* that edge, i.e. MOSI
# from the previous bit.  With the two-clock MISO path (miso -> miso_s0 ->
# miso_s1 -> rx_shift) CLKDIV >= 2 gives the sample enough time and behaves
# correctly; CLKDIV <= 1 is off by one bit.  The reset value of CLKDIV is 24,
# so the bug is not visible in the default configuration.  Tests record the
# observed behaviour; the RTL is not modified.
@cocotb.test()
async def test_spi_loopback_clkdiv_one_bit_skew(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    bad = await _first_bad_mode(dut, bm, 0x96, 1)
    assert bad is not None, (
        "spi.v CLKDIV=1 loopback now matches TX: the RTL bug in spi.v "
        "(rx_shift samples miso_s1 one clock late) appears fixed -- update "
        "docs/verification_cocotb.md and this test"
    )
    cpol, cpha, lsb, rx = bad
    dut._log.info(
        f"spi.v loopback CLKDIV=1: sent 0x96, got 0x{rx:02x} "
        f"(CPOL={cpol} CPHA={cpha} LSB={lsb}) -- one-bit receive skew"
    )
    # every CLKDIV >= 2 mode receives correctly
    for div in (2, 3, 24):
        assert await _first_bad_mode(dut, bm, 0xA5, div) is None, div


@cocotb.test()
async def test_spi_loopback_all_modes(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    for cpol in (0, 1):
        for cpha in (0, 1):
            for lsb in (False, True):
                for tx in (0xA5, 0x3C, 0x81, 0x00):
                    rx, status, done = await spi_loopback_transfer(
                        dut, bm, tx, cpol, cpha, lsb, clk_div=2)
                    assert rx == tx, (
                        f"mode CPOL={cpol} CPHA={cpha} LSB={lsb}: "
                        f"sent 0x{tx:02x}, got 0x{rx:02x}"
                    )
                    assert done & 0x2, hex(done)
                    assert done & 0x1 == 0, hex(done)
    # CS stays asserted for the whole sequence
    assert (await _pins(dut))[2] == 0xE


@cocotb.test()
async def test_spi_sclk_timing(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    clk_div = 3
    clocks = 16 * (clk_div + 1) + 16
    for cpol in (0, 1):
        await bm.write(SPI_CTRL, CTRL_EN | (CTRL_CPOL if cpol else 0))
        await bm.write(SPI_CLKDIV, clk_div)
        await _idle(dut, 2)                       # SCLK settles to CPOL
        cap_task = cocotb.start_soon(sample_sclk(dut, clocks))
        await bm.write(SPI_TXDATA, 0xA5)
        cap = await cap_task
        await spi_wait_done(dut, bm)
        assert cap.levels[0] == cpol
        assert cap.levels[-1] == cpol
        changes = [i for i in range(1, len(cap.levels))
                   if cap.levels[i] != cap.levels[i - 1]]
        assert len(changes) == 16, changes
        gaps = [b - a for a, b in zip(changes, changes[1:])]
        assert all(g == clk_div + 1 for g in gaps), gaps
        rises = sum(1 for a, b in zip(cap.levels, cap.levels[1:])
                    if a == 0 and b == 1)
        assert rises == 8, rises
        # finish the transfer and drain DONE
        await bm.read(SPI_RXDATA)


@cocotb.test()
async def test_spi_busy_done_and_rxdata_read(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    await bm.write(SPI_CTRL, CTRL_EN)
    await bm.write(SPI_CLKDIV, 8)
    await bm.write(SPI_TXDATA, 0x5A)
    st = (await bm.read(SPI_STATUS)).data
    assert st & 0x1, hex(st)                      # BUSY
    assert st & 0x2 == 0, hex(st)
    st = await spi_wait_done(dut, bm)
    assert st & 0x2 and st & 0x1 == 0, hex(st)
    assert await _irq(dut) == 0                  # IRQ not enabled
    # DONE is sticky until RXDATA is read
    assert (await bm.read(SPI_STATUS)).data & 0x2
    assert await _pins(dut) == (0, 0, 0xE)
    rx = (await bm.read(SPI_RXDATA)).data
    assert rx & 0xFF == 0x5A
    st = (await bm.read(SPI_STATUS)).data
    assert st & 0x2 == 0, hex(st)
    # the received data register keeps its value
    assert (await bm.read(SPI_RXDATA)).data & 0xFF == 0x5A


@cocotb.test()
async def test_spi_irq(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    await bm.write(SPI_CTRL, CTRL_EN | CTRL_IRQ)
    await bm.write(SPI_CLKDIV, 2)
    await bm.write(SPI_TXDATA, 0x3C)
    await spi_wait_done(dut, bm)
    assert await _irq(dut) == 1
    await bm.read(SPI_RXDATA)                    # clears DONE
    assert await _irq(dut) == 0
    # without IRQ_EN, DONE does not interrupt
    await bm.write(SPI_CTRL, CTRL_EN)
    await bm.write(SPI_TXDATA, 0xC3)
    await spi_wait_done(dut, bm)
    assert await _irq(dut) == 0


@cocotb.test()
async def test_spi_txdata_ignored_when_disabled(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    await bm.write(SPI_CTRL, 0)                  # EN=0
    await bm.write(SPI_TXDATA, 0xA5)
    await _idle(dut, 100)
    assert (await bm.read(SPI_STATUS)).data == 0
    assert await _pins(dut) == (0, 0, 0xE)       # no clocks, CS held
    # enabling later must not start the ignored byte
    await bm.write(SPI_CTRL, CTRL_EN)
    await _idle(dut, 200)
    assert (await bm.read(SPI_STATUS)).data == 0
    assert await _pins(dut) == (0, 0, 0xE)


@cocotb.test()
async def test_spi_txdata_while_busy_ignored(dut):
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    await bm.write(SPI_CTRL, CTRL_EN)
    await bm.write(SPI_CLKDIV, 30)               # long transfer
    await bm.write(SPI_TXDATA, 0xA5)
    st = (await bm.read(SPI_STATUS)).data
    assert st & 0x1, hex(st)
    await bm.write(SPI_TXDATA, 0x5A)             # ignored while busy
    st = await spi_wait_done(dut, bm)
    assert st & 0x2
    assert (await bm.read(SPI_RXDATA)).data & 0xFF == 0xA5


@cocotb.test()
async def test_spi_clkdiv_byte_lanes_and_alias(dut):
    bm = await _setup(dut)
    await bm.write(SPI_CLKDIV, 0x0000_00FF, wstrb=0b0001)
    assert (await bm.read(SPI_CLKDIV)).data == 0xFF
    await bm.write(SPI_CLKDIV, 0xAB00_0000, wstrb=0b1000)
    assert (await bm.read(SPI_CLKDIV)).data == 0xAB00_00FF
    await bm.write(SPI_CLKDIV, 0x0000_1100, wstrb=0b0010)
    assert (await bm.read(SPI_CLKDIV)).data == 0xAB00_11FF
    await bm.write(SPI_CLKDIV, 0, wstrb=0)
    assert (await bm.read(SPI_CLKDIV)).data == 0xAB00_11FF
    # spi.v decodes addr[7:0]: +0x100 aliases CTRL, +0x114 aliases CS
    await bm.write(SPI_BASE + 0x100, CTRL_EN | CTRL_LSB)
    assert (await bm.read(SPI_CTRL)).data == (CTRL_EN | CTRL_LSB)
    await bm.write(SPI_BASE + 0x114, 0x5)
    assert (await bm.read(SPI_CS)).data == 0x5
    assert (await _pins(dut))[2] == 0xA
    await bm.write(SPI_CTRL, 0)
    await bm.write(SPI_CS, 0)


@cocotb.test()
async def test_spi_status_read_only(dut):
    bm = await _setup(dut)
    await bm.write(SPI_STATUS, 0xFFFF_FFFF)
    assert (await bm.read(SPI_STATUS)).data == 0
    await bm.write(SPI_TXDATA, 0x12)             # EN=0: no transfer
    assert (await bm.read(SPI_STATUS)).data == 0


@cocotb.test()
async def test_spi_randomized_loopback(dut):
    rng = make_rng("spi_loopback")
    bm = await _setup(dut)
    dut.spi_loopback.value = 1
    await bm.write(SPI_CS, 0x1)
    for _ in range(20):
        tx = rng.getrandbits(8)
        cpol = rng.getrandbits(1)
        cpha = rng.getrandbits(1)
        lsb = bool(rng.getrandbits(1))
        clk_div = rng.randrange(2, 8)
        rx, status, done = await spi_loopback_transfer(
            dut, bm, tx, cpol, cpha, lsb, clk_div)
        assert rx == tx, (
            f"tx=0x{tx:02x} cpol={cpol} cpha={cpha} lsb={lsb} "
            f"clk_div={clk_div} -> 0x{rx:02x}"
        )
        assert done & 0x2, hex(done)
