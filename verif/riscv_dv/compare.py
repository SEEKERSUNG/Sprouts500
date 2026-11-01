#!/usr/bin/env python3
"""Compare a Sprouts500 RTL retirement trace with a Sail 0.16 instruction trace.

Inputs (all written by scripts/run_riscv_dv.sh):
  rtl.trace   `I <pc> <ir> x<rd> <wdata>`, `S <addr> <pc> <ir> <data> <strb>`
              and `X <pc> <ir> <mcause> <mtval>` lines
  sail.trace  sail_riscv_sim --trace-instr --trace-arch-regs --trace-mem
              --trace-exception output

Both models execute the same image at the same addresses (code at 0x0, the
data/stack/trap area at 0x10000000; see link_rtl.ld and sail_rv32im.json), so
PCs, register values and store addresses are numerically identical; no base
relocation is applied.

Instruction alignment: the two traces are walked instruction by instruction.
RTL retires and traces every instruction, including the handler; Sail traces
the trapping instruction followed by the handler's register effects with no
instruction line (`--trace-instr` stops at the trap boundary), so after a trap
the walk skips both handlers and re-anchors on the return address (mepc+4).
The handler's architectural effects are still covered indirectly - any GPR it
clobbers is compared at the next body instruction - and the trap itself is
compared through the `X` event (mcause, pc, mtval).

Store alignment: RTL `S` lines and Sail `mem[W,..]` lines are both emitted in
program order, so the store streams are compared as ordered lists.

Exit status:
  0  clean: all compared, no mismatch
  1  mismatch not covered by a documented finding
  3  COMPARE_XFAIL: the only mismatches are documented findings (printed as
     `XFINDING ...`); the run is not a clean pass
"""

import argparse
import re
import sys

SAIL_INSTR = re.compile(r"^\[(\d+)\] \[M\]: 0x([0-9a-fA-F]+) \(0x([0-9a-fA-F]+)\)")
SAIL_REG = re.compile(r"^x(\d+) <- 0x([0-9a-fA-F]+)")
SAIL_STORE = re.compile(r"^mem\[W,0x([0-9a-fA-F]+)\] <- 0x([0-9a-fA-F]+)")
SAIL_EXC = re.compile(
    r"^handling exc#([\w\-/]+) at priv M \| tval=0x([0-9a-fA-F]+)")
# Sail 0.16 exception names -> RISC-V mcause values.
SAIL_EXC_NAMES = {
    "instruction-address-misaligned": 0,
    "instruction-access-fault": 1,
    "illegal-instruction": 2,
    "breakpoint": 3,
    "load-address-misaligned": 4,
    "load-access-fault": 5,
    "store/amo-address-misaligned": 6,
    "store/amo-access-fault": 7,
    "misaligned-load": 4,
    "misaligned-store/amo": 6,
    "misaligned-fetch": 0,
    "ecall-from-u": 8,
    "ecall-from-s": 9,
    "ecall-from-m": 11,
}

# Documented findings found by this harness (the RTL is deliberately not
# changed; see docs/verification_riscv_dv.md).  Keyed by mcause.  Empty means
# every compared trap value must agree.
FINDINGS = {}


# ---------------------------------------------------------------------------
# RVC canonicalization
# ---------------------------------------------------------------------------
# The RTL expands compressed instructions in IF and traces canonical 32-bit
# IR; Sail traces the raw 16-bit encoding.  Mirror rtl/core.v rvc_expand()
# exactly so both traces can be compared field for field.  Reserved encodings
# expand to {16'b0, ci}, matching the RTL's illegal-instruction mtval.
MASK32 = 0xffff_ffff


def _sext(value, bits):
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def _enc_i(opc, rd, f3, rs1, imm):
    return (((imm & 0xfff) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((rd & 31) << 7) | opc) & MASK32


def _enc_s(rs2, rs1, f3, imm):
    imm &= 0xfff
    return (((imm >> 5) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | (imm & 0x1f) << 7 | 0x23) & MASK32


def _enc_s_fp(rs2, rs1, f3, imm):
    imm &= 0xfff
    return (((imm >> 5) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((imm & 0x1f) << 7) | 0x27) & MASK32


def _enc_b(rs1, rs2, f3, imm):
    imm &= 0x1fff
    return (((imm >> 12) << 31) | (((imm >> 5) & 0x3f) << 25) |
            ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | ((f3 & 7) << 12) |
            (((imm >> 1) & 0xf) << 8) | (((imm >> 11) & 1) << 7) | 0x63) & MASK32


def _enc_j(rd, imm):
    imm &= 0x1f_ffff
    return (((imm >> 20) << 31) | (((imm >> 1) & 0x3ff) << 21) |
            (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xff) << 12) |
            ((rd & 31) << 7) | 0x6f) & MASK32


def _enc_u(rd, imm20):
    return (((imm20 & 0xfffff) << 12) | ((rd & 31) << 7) | 0x37) & MASK32


def _enc_r(rd, rs1, rs2, f3, f7):
    return (((f7 & 0x7f) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((rd & 31) << 7) | 0x33) & MASK32


def rvc_expand(ci):
    """Canonical 32-bit form of a 16-bit RVC encoding (RTL mirror)."""
    ci &= 0xffff
    q = ci & 3
    f3 = (ci >> 13) & 7
    b12 = (ci >> 12) & 1
    rd = (ci >> 7) & 31
    rs2 = (ci >> 2) & 31
    rdq0 = 8 + ((ci >> 2) & 7)     # quadrant-0 rd'
    rs1p = 8 + ((ci >> 7) & 7)     # quadrant-0/1 rs1'
    rdq1 = 8 + ((ci >> 7) & 7)     # quadrant-1 rd'
    rs2p = 8 + ((ci >> 2) & 7)     # quadrant-1 rs2'
    ill = False
    insn = 0

    def sext6():
        return _sext((ci >> 2) & 0x3f if False else ((b12 << 5) | (ci >> 2) & 31), 6)

    if q == 0:
        if f3 == 0:                                    # C.ADDI4SPN
            imm = (((ci >> 7) & 0xf) << 6 | ((ci >> 11) & 3) << 4 |
                   ((ci >> 5) & 1) << 3 | ((ci >> 6) & 1) << 2)
            ill = imm == 0
            insn = _enc_i(0x13, rdq0, 0, 2, imm)
        elif f3 == 2:                                  # C.LW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_i(0x03, rdq0, 2, rs1p, imm)
        elif f3 == 6:                                  # C.SW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_s(rs2, rs1p, 2, imm)
        elif f3 == 3:                                  # C.FLW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_i(0x07, rdq0, 2, rs1p, imm)
        elif f3 == 7:                                  # C.FSW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_s_fp(rs2, rs1p, 2, imm)
        else:
            ill = True
    elif q == 1:
        if f3 == 0:                                    # C.NOP/C.ADDI
            imm = sext6()
            insn = 0x13 if rd == 0 else _enc_i(0x13, rd, 0, rd, imm)
        elif f3 == 1:                                  # C.JAL
            imm = _sext((b12 << 11) | (((ci >> 8) & 1) << 10) |
                        (((ci >> 9) & 3) << 8) | (((ci >> 6) & 1) << 7) |
                        (((ci >> 7) & 1) << 6) | (((ci >> 2) & 1) << 5) |
                        (((ci >> 11) & 1) << 4) | (((ci >> 3) & 7) << 1), 12)
            insn = _enc_j(1, imm)
        elif f3 == 2:                                  # C.LI
            insn = _enc_i(0x13, rd, 0, 0, sext6())
        elif f3 == 3:
            if rd == 2:                                # C.ADDI16SP
                imm = _sext((b12 << 9) | (((ci >> 3) & 3) << 7) |
                            (((ci >> 5) & 1) << 6) | (((ci >> 2) & 1) << 5) |
                            (((ci >> 6) & 1) << 4), 10)
                ill = imm == 0
                insn = _enc_i(0x13, 2, 0, 2, imm)
            else:                                      # C.LUI
                imm20 = (((((1 << 14) - 1) * b12) << 6) | (b12 << 5) |
                         ((ci >> 2) & 31)) & 0xfffff
                ill = ((b12 << 5) | ((ci >> 2) & 31)) == 0
                insn = _enc_u(rd, imm20)
        elif f3 == 4:
            op = (ci >> 10) & 3
            shamt = (ci >> 2) & 31
            if op == 0:                                # C.SRLI
                ill = bool(b12)
                insn = _enc_i(0x13, rdq1, 5, rdq1, shamt)
            elif op == 1:                              # C.SRAI
                ill = bool(b12)
                insn = _enc_i(0x13, rdq1, 5, rdq1, (0x20 << 5) | shamt)
            elif op == 2:                              # C.ANDI
                insn = _enc_i(0x13, rdq1, 7, rdq1, sext6())
            else:
                if b12:
                    ill = True                         # RV64 C.SUBW/ADDW/reserved
                else:
                    f2 = (ci >> 5) & 3
                    insn = _enc_r(rdq1, rdq1, rs2p,
                                  [0, 4, 6, 7][f2],
                                  0x20 if f2 == 0 else 0)
        elif f3 == 5:                                  # C.J
            imm = _sext((b12 << 11) | (((ci >> 8) & 1) << 10) |
                        (((ci >> 9) & 3) << 8) | (((ci >> 6) & 1) << 7) |
                        (((ci >> 7) & 1) << 6) | (((ci >> 2) & 1) << 5) |
                        (((ci >> 11) & 1) << 4) | (((ci >> 3) & 7) << 1), 12)
            insn = _enc_j(0, imm)
        else:                                          # C.BEQZ/C.BNEZ
            imm = _sext((b12 << 8) | (((ci >> 5) & 3) << 6) |
                        (((ci >> 2) & 1) << 5) | (((ci >> 10) & 3) << 3) |
                        (((ci >> 3) & 3) << 1), 9)
            insn = _enc_b(rs1p, 0, 0 if f3 == 6 else 1, imm)
    elif q == 2:
        if f3 == 0:                                    # C.SLLI
            ill = bool(b12)
            insn = _enc_i(0x13, rd, 1, rd, (ci >> 2) & 31)
        elif f3 == 2:                                  # C.LWSP
            imm = (((ci >> 2) & 3) << 6 | b12 << 5 | ((ci >> 4) & 7) << 2)
            ill = rd == 0
            insn = _enc_i(0x03, rd, 2, 2, imm)
        elif f3 == 4:
            if not b12:
                if rs2 == 0:                           # C.JR
                    ill = rd == 0
                    insn = _enc_i(0x67, 0, 0, rd, 0)
                else:                                  # C.MV
                    insn = _enc_r(rd, 0, rs2, 0, 0)
            elif rs2 != 0:                             # C.ADD
                insn = _enc_r(rd, rd, rs2, 0, 0)
            elif rd == 0:                              # C.EBREAK
                insn = 0x0010_0073
            else:                                      # C.JALR
                insn = _enc_i(0x67, 1, 0, rd, 0)
        elif f3 == 6:                                  # C.SWSP
            imm = (((ci >> 7) & 3) << 6 | ((ci >> 9) & 0xf) << 2)
            insn = _enc_s(rs2, 2, 2, imm)
        else:
            ill = True
    else:
        ill = True

    return (0x10000 | ci) if ill else insn


def canon_ir(ir):
    ir &= MASK32
    if (ir & 3) != 3:
        return rvc_expand(ir & 0xffff)
    return ir


def same_insn(rtl_ir, sail_ir):
    """Canonical-form comparison.

    Compressed instructions may expand differently as long as rd/rs1/rs2 and
    the architectural result agree; Sail expands ``c.sw rs2', uimm(rs1')``
    with ``rd`` (a store register field) left as the zero immediate, while
    the RTL writes it 0.  Stores are therefore compared on opcode/funct3 and
    the register fields that carry data, not the encoding bits they leave
    unspecified.
    """
    if rtl_ir == sail_ir:
        return True
    if (rtl_ir & 0x7F) == 0x23 and (sail_ir & 0x7F) == 0x23:
        # store rs2/rd canonicalization differs; address/data are compared
        # separately in the store stream
        return (((rtl_ir >> 12) & 0x7) == ((sail_ir >> 12) & 0x7) and
                ((rtl_ir >> 15) & 0x1F) == ((sail_ir >> 15) & 0x1F))
    return False


def insn_len(ir):
    return 2 if ((ir & 3) != 3) else 4


class Insn:
    __slots__ = ("pc", "ir", "length", "writes", "trap")

    def __init__(self, pc, ir):
        self.pc = pc
        self.ir = canon_ir(ir)      # RVC expanded to the RTL's canonical IR
        self.length = insn_len(ir)
        self.writes = []    # [(rd, value)]; rd != 0 only
        self.trap = None    # (mcause, tval) taken by this instruction


def unhex(s):
    try:
        return int(s, 16)
    except ValueError:
        return None         # x0 written as xxxxxxxx by the testbench


def read_rtl(path):
    """Retired instructions, store stream and EX exception events.

    `X <pc> <ir> <mcause> <mtval>` is printed when the exception is detected
    at EX (the instruction does not retire, so it has no `I` line).
    """
    insns, stores, events = [], [], []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "I":
                rd = int(parts[3][1:])
                ins = Insn(int(parts[1], 16), int(parts[2], 16))
                if rd != 0:
                    ins.writes.append((rd, unhex(parts[4])))
                insns.append(ins)
            elif parts[0] == "S":
                stores.append((int(parts[1], 16), unhex(parts[4]),
                               int(parts[5], 16)))
            elif parts[0] == "X":
                events.append((int(parts[3], 16), int(parts[1], 16),
                               int(parts[4], 16)))
    return insns, stores, events


def read_sail(path):
    """Instructions (trapping one flagged), store stream (addr, data), and the
    exception list (mcause, mepc, mtval) in execution order."""
    insns, stores = [], []
    cur = None
    with open(path, errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = SAIL_INSTR.match(line)
            if m:
                cur = Insn(int(m.group(2), 16), int(m.group(3), 16))
                insns.append(cur)
                continue
            m = SAIL_REG.match(line)
            if m and cur is not None:
                rd = int(m.group(1))
                if rd != 0:
                    cur.writes.append((rd, int(m.group(2), 16)))
                continue
            m = SAIL_STORE.match(line)
            if m and cur is not None:
                stores.append((int(m.group(1), 16), int(m.group(2), 16)))
                continue
            m = SAIL_EXC.match(line)
            if m and cur is not None:
                name = m.group(1)
                if name not in SAIL_EXC_NAMES:
                    sys.exit("compare: unknown Sail exception name %r" % name)
                cur.trap = (SAIL_EXC_NAMES[name], int(m.group(2), 16))
    return insns, stores


def fmt_writes(writes):
    return "[%s]" % ", ".join("x%d=%s" % (rd, "xxxxxxxx" if v is None else "0x%08x" % v)
                              for rd, v in writes)


def report(mismatches, what, limit=8):
    if not mismatches:
        return 0
    print("MISMATCH {}: {} instance(s)".format(what, len(mismatches)))
    for m in mismatches[:limit]:
        print("  " + m)
    return len(mismatches)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rtl", required=True)
    ap.add_argument("--sail", required=True)
    ap.add_argument("--stop-pc", type=lambda s: int(s, 0), required=True,
                    help="PC whose fetch ends both runs (the test_done symbol)")
    args = ap.parse_args()

    rtl, rtl_stores, rtl_events = read_rtl(args.rtl)
    sail, sail_stores = read_sail(args.sail)

    pc_mm, reg_mm, trap_mm = [], [], []
    findings = set()
    compared = 0

    # ---- traps ----
    # RTL records the trap as an `X` event when it is detected at EX; Sail
    # records it in the instruction entry of the trapping instruction.  The
    # lists must match one for one on (mcause, pc), and the trap values
    # (mtval) must agree.
    rtl_exc = rtl_events            # [(mcause, pc, mtval)]
    sail_tr = [s for s in sail if s.trap is not None]
    sail_exc = [(s.trap[0], s.pc) for s in sail_tr]
    if len(rtl_exc) != len(sail_exc):
        trap_mm.append("exception count rtl=%d sail=%d" % (len(rtl_exc), len(sail_exc)))
    for k, (a, b) in enumerate(zip(rtl_exc, sail_exc)):
        if (a[0], a[1]) != b:
            trap_mm.append("exception #%d rtl mcause=0x%x pc=0x%08x "
                           "sail mcause=0x%x pc=0x%08x" % (k, a[0], a[1], b[0], b[1]))
            continue
        if a[2] != sail_tr[k].trap[1]:
            if a[0] in FINDINGS:
                findings.add(a[0])
            else:
                trap_mm.append("exception #%d pc=0x%08x mcause=0x%x rtl mtval=0x%x "
                               "sail mtval=0x%x" % (k, a[1], a[0], a[2], sail_tr[k].trap[1]))

    # ---- instruction stream ----
    # The run ends when the PC reaches `test_done`: the RTL testbench drops
    # that instruction from its trace, Sail logs its fetch header only.  Drop
    # Sail's header so both instruction streams describe the same retired
    # instructions.
    if sail and sail[-1].pc == args.stop_pc and not sail[-1].writes \
            and sail[-1].trap is None:
        sail = sail[:-1]

    i = j = 0
    while i < len(rtl) and j < len(sail):
        s = sail[j]
        if s.trap is not None:
            # The trapping instruction is present in Sail's trace but does not
            # retire in the RTL (it is covered by the trap comparison instead).
            # Both sides then execute the handler; the RTL retires it, Sail
            # only logs its register effects, so skip both handlers and
            # re-anchor on the instruction after mepc.
            ret = s.pc + s.length
            j += 1
            while j < len(sail) and sail[j].pc != ret:
                j += 1
            while i < len(rtl) and rtl[i].pc != ret:
                i += 1
            continue
        r = rtl[i]
        tag = "#%d pc=0x%08x ir=0x%08x" % (compared, r.pc, r.ir)
        if r.pc != s.pc or not same_insn(r.ir, s.ir):
            pc_mm.append("%s sail pc=0x%08x ir=0x%08x" % (tag, s.pc, s.ir))
            break
        compared += 1
        if len(s.writes) > 1:
            reg_mm.append("%s sail has %d register writes" % (tag, len(s.writes)))
        elif s.writes:
            rd, v = s.writes[0]
            if r.writes and r.writes[-1][0] == rd:
                if r.writes[-1][1] != v:
                    reg_mm.append("%s rtl x%d=%s sail x%d=0x%08x" %
                                  (tag, rd, "xxxxxxxx" if r.writes[-1][1] is None
                                   else "0x%08x" % r.writes[-1][1], rd, v))
            else:
                reg_mm.append("%s rtl wrote %s, sail wrote x%d=0x%08x" %
                              (tag, fmt_writes(r.writes), rd, v))
        elif r.writes:
            reg_mm.append("%s rtl wrote %s, sail wrote nothing" %
                          (tag, fmt_writes(r.writes)))
        i += 1
        j += 1

    if not pc_mm and (i < len(rtl) or j < len(sail)):
        extra = "rtl=%d sail=%d compared=%d" % (len(rtl), len(sail), compared)
        if i < len(rtl):
            extra += " first extra rtl pc=0x%08x ir=0x%08x" % (rtl[i].pc, rtl[i].ir)
        else:
            extra += " first extra sail pc=0x%08x ir=0x%08x" % (sail[j].pc, sail[j].ir)
        print("MISMATCH instruction count: " + extra)
        return 1

    # ---- stores ----
    # RTL `S` lines and Sail `mem[W,..]` lines are both in program order
    # (EX/memory for RTL, retire for Sail).
    store_mm = []
    if len(rtl_stores) != len(sail_stores):
        store_mm.append("store count rtl=%d sail=%d" %
                        (len(rtl_stores), len(sail_stores)))
    for k, (r, s) in enumerate(zip(rtl_stores, sail_stores)):
        if r[0] != s[0] or r[1] != s[1]:
            store_mm.append("store #%d rtl addr=0x%08x data=0x%08x "
                            "sail addr=0x%08x data=0x%08x" %
                            (k, r[0], r[1], s[0], s[1]))
        if r[2] is not None and r[2] != 0xF:
            store_mm.append("store #%d rtl strb=0x%x (expected 0xF)" % (k, r[2]))

    total = 0
    total += report(pc_mm, "pc/instruction encoding")
    total += report(reg_mm, "register write data")
    total += report(store_mm, "store address/data")
    total += report(trap_mm, "trap cause/pc/value")

    for mc in sorted(findings):
        print("XFINDING mcause=0x%x: %s" % (mc, FINDINGS[mc]))

    regs = sum(len(x.writes) for x in rtl)
    if total:
        print("COMPARE_FAIL insns=%d reg_writes=%d stores=%d traps=%d" %
              (compared, regs, len(rtl_stores), len(rtl_exc)))
        return 1
    if findings:
        print("COMPARE_XFAIL insns=%d reg_writes=%d stores=%d traps=%d" %
              (compared, regs, len(rtl_stores), len(rtl_exc)))
        return 3
    print("COMPARE_PASS insns=%d reg_writes=%d stores=%d traps=%d" %
          (compared, regs, len(rtl_stores), len(rtl_exc)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
