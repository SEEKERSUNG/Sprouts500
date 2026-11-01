# Sprouts500 directed RV32A test: LR.W/SC.W reservation semantics.
.section .text.init
.globl rvtest_entry_point
rvtest_entry_point:
    la   x30, handler
    csrw 0x305, x30
    la   x28, begin_signature
    li   x1, 0x10000000
    li   x2, 5
    sw   x2, 0(x1)

    lr.w x3, (x1)                 # 5
    sw   x3, 0(x28)
    li   x4, 0x11
    sc.w x5, x4, (x1)             # success -> 0
    sw   x5, 4(x28)
    lw   x7, 0(x1)
    sw   x7, 8(x28)

    sc.w x8, x4, (x1)             # no reservation -> 1
    sw   x8, 12(x28)
    lw   x7, 0(x1)
    sw   x7, 16(x28)

    lr.w x9, (x1)                 # 0x11
    li   x10, 0x22
    sw   x10, 0(x1)               # same-word store invalidates
    sc.w x11, x4, (x1)            # -> 1
    sw   x11, 20(x28)
    lw   x7, 0(x1)
    sw   x7, 24(x28)

    lr.w x12, (x1)                # 0x22
    li   x13, 0x33
    sw   x13, 4(x1)               # unrelated word: reservation survives
    sc.w x14, x4, (x1)            # -> 0
    sw   x14, 28(x28)
    lw   x7, 0(x1)
    sw   x7, 32(x28)

    lr.w x15, (x1)                # 0x11
    li   x16, 1
    amoadd.w x17, x16, (x1)       # same-word AMO invalidates
    sw   x17, 36(x28)              # old = 0x11
    lw   x7, 0(x1)
    sw   x7, 40(x28)               # 0x12
    sc.w x18, x4, (x1)            # -> 1
    sw   x18, 44(x28)

    lr.w x19, (x1)                # 0x12
    ecall                         # trap clears the reservation
    sc.w x20, x4, (x1)            # -> 1
    sw   x20, 48(x28)

    lr.w x21, (x1)                # 0x12
    addi x23, x1, 4
    sc.w x22, x13, (x23)          # different address -> 1
    sw   x22, 52(x28)
    lw   x7, 4(x1)
    sw   x7, 56(x28)
    lw   x7, 0(x1)
    sw   x7, 60(x28)

    li   t0, 0x003FFFF0
    li   t1, 0x5A5A5A5A
    sw   t1, 0(t0)
1:  j 1b

handler:
    csrr t1, 0x342
    csrr t2, 0x341
    addi t2, t2, 4
    csrw 0x341, t2
    mret

.section .data
.align 4
.globl begin_signature
begin_signature:
    .fill 16, 4, 0
