#!/usr/bin/env bash
# Sprouts500 environment (Ubuntu / Linux).
#   source scripts/env.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SPROUTS500_ROOT="$ROOT"
export RISCV_PREFIX="${RISCV_PREFIX:-riscv64-unknown-elf-}"
export PYTHON="${PYTHON:-python3}"

case "$(uname -s)" in
    Linux*) ;;
    *) echo "warning: Sprouts500 scripts target Ubuntu/Linux" >&2 ;;
esac

# OpenSTA system install (scripts/setup_opensta.sh).
export OPENSTA_HOME="${OPENSTA_HOME:-/opt/opensta}"
if [ -d "$OPENSTA_HOME/bin" ]; then
    case ":$PATH:" in
        *":$OPENSTA_HOME/bin:"*) ;;
        *) export PATH="$OPENSTA_HOME/bin:$PATH" ;;
    esac
fi
