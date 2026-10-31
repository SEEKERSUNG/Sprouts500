#!/usr/bin/env bash
# Run the cocotb pytest suites under Icarus Verilog.
#
# Usage:
#   bash scripts/run_cocotb.sh [paths...] [extra pytest args...]
#
# Without a path, runs cocotb/soc and cocotb/core when it exists.  Arguments
# that name existing files/directories are test targets; everything from the
# first option (or non-existent path) onwards is passed to pytest, e.g.
#   bash scripts/run_cocotb.sh cocotb/soc -k timer -x
#   SEED=12345 bash scripts/run_cocotb.sh cocotb/soc/test_uart.py -q
#
# Results: logs/cocotb/junit.xml plus logs/cocotb/run-<timestamp>.log.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
LOG_DIR="$ROOT/logs/cocotb"
STAMP="$(date +%Y%m%d-%H%M%S)-$$"
LOG="$LOG_DIR/run-$STAMP.log"
JUNIT="$LOG_DIR/junit.xml"

if [ ! -x "$VENV/bin/python" ]; then
    echo "error: $VENV not found; run 'bash scripts/setup_cocotb.sh' first" >&2
    exit 1
fi
if ! command -v iverilog >/dev/null 2>&1; then
    echo "error: iverilog not found (run scripts/setup_ubuntu.sh)" >&2
    exit 1
fi
mkdir -p "$LOG_DIR"

targets=()
while [ "$#" -gt 0 ] && [[ "$1" != -?* ]]; do
    if [ -e "$1" ]; then
        targets+=("$1")
        shift
    else
        echo "error: test target does not exist: $1" >&2
        exit 1
    fi
done
if [ "${#targets[@]}" -eq 0 ]; then
    # Fresh JUnit files per run so stale results from earlier runs are not
    # mistaken for this run's.
    rm -f "$LOG_DIR"/junit*.xml
    targets=("cocotb/soc")
    if [ -d "$ROOT/cocotb/core" ]; then
        targets+=("cocotb/core")
    fi
fi

export SIM=icarus
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
# cocotb seeds its random.CocotbRandom from COCOTB_RANDOM_SEED (or time);
# common/misc.test_seed() lets SEED= override it for the tests' own RNG.
if [ -n "${SEED:-}" ]; then
    if [[ "$SEED" =~ ^0[xX][0-9a-fA-F]+$ ]]; then
        seed_dec=$((SEED))
    elif [[ "$SEED" =~ ^[0-9]+$ ]]; then
        seed_dec="$SEED"
    else
        echo "error: SEED must be a decimal or 0x-hex integer: $SEED" >&2
        exit 2
    fi
    echo "SEED=$SEED (COCOTB_RANDOM_SEED=$seed_dec)"
    export COCOTB_RANDOM_SEED="$seed_dec"
else
    echo "SEED unset: each run uses a fresh cocotb random seed (printed in the log)"
fi

echo "targets: ${targets[*]}"
echo "log:     $LOG"
echo "junit:   $JUNIT"

# The core and SoC suites both define test_bus.py; with pytest's default
# prepend import mode the second one collides.  Run one pytest per directory
# (duplicate -k/-m-like filters and plain file paths are forwarded to each).
set +e
status=0
matched=0
empty=0
: > "$LOG"
first=1
for target in "${targets[@]}"; do
    if [ "$first" -eq 0 ]; then
        echo "" >> "$LOG"
        junit_arg="--junitxml=$LOG_DIR/junit-$(basename "$target").xml"
    else
        junit_arg="--junitxml=$JUNIT"
    fi
    first=0
    "$VENV/bin/python" -m pytest \
        -o python_files='*.py' \
        -p cocotb_tools._pytest.plugin \
        "$junit_arg" \
        "$target" "$@" 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 5 ]; then
        # A shared -k/-m filter may match only one suite; remember it and
        # only fail if no target collected any test at all.
        echo "note: no tests matched in $target"
        empty=1
    elif [ "$rc" -ne 0 ]; then
        status=$rc
    else
        matched=1
    fi
done
if [ "$status" -eq 0 ] && [ "$matched" -eq 0 ] && [ "$empty" -eq 1 ]; then
    status=5
fi
set -e

if [ "$status" -ne 0 ]; then
    echo "cocotb regression FAILED (exit $status); see $LOG" >&2
else
    echo "cocotb regression PASSED; log: $LOG"
fi
exit "$status"
