# Directed RV32A lockstep image: all nine AMO*.W operations, LR/SC success,
# failed SC, and a bounded LR/SC retry loop.  The memory effect is observed by
# later loads, whose values are compared in the retirement trace; Sail does not
# log AMO writes in its --trace-mem stream (the RTL trace omits them too).
.globl _start
.section .text
.option norvc
_start:
    la sp, user_stack_end
    la x6, trap_record
    la x1, data_word
    li x2, 5
    sw x2, 0(x1)
    li x3, 3
    amoadd.w x4, x3, (x1)
    sw x4, 0(x6)
    lw x5, 0(x1)
    sw x5, 4(x6)
    li x7, 0x10
    amoswap.w x8, x7, (x1)
    sw x8, 8(x6)
    amomin.w x9, x2, (x1)
    li x10, -4
    amomax.w x11, x10, (x1)
    li x12, 0x0f
    amoxor.w x13, x12, (x1)
    li x14, 0x13
    amoand.w x15, x14, (x1)
    li x16, 0x40
    amoor.w x17, x16, (x1)
    li x18, 0x7fffffff
    amomaxu.w x19, x18, (x1)
    li x20, 0x80000000
    amominu.w x21, x20, (x1)
    lw x22, 0(x1)
    sw x22, 40(x6)
    # LR/SC: the constrained sequence must eventually succeed
    li x23, 0
    li x24, 0x1234
    li x25, 8
1:  lr.w x26, (x1)
    sc.w x27, x24, (x1)
    bnez x27, 2f
    addi x23, x23, 1
    j 3f
2:  addi x25, x25, -1
    bnez x25, 1b
3:  sw x27, 44(x6)
    sw x23, 48(x6)
    lw x28, 0(x1)
    sw x28, 52(x6)
    la x29, test_done
    jalr x0, x29, 0
test_done:
    j test_done
.section .region_0,"aw",@progbits
.align 4
data_word:
    .word 0
.section .user_stack,"aw",@progbits
.align 2
.rept 127
.4byte 0x0
.endr
user_stack_end:
.4byte 0x0
