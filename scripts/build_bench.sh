#!/usr/bin/env bash
# Build and run the CoreMark and Dhrystone benchmarks on the Sprouts500 SoC.
#
#   bash scripts/build_bench.sh coremark              # best (default)
#   bash scripts/build_bench.sh dhrystone             # best
#   MODE=ground bash scripts/build_bench.sh dhrystone # Dhrystone ground rule
#   BENCH="coremark dhrystone" bash scripts/build_bench.sh
#   ITERATIONS=10 DHRY_RUNS=1000 bash scripts/build_bench.sh
#
# "ground" compiles the unmodified benchmark sources with -O2 (Dhrystone keeps
# its own `#pragma GCC optimize("no-inline")`).  "best" uses per-benchmark
# optimisation flags; for Dhrystone the source is copied to build/ with only
# that non-algorithmic pragma removed, so the benchmark logic is unchanged.
#
# The timing reference is the `cycle` CSR at 100 MHz (the SoC clock of
# sim/tb_bench.v; the HZ/CLOCKS_PER_SEC constants in bench/*/port match it).
# UART output is decoded by the testbench and printed.
#
# Output: build/bench/<name>.{elf,bin,hex}; logs/bench/<name>.{log,uart}
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
SIZE="${RISCV_PREFIX}size"
IVERILOG="$(command -v iverilog)"
VVP="$(command -v vvp)"

MODE="${MODE:-best}"
case "$MODE" in
    ground|best) ;;
    *) echo "MODE must be ground or best (got '$MODE')" >&2; exit 2 ;;
esac

# Common code-generation flags; per-benchmark optimisation flags are added by
# each builder.  TCM data is small, so no small-data/gp-relative addressing.
# RV32IMACF: compressed code shrinks the image and the IF expander removes
# redundant fetch cycles.  MARCH can be overridden to e.g. rv32im_zicsr for
# an A/B comparison.
MARCH="${MARCH:-rv32imacf_zicsr}"
BASE_FLAGS=(
    -march="$MARCH" -mabi=ilp32
    -mcmodel=medany -msmall-data-limit=0
    -g -ffreestanding -fno-common
    -I "$ROOT/bench/include" -I "$ROOT/bench/runtime"
    -ffunction-sections -fdata-sections
)
BASE_LINK_FLAGS=(
    -nostdlib -nostartfiles
    -Wl,--no-relax -Wl,--no-warn-rwx-segments
    -Wl,--gc-sections
    -T "$ROOT/bench/bench.ld"
)

opt_flags() {  # <benchmark>; one flag per line
    if [ -n "${BENCH_OPT_FLAGS:-}" ]; then
        printf '%s\n' ${BENCH_OPT_FLAGS}
        return
    fi
    if [ "$MODE" = ground ]; then
        printf '%s\n' -O2
        return
    fi
    case "$1" in
        coremark)
            # Unrolling + IPA-PTA is fastest and still fits in the 32 KiB ITCM
            # (about 27.5 KB text).  Plain -O3/LTO is slower here.
            printf '%s\n' -O2 -flto -funroll-loops -fipa-pta ;;
        dhrystone)
            # -O3 hurts Dhrystone on this core; -O2+LTO (with the source's
            # no-inline pragma removed) is the fastest measured variant.
            printf '%s\n' -O2 -flto ;;
    esac
}

lto_flags() {  # <opt flags...>
    case " $* " in
        *" -flto "*) printf '%s\n' -flto ;;
    esac
}

flags_str() {  # <opt flags...>
    printf '%s -march=%s' "$*" "$MARCH"
}

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
    local -a opt lto
    mapfile -t opt < <(opt_flags coremark)
    mapfile -t lto < <(lto_flags "${opt[@]}")
    local fstr
    fstr="$(flags_str "${opt[@]}")"

    rm -rf "$obj"
    mkdir -p "$obj"

    local cflags=("${BASE_FLAGS[@]}" "${opt[@]}"
                  -I "$ROOT/bench/coremark/port" -I "$ROOT/bench/raw/coremark"
                  -Wno-unused-parameter -Wno-implicit-fallthrough
                  "-DFLAGS_STR=\"$fstr\""
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

    "$CC" "${BASE_FLAGS[@]}" "${BASE_LINK_FLAGS[@]}" "${lto[@]}" \
        "$ROOT/bench/start.S" "$obj"/*.o -lgcc -o "$out.elf"
    "$SIZE" "$out.elf"
    if [ "${BUILD_ONLY:-0}" = 1 ]; then return 0; fi
    run_bench "$name" "$out" "${MAX_CYCLES:-120000000}"
}

build_dhrystone() {
    local name="dhrystone_${MODE}"
    local out="$OUT/$name"
    local obj="$OUT/${name}_obj"
    local runs="${DHRY_RUNS:-500}"
    local -a opt lto
    mapfile -t opt < <(opt_flags dhrystone)
    mapfile -t lto < <(lto_flags "${opt[@]}")

    rm -rf "$obj"
    mkdir -p "$obj"

    # "best": compile the upstream source with only its non-algorithmic
    # `#pragma GCC optimize("no-inline")` removed.  The benchmark logic and
    # data are the unmodified Dhrystone 2.1 source.
    local dsrc="$ROOT/bench/raw/dhrystone/dhrystone.c"
    local dmain="$ROOT/bench/dhrystone/port/main.c"
    if [ "$MODE" = best ]; then
        local src_copy="$OUT/dhrystone_best_src"
        rm -rf "$src_copy"
        mkdir -p "$src_copy"
        sed '/^#pragma GCC optimize ("no-inline")$/d' "$dsrc" > "$src_copy/dhrystone.c"
        sed '/^#pragma GCC optimize ("no-inline")$/d' "$dmain" > "$src_copy/main.c"
        dsrc="$src_copy/dhrystone.c"
        dmain="$src_copy/main.c"
    fi

    local cflags=("${BASE_FLAGS[@]}" "${opt[@]}"
                  -I "$ROOT/bench/dhrystone/port" -I "$ROOT/bench/raw/dhrystone"
                  -include "$ROOT/bench/dhrystone/port/dhry_patch.h"
                  -DNUMBER_OF_RUNS="$runs"
                  -Wno-unused-parameter -Wno-implicit-fallthrough
                  -Wno-implicit-function-declaration -Wno-implicit-int
                  -Wno-return-type)

    local srcs=(
        "$ROOT/bench/runtime/bench_io.c"
        "$ROOT/bench/runtime/string.c"
        "$dmain"
        "$dsrc"
    )
    local o
    for f in "${srcs[@]}"; do
        o="$obj/$(basename "${f%.c}").o"
        "$CC" "${cflags[@]}" -c "$f" -o "$o"
    done

    "$CC" "${BASE_FLAGS[@]}" "${BASE_LINK_FLAGS[@]}" "${lto[@]}" \
        "$ROOT/bench/start.S" "$obj"/*.o -lgcc -o "$out.elf"
    "$SIZE" "$out.elf"
    if [ "${BUILD_ONLY:-0}" = 1 ]; then return 0; fi
    run_bench "$name" "$out" "${MAX_CYCLES:-120000000}"
}

run_bench() {
    local name=$1 out=$2 max_cycles=$3
    local tb="$OUT/tb_bench.vvp"
    local log="$BENCH_LOG/$name.uart"
    local stdout="$BENCH_LOG/$name.log"

    "$OBJCOPY" -O binary "$out.elf" "$out.bin"
    pack_hex "$out.bin" "$out.hex"

    echo "== $name ($("$SIZE" "$out.elf" | awk 'NR==2 {print $1"B text, "$3"B bss"}')) =="

    local plus=("+ITCM_HEX=$out.hex" "+MAX_CYCLES=$max_cycles" "+UART_FILE=$log")
    if [ -n "${BUS_TRACE:-}" ]; then
        plus+=("+BUS_TRACE=$BENCH_LOG/$name.bus")
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

"$IVERILOG" -g2012 -o "$OUT/tb_bench.vvp" \
    "$ROOT/sim/tb_bench.v" \
    "$ROOT/rtl/soc_top.v" "$ROOT/rtl/core.v" "$ROOT/rtl/fp_unit.v" "$ROOT/rtl/bus.v" \
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
