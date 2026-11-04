#!/usr/bin/env bash
# Build and run a user C program on the Sprouts500 SoC.
#
#   bash scripts/build_app.sh sw/example
#   bash scripts/build_app.sh sw/example +VCD
#   MAX_CYCLES=500000 bash scripts/build_app.sh sw/example
#
# The program directory must contain main.c and any extra .c/.S files.  The
# runtime (crt0.S, trap.S, soc.c, string.c, app.ld) comes from sw/lib; do not
# define _start or trap_entry in the program itself.  A strong trap_handler()
# overrides the weak default.
#
# Output: build/app/{app.elf,app.bin,app.hex,tb_app.vvp,run.log}
# Optional: OUT=..., APP_LD=... select another build/link layout; RUN_SIM=0
# compiles the image without running the normal address-0 application TB.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

if [ $# -lt 1 ] || [ ! -d "$1" ]; then
    echo "usage: bash scripts/build_app.sh <app-dir> [vvp plusargs...]" >&2
    echo "example: bash scripts/build_app.sh sw/example" >&2
    exit 2
fi

APP_DIR="$(cd "$1" && pwd)"
shift

LIB="$ROOT/sw/lib"
OUT="${OUT:-$ROOT/build/app}"
LINKER_SCRIPT="${APP_LD:-$LIB/app.ld}"
mkdir -p "$OUT"

SRCS=()
while IFS= read -r -d '' f; do
    SRCS+=("$f")
done < <(find "$APP_DIR" -type f \( -name '*.c' -o -name '*.S' \) -print0 | sort -z)

if [ ${#SRCS[@]} -eq 0 ]; then
    echo "no .c/.S sources found in $APP_DIR" >&2
    exit 2
fi

if grep -qs '^[[:space:]]*\.globl[[:space:]]\+_start' "${SRCS[@]}"; then
    echo "error: $_start is provided by sw/lib/crt0.S; do not define it in $APP_DIR" >&2
    exit 2
fi
if grep -qs '^[[:space:]]*\.globl[[:space:]]\+trap_entry' "${SRCS[@]}"; then
    echo "error: trap_entry is provided by sw/lib/trap.S; do not define it in $APP_DIR" >&2
    exit 2
fi

ELF="$OUT/app.elf"
BIN="$OUT/app.bin"
HEX="$OUT/app.hex"
TBVVP="$OUT/tb_app.vvp"
LOG="$OUT/run.log"

echo "compiling $APP_DIR"
MARCH="${MARCH:-rv32imacf_zicsr}"
"${RISCV_PREFIX}gcc" \
    -march="$MARCH" -mabi=ilp32 \
    -mcmodel=medany -msmall-data-limit=0 \
    -O2 -g -Wall -Wextra -ffreestanding -fno-common \
    -nostdlib -nostartfiles \
    -Wl,--no-relax -Wl,--no-warn-rwx-segments -T "$LINKER_SCRIPT" \
    -I "$LIB" -I "$APP_DIR" \
    "$LIB/crt0.S" "$LIB/trap.S" "$LIB/soc.c" "$LIB/string.c" "${SRCS[@]}" \
    -lgcc -o "$ELF"

"${RISCV_PREFIX}objcopy" -O binary "$ELF" "$BIN"

"$PYTHON" -I - "$BIN" "$HEX" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
if len(b) % 4:
    b += b'\0' * (4 - len(b) % 4)
with open(sys.argv[2], 'w') as f:
    for i in range(0, len(b), 4):
        f.write('%08x\n' % int.from_bytes(b[i:i+4], 'little'))
PY

"${RISCV_PREFIX}size" "$ELF"

if [ "${RUN_SIM:-1}" = "0" ]; then
    exit 0
fi

iverilog -g2012 -o "$TBVVP" \
    "$ROOT/sim/tb_app.v" "$ROOT/sim/i2c_eeprom.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/fp_unit.v" "$ROOT/rtl/bus.v" \
    "$ROOT/rtl/timer.v" "$ROOT/rtl/gpio.v" "$ROOT/rtl/uart.v" "$ROOT/rtl/spi.v" \
    "$ROOT/rtl/i2c.v"

set +e
vvp "$TBVVP" "+ITCM_HEX=$HEX" "+MAX_CYCLES=${MAX_CYCLES:-2000000}" "$@" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e

if [ "$status" -ne 0 ]; then
    echo "vvp failed (status $status)" >&2
    exit "$status"
fi
if ! grep -q 'TEST_PASS' "$LOG"; then
    echo "APP FAILED: see $LOG" >&2
    exit 1
fi
