"""UART bit-level helpers: decode the DUT's TX line and bit-bang RX.

The UART is 8N1.  ``baud_div`` is the value in the BAUD register; the nominal
bit length is ``baud_div + 1`` clocks (434 with the reset value 433).

``decode_uart_tx`` samples ``uart_tx`` on every rising edge around a frame, so
tests can both decode the byte and check that every level change happens on
the expected bit grid.  Start it *before* writing TXDATA:

    frame_task = cocotb.start_soon(decode_uart_tx(dut, baud_div=433))
    await bm.write(UART_TXDATA, 0x55)
    frame = await frame_task
    assert frame.byte == 0x55
    assert frame.on_bit_grid()

Measured rtl/uart.v timing (50 MHz, BAUD=433): the start bit lasts
``baud_div`` clocks (one clock short, because the counter starts at zero on
the edge that leaves ST_IDLE), each data bit lasts ``baud_div + 1`` clocks,
and the stop bit starts on the nominal grid; TX_DONE follows one nominal bit
period later.  ``on_bit_grid`` checks transitions against that grid.
"""
from __future__ import annotations

from dataclasses import dataclass

from cocotb.triggers import ReadOnly, RisingEdge

DEFAULT_BAUD_DIV = 433


@dataclass
class UartFrame:
    byte: int
    """Decoded data byte (LSB first)."""
    levels: list[int]
    """txd sampled on every rising edge; index 0 is the start-bit edge."""
    start_clocks: int
    """Length of the start bit in clocks (``baud_div``)."""
    bit_clocks: int
    """Nominal bit length in clocks (``baud_div + 1``)."""
    start_level: int = 0

    def transitions(self) -> list[int]:
        """Indices in ``levels`` where txd changes level.

        Index 0 is always included: it is the falling start edge relative to
        the idle-high line.
        """
        return [0] + [i for i in range(1, len(self.levels))
                      if self.levels[i] != self.levels[i - 1]]

    @property
    def stop_level(self) -> int:
        return self.levels[-1]

    def grid_offsets(self) -> list[int]:
        """Transitions after the start edge, relative to the first boundary."""
        return [t - self.start_clocks for t in self.transitions()[1:]]

    def on_bit_grid(self) -> bool:
        """True if every txd transition is a multiple of the bit length.

        The start edge itself is at index 0; the start bit ends at
        ``start_clocks`` and every later transition must sit at
        ``start_clocks + k * bit_clocks``.
        """
        tr = self.transitions()
        if not tr or tr[0] != 0:
            return False
        offsets = self.grid_offsets()
        return all(off % self.bit_clocks == 0 for off in offsets)


async def decode_uart_tx(dut, baud_div: int = DEFAULT_BAUD_DIV,
                         max_wait: int = 200_000) -> UartFrame:
    """Decode one 8N1 frame on ``uart_tx`` (start coroutine before TXDATA)."""
    bit = baud_div + 1
    clk = dut.clk
    levels: list[int] = []
    waited = 0
    while True:
        await RisingEdge(clk)
        await ReadOnly()
        cur = int(dut.uart_tx.value)
        levels.append(cur)
        if cur == 0:
            break
        waited += 1
        if waited > max_wait:
            raise TimeoutError("uart_tx never started (no falling edge)")
    # Drop the leading idle-high samples: index 0 must be the start edge.
    levels = [levels[-1]]
    # The start edge has been sampled; sample ten nominal bit periods more.
    for _ in range(10 * bit):
        await RisingEdge(clk)
        await ReadOnly()
        levels.append(int(dut.uart_tx.value))

    data = 0
    for i in range(8):
        idx = (i + 1) * bit + bit // 2
        if idx < len(levels) and levels[idx]:
            data |= 1 << i
    return UartFrame(data, levels, baud_div, bit)


async def _wait_edges(dut, n: int) -> None:
    for _ in range(n):
        await RisingEdge(dut.clk)


async def send_uart_byte(dut, value: int, baud_div: int = DEFAULT_BAUD_DIV,
                         stop_level: int = 1, trailing_high: int = 2) -> None:
    """Bit-bang one 8N1 frame onto ``uart_rx``.

    The line is held high for ``trailing_high`` clocks first (>= 2 so the RX
    synchronizer sees an idle high and then a clean falling edge).  Leaves the
    line idle high afterwards.
    """
    bit = baud_div + 1
    await _wait_edges(dut, max(trailing_high, 2))
    dut.uart_rx.value = 0                      # start
    await _wait_edges(dut, bit)
    for i in range(8):
        dut.uart_rx.value = (value >> i) & 1
        await _wait_edges(dut, bit)
    dut.uart_rx.value = 1 if stop_level else 0
    await _wait_edges(dut, bit)
    dut.uart_rx.value = 1
    if trailing_high:
        await _wait_edges(dut, trailing_high)


async def send_uart_bytes(dut, values, baud_div: int = DEFAULT_BAUD_DIV,
                          stop_level: int = 1, gap_clocks: int = 0) -> None:
    for v in values:
        await send_uart_byte(dut, v, baud_div, stop_level,
                             trailing_high=gap_clocks)
