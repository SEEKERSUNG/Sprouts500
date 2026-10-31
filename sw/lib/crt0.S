    .section .text.init
    .globl _start

/* Startup for sw/lib/app.ld:
 *   - stack top is the end of DTCM (16 KiB at 0x1000_0000)
 *   - .bss lives in DTCM and is not preloaded, so zero it here
 *   - main()'s return value becomes the result code in the mailboxes
 *
 * gp is left untouched: build with -mcmodel=medany -msmall-data-limit=0.
 */
_start:
    la      sp, _stack_top

    la      t0, _sbss
    la      t1, _ebss
1:  bgeu    t0, t1, 2f
    sw      zero, 0(t0)
    addi    t0, t0, 4
    j       1b

2:  call    main
    call    soc_halt            /* a0 = main's return value; does not return */

3:  j       3b
