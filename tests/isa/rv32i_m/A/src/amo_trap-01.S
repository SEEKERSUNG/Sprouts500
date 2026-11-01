# Sprouts500 directed RV32A traps: misaligned and MMIO atomics fault with the
# architectural causes (LR: 4/5, AMO and SC: 6/7), and leave memory unchanged.
.section .text.init
.globl rvtest_entry_point
rvtest_entry_point:
    la   x31, handler
    csrw 0x305, x31
    la   x30, begin_signature
    la   x29, begin_signature
    li   x1, 0x10000000
    li   x2, 5
    sw   x2, 0(x1)

    li   x6, 0x10000002
    li   x5, 7
    amoadd.w x5, x5, (x6)        # cause 6, mtval = 0x10000002
    li   x7, 0x10000002
    lr.w x8, (x7)                # cause 4
    li   x9, 0x10000002
    sc.w x10, x5, (x9)           # cause 6

    li   x10, 0x20000000
    li   x5, 9
    amoadd.w x5, x5, (x10)       # cause 7
    li   x11, 0x20000000
    lr.w x12, (x11)              # cause 5
    li   x13, 0x20000000
    sc.w x14, x5, (x13)          # cause 7

    lw   x15, 0(x1)
    sw   x15, 48(x29)            # memory must still be 5

    li   t0, 0x003FFFF0
    li   t1, 0x5A5A5A5A
    sw   t1, 0(t0)
1:  j 1b

handler:
    csrr t1, 0x342
    sw   t1, 0(x30)
    csrr t1, 0x343
    sw   t1, 4(x30)
    addi x30, x30, 8
    csrr t1, 0x341
    addi t1, t1, 4
    csrw 0x341, t1
    mret

.section .data
.align 4
.globl begin_signature
begin_signature:
    .fill 7, 4, 0
