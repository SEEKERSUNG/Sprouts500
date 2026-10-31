#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"
mkdir -p "$ROOT/build/syn"

LIB="$ROOT/lib/NangateOpenCellLibrary_typical.lib"
if [ ! -f "$LIB" ]; then
    echo "missing $LIB"
    echo "download it with: bash scripts/fetch_lib.sh"
    exit 1
fi

# The TCM arrays are shrunk to 2 words for gate-level CPU timing.  They are
# inferred register arrays, not compiled SRAM macros; a real memory compiler +
# Liberty RAM models are required for full TCM signoff.  The CPU logic, divider,
# bus, CSR and control paths are unchanged.
yosys -p "
    read_verilog $ROOT/rtl/core.v;
    chparam -set ITCM_WORDS 2 -set DTCM_WORDS 2 core;
    synth -top core -flatten;
    dfflibmap -liberty $LIB;
    abc -fast -liberty $LIB;
    opt_clean;
    write_verilog -noattr -noexpr $ROOT/build/syn/core_netlist.v;
    stat
" 2>&1 | tee "$ROOT/build/syn/yosys.log"

# Yosys/ABC does not resize high-fanout nets.  Promote drivers of flat register
# file muxes / pipeline enables / inferred memory enables to stronger cells.
# This is a first-order pre-CTS fix; a real resizer + CTS should replace it.
"$PYTHON" "$ROOT/scripts/upsize_fanout.py" "$ROOT/build/syn/core_netlist.v" 40
