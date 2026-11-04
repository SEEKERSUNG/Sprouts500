"""pytest <-> cocotb 2.x glue for a whole test module.

In cocotb 2.x a pytest test decorated with ``@pytest.mark.cocotb_runner``
starts one simulator process.  That process runs every ``async def test_*`` of
the same Python module in sequence (all tests share one simulation, so each
test must reset the design itself; the helpers in ``clock_reset.py`` do).

The HDL build is shared by every module (``sim_build/tb_soc_bus``); the
Icarus runner only recompiles when a source file is newer.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from cocotb_tools._pytest.hdl import HDL

ROOT = Path(__file__).resolve().parents[2]

TOPLEVEL = "tb_soc_bus"

#: Files needed by the SoC testbench, in compile order (RTL first).
SOURCES = (
    "rtl/core.v",
    "rtl/fp_unit.v",
    "rtl/bus.v",
    "rtl/timer.v",
    "rtl/gpio.v",
    "rtl/uart.v",
    "rtl/spi.v",
    "rtl/i2c.v",
    "cocotb/soc/fake_slave.v",
    "cocotb/soc/tb_soc_bus.v",
)

BUILD_DIR = Path("sim_build") / TOPLEVEL


def soc_hdl(hdl: HDL) -> HDL:
    """Configure *hdl* for the SoC testbench and compile it if needed."""
    hdl.toplevel = TOPLEVEL
    # Icarus reads +timescale+<unit>/<precision>; include the magnitude.
    hdl.timescale = ("1ns", "1ps")
    hdl.sources = [ROOT / src for src in SOURCES]
    hdl.build_dir = BUILD_DIR
    hdl.build()
    return hdl


def run_tests(
    hdl: HDL,
    plusargs: Iterable[str] = (),
    env: dict[str, str] | None = None,
    test_filter: str | None = None,
) -> None:
    """Run the cocotb tests of the calling module in the simulator.

    cocotb 2.1's ``HDL.test`` has no ``test_filter`` argument; the filter is
    passed to the simulator process through ``COCOTB_TEST_FILTER``.
    """
    extra = dict(env or {})
    if test_filter:
        extra["COCOTB_TEST_FILTER"] = test_filter
    hdl.test(plusargs=list(plusargs), env=extra)
