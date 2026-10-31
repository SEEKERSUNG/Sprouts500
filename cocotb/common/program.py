"""A tiny RV32I/M assembler used to build SoC test programs.

Only what the cocotb tests need is implemented: the base RV32I instruction
encodings, the M-extension R-type ops, and the M-mode system instructions used
by the interrupt test.  Encodings follow the RISC-V unprivileged spec; the
assembler is a plain Python object, not a compiler.

Conventions used by the SoC tests:

* code is linked at ITCM address 0 (``tb_soc_bus`` loads a flat hex image);
* ``x30``/``x31`` are scratch registers reserved for ``halt_sequence``;
* the halt mailbox is the last ITCM word (0x7FFC): writing ``HALT_MAGIC``
  there stops the testbench wait loop.

Register names accepted anywhere a register is expected:
``"x5"``, ``"a0"``, ``"t0"``, ``"sp"``, integer 0..31.
"""
from __future__ import annotations

from pathlib import Path

MASK32 = 0xFFFF_FFFF
HALT_ADDR = 0x0000_7FFC
HALT_MAGIC = 0x5A5A_5A5A


def reg(r: int | str) -> int:
    if isinstance(r, int):
        if not 0 <= r < 32:
            raise ValueError(f"bad register {r}")
        return r
    name = r.strip().lower()
    if name.startswith("x") and name[1:].isdigit():
        n = int(name[1:])
        if 0 <= n < 32:
            return n
    abi = {
        "zero": 0, "ra": 1, "sp": 2, "gp": 3, "tp": 4,
        "t0": 5, "t1": 6, "t2": 7, "s0": 8, "fp": 8, "s1": 9,
        "a0": 10, "a1": 11, "a2": 12, "a3": 13, "a4": 14, "a5": 15,
        "a6": 16, "a7": 17, "s2": 18, "s3": 19, "s4": 20, "s5": 21,
        "s6": 22, "s7": 23, "s8": 24, "s9": 25, "s10": 26, "s11": 27,
        "t3": 28, "t4": 29, "t5": 30, "t6": 31,
    }
    if name in abi:
        return abi[name]
    raise ValueError(f"unknown register {r!r}")


def _u(v: int, bits: int, what: str) -> int:
    if not -(1 << (bits - 1)) <= v < (1 << bits):
        raise ValueError(f"{what} out of range: {v}")
    return v & ((1 << bits) - 1)


def r_type(opcode: int, rd, funct3: int, rs1, rs2, funct7: int) -> int:
    return (
        ((funct7 & 0x7F) << 25)
        | ((reg(rs2) & 0x1F) << 20)
        | ((reg(rs1) & 0x1F) << 15)
        | ((funct3 & 7) << 12)
        | ((reg(rd) & 0x1F) << 7)
        | (opcode & 0x7F)
    )


def i_type(opcode: int, rd, funct3: int, rs1, imm: int) -> int:
    return (
        (_u(imm, 12, "I-immediate") << 20)
        | ((reg(rs1) & 0x1F) << 15)
        | ((funct3 & 7) << 12)
        | ((reg(rd) & 0x1F) << 7)
        | (opcode & 0x7F)
    )


def s_type(opcode: int, funct3: int, rs1, rs2, imm: int) -> int:
    imm = _u(imm, 12, "S-immediate")
    return (
        ((imm >> 5) << 25)
        | ((reg(rs2) & 0x1F) << 20)
        | ((reg(rs1) & 0x1F) << 15)
        | ((funct3 & 7) << 12)
        | ((imm & 0x1F) << 7)
        | (opcode & 0x7F)
    )


def b_type(opcode: int, funct3: int, rs1, rs2, imm: int) -> int:
    imm = _u(imm, 13, "B-immediate")
    return (
        (((imm >> 12) & 1) << 31)
        | (((imm >> 5) & 0x3F) << 25)
        | ((reg(rs2) & 0x1F) << 20)
        | ((reg(rs1) & 0x1F) << 15)
        | ((funct3 & 7) << 12)
        | (((imm >> 1) & 0xF) << 8)
        | (((imm >> 11) & 1) << 7)
        | (opcode & 0x7F)
    )


def u_type(opcode: int, rd, imm20: int) -> int:
    return ((imm20 & 0xFFFFF) << 12) | ((reg(rd) & 0x1F) << 7) | (opcode & 0x7F)


def j_type(opcode: int, rd, imm: int) -> int:
    imm = _u(imm, 21, "J-immediate")
    return (
        (((imm >> 20) & 1) << 31)
        | (((imm >> 1) & 0x3FF) << 21)
        | (((imm >> 11) & 1) << 20)
        | (((imm >> 12) & 0xFF) << 12)
        | ((reg(rd) & 0x1F) << 7)
        | (opcode & 0x7F)
    )


OP_LUI, OP_AUIPC, OP_JAL, OP_JALR = 0x37, 0x17, 0x6F, 0x67
OP_BRANCH, OP_LOAD, OP_STORE, OP_IMM, OP_R = 0x63, 0x03, 0x23, 0x13, 0x33
OP_SYSTEM, OP_MISC = 0x73, 0x0F


class Assembler:
    def __init__(self) -> None:
        self.words: list[int] = []
        self.labels: dict[str, int] = {}
        self._relocs: list[tuple[int, str, str, tuple]] = []

    # -- labels ------------------------------------------------------------
    def here(self) -> int:
        return len(self.words)

    def label(self, name: str) -> "Assembler":
        if name in self.labels:
            raise ValueError(f"duplicate label {name}")
        self.labels[name] = len(self.words)
        return self

    def _offset(self, target: int | str) -> int:
        if isinstance(target, int):
            return target
        return self.labels[target] * 4

    # -- raw ---------------------------------------------------------------
    def emit(self, word: int) -> "Assembler":
        self.words.append(word & MASK32)
        return self

    # -- U / immediate -----------------------------------------------------
    def lui(self, rd, imm20: int) -> "Assembler":
        return self.emit(u_type(OP_LUI, rd, imm20))

    def auipc(self, rd, imm20: int) -> "Assembler":
        return self.emit(u_type(OP_AUIPC, rd, imm20))

    def li(self, rd, value: int) -> "Assembler":
        """Materialize a 32-bit constant with LUI+ADDI (never uses x0)."""
        value &= MASK32
        lo = value & 0xFFF
        hi = (value >> 12) & 0xFFFFF
        if lo & 0x800:
            hi = (hi + 1) & 0xFFFFF
            lo = lo - 0x1000
        if value == 0:
            return self.addi(rd, "x0", 0)
        if hi == 0:
            return self.addi(rd, "x0", lo)
        return self.lui(rd, hi).addi(rd, rd, lo)

    def mv(self, rd, rs) -> "Assembler":
        return self.addi(rd, rs, 0)

    def nop(self) -> "Assembler":
        return self.addi("x0", "x0", 0)

    def addi(self, rd, rs1, imm: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b000, rs1, imm))

    def slti(self, rd, rs1, imm: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b010, rs1, imm))

    def xori(self, rd, rs1, imm: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b100, rs1, imm))

    def ori(self, rd, rs1, imm: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b110, rs1, imm))

    def andi(self, rd, rs1, imm: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b111, rs1, imm))

    def slli(self, rd, rs1, shamt: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b001, rs1, shamt & 0x1F))

    def srli(self, rd, rs1, shamt: int) -> "Assembler":
        return self.emit(i_type(OP_IMM, rd, 0b101, rs1, shamt & 0x1F))

    def srai(self, rd, rs1, shamt: int) -> "Assembler":
        return self.emit(
            i_type(OP_IMM, rd, 0b101, rs1, 0x400 | (shamt & 0x1F))
        )

    # -- R type ------------------------------------------------------------
    def _r(self, rd, rs1, rs2, funct3: int, funct7: int) -> "Assembler":
        return self.emit(r_type(OP_R, rd, funct3, rs1, rs2, funct7))

    def add(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b000, 0x00)
    def sub(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b000, 0x20)
    def sll(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b001, 0x00)
    def slt(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b010, 0x00)
    def sltu(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b011, 0x00)
    def xor(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b100, 0x00)
    def srl(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b101, 0x00)
    def sra(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b101, 0x20)
    def or_(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b110, 0x00)
    def and_(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b111, 0x00)

    # M extension
    def mul(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b000, 0x01)
    def mulh(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b001, 0x01)
    def mulhsu(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b010, 0x01)
    def mulhu(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b011, 0x01)
    def div(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b100, 0x01)
    def divu(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b101, 0x01)
    def rem(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b110, 0x01)
    def remu(self, rd, rs1, rs2): return self._r(rd, rs1, rs2, 0b111, 0x01)

    # -- loads / stores ----------------------------------------------------
    def _load(self, rd, funct3: int, rs1, off: int) -> "Assembler":
        return self.emit(i_type(OP_LOAD, rd, funct3, rs1, off))

    def lb(self, rd, off: int, rs1): return self._load(rd, 0b000, rs1, off)
    def lh(self, rd, off: int, rs1): return self._load(rd, 0b001, rs1, off)
    def lw(self, rd, off: int, rs1): return self._load(rd, 0b010, rs1, off)
    def lbu(self, rd, off: int, rs1): return self._load(rd, 0b100, rs1, off)
    def lhu(self, rd, off: int, rs1): return self._load(rd, 0b101, rs1, off)

    def _store(self, funct3: int, rs2, rs1, off: int) -> "Assembler":
        return self.emit(s_type(OP_STORE, funct3, rs1, rs2, off))

    def sb(self, rs2, off: int, rs1): return self._store(0b000, rs2, rs1, off)
    def sh(self, rs2, off: int, rs1): return self._store(0b001, rs2, rs1, off)
    def sw(self, rs2, off: int, rs1): return self._store(0b010, rs2, rs1, off)

    # -- control flow ------------------------------------------------------
    def _branch(self, funct3: int, rs1, rs2, target: int | str) -> "Assembler":
        idx = len(self.words)
        self.words.append(b_type(OP_BRANCH, funct3, rs1, rs2, 0))
        self._relocs.append((idx, "B", target, (funct3, rs1, rs2)))
        return self

    def beq(self, rs1, rs2, target): return self._branch(0b000, rs1, rs2, target)
    def bne(self, rs1, rs2, target): return self._branch(0b001, rs1, rs2, target)
    def blt(self, rs1, rs2, target): return self._branch(0b100, rs1, rs2, target)
    def bge(self, rs1, rs2, target): return self._branch(0b101, rs1, rs2, target)
    def bltu(self, rs1, rs2, target): return self._branch(0b110, rs1, rs2, target)
    def bgeu(self, rs1, rs2, target): return self._branch(0b111, rs1, rs2, target)

    def jal(self, rd, target: int | str) -> "Assembler":
        idx = len(self.words)
        self.words.append(j_type(OP_JAL, rd, 0))
        self._relocs.append((idx, "J", target, (rd,)))
        return self

    def j(self, target: int | str) -> "Assembler":
        return self.jal("x0", target)

    def jalr(self, rd, rs1, off: int = 0) -> "Assembler":
        return self.emit(i_type(OP_JALR, rd, 0b000, rs1, off))

    def ret(self) -> "Assembler":
        return self.jalr("x0", "ra", 0)

    # -- system ------------------------------------------------------------
    def _csr(self, funct3: int, rd, csr: int, rs1) -> "Assembler":
        return self.emit(i_type(OP_SYSTEM, rd, funct3, rs1, csr))

    def csrrw(self, rd, csr: int, rs1): return self._csr(0b001, rd, csr, rs1)
    def csrrs(self, rd, csr: int, rs1): return self._csr(0b010, rd, csr, rs1)
    def csrrc(self, rd, csr: int, rs1): return self._csr(0b011, rd, csr, rs1)
    def csrrwi(self, rd, csr: int, uimm: int):
        return self._csr(0b101, rd, csr, uimm)

    def ecall(self) -> "Assembler":
        return self.emit(0x0000_0073)

    def ebreak(self) -> "Assembler":
        return self.emit(0x0010_0073)

    def mret(self) -> "Assembler":
        return self.emit(0x3020_0073)

    def wfi(self) -> "Assembler":
        return self.emit(0x1050_0073)

    # -- finalize ----------------------------------------------------------
    def assemble(self) -> list[int]:
        """Resolve label relocations and return the instruction words."""
        words = list(self.words)
        for idx, kind, target, operands in self._relocs:
            off = self._offset(target) - idx * 4
            if kind == "B":
                funct3, rs1, rs2 = operands
                words[idx] = b_type(OP_BRANCH, funct3, rs1, rs2, off)
            elif kind == "J":
                (rd,) = operands
                words[idx] = j_type(OP_JAL, rd, off)
            else:  # pragma: no cover - defensive
                raise AssertionError(kind)
        return words


def halt_sequence(asm: Assembler, addr: int = HALT_ADDR,
                  magic: int = HALT_MAGIC) -> Assembler:
    """Write *magic* to *addr* (ITCM halt mailbox) and spin forever.

    Uses x31 for the address and x30 for the magic; programs must not keep
    live values in those two registers across this call.
    """
    asm.li("x31", addr)
    asm.li("x30", magic)
    asm.sw("x30", 0, "x31")
    asm.j(0)  # jal x0, 0: infinite self-loop
    return asm


def build_program(asm: Assembler, size_words: int = 8192) -> list[int]:
    words = asm.assemble()
    if len(words) > size_words:
        raise ValueError(f"program too large: {len(words)} > {size_words} words")
    # Pad the image: $readmemh leaves unlisted words undefined, and the core
    # would execute X instructions if it ever ran past the image.
    return words + [0] * (size_words - len(words))


def write_program(path: Path, asm: Assembler, size_words: int = 8192) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{w:08x}\n" for w in build_program(asm, size_words)))
    return path
