#include "soc.h"

int main(void)
{
    unsigned int saved, after;

    /* ITCM address zero is a valid hardware address, but a C null-pointer
     * dereference is undefined and GCC folds it to ebreak.  Use explicit
     * loads/stores for this protection test. */
    __asm__ volatile("lw %0, 0(zero)" : "=r"(saved));
    __asm__ volatile("sw %0, 0(zero)" : : "r"(~saved) : "memory");
    __asm__ volatile("lw %0, 0(zero)" : "=r"(after));
    if (after != saved)
        return 0xB0070001u;

    uart_init();
    uart_puts("APP_OK\n");
    uart_flush();
    return 0;
}
