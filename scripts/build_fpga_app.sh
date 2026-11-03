#!/usr/bin/env bash
# Compile an application for UART boot at 0x810 and package header + raw image.
#   bash scripts/build_fpga_app.sh sw/example
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

if [ $# -ne 1 ] || [ ! -d "$1" ]; then
    echo "usage: bash scripts/build_fpga_app.sh <app-dir>" >&2
    exit 2
fi

OUT="$ROOT/build/fpga/app"
MARCH="${MARCH:-rv32imafc_zicsr}"
RUN_SIM=0 OUT="$OUT" APP_LD="$ROOT/sw/boot/app.ld" MARCH="$MARCH" \
    bash "$ROOT/scripts/build_app.sh" "$1"

"$PYTHON" -I "$ROOT/scripts/uart_boot.py" --pack-only \
    --image "$OUT/app.bin" --output "$OUT/app.pkg"
