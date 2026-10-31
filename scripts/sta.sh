#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"

if [ ! -f "$ROOT/build/syn/core_netlist.v" ]; then
    echo "missing build/syn/core_netlist.v; run: bash scripts/synth.sh" >&2
    exit 1
fi

STA="${STA:-}"
if [ -z "$STA" ]; then
    if command -v sta >/dev/null 2>&1; then
        STA="$(command -v sta)"
    elif [ -x "$OPENSTA_HOME/bin/sta" ]; then
        STA="$OPENSTA_HOME/bin/sta"
    elif [ -x /opt/opensta/bin/sta ]; then
        STA=/opt/opensta/bin/sta
    else
        echo "OpenSTA 'sta' not found; install it with: sudo bash scripts/setup_opensta.sh" >&2
        exit 1
    fi
fi

cd "$ROOT"
exec "$STA" -no_splash -exit scripts/sta.tcl
