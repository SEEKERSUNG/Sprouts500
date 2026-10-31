#!/usr/bin/env bash
# Set up the riscv-dv (pyflow) instruction generator and Sail 0.16 golden model
# for Sprouts500 lockstep verification.
#
#   bash scripts/setup_riscv_dv.sh
#
# Everything needed is vendored under verif/riscv_dv/vendor except:
#   - pyboolector (binary wheel from PyPI), required by pyvsc's solver
#   - the Sail 0.16 Linux x86_64 release tarball, downloaded once to
#     build/tools/sail_dl and checked by size and SHA-256 before extraction
#
# The script is idempotent, needs no sudo, and does not leave anything in /tmp.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIF="$ROOT/verif/riscv_dv"
VENDOR="$VERIF/vendor"
VENV="$ROOT/.venv-dv"
TOOLS="$ROOT/build/tools"
SAIL_VER="0.16"
SAIL_DIR="$TOOLS/sail"
SAIL_URL="https://github.com/riscv/sail-riscv/releases/download/${SAIL_VER}/sail-riscv-Linux-x86_64.tar.gz"
SAIL_SHA="f9bb8a32b8f3eddbdf69b3a29d96dff4ba1e77f45877165e0478418367da6bee"
SAIL_SIZE="2966239"
PYBOO_PIN="PyBoolector==3.2.4.38488416565"
PYTHON="${PYTHON:-python3}"

say()  { printf '[riscv-dv setup] %s\n' "$*"; }
fail() { printf '[riscv-dv setup] ERROR: %s\n' "$*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null || fail "'$PYTHON' not found"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python >= 3.10 required (found $("$PYTHON" --version 2>&1))"

# --------------------------------------------------------------------------
# 1. virtualenv (+ pip bootstrap; Ubuntu's python3 has no ensurepip)
# --------------------------------------------------------------------------
say "virtualenv: $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv --without-pip "$VENV" \
        || fail "python3 -m venv failed (install python3-venv?)"
fi

if ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    say "bootstrapping pip inside .venv-dv"
    mkdir -p "$TOOLS"
    # Prefer the pip already present in the repo's main .venv (offline, no
    # download); otherwise fetch get-pip.py.  get-pip is only an interpreter
    # bootstrap - it is removed after use.
    main_pip=""
    for p in "$ROOT"/.venv/lib/python*/site-packages; do
        [ -d "$p/pip" ] && main_pip="$p"
    done
    if [ -n "$main_pip" ]; then
        say "using bundled pip from ${main_pip#$ROOT/}"
        cp -a "$main_pip/pip" "$main_pip/pip-"*.dist-info \
              "$( "$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' )/"
    else
        say "downloading get-pip.py"
        curl -fsSL --retry 3 -o "$TOOLS/get-pip.py" https://bootstrap.pypa.io/get-pip.py \
            || fail "cannot download get-pip.py (no network and no .venv pip available)"
        "$VENV/bin/python" "$TOOLS/get-pip.py" --no-cache-dir \
            || fail "get-pip.py failed"
        rm -f "$TOOLS/get-pip.py"
    fi
    "$VENV/bin/python" -m pip --version >/dev/null 2>&1 \
        || fail "pip bootstrap failed"
fi
VPY="$VENV/bin/python"
say "python: $("$VPY" --version 2>&1)"

# --------------------------------------------------------------------------
# 2. solver dependency (binary wheel, PyPI)
# --------------------------------------------------------------------------
if ! "$VPY" -c 'import pyboolector' >/dev/null 2>&1; then
    say "installing $PYBOO_PIN from PyPI"
    "$VPY" -m pip install --no-cache-dir "$PYBOO_PIN" \
        || fail "pip install $PYBOO_PIN failed (network required on first setup)"
fi

# --------------------------------------------------------------------------
# 3. vendored pyvsc / pyucis (offline, pinned)
# --------------------------------------------------------------------------
SITE="$("$VPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
say "site-packages: ${SITE#$ROOT/}"

install_tree() {  # <src-dir> <package-dir-name> <dist-info-name-prefix>
    local src="$1" pkg="$2" meta="$3"
    [ -d "$src/$pkg" ] || fail "missing vendored package $src/$pkg"
    # dist-info directories are versioned ("pyvsc-0.9.1...dist-info"), so match
    # the prefix instead of an exact name.
    if [ -d "$SITE/$pkg" ] && compgen -G "$SITE/${meta}*" >/dev/null; then
        say "vendored $pkg already installed"
    else
        rm -rf "$SITE/$pkg" "$SITE/${meta}"*
        cp -a "$src/$pkg" "$SITE/$pkg"
        cp -a "$src/${meta}"* "$SITE/"
        say "installed vendored $pkg"
    fi
}
install_tree "$VENDOR/wheels/pyvsc" vsc pyvsc-
install_tree "$VENDOR/wheels/pyucis" ucis pyucis-
# pyvsc imports toposort at import time; ship the pure-python fallback.
[ -f "$SITE/toposort.py" ] || cp -a "$VENDOR/_shim/toposort.py" "$SITE/toposort.py"

# --------------------------------------------------------------------------
# 4. compatibility patches (Python 3.14 / pyboolector >= 3.2)
# --------------------------------------------------------------------------
# pyucis still imports ctypes internals from the private `_ctypes` module,
# which Python 3.13+ no longer exposes for these names.
patched=0
while IFS= read -r f; do
    "$VPY" - "$f" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
n = s.replace("from _ctypes import", "from ctypes import")
if n != s:
    open(p, "w").write(n)
PY
    patched=$((patched + 1))
done < <(grep -rl "from _ctypes import" "$SITE/ucis" 2>/dev/null || true)
[ "$patched" -gt 0 ] && say "patched $patched pyucis file(s): _ctypes -> ctypes"

# pyboolector 3.2 moved the option constants into the BtorOption enum.
RAND="$SITE/vsc/model/randomizer.py"
if [ -f "$RAND" ] && grep -q 'pyboolector\.BTOR_OPT_' "$RAND"; then
    "$VPY" - "$RAND" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = re.sub(r"pyboolector\.(BTOR_OPT_[A-Z0-9_]+)", r"pyboolector.BtorOption.\1", s)
open(p, "w").write(s)
PY
    say "patched pyvsc randomizer.py: pyboolector.BTOR_OPT_* -> BtorOption.BTOR_OPT_*"
fi

# --------------------------------------------------------------------------
# 5. Sail 0.16 golden model
# --------------------------------------------------------------------------
SAIL_EXE="$SAIL_DIR/bin/sail_riscv_sim"
if [ ! -x "$SAIL_EXE" ]; then
    mkdir -p "$TOOLS/sail_dl"
    TARBALL="$TOOLS/sail_dl/sail-riscv-Linux-x86_64.tar.gz"
    if [ -f "$TARBALL" ]; then
        have_size="$(stat -c %s "$TARBALL")"
        have_sha="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
        if [ "$have_size" != "$SAIL_SIZE" ] || [ "$have_sha" != "$SAIL_SHA" ]; then
            say "discarding cached Sail tarball (size/checksum mismatch)"
            rm -f "$TARBALL"
        fi
    fi
    if [ ! -f "$TARBALL" ]; then
        say "downloading Sail $SAIL_VER ($SAIL_SIZE bytes)"
        curl -fL --retry 3 --connect-timeout 20 -o "$TARBALL.part" "$SAIL_URL" \
            || fail "cannot download $SAIL_URL"
        mv "$TARBALL.part" "$TARBALL"
    fi
    have_size="$(stat -c %s "$TARBALL")"
    have_sha="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
    [ "$have_size" = "$SAIL_SIZE" ] || fail "Sail tarball size $have_size != $SAIL_SIZE"
    [ "$have_sha" = "$SAIL_SHA" ] || fail "Sail tarball sha256 $have_sha != $SAIL_SHA"
    say "Sail tarball checksum OK"
    rm -rf "$TOOLS/sail_extract"
    mkdir -p "$TOOLS/sail_extract"
    tar -xzf "$TARBALL" -C "$TOOLS/sail_extract" || fail "Sail tarball extraction failed"
    [ -x "$TOOLS/sail_extract/sail-riscv-Linux-x86_64/bin/sail_riscv_sim" ] \
        || fail "unexpected Sail tarball layout"
    rm -rf "$SAIL_DIR"
    mv "$TOOLS/sail_extract/sail-riscv-Linux-x86_64" "$SAIL_DIR"
    rmdir "$TOOLS/sail_extract"
    chmod +x "$SAIL_EXE"
else
    say "Sail $SAIL_VER already extracted"
fi

# --------------------------------------------------------------------------
# 6. versions
# --------------------------------------------------------------------------
say "versions:"
"$VPY" - <<'PY'
import importlib.metadata as md
for p in ("pyvsc", "pyucis", "PyBoolector", "toposort"):
    try:
        print("  %-12s %s" % (p, md.version(p)))
    except md.PackageNotFoundError:
        print("  %-12s (not installed)" % p)
import vsc, ucis, pyboolector
print("  imports      vsc, ucis, pyboolector OK")
PY
printf '  %-12s %s\n' "sail" "$("$SAIL_EXE" --version 2>&1 | head -n 1)"

say "done; run: bash scripts/run_riscv_dv.sh [seeds...]"
