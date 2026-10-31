#!/usr/bin/env bash
# Install CUDD 3.0.0 and OpenSTA system-wide.
#
#   sudo bash scripts/setup_opensta.sh
#
# Defaults:
#   /opt/opensta              source/build tree + bin/sta
#   /usr/local/bin/sta        command symlink
#   /etc/profile.d/opensta.sh login-shell PATH setup
#
# Optional overrides:
#   OPENSTA_PREFIX=/opt/opensta
#   OPENSTA_BIN_DIR=/usr/local/bin
#   OPENSTA_REBUILD=1          force a rebuild even if build/sta exists
#   OPENSTA_GIT=https://gitcode.com/gh_mirrors/op/OpenSTA.git
#   GH_RAW_MIRROR=<url-prefix>
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PREFIX="${OPENSTA_PREFIX:-/opt/opensta}"
BIN_DIR="${OPENSTA_BIN_DIR:-/usr/local/bin}"
OPENSTA="$PREFIX/OpenSTA"
CUDD="$PREFIX/cudd-3.0.0"
REBUILD="${OPENSTA_REBUILD:-0}"
MIRROR="${GH_RAW_MIRROR:-}"
OPENSTA_GIT="${OPENSTA_GIT:-https://github.com/The-OpenROAD-Project/OpenSTA.git}"

if [ "$(id -u)" -ne 0 ]; then
    echo "OpenSTA is installed system-wide; run:" >&2
    echo "  sudo bash $0" >&2
    exit 1
fi

mkdir -p "$PREFIX" "$BIN_DIR"

if [ "$REBUILD" = "1" ]; then
    rm -rf "$OPENSTA/build" "$CUDD"
fi

if [ "$REBUILD" = "1" ] || [ ! -x "$OPENSTA/build/sta" ]; then
    # --------------------------------------------------------------
    # sources
    # --------------------------------------------------------------
    if [ ! -f "$OPENSTA/CMakeLists.txt" ]; then
        rm -rf "$OPENSTA"
        git clone --depth 1 "$OPENSTA_GIT" "$OPENSTA"
    fi

    if [ ! -f "$CUDD/configure" ]; then
        rm -rf "$CUDD"
        if [ ! -f "$PREFIX/cudd-3.0.0.tar.gz" ]; then
            curl -L --retry 3 --max-time 600 \
                -o "$PREFIX/cudd-3.0.0.tar.gz" \
                "${MIRROR}https://raw.githubusercontent.com/davidkebo/cudd/main/cudd_versions/cudd-3.0.0.tar.gz"
        fi
        tar -xzf "$PREFIX/cudd-3.0.0.tar.gz" -C "$PREFIX"
    fi

    # --------------------------------------------------------------
    # CUDD
    # --------------------------------------------------------------
    ( cd "$CUDD" && ./configure && make -j"$(nproc)" )

    # --------------------------------------------------------------
    # OpenSTA
    # --------------------------------------------------------------
    TCL_ARGS=()
    for lib in /usr/lib/x86_64-linux-gnu/libtcl8.6.so /usr/lib64/libtcl8.6.so /usr/lib/libtcl8.6.so; do
        if [ -f "$lib" ]; then TCL_ARGS+=("-DTCL_LIBRARY=$lib"); break; fi
    done
    for hdr in /usr/include/tcl8.6/tcl.h /usr/include/tcl/tcl.h /usr/include/tcl.h; do
        if [ -f "$hdr" ]; then TCL_ARGS+=("-DTCL_HEADER=$hdr"); break; fi
    done

    cmake -S "$OPENSTA" -B "$OPENSTA/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCUDD_DIR="$CUDD" \
        -DUSE_TCL_READLINE=OFF \
        -DBUILD_TESTS=OFF \
        "${TCL_ARGS[@]}"

    cmake --build "$OPENSTA/build" -j"$(nproc)"
else
    echo "using existing OpenSTA build: $OPENSTA/build/sta"
fi

install -m 755 "$OPENSTA/build/sta" "$PREFIX/bin/sta"
ln -sfn "$PREFIX/bin/sta" "$BIN_DIR/sta"

# System-wide login-shell environment.
PROFILE=/etc/profile.d/opensta.sh
cat > "$PROFILE" <<EOF
# OpenSTA system install
export OPENSTA_HOME=$PREFIX
case ":\$PATH:" in
    *":\$OPENSTA_HOME/bin:"*) ;;
    *) export PATH="\$OPENSTA_HOME/bin:\$PATH" ;;
esac
EOF
chmod 644 "$PROFILE"

# Let the normal project owner rebuild without sudo.
OWNER="$(stat -c %U "$ROOT" 2>/dev/null || echo root)"
GROUP="$(stat -c %G "$ROOT" 2>/dev/null || echo root)"
if id "$OWNER" >/dev/null 2>&1; then
    chown -R "$OWNER:$GROUP" "$PREFIX" 2>/dev/null || true
fi

echo
echo "OpenSTA: $PREFIX/bin/sta"
echo "Command: $BIN_DIR/sta"
"$PREFIX/bin/sta" -version || true
