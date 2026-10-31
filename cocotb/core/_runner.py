"""pytest/cocotb runner glue for the Sprouts500 core suite.

Builds ``rtl/core.v`` + ``tb_core_cocotb.v`` once with Icarus and runs one
cocotb test module per pytest case.  Seeds are controlled by
``COCOTB_SEEDS`` (default 3).
"""

from __future__ import annotations

import os
import pathlib
import xml.etree.ElementTree as ET

from cocotb_tools.runner import get_runner

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
RTL_CORE = REPO / "rtl" / "core.v"
TB = HERE / "tb_core_cocotb.v"
BUILD_ROOT = REPO / "build" / "cocotb_core"
BUILD_DIR = BUILD_ROOT / "sim_build"
RESULTS_DIR = BUILD_ROOT / "results"

TOPLEVEL = "tb_core_cocotb"

# Simulations listed here are known RTL differences documented in
# ``docs/verification_cocotb_core.md`` (section 发现的问题).  cocotb's own pass/fail
# is preserved, so the case still fails loudly if the RTL is fixed; the pytest
# wrapper only stops propagating the simulator-level failure.
_PYTEST_PLUGIN_ENV = (
    # Variables the cocotb pytest plugin injects into runner processes.  The
    # venv's cocotb 2.1 wheel has no ``pytest11`` entry point, so a simulation
    # launched with these set enters the pytest regression manager without the
    # collection plugin and silently collects zero tests.  Removing them makes
    # the simulation use cocotb's classic regression manager, which is what the
    # suite's plain ``@cocotb.test()`` modules are written for.
    "COCOTB_PYTEST_NODEID", "COCOTB_PYTEST_REPORTER_ADDRESS",
    "COCOTB_PYTEST_ARGS", "COCOTB_PYTEST_DIR", "COCOTB_PYTEST_KEYWORDS",
    "PYGPI_USERS",
)

SIM_XFAIL = {}


class _SimXfail(Exception):
    """Raised for a known-difference case whose simulation failed as expected."""


def _sim_failed(results_xml: pathlib.Path) -> str | None:
    """Return the failure message of a cocotb results file, if any."""
    try:
        root = ET.parse(results_xml).getroot()
    except (OSError, ET.ParseError):
        return None
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                return node.get("message") or tag
    return None


def get_seeds(default: int = 3) -> int:
    raw = os.environ.get("COCOTB_SEEDS", str(default))
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def build(force: bool = False):
    runner = get_runner("icarus")
    runner.build(
        sources=[TB, RTL_CORE],
        hdl_toplevel=TOPLEVEL,
        build_dir=BUILD_DIR,
        timescale=("1ns", "1ps"),
        always=force,
    )
    return runner


def run_test(module: str, seed: int = 0, case: str | None = None,
             extra_env: dict[str, str] | None = None):
    """Build (incrementally) and run one cocotb test module.

    ``TB_SEED`` is exported to the simulation; ``TB_CASE`` selects a scenario
    when the module contains more than one.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    env = {"TB_SEED": str(seed)}
    if case is not None:
        env["TB_CASE"] = case
    if extra_env:
        env.update(extra_env)
    tag = module if case is None else f"{module}_{case}"
    results = RESULTS_DIR / f"{tag}_seed{seed}.xml"
    # Build and run with the cocotb pytest plugin's live-session variables
    # removed: cocotb then reports as a plain script and the return code (and
    # results XML) are a single deterministic pass/fail that the pytest
    # wrapper below can translate into SIM_XFAIL.
    # Keep the sim from collecting the test module through the cocotb pytest
    # plugin (PYTHONPATH always contains the pytest invocation dir).
    saved = {k: os.environ.pop(k, None) for k in _PYTEST_PLUGIN_ENV}
    try:
        runner = build()
        try:
            runner.test(
                test_module=module,
                hdl_toplevel=TOPLEVEL,
                build_dir=BUILD_DIR,
                test_dir=HERE,
                seed=seed,
                extra_env=env,
                results_xml=str(results),
            )
        except SystemExit:
            known = SIM_XFAIL.get((module, case))
            message = _sim_failed(results)
            if known is not None and message is not None:
                raise _SimXfail(
                    f"known RTL difference ({tag}): {known} -- {message}")
            raise
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def run_case(module: str, seed: int = 0, case: str | None = None,
             extra_env: dict[str, str] | None = None):
    """Alias kept for readability in test files."""
    run_test(module, seed=seed, case=case, extra_env=extra_env)
