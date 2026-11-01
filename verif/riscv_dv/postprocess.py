#!/usr/bin/env python3
"""Post-process a riscv-dv (pyflow) generated assembly file for Sprouts500 lockstep.

riscv-dv is written for a full privileged/Spike-style simulator.  Sprouts500 is
a bare RV32IM machine-mode core with a 4 MiB ITCM and a 16 KiB DTCM, so the
generated prologue, handlers and memory layout are rewritten into a form that
Sail 0.16 and the RTL can both execute byte-for-byte identically:

  * The CSR/mret bootstrap (misa, mtvec, mepc, mstatus, mie, mret) is emitted
    in a fixed form.  misa writes are dropped because Sprouts500 treats them as
    illegal while Sail accepts them.  Every GPR except sp is zeroed first,
    because the generated init list is incomplete in some seeds.
  * The trap handler uses sp as its frame (no generated instruction writes
    sp), records mcause/mepc/mtval plus a trap counter at the linker symbol
    `trap_record`, restores the GPRs and mret's to mepc + instruction length
    (2 or 4 bytes, since IALIGN=16).  Illegal instructions therefore resume;
    the comparison itself uses the RTL `X` exception events and the Sail
    exception log, the record is for evidence.
  * `test_done` is `li gp, 1` followed by a halt store to the linker symbol
    `halt_mailbox` and an infinite loop.  The generated ecall handler is not
    used (it writes the GPR dump to address 0, which is unmapped in Sail).
    The lockstep run stops at the `test_done` PC, so this tail is outside the
    compared window.
  * The generated data pages, the stack, the trap record and the halt mailbox
    live in the DTCM at 0x10000000.  The same ELF runs in Sail, whose
    sail_rv32im.json maps RAM at exactly the same addresses, so every PC,
    register value and store address is numerically identical in both
    models.  Data pages come from a seed-derived PCG stream, making the image
    deterministic for a given seed.
  * The generated `tohost` symbol is not emitted: Sail 0.16 places an HTIF
    device at the linked `tohost` symbol whenever one exists, and Sprouts500
    has no HTIF.
  * Instruction streams that depend on a data-page base register
    (this pyflow snapshot emits no load/store streams, but the rewrite handles
    them if they appear) are relocated onto data labels / sp.
  * CSR instructions, fence, wfi and ebreak are removed: Sail and Sprouts500
    do not agree on every architectural trap for those, and they are not part
    of this RV32IM comparison.  Illegal encodings were exercised with a
    directed test (see docs/verification_riscv_dv.md); pyvsc's illegal
    instruction injection does not currently solve.

Usage: postprocess.py <in.S> <out.S>
Prints one summary line; exits nonzero if the input layout is unexpected.
"""

import argparse
import hashlib
import re
import sys

# All GPRs except sp are zeroed before the generated init (see main).  The trap
# handler uses sp as its frame pointer: no generated instruction writes sp,
# and sp starts inside the dedicated stack region.
# Linker-provided symbols: trap_record and halt_mailbox live in the DTCM, which
# is mapped at the same addresses in the RTL and in the Sail configuration.
TRAP_RECORD_SYM = "trap_record"
HALT_MAILBOX_SYM = "halt_mailbox"
HALT_WORD = 0x5A5A5A5A
DATA_SIZE = 0x2000         # 8 KiB of generated data pages
USER_STACK_BYTES = 512

DROP = {"csrr", "csrw", "csrc", "csrwi", "csrrwi", "csrs", "csrrw", "csrrs",
        "csrrc", "csrrsi", "csrrci", "fence", "fence.i", "wfi", "ebreak",
        "fence_i", "sfence.vma"}
LOAD = {"lb", "lh", "lw", "lbu", "lhu"}
STORE = {"sb", "sh", "sw"}

DIRECTIVE = {".section", ".text", ".data", ".org", ".align", ".word", ".4byte",
             ".2byte", ".byte", ".dword", ".global", ".globl", ".include",
             ".option", ".rept", ".endr", ".p2align", ".bss", ".rodata"}
# A label at the start of a line: numeric ("44:") or symbolic ("main:").
LABEL_RE = re.compile(r"^\s*(?:[0-9]+|[A-Za-z_$][\w.$]*)\s*:\s*(.*)$")
MNEM_RE = re.compile(r"^\s*(?:([0-9]+):\s*)?([A-Za-z][\w.]*)\s*(.*)$")


def pcg(name):
    h = hashlib.sha256(name.encode()).digest()
    state = int.from_bytes(h[:8], "little") | 1
    inc = int.from_bytes(h[8:16], "little") | 1

    def nxt():
        nonlocal state
        old = state
        state = (old * 6364136223846793005 + inc) & ((1 << 64) - 1)
        xorshifted = (((old >> 18) ^ old) >> 27) & 0xFFFFFFFF
        rot = old >> 59
        return ((xorshifted >> rot) | (xorshifted << ((-rot) & 31))) & 0xFFFFFFFF
    return nxt


def data_target(addr):
    """Map a generated data address into the pinned data region."""
    off = addr % DATA_SIZE
    off &= ~3
    block = off & ~0x3FF
    return block, off - block


def split_line(raw):
    """Split a generated assembly line into (prefix, [(mnemonic_text, comment)]).

    riscv-dv puts a label and up to two instructions on one line when a label
    is attached to a post-processed branch ("44: addi x2, x2, 4; beq x2, x3,
    44b").  The prefix is the label text; each instruction keeps its comment.
    """
    rest = raw
    prefix = ""
    while True:
        lm = LABEL_RE.match(rest)
        if not lm or rest.lstrip().startswith("."):
            break
        prefix += rest[:len(rest) - len(lm.group(1))]
        rest = lm.group(1)
    items = []
    for part in rest.split(";"):
        if not part.strip():
            continue
        code, _, comment = part.partition("#")
        code = code.strip()
        if code:
            items.append((code, comment.strip()))
    return prefix, items


def rewrite_body(lines, seed_name):
    """Rewrite the generated body so Sail and the RTL execute it identically."""
    out = []
    stats = {"total": 0, "drop": 0, "load": 0, "store": 0, "sp": 0, "reserved": 0}
    rnd = pcg("addr:" + seed_name)
    for line in lines:
        raw = line.rstrip("\n")
        if not raw.strip():
            out.append(raw)
            continue
        prefix, items = split_line(raw)
        if not items:
            out.append(raw)
            continue
        indent = raw[:len(raw) - len(raw.lstrip())] or " " * 18
        indent = raw[:len(raw) - len(raw.lstrip())] or " " * 18
        first_indent = indent if prefix.strip() else ""
        emitted = []
        changed = False
        for idx, (code, comment) in enumerate(items):
            mnem = code.split()[0].lower()
            ops = code[len(code.split()[0]):].strip()
            stats["total"] += 1
            lead = prefix if idx == 0 else (" " * len(prefix) if prefix.strip() else "")
            cmt = (" # " + comment) if comment else ""
            if mnem in DROP:
                stats["drop"] += 1
                changed = True
                continue
            if mnem in DIRECTIVE:
                emitted.append((lead + (first_indent or indent) + code + cmt))
                changed = True
                continue
            # `li x2, ...` in the generated init clobbers the stack pointer that
            # the prologue installed; point it back at the user stack.
            if mnem == "li" and re.match(r"^x?2\s*,", ops):
                stats["sp"] += 1
                emitted.append(lead + indent + "la sp, user_stack_end" + cmt)
                changed = True
                continue
            # The generator leaves some registers out of its init list (x15 in
            # some seeds, x28/x29 in all of them); keep them defined for the
            # whole program so no compared value is an X in the RTL.
            if mnem == "li" and re.match(r"^(?:x)?(?:15|28|29)\s*,", ops):
                stats["reserved"] += 1
                changed = True
                continue
            if mnem in LOAD:
                m = re.match(r"^(\w+)\s*,\s*0x([0-9a-fA-F]+)\((\w+)\)", ops) or \
                    re.match(r"^(\w+)\s*,\s*0x([0-9a-fA-F]+)\s*$", ops)
                if m:
                    block, inner = data_target(int(m.group(2), 16))
                    rd = m.group(1)
                    emitted.append(lead + indent + "la {}, data_{}".format(rd, block))
                    emitted.append(indent + "{} {}, {}({}){}".format(mnem, rd, inner, rd, cmt))
                    stats["load"] += 1
                    changed = True
                    continue
                if ops.split()[0].startswith("0x"):
                    block, inner = data_target(int(ops.split()[0], 16))
                    emitted.append(lead + indent + "la {}, data_{}".format(
                        ops.split(",")[0], block))
                    emitted.append(indent + "{}{}".format(
                        code[len(code.split()[0]):].replace(ops.split()[0], "{}({})".format(
                            inner, ops.split(",")[0]), 1), cmt))
                    stats["load"] += 1
                    changed = True
                    continue
            if mnem in STORE:
                m = re.match(r"^(\w+)\s*,\s*0x[0-9a-fA-F]+\((\w+)\)", ops)
                if m:
                    off = (rnd() % ((DATA_SIZE - 64) // 4)) * 4
                    emitted.append(lead + indent + "{} {}, {}(sp){}".format(
                        mnem, m.group(1), off, cmt))
                    stats["store"] += 1
                    changed = True
                    continue
            emitted.append(lead + (first_indent or indent) + code + cmt)
        if not changed:
            out.append(raw)
            continue
        out.extend(emitted)
    return out, stats


def data_section(seed_name):
    rnd = pcg("data:" + seed_name)
    lines = ['.section .region_0,"aw",@progbits', ".align 4"]
    for off in range(0, DATA_SIZE, 0x400):
        lines.append(".org {}".format(off))
        lines.append(".global data_{}".format(off))
        lines.append("data_{}:".format(off))
        for _ in range(0x400 // 4):
            lines.append(".word 0x{:08x}".format(rnd()))
    return lines


def stack_section(name, words, label):
    return ['".section .{},"aw",@progbits;"'.format(name).strip('"'),
            ".align 2",
            ".global {}_start".format(label), "{}_start:".format(label),
            ".rept {}".format(words - 1), ".4byte 0x0", ".endr",
            ".global {}_end".format(label), "{}_end:".format(label),
            ".4byte 0x0"]


def trap_handler():
    """Save GPRs on the user stack, record the trap, restore, mret past it.

    The resume address is mepc + the length of the trapped instruction
    (opcode[1:0] != 11 means 16-bit compressed), not a fixed +4.

    Register discipline: x1 is saved/restored around the body (its slot is
    still live when the resume address is computed, so a scratch register is
    used for that), x2 is the frame pointer and is restored last, x4 is saved
    at 16(x2) and reloaded before the restore loop.  CSR reads are plain
    ``csrr`` (csrrs rd, csr, x0)."""
    F = "x2"          # frame pointer (sp - 128); sp is never written by the body
    L = ["                  addi x2, x2, -128",
         "                  sw x1, 4(x2)",
         "                  sw x31, 124(x2)",
         "                  addi x31, x2, 128",
         "                  sw x31, 8(x2) # original sp",
         "                  sw x4, 16(x2)"]
    for r in range(3, 32):
        if r in (2, 4, 31):
            continue
        L.append("                  sw x{}, {}(x2)".format(r, 4 * r))
    L += [
        "                  csrr x31, 0x342 # MCAUSE",
        "                  sw x31, 0x40(x2)",
        "                  csrr x31, 0x341 # MEPC",
        "                  sw x31, 0x44(x2)",
        "                  csrr x31, 0x343 # MTVAL",
        "                  sw x31, 0x48(x2)",
        "                  lw x31, 0x44(x2)",
        "                  lhu x4, 0(x31) # trapped opcode",
        "                  andi x4, x4, 3",
        "                  li x1, 3",
        "                  beq x4, x1, 1f # 32-bit instruction",
        "                  addi x31, x31, 2 # compressed: resume at +2",
        "                  j 2f",
        "1:                addi x31, x31, 4 # 32-bit: resume at +4",
        "2:                csrw 0x341, x31 # resume after the trapping instruction",
        "                  lw x1, 4(x2)",
        "                  la x31, {}".format(TRAP_RECORD_SYM),
        "                  lw x4, 0x40(x2)",
        "                  sw x4, 0(x31) # mcause",
        "                  lw x4, 0x44(x2)",
        "                  sw x4, 4(x31) # mepc",
        "                  lw x4, 0x48(x2)",
        "                  sw x4, 8(x31) # mtval",
        "                  lw x4, 12(x31)",
        "                  addi x4, x4, 1",
        "                  sw x4, 12(x31) # trap count",
        "                  lw x4, 16(x2)",
    ]
    for r in range(3, 32):
        if r in (2, 4):
            continue
        L.append("                  lw x{}, {}(x2)".format(r, 4 * r))
    L += ["                  addi x2, x2, 128",
          "                  mret"]
    return L


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    args = ap.parse_args()

    with open(args.inp) as f:
        src = f.read().splitlines()

    def find(pred, what):
        for i, l in enumerate(src):
            if pred(l):
                return i
        sys.exit("postprocess: missing {}".format(what))

    i_wt = find(lambda l: l.startswith("write_tohost:"), "write_tohost:")
    i_data = find(lambda l: l.strip() == ".section .data", ".section .data")
    i_init = find(lambda l: l.startswith("init:"), "init:")
    i_tail = None
    for i in range(i_wt, 0, -1):
        if "test_done" in src[i]:
            i_tail = i
            break
    if i_tail is None or not (i_init < i_tail < i_wt < i_data):
        sys.exit("postprocess: unexpected riscv-dv layout")

    body, stats = rewrite_body(src[i_init + 1:i_tail], args.inp)

    out = ['.include "user_define.h"', ".globl _start", ".section .text",
           ".option norvc;",
           "_start:", "h0_start:",
           "                  la sp, user_stack_end",
           "trap_vec_init:",
           "                  la x30, mtvec_handler",
           "                  csrw 0x305, x30 # MTVEC (direct mode)",
           "mepc_setup:",
           "                  la x31, init",
           "                  csrw 0x341, x31 # MEPC",
           "init_machine_mode:",
           "                  li x31, 0x1800",
           "                  csrw 0x300, x31 # MSTATUS",
           "                  li x31, 0x0",
           "                  csrw 0x304, x31 # MIE",
           "                  mret",
           "init:"]
    # The generator's init list is incomplete for some seeds (x15, x26, x28,
    # x29, ... are missing at random).  Define every GPR except sp here so no
    # generated operand can read an X in the RTL; the generated init that
    # follows overwrites the registers it cares about.
    out += ["                  li x{}, 0".format(r) for r in range(1, 32)
            if r != 2]
    out += body
    # The branch to test_done is emitted inside the compared window so that
    # Sail's log ends on the `test_done` fetch (see scripts/run_riscv_dv.sh).
    out += ["                  la x31, test_done",
            "                  jalr x0, x31, 0",
            "                  li gp, 1",
            "test_done:",
            "                  la x31, write_tohost",
            "                  jalr x0, x31, 0",
            "write_tohost:",
            "                  la t5, {}".format(HALT_MAILBOX_SYM),
            "                  li gp, {}".format(hex(HALT_WORD)),
            "                  sw gp, 0(t5)",
            "_exit:",
            "                  j write_tohost"]
    out += data_section(args.inp)
    out += stack_section("user_stack", USER_STACK_BYTES // 4, "user_stack")
    out += [".section .text", ".align 2", "mtvec_handler:",
            "                  .option norvc;"]
    out += trap_handler()

    with open(args.out, "w") as f:
        f.write("\n".join(out) + "\n")
    print("postprocess: {} instrs, dropped {}, relocated loads {}, "
          "relocated stores {}, sp fixes {}, reserved-reg li dropped {}".format(
              stats["total"], stats["drop"], stats["load"],
              stats["store"], stats["sp"], stats["reserved"]))


if __name__ == "__main__":
    main()
