#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

mkdir -p "$ROOT/build"

MARCH="${MARCH:-rv32imafc_zicsr}"
"${RISCV_PREFIX}gcc" -march="$MARCH" -mabi=ilp32 -O2 \
    -nostdlib -nostartfiles -ffreestanding -Wl,--no-relax \
    -T "$ROOT/sw/demo/link.ld" \
    "$ROOT/sw/demo/start.S" "$ROOT/sw/demo/trap.S" "$ROOT/sw/demo/main.c" \
    -o "$ROOT/build/demo.elf"

"${RISCV_PREFIX}objcopy" -O binary "$ROOT/build/demo.elf" "$ROOT/build/demo.bin"

"$PYTHON" -I - "$ROOT/build/demo.bin" "$ROOT/build/demo.hex" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
if len(b) % 4:
    b += b'\0' * (4 - len(b) % 4)
with open(sys.argv[2], 'w') as f:
    for i in range(0, len(b), 4):
        f.write('%08x\n' % int.from_bytes(b[i:i+4], 'little'))
PY

iverilog -g2012 -o "$ROOT/build/tb_soc.vvp" \
    "$ROOT/sim/tb_soc.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/fp_unit.v" "$ROOT/rtl/bus.v" \
    "$ROOT/rtl/timer.v" "$ROOT/rtl/gpio.v" "$ROOT/rtl/uart.v" "$ROOT/rtl/spi.v"

vvp "$ROOT/build/tb_soc.vvp" "+ITCM_HEX=$ROOT/build/demo.hex" +MAX_CYCLES=500000
