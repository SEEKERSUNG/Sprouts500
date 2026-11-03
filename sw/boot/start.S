    .section .text.init
    .globl _start

_start:
    la      sp, _stack_top
    la      t0, _sbss
    la      t1, _ebss
1:  bgeu    t0, t1, 2f
    sw      zero, 0(t0)
    addi    t0, t0, 4
    j       1b
2:  call    boot_main
3:  j       3b

/* Hand over in M-mode with interrupts disabled.  mret also redirects fetch
 * and clears the RAS, so no Bootloader return predictions leak into the app. */
    .globl boot_jump
    .type boot_jump, @function
boot_jump:
    csrw    mepc, a0
    fence.i
    mret
