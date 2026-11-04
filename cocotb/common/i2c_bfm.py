"""I2C master helpers and a small, clocked 7-bit slave/EEPROM model.

The model watches the real open-drain pads on every SoC clock. It acknowledges
its address, accepts a memory pointer and write data, and returns sequential
bytes for reads. Tests can disable ACKs or request a one-shot SCL stretch.
"""
from __future__ import annotations

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

I2C_BASE = 0x2000_4000
I2C_CTRL = I2C_BASE + 0x00
I2C_STATUS = I2C_BASE + 0x04
I2C_CLKDIV = I2C_BASE + 0x08
I2C_TXDATA = I2C_BASE + 0x0C
I2C_RXDATA = I2C_BASE + 0x10
I2C_CMD = I2C_BASE + 0x14

CTRL_EN = 1 << 0
CTRL_IRQ = 1 << 1

STATUS_BUSY = 1 << 0
STATUS_DONE = 1 << 1
STATUS_NACK = 1 << 2
STATUS_ACTIVE = 1 << 3
STATUS_SCL = 1 << 4
STATUS_SDA = 1 << 5

CMD_START = 1 << 0
CMD_STOP = 1 << 1
CMD_READ = 1 << 2
CMD_WRITE = 1 << 3
CMD_NACK = 1 << 4


class I2CSlave:
    """Byte-addressed 256-byte slave, sampled in the system-clock domain."""

    def __init__(self, dut, address: int = 0x50) -> None:
        self.dut = dut
        self.address = address
        self.ack_address = True
        self.ack_data = True
        self.mem = bytearray(256)
        self.ptr = 0
        self.received: list[int] = []
        self.transmitted: list[int] = []
        self.starts = 0
        self.stops = 0
        self.scl_rises = 0
        self.scl_falls = 0
        self._stretch_request = 0
        self._stretch_active = 0
        self._sda_low = False
        self._scl_low = False
        self.task = cocotb.start_soon(self._run())

    def stretch_next(self, cycles: int) -> None:
        """Hold SCL low for *cycles* after the next observed falling edge."""
        self._stretch_request = cycles

    async def _run(self) -> None:
        active = False
        addressed = False
        sending = False
        wait_stop = False
        expect_pointer = False
        read_after_ack = False
        ack_pending = False
        bit_count = 0
        rx = 0
        tx = 0
        prev_scl = 1
        prev_sda = 1

        while True:
            await RisingEdge(self.dut.clk)
            # Drive in the write phase using the response computed from the
            # preceding sampled edge, then sample the DUT's latest pad state.
            self.dut.i2c_sda_ext_en.value = 1 if self._sda_low else 0
            self.dut.i2c_scl_ext_en.value = 1 if self._scl_low else 0
            await ReadOnly()
            scl = int(self.dut.i2c_scl.value)
            sda = int(self.dut.i2c_sda.value)
            start = bool(prev_sda and not sda and scl)
            stop = bool(not prev_sda and sda and scl)
            rise = bool(not prev_scl and scl)
            fall = bool(prev_scl and not scl)
            prev_scl, prev_sda = scl, sda
            if rise:
                self.scl_rises += 1
            if fall:
                self.scl_falls += 1

            if start:
                active = True
                addressed = False
                sending = False
                wait_stop = False
                expect_pointer = False
                read_after_ack = False
                ack_pending = False
                bit_count = 0
                rx = 0
                self.starts += 1
            elif stop:
                active = False
                addressed = False
                sending = False
                wait_stop = False
                expect_pointer = False
                read_after_ack = False
                ack_pending = False
                bit_count = 0
                self.stops += 1
            elif active and rise:
                if not sending:
                    if bit_count < 8:
                        rx = ((rx << 1) | sda) & 0xFF
                        bit_count += 1
                        if bit_count == 8:
                            self.received.append(rx)
                            if not addressed:
                                if ((rx >> 1) == self.address and
                                        self.ack_address):
                                    addressed = True
                                    ack_pending = True
                                    if rx & 1:
                                        read_after_ack = True
                                    else:
                                        expect_pointer = True
                                else:
                                    wait_stop = True
                            elif self.ack_data:
                                if expect_pointer:
                                    self.ptr = rx
                                    expect_pointer = False
                                else:
                                    self.mem[self.ptr] = rx
                                    self.ptr = (self.ptr + 1) & 0xFF
                                ack_pending = True
                            else:
                                wait_stop = True
                    elif bit_count == 8:
                        ack_pending = False
                        if read_after_ack:
                            sending = True
                            read_after_ack = False
                            tx = self.mem[self.ptr]
                            self.transmitted.append(tx)
                            bit_count = 0
                        else:
                            bit_count = 9 if wait_stop else 0
                            rx = 0
                else:
                    if bit_count < 8:
                        bit_count += 1
                    elif bit_count == 8:
                        if not sda:  # master ACK: sequential next byte
                            self.ptr = (self.ptr + 1) & 0xFF
                            tx = self.mem[self.ptr]
                            self.transmitted.append(tx)
                            bit_count = 0
                        else:
                            sending = False
                            wait_stop = True
                            bit_count = 9

            # Change the slave's SDA driver only while SCL is low. Updating
            # it after a rising edge would create a false repeated START.
            if start or stop:
                self._sda_low = False
            elif active and fall:
                if sending:
                    if bit_count < 8:
                        self._sda_low = ((tx >> (7 - bit_count)) & 1) == 0
                    else:
                        self._sda_low = False  # master ACK/NACK bit
                elif bit_count == 8 and ack_pending:
                    self._sda_low = True
                else:
                    self._sda_low = False

            if fall and self._stretch_request:
                self._stretch_active = self._stretch_request
                self._stretch_request = 0
            self._scl_low = self._stretch_active > 0
            if self._stretch_active:
                self._stretch_active -= 1


async def i2c_command(dut, bm, data: int, command: int,
                      timeout_cycles: int = 40000) -> int:
    """Issue one byte command, poll DONE and return the final STATUS."""
    await bm.write(I2C_TXDATA, data)
    await bm.write(I2C_CMD, command)
    waited = 0
    while True:
        status = (await bm.read(I2C_STATUS)).data
        if status & STATUS_DONE:
            return status
        if waited > timeout_cycles:
            raise TimeoutError(
                f"I2C command 0x{command:02x} timed out; STATUS=0x{status:08x}")
        waited += 4


async def i2c_stop(dut, bm) -> int:
    return await i2c_command(dut, bm, 0, CMD_STOP)


async def i2c_write(dut, bm, address: int, pointer: int,
                    payload: bytes, *, div: int = 3) -> list[int]:
    """Write pointer + payload and return the STATUS of every byte."""
    await bm.write(I2C_CLKDIV, div)
    await bm.write(I2C_CTRL, CTRL_EN)
    values = [await i2c_command(dut, bm, (address << 1) | 0,
                                CMD_START | CMD_WRITE)]
    values.append(await i2c_command(dut, bm, pointer, CMD_WRITE))
    for index, value in enumerate(payload):
        cmd = CMD_WRITE | (CMD_STOP if index == len(payload) - 1 else 0)
        values.append(await i2c_command(dut, bm, value, cmd))
    return values


async def i2c_read(dut, bm, address: int, pointer: int, length: int,
                   *, div: int = 3) -> tuple[bytes, list[int]]:
    """Set pointer, use repeated START, then read *length* bytes."""
    await bm.write(I2C_CLKDIV, div)
    await bm.write(I2C_CTRL, CTRL_EN)
    values = [await i2c_command(dut, bm, (address << 1) | 0,
                                CMD_START | CMD_WRITE)]
    values.append(await i2c_command(dut, bm, pointer, CMD_WRITE))
    values.append(await i2c_command(dut, bm, (address << 1) | 1,
                                    CMD_START | CMD_WRITE))
    data = bytearray()
    for index in range(length):
        cmd = CMD_READ
        if index == length - 1:
            cmd |= CMD_NACK | CMD_STOP
        values.append(await i2c_command(dut, bm, 0, cmd))
        data.append((await bm.read(I2C_RXDATA)).data & 0xFF)
    return bytes(data), values
