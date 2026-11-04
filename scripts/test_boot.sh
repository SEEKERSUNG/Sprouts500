#!/usr/bin/env bash
# End-to-end UART boot regression: protocol unit tests + real RTL UART download.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"
OUT="$ROOT/build/fpga"
LOGS="$ROOT/logs/boot"
export MARCH="${MARCH:-rv32imafc_zicsr}"
mkdir -p "$OUT" "$LOGS"

bash "$ROOT/scripts/build_boot.sh"
bash "$ROOT/scripts/build_fpga_app.sh" "$ROOT/sw/boot_test"
"$PYTHON" -I "$ROOT/tests/boot/test_uart_boot.py"

"$PYTHON" -I - "$OUT/app/app.pkg" "$OUT/app/app_pkg_bytes.hex" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
with open(sys.argv[2], 'w') as f:
    for value in b:
        f.write(f'{value:02x}\n')
print(f'IMAGE_LEN={len(b)}')
PY

IMAGE_LEN=$(stat -c %s "$OUT/app/app.pkg")
iverilog -g2012 -s tb_boot_init -o "$OUT/tb_boot_init.vvp" \
    "$ROOT/sim/tb_boot_init.v" "$ROOT/fpga/soc_fpga_top.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/fp_unit.v" \
    "$ROOT/rtl/bus.v" "$ROOT/rtl/timer.v" "$ROOT/rtl/gpio.v" \
    "$ROOT/rtl/uart.v" "$ROOT/rtl/spi.v" "$ROOT/rtl/i2c.v"
# soc_fpga_top's default ITCM_INIT_FILE is relative to the synthesis tool's
# working directory; run from the repo root so the same default works here.
( cd "$ROOT" && vvp "$OUT/tb_boot_init.vvp" "+BOOT_HEX=$OUT/boot/boot.hex" ) \
    2>&1 | tee "$LOGS/tb_boot_init.log"
if ! grep -q 'BOOT_INIT_PASS' "$LOGS/tb_boot_init.log"; then
    echo "UART BOOT INIT TEST FAILED: see $LOGS/tb_boot_init.log" >&2
    exit 1
fi

iverilog -g2012 -s tb_boot -o "$OUT/tb_boot.vvp" \
    "$ROOT/sim/tb_boot.v" "$ROOT/fpga/soc_fpga_top.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/fp_unit.v" \
    "$ROOT/rtl/bus.v" "$ROOT/rtl/timer.v" "$ROOT/rtl/gpio.v" \
    "$ROOT/rtl/uart.v" "$ROOT/rtl/spi.v" "$ROOT/rtl/i2c.v"

set +e
vvp "$OUT/tb_boot.vvp" \
    "+BOOT_HEX=$OUT/boot/boot.hex" \
    "+IMAGE_HEX=$OUT/app/app_pkg_bytes.hex" \
    "+IMAGE_LEN=$IMAGE_LEN" 2>&1 | tee "$LOGS/tb_boot.log"
status=${PIPESTATUS[0]}
set -e
if [ "$status" -ne 0 ] || ! grep -q 'BOOT_TEST_PASS' "$LOGS/tb_boot.log"; then
    echo "UART BOOT TEST FAILED: see $LOGS/tb_boot.log" >&2
    exit 1
fi
