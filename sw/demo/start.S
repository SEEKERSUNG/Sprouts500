    .section .text.init
    .globl _start
_start:
    li      sp, 0x00007FF0
    li      t0, 0x2000
    csrs    mstatus, t0
    call    main

    /* simulation halt mailbox (also a convenient reset loop) */
    li      t0, 0x00007FFC
    li      t1, 0x5A5A5A5A
    sw      t1, 0(t0)
1:  j       1b
