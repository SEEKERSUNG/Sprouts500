#include "soc.h"

int main(void)
{
    unsigned int saved, after, status;

    /* ITCM address zero is a valid hardware address, but a C null-pointer
     * dereference is undefined and GCC folds it to ebreak.  Use explicit
     * loads/stores for the protection tests. */
    __asm__ volatile("lw %0, 0(zero)" : "=r"(saved));
    __asm__ volatile("sw %0, 0(zero)" : : "r"(~saved) : "memory");
    __asm__ volatile("lw %0, 0(zero)" : "=r"(after));
    if (after != saved)
        return 0xB0070001u;

    /* Last word and last byte of the 2 KiB Boot region. */
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(saved) : : "t0", "memory");
    __asm__ volatile("li t0, 0x7fc\n\tli t1, 0x5a5a5a5a\n\tsw t1, 0(t0)"
                     : : : "t0", "t1", "memory");
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(after) : : "t0", "memory");
    if (after != saved)
        return 0xB0070002u;

    __asm__ volatile("li t0, 0x7ff\n\tlbu %0, 0(t0)"
                     : "=r"(saved) : : "t0", "memory");
    __asm__ volatile("li t0, 0x7ff\n\tli t1, 0x5a\n\tsb t1, 0(t0)"
                     : : : "t0", "t1", "memory");
    __asm__ volatile("li t0, 0x7ff\n\tlbu %0, 0(t0)"
                     : "=r"(after) : : "t0", "memory");
    if (after != saved)
        return 0xB0070003u;

    /* A reserved SC into the protected region must fail, not pretend to
     * commit while the hardware drops the write. */
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(saved) : : "t0", "memory");
    __asm__ volatile(
        "li t0, 0x7fc\n\t"
        "lr.w t1, 0(t0)\n\t"
        "li t2, 0x12345678\n\t"
        "sc.w %0, t2, 0(t0)"
        : "=r"(status) : : "t0", "t1", "t2", "memory");
    if (status == 0)
        return 0xB0070004u;
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(after) : : "t0", "memory");
    if (after != saved)
        return 0xB0070005u;

    /* AMO writes are ignored too; the read result is allowed to be the old
     * word, but memory must not change. */
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(saved) : : "t0", "memory");
    __asm__ volatile("li t0, 0x7fc\n\tli t1, 1\n\tamoadd.w x0, t1, 0(t0)"
                     : : : "t0", "t1", "memory");
    __asm__ volatile("li t0, 0x7fc\n\tlw %0, 0(t0)"
                     : "=r"(after) : : "t0", "memory");
    if (after != saved)
        return 0xB0070006u;

    uart_init();
    uart_puts("APP_OK\n");
    uart_flush();
    return 0;
}
