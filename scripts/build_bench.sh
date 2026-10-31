#!/usr/bin/env bash
# Build and run the CoreMark and Dhrystone benchmarks on the Sprouts500 SoC.
#
#   bash scripts/build_bench.sh coremark
#   bash scripts/build_bench.sh dhrystone
#   ITERATIONS=10 bash scripts/build_bench.sh coremark
#   DHRY_RUNS=1000 bash scripts/build_bench.sh dhrystone
#   BENCH="coremark dhrystone" bash scripts/build_bench.sh   # both
#
# The timing reference is the `cycle` CSR at 50 MHz (the SoC clock of
# sim/tb_bench.v).  UART output is decoded by the testbench and printed.
#
# Output: build/bench/<name>.{elf,bin,hex} and build/bench/<name>.log
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

BENCH_NAME="${1:-}"
if [ -z "$BENCH_NAME" ]; then
    BENCH_NAME="${BENCH:-coremark dhrystone}"
fi

OUT="$ROOT/build/bench"
BENCH_LOG="$ROOT/logs/bench"
mkdir -p "$OUT" "$BENCH_LOG"

CC="${RISCV_PREFIX}gcc"
OBJCOPY="${RISCV_PREFIX}objcopy"
NM="${RISCV_PREFIX}nm"
IVERILOG="$(command -v iverilog)"
VVP="$(command -v vvp)"

COMMON_FLAGS=(
    -march=rv32im_zicsr -mabi=ilp32
    -mcmodel=medany -msmall-data-limit=0
    -O2 -g -ffreestanding -fno-common
    -I "$ROOT/bench/include" -I "$ROOT/bench/runtime"
    -ffunction-sections -fdata-sections
)
LINK_FLAGS=(
    -nostdlib -nostartfiles
    -Wl,--no-relax -Wl,--no-warn-rwx-segments
    -Wl,--gc-sections
    -T "$ROOT/bench/bench.ld"
)

pack_hex() {
    "$PYTHON" -I - "$1" "$2" <<'PY'
import sys
from pathlib import Path
b = Path(sys.argv[1]).read_bytes()
if len(b) % 4:
    b += b'\0' * (4 - len(b) % 4)
Path(sys.argv[2]).write_text(''.join('%08x\n' % int.from_bytes(b[i:i+4], 'little')
                                     for i in range(0, len(b), 4)))
PY
}

build_coremark() {
    local name=coremark
    local out="$OUT/$name"
    local obj="$OUT/${name}_obj"
    local iters="${ITERATIONS:-1}"
    rm -rf "$obj"
    mkdir -p "$obj"

    local cflags=("${COMMON_FLAGS[@]}"
                  -I "$ROOT/bench/coremark/port" -I "$ROOT/bench/raw/coremark"
                  -Wno-unused-parameter -Wno-implicit-fallthrough
                  "-DFLAGS_STR=\"-O2 -march=rv32im_zicsr\""
                  "-DITERATIONS=$iters" -DPERFORMANCE_RUN=1)

    local srcs=(
        "$ROOT/bench/runtime/bench_io.c"
        "$ROOT/bench/runtime/string.c"
        "$ROOT/bench/coremark/port/main.c"
        "$ROOT/bench/coremark/port/core_portme.c"
        "$ROOT/bench/coremark/port/ee_printf.c"
        "$ROOT/bench/raw/coremark/core_list_join.c"
        "$ROOT/bench/raw/coremark/core_matrix.c"
        "$ROOT/bench/raw/coremark/core_state.c"
        "$ROOT/bench/raw/coremark/core_util.c"
    )
    local o
    for f in "${srcs[@]}"; do
        o="$obj/$(basename "${f%.c}").o"
        "$CC" "${cflags[@]}" -c "$f" -o "$o"
    done
    # core_main.c defines main(); rename it so the port wrapper can report.
    "$CC" "${cflags[@]}" -Dmain=coremark_main \
        -c "$ROOT/bench/raw/coremark/core_main.c" -o "$obj/core_main.o"

    "$CC" "${COMMON_FLAGS[@]}" "${LINK_FLAGS[@]}" \
        "$ROOT/bench/start.S" "$obj"/*.o -lgcc -o "$out.elf"
    run_bench "$name" "$out" "${MAX_CYCLES:-120000000}"
}

build_dhrystone() {
    local name=dhrystone
    local out="$OUT/$name"
    local obj="$OUT/${name}_obj"
    local runs="${DHRY_RUNS:-500}"
    rm -rf "$obj"
    mkdir -p "$obj"

    local cflags=("${COMMON_FLAGS[@]}"
                  -I "$ROOT/bench/dhrystone/port" -I "$ROOT/bench/raw/dhrystone"
                  -include "$ROOT/bench/dhrystone/port/dhry_patch.h"
                  -DNUMBER_OF_RUNS="$runs"
                  -Wno-unused-parameter -Wno-implicit-fallthrough
                  -Wno-implicit-function-declaration -Wno-implicit-int
                  -Wno-return-type)

    local srcs=(
        "$ROOT/bench/runtime/bench_io.c"
        "$ROOT/bench/runtime/string.c"
        "$ROOT/bench/dhrystone/port/main.c"
        "$ROOT/bench/raw/dhrystone/dhrystone.c"
    )
    local o
    for f in "${srcs[@]}"; do
        o="$obj/$(basename "${f%.c}").o"
        "$CC" "${cflags[@]}" -c "$f" -o "$o"
    done

    "$CC" "${COMMON_FLAGS[@]}" "${LINK_FLAGS[@]}" \
        "$ROOT/bench/start.S" "$obj"/*.o -lgcc -o "$out.elf"
    run_bench "$name" "$out" "${MAX_CYCLES:-120000000}"
}

run_bench() {
    local name=$1 out=$2 max_cycles=$3
    local tb="$OUT/tb_bench.vvp"
    local log="$BENCH_LOG/$name.uart"
    local stdout="$BENCH_LOG/$name.log"
    local bustrace="$BENCH_LOG/$name.bus"

    "$OBJCOPY" -O binary "$out.elf" "$out.bin"
    pack_hex "$out.bin" "$out.hex"

    local size
    size="$("${RISCV_PREFIX}size" "$out.elf" | awk 'NR==2 {print $1"B text, "$3"B bss"}')"
    echo "== $name ($size) =="

    local plus=("+ITCM_HEX=$out.hex" "+MAX_CYCLES=$max_cycles" "+UART_FILE=$log")
    if [ -n "${BUS_TRACE:-}" ]; then
        plus+=("+BUS_TRACE=$bustrace")
    fi

    set +e
    "$VVP" "$tb" "${plus[@]}" > "$stdout" 2>&1
    local rc=$?
    set -e
    cat "$log"
    echo "   UART bytes: $(wc -c < "$log")"
    grep -E 'BENCH_(RESULT|CYCLES|PASS|FAIL|TIMEOUT)' "$stdout"
    if [ $rc -ne 0 ] || ! grep -q 'BENCH_PASS' "$stdout"; then
        echo "benchmark $name failed (exit $rc); see $stdout" >&2
        return 1
    fi
}

# Build the testbench once per invocation.
"$IVERILOG" -g2012 -o "$OUT/tb_bench.vvp" \
    "$ROOT/sim/tb_bench.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/bus.v" \
    "$ROOT/rtl/timer.v" "$ROOT/rtl/gpio.v" "$ROOT/rtl/uart.v" "$ROOT/rtl/spi.v"

rc=0
for b in $BENCH_NAME; do
    case "$b" in
        coremark)  build_coremark  || rc=1 ;;
        dhrystone) build_dhrystone || rc=1 ;;
        *) echo "unknown benchmark: $b (use coremark or dhrystone)" >&2; rc=2 ;;
    esac
done
exit $rc
