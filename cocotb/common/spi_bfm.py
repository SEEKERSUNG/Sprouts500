"""SPI helpers: SCLK timing capture and loopback transfers.

The SPI master is register-driven; tests use ``BusMaster`` for the register
accesses.  These helpers only watch pins / poll STATUS.
"""
from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import ReadOnly, RisingEdge

SPI_BASE = 0x2000_3000
SPI_CTRL = SPI_BASE + 0x00
SPI_STATUS = SPI_BASE + 0x04
SPI_CLKDIV = SPI_BASE + 0x08
SPI_TXDATA = SPI_BASE + 0x0C
SPI_RXDATA = SPI_BASE + 0x10
SPI_CS = SPI_BASE + 0x14

CTRL_EN = 1 << 0
CTRL_CPOL = 1 << 1
CTRL_CPHA = 1 << 2
CTRL_IRQ = 1 << 3
CTRL_LSB = 1 << 4


@dataclass
class SclkCapture:
    levels: list[int]

    def runs(self) -> list[tuple[int, int]]:
        """(level, number of clocks) runs in the sampled SCLK trace."""
        if not self.levels:
            return []
        out = [(self.levels[0], 1)]
        for v in self.levels[1:]:
            if v == out[-1][0]:
                out[-1] = (v, out[-1][1] + 1)
            else:
                out.append((v, 1))
        return out


async def sample_sclk(dut, clocks: int) -> SclkCapture:
    """Sample ``spi_sclk`` after every rising edge for *clocks* cycles."""
    levels = []
    for _ in range(clocks):
        await RisingEdge(dut.clk)
        await ReadOnly()
        levels.append(int(dut.spi_sclk.value))
    return SclkCapture(levels)


async def spi_wait_done(dut, bm, timeout_cycles: int = 40000) -> int:
    """Poll STATUS until DONE; returns the STATUS value."""
    waited = 0
    while True:
        st = await bm.read(SPI_STATUS)
        if st.data & 0x2:
            return st.data
        waited += 1
        if waited * 4 > timeout_cycles:
            raise TimeoutError(f"SPI never finished; last STATUS=0x{st.data:08x}")
        await RisingEdge(dut.clk)


async def spi_loopback_transfer(dut, bm, tx_byte: int, cpol: int, cpha: int,
                                lsb_first: bool, clk_div: int,
                                timeout_cycles: int = 40000):
    """Run one loopback byte and return (rx_byte, status_during, status_done)."""
    ctrl = (CTRL_EN | ((1 if cpol else 0) << 1) | ((1 if cpha else 0) << 2)
            | (CTRL_LSB if lsb_first else 0))
    await bm.write(SPI_CTRL, ctrl)
    await bm.write(SPI_CLKDIV, clk_div)
    await bm.write(SPI_TXDATA, tx_byte)
    busy = await bm.read(SPI_STATUS)
    done = await spi_wait_done(dut, bm, timeout_cycles)
    rx = await bm.read(SPI_RXDATA)
    return rx.data & 0xFF, busy.data, done
