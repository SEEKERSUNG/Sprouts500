"""IF-stage branch prediction (rtl/core.v ``Branch prediction``).

The BTB is 64 entries (``BTB_IBITS=6``): index ``IF_PC[6:1]``, tag
``IF_PC[31:7]``, one 2-bit saturating direction counter per entry.  The lookup
runs on registered ``IF_PC`` and its result (``IF_pred_taken_q``/
``IF_pred_target_q``) travels with the instruction through ID/EX.  In EX the
resolution/update logic produces ``ex_pred_mispredict`` (redirect) and
``btb_we`` (train).  The behavioural contract that these cases pin down:

* an entry is trained only by an ``ex_ctrl_resolved`` control instruction
  (``EX_valid && ex_ctrl_class && !ex_exception && !trap_taken &&
  !pipe_freeze``); ``trap_taken``/``mret_taken`` win over the mispredict
  redirect, so a trap (exception or interrupt) cancels the training write and
  the mispredict redirect for that instruction;
* unconditional jumps train the counter to ``2'b11`` and start from any
  counter value, a taken branch from an invalid entry starts at ``2'b10`` and
  a not-taken branch decrements (an invalid entry is not inserted);
* ``ex_pred_mispredict`` asserts when a taken control instruction was not
  predicted or its target differs, or when a predicted-taken slot turns out
  not to be a taken control instruction (stale non-control / wrong target);
* every ``halt`` ends with ``jal x0, 0``, which cold-mispredicts once; the
  assertions below therefore look at the case's own PCs.

Cases (``TB_CASE``)
-------------------
loop_taken         countdown ``bne`` loop: cold mispredict, then correct
                   predictions, loop-exit mispredict, saturated counter.
alternating        direction depends on the iteration parity: the 2-bit
                   counter oscillates and every direction change redirects.
nested_loop        inner loop inside an outer loop plus a ``jal``/``jalr``
                   call: trace equality, small mispredict bound.
self_loop          a self-target ``jal x0, 0`` spin: first execution
                   mispredicts, later ones are predicted correctly and IF
                   stays on the loop PC.
exception_no_train misaligned taken branch: trap, no training, no redirect.
interrupt_no_train predicted-taken branch interrupted in EX: mepc = branch PC,
                   no training; after ``mret`` the branch re-executes.
jalr_target_change the same ``jalr`` alternates between two targets: cold
                   mispredict then target-change mispredicts.
stale_noncontrol   a ``jal`` word in ITCM data is executed, then patched into a
                   non-control instruction: the stale prediction redirects
                   and the fall-through slot retires.
stall_freeze       a predicted-taken branch behind a load-use stall and in
                   front of a ``div``: no mispredict while ``pipe_freeze``.
"""

import pathlib
import sys

import cocotb
import pytest
from cocotb.triggers import Timer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _helpers import (  # noqa: E402
    DTCM_BASE, DTCM_WORDS, ITCM_DATA_BASE, ITCM_WORDS, Asm, CoreHarness,
    RV32Interp, check_trace, enc_amo, enc_b, enc_i, enc_j, expect, tb_case,
)
from _runner import run_test  # noqa: E402

Z_OFF = 0x100                          # stale_noncontrol data word in ITCM


def _init_regs(a):
    """Give every integer register a known zero (untouched regs read X).

    x12/x13 are zeroed explicitly: the ``div``/``rem`` opcodes (used by the
    stall_freeze case) read x12..x15 as rs1/rs2, which are otherwise X with
    Icarus even though the instruction is on a path that never retires.
    """
    for r in range(1, 30):
        a.addi(r, 0, 0)
    a.addi(12, 0, 0)
    a.addi(13, 0, 0)


def _zero_dtcm():
    return [0] * DTCM_WORDS


def _zero_image(words):
    return list(words) + [0] * (ITCM_WORDS - len(words))


def _halt_pc(words):
    """Address of the halt store (``sw HALT_MAGIC``) in a zero-filled image."""
    for idx, w in enumerate(words):
        if (w & 0x7F) == 0x23 and (w >> 20) & 0x1F == 30 and ((w >> 12) & 7) == 2:
            return 4 * idx
    raise AssertionError("no halt store in the image")


class Watcher:
    """Cycle-level prediction log (mispredicts, trap cycle, freeze overlap)."""

    def __init__(self):
        self.mispredicts = []          # (cycle, pc, pred, actual, target)
        self.misp_extra = []           # (cycle, pc, btb_we, redirect_pc)
        self.trap = None               # (cycle, pc, pred, actual, misp, we, rpc)
        self.freeze_redirect = 0       # ex_pred_mispredict && pipe_freeze
        self.we_freeze = 0             # btb_we && pipe_freeze

    def hook(self, h):
        if h.ex_pred_mispredict:
            self.mispredicts.append((h.cycle, h.EX_PC, h.EX_pred_taken,
                                     h.ex_ctrl_actual, h.ex_target))
            self.misp_extra.append((h.cycle, h.EX_PC, h.btb_we,
                                    h.redirect_pc))
            if h.pipe_freeze:
                self.freeze_redirect += 1
        if h.btb_we and h.pipe_freeze:
            self.we_freeze += 1
        if h.trap_taken and self.trap is None:
            self.trap = (h.cycle, h.EX_PC, h.EX_pred_taken, h.ex_ctrl_actual,
                         h.ex_pred_mispredict, h.btb_we, h.redirect_pc)

    def at(self, pc):
        return [m for m in self.mispredicts if m[1] == pc]


def _base_stats():
    return {"raw_stall": 0, "IF_ID_hold": 0, "pipe_freeze": 0, "div_busy": 0,
            "trap_taken": 0, "btb_we": 0, "id_flush_q": 0,
            "ex_pred_mispredict": 0}


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------
def build_loop_taken(seed=0):
    n_iter = 5
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(1, 0)
    a.li(3, n_iter)
    a.label("head")
    head = a.here()
    a.addi(2, 2, 1)
    a.addi(1, 1, 1)
    bp = a.here()
    a.bne(1, 3, "head")
    a.halt()
    words = a.assemble()
    return words, {"head": head, "bp": bp, "n_iter": n_iter,
                   "spin": a.halt_loop_pc,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_alternating(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(8, 0)                          # iteration counter
    a.li(11, 9)                         # loop bound
    a.label("head")
    head = a.here()
    a.addi(8, 8, 1)
    a.andi(9, 8, 1)                     # 1 on odd iterations, 0 on even
    bp = a.here()
    a.beq(9, 0, "alt_t")                # taken on even iterations
    a.addi(7, 7, 1)
    a.j("alt_n")
    a.label("alt_t")
    a.addi(7, 7, 2)
    a.label("alt_n")
    a.blt(8, 11, "head")
    a.halt()
    words = a.assemble()
    return words, {"head": head, "bp": bp, "bound": 11,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_nested_loop(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.jal(5, "fn")
    a.label("back")
    a.li(2, 0)
    a.li(4, 0)
    a.li(3, 3)                          # inner limit
    a.li(8, 3)                          # outer limit
    a.label("outer")
    outer = a.here()
    a.li(6, 0)
    a.label("inner")
    inner = a.here()
    a.addi(6, 6, 1)
    a.addi(2, 2, 1)
    inner_br = a.here()
    a.bne(6, 3, "inner")
    a.addi(4, 4, 1)
    outb = a.here()
    out_br = a.here()
    a.blt(4, 8, "outer")
    a.halt()
    a.label("fn")
    fn = a.here()
    a.addi(7, 7, 1)
    jr = a.here()
    a.jalr(0, 5, 0)
    words = a.assemble()
    return words, {"outer": outer, "inner": inner, "outb": outb, "fn": fn,
                   "jr": jr, "inner_br": inner_br, "out_br": out_br,
                   "back": a.labels["back"],
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_self_loop(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(2, 0)
    a.li(3, 6)
    a.label("head")
    head = a.here()
    a.addi(2, 2, 1)
    a.bne(2, 3, "head")
    a.halt()
    spin = a.halt_loop_pc
    words = a.assemble()
    return words, {"head": head, "spin": spin,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_exception_no_train(seed=0):
    """A faulting straddling instruction must not train the BTB/redirect.

    With IALIGN=16 a 2-mod-4 branch target is legal, so the old misaligned
    branch no longer faults.  A 32-bit instruction starting at PC[1]=1 is
    assembled from the next word; placing 0xFFFF there with a zero next word
    makes a legal-width (32-bit), illegal-encoding instruction at a 2-mod-4
    PC.  It exercises the straddle detect plus the trap path.
    """
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.la(28, "handler")
    a.csrw(0x305, 28)
    a.li(2, 0)
    a.label("head")
    head = a.here()
    a.addi(2, 2, 1)
    a.li(3, 3)
    bp = a.here()
    a.bne(2, 3, "head")
    a.label("hb")
    hb = a.here()
    a.cword([0x0001])                   # C.NOP
    a.raw16(0xFFFF)                     # low half of 0x0000FFFF
    amo = a.here() - 2                  # 2-mod-4 PC of the straddling word
    a.raw(0x0000_0000)                  # high half: {16'b0, 0xFFFF} = illegal
    a.label("resume")
    resume = a.here()
    a.addi(4, 4, 1)                     # first instruction after the trap
    a.label("t")
    tgt = a.here()
    a.addi(7, 7, 1)
    a.halt()
    a.label("handler")
    hand = a.here()
    a.csrr(20, 0x342)                   # mcause
    a.csrr(21, 0x341)                   # mepc
    a.la(21, "resume")                  # the straddler's bytes are not code
    a.csrw(0x341, 21)
    a.mret()
    words = a.assemble()
    return words, {"head": head, "bp": bp, "hb": hb, "amo": amo, "tgt": tgt,
                   "resume": resume, "hand": hand,
                   "halt_pc": _halt_pc(words), "dtcm": dtcm}


def build_interrupt_no_train(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(19, 0)                         # handler entry counter
    a.li(22, DTCM_BASE)                 # handler record pointer
    a.la(5, "handler")
    a.csrw(0x305, 5)                    # mtvec
    a.li(5, 0)
    a.csrw(0x304, 5)
    a.li(5, 0x80)                       # mie: timer IRQ
    a.csrs(0x304, 5)
    a.li(5, 0x8)                        # mstatus: MIE
    a.csrs(0x300, 5)
    a.li(5, ITCM_DATA_BASE + 0x40)
    a.li(8, 1)
    a.li(3, 3)
    a.li(2, 0)
    a.label("head")
    head = a.here()
    a.lw(6, 5, 0)                       # load-use producer for the branch
    bp = a.here()
    a.bne(6, 8, "target")
    a.addi(10, 10, 1)
    a.label("target")
    tgt = a.here()
    a.addi(2, 2, 1)
    a.beq(2, 3, "out")
    a.j("head")
    a.label("out")
    a.halt()
    a.label("handler")
    hand = a.here()
    a.csrr(20, 0x342)                   # mcause
    a.csrr(21, 0x341)                   # mepc (branch PC: it re-executes)
    a.sw(20, 22, 0)
    a.sw(21, 22, 4)
    a.addi(22, 22, 8)
    a.addi(19, 19, 1)
    a.mret()
    words = a.assemble()
    return words, {"head": head, "bp": bp, "target": tgt, "hand": hand,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_jalr_target_change(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(5, DTCM_BASE)
    a.label("loop")
    lp = a.here()
    a.lw(6, 5, 0)                       # target address from DTCM
    jr = a.here()
    a.jalr(0, 6, 0)
    poison = a.here()
    a.addi(10, 10, 1)                   # fetched behind the jalr, never retires
    t1 = a.here()
    a.addi(1, 1, 1)
    a.addi(5, 5, 4)
    a.j("loop")
    t2 = a.here()
    a.addi(2, 2, 1)
    a.halt()
    words = a.assemble()
    dtcm[0] = t1
    dtcm[1] = t2
    return words, {"loop": lp, "jr": jr, "poison": poison, "t1": t1, "t2": t2,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_stale_noncontrol(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    z = ITCM_DATA_BASE + Z_OFF
    a.li(21, z)
    a.label("call1")
    c1 = a.here()
    a.jalr(0, 21, 0)                    # execute the data word at Z: jal x0,T
    a.label("exit")
    exit_pc = a.here()
    a.li(3, 3)                          # executed only via T
    a.li(20, enc_i(0x13, 22, 0, 22, 1)) # addi x22, x22, 1
    patch = a.here()
    a.sw(20, 21, 0)                     # patch Z into addi x22,x22,1
    a.jalr(0, 21, 0)                    # second pass through Z (stale entry)
    after2 = a.here()
    a.halt()
    a.label("T")
    t_pc = a.here()
    a.addi(3, 3, 1)
    a.j("exit")
    words = a.assemble()
    z_words = [enc_j(0x6F, 0, t_pc - z),     # jal x0, T
               enc_j(0x6F, 0, after2 - (z + 4))]  # j after2
    return words, {"z": z, "t": t_pc, "exit": exit_pc, "patch": patch,
                   "after2": after2, "c1": c1, "z_words": z_words,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_stall_freeze(seed=0):
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(5, ITCM_DATA_BASE + 0x40)
    a.li(4, 2)
    a.li(9, 2)
    a.label("head")
    head = a.here()
    a.addi(4, 4, -1)
    a.lw(6, 5, 0)                       # distance-0 load-use producer for bne
    bp = a.here()
    a.bne(4, 6, "head")                 # taken (4 -> 1, then 0; 6 = 0)
    a.div(12, 6, 9)                     # divider freeze behind the branch
    a.addi(13, 12, 0)
    a.halt()
    words = a.assemble()
    return words, {"head": head, "bp": bp,
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


def build_ras_calls(seed=0):
    """Shared/nested returns use the RAS; bad targets still redirect in EX."""
    dtcm = _zero_dtcm()
    a = Asm()
    _init_regs(a)
    a.li(8, 2)
    a.label("loop")
    a.jal(1, "fn1")
    a.addi(2, 2, 1)
    a.jal(1, "fn1")                  # same return, different link value
    a.addi(3, 3, 1)
    a.jal(5, "fn5")                  # alternate architectural link register
    a.addi(4, 4, 1)
    a.addi(8, 8, -1)
    a.bne(8, 0, "loop")
    a.jal(1, "outer")
    a.addi(9, 9, 1)
    a.jal(1, "bad_fn")

    a.label("fn1")
    a.addi(7, 7, 1)
    ret1 = a.here()
    a.jalr(0, 1, 0)
    a.label("fn5")
    a.addi(10, 10, 1)
    ret5 = a.here()
    a.jalr(0, 5, 0)

    a.label("outer")
    a.addi(5, 1, 0)                  # save the outer link across the inner call
    a.addi(11, 11, 1)
    a.jal(1, "inner")
    a.addi(12, 12, 1)
    outer_ret = a.here()
    a.jalr(0, 5, 0)
    a.label("inner")
    a.addi(13, 13, 1)
    inner_ret = a.here()
    a.jalr(0, 1, 0)

    a.label("bad_fn")
    a.la(1, "good")                  # RAS top is deliberately wrong
    bad_ret = a.here()
    a.jalr(0, 1, 0)
    a.label("good")
    a.la(1, "empty_target")
    empty_ret = a.here()              # empty stack: BTB fallback, no underflow
    a.jalr(0, 1, 0)
    a.label("empty_target")
    a.halt()
    words = a.assemble()
    return words, {"ret1": ret1, "ret5": ret5, "outer_ret": outer_ret,
                   "inner_ret": inner_ret, "bad_ret": bad_ret,
                   "empty_ret": empty_ret, "good": a.labels["good"],
                   "empty_target": a.labels["empty_target"],
                   "halt_pc": a.halt_store_pc, "dtcm": dtcm}


BUILDERS = {
    "loop_taken": build_loop_taken,
    "alternating": build_alternating,
    "nested_loop": build_nested_loop,
    "self_loop": build_self_loop,
    "exception_no_train": build_exception_no_train,
    "interrupt_no_train": build_interrupt_no_train,
    "jalr_target_change": build_jalr_target_change,
    "stale_noncontrol": build_stale_noncontrol,
    "stall_freeze": build_stall_freeze,
    "ras_calls": build_ras_calls,
}

CASES = tuple(BUILDERS)


# ---------------------------------------------------------------------------
# case checks: called after a run; every assertion states the measured reason
# ---------------------------------------------------------------------------
def check_loop_taken(h, w, pcs):
    bp, head, n = w["bp"], w["head"], w["n_iter"]
    at = w["watcher"].at(bp)
    expect(len(at) == 2,
           f"loop_taken: {len(at)} mispredicts at the loop branch 0x{bp:08x}, "
           f"expected 2 (one cold + one loop-exit): {at}")
    cold, exit_ = at
    expect(cold[2] == 0 and cold[3] == 1,
           f"loop_taken: the cold mispredict has EX_pred_taken={cold[2]} "
           f"ex_ctrl_actual={cold[3]}, expected 0/1 (empty BTB)")
    expect(exit_[2] == 1 and exit_[3] == 0,
           f"loop_taken: the second mispredict has EX_pred_taken={exit_[2]} "
           f"ex_ctrl_actual={exit_[3]}, expected 1/0 (loop exit)")
    # Only the cold mispredict and the exit redirect may be mispredicts for
    # the loop; the halt ``jal x0, 0`` spin is the only other one.
    others = [m for m in w["watcher"].mispredicts
              if m[1] not in (bp, w["spin"])]
    expect(not others, f"loop_taken: unexpected mispredicts {others}")
    expect(h.btb_find(bp) == (True, head, 2),
           f"loop_taken: BTB entry for 0x{bp:08x} is {h.btb_find(bp)}, "
           f"expected (True, 0x{head:08x}, cnt=2) after {n} iterations "
           f"(the exit decrement leaves 2'b10)")
    # The counter must have saturated while the loop was still taking.
    expect(w["bp_cnt"] and max(w["bp_cnt"]) == 3,
           f"loop_taken: BTB counter samples at the branch {w['bp_cnt']}, "
           f"expected it to reach the saturated 2'b11")
    cnt = h.btb_find(bp)[2]
    expect(cnt in (1, 2, 3), f"loop_taken: BTB counter {cnt} outside 2 bits")
    expect(w["stats"]["raw_stall"] == 0,
           f"loop_taken: {w['stats']['raw_stall']} raw_stall cycles in an "
           f"integer-only loop")
    expect(w["stats"]["id_flush_q"] == 3,
           f"loop_taken: id_flush_q high {w['stats']['id_flush_q']} cycles, "
           f"expected 3 (cold + exit + halt spin redirects), "
           f"mispredicts={w['stats']['ex_pred_mispredict']}")


def check_alternating(h, w, pcs):
    bp = w["bp"]
    at = w["watcher"].at(bp)
    expect(len(at) >= 2,
           f"alternating: {len(at)} mispredicts at the parity branch "
           f"0x{bp:08x}; the 2-bit counter should disagree with the "
           f"alternating direction repeatedly")
    seq = [(m[2], m[3]) for m in at]
    expect(any(p == 1 and a == 0 for p, a in seq) and
           any(p == 0 and a == 1 for p, a in seq),
           f"alternating: mispredicts never covered both directions ({seq})")
    hit, _target, cnt = h.btb_find(bp)
    expect(hit and cnt is not None and 0 <= cnt <= 3,
           f"alternating: BTB entry for 0x{bp:08x} is "
           f"{h.btb_find(bp)}, counter left the 2-bit range")
    expect(h.reg(2) == 0, f"alternating: x2=0x{h.reg(2)!r}")


def check_nested_loop(h, w, pcs):
    bp = w["stats"]
    at_inner = w["watcher"].at(w["inner_br"])
    at_out = w["watcher"].at(w["out_br"])
    expect(at_inner, f"nested_loop: the inner bne 0x{w['inner_br']:08x} never "
                     f"mispredicted (cold)")
    expect(at_out, f"nested_loop: the outer blt 0x{w['out_br']:08x} never "
                   f"mispredicted (cold)")
    # Measured: jal cold, jalr cold, inner cold + 3 exits, outer cold + exit,
    # halt spin.  The case's own control instructions (minus the spin) bound
    # the training redirects.
    total = len(w["watcher"].mispredicts)
    expect(total <= 9,
           f"nested_loop: {total} mispredicts over the call + 3x3 loop nest, "
           f"bound is 9 (measured)")
    expect(bp["raw_stall"] == 0,
           f"nested_loop: {bp['raw_stall']} raw_stall cycles")
    expect(bp["btb_we"] >= 10,
           f"nested_loop: btb_we high only {bp['btb_we']} cycles; the "
           f"control instructions were not trained")
    expect(h.btb_find(w["jr"])[0] and h.btb_find(w["outb"])[0],
           f"nested_loop: jalr/outer-loop entries invalid "
           f"({h.btb_find(w['jr'])}, {h.btb_find(w['outb'])})")
    expect(h.reg(7) == 1 and h.reg(2) == 9 and h.reg(4) == 3,
           f"nested_loop: x7={h.reg(7)} x2={h.reg(2)} x4={h.reg(4)}, expected "
           f"1/9/3")


def check_self_loop(h, w, pcs):
    spin = w["spin"]
    at = w["watcher"].at(spin)
    expect(len(at) == 1,
           f"self_loop: {len(at)} mispredicts at the self-target spin "
           f"0x{spin:08x}, expected exactly 1 (cold)")
    m = at[0]
    if True:
        print("DBG self_loop first:", m)
    expect(m[2] == 0 and m[3] == 1 and m[4] == spin,
           f"self_loop: spin mispredict pred={m[2]} act={m[3]} target="
           f"0x{m[4]:08x}, expected 0/1/0x{spin:08x}")
    # After the cold redirect IF stays on the loop PC: same-cycle lookup sees
    # the newly trained entry and the spin is predicted forever.
    expect(w["spin_cycles"] >= 90,
           f"self_loop: IF_PC_q was on the spin PC for only "
           f"{w['spin_cycles']} of the sample window (run tail + 100 cycles), "
           f"expected it to stay there")
    expect(w["spin_mispredicts_after"] == 0,
           f"self_loop: {w['spin_mispredicts_after']} further spin "
           f"mispredicts; a self loop must be predicted after training")
    expect(h.btb_find(spin) == (True, spin, 3),
           f"self_loop: BTB entry for the spin is {h.btb_find(spin)}, "
           f"expected (True, 0x{spin:08x}, cnt=3) (unconditional -> saturated)")


def check_exception_no_train(h, w, pcs):
    amo = w["amo"]
    t = w["watcher"].trap
    expect(t is not None, "exception_no_train: no trap taken")
    cyc, pc, pred, act, misp, we, rpc = t
    expect(pc == amo, f"exception_no_train: trap at 0x{pc:08x}, expected the "
                      f"straddling instruction 0x{amo:08x}")
    expect(misp == 0,
           "exception_no_train: the faulting instruction asserted "
           "ex_pred_mispredict; a trap must cancel the prediction redirect")
    expect(we == 0,
           "exception_no_train: btb_we high in the trap cycle; a trapping "
           "instruction must not train the BTB")
    expect(w["watcher"].at(amo) == [],
           f"exception_no_train: mispredict recorded at the faulting slot "
           f"{w['watcher'].at(amo)}")
    hit, _target, _cnt = h.btb_find(amo)
    expect(not hit,
           f"exception_no_train: BTB entry for 0x{amo:08x} became valid "
           f"{h.btb_find(amo)}")
    expect(h.mcause == 2, f"exception_no_train: mcause=0x{h.mcause!r}")
    expect(h.mtval == 0x0000_FFFF,
           f"exception_no_train: mtval=0x{h.mtval!r}, expected 0x0000ffff "
           f"(the zero-extended straddling instruction)")
    expect(h.mepc == w["resume"],
           f"exception_no_train: final mepc=0x{h.mepc:08x}, expected the "
           f"resume label 0x{w['resume']:08x}")
    expect(w["stats"]["trap_taken"] == 1,
           f"exception_no_train: {w['stats']['trap_taken']} trap cycles")
    expect(h.reg(4) == 1,
           f"exception_no_train: x4={h.reg(4)}: the handler did not resume "
           f"at the instruction after the fault")
    expect(h.reg(7) == 1,
           f"exception_no_train: x7={h.reg(7)}: execution did not continue "
           f"past the resume point")


def check_interrupt_no_train(h, w, pcs):
    bp = w["bp"]
    t = w["watcher"].trap
    expect(t is not None,
           "interrupt_no_train: the timer IRQ never caused a trap")
    cyc, pc, pred, act, misp, we, rpc = t
    expect(pc == bp,
           f"interrupt_no_train: trap at 0x{pc:08x}, expected the predicted "
           f"branch in EX 0x{bp:08x}")
    expect(pred == 1,
           f"interrupt_no_train: the interrupted branch had "
           f"EX_pred_taken={pred}, expected 1")
    expect(misp == 0 and we == 0,
           f"interrupt_no_train: trap cycle mispredict={misp} btb_we={we}; "
           f"an interrupt must cancel prediction feedback")
    expect(w["stats"]["trap_taken"] == 1,
           f"interrupt_no_train: {w['stats']['trap_taken']} trap cycles")
    expect(h.dtcm(0) == 0x8000_0007,
           f"interrupt_no_train: handler mcause=0x{h.dtcm(0)!r}, expected "
           f"0x80000007 (timer)")
    expect(h.dtcm(1) == bp,
           f"interrupt_no_train: handler mepc=0x{h.dtcm(1)!r}, expected the "
           f"branch PC 0x{bp:08x}")
    expect(h.reg(19) == 1, f"interrupt_no_train: x19={h.reg(19)} handler "
                           f"entries, expected 1")
    expect(h.btb_find(bp) == (True, w["target"], 3),
           f"interrupt_no_train: after mret/re-execution the entry is "
           f"{h.btb_find(bp)}, expected (True, 0x{w['target']:08x}, cnt=3)")
    expect(h.reg(10) == 0 and h.reg(2) == 3,
           f"interrupt_no_train: x10={h.reg(10)} x2={h.reg(2)}; the branch "
           f"re-executed after mret and x10 is only incremented on the "
           f"fall-through")


def check_jalr_target_change(h, w, pcs):
    jr = w["jr"]
    at = w["watcher"].at(jr)
    expect(len(at) == 2,
           f"jalr_target_change: {len(at)} mispredicts at 0x{jr:08x}, "
           f"expected 2 (cold + target change): {at}")
    cold, change = at
    expect(cold[2] == 0 and cold[3] == 1 and cold[4] == w["t1"],
           f"jalr_target_change: cold mispredict {cold}, expected pred 0, "
           f"actual 1, target 0x{w['t1']:08x}")
    expect(change[2] == 1 and change[3] == 1 and change[4] == w["t2"],
           f"jalr_target_change: target-change mispredict {change}, expected "
           f"pred 1, actual 1, target 0x{w['t2']:08x}")
    expect(h.btb_find(jr) == (True, w["t2"], 3),
           f"jalr_target_change: final entry {h.btb_find(jr)}, expected "
           f"(True, 0x{w['t2']:08x}, cnt=3)")
    # IF fetches the jalr fall-through while the jalr (predicted not-taken)
    # travels to EX; the redirect flushes it.  It may retire only after the
    # very first, cold jalr, whose sequence contains the fetch of this slot.
    expect(pcs.count(w["poison"]) <= 1,
           f"jalr_target_change: fall-through 0x{w['poison']:08x} retired "
           f"{pcs.count(w['poison'])} times; a taken jalr must flush it")
    expect(h.reg(1) == 1 and h.reg(2) == 1,
           f"jalr_target_change: x1={h.reg(1)} x2={h.reg(2)}, expected 1/1")


def check_stale_noncontrol(h, w, pcs):
    z = w["z"]
    at = w["watcher"].at(z)
    expect(len(at) == 2,
           f"stale_noncontrol: {len(at)} mispredicts at the data word "
           f"0x{z:08x}, expected 2 (cold jal pass + stale non-control pass): "
           f"{w['watcher'].mispredicts}")
    cold, stale = at
    expect(cold[2] == 0 and cold[3] == 1 and cold[4] == w["t"],
           f"stale_noncontrol: first pass {cold}, expected pred 0, actual 1, "
           f"target 0x{w['t']:08x} (cold jal from the data word)")
    expect(stale[2] == 1 and stale[3] == 0,
           f"stale_noncontrol: stale mispredict pred={stale[2]} "
           f"act={stale[3]}, expected 1/0 (predicted taken, not a control "
           f"instruction)")
    extra = [e for e in w["watcher"].misp_extra if e[1] == z]
    expect(len(extra) == 2, f"stale_noncontrol: mispredict extras {extra}")
    expect(extra[1][2] == 0,
           f"stale_noncontrol: btb_we={extra[1][2]} in the stale redirect "
           f"cycle; a non-control slot must not re-train")
    expect(extra[1][3] == z + 4,
           f"stale_noncontrol: redirect target 0x{extra[1][3]!r}, expected "
           f"the fall-through 0x{z + 4:08x}")
    expect(extra[0][2] == 1,
           f"stale_noncontrol: the cold jal pass did not train the entry "
           f"(btb_we={extra[0][2]})")
    expect(pcs.count(z) == 2,
           f"stale_noncontrol: the data word 0x{z:08x} retired "
           f"{pcs.count(z)} time(s), expected 2 (cold jal + stale pass)")
    second = len(pcs) - 1 - pcs[::-1].index(z)
    expect(pcs[second + 1] == z + 4,
           f"stale_noncontrol: after the stale pass the core executed "
           f"0x{pcs[second + 1]:08x}, expected the fall-through 0x{z + 4:08x}")
    expect(pcs.count(w["t"]) == 1,
           f"stale_noncontrol: target 0x{w['t']:08x} retired "
           f"{pcs.count(w['t'])} time(s), expected 1 (first pass only)")
    expect(w["t"] not in pcs[second:],
           f"stale_noncontrol: the stale target 0x{w['t']:08x} executed "
           f"after the patched pass")
    expect(h.btb_find(z) == (True, w["t"], 3),
           f"stale_noncontrol: entry {h.btb_find(z)} changed by the "
           f"non-control redirect; expected (True, 0x{w['t']:08x}, cnt=3)")
    expect(h.reg(22) == 1,
           f"stale_noncontrol: x22={h.reg(22)}, expected 1 (patched addi)")
    expect(h.reg(3) == 3,
           f"stale_noncontrol: x3={h.reg(3)}, expected 3 (T executed exactly "
           f"3 times)")


def check_stall_freeze(h, w, pcs):
    bp = w["bp"]
    stats = w["stats"]
    at = w["watcher"].at(bp)
    expect(len(at) == 2,
           f"stall_freeze: {len(at)} mispredicts at 0x{bp:08x}, expected 2 "
           f"(cold taken + final not-taken): {at}")
    expect(w["watcher"].freeze_redirect == 0,
           f"stall_freeze: ex_pred_mispredict high in "
           f"{w['watcher'].freeze_redirect} pipe_freeze cycle(s)")
    expect(w["watcher"].we_freeze == 0,
           f"stall_freeze: btb_we high in {w['watcher'].we_freeze} "
           f"pipe_freeze cycle(s)")
    expect(stats["pipe_freeze"] > 0,
           "stall_freeze: pipe_freeze never observed (no div)")
    expect(stats["raw_stall"] > 0, "stall_freeze: no load-use stall")
    expect(w["held_in_id"] >= 1,
           f"stall_freeze: the branch was held in ID during only "
           f"{w['held_in_id']} stall cycle(s); the load-use stall was not "
           f"combined with the branch")
    expect(h.reg(4) == 0 and h.reg(12) == 0 and h.reg(13) == 0,
           f"stall_freeze: x4={h.reg(4)} x12={h.reg(12)} x13={h.reg(13)}, "
           f"expected 0/0/0 (6/2, 0/2)")


def check_ras_calls(h, w, pcs):
    for name in ("ret1", "ret5", "outer_ret", "inner_ret"):
        at = w["watcher"].at(w[name])
        expect(at == [],
               f"ras_calls: {name} at 0x{w[name]:08x} mispredicted: {at}; "
               "a nonempty RAS must predict shared/nested returns")
    bad = w["watcher"].at(w["bad_ret"])
    expect(len(bad) == 1 and bad[0][2] == 1 and bad[0][3] == 1 and
           bad[0][4] == w["good"],
           f"ras_calls: bad return target was not corrected once in EX: {bad}")
    empty = w["watcher"].at(w["empty_ret"])
    expect(len(empty) == 1 and empty[0][4] == w["empty_target"],
           f"ras_calls: empty-stack return must use the normal BTB/EX path: {empty}")
    expect(int(h.core.ras_count.value) == 0,
           f"ras_calls: final RAS count={int(h.core.ras_count.value)}, expected 0")
    expect(h.reg(2) == 2 and h.reg(3) == 2 and h.reg(4) == 2 and h.reg(7) == 4,
           f"ras_calls: call-site results x2={h.reg(2)} x3={h.reg(3)} "
           f"x4={h.reg(4)} x7={h.reg(7)}, expected 2/2/2/4")
    expect(h.reg(9) == 1 and h.reg(10) == 2 and h.reg(12) == 1 and h.reg(13) == 1,
           "ras_calls: nested/x5 function bodies did not retire exactly as expected")


# ---------------------------------------------------------------------------
# per-case execution (trace comparison + cycle-level checks)
# ---------------------------------------------------------------------------
async def _run_ref(dut, case, words, info, max_cycles=20_000):
    """Run a case against the interpreter and collect prediction stats."""
    image = _zero_image(words)
    dtcm = info["dtcm"]
    ref = RV32Interp(image, dtcm_words=dtcm).run()
    h = CoreHarness(dut)
    h.load_program(image, zero_rest=True)
    h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    await h.reset()
    stats = _base_stats()
    watcher = Watcher()
    extra = {"held_in_id": 0, "bp_cnt": []}

    def hook(hh):
        watcher.hook(hh)
        if case == "stall_freeze" and hh.raw_stall and hh.ID_valid \
                and hh.ID_PC == info.get("bp"):
            extra["held_in_id"] += 1
        if case == "self_loop":
            extra.setdefault("spin_cycles", 0)
            if info["spin"] is not None and hh.IF_PC_q == info["spin"]:
                extra["spin_cycles"] += 1
        if case == "loop_taken" and hh.EX_valid and hh.EX_PC == info["bp"]:
            extra["bp_cnt"].append(hh.btb_find(info["bp"])[2])

    trace = await h.run_with_trace(info["halt_pc"], max_cycles=max_cycles,
                                   stats=stats, cycle_hook=hook)
    pcs = [pc for pc, _ir in trace]
    check_trace(pcs, ref.trace, label=f"branch_pred {case} trace")
    h.compare_regs(ref.regs, ignore=(0,), label=f"branch_pred {case} regfile")
    h.compare_dtcm_words(ref.mem.dtcm_words(),
                         label=f"branch_pred {case} DTCM")
    dut._log.info("branch_pred case=%s stats=%s mis=%s", case, stats,
                  watcher.mispredicts)
    w = dict(info)
    w.update({"stats": stats, "watcher": watcher, "ref": ref, "pcs": pcs,
              "spin_cycles": extra.get("spin_cycles", 0),
              "held_in_id": extra["held_in_id"],
              "bp_cnt": extra["bp_cnt"]})
    return h, w


async def _case_loop_taken(dut):
    words, info = build_loop_taken(0)
    h, w = await _run_ref(dut, "loop_taken", words, info)
    check_loop_taken(h, w, w["pcs"])


async def _case_alternating(dut):
    words, info = build_alternating(0)
    h, w = await _run_ref(dut, "alternating", words, info)
    check_alternating(h, w, w["pcs"])


async def _case_nested_loop(dut):
    words, info = build_nested_loop(0)
    h, w = await _run_ref(dut, "nested_loop", words, info)
    check_nested_loop(h, w, w["pcs"])


async def _case_self_loop(dut):
    words, info = build_self_loop(0)
    spin = info["spin"]
    h, w = await _run_ref(dut, "self_loop", words, info)
    # Keep watching the spin for 100 cycles after the halt store retired; the
    # architecture is done, only IF/EX prediction activity is of interest.
    mis_after = len(w["watcher"].mispredicts)
    spin_cycles = w["spin_cycles"]
    for _ in range(100):
        await h.step()
        w["watcher"].hook(h)
        if h.IF_PC_q == spin:
            spin_cycles += 1
    w["spin_cycles"] = spin_cycles
    w["spin_mispredicts_after"] = len(w["watcher"].mispredicts) - mis_after
    expect(w["spin_cycles"] > 0,
           "self_loop: IF_PC_q never observed on the halt spin PC")
    check_self_loop(h, w, w["pcs"])


async def _case_exception_no_train(dut):
    # The reference interpreter intentionally does not model traps/handlers,
    # and the faulting slot is a straddling illegal instruction, so this case
    # is checked directly (trap watcher + architectural state) instead of
    # against an interpreter trace.
    words, info = build_exception_no_train(0)
    h = CoreHarness(dut)
    h.load_program(_zero_image(words), zero_rest=True)
    h.write_dtcm_words(0, info["dtcm"])
    await Timer(1, unit="ns")
    await h.reset()
    stats = _base_stats()
    watcher = Watcher()
    trace_core = await h.run_with_trace(info["halt_pc"], max_cycles=5000,
                                        stats=stats, cycle_hook=watcher.hook)
    pcs = [pc for pc, _ir in trace_core]
    w = dict(info)
    w.update({"stats": stats, "watcher": watcher, "pcs": pcs})
    dut._log.info("branch_pred exception_no_train: stats=%s trap=%s mis=%s",
                  stats, watcher.trap, watcher.mispredicts)
    check_exception_no_train(h, w, pcs)


async def _case_interrupt_no_train(dut):
    words, info = build_interrupt_no_train(0)
    bp, hand = info["bp"], info["hand"]
    hand_end = len(words) * 4
    # The reference interpreter does not model CSR/mret: patch the SYSTEM
    # words with NOPs for the trace oracle and drop the handler PCs.
    NOP = 0x0000_0013
    ref_words = [NOP if (w & 0x7F) == 0x73 else w for w in words]
    ref = RV32Interp(_zero_image(ref_words), dtcm_words=info["dtcm"]).run()
    h = CoreHarness(dut)
    h.load_program(_zero_image(words), zero_rest=True)
    h.write_dtcm_words(0, info["dtcm"])
    await Timer(1, unit="ns")
    await h.reset()
    h.set_irqs()
    stats = _base_stats()
    watcher = Watcher()
    st = {"bubbles": 0, "raised": False}

    def hook(hh):
        # Raise the timer IRQ while the MEM load forwards to the branch in ID;
        # the branch enters EX on the following cycle with irq_pending high.
        if not st["raised"] and hh.MEM_valid and hh.MEM_mem_read \
                and not hh.raw_stall and hh.ID_PC == bp:
            st["bubbles"] += 1
            if st["bubbles"] >= 2:
                st["raised"] = True
                hh.set_irqs(timer=1)
        watcher.hook(hh)

    def trap_hook():
        h.set_irqs()

    trace = await h.run_with_trace(info["halt_pc"], max_cycles=5000,
                                   stats=stats, cycle_hook=hook,
                                   trap_hook=trap_hook)
    core_pcs = [p for p, _ir in trace if not (hand <= p < hand_end)]
    check_trace(core_pcs, ref.trace, label="branch_pred interrupt_no_train trace")
    expect(st["raised"], "interrupt_no_train: the branch never reached a "
                         "load-use bubble in ID; the IRQ was not raised")
    w = dict(info)
    w.update({"stats": stats, "watcher": watcher, "pcs": core_pcs})
    dut._log.info("branch_pred interrupt_no_train: stats=%s trap=%s mis=%s "
                  "dtcm=%s", stats, watcher.trap, watcher.mispredicts,
                  [h.dtcm(i) for i in range(4)])
    check_interrupt_no_train(h, w, core_pcs)


async def _case_jalr_target_change(dut):
    words, info = build_jalr_target_change(0)
    image = _zero_image(words)
    dtcm = info["dtcm"]
    ref = RV32Interp(image, dtcm_words=dtcm).run()
    h = CoreHarness(dut)
    h.load_program(image, zero_rest=True)
    h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    await h.reset()
    stats = _base_stats()
    watcher = Watcher()
    poison_pcs = [0]

    def hook(hh):
        watcher.hook(hh)
        if hh.IF_PC == info["poison"]:
            poison_pcs[0] += 1

    trace = await h.run_with_trace(info["halt_pc"], max_cycles=5000,
                                   stats=stats, cycle_hook=hook)
    pcs = [pc for pc, _ir in trace]
    check_trace(pcs, ref.trace, label="branch_pred jalr_target_change trace")
    h.compare_regs(ref.regs, ignore=(0,),
                   label="branch_pred jalr_target_change regfile")
    check_jalr_target_change(h, dict(info, stats=stats, watcher=watcher,
                                     pcs=pcs), pcs)


async def _case_stale_noncontrol(dut):
    words, info = build_stale_noncontrol(0)
    z = info["z"]
    image = _zero_image(words)
    for i, zw in enumerate(info["z_words"]):
        off = z + 4 * i
        image[off // 4] = zw
    dtcm = info["dtcm"]
    ref = RV32Interp(image, dtcm_words=dtcm).run()
    h = CoreHarness(dut)
    h.load_program(words, zero_rest=True)       # leave the ITCM data as 0
    h.write_itcm_words(z // 4, info["z_words"])
    h.write_dtcm_words(0, dtcm)
    await Timer(1, unit="ns")
    await h.reset()
    stats = _base_stats()
    watcher = Watcher()
    trace = await h.run_with_trace(info["halt_pc"], max_cycles=5000,
                                   stats=stats, cycle_hook=watcher.hook)
    pcs = [pc for pc, _ir in trace]
    check_trace(pcs, ref.trace, label="branch_pred stale_noncontrol trace")
    h.compare_regs(ref.regs, ignore=(0,),
                   label="branch_pred stale_noncontrol regfile")
    w = dict(info)
    w.update({"stats": stats, "watcher": watcher, "pcs": pcs})
    dut._log.info("branch_pred stale_noncontrol: stats=%s mis=%s extra=%s",
                  stats, watcher.mispredicts, watcher.misp_extra)
    check_stale_noncontrol(h, w, pcs)


async def _case_stall_freeze(dut):
    words, info = build_stall_freeze(0)
    h, w = await _run_ref(dut, "stall_freeze", words, info)
    check_stall_freeze(h, w, w["pcs"])


async def _case_ras_calls(dut):
    words, info = build_ras_calls(0)
    h, w = await _run_ref(dut, "ras_calls", words, info)
    check_ras_calls(h, w, w["pcs"])


_RUNNERS = {
    "loop_taken": _case_loop_taken,
    "alternating": _case_alternating,
    "nested_loop": _case_nested_loop,
    "self_loop": _case_self_loop,
    "exception_no_train": _case_exception_no_train,
    "interrupt_no_train": _case_interrupt_no_train,
    "jalr_target_change": _case_jalr_target_change,
    "stale_noncontrol": _case_stale_noncontrol,
    "stall_freeze": _case_stall_freeze,
    "ras_calls": _case_ras_calls,
}


@pytest.mark.parametrize("case", CASES)
def test_branch_pred(case):
    run_test("test_branch_pred", seed=0, case=case)


@cocotb.test()
async def branch_pred(dut):
    case = tb_case("loop_taken")
    expect(case in _RUNNERS, f"unknown branch_pred case {case}")
    await _RUNNERS[case](dut)
