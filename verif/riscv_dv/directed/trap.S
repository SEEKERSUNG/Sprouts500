# no user_define.h: the directed image is self-contained
.globl _start
.section .text
.option norvc;
_start:
                  la sp, user_stack_end
                  la x30, mtvec_handler
                  csrw 0x305, x30
                  la x31, init
                  csrw 0x341, x31
                  li x31, 0x1800
                  csrw 0x300, x31
                  mret
init:
                  li x1, 0
                  li x30, 0
                  li x31, 0
                  li x5, 0x1234
                  li x9, 0
                  .word 0xffffffff
                  csrr x6, 0x342
                  csrr x7, 0x341
                  csrr x8, 0x343
                  addi x5, x5, 1
                  addi x9, x9, 1
                  .word 0x0000007f
                  csrr x6, 0x342
                  csrr x7, 0x341
                  csrr x8, 0x343
                  addi x5, x5, 1
                  addi x9, x9, 1
                  .word 0x0000700b
                  csrr x6, 0x342
                  csrr x7, 0x341
                  csrr x8, 0x343
                  addi x5, x5, 1
                  addi x9, x9, 1
                  li x12, 0x10000002
                  lw x10, 0(x12)
                  li x12, 0x10000002
                  sw x10, 0(x12)
                  .align 2
                  la x13, target
                  addi x13, x13, 2
                  jalr x11, 0(x13)
                  nop
                  csrr x6, 0x342
                  csrr x8, 0x343
target:
                  li x14, 0x1234
                  la x31, test_done
                  jalr x0, x31, 0
test_done:
                  la x31, write_tohost
                  jalr x0, x31, 0
write_tohost:
                  la t5, halt_mailbox
                  li gp, 0x5a5a5a5a
                  sw gp, 0(t5)
_exit:
                  j write_tohost
.section .data
.section .user_stack,"aw",@progbits
.align 2
user_stack_start:
.rept 127
.4byte 0x0
.endr
user_stack_end:
.4byte 0x0
.section .text
.align 2
mtvec_handler:
                  .option norvc;
                  csrr x5, 0x342
                  csrr x6, 0x341
                  csrr x7, 0x343
                  la x4, trap_record
                  lw x8, 12(x4)
                  addi x8, x8, 1
                  sw x8, 12(x4)
                  sw x5, 0(x4)
                  sw x6, 4(x4)
                  sw x7, 8(x4)
                  addi x6, x6, 4
                  csrw 0x341, x6
                  mret
