"""pytest configuration for the Sprouts500 cocotb suites.

* The cocotb pytest plugin is loaded explicitly: the cocotb 2.x wheel does not
  register a ``pytest11`` entry point, so plain ``pytest`` would otherwise
  reject the ``cocotb_runner`` marker.
* ``cocotb/`` is put on ``sys.path`` so test modules can ``from common.xxx
  import ...``.  ``cocotb/`` must not contain ``__init__.py`` (it would
  shadow the installed ``cocotb`` package).
"""
from __future__ import annotations

import sys
from pathlib import Path

COCOTB_DIR = Path(__file__).resolve().parent
if str(COCOTB_DIR) not in sys.path:
    sys.path.insert(0, str(COCOTB_DIR))

pytest_plugins = ["cocotb_tools._pytest.plugin"]
