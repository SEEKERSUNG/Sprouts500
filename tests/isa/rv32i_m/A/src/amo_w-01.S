# Sprouts500 directed RV32A test: the nine AMO*.W operations on DTCM and ITCM.
# Project-owned test (the upstream riscv-arch-test old-framework tree has no A
# suite), exercising old-value results and the memory side effect.
.section .text.init
.globl rvtest_entry_point
rvtest_entry_point:
    la   x6, begin_signature
    li   x1, 0x10000000
    li   x2, 5
    sw   x2, 0(x1)

    li   x3, 3
    amoadd.w  x4, x3, (x1)
    sw   x4, 0(x6)
    lw   x5, 0(x1)
    sw   x5, 4(x6)

    li   x3, 0x10
    amoswap.w x4, x3, (x1)
    sw   x4, 8(x6)
    lw   x5, 0(x1)
    sw   x5, 12(x6)

    li   x3, 0x0f
    amoxor.w  x4, x3, (x1)
    sw   x4, 16(x6)
    lw   x5, 0(x1)
    sw   x5, 20(x6)

    li   x3, 0x13
    amoand.w  x4, x3, (x1)
    sw   x4, 24(x6)
    lw   x5, 0(x1)
    sw   x5, 28(x6)

    li   x3, 0x40
    amoor.w   x4, x3, (x1)
    sw   x4, 32(x6)
    lw   x5, 0(x1)
    sw   x5, 36(x6)

    li   x3, -16
    amomin.w  x4, x3, (x1)
    sw   x4, 40(x6)
    lw   x5, 0(x1)
    sw   x5, 44(x6)

    li   x3, -16
    amomax.w  x4, x3, (x1)
    sw   x4, 48(x6)
    lw   x5, 0(x1)
    sw   x5, 52(x6)

    li   x3, 0x7fffffff
    amominu.w x4, x3, (x1)
    sw   x4, 56(x6)
    lw   x5, 0(x1)
    sw   x5, 60(x6)

    li   x3, 0x80000000
    amomaxu.w x4, x3, (x1)
    sw   x4, 64(x6)
    lw   x5, 0(x1)
    sw   x5, 68(x6)

    # ITCM (tag 0) atomics use the same memory as instruction fetch.
    la   x7, itcm_word
    lw   x5, 0(x7)
    sw   x5, 72(x6)
    li   x3, 0x11111111
    amoswap.w x4, x3, (x7)        # swap: destination is exact (0x12345678->0x11111111)
    sw   x4, 76(x6)
    lw   x5, 0(x7)
    sw   x5, 80(x6)

    li   t0, 0x003FFFF0
    li   t1, 0x5A5A5A5A
    sw   t1, 0(t0)
1:  j 1b

.section .data
.align 4
.globl begin_signature
begin_signature:
    .fill 21, 4, 0
itcm_word:
    .word 0x12345678
