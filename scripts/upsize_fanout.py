#!/usr/bin/env python3
"""Drive-strength upsizing for a mapped structural netlist.

Yosys/ABC does not insert buffers or resize high-fanout nets.  This pass scans
the gate netlist, finds nets with many input pins and promotes the driving cell
to a stronger cell from the same family.  It is a first-order timing fix for
flat inferred memories / register-file muxes / pipeline enables; a real flow
should use a resizer (OpenROAD repair_design) or SRAM macros.

Usage: python3 scripts/upsize_fanout.py build/syn/core_netlist.v [threshold]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# strengths available in Nangate45 (ascending)
STRENGTHS = {
    "INV": ["X1", "X2", "X4", "X8", "X16", "X32"],
    "BUF": ["X1", "X2", "X3", "X4", "X8", "X16", "X32"],
    "NAND2": ["X1", "X2", "X4"],
    "NAND3": ["X1", "X2", "X4"],
    "NAND4": ["X1", "X2", "X4"],
    "NOR2": ["X1", "X2", "X4"],
    "NOR3": ["X1", "X2", "X4"],
    "NOR4": ["X1", "X2", "X4"],
    "AOI21": ["X1", "X2", "X4"],
    "AOI22": ["X1", "X2", "X4"],
    "AOI211": ["X1", "X2", "X4"],
    "AOI221": ["X1", "X2", "X4"],
    "AOI222": ["X1", "X2", "X4"],
    "OAI21": ["X1", "X2", "X4"],
    "OAI22": ["X1", "X2", "X4"],
    "OAI211": ["X1", "X2", "X4"],
    "OAI221": ["X1", "X2", "X4"],
    "OAI222": ["X1", "X2", "X4"],
    "MUX2": ["X1", "X2"],
    "XOR2": ["X1", "X2"],
    "XNOR2": ["X1", "X2"],
    "DFF": ["X1", "X2"],
    "DFFR": ["X1", "X2"],
    "DFFS": ["X1", "X2"],
}

OUTPUT_PIN = {
    "INV": "ZN", "NAND": "ZN", "NOR": "ZN", "AOI": "ZN", "OAI": "ZN",
    "BUF": "Z", "MUX2": "Z", "XOR": "Z", "XNOR": "Z",
    "DFF": "Q", "DFFR": "Q", "DFFS": "Q",
}


def base_type(t: str):
    m = re.match(r"^(.*)_X\d+$", t)
    return m.group(1) if m else None


def output_pin(t: str):
    for prefix in ("INV", "NAND", "NOR", "AOI", "OAI"):
        if t.startswith(prefix):
            return "ZN"
    for prefix in ("BUF", "MUX2", "XOR", "XNOR"):
        if t.startswith(prefix):
            return "Z"
    if t.startswith(("DFF", "DFFR", "DFFS")):
        return "Q"
    return None


def target_index(fanout: int, n: int) -> int:
    if fanout >= 400:
        return n - 1
    if fanout >= 150:
        return min(n - 1, 4)   # X8 for INV/BUF, X4 for logic
    if fanout >= 60:
        return min(n - 1, 3)
    return min(n - 1, 2)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    text = path.read_text()
    lines = text.splitlines()

    fanout: dict[str, int] = {}
    for m in re.finditer(r"\.\w+\(([^)]*)\)", text):
        net = m.group(1)
        if net:
            fanout[net] = fanout.get(net, 0) + 1

    cells = []
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s+(\S+)\s*\(\s*$", lines[i])
        if not m:
            i += 1
            continue
        typ, inst = m.group(1), m.group(2)
        ports = {}
        j = i + 1
        while j < len(lines):
            for pm in re.finditer(r"\.(\w+)\(([^)]*)\)", lines[j]):
                ports[pm.group(1)] = pm.group(2)
            if ");" in lines[j]:
                break
            j += 1
        cells.append((i, typ, inst, ports))
        i = j + 1

    changed = 0
    for idx, typ, inst, ports in cells:
        fam = base_type(typ)
        pin = output_pin(typ)
        if fam not in STRENGTHS or pin is None or pin not in ports:
            continue
        net = ports[pin]
        fo = fanout.get(net, 0)
        if fo < threshold:
            continue
        strengths = STRENGTHS[fam]
        ti = target_index(fo, len(strengths))
        new_typ = f"{fam}_{strengths[ti]}"
        if new_typ == typ:
            continue
        lines[idx] = lines[idx].replace(typ, new_typ, 1)
        changed += 1

    path.write_text("\n".join(lines) + "\n")
    top = sorted(fanout.items(), key=lambda kv: kv[1], reverse=True)[:8]
    print(f"upsize: {changed} cells, threshold={threshold}")
    print("top nets: " + ", ".join(f"{n}={c}" for n, c in top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
