#!/usr/bin/env bash
# riscv-dv <-> Sail 0.16 lockstep run for Sprouts500.
#
#   bash scripts/run_riscv_dv.sh               # seeds 1 2 3
#   bash scripts/run_riscv_dv.sh 7 42 999      # explicit seeds
#   INSTR_CNT=500 bash scripts/run_riscv_dv.sh 7
#
# For every seed:
#   1. riscv-dv (pyflow) generates an RV32IM program into build/riscv_dv/seed_<s>/
#   2. verif/riscv_dv/postprocess.py rewrites it into a bare-metal image that
#      Sail and the RTL execute identically (verif/riscv_dv/link_rtl.ld)
#   3. the image is linked at the RTL addresses (code in the ITCM at 0x0, data
#      in the DTCM at 0x10000000) and executed by sail_riscv_sim (configured
#      with the matching memory map) and by sim/tb_lockstep.v (Icarus)
#   4. verif/riscv_dv/compare.py compares the two retirement traces, the
#      store streams and the exception events (mcause/pc/mtval)
#
# Logs: logs/riscv_dv/seed_<s>.log (concatenated) and the per-stage files in
# build/riscv_dv/seed_<s>/; summary: logs/riscv_dv/summary.txt.
# Exit status is 0 only if every seed passes.  Run scripts/setup_riscv_dv.sh
# first.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIF="$ROOT/verif/riscv_dv"
VENDOR="$VERIF/vendor"
PYFLOW="$VENDOR/pyflow/pygen_src/test/riscv_instr_base_test.py"
VPY="$ROOT/.venv-dv/bin/python"
SAIL="$ROOT/build/tools/sail/bin/sail_riscv_sim"
BUILD="$ROOT/build/riscv_dv"
LOGS="$ROOT/logs/riscv_dv"
TB="$ROOT/sim/tb_lockstep.v"
CORE="$ROOT/rtl/core.v"
FPU="$ROOT/rtl/fp_unit.v"
INSTR_CNT="${INSTR_CNT:-200}"
MAX_CYCLES="${MAX_CYCLES:-2000000}"
PREFIX="${RISCV_PREFIX:-riscv64-unknown-elf-}"
SAIL_CONFIG="$VERIF/sail_rv32im.json"

SEEDS=("$@")
[ ${#SEEDS[@]} -eq 0 ] && SEEDS=(1 2 3)

say() { printf '[riscv-dv] %s\n' "$*"; }
die() { printf '[riscv-dv] ERROR: %s\n' "$*" >&2; exit 2; }
hex() { printf '0x%x' "$1"; }

[ -x "$VPY" ]  || die "missing .venv-dv; run: bash scripts/setup_riscv_dv.sh"
[ -x "$SAIL" ] || die "missing Sail; run: bash scripts/setup_riscv_dv.sh"
[ -f "$SAIL_CONFIG" ] || die "missing $SAIL_CONFIG"
tool() {  # <name> -> path of ${PREFIX}<name> or <name>
    command -v "${PREFIX}$1" 2>/dev/null || command -v "$1" 2>/dev/null
}
GCC="$(tool gcc)"           || true
OBJCOPY="$(tool objcopy)"   || true
NM="$(tool nm)"             || true
IVERILOG="$(command -v iverilog)" || true
VVP="$(command -v vvp)"     || true
for v in GCC OBJCOPY NM IVERILOG VVP; do
    [ -n "${!v}" ] || die "missing tool (set RISCV_PREFIX or run scripts/setup_ubuntu.sh)"
done

mkdir -p "$BUILD" "$LOGS"

say "building Icarus lockstep testbench"
"$IVERILOG" -g2012 -o "$BUILD/tb_lockstep.vvp" "$TB" "$CORE" "$FPU" \
    > "$LOGS/iverilog.log" 2>&1 \
    || { cat "$LOGS/iverilog.log"; die "testbench compile failed"; }

sym() { "$NM" "$1" | awk -v s="$2" '$3 == s { print $1; exit }'; }

# image <name> <label> <asm> <stop-sym> <halt-sym> [march]
#   link, load and run one image in both models, compare, print one result line
image() {
    local name=$1 label=$2 asm_src=$3 stop_sym=$4 halt_sym=$5
    local march=${6:-rv32im_zicsr}
    local b="$BUILD/$name"
    local l="$LOGS/$name.log"
    local stop halt stop_dec halt_dec rc

    mkdir -p "$b" "$LOGS"
    : >> "$l"
    L() { printf '%s\n' "$*" | tee -a "$l"; }
    L "== $label =="

    # ---- 3. link ----
    # One image, linked at the RTL addresses.  Sail 0.16 is configured with
    # the same memory map (verif/riscv_dv/sail_rv32im.json: RAM at 0x0 and at
    # 0x10000000, no ROM/CLINT/HTIF), so every PC, register value and store
    # address is numerically identical in both models.
    "$GCC" -march="$march" -mabi=ilp32 -nostdlib -nostartfiles -static \
        -mno-relax -I "$b" -T "$VERIF/link_rtl.ld" "$asm_src" -o "$b/prog.elf" \
        > "$b/link.log" 2>&1 \
        || { L "FAIL link"; tail -n 20 "$b/link.log" | sed 's/^/    /' | tee -a "$l"; return 1; }
    stop="$(sym "$b/prog.elf" "$stop_sym")"
    halt="$(sym "$b/prog.elf" "$halt_sym")"
    if [ -z "$stop" ] || [ -z "$halt" ]; then
        L "FAIL link symbols missing"; return 1
    fi
    stop_dec=$(printf '%d' "0x$stop")
    halt_dec=$(printf '%d' "0x$halt")
    # Sail checks --stop-at-pc before fetching the instruction and stops; the
    # RTL testbench stops when that instruction reaches WB, so both traces end
    # before the stop instruction.

    "$OBJCOPY" -O binary -j .text "$b/prog.elf" "$b/text.bin" \
        && "$OBJCOPY" -O binary -j .region_0 "$b/prog.elf" "$b/data.bin" \
        || { L "FAIL objcopy"; return 1; }
    "$VPY" "$VERIF/bin2hex.py" "$b/text.bin" "$b/itcm.hex"
    "$VPY" "$VERIF/bin2hex.py" "$b/data.bin" "$b/dtcm.hex"

    # ---- 4. RTL ----
    "$VVP" -n "$BUILD/tb_lockstep.vvp" \
        "+ITCM_HEX=$b/itcm.hex" "+DTCM_HEX=$b/dtcm.hex" \
        "+STOP_PC=$stop_dec" "+HALT_ADDR=$halt_dec" \
        "+TRACE=$b/rtl.trace" "+DTCM_DUMP=$b/dtcm.dump" \
        "+MAX_CYCLES=$MAX_CYCLES" > "$b/rtl.log" 2>&1
    rc=$?
    if [ $rc -ne 0 ] || ! grep -q LOCKSTEP_DONE "$b/rtl.log" \
            || grep -q 'LOCKSTEP_TIMEOUT\|LOCKSTEP_UNMAPPED' "$b/rtl.log"; then
        L "FAIL rtl run"; grep 'LOCKSTEP_' "$b/rtl.log" | sed 's/^/    /' | tee -a "$l"
        return 1
    fi

    # ---- 5. Sail ----
    "$SAIL" --config "$SAIL_CONFIG" --stop-at-pc "$(hex "$stop_dec")" \
        --inst-limit 2000000 --trace-instr --trace-arch-regs --trace-mem \
        --trace-exception --trace-output "$b/sail.trace" "$b/prog.elf" \
        > "$b/sail.stdout" 2>&1
    rc=$?
    if [ $rc -ne 0 ]; then
        L "FAIL sail run (rc=$rc)"; tail -n 5 "$b/sail.stdout" | sed 's/^/    /' | tee -a "$l"
        return 1
    fi

    # ---- 6. compare ----
    "$VPY" "$VERIF/compare.py" --rtl "$b/rtl.trace" --sail "$b/sail.trace" --stop-pc "$stop_dec" \
        > "$b/compare.log" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        L "$(grep -m1 'COMPARE_PASS' "$b/compare.log")"
        L "$(grep 'LOCKSTEP_DONE' "$b/rtl.log" | sed 's/^/    rtl: /')"
        return 0
    fi
    L "$(grep -m1 'COMPARE_' "$b/compare.log")"
    grep 'XFINDING' "$b/compare.log" | sed 's/^/    /' | tee -a "$l"
    grep -E 'MISMATCH|^  |NOTE' "$b/compare.log" | head -n 30 | sed 's/^/    /' | tee -a "$l"
    return 1
}

# run_seed <seed> -> 0 pass / 1 fail
run_seed() {
    local seed=$1
    local b="$BUILD/seed_$seed"
    local l="$LOGS/seed_$seed.log"

    rm -rf "$b"; mkdir -p "$LOGS" "$b"
    : > "$l"
    L() { printf '%s\n' "$*" | tee -a "$l"; }
    L "== seed $seed (instr_cnt=$INSTR_CNT) =="

    # ---- 1. generate ----
    (
        cd "$b" || exit 1
        touch user_define.h user_init.s
        PYTHONPATH="$VENDOR/_shim:$VENDOR/pyflow" \
        "$VPY" "$PYFLOW" --num_of_tests=1 --seed="$seed" --target=rv32imc \
            --disable_compressed_instr=1 --num_of_sub_program=0 \
            --instr_cnt="$INSTR_CNT" --no_csr_instr=1 --asm_file_name="$b/gen"
    ) > "$b/gen.log" 2>&1
    if [ ! -f "$b/gen_0.S" ]; then
        L "FAIL generate"; tail -n 20 "$b/gen.log" | sed 's/^/    /' | tee -a "$l"
        return 1
    fi

    # ---- 2. postprocess ----
    if ! "$VPY" "$VERIF/postprocess.py" "$b/gen_0.S" "$b/pp.S" > "$b/postprocess.log" 2>&1; then
        L "FAIL postprocess"; tee -a "$l" < "$b/postprocess.log"; return 1
    fi
    L "$(cat "$b/postprocess.log")"

    # ---- 3..6. link, RTL, Sail, compare ----
    image "seed_$seed" "seed $seed" "$b/pp.S" test_done halt_mailbox
}

summary="$LOGS/summary.txt"
: > "$summary"
fails=0
for s in "${SEEDS[@]}"; do
    if run_seed "$s"; then
        res="PASS"
    else
        res="FAIL"; fails=$((fails + 1))
    fi
    seed_line="$(grep -E 'COMPARE_PASS|^FAIL' "$LOGS/seed_$s.log" | tail -n 1)"
    printf 'seed %-6s %s  %s\n' "$s" "$res" "${seed_line#FAIL }" | tee -a "$summary"
done

# Directed image: traps that the random flow cannot produce (illegal encodings,
# unaligned load/store/fetch).  Its own trap handler writes a record to
# `trap_record` as evidence; the comparison uses the RTL/Sail exception events.
total=${#SEEDS[@]}
rm -rf "$BUILD/directed_trap" "$LOGS/directed_trap.log"
if image directed_trap "directed trap coverage" \
        "$VERIF/directed/trap.S" test_done halt_mailbox; then
    res="PASS"
else
    res="FAIL"; fails=$((fails + 1))
fi
dir_line="$(grep -E 'COMPARE_PASS|^FAIL' "$LOGS/directed_trap.log" | tail -n 1)"
printf 'directed trap %s  %s\n' "$res" "${dir_line}" | tee -a "$summary"
total=$((total + 1))

# Directed RV32A and RV32C images (the vendored random target emits only I/M).
for d in "directed_amo:amo.S:rv32ia_zicsr" "directed_compressed:compressed.S:rv32ic_zicsr"; do
    name=${d%%:*}; rest=${d#*:}; src=${rest%%:*}; march=${rest##*:}
    rm -rf "$BUILD/$name" "$LOGS/$name.log"
    if image "$name" "$name" "$VERIF/directed/$src" test_done halt_mailbox "$march"; then
        res="PASS"
    else
        res="FAIL"; fails=$((fails + 1))
    fi
    line="$(grep -E 'COMPARE_PASS|^FAIL' "$LOGS/$name.log" | tail -n 1)"
    printf '%s %s  %s\n' "$name" "$res" "$line" | tee -a "$summary"
    total=$((total + 1))
done
printf '\nriscv-dv lockstep: %d/%d passed\n' $(( total - fails )) "$total" | tee -a "$summary"
[ "$fails" -eq 0 ]
