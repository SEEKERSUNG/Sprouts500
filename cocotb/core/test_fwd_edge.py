"""Directed edge cases for the TCM store->load merge and load forwarding.

These target the EX-phase read/merge added with the MEM load-forwarding
optimization: an older store in MEM must contribute its byte lanes to the
load being read from EX, and a TCM load must forward from MEM after an AMO.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    CoreHarness, DTCM_BASE, HALT_ADDR, HALT_MAGIC, Asm, enc_amo, expect,
    tb_case,
)
from _runner import run_test  # noqa: E402

AMO_ADD = 0b00000


def _tail(a):
    a.align()
    halt = a.here()
    a.li(31, HALT_ADDR)
    a.li(30, HALT_MAGIC)
    a.sw(30, 31, 0)
    a.self_loop()
    return a.assemble(), halt


def build_merge():
    a = Asm()
    a.li(1, DTCM_BASE)
    a.li(2, 0x1122_3344)
    a.sw(2, 1, 0)

    # word store immediately followed by a same-word load
    a.li(3, 0xAABB_CCDD)
    a.sw(3, 1, 0)
    a.lw(4, 1, 0)
    a.sw(4, 1, 0x10)

    # byte store then word load: only lane 1 changes
    a.li(5, 0x55)
    a.sb(5, 1, 1)
    a.lw(6, 1, 0)
    a.sw(6, 1, 0x14)

    # half store then word load: lanes 2/3 change
    a.li(7, 0x1234)
    a.sh(7, 1, 2)
    a.lw(8, 1, 0)
    a.sw(8, 1, 0x18)

    # AMO writes the word; the immediately younger load must see the new value
    a.li(9, 1)
    a.raw(enc_amo(AMO_ADD, 10, 1, 9))
    a.lw(11, 1, 0)
    a.sw(11, 1, 0x1c)

    # store, one intervening instruction, load: the store is in WB when the
    # load is in EX.  The EX-phase read must still see the store's word.
    a.li(12, 0xDEAD_BEEF)
    a.sw(12, 1, 0x20)
    a.nop()
    a.lw(13, 1, 0x20)
    a.sw(13, 1, 0x24)
    words, halt = _tail(a)
    return words, halt


async def _run(dut):
    words, halt = build_merge()
    h = CoreHarness(dut)
    h.load_program(words, zero_rest=True)
    await Timer(1, unit="ns")
    await h.reset()
    stats = {"raw_stall": 0, "trap_taken": 0}
    await h.run_with_trace(halt, max_cycles=3000, stats=stats)
    return h, stats


@cocotb.test()
async def fwd_edge(dut):
    h, stats = await _run(dut)
    expect(h.dtcm(0x10 // 4) == 0xAABB_CCDD,
           f"word store->load 0x{h.dtcm(0x10 // 4):08x}")
    expect(h.dtcm(0x14 // 4) == 0xAABB_55DD,
           f"byte merge 0x{h.dtcm(0x14 // 4):08x}")
    expect(h.dtcm(0x18 // 4) == 0x1234_55DD,
           f"half merge 0x{h.dtcm(0x18 // 4):08x}")
    expect(h.dtcm(0x1c // 4) == 0x1234_55DE,
           f"AMO->load 0x{h.dtcm(0x1c // 4):08x}")
    expect(h.dtcm(0x24 // 4) == 0xDEAD_BEEF,
           f"store distance-2 load 0x{h.dtcm(0x24 // 4):08x}")
    expect(stats["trap_taken"] == 0, "forwarding edge case trapped")


@pytest.mark.parametrize("case", ("merge",))
def test_fwd_edge(case):
    run_test("test_fwd_edge", seed=0, case=case)
