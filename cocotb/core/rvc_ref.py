"""Python mirror of the RTL RVC expander (rtl/core.v rvc_expand).

Kept in a module so the cocotb reference model and tests can canonicalize a
16-bit encoding exactly as IF does.  Reserved encodings expand to
``{16'b0, ci}`` (the RTL's illegal-instruction form).
"""

MASK32 = 0xFFFF_FFFF


def _sext(value, bits):
    sign = 1 << (bits - 1)
    return (value ^ sign) - sign


def _enc_i(opc, rd, f3, rs1, imm):
    return (((imm & 0xFFF) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((rd & 31) << 7) | opc) & MASK32


def _enc_s(rs2, rs1, f3, imm):
    imm &= 0xFFF
    return (((imm >> 5) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((imm & 0x1F) << 7) | 0x23) & MASK32


def _enc_s_fp(rs2, rs1, f3, imm):
    imm &= 0xfff
    return (((imm >> 5) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((imm & 0x1f) << 7) | 0x27) & MASK32


def _enc_b(rs1, rs2, f3, imm):
    imm &= 0x1FFF
    return (((imm >> 12) << 31) | (((imm >> 5) & 0x3F) << 25) |
            ((rs2 & 31) << 20) | ((rs1 & 31) << 15) | ((f3 & 7) << 12) |
            (((imm >> 1) & 0xF) << 8) | (((imm >> 11) & 1) << 7) | 0x63) & MASK32


def _enc_j(rd, imm):
    imm &= 0x1F_FFFF
    return (((imm >> 20) << 31) | (((imm >> 1) & 0x3FF) << 21) |
            (((imm >> 11) & 1) << 20) | (((imm >> 12) & 0xFF) << 12) |
            ((rd & 31) << 7) | 0x6F) & MASK32


def _enc_u(rd, imm20):
    return (((imm20 & 0xFFFFF) << 12) | ((rd & 31) << 7) | 0x37) & MASK32


def _enc_r(rd, rs1, rs2, f3, f7):
    return (((f7 & 0x7F) << 25) | ((rs2 & 31) << 20) | ((rs1 & 31) << 15) |
            ((f3 & 7) << 12) | ((rd & 31) << 7) | 0x33) & MASK32


def rvc_expand(ci):
    """Canonical 32-bit form of a 16-bit RVC encoding."""
    ci &= 0xFFFF
    q = ci & 3
    f3 = (ci >> 13) & 7
    b12 = (ci >> 12) & 1
    rd = (ci >> 7) & 31
    rs2 = (ci >> 2) & 31
    rdq0 = 8 + ((ci >> 2) & 7)
    rs1p = 8 + ((ci >> 7) & 7)
    rdq1 = 8 + ((ci >> 7) & 7)
    rs2p = 8 + ((ci >> 2) & 7)
    ill = False
    insn = 0

    def sext6():
        return _sext((b12 << 5) | ((ci >> 2) & 31), 6)

    def joff():
        return _sext((b12 << 11) | (((ci >> 8) & 1) << 10) |
                     (((ci >> 9) & 3) << 8) | (((ci >> 6) & 1) << 7) |
                     (((ci >> 7) & 1) << 6) | (((ci >> 2) & 1) << 5) |
                     (((ci >> 11) & 1) << 4) | (((ci >> 3) & 7) << 1), 12)

    if q == 0:
        if f3 == 0:                                    # C.ADDI4SPN
            imm = (((ci >> 7) & 0xF) << 6 | ((ci >> 11) & 3) << 4 |
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
            insn = _enc_s(rs2p, rs1p, 2, imm)
        elif f3 == 3:                                  # C.FLW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_i(0x07, rdq0, 2, rs1p, imm)
        elif f3 == 7:                                  # C.FSW
            imm = (((ci >> 5) & 1) << 6 | ((ci >> 10) & 7) << 3 |
                   ((ci >> 6) & 1) << 2)
            insn = _enc_s_fp(rs2p, rs1p, 2, imm)
        else:
            ill = True
    elif q == 1:
        if f3 == 0:                                    # C.NOP/C.ADDI
            imm = sext6()
            insn = 0x13 if rd == 0 else _enc_i(0x13, rd, 0, rd, imm)
        elif f3 == 1:                                  # C.JAL
            insn = _enc_j(1, joff())
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
                imm20 = ((((1 << 14) - 1) * b12) << 6 | b12 << 5 |
                         ((ci >> 2) & 31)) & 0xFFFFF
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
                    insn = _enc_r(rdq1, rdq1, rs2p, [0, 4, 6, 7][f2],
                                  0x20 if f2 == 0 else 0)
        elif f3 == 5:                                  # C.J
            insn = _enc_j(0, joff())
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
            imm = (((ci >> 7) & 3) << 6 | ((ci >> 9) & 0xF) << 2)
            insn = _enc_s(rs2, 2, 2, imm)
        elif f3 == 3:                                  # C.FLWSP
            imm = (((ci >> 2) & 3) << 6 | b12 << 5 | ((ci >> 4) & 7) << 2)
            insn = _enc_i(0x07, rd, 2, 2, imm)
        elif f3 == 7:                                  # C.FSWSP
            imm = (((ci >> 7) & 3) << 6 | ((ci >> 9) & 0xF) << 2)
            insn = _enc_s_fp(rs2, 2, 2, imm)
        else:
            ill = True
    else:
        ill = True

    return (0x10000 | ci) if ill else insn


def insn_len(ir):
    return 2 if ((ir & 3) != 3) else 4


def canon_ir(ir):
    ir &= MASK32
    return rvc_expand(ir & 0xFFFF) if (ir & 3) != 3 else ir
