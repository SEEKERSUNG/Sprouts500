"""Bus interface tests driven by ``BusSlave`` (``_helpers.BusSlave``).

The fake slave samples ``bus_req`` at every rising edge, applies a write exactly
once per transaction (at the ``bus_req`` rising edge) and holds ``bus_ready``
low for a configurable number of cycles.

What is targeted (``docs/architecture.md`` §6.2, §9 item 6; the
``csr_double_write`` case probes §9 item 8):

* one transaction per access: address/``we``/lane-aligned ``wdata``/``wstrb``;
* ``bus_wait`` freezes MEM, IF and ID together and repeats the same transaction
  until ``bus_ready`` rises; a ``bus_req`` re-asserting while a transaction is
  still active would be a duplicate side effect (counted by
  ``unexpected_rise_while_active``);
* ``bus_conflict`` (rtl/core.v:669) inserts a bubble between back-to-back bus
  accesses: ``bus_req`` must fall for one cycle between two transactions;
* a read result is captured once (rtl/core.v:780-783) and forwarded through WB;
* a word access must complete exactly once even when the slave never becomes
  ready on the first cycle.

Back-to-back store/read programs are position independent (every access
recomputes its address with ``lui``) because ``bus_conflict`` and the freeze
shift the PC of the following instruction, which would corrupt
``auipc``-relative addressing.
"""

import pathlib
import random
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    BUS_BASE, Asm, BusSlave, CoreHarness, RV32Interp, check_trace, expect,
    tb_case, tb_seed,
)
from _runner import run_test  # noqa: E402

N_ACCESS = 40


def build_rand_stores(seed):
    """Random stores (sb/sh/sw) to random byte-aligned bus addresses."""
    rng = random.Random(seed * 7919 + 1)
    a = Asm()
    a.li(2, 0)
    a.li(20, BUS_BASE)
    for _ in range(N_ACCESS):
        a.li(10, rng.getrandbits(32))
        a.li(4, rng.randrange(0, 0x400, 2))
        op = rng.choice(["sb", "sh", "sw"])
        if op == "sw":
            off = rng.choice([0x0, 0x4, 0x8, 0xC, 0x100])
        elif op == "sh":
            off = rng.choice([0x0, 0x2, 0x4, 0x6])
        else:
            off = rng.choice([0x0, 0x1, 0x2, 0x3])
        getattr(a, op)(10, 20, off + (0x200 if rng.random() < 0.5 else 0))
    a.halt()
    return a.assemble(), a.halt_store_pc


def build_rand_loads(seed):
    """Random loads (lb/lbu/lh/lhu/lw) whose results are XORed into x2."""
    rng = random.Random(seed * 104729 + 3)
    a = Asm()
    a.li(2, 0)
    a.li(20, BUS_BASE)
    for _ in range(N_ACCESS):
        op = rng.choice(["lb", "lbu", "lh", "lhu", "lw"])
        if op == "lw":
            off = rng.choice([0x0, 0x4, 0x40, 0x100])
        elif op in ("lh", "lhu"):
            off = rng.choice([0x0, 0x2, 0x10])
        else:
            off = rng.choice([0x0, 0x1, 0x2, 0x3, 0x20])
        a.li(4, off)
        getattr(a, op)(5, 20, 0)
        a.add(4, 20, 4)                 # same address computed at run time
        getattr(a, op)(5, 4, 0)
        a.xor(2, 2, 5)
    a.halt()
    return a.assemble(), a.halt_store_pc


def build_back_to_back(seed=0):
    """store; load; store; load without any intervening instruction."""
    a = Asm()
    a.li(2, 0)
    a.li(20, BUS_BASE)
    a.li(10, 0xCAFE_BABE)
    a.sw(10, 20, 0)
    a.lw(11, 20, 4)
    a.sw(10, 20, 8)
    a.lw(12, 20, 12)
    a.add(2, 11, 12)
    a.halt()
    return a.assemble(), a.halt_store_pc


def build_slow_read(seed=0):
    """One load whose address the slave answers only after 6 wait cycles."""
    a = Asm()
    a.li(2, 0)
    a.li(20, BUS_BASE)
    a.lw(11, 20, 0x80)
    a.addi(12, 11, 0)                   # consumes the load via WB bypass
    a.add(2, 11, 12)
    a.halt()
    return a.assemble(), a.halt_store_pc


def build_csr_bus_wait2():
    """csrrs to mscratch held in EX while the previous store waits on the bus."""
    a = Asm()
    a.li(20, BUS_BASE)
    a.li(8, 0x1F0)
    a.li(10, 0x1234_5678)
    # The store sits in EX in the cycle the csrrs enters EX; bus_wait then
    # freezes the csrrs there while MEM waits for bus_ready.
    msw_pc = a.here()
    a.sw(10, 20, 0xC0)                  # slow store; bus_wait freezes EX here
    csr_pc = a.here()
    a.csrrs(0, 0x340, 8)                # mscratch |= 0x1F0, result discarded
    a.halt()
    return a.assemble(), a.halt_store_pc, csr_pc


CASES = {
    "rand_stores": (build_rand_stores, (), (0, 3)),
    "rand_loads": (build_rand_loads, (), (0, 3)),
    "back_to_back": (build_back_to_back, (), (0, 4)),
    "slow_read": (build_slow_read, (), (6, 6)),
}
SLOW_ADDR = BUS_BASE + 0x80




@pytest.mark.parametrize("case", list(CASES))
def test_bus(case):
    seed = 0 if case in ("back_to_back", "slow_read") else tb_seed()
    run_test("test_bus", seed=seed, case=case)


def test_bus_csr_double_write():
    run_test("test_bus", seed=0, case="csr_double_write")


@cocotb.test()
async def bus(dut):
    case = tb_case("rand_stores")
    if case == "csr_double_write":
        await _double_write_case(dut)
        return
    seed = tb_seed()
    builder, args, waits = CASES[case]
    words, halt_pc = builder(seed, *args)

    rng = random.Random(seed * 31 + 5)
    init = bytes(rng.getrandbits(8) for _ in range(0x400))
    slave = BusSlave(dut, dut.clk, size=0x800, seed=seed,
                     wait_range=waits,
                     wait_by_addr=(dict() if case == "rand_stores"
                                   else {SLOW_ADDR: waits[0]}),
                     init_bytes=init)

    ref = RV32Interp(words)
    ref.mem.load_bus_bytes(init)
    ref = ref.run()
    ref_mem = bytearray(ref.mem.bus)
    init = bytes(slave.mem)            # as installed in the fake slave

    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    slave.start()
    await h.reset()

    stats = {"trap_taken": 0, "bus_wait": 0, "bus_conflict": 0, "wb_repeat": 0}
    trace = await h.run_with_trace(halt_pc, max_cycles=200_000, stats=stats)
    slave.stop()
    pcs = [pc for pc, _ir in trace]

    expect(stats["trap_taken"] == 0, f"{case}: unexpected trap")
    check_trace(pcs, ref.trace, label=f"bus {case} seed={seed} trace")
    checked = {"rand_stores": (2,), "rand_loads": (2, 5),
               "back_to_back": (2, 11, 12), "slow_read": (2, 11, 12)}[case]
    for idx in checked:
        got = h.reg(idx)
        expect(got == ref.regs[idx],
               f"{case}: x{idx}={got!r}, ref 0x{ref.regs[idx]:08x}")

    # -- transaction level checks -----------------------------------------
    expect(slave.rise_count == len(slave.txns),
           "bus_req rise without a recorded transaction")
    expect(slave.unexpected_rise_while_active == 0,
           f"{case}: bus_req re-asserted while a transaction was active "
           f"({slave.unexpected_rise_while_active}x)")

    # Compare the bus byte model against the byte-accurate reference model.
    used = max(t.addr for t in slave.txns) + 4 - BUS_BASE if slave.txns else 0
    fdiff = _first_diff(slave.mem[:used], ref_mem[:used])
    expect(fdiff >= used,
           f"{case}: bus byte model differs from reference at "
           f"0x{BUS_BASE + fdiff:08x}: core {slave.mem[fdiff:fdiff + 4].hex()} "
           f"ref {ref_mem[fdiff:fdiff + 4].hex()}")

    if case == "back_to_back":
        got = [(t.addr, t.we, t.wstrb) for t in slave.txns]
        want = [(BUS_BASE + 0, 1, 0xF), (BUS_BASE + 4, 0, 0),
                (BUS_BASE + 8, 1, 0xF), (BUS_BASE + 12, 0, 0)]
        expect(got == want, f"back_to_back transactions {got} != {want}")
        for t in slave.txns:
            expect(t.end_cycle is not None, "transaction never completed")
        for t0, t1 in zip(slave.txns, slave.txns[1:]):
            expect(t1.start_cycle > t0.end_cycle,
                   f"bus_req never fell between transactions: "
                   f"{t0} then {t1}")
        expect(stats["bus_conflict"] >= 2,
               f"bus_conflict only {stats['bus_conflict']} cycle(s) for "
               f"back-to-back accesses")
        # Requests are identical for both accesses.
        expect(slave.txns[0].wdata == slave.txns[2].wdata,
               "store data changed between back-to-back accesses")
        expect(slave.txns[0].addr != slave.txns[2].addr,
               "second back-to-back store went to the same address")

    if case == "slow_read":
        expect(len(slave.txns) == 1,
               f"slow read produced {len(slave.txns)} transactions")
        txn = slave.txns[0]
        expect(txn.we == 0 and txn.addr == SLOW_ADDR, f"bad txn {txn}")
        expect(txn.wait_cycles >= 6, f"only {txn.wait_cycles} wait cycles")
        expect(stats["bus_wait"] >= 6, "core did not assert bus_wait")
        # The core asserts bus_wait for every cycle the slave holds the
        # request and releases it exactly when bus_ready rises: no extra
        # wait and no premature completion.
        expect(stats["bus_wait"] == txn.wait_cycles,
               f"bus_wait {stats['bus_wait']} cycles vs slave "
               f"{txn.wait_cycles} wait cycles")
        expect(h.reg(11) == ref.regs[11], "slow load result not forwarded")

    if case == "rand_stores":
        stores = slave.writes
        expect(len(stores) == N_ACCESS,
               f"{len(stores)} writes for {N_ACCESS} stores")
        for t in stores:
            width = {1: 1, 2: 3, 4: 0xF}[bin(t.wstrb).count("1")]
            expect(t.wstrb in (width << lane for lane in range(4)),
                   f"wstrb {t.wstrb:04b} is not a {bin(t.wstrb).count('1')}-"
                   f"byte lane pattern")
            expect(t.addr & ~3 == t.addr, "bus_addr is not word aligned")
            expect((t.wdata & ~_active_bytes(t.wstrb)) == 0,
                   f"wdata 0x{t.wdata:08x} has non-zero bytes outside wstrb")
            expect(t.end_cycle is not None, "store transaction never completed")

    if case == "rand_loads":
        expect(len(slave.writes) == 0, "load-only program issued a write")
        expect(len(slave.txns) >= N_ACCESS - 4,
               f"only {len(slave.txns)} read transactions")

    dut._log.info("bus case=%s seed=%d: %d txns, bus_wait=%d, "
                  "bus_conflict=%d, wb_repeat=%d", case, seed, len(slave.txns),
                  stats["bus_wait"], stats["bus_conflict"],
                  stats["wb_repeat"])


def _active_bytes(wstrb):
    m = 0
    for lane in range(4):
        if wstrb & (1 << lane):
            m |= 0xFF << (8 * lane)
    return m


def _first_diff(a, b):
    """First differing byte index, or ``len(a)`` when equal/prefix-equal."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return len(a)


async def _double_write_case(dut):
    """A CSR held in EX during a bus wait must commit exactly once.

    The store next to the csrrs asserts ``bus_req`` in the same cycle the
    csrrs enters EX; the slave lowers ``bus_ready`` at that edge, ``bus_wait``
    freezes EX and the csrrs stays there for several cycles.  ``csr_we`` is
    gated by ``!pipe_freeze`` (rtl/core.v:1047), so it must be high for exactly
    one cycle even though EX is frozen; ``mscratch`` must end at 0x1f0.
    """
    words, halt_pc, csr_pc = build_csr_bus_wait2()
    frozen_pcs = {csr_pc}
    slave = BusSlave(dut, dut.clk, size=0x1000, seed=0, wait_range=(4, 4),
                     wait_by_addr={BUS_BASE + 0xC0: 4})
    h = CoreHarness(dut)
    h.load_program(words)
    await Timer(1, unit="ns")
    slave.start()
    await h.reset()
    stats = {"trap_taken": 0, "csr_we": 0, "bus_wait": 0}
    frozen = {}

    def cycle_hook(hh):
        if hh.pipe_freeze and hh.EX_PC in frozen_pcs and hh.EX_valid:
            frozen[hh.EX_PC] = frozen.get(hh.EX_PC, 0) + 1

    await h.run_with_trace(halt_pc, max_cycles=20_000, stats=stats,
                           cycle_hook=cycle_hook)
    slave.stop()
    dut._log.info("csr_double_write: csr_we high %d cycle(s), csrrs frozen "
                  "%d cycle(s), mscratch=0x%x", stats["csr_we"],
                  frozen.get(csr_pc, 0), h.mscratch or 0)
    expect(stats["trap_taken"] == 0, "unexpected trap")
    expect(stats["bus_wait"] >= 4, "the slow store did not stall the bus")
    # The csrrs must be held in EX for part of the bus wait, but csr_we must
    # see exactly one commit edge (rtl/core.v now gates it with !pipe_freeze).
    expect(frozen.get(csr_pc, 0) >= 1,
           f"the csrrs at 0x{csr_pc:08x} was never frozen in EX")
    expect(stats["csr_we"] == 1,
           f"csr_we stayed high for {stats['csr_we']} cycles; the csrrs was "
           f"held in EX for {frozen.get(csr_pc, 0)} bus_wait cycles, so it "
           f"must commit exactly once")
    expect((h.mscratch or 0) == 0x1F0,
           f"mscratch=0x{h.mscratch or 0:x}, expected 0x1f0")
