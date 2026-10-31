#!/usr/bin/env bash
# Install the Ubuntu host toolchain used by Sprouts500.
# Usage: bash scripts/setup_ubuntu.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script targets Ubuntu/Linux." >&2
    exit 1
fi

sudo apt-get update

# universe is enabled by default on current Ubuntu releases; only add it if the
# configured sources do not mention it.
if ! (grep -qs "universe" /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || \
      grep -qs "universe" /etc/apt/sources.list 2>/dev/null); then
    sudo apt-get install -y software-properties-common
    sudo add-apt-repository -y universe
fi

sudo apt-get install -y \
    build-essential cmake ninja-build git wget curl python3 python3-venv python3-dev \
    iverilog yosys gtkwave \
    tcl-dev swig bison flex automake autotools-dev \
    libgtest-dev libeigen3-dev libfmt-dev \
    gcc-riscv64-unknown-elf

# optional; OpenSTA is built with USE_TCL_READLINE=OFF
sudo apt-get install -y tcl-tclreadline || true

echo
echo "== versions =="
"${RISCV_PREFIX}gcc" --version | head -1
iverilog -V 2>&1 | head -1
yosys -V
"$PYTHON" --version

echo
echo "Next:"
echo "  python3 scripts/fetch_isa.py"
echo "  python3 scripts/run_isa.py"
echo "  bash scripts/build_demo.sh"
echo "  bash scripts/fetch_lib.sh && bash scripts/synth.sh"
echo "  sudo bash scripts/setup_opensta.sh && bash scripts/sta.sh"
echo "  bash scripts/setup_cocotb.sh && bash scripts/run_cocotb.sh"
echo "  bash scripts/setup_riscv_dv.sh && bash scripts/run_riscv_dv.sh"
