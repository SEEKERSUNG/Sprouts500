"""Small shared utilities: byte masks, seeded RNG, hex-image writing."""
from __future__ import annotations

import logging
import os
import random
from pathlib import Path

import cocotb

ROOT = Path(__file__).resolve().parents[2]


def byte_mask(wstrb: int) -> int:
    """Expand a 4-bit write strobe to a 32-bit byte mask."""
    mask = 0
    for lane in range(4):
        if (wstrb >> lane) & 1:
            mask |= 0xFF << (8 * lane)
    return mask


def merge_bytes(old: int, data: int, wstrb: int) -> int:
    mask = byte_mask(wstrb)
    return (old & ~mask) | (data & mask)


def test_seed() -> int:
    """Seed for this test: ``SEED=`` environment wins, else cocotb's seed."""
    env = os.environ.get("SEED")
    # ``int(..., 0)`` accepts decimal, 0x..., 0o... and 0b... forms.
    return int(env, 0) if env not in (None, "") else int(cocotb.RANDOM_SEED)


def make_rng(name: str) -> random.Random:
    """Return a per-test RNG and log the seed (reproducible via SEED=...)."""
    seed = test_seed()
    # Plain logger: cocotb.log carries the "test" context marker, which makes
    # the line hard to grep from logs/cocotb/run-*.log.
    log = logging.getLogger("sprouts500")
    log.setLevel(logging.INFO)
    log.info("RNG %s seed=%d", name, seed)
    return random.Random(seed)


def write_hex_image(path: Path, words: list[int]) -> Path:
    """Write a flat 32-bit word image for ``$readmemh``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{w & 0xFFFF_FFFF:08x}\n" for w in words))
    return path


def program_path(name: str) -> Path:
    """HEX image path under the repository's build output directory."""
    return ROOT / "sim_build" / "programs" / f"{name}.hex"
