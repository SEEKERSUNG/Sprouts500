#!/usr/bin/env bash
# Fetch the Nangate45 Liberty for local synthesis/STA only.  The file is
# proprietary (see its header) and is deliberately not redistributed in git.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/env.sh"
mkdir -p "$ROOT/lib"

# Optional mirror prefix: GH_RAW_MIRROR=<url-prefix>
MIRROR="${GH_RAW_MIRROR:-}"
URL="${MIRROR}https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib"

curl -L --retry 3 --max-time 600 -o "$ROOT/lib/NangateOpenCellLibrary_typical.lib" "$URL"
ls -lh "$ROOT/lib/NangateOpenCellLibrary_typical.lib"
