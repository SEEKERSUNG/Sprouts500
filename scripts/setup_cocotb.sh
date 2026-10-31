#!/usr/bin/env bash
# Create the local cocotb/pytest virtual environment.
#
# No sudo and nothing outside the repository: everything lives in .venv/.
# Re-running is safe: an existing environment with cocotb and pytest is left
# untouched.  If the distro Python was built without ensurepip, the venv is
# created with --without-pip and get-pip.py bootstraps pip inside it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found (install python3 and python3-venv)" >&2
    exit 1
fi
if ! command -v iverilog >/dev/null 2>&1; then
    echo "error: iverilog not found (run scripts/setup_ubuntu.sh)" >&2
    exit 1
fi

if [ -x "$VENV/bin/python" ]; then
    if "$VENV/bin/python" -c 'import cocotb, pytest, cocotb_tools' >/dev/null 2>&1; then
        echo "cocotb venv already present: $VENV (skipping install)"
        "$VENV/bin/python" -c 'import cocotb, pytest; print("cocotb", cocotb.__version__); print("pytest", pytest.__version__)'
        iverilog -V 2>/dev/null | head -n 1 || true
        exit 0
    fi
    echo "existing venv lacks cocotb/pytest; repairing $VENV"
else
    echo "creating venv: $VENV"
    if ! "$PYTHON" -m venv "$VENV" >/dev/null 2>&1; then
        echo "ensurepip unavailable; creating venv with --without-pip"
        "$PYTHON" -m venv --without-pip "$VENV"
    fi
fi

if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    echo "bootstrapping pip inside the venv"
    if "$VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1; then
        :
    else
        GET_PIP="$(mktemp "${TMPDIR:-/tmp}/get-pip.XXXXXX.py")"
        trap 'rm -f "$GET_PIP"' EXIT
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$GET_PIP"
        elif command -v wget >/dev/null 2>&1; then
            wget -qO "$GET_PIP" https://bootstrap.pypa.io/get-pip.py
        else
            echo "error: neither ensurepip nor curl/wget available" >&2
            exit 1
        fi
        "$VENV/bin/python" "$GET_PIP"
    fi
fi

echo "installing cocotb and pytest into $VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check cocotb pytest

echo
echo "environment ready (no system packages were modified):"
"$VENV/bin/python" -c 'import cocotb, pytest; print("cocotb", cocotb.__version__); print("pytest", pytest.__version__)'
iverilog -V 2>/dev/null | head -n 1 || true
