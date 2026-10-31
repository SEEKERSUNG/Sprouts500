"""Directed CSR and trap tests.

One simulation per case (``TB_CASE``).  CSR semantics are checked against
hand-computed expectations because the reference interpreter intentionally does
not model SYSTEM instructions.

Cases
-----
csr_rw        csrrw/csrrs/csrrc/csrrwi/csrrsi/csrrci read-old/write-new values,
              mepc low-bit masking, mtvec/mscratch/mcause/mtval/mstatus writes,
              read-only CSR reads (cycle/instret/misa/mip/mvendorid).
csr_ro_trap   a write to a read-only CSR must raise illegal-instruction.
ecall         cause 11, mepc = ecall PC, mtval = 0, mstatus MPIE/MPP/MIE
              transitions across trap and mret.
ebreak        cause 3, handler skips the instruction.
illegal       cause 2, mtval = instruction word.
mis_load      cause 4, mtval = address, destination not written.
mis_store     cause 6, mtval = address, memory unchanged.
mis_branch    taken branch with a 2-mod-4 target -> cause 0, mtval = target.
mis_jalr      jalr target with bit 1 set -> cause 0, mtval = target & ~1.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    DTCM_BASE, Asm, CoreHarness, enc_b, enc_i, expect, tb_case,
)
from _runner import run_test  # noqa: E402

CSR_ATTR = {0x300: "mstatus", 0x304: "mie", 0x305: "mtvec",
            0x340: "mscratch", 0x341: "mepc", 0x342: "mcause",
            0x343: "mtval", 0x344: "mip"}


def _handler(a, label="handler"):
    """Record cause/mepc/mtval/mstatus, skip the faulting instruction, mret."""
    a.label(label)
    a.csrr(20, 0x342)          # mcause
    a.csrr(21, 0x341)          # mepc
    a.csrr(22, 0x343)          # mtval
    a.csrr(23, 0x300)          # mstatus as seen in the handler
    a.addi(21, 21, 4)          # resume after the faulting instruction
    a.csrw(0x341, 21)
    a.mret()


def _install_handler(a):
    a.la(5, "handler")
    a.csrw(0x305, 5)           # mtvec, direct mode


def _common_trap_case(fault_emit, cause, tval, setup=None,
                      pre_regs=None, marker_pc_delta=4):
    """Build a program with one faulting instruction and a trap handler.

    ``fault_emit(a)`` emits the faulting instruction and returns its PC.
    """
    a = Asm()
    a.li(10, 0)                # marker: handler must not touch x10
    a.li(11, 0)
    _install_handler(a)
    if setup is not None:
        setup(a)
    fault_pc = fault_emit(a)
    marker_pc = a.here()
    a.addi(10, 10, 0x123)
    a.halt()
    _handler(a)
    words = a.assemble()
    exp = {
        "regs": {10: 0x123, 20: cause, 21: fault_pc + 4,
                 22: tval(fault_pc) if callable(tval) else tval,
                 23: 0x1800},
        "csrs": {"mcause": cause, "mepc": fault_pc + 4,
                 "mstatus": 0x1880, "mtval":
                 tval(fault_pc) if callable(tval) else tval},
        "marker_pc": marker_pc,
    }
    if pre_regs:
        exp["regs"].update(pre_regs)
    return words, a.halt_store_pc, exp


def build_csr_rw():
    a = Asm()
    a.li(5, 0x1234_5678)
    a.csrw(0x340, 5)
    a.csrr(6, 0x340)                  # 0x12345678
    a.li(8, 0xFFFF_0000)
    a.csrrs(7, 0x340, 8)              # old = 0xFFFF5678
    a.li(10, 0x0F0F_0F0F)
    a.csrrc(9, 0x340, 10)             # old = 0xFFFF5678
    a.csrrwi(11, 0x340, 0x01)         # old = 0xF0F05070
    a.csrrsi(12, 0x340, 0x1E)         # old = 0x01
    a.csrrci(13, 0x340, 0x0F)         # old = 0x1F
    a.csrr(14, 0x340)                 # 0x10
    a.csrrs(15, 0x340, 0)             # rs1=x0: read-only, no write
    a.li(16, 0x1234_567F)
    a.csrw(0x341, 16)
    a.csrr(17, 0x341)                 # mepc masks low 2 bits
    a.li(18, 0x2A)
    a.csrw(0x305, 18)
    a.csrr(19, 0x305)
    a.li(20, 0xDEAD_BEEF)
    a.csrw(0x342, 20)
    a.csrr(21, 0x342)
    a.li(22, 0xCAFE_BABE)
    a.csrw(0x343, 22)
    a.csrr(23, 0x343)
    a.li(24, 0x0000_1888)
    a.csrw(0x300, 24)
    a.csrr(25, 0x300)
    a.csrr(26, 0x301)                 # misa
    a.csrr(27, 0x344)                 # mip
    a.csrr(28, 0xf11)                 # mvendorid
    a.csrr(29, 0xc82)                 # instret high
    a.halt()
    words = a.assemble()
    exp = {
        "regs": {6: 0x1234_5678, 7: 0x1234_5678, 9: 0xFFFF_5678,
                 11: 0xF0F0_5070, 12: 0x01, 13: 0x1F, 14: 0x10, 15: 0x10,
                 17: 0x1234_567C, 19: 0x2A, 21: 0xDEAD_BEEF,
                 23: 0xCAFE_BABE, 25: 0x1888, 26: 0x4000_1100,
                 27: 0x0, 28: 0x0, 29: 0x0},
        "csrs": {"mscratch": 0x10, "mepc": 0x1234_567C, "mtvec": 0x2A,
                 "mcause": 0xDEAD_BEEF, "mtval": 0xCAFE_BABE,
                 "mstatus": 0x1888},
        "nonzero": (5, 16, 18, 20, 22, 24),
    }
    return words, a.halt_store_pc, exp


def build_csr_ro_trap():
    word = enc_i(0x73, 0, 1, 5, 0xC00)   # csrrw x0, cycle, x5

    def fault_emit(a):
        a.li(5, 0x1)
        pc = a.here()
        a.raw(word)
        return pc

    return _common_trap_case(fault_emit, 2, word,
                             pre_regs={5: 0x1})


def build_ecall():
    def setup(a):
        a.li(5, 0x8)
        a.csrs(0x300, 5)                 # MIE = 1

    def fault_emit(a):
        pc = a.here()
        a.ecall()
        return pc

    words, halt_pc, exp = _common_trap_case(fault_emit, 11, 0, setup=setup)
    exp["regs"][23] = 0x1880
    exp["csrs"]["mstatus"] = 0x1888
    return words, halt_pc, exp


def build_ebreak():
    def fault_emit(a):
        pc = a.here()
        a.ebreak()
        return pc

    return _common_trap_case(fault_emit, 3, 0)


def build_illegal():
    def fault_emit(a):
        pc = a.here()
        a.raw(0xFFFF_FFFF)
        return pc

    return _common_trap_case(fault_emit, 2, 0xFFFF_FFFF)


def build_mis_load():
    def setup(a):
        a.li(5, 0x55)
        a.li(28, DTCM_BASE)

    def fault_emit(a):
        pc = a.here()
        a.lw(5, 28, 1)                   # addr[0] set, word load
        return pc

    words, halt_pc, exp = _common_trap_case(
        fault_emit, 4, DTCM_BASE + 1, setup=setup, pre_regs={5: 0x55})
    return words, halt_pc, exp


def build_mis_store():
    def setup(a):
        a.li(5, 0xDEAD_BEEF)
        a.li(28, DTCM_BASE)

    def fault_emit(a):
        pc = a.here()
        a.sw(5, 28, 2)                   # addr[1:0] = 2, word store
        return pc

    words, halt_pc, exp = _common_trap_case(
        fault_emit, 6, DTCM_BASE + 2, setup=setup, pre_regs={5: 0xDEAD_BEEF})
    exp["dtcm"] = {0: 0x0000_0000}
    return words, halt_pc, exp


def build_mis_branch():
    def fault_emit(a):
        pc = a.here()
        a.raw(enc_b(0x63, 0, 0, 0, 6))   # beq x0,x0 -> pc+6 (2 mod 4)
        return pc

    def tval(pc):
        return pc + 6

    words, halt_pc, exp = _common_trap_case(fault_emit, 0, tval)
    exp["regs"][10] = 0x123                 # continued after the branch
    return words, halt_pc, exp


def build_mis_jalr():
    def fault_emit(a):
        pc_au = a.here()
        a.auipc(5, 0)
        p_addi = a.here()
        a.addi(5, 5, (p_addi + 18) - pc_au)  # x5 = p_addi + 18
        pc = a.here()
        a.jalr(0, 5, 0)                       # target bit1 set -> misaligned
        return pc

    def tval(pc):
        return pc + 14                        # p_addi+18 where pc=p_addi+4

    words, halt_pc, exp = _common_trap_case(fault_emit, 0, tval)
    exp["regs"][10] = 0x123
    return words, halt_pc, exp


BUILDERS = {
    "csr_rw": build_csr_rw,
    "csr_ro_trap": build_csr_ro_trap,
    "ecall": build_ecall,
    "ebreak": build_ebreak,
    "illegal": build_illegal,
    "mis_load": build_mis_load,
    "mis_store": build_mis_store,
    "mis_branch": build_mis_branch,
    "mis_jalr": build_mis_jalr,
}


@pytest.mark.parametrize("case", list(BUILDERS))
def test_csr_trap(case):
    run_test("test_csr_trap", seed=0, case=case)


@cocotb.test()
async def csr_trap(dut):
    case = tb_case("csr_rw")
    words, halt_pc, exp = BUILDERS[case]()

    h = CoreHarness(dut)
    h.load_program(words)
    if exp.get("dtcm"):
        h.write_dtcm_words(0, [exp["dtcm"].get(i, 0)
                               for i in range(max(exp["dtcm"]) + 1)])
    await Timer(1, unit="ns")
    await h.reset()
    await h.run_until_halt(max_cycles=20_000, halt_pc=halt_pc)

    for idx, value in exp["regs"].items():
        got = h.reg(idx)
        expect(got == value,
               f"{case}: x{idx} got {got!r} expected 0x{value:08x}")
    for name, value in exp.get("csrs", {}).items():
        got = getattr(h, name) if name != "mip" else h.sig("mip_comb")
        expect(got == value,
               f"{case}: {name} got {got!r} expected 0x{value:08x}")
    for idx in exp.get("nonzero", ()):
        got = h.reg(idx)
        expect(got is not None and got != 0,
               f"{case}: x{idx} unexpectedly zero/unknown")
    if exp.get("dtcm"):
        for idx, value in exp["dtcm"].items():
            got = h.dtcm(idx)
            expect(got == value,
                   f"{case}: DTCM[{idx}] got {got!r} expected 0x{value:08x}")

    dut._log.info("csr_trap case=%s: halted at cycle %d", case, h.cycle)
