"""UART peripheral tests (rtl/uart.v).

Covered:
* reset values (docs/registers.md section 8), including BAUD=867;
* TX disabled (TX_EN=0) ignores TXDATA writes;
* TX frame format: start bit, LSB-first data, stop bit, one bit every
  BAUD+1 clocks (default 434), TX_BUSY during the frame and TX_DONE only at
  the end of the stop bit;
* TX_DONE stays set until the next TXDATA write / TX disable;
* writing TXDATA while TX_BUSY is ignored;
* RX bit-banged at the default and a custom baud rate;
* RX framing error (stop bit low) sets RX_OVERRUN without RX_VALID;
* RX overrun (new byte while RX_VALID still set) and RXDATA read clearing
  RX_VALID/RX_OVERRUN;
* TX/RX interrupt generation;
* register byte lanes, read-only/write-only behaviour and address aliasing
  (uart.v decodes addr[7:0]);
* randomized TX->RX loopback string round-trip.

One cocotb simulation is shared by all tests in this module; each test starts
with ``reset_soc`` so register state is fresh.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc
from common.misc import make_rng
from common.runner import run_tests, soc_hdl
from common.uart_bfm import (
    DEFAULT_BAUD_DIV,
    decode_uart_tx,
    send_uart_bytes,
)

UART = 0x2000_2000
CTRL = UART + 0x00
STATUS = UART + 0x04
BAUD = UART + 0x08
TXDATA = UART + 0x0C
RXDATA = UART + 0x10

TX_EN, RX_EN, TX_IRQ, RX_IRQ = 1, 2, 4, 8
BUSY, VALID, DONE, OVERRUN = 1, 2, 4, 8


@pytest.mark.cocotb_runner
def test_uart(hdl):
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
    return int(dut.uart_irq_o.value)


async def _tx_line(dut):
    await RisingEdge(dut.clk)
    await ReadOnly()
    return int(dut.uart_tx.value)


async def _wait_not_busy(dut, bm, timeout: int = 4000) -> int:
    """Poll STATUS until TX_BUSY clears; returns the STATUS value."""
    for _ in range(timeout):
        st = (await bm.read(STATUS)).data
        if st & BUSY == 0:
            return st
    raise AssertionError("UART TX stayed busy")


async def _wait_status(dut, bm, mask: int, timeout: int = 4000) -> int:
    """Poll STATUS until (status & mask) == mask; returns the STATUS value."""
    for _ in range(timeout):
        st = (await bm.read(STATUS)).data
        if st & mask == mask:
            return st
    raise AssertionError(f"UART status never had mask 0x{mask:x} set")


async def _send_rx(dut, values, baud_div: int = DEFAULT_BAUD_DIV,
                   stop_level: int = 1) -> None:
    """Bit-bang bytes into uart_rx; current phase must be ReadWrite."""
    await RisingEdge(dut.clk)
    await send_uart_bytes(dut, values, baud_div=baud_div,
                          stop_level=stop_level)


@cocotb.test()
async def test_uart_reset_values(dut):
    bm = await _setup(dut)
    assert (await bm.read(CTRL)).data == 0
    assert (await bm.read(STATUS)).data == 0
    assert (await bm.read(BAUD)).data == 867
    assert (await bm.read(RXDATA)).data == 0
    assert (await bm.read(TXDATA)).data == 0      # write-only, reads as 0
    assert (await bm.read(UART + 0x1C)).data == 0
    assert (await bm.read(UART + 0x14)).data == 0
    assert await _tx_line(dut) == 1
    assert await _irq(dut) == 0


@cocotb.test()
async def test_uart_tx_disabled_ignores_writes(dut):
    bm = await _setup(dut)
    await bm.write(TXDATA, 0xA5)              # TX_EN=0
    await _idle(dut, 100)
    assert (await bm.read(STATUS)).data == 0
    assert await _tx_line(dut) == 1
    # enabling TX later must not resurrect the ignored byte
    await bm.write(CTRL, TX_EN)
    await _idle(dut, 200)
    st = (await bm.read(STATUS)).data
    assert st & (BUSY | DONE) == 0, hex(st)
    assert await _tx_line(dut) == 1


@cocotb.test()
async def test_uart_tx_frame_and_timing(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, TX_EN)
    # 0x00 is all-zero data and 0xFF all-one data: both still have a start and
    # (for 0xFF) a stop transition, so the frame is decodable.
    # bit periods are measured from txd transitions, so use bytes that
    # actually toggle; all-zero/all-one bytes still decode (start/stop).
    for byte in (0x55, 0xA5, 0xF0, 0x0F):
        frame_task = cocotb.start_soon(decode_uart_tx(dut))
        await bm.write(TXDATA, byte)
        # busy while the frame is running
        st = (await bm.read(STATUS)).data
        assert st & BUSY, f"byte 0x{byte:02x}: not busy ({st:#x})"
        assert st & DONE == 0, hex(st)
        frame = await frame_task
        assert frame.byte == byte, (hex(byte), hex(frame.byte))
        assert frame.transitions()[0] == 0
        assert frame.on_bit_grid(), (hex(byte), frame.grid_offsets())
        assert frame.start_level == 0 and frame.stop_level == 1
        if byte & 1:
            # data bit 0 is 1, so the start-bit length is directly visible
            assert frame.transitions()[1] == DEFAULT_BAUD_DIV
        st = await _wait_status(dut, bm, DONE)
        assert st & BUSY == 0, hex(st)
        assert st & DONE, hex(st)
        # TX_DONE is sticky until the next TXDATA write or TX disable
        assert (await bm.read(STATUS)).data & DONE
        assert await _tx_line(dut) == 1


@cocotb.test()
async def test_uart_tx_done_cleared_by_disable_and_restart(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, TX_EN)
    frame_task = cocotb.start_soon(decode_uart_tx(dut))
    await bm.write(TXDATA, 0x3C)
    await frame_task
    await _wait_status(dut, bm, DONE)
    await bm.write(CTRL, 0)
    st = (await bm.read(STATUS)).data
    assert st & DONE == 0
    assert await _tx_line(dut) == 1
    # a new frame clears DONE and raises BUSY again
    await bm.write(CTRL, TX_EN)
    frame_task = cocotb.start_soon(decode_uart_tx(dut))
    await bm.write(TXDATA, 0xC3)
    st = (await bm.read(STATUS)).data
    assert st & BUSY and not (st & DONE), hex(st)
    frame = await frame_task
    assert frame.byte == 0xC3


@cocotb.test()
async def test_uart_tx_write_while_busy_ignored(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, TX_EN)
    frame_task = cocotb.start_soon(decode_uart_tx(dut))
    await bm.write(TXDATA, 0xAA)
    st = (await bm.read(STATUS)).data
    assert st & BUSY
    await bm.write(TXDATA, 0x55)              # must be ignored while busy
    await bm.write(TXDATA, 0x7E)
    frame = await frame_task
    assert frame.byte == 0xAA
    # no second frame starts afterwards
    await _idle(dut, 10 * (DEFAULT_BAUD_DIV + 1))
    st = (await bm.read(STATUS)).data
    assert st & BUSY == 0
    assert await _tx_line(dut) == 1


@cocotb.test()
async def test_uart_tx_custom_baud(dut):
    bm = await _setup(dut)
    baud_div = 9                              # 10 clocks per bit
    await bm.write(BAUD, baud_div)
    await bm.write(CTRL, TX_EN)
    frame_task = cocotb.start_soon(decode_uart_tx(dut, baud_div=baud_div))
    await bm.write(TXDATA, 0x96)
    frame = await frame_task
    assert frame.byte == 0x96
    assert frame.start_clocks == baud_div
    assert frame.bit_clocks == baud_div + 1
    assert frame.on_bit_grid(), frame.grid_offsets()
    await _wait_status(dut, bm, DONE)


@cocotb.test()
async def test_uart_rx_byte(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, RX_EN)
    await _send_rx(dut, [0x5A])
    st = await _wait_status(dut, bm, VALID)
    assert st & OVERRUN == 0, hex(st)
    rx = (await bm.read(RXDATA)).data
    assert rx == 0x5A
    st = (await bm.read(STATUS)).data
    assert st & (VALID | OVERRUN) == 0, hex(st)


@cocotb.test()
async def test_uart_rx_custom_baud_and_all_bytes(dut):
    bm = await _setup(dut)
    baud_div = 16
    await bm.write(BAUD, baud_div)
    await bm.write(CTRL, RX_EN)
    for byte in (0x00, 0xFF, 0x81, 0x7E):
        await _send_rx(dut, [byte], baud_div=baud_div)
        await _wait_status(dut, bm, VALID)
        assert (await bm.read(RXDATA)).data == byte


@cocotb.test()
async def test_uart_rx_framing_error(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, RX_EN)
    await _send_rx(dut, [0x42], stop_level=0)
    st = await _wait_status(dut, bm, OVERRUN)
    assert st & VALID == 0, hex(st)
    # framing error: RTL stores nothing (rx_data only commits on a stop bit
    # of 1, uart.v ST_STOP), and the read clears RX_OVERRUN.
    rx = (await bm.read(RXDATA)).data
    assert rx == 0
    st = (await bm.read(STATUS)).data
    assert st & (VALID | OVERRUN) == 0, hex(st)
    # receiver recovers and accepts a good frame
    await _send_rx(dut, [0xA7])
    await _wait_status(dut, bm, VALID)
    assert (await bm.read(RXDATA)).data == 0xA7


@cocotb.test()
async def test_uart_rx_overrun(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, RX_EN)
    await _send_rx(dut, [0x11])
    await _wait_status(dut, bm, VALID)
    # second byte arrives while RX_VALID is still set
    await _send_rx(dut, [0x22])
    st = await _wait_status(dut, bm, OVERRUN)
    assert st & VALID, hex(st)           # new byte replaces the old one
    assert (await bm.read(RXDATA)).data == 0x22
    st = (await bm.read(STATUS)).data
    assert st & (VALID | OVERRUN) == 0, hex(st)


@cocotb.test()
async def test_uart_tx_irq(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, TX_EN | TX_IRQ)
    assert await _irq(dut) == 0
    frame_task = cocotb.start_soon(decode_uart_tx(dut))
    await bm.write(TXDATA, 0x66)
    await frame_task
    await _wait_status(dut, bm, DONE)
    assert await _irq(dut) == 1
    await bm.write(CTRL, 0)
    assert await _irq(dut) == 0
    # without TX_IRQ the DONE flag alone does not interrupt
    await bm.write(CTRL, TX_EN)
    frame_task = cocotb.start_soon(decode_uart_tx(dut))
    await bm.write(TXDATA, 0x99)
    await frame_task
    await _wait_status(dut, bm, DONE)
    assert await _irq(dut) == 0


@cocotb.test()
async def test_uart_rx_irq(dut):
    bm = await _setup(dut)
    await bm.write(CTRL, RX_EN | RX_IRQ)
    assert await _irq(dut) == 0
    await _send_rx(dut, [0x81])
    await _wait_status(dut, bm, VALID)
    assert await _irq(dut) == 1
    assert (await bm.read(RXDATA)).data == 0x81
    assert await _irq(dut) == 0
    # without RX_IRQ a received byte does not interrupt
    await bm.write(CTRL, RX_EN)
    await _send_rx(dut, [0x18])
    await _wait_status(dut, bm, VALID)
    assert await _irq(dut) == 0


@cocotb.test()
async def test_uart_baud_byte_lanes_and_alias(dut):
    bm = await _setup(dut)
    await bm.write(BAUD, 0x0000_00FF, wstrb=0b0001)
    assert (await bm.read(BAUD)).data == 0x3FF
    await bm.write(BAUD, 0xAB00_0000, wstrb=0b1000)
    assert (await bm.read(BAUD)).data == 0xAB00_03FF
    await bm.write(BAUD, 0x0000_1100, wstrb=0b0010)
    assert (await bm.read(BAUD)).data == 0xAB00_11FF
    # no byte strobe: write ignored
    await bm.write(BAUD, 0, wstrb=0)
    assert (await bm.read(BAUD)).data == 0xAB00_11FF
    # uart.v decodes addr[7:0], so +0x100 aliases CTRL
    await bm.write(UART + 0x100, TX_EN)
    assert (await bm.read(CTRL)).data & TX_EN
    await bm.write(CTRL, 0)


@cocotb.test()
async def test_uart_status_read_only(dut):
    bm = await _setup(dut)
    await bm.write(STATUS, 0xFFFF_FFFF)
    assert (await bm.read(STATUS)).data == 0
    # TXDATA writes never affect STATUS without TX_EN
    await bm.write(TXDATA, 0x12)
    assert (await bm.read(STATUS)).data == 0


@cocotb.test()
async def test_uart_randomized_loopback(dut):
    rng = make_rng("uart_loopback")
    bm = await _setup(dut)
    # TB wires uart_rx = uart_tx, so this is a full TX->RX round trip.
    dut.uart_tx_d.value = 1
    dut.uart_loopback.value = 1
    await bm.write(CTRL, TX_EN | RX_EN)
    values = [rng.getrandbits(8) for _ in range(8)]
    for idx, byte in enumerate(values):
        await bm.write(TXDATA, byte)
        try:
            await _wait_status(dut, bm, VALID)
        except AssertionError:
            raise AssertionError(
                f"byte {idx}=0x{byte:02x}: no RX_VALID; "
                f"last STATUS=0x{(await bm.read(STATUS)).data:08x}, "
                f"tx={await _tx_line(dut)}, irq={await _irq(dut)}"
            )
        rx = (await bm.read(RXDATA)).data
        assert rx == byte, f"sent 0x{byte:02x}, received 0x{rx:02x}"
        # software must wait for TX_BUSY to clear before the next byte
        await _wait_not_busy(dut, bm)
    assert (await bm.read(STATUS)).data & (VALID | OVERRUN | BUSY) == 0
