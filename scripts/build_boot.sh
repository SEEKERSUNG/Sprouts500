#!/usr/bin/env bash
# Build the resident 2 KiB UART Bootloader and its FPGA ITCM word-hex image.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

OUT="$ROOT/build/fpga/boot"
mkdir -p "$OUT"
MARCH="${BOOT_MARCH:-rv32imc_zicsr_zifencei}"
# GCC 13 otherwise diagnoses constant absolute MMIO pointers as zero-size arrays.

"${RISCV_PREFIX}gcc" \
    -march="$MARCH" -mabi=ilp32 \
    -mcmodel=medany -msmall-data-limit=0 \
    -Os -g -Wall -Wextra -Wno-array-bounds -ffreestanding -fno-common -fno-builtin \
    -nostdlib -nostartfiles \
    -Wl,--no-relax -Wl,--no-warn-rwx-segments -T "$ROOT/sw/boot/boot.ld" \
    "$ROOT/sw/boot/start.S" "$ROOT/sw/boot/boot.c" \
    -lgcc -o "$OUT/boot.elf"

"${RISCV_PREFIX}objcopy" -O binary "$OUT/boot.elf" "$OUT/boot.bin"
"${RISCV_PREFIX}size" "$OUT/boot.elf"

"$PYTHON" -I - "$OUT/boot.bin" "$OUT/boot.hex" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
if len(b) > 2048:
    raise SystemExit(f"bootloader is {len(b)} bytes; limit is 2048")
b += b'\0' * (2048 - len(b))
with open(sys.argv[2], 'w') as f:
    for i in range(0, len(b), 4):
        f.write('%08x\n' % int.from_bytes(b[i:i+4], 'little'))
print(f"BOOT_IMAGE bytes={len(b)} words={len(b)//4}")
PY
