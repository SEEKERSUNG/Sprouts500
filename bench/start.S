    .section .text.init
    .globl _start

/* Startup for the benchmarks (bench/bench.ld):
 *   sp = top of DTCM, clear .bss, then run the benchmark main and halt with
 *   its return value in the result mailbox.
 */
_start:
    la      sp, _stack_top

    /* Enable mstatus.FS so any float code can use the RV32F unit. */
    li      t2, 0x2000
    csrs    mstatus, t2

    la      t0, _sbss
    la      t1, _ebss
1:  bgeu    t0, t1, 2f
    sw      zero, 0(t0)
    addi    t0, t0, 4
    j       1b

2:  call    main
    mv      a1, zero
    call    bench_finish

3:  j       3b
