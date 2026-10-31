"""GPIO peripheral tests (rtl/gpio.v).

Covered:
* reset values;
* DIR/OUT/IN readback, pins driven through the inout pad;
* the external world may only drive pins whose DIR bit is 0;
* IN shows the two-flop synchronized value;
* edge interrupt: any toggle on an enabled pin sets IRQ_PEND; IRQ=1 only if
  some pending bit is set (gpio.v has no per-bit IE gating of IRQ);
* W1C with byte-lane strobes;
* byte-lane writes to DIR/OUT/IE;
* address aliasing (gpio decodes addr[7:0]) and IN being read-only;
* randomized traffic against a cycle-accurate Python model.
"""
from __future__ import annotations

import pytest

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from common.bus_master import BusMaster
from common.clock_reset import reset_soc
from common.misc import make_rng
from common.models import Scoreboard
from common.runner import run_tests, soc_hdl

GPIO = 0x2000_1000
DIR = GPIO + 0x00
OUT = GPIO + 0x04
IN = GPIO + 0x08
IE = GPIO + 0x0C
IPEND = GPIO + 0x10


@pytest.mark.cocotb_runner
def test_gpio(hdl):
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


async def _set_ext(dut, value: int, en: int = 1) -> None:
    """Drive the external pad value for the next clock edge."""
    await RisingEdge(dut.clk)
    dut.gpio_ext_val.value = value
    dut.gpio_ext_en.value = en


async def _irq(dut):
    await RisingEdge(dut.clk)
    await ReadOnly()
    return int(dut.gpio_irq_o.value)


def _pad(dut) -> int:
    v = dut.gpio_pin.value
    if not v.is_resolvable:
        return -1
    return int(v)


@cocotb.test()
async def test_gpio_reset_values(dut):
    bm = await _setup(dut)
    for reg in (DIR, OUT, IN, IE, IPEND):
        assert (await bm.read(reg)).data == 0, hex(reg)
    assert _pad(dut) == 0          # DIR=0 and no external drive
    assert await _irq(dut) == 0


@cocotb.test()
async def test_gpio_outputs_reach_pins(dut):
    bm = await _setup(dut)
    await bm.write(DIR, 0xFF)
    await bm.write(OUT, 0x5A)
    assert (await bm.read(DIR)).data == 0xFF
    assert (await bm.read(OUT)).data == 0x5A
    await _idle(dut, 1)
    assert _pad(dut) == 0x5A, hex(_pad(dut))
    # high pins are also what IN synchronizes to
    await _idle(dut, 3)
    assert (await bm.read(IN)).data == 0x5A


@cocotb.test()
async def test_gpio_inputs_only_on_input_pins(dut):
    bm = await _setup(dut)
    # external drive is ignored while DIR bit is 1
    await bm.write(DIR, 0x0F)
    await _set_ext(dut, 0xA5)
    await _idle(dut, 3)
    in_val = (await bm.read(IN)).data
    assert in_val & 0xF == 0, hex(in_val)         # driven low by OUT=0
    assert in_val >> 4 == 0xA, hex(in_val)        # external drive on inputs
    assert _pad(dut) == 0xA0, hex(_pad(dut))
    # switch them to inputs: external value now wins everywhere
    await bm.write(DIR, 0)
    await _idle(dut, 3)
    assert (await bm.read(IN)).data == 0xA5
    assert _pad(dut) == 0xA5


@cocotb.test()
async def test_gpio_synchronizer_latency(dut):
    bm = await _setup(dut)
    await _set_ext(dut, 1)
    # IN needs two flops after the pad settles: the first read (committed one
    # edge after the pad change) still sees 0, the next sees 1.
    r1 = await bm.read(IN)
    r2 = await bm.read(IN)
    assert (r1.data, r2.data) == (0, 1), (r1.data, r2.data)


@cocotb.test()
async def test_gpio_edge_interrupt(dut):
    bm = await _setup(dut)
    await bm.write(IE, 0x3)
    assert await _irq(dut) == 0
    await _set_ext(dut, 0x1)
    await _idle(dut, 4)
    assert (await bm.read(IPEND)).data & 0x3 == 0x1
    assert await _irq(dut) == 1
    # disabling IE does not clear an already pending bit
    await bm.write(IE, 0)
    assert await _irq(dut) == 1
    # a falling edge on another enabled pin also sets pending
    await bm.write(IE, 0x3)
    await _set_ext(dut, 0x3)          # pin 1 rises, pin 0 stays high
    await _idle(dut, 4)
    assert (await bm.read(IPEND)).data & 0x3 == 0x3
    # W1C clears and drops IRQ
    await bm.write(IPEND, 0x3)
    assert (await bm.read(IPEND)).data == 0
    assert await _irq(dut) == 0


@cocotb.test()
async def test_gpio_no_edge_no_pending(dut):
    bm = await _setup(dut)
    await bm.write(IE, 0xFFFF_FFFF)
    await _set_ext(dut, 0x1234)
    await _idle(dut, 4)
    await bm.write(IPEND, 0xFFFF_FFFF)
    # the level stays the same: no new edges, pending must stay clear
    await _idle(dut, 10)
    assert (await bm.read(IPEND)).data == 0
    assert await _irq(dut) == 0


@cocotb.test()
async def test_gpio_w1c_byte_lanes(dut):
    bm = await _setup(dut)
    await bm.write(IE, 0xFF)
    await _set_ext(dut, 0xFF)
    await _idle(dut, 4)
    assert (await bm.read(IPEND)).data == 0xFF
    await bm.write(IPEND, 0x0000_000F, wstrb=0b0001)   # clear pin 0..3
    assert (await bm.read(IPEND)).data == 0xF0
    await bm.write(IPEND, 0x0000_00FF)                 # clear 0..7
    assert (await bm.read(IPEND)).data == 0
    # W1C with a data byte whose lane is not strobed does nothing
    await _set_ext(dut, 0)
    await _idle(dut, 4)
    assert (await bm.read(IPEND)).data == 0xFF
    await bm.write(IPEND, 0xFF, wstrb=0)
    assert (await bm.read(IPEND)).data == 0xFF
    await bm.write(IPEND, 0x1F, wstrb=0b0001)          # clear pending 0..4
    assert (await bm.read(IPEND)).data == 0xE0


@cocotb.test()
async def test_gpio_byte_lane_writes(dut):
    bm = await _setup(dut)
    await bm.write(DIR, 0x00FF_00FF)
    await bm.write(DIR, 0xFF00_FF00, wstrb=0b1100)
    assert (await bm.read(DIR)).data == 0xFF00_00FF
    await bm.write(OUT, 0x1234_5678)
    await bm.write(OUT, 0xAA, wstrb=0b0001)
    assert (await bm.read(OUT)).data == 0x1234_56AA
    await bm.write(IE, 0xFFFFFFFF)
    await bm.write(IE, 0, wstrb=0b0010)
    assert (await bm.read(IE)).data == 0xFFFF_00FF


@cocotb.test()
async def test_gpio_in_is_read_only_and_alias(dut):
    bm = await _setup(dut)
    await bm.write(IN, 0xFFFF_FFFF)
    assert (await bm.read(IN)).data == 0
    # gpio.v decodes addr[7:0]: +0x100 hits DIR
    await bm.write(GPIO + 0x100, 0xABCD)
    assert (await bm.read(DIR)).data == 0xABCD
    await bm.write(DIR, 0)
    await bm.write(OUT, 0)
    await bm.write(IE, 0)


@cocotb.test()
async def test_gpio_randomized_scoreboard(dut):
    rng = make_rng("gpio_randomized")
    bm = await _setup(dut)
    sb = Scoreboard()
    dut.gpio_ext_val.value = 0
    dut.gpio_ext_en.value = 1

    async def wr(addr, data, wstrb=0xF):
        res = await bm.write(addr, data, wstrb)
        sb.write(res.commit_cycle, addr, data, wstrb)

    async def rd(addr):
        res = await bm.read(addr)
        sb.read(res.commit_cycle, addr, res.data)

    async def set_ext(value):
        await _set_ext(dut, value)
        sb.gpio.note_input(bm.cycles + 1, value)

    regs = [DIR, OUT, IN, IE, IPEND]
    for step in range(80):
        r = rng.random()
        if r < 0.25:
            await set_ext(rng.getrandbits(rng.choice([1, 4, 8, 32])))
        elif r < 0.5:
            reg = rng.choice([DIR, OUT, IE])
            await wr(rng.choice([reg, GPIO + 0x100 + (reg - GPIO)]),
                     rng.getrandbits(32))
        elif r < 0.6:
            await wr(IPEND, rng.getrandbits(32), rng.randrange(16))
        else:
            await rd(rng.choice(regs))
        for _ in range(rng.choice([0, 0, 1, 2, 3])):
            await RisingEdge(dut.clk)
            await ReadOnly()
        await RisingEdge(dut.clk)
    await rd(IPEND)
    sb.check()
