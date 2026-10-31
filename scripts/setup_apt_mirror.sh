#!/usr/bin/env bash
# Switch Ubuntu apt sources to a faster local mirror. Optional.
# Default target is a public Ubuntu mirror example; run before scripts/setup_ubuntu.sh.
#
# Usage:
#   bash scripts/setup_apt_mirror.sh
#   MIRROR=https://<mirror-host>/ubuntu/ bash scripts/setup_apt_mirror.sh
set -euo pipefail

MIRROR="${MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/ubuntu/}"
if [ "${MIRROR%/}" = "$MIRROR" ]; then
    MIRROR="$MIRROR/"
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script targets Ubuntu/Linux." >&2
    exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=(sudo)
fi

if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    FILE=/etc/apt/sources.list.d/ubuntu.sources
elif [ -f /etc/apt/sources.list ]; then
    FILE=/etc/apt/sources.list
else
    echo "cannot find apt source file" >&2
    exit 1
fi

"${SUDO[@]}" cp -a "$FILE" "$FILE.bak.$(date +%s)"

"${SUDO[@]}" sed -i \
    -e "s|http://archive.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|https://archive.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|http://security.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|https://security.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|http://cn.archive.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|https://cn.archive.ubuntu.com/ubuntu/|$MIRROR|g" \
    -e "s|http://ports.ubuntu.com/ubuntu-ports/|$MIRROR|g" \
    -e "s|https://ports.ubuntu.com/ubuntu-ports/|$MIRROR|g" \
    "$FILE"

echo "apt source: $FILE"
grep -nE "^URIs:|^deb " "$FILE" | head -10 || true

"${SUDO[@]}" apt-get update
