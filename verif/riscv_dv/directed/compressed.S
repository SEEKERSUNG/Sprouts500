# Directed RV32C lockstep image: integer/memory compressed forms, mixed
# 16/32-bit code, a 32-bit instruction assembled from a straddling lane 1,
# compressed control transfers and links, and an illegal compressed encoding.
.globl _start
.section .text
.option rvc
_start:
    la sp, user_stack_end
    la t0, handler
    csrw 0x305, t0
    li t0, 0x10000000
    c.li a0, 5
    c.addi a0, -1
    c.slli a0, 2
    c.srli a0, 1
    c.srai a0, 1
    c.mv s0, t0
    c.sw a0, 0(s0)
    c.lw a1, 0(s0)
    c.add a1, a0
    c.sub a1, a0
    c.xor a1, a0
    c.or a1, a0
    c.and a1, a0
    c.mv a2, a0
    c.addi16sp sp, 0x20
    c.addi4spn s1, sp, 0x10
    c.swsp a1, 8(sp)
    c.lwsp a4, 8(sp)
    c.li a5, 3
loop1:
    c.addi a5, -1
    c.bnez a5, loop1
    c.li a7, 2
    c.beqz a5, 1f
    j 2f
1:  j 2f
2:  la t1, sub1
    c.jalr t1
    # A 32-bit instruction whose low half is in lane 1 of this word:
    # {0x0093, 0x0050} = addi x1, x0, 5.  One IF bubble is expected.
    c.nop
    .2byte 0x0093
    .2byte 0x0050
    c.addi x1, 1
    # Illegal compressed (C.SLLI shamt[5]=1) -> cause 2, mtval=0x1086
    .2byte 0x1086
    li a6, 0x22
    la t2, test_done
    jalr x0, t2, 0
sub1:
    c.addi a5, 1
    c.jr x1
.option norvc
.p2align 2
handler:
    csrrs t3, 0x342, x0
    csrrs t4, 0x341, x0
    csrrs t5, 0x343, x0
    la t6, trap_record
    sw t3, 0(t6)
    sw t4, 4(t6)
    sw t5, 8(t6)
    addi t4, t4, 2
    csrw 0x341, t4
    mret
.option norvc
.p2align 2
test_done:
    j test_done
.section .user_stack,"aw",@progbits
.align 2
.rept 127
.4byte 0x0
.endr
user_stack_end:
.4byte 0x0
