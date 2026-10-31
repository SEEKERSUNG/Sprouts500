#!/usr/bin/env python3
"""Pack a flat binary into one 32-bit little-endian word per line (for $readmemh)."""
import sys
from pathlib import Path

data = Path(sys.argv[1]).read_bytes()
if len(data) % 4:
    data += b"\x00" * (4 - len(data) % 4)
with open(sys.argv[2], "w", encoding="ascii") as f:
    for i in range(0, len(data), 4):
        f.write("%08x\n" % int.from_bytes(data[i:i + 4], "little"))
