"""Shared helpers for the Sprouts500 cocotb core test-suite.

This module is intentionally self-contained: it does not import anything from
``cocotb/common`` or any other directory owned by the SoC suite.

Contents
--------
* minimal RV32IM assembler (``Asm``) with label fixups,
* an independent RV32IM reference interpreter (``RV32Interp``),
* ``CoreHarness`` wrapping hierarchical access to the DUT,
* ``BusSlave``, a fake single-outstanding bus slave with randomized wait
  states and transaction logging.
"""

from __future__ import annotations

import random

import rvc_ref

from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

MASK32 = 0xFFFFFFFF
HALT_ADDR = 0x0000_3FFC       # last ITCM word in the 4096-word cocotb config
HALT_MAGIC = 0x5A5A_5A5A
ITCM_WORDS = 4096
DTCM_WORDS = 1024
ITCM_BYTES = ITCM_WORDS * 4
DTCM_BYTES = DTCM_WORDS * 4
ITCM_DATA_BASE = 0x0000_2000  # code lives below this, data above it
DTCM_BASE = 0x1000_0000
BUS_BASE = 0x2000_0000
BUS_BYTES = 0x1000              # reference-model window for the fake bus slave


# ---------------------------------------------------------------------------
# small integer helpers
# ---------------------------------------------------------------------------
def u32(value: int) -> int:
    return value & MASK32


def s32(value: int) -> int:
    value &= MASK32
    return value - (1 << 32) if value & 0x8000_0000 else value


def sext(value: int, bits: int) -> int:
    mask = (1 << bits) - 1
    value &= mask
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def u64(value: int) -> int:
    return value & 0xFFFF_FFFF_FFFF_FFFF


def s64(value: int) -> int:
    value &= 0xFFFF_FFFF_FFFF_FFFF
    return value - (1 << 64) if value & (1 << 63) else value


def handle_int(handle):
    """Return an int for a cocotb handle, or None when it has X/Z bits."""
    try:
        return int(handle.value)
    except (ValueError, TypeError):
        return None


def random_operand(rng, pool=None):
    """Interesting 32-bit operand: boundaries mixed with random values."""
    if pool is None:
        pool = [0, 1, 2, 3, 0x7FFF_FFFF, 0x8000_0000, 0x8000_0001, 0xFFFF_FFFE,
                0xFFFF_FFFF, 0x5555_5555, 0xAAAA_AAAA, 0x0001_0000, 0xFFFF_0000,
                0x0000_FFFF, 0x00FF_00FF, 0x1F, 0x1E, 0x20, 0x21, 0x13]
    r = rng.random()
    if r < 0.55:
        return rng.choice(pool)
    if r < 0.9:
        return rng.getrandbits(32)
    return rng.getrandbits(rng.randint(1, 32))


def expect(cond, msg: str):
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# RV32IM encoders
# ---------------------------------------------------------------------------
def enc_r(opcode, rd, funct3, rs1, rs2, funct7):
    return ((funct7 & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def enc_i(opcode, rd, funct3, rs1, imm):
    return ((imm & 0xFFF) << 20) | ((rs1 & 0x1F) << 15) | ((funct3 & 0x7) << 12) | \
           ((rd & 0x1F) << 7) | (opcode & 0x7F)


def enc_s(opcode, funct3, rs1, rs2, imm):
    imm &= 0xFFF
    return (((imm >> 5) & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | \
           ((funct3 & 0x7) << 12) | ((imm & 0x1F) << 7) | (opcode & 0x7F)


def enc_b(opcode, funct3, rs1, rs2, imm):
    imm &= 0x1FFF
    return (((imm >> 12) & 1) << 31) | (((imm >> 5) & 0x3F) << 25) | ((rs2 & 0x1F) << 20) | \
           ((rs1 & 0x1F) << 15) | ((funct3 & 0x7) << 12) | (((imm >> 1) & 0xF) << 8) | \
           (((imm >> 11) & 1) << 7) | (opcode & 0x7F)


def enc_u(opcode, rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | ((rd & 0x1F) << 7) | (opcode & 0x7F)


def enc_j(opcode, rd, imm):
    imm &= 0x1F_FFFF
    return (((imm >> 20) & 1) << 31) | (((imm >> 1) & 0x3FF) << 21) | \
           (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xFF) << 12) | \
           ((rd & 0x1F) << 7) | (opcode & 0x7F)


def enc_amo(funct5, rd, rs1, rs2, aq=0, rl=0, funct3=2):
    """AMO*.W / LR.W / SC.W: funct5 | aq | rl | rs2 | rs1 | 010 | rd | 0101111.

    AMOs have no immediate field: the effective address is rs1 + 0.
    """
    return ((funct5 & 0x1F) << 27) | ((aq & 1) << 26) | ((rl & 1) << 25) | \
           ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | ((funct3 & 7) << 12) | \
           ((rd & 0x1F) << 7) | 0x2F


def _fld(v, hi, lo):
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def _pack(*pairs):
    """pairs: (value, hi, lo) fields packed into a 16-bit encoding."""
    out = 0
    for item in pairs:
        v, hi, lo = item
        out |= (int(v) & ((1 << (hi - lo + 1)) - 1)) << lo
    return out & 0xFFFF


# -- RVC encoders (16-bit) ------------------------------------------------
def ce_addi(rd, imm):
    return _pack((0, 15, 13), ((imm >> 5) & 1, 12, 12), (rd, 11, 7),
                 (imm & 31, 6, 2), (1, 1, 0))


def ce_li(rd, imm):
    """C.LI (funct3=010), not C.ADDI (funct3=000): different expansion for rs1."""
    return _pack((2, 15, 13), ((imm >> 5) & 1, 12, 12), (rd, 11, 7),
                 (imm & 31, 6, 2), (1, 1, 0))


def ce_lui(rd, imm6):
    return _pack((3, 15, 13), ((imm6 >> 5) & 1, 12, 12), (rd, 11, 7),
                 (imm6 & 31, 6, 2), (1, 1, 0))


def ce_addi16sp(imm):
    off = imm & 0x3FF
    return _pack((3, 15, 13), (_fld(off, 9, 9), 12, 12), (2, 11, 7),
                 (_fld(off, 4, 4), 6, 6), (_fld(off, 6, 6), 5, 5),
                 (_fld(off, 8, 7), 4, 3), (_fld(off, 5, 5), 2, 2), (1, 1, 0))


def ce_addi4spn(rdp, imm):
    return _pack((0, 15, 13), (_fld(imm, 9, 6), 10, 7),
                 (_fld(imm, 5, 4), 12, 11), (_fld(imm, 2, 2), 6, 6),
                 (_fld(imm, 3, 3), 5, 5), (rdp - 8, 4, 2), (0, 1, 0))


def ce_lw(rdp, rs1p, uimm):
    return _pack((2, 15, 13), (_fld(uimm, 5, 3), 12, 10), (rs1p - 8, 9, 7),
                 (_fld(uimm, 6, 6), 5, 5), (_fld(uimm, 2, 2), 6, 6),
                 (rdp - 8, 4, 2), (0, 1, 0))


def ce_sw(rs2p, rs1p, uimm):
    return _pack((6, 15, 13), (_fld(uimm, 5, 3), 12, 10), (rs1p - 8, 9, 7),
                 (_fld(uimm, 6, 6), 5, 5), (_fld(uimm, 2, 2), 6, 6),
                 (rs2p - 8, 4, 2), (0, 1, 0))


def ce_lwsp(rd, uimm):
    return _pack((2, 15, 13), (_fld(uimm, 5, 5), 12, 12), (rd, 11, 7),
                 (_fld(uimm, 4, 2), 6, 4), (_fld(uimm, 7, 6), 3, 2), (2, 1, 0))


def ce_swsp(rs2, uimm):
    return _pack((6, 15, 13), (_fld(uimm, 5, 2), 12, 9),
                 (_fld(uimm, 7, 6), 8, 7), (rs2, 6, 2), (2, 1, 0))


def ce_slli(rd, shamt):
    return _pack((0, 15, 13), ((shamt >> 5) & 1, 12, 12), (rd, 11, 7),
                 (shamt & 31, 6, 2), (2, 1, 0))


def ce_srli(rdp, shamt):
    return _pack((4, 15, 13), (0, 11, 10), (rdp - 8, 9, 7),
                 (shamt & 31, 6, 2), (1, 1, 0))


def ce_srai(rdp, shamt):
    return _pack((4, 15, 13), (1, 11, 10), (rdp - 8, 9, 7),
                 (shamt & 31, 6, 2), (1, 1, 0))


def ce_andi(rdp, imm):
    return _pack((4, 15, 13), (2, 11, 10), ((imm >> 5) & 1, 12, 12),
                 (rdp - 8, 9, 7), (imm & 31, 6, 2), (1, 1, 0))


def ce_regop(rdp, rs2p, op):
    f3 = {0: (0, 0x20), 1: (4, 0), 2: (6, 0), 3: (7, 0)}[op]
    return _pack((4, 15, 13), (3, 11, 10), (rdp - 8, 9, 7),
                 ((op, 6, 5)), (rs2p - 8, 4, 2), (1, 1, 0))


def ce_sub(rdp, rs2p): return ce_regop(rdp, rs2p, 0)
def ce_xor(rdp, rs2p): return ce_regop(rdp, rs2p, 1)
def ce_or(rdp, rs2p):  return ce_regop(rdp, rs2p, 2)
def ce_and(rdp, rs2p): return ce_regop(rdp, rs2p, 3)


def ce_j(off):
    off &= 0xFFF
    return _pack((5, 15, 13), (_fld(off, 11, 11), 12, 12),
                 (_fld(off, 4, 4), 11, 11), (_fld(off, 9, 8), 10, 9),
                 (_fld(off, 10, 10), 8, 8), (_fld(off, 6, 6), 7, 7),
                 (_fld(off, 7, 7), 6, 6), (_fld(off, 3, 1), 5, 3),
                 (_fld(off, 5, 5), 2, 2), (1, 1, 0))


def ce_jal(off):
    return (ce_j(off) & ~(7 << 13)) | (1 << 13)


def ce_branch(rs1p, off, beqz):
    off &= 0x1FF
    return _pack((6 if beqz else 7, 15, 13), (_fld(off, 8, 8), 12, 12),
                 (_fld(off, 4, 3), 11, 10), (rs1p - 8, 9, 7),
                 (_fld(off, 7, 6), 6, 5), (_fld(off, 2, 1), 4, 3),
                 (_fld(off, 5, 5), 2, 2), (1, 1, 0))


def ce_beqz(rs1p, off): return ce_branch(rs1p, off, True)
def ce_bnez(rs1p, off): return ce_branch(rs1p, off, False)


def ce_mv(rd, rs2):
    return _pack((4, 15, 13), (0, 12, 12), (rd, 11, 7), (rs2, 6, 2), (2, 1, 0))


def ce_add(rd, rs2):
    return _pack((4, 15, 13), (1, 12, 12), (rd, 11, 7), (rs2, 6, 2), (2, 1, 0))


def ce_jr(rs1):
    return _pack((4, 15, 13), (0, 12, 12), (rs1, 11, 7), (2, 1, 0))


def ce_jalr(rs1):
    return _pack((4, 15, 13), (1, 12, 12), (rs1, 11, 7), (2, 1, 0))


CE_NOP = 0x0001
CE_EBREAK = 0x9002


class _Fixup:
    __slots__ = ("kind", "label", "pc", "funct3", "rs1", "rs2", "rd", "auipc_pc")

    def __init__(self, kind, label, pc, **kw):
        self.kind = kind
        self.label = label
        self.pc = pc
        self.funct3 = kw.get("funct3", 0)
        self.rs1 = kw.get("rs1", 0)
        self.rs2 = kw.get("rs2", 0)
        self.rd = kw.get("rd", 0)
        self.auipc_pc = kw.get("auipc_pc", 0)


class _Half:
    """A 16-bit RVC encoding inside Asm.items (a bare int would be ambiguous
    with small 32-bit instruction words such as the NOP 0x00000013)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value & 0xFFFF


class Asm:
    """Tiny assembler supporting labels, forward branch/jump fixups and ``la``."""

    def __init__(self):
        self.items = []
        self.labels = {}
        self.halt_store_pc = None
        self.halt_loop_pc = None

    # -- layout ------------------------------------------------------------
    # Items are 32-bit words (ints), fixups, or 16-bit RVC halfwords (_Half).
    # RVC tests pack mixed widths; everything else uses whole words.
    def _emit_half(self, half: int):
        self.items.append(_Half(half))

    def here(self) -> int:
        return sum(2 if isinstance(item, _Half) else 4
                   for item in self.items)

    def org(self, pc: int):
        """Start assembling at ``pc`` (used for straight-line blocks that are
        built separately and then copied into a larger image)."""
        expect(pc % 4 == 0 and pc >= 0, f"bad org {pc}")
        expect(pc >= self.here(), "org moves backwards")
        while self.here() < pc:
            self.emit(0)

    def label(self, name: str):
        expect(name not in self.labels, f"duplicate label {name}")
        self.labels[name] = self.here()

    def emit(self, word: int):
        # A 32-bit instruction always starts word aligned.  Pad a dangling
        # lane with C.NOP (0x0001), which is legal if the previous compressed
        # instruction falls through into it.
        while self.here() % 4:
            self.items.append(_Half(0x0001))
        self.items.append(word & MASK32)

    # -- generic encoders --------------------------------------------------
    def _r(self, rd, rs1, rs2, funct3, funct7=0):
        self.emit(enc_r(0x33, rd, funct3, rs1, rs2, funct7))

    def _i(self, rd, rs1, imm, funct3, opcode=0x13):
        self.emit(enc_i(opcode, rd, funct3, rs1, imm))

    def _s(self, rs2, rs1, imm, funct3):
        self.emit(enc_s(0x23, funct3, rs1, rs2, imm))

    def _b(self, label, rs1, rs2, funct3):
        self.items.append(_Fixup("b", label, self.here(), funct3=funct3,
                                 rs1=rs1, rs2=rs2))

    # -- ALU ---------------------------------------------------------------
    def add(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 0)
    def sub(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 0, 0x20)
    def sll(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 1)
    def slt(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 2)
    def sltu(self, rd, rs1, rs2): self._r(rd, rs1, rs2, 3)
    def xor(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 4)
    def srl(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 5)
    def sra(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 5, 0x20)
    def or_(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 6)
    def and_(self, rd, rs1, rs2): self._r(rd, rs1, rs2, 7)

    def mul(self, rd, rs1, rs2):    self._r(rd, rs1, rs2, 0, 0x01)
    def mulh(self, rd, rs1, rs2):   self._r(rd, rs1, rs2, 1, 0x01)
    def mulhsu(self, rd, rs1, rs2): self._r(rd, rs1, rs2, 2, 0x01)
    def mulhu(self, rd, rs1, rs2):  self._r(rd, rs1, rs2, 3, 0x01)
    def div(self, rd, rs1, rs2):    self._r(rd, rs1, rs2, 4, 0x01)
    def divu(self, rd, rs1, rs2):   self._r(rd, rs1, rs2, 5, 0x01)
    def rem(self, rd, rs1, rs2):    self._r(rd, rs1, rs2, 6, 0x01)
    def remu(self, rd, rs1, rs2):   self._r(rd, rs1, rs2, 7, 0x01)

    def addi(self, rd, rs1, imm):  self._i(rd, rs1, imm, 0)
    def slti(self, rd, rs1, imm):  self._i(rd, rs1, imm, 2)
    def sltiu(self, rd, rs1, imm): self._i(rd, rs1, imm, 3)
    def xori(self, rd, rs1, imm):  self._i(rd, rs1, imm, 4)
    def ori(self, rd, rs1, imm):   self._i(rd, rs1, imm, 6)
    def andi(self, rd, rs1, imm):  self._i(rd, rs1, imm, 7)
    def slli(self, rd, rs1, sh):   self._i(rd, rs1, sh & 0x1F, 1)
    def srli(self, rd, rs1, sh):   self._i(rd, rs1, sh & 0x1F, 5)
    def srai(self, rd, rs1, sh):   self._i(rd, rs1, (sh & 0x1F) | 0x400, 5)

    # -- upper immediates --------------------------------------------------
    def lui(self, rd, imm20):  self.emit(enc_u(0x37, rd, imm20))
    def auipc(self, rd, imm20): self.emit(enc_u(0x17, rd, imm20))

    # -- RVC ---------------------------------------------------------------
    def cword(self, words):
        """Emit 16-bit compressed encodings back to back."""
        for w in words:
            expect(0 <= w < 0x10000 and (w & 3) != 3,
                   f"not a compressed encoding: {w:#x}")
            self._emit_half(w)

    def raw16(self, half):
        """Emit any 16-bit halfword (including the low half of a 32-bit op)."""
        expect(0 <= half < 0x10000, f"bad halfword {half:#x}")
        self._emit_half(half)

    def raw32_unaligned(self, word):
        """Emit a 32-bit word at the current (possibly 2-mod-4) address, for
        intentional straddle tests.  No alignment fill is inserted."""
        self.items.append(word & MASK32)

    def jal_unaligned(self, rd, label):
        """32-bit JAL fixup placed at the current (possibly odd) address."""
        self.items.append(_Fixup("j", label, self.here(), rd=rd))

    def align(self):
        """Pad one lane with C.NOP so a following 32-bit instruction is word
        aligned.  Only needed on paths where the preceding compressed
        instruction can fall through."""
        if self.here() % 4:
            self._emit_half(0x0001)

    def li(self, rd, value: int):
        value &= MASK32
        if -2048 <= s32(value) <= 2047:
            self.addi(rd, 0, s32(value))
        else:
            hi = ((value + 0x800) >> 12) & 0xFFFFF
            lo = s32(value - (hi << 12))
            self.lui(rd, hi)
            self.addi(rd, rd, lo)

    def la(self, rd, label):
        pc = self.here()
        self.items.append(_Fixup("la_hi", label, pc, rd=rd))
        self.items.append(_Fixup("la_lo", label, self.here(), rd=rd, auipc_pc=pc))

    # -- control flow ------------------------------------------------------
    def jal(self, rd, label):
        self.items.append(_Fixup("j", label, self.here(), rd=rd))

    def j(self, label):
        self.jal(0, label)

    def jalr(self, rd, rs1, imm=0):
        self.emit(enc_i(0x67, rd, 0, rs1, imm))

    def beq(self, rs1, rs2, label):  self._b(label, rs1, rs2, 0)
    def bne(self, rs1, rs2, label):  self._b(label, rs1, rs2, 1)
    def blt(self, rs1, rs2, label):  self._b(label, rs1, rs2, 4)
    def bge(self, rs1, rs2, label):  self._b(label, rs1, rs2, 5)
    def bltu(self, rs1, rs2, label): self._b(label, rs1, rs2, 6)
    def bgeu(self, rs1, rs2, label): self._b(label, rs1, rs2, 7)

    # -- memory ------------------------------------------------------------
    def lb(self, rd, rs1, imm):  self._i(rd, rs1, imm, 0, 0x03)
    def lh(self, rd, rs1, imm):  self._i(rd, rs1, imm, 1, 0x03)
    def lw(self, rd, rs1, imm):  self._i(rd, rs1, imm, 2, 0x03)
    def lbu(self, rd, rs1, imm): self._i(rd, rs1, imm, 4, 0x03)
    def lhu(self, rd, rs1, imm): self._i(rd, rs1, imm, 5, 0x03)
    def sb(self, rs2, rs1, imm): self._s(rs2, rs1, imm, 0)
    def sh(self, rs2, rs1, imm): self._s(rs2, rs1, imm, 1)
    def sw(self, rs2, rs1, imm): self._s(rs2, rs1, imm, 2)

    # -- system ------------------------------------------------------------
    def csrrw(self, rd, csr, rs1):  self.emit(enc_i(0x73, rd, 1, rs1, csr))
    def csrrs(self, rd, csr, rs1):  self.emit(enc_i(0x73, rd, 2, rs1, csr))
    def csrrc(self, rd, csr, rs1):  self.emit(enc_i(0x73, rd, 3, rs1, csr))
    def csrrwi(self, rd, csr, uimm):  self.emit(enc_i(0x73, rd, 5, uimm, csr))
    def csrrsi(self, rd, csr, uimm):  self.emit(enc_i(0x73, rd, 6, uimm, csr))
    def csrrci(self, rd, csr, uimm):  self.emit(enc_i(0x73, rd, 7, uimm, csr))

    def csrr(self, rd, csr):  self.csrrs(rd, csr, 0)
    def csrw(self, csr, rs1): self.csrrw(0, csr, rs1)
    def csrs(self, csr, rs1): self.csrrs(0, csr, rs1)
    def csrc(self, csr, rs1): self.csrrc(0, csr, rs1)
    def csrwi(self, csr, uimm): self.csrrwi(0, csr, uimm)
    def csrsi(self, csr, uimm): self.csrrsi(0, csr, uimm)
    def csrci(self, csr, uimm): self.csrrci(0, csr, uimm)

    def ecall(self):  self.emit(0x0000_0073)
    def ebreak(self): self.emit(0x0010_0073)
    def mret(self):   self.emit(0x3020_0073)
    def wfi(self):    self.emit(0x1050_0073)
    def fence(self):  self.emit(enc_i(0x0F, 0, 0, 0, 0))
    def fence_i(self): self.emit(enc_i(0x0F, 0, 1, 0, 0))
    def nop(self):    self.addi(0, 0, 0)

    def raw(self, word: int):
        self.emit(word)

    def self_loop(self):
        self.emit(enc_j(0x6F, 0, 0))  # jal x0, 0

    def halt(self, addr_reg=31, magic_reg=30):
        """Store the halt magic to the mailbox and spin.  Clobbers two regs."""
        self.li(addr_reg, HALT_ADDR)
        self.li(magic_reg, HALT_MAGIC)
        self.halt_store_pc = self.here()
        self.sw(magic_reg, addr_reg, 0)
        self.halt_loop_pc = self.here()
        self.self_loop()

    # -- resolution --------------------------------------------------------
    def assemble(self) -> list[int]:
        return pack_words(self._resolve())

    def _resolve(self) -> list:
        out = []
        # A 32-bit plain word may coincide with a small numeric label name
        # ("276"), so only fixup objects are resolved against the label map.
        for idx, item in enumerate(self.items):
            if isinstance(item, (_Half, int)):
                out.append(item)
                continue
            expect(not isinstance(item, int), "int item")
            expect(item.label in self.labels, f"undefined label {item.label}")
            target = self.labels[item.label]
            if item.kind == "b":
                off = target - item.pc
                expect(-4096 <= off <= 4094 and off % 2 == 0,
                       f"branch offset out of range: {off}")
                out.append(enc_b(0x63, item.funct3, item.rs1, item.rs2, off))
            elif item.kind == "j":
                off = target - item.pc
                expect(-(1 << 20) <= off <= (1 << 20) - 2 and off % 2 == 0,
                       f"jal offset out of range: {off}")
                out.append(enc_j(0x6F, item.rd, off))
            elif item.kind == "la_hi":
                off = target - item.pc
                expect(-(1 << 31) <= off < (1 << 31), "la offset out of range")
                hi = ((off + 0x800) >> 12) & 0xFFFFF
                out.append(enc_u(0x17, item.rd, hi))
            elif item.kind == "la_lo":
                off = target - item.auipc_pc
                hi = ((off + 0x800) >> 12) & 0xFFFFF
                lo = off - (hi << 12)
                expect(-2048 <= lo <= 2047, f"la lo out of range: {lo}")
                out.append(enc_i(0x13, item.rd, 0, item.rd, lo))
            else:  # pragma: no cover
                raise AssertionError(f"unknown fixup {item.kind}")
        return out


def pack_words(halfwords_and_words):
    """Pack a mixed list of _Half and 32-bit words into ITCM words in address
    order (little-endian lanes)."""
    data = bytearray()
    for item in halfwords_and_words:
        if isinstance(item, _Half):
            data += item.value.to_bytes(2, "little")
        else:
            data += int(item).to_bytes(4, "little")
    if len(data) % 4:
        # A trailing low halfword leaves its lane 0 unassigned.  Fill it with
        # 0x0013 (the high half of a NOP) so that executing it is a NOP; a
        # zero word would be an illegal instruction there.
        data += b"\x37\x00" if len(data) % 4 == 2 else b"\x37"
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]


# ---------------------------------------------------------------------------
# reference memory / interpreter
# ---------------------------------------------------------------------------
class RefMemory:
    """Byte-addressed memory model.

    ITCM 16 KiB at 0, DTCM 4 KiB at 0x1000_0000 and an optional 4 KiB window
    backing the fake bus slave (tag 0x2, base ``BUS_BASE``).  Tests that drive
    ``BusSlave`` copy the slave's initial bytes into ``mem.bus`` so the
    interpreter and the RTL see identical external memory.
    """

    def __init__(self, itcm_words=None, dtcm_words=None):
        self.itcm = bytearray(ITCM_BYTES)
        self.dtcm = bytearray(DTCM_BYTES)
        self.bus = bytearray(BUS_BYTES)
        if itcm_words is not None:
            self.load_itcm_words(itcm_words)
        if dtcm_words is not None:
            self.load_dtcm_words(dtcm_words)

    def load_bus_bytes(self, data):
        expect(len(data) <= BUS_BYTES, "bus image too large")
        self.bus[:len(data)] = data

    def load_itcm_words(self, words):
        for i, w in enumerate(words):
            self.itcm[4 * i:4 * i + 4] = u32(w).to_bytes(4, "little")

    def load_dtcm_words(self, words):
        expect(len(words) <= DTCM_WORDS, "too many DTCM words")
        for i, w in enumerate(words):
            self.dtcm[4 * i:4 * i + 4] = u32(w).to_bytes(4, "little")

    def _region(self, addr):
        tag = (addr >> 28) & 0xF
        if tag == 0x0:
            return self.itcm, addr & (ITCM_BYTES - 1)
        if tag == 0x1:
            return self.dtcm, addr & (DTCM_BYTES - 1)
        if tag == 0x2:
            return self.bus, addr & (BUS_BYTES - 1)
        raise AssertionError(f"unmapped address in reference model: 0x{addr:08x}")

    def read_byte(self, addr):
        mem, off = self._region(addr)
        return mem[off]

    def write_byte(self, addr, value):
        mem, off = self._region(addr)
        mem[off] = value & 0xFF

    def load(self, addr, size, signed):
        raw = 0
        for i in range(size):
            raw |= self.read_byte(addr + i) << (8 * i)
        return sext(raw, size * 8) & MASK32 if signed else raw & MASK32

    def store(self, addr, size, value):
        for i in range(size):
            self.write_byte(addr + i, (value >> (8 * i)) & 0xFF)

    def itcm_word(self, idx):
        return int.from_bytes(self.itcm[4 * idx:4 * idx + 4], "little")

    def dtcm_word(self, idx):
        return int.from_bytes(self.dtcm[4 * idx:4 * idx + 4], "little")

    def itcm_words(self):
        return [self.itcm_word(i) for i in range(ITCM_WORDS)]

    def dtcm_words(self):
        return [self.dtcm_word(i) for i in range(DTCM_WORDS)]


class ExecResult:
    def __init__(self, regs, trace, mem, steps, halted):
        self.regs = regs
        self.trace = trace
        self.mem = mem
        self.steps = steps
        self.halted = halted


class RV32Interp:
    """Independent instruction-by-instruction RV32IM reference model.

    Only TCM memory and the I/M base ISA are modeled.  CSR/system instructions
    raise so tests that need them use directed expectations instead.
    """

    def __init__(self, program_words, dtcm_words=None, regs=None):
        self.mem = RefMemory(program_words, dtcm_words)
        self.regs = [0] * 32
        if regs:
            for i, v in regs.items():
                self.regs[i] = u32(v)
        self.pc = 0
        self.trace = []
        self.lr_resv = None       # RV32A reservation (word address)

    def _write_rd(self, rd, value):
        if rd != 0:
            self.regs[rd] = u32(value)

    def step(self):
        pc = self.pc
        raw = self.mem.load(pc, 2, signed=False) & 0xFFFF
        if (raw & 3) != 3:
            # IALIGN=16: expand RVC to canonical 32-bit IR (mirror of IF)
            expanded = rvc_ref.rvc_expand(raw)
            if expanded == (0x10000 | raw):
                raise AssertionError(
                    f"illegal compressed 0x{raw:04x} at 0x{pc:08x}")
            ir = expanded & MASK32
            length = 2
        else:
            ir = self.mem.load(pc, 4, signed=False)
            length = 4
        self.trace.append(pc)
        opcode = ir & 0x7F
        rd = (ir >> 7) & 0x1F
        funct3 = (ir >> 12) & 0x7
        rs1 = (ir >> 15) & 0x1F
        rs2 = (ir >> 20) & 0x1F
        funct7 = (ir >> 25) & 0x7F
        a = self.regs[rs1]
        b = self.regs[rs2]
        next_pc = u32(pc + length)
        halted_store = False

        if opcode == 0x37:      # LUI
            self._write_rd(rd, ir & 0xFFFF_F000)
        elif opcode == 0x17:    # AUIPC
            self._write_rd(rd, pc + (ir & 0xFFFF_F000))
        elif opcode == 0x6F:    # JAL
            imm = sext(((ir >> 31) << 20) | (((ir >> 12) & 0xFF) << 12) |
                       (((ir >> 20) & 1) << 11) | (((ir >> 21) & 0x3FF) << 1), 21)
            self._write_rd(rd, pc + length)
            next_pc = u32(pc + imm)
        elif opcode == 0x67:    # JALR
            imm = sext(ir >> 20, 12)
            target = u32(a + imm) & ~1
            self._write_rd(rd, pc + length)
            next_pc = target
        elif opcode == 0x63:    # BRANCH
            imm = sext(((ir >> 31) << 12) | (((ir >> 7) & 1) << 11) |
                       (((ir >> 25) & 0x3F) << 5) | (((ir >> 8) & 0xF) << 1), 13)
            if funct3 == 0:
                taken = a == b
            elif funct3 == 1:
                taken = a != b
            elif funct3 == 4:
                taken = s32(a) < s32(b)
            elif funct3 == 5:
                taken = s32(a) >= s32(b)
            elif funct3 == 6:
                taken = a < b
            elif funct3 == 7:
                taken = a >= b
            else:
                raise AssertionError(f"bad branch funct3 {funct3}")
            if taken:
                next_pc = u32(pc + imm)
        elif opcode == 0x03:    # LOAD
            imm = sext(ir >> 20, 12)
            addr = u32(a + imm)
            if funct3 == 0:
                value = self.mem.load(addr, 1, True)
            elif funct3 == 1:
                value = self.mem.load(addr, 2, True)
            elif funct3 == 2:
                value = self.mem.load(addr, 4, False)
            elif funct3 == 4:
                value = self.mem.load(addr, 1, False)
            elif funct3 == 5:
                value = self.mem.load(addr, 2, False)
            else:
                raise AssertionError(f"bad load funct3 {funct3}")
            self._write_rd(rd, value)
        elif opcode == 0x23:    # STORE
            imm = sext(((ir >> 25) & 0x7F) << 5 | ((ir >> 7) & 0x1F), 12)
            addr = u32(a + imm)
            if funct3 == 0:
                self.mem.store(addr, 1, b)
            elif funct3 == 1:
                self.mem.store(addr, 2, b)
            elif funct3 == 2:
                self.mem.store(addr, 4, b)
            else:
                raise AssertionError(f"bad store funct3 {funct3}")
            if self.lr_resv is not None and u32(addr) & ~3 == self.lr_resv:
                self.lr_resv = None
            if addr == HALT_ADDR and b == HALT_MAGIC:
                halted_store = True
        elif opcode == 0x13:    # OP-IMM
            imm = sext(ir >> 20, 12)
            if funct3 == 0:
                value = a + imm
            elif funct3 == 2:
                value = 1 if s32(a) < s32(imm) else 0
            elif funct3 == 3:
                value = 1 if a < u32(imm) else 0
            elif funct3 == 4:
                value = a ^ u32(imm)
            elif funct3 == 6:
                value = a | u32(imm)
            elif funct3 == 7:
                value = a & u32(imm)
            elif funct3 == 1:
                value = a << (imm & 0x1F)
            elif funct3 == 5:
                sh = imm & 0x1F
                if imm & 0x400:
                    value = s32(a) >> sh
                else:
                    value = a >> sh
            else:
                raise AssertionError(f"bad op-imm funct3 {funct3}")
            self._write_rd(rd, value)
        elif opcode == 0x33:    # OP (base + M)
            if funct7 == 0x01:
                su_a = s32(a)
                su_b = s32(b)
                if funct3 == 0:
                    value = a * b
                elif funct3 == 1:
                    value = (su_a * su_b) >> 32
                elif funct3 == 2:
                    value = (su_a * b) >> 32
                elif funct3 == 3:
                    value = (a * b) >> 32
                elif funct3 == 4:
                    if b == 0:
                        value = MASK32
                    elif su_a == -(1 << 31) and su_b == -1:
                        value = 0x8000_0000
                    else:
                        value = int(su_a / su_b) & MASK32
                elif funct3 == 5:
                    value = MASK32 if b == 0 else (a // b)
                elif funct3 == 6:
                    if b == 0:
                        value = a
                    elif su_a == -(1 << 31) and su_b == -1:
                        value = 0
                    else:
                        q = int(su_a / su_b)
                        value = su_a - q * su_b
                elif funct3 == 7:
                    value = a if b == 0 else (a % b)
                else:
                    raise AssertionError(f"bad M funct3 {funct3}")
            else:
                if funct3 == 0:
                    value = a - b if funct7 == 0x20 else a + b
                elif funct3 == 1:
                    value = a << (b & 0x1F)
                elif funct3 == 2:
                    value = 1 if s32(a) < s32(b) else 0
                elif funct3 == 3:
                    value = 1 if a < b else 0
                elif funct3 == 4:
                    value = a ^ b
                elif funct3 == 5:
                    value = (s32(a) >> (b & 0x1F)) if funct7 == 0x20 else (a >> (b & 0x1F))
                elif funct3 == 6:
                    value = a | b
                elif funct3 == 7:
                    value = a & b
                else:
                    raise AssertionError(f"bad OP funct3 {funct3}")
            self._write_rd(rd, value)
        elif opcode == 0x2F:    # AMO (RV32A, .W)
            if funct3 != 2:
                raise AssertionError(f"bad AMO funct3 {funct3}")
            funct5 = (ir >> 27) & 0x1F
            addr = u32(a)
            if addr % 4:
                raise AssertionError(
                    "misaligned AMO (the RTL traps); directed trap tests use "
                    "hand-built expectations instead")
            old = self.mem.load(addr, 4, signed=False)
            if funct5 == 0b00010:           # LR.W
                if rs2 != 0:
                    raise AssertionError("LR.W with rs2 != 0 (the RTL traps)")
                value = old
                self.lr_resv = addr
            elif funct5 == 0b00011:         # SC.W
                ok = self.lr_resv == addr
                if ok:
                    self.mem.store(addr, 4, b)
                    self.lr_resv = None
                    if addr == HALT_ADDR and b == HALT_MAGIC:
                        halted_store = True
                value = 0 if ok else 1
            else:
                new = {
                    0b00000: (old + b) & MASK32,
                    0b00001: b,
                    0b00100: old ^ b,
                    0b01000: old | b,
                    0b01100: old & b,
                    0b10000: old if s32(old) < s32(b) else b,
                    0b10100: old if s32(old) > s32(b) else b,
                    0b11000: old if old < b else b,
                    0b11100: old if old > b else b,
                }.get(funct5)
                if new is None:
                    raise AssertionError(f"unsupported AMO funct5 {funct5:#07b}")
                self.mem.store(addr, 4, new)
                self.lr_resv = None if self.lr_resv == addr else self.lr_resv
                value = old
            self._write_rd(rd, value)
        elif opcode == 0x0F:    # MISC-MEM (fence / fence.i / wfi)
            pass
        else:
            raise AssertionError(f"unsupported opcode 0x{opcode:02x} at 0x{pc:08x}")

        self.pc = next_pc
        return halted_store

    def run(self, max_steps=200_000):
        steps = 0
        halted = False
        while steps < max_steps:
            halted = self.step()
            steps += 1
            if halted:
                break
        expect(halted, f"reference model did not reach halt in {steps} steps")
        return ExecResult(list(self.regs), list(self.trace), self.mem, steps, halted)

    def exec(self, n, regs=None):
        """Execute exactly ``n`` steps (no halt requirement) and return a result.

        ``regs`` optionally seeds the architectural state before the first step
        (used to build straight-line blocks whose PC-dependent instructions
        were resolved against their real link address).
        """
        for i, v in (regs or {}).items():
            self.regs[i] = u32(v)
        self.trace = []
        for _ in range(n):
            self.step()
        return ExecResult(list(self.regs), list(self.trace), self.mem, n, False)


# ---------------------------------------------------------------------------
# DUT harness
# ---------------------------------------------------------------------------
class CoreHarness:
    """Convenience wrapper over the ``tb_core_cocotb`` hierarchy."""

    def __init__(self, dut, period_ns=10, halt_index=HALT_ADDR // 4):
        self.dut = dut
        self.core = dut.dut
        self.clk = dut.clk
        self.period_ns = period_ns
        self.halt_index = halt_index
        self.cycle = 0
        self._clock_started = False

    # -- basic driving -----------------------------------------------------
    def start_clock(self):
        if not self._clock_started:
            Clock(self.clk, self.period_ns, unit="ns").start()
            self._clock_started = True

    def drive_idle_inputs(self, rstn=0):
        self.dut.rstn.value = rstn
        self.dut.bus_ready.value = 1
        self.dut.bus_rdata.value = 0
        self.dut.timer_irq.value = 0
        self.dut.ext_irq.value = 0
        self.dut.soft_irq.value = 0

    def set_irqs(self, timer=0, ext=0, soft=0):
        self.dut.timer_irq.value = int(timer)
        self.dut.ext_irq.value = int(ext)
        self.dut.soft_irq.value = int(soft)

    async def step(self, n=1):
        for _ in range(n):
            await RisingEdge(self.clk)
            # one timestep past the edge: all non-blocking updates in the
            # design have settled, and we stay in the read-write phase so
            # callers may drive inputs or read outputs freely.
            await Timer(1, unit="ps")
            self.cycle += 1

    async def reset(self, cycles=8):
        self.start_clock()
        self.drive_idle_inputs(rstn=0)
        await self.step(cycles)
        self.dut.rstn.value = 1
        await self.step(1)

    # -- hierarchical reads ------------------------------------------------
    def sig(self, name):
        return handle_int(getattr(self.core, name))

    @property
    def IF_PC(self): return self.sig("IF_PC")

    @property
    def IF_PC_q(self): return self.sig("IF_PC_q")

    @property
    def ID_PC(self): return self.sig("ID_PC")

    @property
    def ID_IR(self): return self.sig("ID_IR")

    @property
    def ID_valid(self): return self.sig("ID_valid")

    @property
    def EX_PC(self): return self.sig("EX_PC")

    @property
    def EX_IR(self): return self.sig("EX_IR")

    @property
    def EX_valid(self): return self.sig("EX_valid")

    @property
    def MEM_PC(self): return self.sig("MEM_PC")

    @property
    def MEM_addr(self): return self.sig("MEM_addr")

    @property
    def MEM_wdata(self): return self.sig("MEM_wdata")

    @property
    def MEM_wstrb(self): return self.sig("MEM_wstrb")

    @property
    def MEM_valid(self): return self.sig("MEM_valid")

    @property
    def WB_PC(self): return self.sig("WB_PC")

    @property
    def WB_IR(self): return self.sig("WB_IR")

    @property
    def WB_rd(self): return self.sig("WB_rd")

    @property
    def WB_valid(self): return self.sig("WB_valid")

    @property
    def WB_rd_write_req(self): return self.sig("WB_rd_write_req")

    @property
    def raw_stall(self): return self.sig("raw_stall")

    @property
    def pipe_freeze(self): return self.sig("pipe_freeze")

    @property
    def bus_wait(self): return self.sig("bus_wait")

    @property
    def bus_conflict(self): return self.sig("bus_conflict")

    @property
    def trap_taken(self): return self.sig("trap_taken")

    @property
    def csr_we(self): return self.sig("csr_we")

    @property
    def mem_dtcm_we(self): return self.sig("mem_dtcm_we")

    @property
    def mem_itcm_we(self): return self.sig("mem_itcm_we")

    @property
    def div_state(self): return self.sig("div_state")

    @property
    def div_busy(self): return self.sig("div_busy")

    @property
    def IF_ID_hold(self): return self.sig("IF_ID_hold")

    # -- branch prediction observability (rtl/core.v ``Branch prediction``) --
    # Read-only taps into the BTB and the IF/ID/EX prediction plumbing.
    # ``btb_*``, ``EX_pred_*`` and ``ex_pred_mispredict``/``btb_we``/``redirect``
    # are combinational; ``IF_pred_*_q`` are registered with ``if_rdata_q``.
    # BTB geometry mirrors the RTL localparam BTB_IBITS=6 (64 entries,
    # index PC[6:1], tag PC[31:7] because IALIGN=16).
    BTB_IBITS = 6
    BTB_NUM = 1 << BTB_IBITS

    @staticmethod
    def btb_index(pc):
        return (pc >> 1) & ((1 << CoreHarness.BTB_IBITS) - 1)

    @staticmethod
    def btb_tag_of(pc):
        return (pc & MASK32) >> (CoreHarness.BTB_IBITS + 1)

    def btb_entry(self, idx):
        """(valid, tag, target, cnt) of BTB entry ``idx``; entries are raw."""
        return (handle_int(self.core.btb_valid[idx]),
                handle_int(self.core.btb_tag[idx]),
                handle_int(self.core.btb_target[idx]),
                handle_int(self.core.btb_cnt[idx]))

    def btb_find(self, pc):
        """(hit, target, cnt) as seen by the IF lookup of ``pc``.

        ``hit`` is False when the entry is invalid or its tag differs;
        ``cnt``/``target`` are None only on X/Z.
        """
        pc &= MASK32
        idx = self.btb_index(pc)
        valid, tag, target, cnt = self.btb_entry(idx)
        hit = bool(valid) and tag == self.btb_tag_of(pc)
        return (hit, target, cnt)

    @property
    def btb_valid(self): return handle_int(self.core.btb_valid)

    @property
    def btb_pred_taken(self): return self.sig("btb_pred_taken")

    @property
    def btb_pred_target(self): return self.sig("btb_pred_target")

    # RVC front-end and RV32A taps.
    def if_straddle_pending(self):
        # The two-word IF window means there is no straddle-assembly state;
        # kept as a 0-valued tap for older tests/diagnostics.
        return 0
    def if_insn_pc(self): return self.sig("if_insn_pc")
    def if_insn_ir(self): return self.sig("if_insn_ir")
    def if_insn_compressed(self): return self.sig("if_insn_compressed")
    def id_is_compressed(self): return self.sig("ID_is_compressed")
    def ex_is_compressed(self): return self.sig("EX_is_compressed")
    def mem_amo_active(self): return self.sig("mem_amo_active")
    def mem_amo_we(self): return self.sig("mem_amo_we")
    def lr_resv_valid(self): return self.sig("lr_resv_valid")
    def lr_resv_addr(self): return self.sig("lr_resv_addr")
    def mem_amo_phase(self): return self.sig("MEM_amo_phase")

    @property
    def IF_pred_taken_q(self): return self.sig("IF_pred_taken_q")

    @property
    def IF_pred_target_q(self): return self.sig("IF_pred_target_q")

    @property
    def ID_pred_taken(self): return self.sig("ID_pred_taken")

    @property
    def ID_pred_target(self): return self.sig("ID_pred_target")

    @property
    def EX_pred_taken(self): return self.sig("EX_pred_taken")

    @property
    def EX_pred_target(self): return self.sig("EX_pred_target")

    @property
    def ex_pred_mispredict(self): return self.sig("ex_pred_mispredict")

    @property
    def ex_ctrl_actual(self): return self.sig("ex_ctrl_actual")

    @property
    def ex_target(self): return self.sig("ex_target")

    @property
    def ex_exception(self): return self.sig("ex_exception")

    @property
    def btb_we(self): return self.sig("btb_we")

    @property
    def redirect(self): return self.sig("redirect")

    @property
    def redirect_pc(self): return self.sig("redirect_pc")

    @property
    def id_flush_q(self): return self.sig("id_flush_q")

    # EX/MEM->ID forwarding observability (rtl/core.v ``ex_fwd_ok`` /
    # ``mem_fwd_ok``).  Read-only taps; ``sig`` returns None on X/Z.
    @property
    def EX_rd(self): return self.sig("EX_rd")

    @property
    def EX_rd_write_req(self): return self.sig("EX_rd_write_req")

    @property
    def EX_mem_read(self): return self.sig("EX_mem_read")

    @property
    def ex_result(self): return self.sig("ex_result")

    @property
    def ex_fwd_ok(self): return self.sig("ex_fwd_ok")

    @property
    def MEM_rd(self): return self.sig("MEM_rd")

    @property
    def MEM_rd_write_req(self): return self.sig("MEM_rd_write_req")

    @property
    def MEM_mem_read(self): return self.sig("MEM_mem_read")

    @property
    def MEM_result(self): return self.sig("MEM_result")

    @property
    def mem_fwd_ok(self): return self.sig("mem_fwd_ok")

    @property
    def id_rs1_data(self): return self.sig("id_rs1_data")

    @property
    def id_rs2_data(self): return self.sig("id_rs2_data")

    @property
    def mstatus(self): return self.sig("mstatus")

    @property
    def mie(self): return self.sig("mie")

    @property
    def mtvec(self): return self.sig("mtvec")

    @property
    def mscratch(self): return self.sig("mscratch")

    @property
    def mepc(self): return self.sig("mepc")

    @property
    def mcause(self): return self.sig("mcause")

    @property
    def mtval(self): return self.sig("mtval")

    @property
    def cycle_count(self): return handle_int(self.core.cycle_cnt)

    @property
    def instret_count(self): return handle_int(self.core.instret_cnt)

    def reg(self, idx):
        return handle_int(self.core.regfiles[idx])

    def regs(self):
        return [handle_int(self.core.regfiles[i]) for i in range(32)]

    def itcm(self, idx):
        return handle_int(self.core.ITCM[idx])

    def dtcm(self, idx):
        return handle_int(self.core.DTCM[idx])

    def itcm_words(self, start=0, count=ITCM_WORDS):
        return [self.itcm(start + i) for i in range(count)]

    def dtcm_words(self, start=0, count=DTCM_WORDS):
        return [self.dtcm(start + i) for i in range(count)]

    # -- memory initialisation --------------------------------------------
    def load_program(self, words, zero_rest=False):
        """Load instruction words starting at ITCM word 0."""
        expect(len(words) <= ITCM_WORDS, "program too large for ITCM")
        for i, w in enumerate(words):
            self.core.ITCM[i].value = u32(w)
        if zero_rest:
            for i in range(len(words), ITCM_WORDS):
                self.core.ITCM[i].value = 0

    def write_itcm_words(self, start_idx, words):
        for i, w in enumerate(words):
            self.core.ITCM[start_idx + i].value = u32(w)

    def write_dtcm_words(self, start_idx, words):
        expect(start_idx + len(words) <= DTCM_WORDS, "DTCM image too large")
        for i, w in enumerate(words):
            self.core.DTCM[start_idx + i].value = u32(w)

    def fill_itcm_bytes(self, base_addr, data: bytes):
        """Byte-granular ITCM initialisation (base must be word aligned)."""
        expect(base_addr % 4 == 0, "ITCM byte fill must be word aligned")
        for i in range(0, len(data), 4):
            chunk = data[i:i + 4].ljust(4, b"\x00")
            self.core.ITCM[base_addr // 4 + i // 4].value = int.from_bytes(chunk, "little")

    def fill_dtcm_bytes(self, base_addr, data: bytes):
        expect(base_addr % 4 == 0, "DTCM byte fill must be word aligned")
        for i in range(0, len(data), 4):
            chunk = data[i:i + 4].ljust(4, b"\x00")
            self.core.DTCM[base_addr // 4 + i // 4].value = int.from_bytes(chunk, "little")

    # -- execution helpers -------------------------------------------------
    async def run_until_halt(self, max_cycles=200_000, halt_pc=None):
        """Run until the halt store retires (or, if ``halt_pc`` is None, until
        the halt mailbox holds the magic value).

        Retirement is used instead of the mailbox value because a previous
        test image may already have left the magic word in ITCM; the mailbox
        alone cannot distinguish that from a fresh halt.
        """
        if halt_pc is None:
            for _ in range(max_cycles):
                await self.step()
                if self.itcm(self.halt_index) == HALT_MAGIC:
                    await self.step(2)
                    return self.cycle
        else:
            for _ in range(max_cycles):
                await self.step()
                if self.WB_valid and self.WB_PC == halt_pc:
                    await self.step(2)
                    expect(self.itcm(self.halt_index) == HALT_MAGIC,
                           "halt store retired but mailbox was not written")
                    return self.cycle
        raise AssertionError(
            f"halt not reached in {max_cycles} cycles "
            f"(pc=0x{self.IF_PC:08x}, halt_pc={halt_pc}, "
            f"mailbox={self.itcm(self.halt_index)!r})")

    async def run_until_retire(self, pc, max_cycles=200_000):
        """Run until the instruction at ``pc`` retires (first observation)."""
        for _ in range(max_cycles):
            await self.step()
            if self.WB_valid and self.WB_PC == pc:
                await self.step(2)
                return self.cycle
        raise AssertionError(f"instruction at 0x{pc:08x} never retired")

    async def run_trace_until_pc(self, stop_pc, max_cycles=100_000):
        """Record the retirement trace (see ``run_with_trace``) until the
        instruction at ``stop_pc`` has retired; return the trace, stopping it
        after the first retirement of ``stop_pc`` has been appended."""
        trace = []
        prev_valid = False
        prev_pc = None
        for _ in range(max_cycles):
            await self.step()
            if self.WB_valid:
                repeat = prev_valid and prev_pc == self.WB_PC
                if not repeat:
                    trace.append((self.WB_PC, self.WB_IR))
                prev_valid = True
                prev_pc = self.WB_PC
                if not repeat and self.WB_PC == stop_pc:
                    await self.step(2)
                    return trace
            else:
                prev_valid = False
                prev_pc = None
        raise AssertionError(
            f"0x{stop_pc:x} not retired in {max_cycles} cycles "
            f"(pc=0x{self.IF_PC:08x})")

    async def run_with_trace(self, halt_pc, max_cycles=200_000, stats=None,
                             cycle_hook=None, trap_hook=None):
        """Run until the halt store retires, recording every retired PC.

        ``stats`` optionally maps signal names (``raw_stall``, ``div_busy``,
        ``csr_we``, ...) to counters; every cycle in which the signal is high is
        counted.  The key ``"wb_repeat"`` counts cycles in which WB holds the
        same PC as the previous cycle (pipeline freeze).

        ``cycle_hook(harness)`` is called after every sampled cycle (used to
        raise interrupt levels when a condition such as ``div_busy`` is
        observed).  ``trap_hook()`` is called once the sampled ``trap_taken``
        has actually committed: ``trap_taken`` is combinational and the trap
        state (mepc/redirect) is written at the *next* posedge, so the hook
        fires one cycle after the first cycle in which ``trap_taken`` was
        high.  Dropping a level-sensitive IRQ source in the very cycle where
        ``trap_taken`` is first observed would clear ``irq_pending`` before
        that committing edge and the trap would never be taken.

        Records the ``(WB_PC, WB_IR)`` pair of each cycle in which a valid
        instruction is in WB.  ``MEM_valid`` repeats a PC across stall/freeze
        cycles, but ``WB_valid`` is de-asserted on a bubble.

        Duplicates are filtered: rtl/core.v:927 holds WB across
        ``pipe_freeze`` (divider busy / bus wait).  When a ``<=`` store to the
        halt mailbox executes in EX, the write commits at the same posedge
        MEM->WB happens, and the following instruction may then freeze in EX
        (e.g. a div whose operand comes from the just-stored location).  WB
        then holds the store and ``WB_valid`` stays high for several cycles
        for one instruction.  Sampling is done after the edge, so the first
        cycle in which a PC appears is the true retirement.
        """
        trace = []
        prev_valid = False
        prev_pc = None
        trap_pending = False
        for _ in range(max_cycles):
            await self.step()
            for name in stats or ():
                if name != "wb_repeat" and self.sig(name):
                    stats[name] += 1
            # Trap state commits at the posedge that ends the cycle in which
            # ``trap_taken`` was sampled high; fire the hook after that edge.
            if trap_hook is not None and trap_pending:
                trap_hook()
            trap_pending = bool(self.trap_taken)
            if cycle_hook is not None:
                cycle_hook(self)
            if self.WB_valid:
                if prev_valid and prev_pc == self.WB_PC and stats is not None:
                    stats["wb_repeat"] = stats.get("wb_repeat", 0) + 1
                if not (prev_valid and prev_pc == self.WB_PC):
                    trace.append((self.WB_PC, self.WB_IR))
                prev_valid = True
                prev_pc = self.WB_PC
            else:
                prev_valid = False
                prev_pc = None
            if self.WB_valid and self.WB_PC == halt_pc:
                await self.step(2)
                return trace
        raise AssertionError(
            f"halt (pc=0x{halt_pc:x}) not retired in {max_cycles} cycles "
            f"(pc=0x{self.IF_PC:08x})")

    # -- comparisons -------------------------------------------------------
    def compare_regs(self, expected, ignore=(0,), label="regfile"):
        """Compare ``self.regs()`` against a 32-entry list or dict."""
        problems = []
        for idx in range(32):
            if idx in ignore:
                continue
            exp = expected[idx] if isinstance(expected, (list, tuple)) else expected.get(idx, 0)
            got = self.reg(idx)
            if got != u32(exp):
                problems.append(f"x{idx}: got {fmt(got)} exp 0x{u32(exp):08x}")
        expect(not problems, f"{label} mismatch:\n  " + "\n  ".join(problems[:24]))

    def compare_itcm_words(self, expected_words, label="ITCM"):
        problems = []
        for idx, exp in enumerate(expected_words):
            got = self.itcm(idx)
            if exp is None:
                continue
            if got != u32(exp):
                problems.append(f"ITCM[{idx}] (0x{idx * 4:05x}): got {fmt(got)} "
                                f"exp 0x{u32(exp):08x}")
        expect(not problems, f"{label} mismatch:\n  " + "\n  ".join(problems[:24]))

    def compare_dtcm_words(self, expected_words, label="DTCM"):
        problems = []
        for idx, exp in enumerate(expected_words):
            got = self.dtcm(idx)
            if exp is None:
                continue
            if got != u32(exp):
                problems.append(f"DTCM[{idx}] (0x{0x1000_0000 + idx * 4:08x}): "
                                f"got {fmt(got)} exp 0x{u32(exp):08x}")
        expect(not problems, f"{label} mismatch:\n  " + "\n  ".join(problems[:24]))


def fmt(value):
    return "X" if value is None else f"0x{value:08x}"


def first_mismatch(core_list, ref_list):
    for i, (a, b) in enumerate(zip(core_list, ref_list)):
        if a != b:
            return i
    if len(core_list) != len(ref_list):
        return min(len(core_list), len(ref_list))
    return None


def check_trace(core_trace_pcs, ref_trace_pcs, label="execution trace"):
    """Compare executed-PC sequences, reporting the first divergence."""
    i = first_mismatch(core_trace_pcs, ref_trace_pcs)
    if i is None:
        return
    lo = max(0, i - 4)
    core_win = [f"0x{p:x}" for p in core_trace_pcs[lo:i + 4]]
    ref_win = [f"0x{p:x}" for p in ref_trace_pcs[lo:i + 4]]
    raise AssertionError(
        f"{label} diverged at executed instruction #{i} "
        f"(core={fmt(core_trace_pcs[i] if i < len(core_trace_pcs) else None)}, "
        f"ref={fmt(ref_trace_pcs[i] if i < len(ref_trace_pcs) else None)}):\n"
        f"  core: {core_win}\n  ref : {ref_win}")


def tb_seed(default=1234):
    import os
    return int(os.environ.get("TB_SEED", str(default)))


def tb_case(default):
    import os
    return os.environ.get("TB_CASE", default)


# ---------------------------------------------------------------------------
# fake bus slave
# ---------------------------------------------------------------------------
class BusTxn:
    __slots__ = ("addr", "we", "wdata", "wstrb", "start_cycle", "end_cycle",
                 "wait_cycles")

    def __init__(self, addr, we, wdata, wstrb, start_cycle):
        self.addr = addr
        self.we = we
        self.wdata = wdata
        self.wstrb = wstrb
        self.start_cycle = start_cycle
        self.end_cycle = None
        self.wait_cycles = 0

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"BusTxn(addr=0x{self.addr:08x}, we={self.we}, "
                f"wdata=0x{self.wdata:08x}, wstrb={self.wstrb:x}, "
                f"start={self.start_cycle}, end={self.end_cycle})")


class BusSlave:
    """Single-outstanding fake bus slave.

    Drives ``bus_ready``/``bus_rdata`` from a coroutine that samples the bus at
    every rising edge.  Readiness is held low for a configurable number of
    cycles, which freezes the core pipeline.  Writes are applied to a byte
    model exactly once per transaction (at the rising edge of ``bus_req``), so
    duplicate side effects caused by a frozen MEM stage would be observable.
    """

    def __init__(self, dut, clk, size=0x1000, seed=0,
                 wait_range=(0, 3), wait_by_addr=None, init_bytes=None):
        self.dut = dut
        self.clk = clk
        self.base = BUS_BASE
        self.size = size
        self.rng = random.Random(seed)
        self.wait_range = wait_range
        self.wait_by_addr = dict(wait_by_addr or {})
        self.mem = bytearray(size)
        if init_bytes is not None:
            expect(len(init_bytes) <= size, "bus init image too large")
            self.mem[:len(init_bytes)] = init_bytes
        self.txns = []
        self.rise_count = 0
        self.unexpected_rise_while_active = 0
        self._task = None
        self._active = None
        self._ready_high_driven = True
        self._cycle = 0
        self.if_pc_samples = []
        self.last_ready_value = 1

    # -- model access ------------------------------------------------------
    def _off(self, addr):
        off = addr - self.base
        expect(0 <= off < self.size, f"bus address 0x{addr:08x} outside fake slave")
        return off & ~3

    def model_word(self, addr):
        off = self._off(addr)
        return int.from_bytes(self.mem[off:off + 4], "little")

    def set_model_word(self, addr, value):
        off = self._off(addr)
        self.mem[off:off + 4] = u32(value).to_bytes(4, "little")

    def _apply_write(self, addr, wdata, wstrb):
        off = self._off(addr)
        for lane in range(4):
            if wstrb & (1 << lane):
                self.mem[off + lane] = (wdata >> (8 * lane)) & 0xFF

    # -- coroutine ---------------------------------------------------------
    def start(self):
        import cocotb
        self._task = cocotb.start_soon(self._run())

    async def _run(self):
        self.dut.bus_ready.value = 1
        self.dut.bus_rdata.value = 0
        prev_req = 0
        prev_req_cycle = -1

        while True:
            await RisingEdge(self.clk)
            await Timer(1, unit="ps")
            self._cycle += 1

            # Decide readiness for the upcoming cycle before sampling.
            if self._active is not None and self._active.end_cycle is None:
                if self._ready_high_driven:
                    # ready was high during the cycle that just ended: the
                    # transaction completed at this edge.
                    self._active.end_cycle = self._cycle
                    self._active = None
                    self.dut.bus_ready.value = 1
                    self._ready_high_driven = True
                else:
                    self._active.wait_cycles += 1
                    if self._active.wait_cycles >= self._wait_target(self._active):
                        self.dut.bus_ready.value = 1
                        self._ready_high_driven = True
                        self.dut.bus_rdata.value = self._rdata(self._active)
                    else:
                        self.dut.bus_ready.value = 0
                        self._ready_high_driven = False
            else:
                self.dut.bus_ready.value = 1

            # Post-edge values are the state for the upcoming cycle.
            try:
                req = int(self.dut.bus_req.value)
                we = int(self.dut.bus_we.value)
                addr = int(self.dut.bus_addr.value)
                wdata = int(self.dut.bus_wdata.value)
                wstrb = int(self.dut.bus_wstrb.value)
            except ValueError:
                # X while reset; keep driving idle.
                prev_req = 0
                continue

            if req and not prev_req:
                self.rise_count += 1
                if self._active is not None and self._active.end_cycle is None:
                    self.unexpected_rise_while_active += 1
                txn = BusTxn(addr, we, wdata, wstrb, self._cycle)
                if we:
                    self._apply_write(addr, wdata, wstrb)
                self.txns.append(txn)
                self._active = txn
                k = self._wait_target(txn)
                if k > 0:
                    self.dut.bus_ready.value = 0
                    self._ready_high_driven = False
                else:
                    self.dut.bus_rdata.value = self._rdata(txn)
                    self.dut.bus_ready.value = 1
                    self._ready_high_driven = True

            if req and not we and self._active is not None and self._active.end_cycle is None:
                # keep read data stable for the whole transaction
                self.dut.bus_rdata.value = self._rdata(self._active)

            prev_req = req
            prev_req_cycle = self._cycle
            self.last_ready_value = 1 if self._ready_high_driven else 0

    def _wait_target(self, txn):
        if txn.addr in self.wait_by_addr:
            return self.wait_by_addr[txn.addr]
        lo, hi = self.wait_range
        return self.rng.randint(lo, hi)

    def _rdata(self, txn):
        return self.model_word(txn.addr)

    # -- convenience -------------------------------------------------------
    @property
    def writes(self):
        return [t for t in self.txns if t.we]

    def txn_summary(self):
        return [(t.addr, t.we, t.wdata, t.wstrb) for t in self.txns]

    def stop(self):
        if self._task is not None:
            self._task.kill()
            self._task = None
