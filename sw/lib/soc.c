// Minimal bare-metal runtime for Sprouts500 user programs.
// UART helpers, halt/result mailbox, and a weak default trap handler.

#include "soc.h"

void uart_init(void)
{
    UART_BAUD = UART_BAUD_100MHZ_115200;
    UART_CTRL = UART_CTRL_TX_EN;
}

void uart_putc(char c)
{
    while (UART_STATUS & UART_STATUS_TX_BUSY)
        ;
    UART_TXDATA = (unsigned char)c;
}

void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

void uart_puthex(unsigned int v)
{
    int i;
    for (i = 28; i >= 0; i -= 4)
        uart_putc("0123456789abcdef"[(v >> i) & 0xFu]);
}

void uart_putdec(unsigned int v)
{
    char buf[10];
    int n = 0;

    if (v == 0) {
        uart_putc('0');
        return;
    }
    while (v != 0) {
        buf[n++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (n != 0)
        uart_putc(buf[--n]);
}

void uart_flush(void)
{
    while (UART_STATUS & UART_STATUS_TX_BUSY)
        ;
}

// Write the result code, then the halt magic word the testbench watches.
// main()'s return value reaches this through crt0.S.
void soc_halt(unsigned int code)
{
    SOC_IO32(SOC_RESULT_ADDR) = code;
    SOC_IO32(SOC_HALT_ADDR) = SOC_HALT_MAGIC;
    for (;;)
        ;
}

// Weak default: any trap that reaches here is fatal.  A test program that
// wants to handle interrupts defines its own strong trap_handler().
__attribute__((weak)) void trap_handler(unsigned int cause, unsigned int epc,
                                        unsigned int tval)
{
    (void)epc;
    (void)tval;
    soc_halt(0xBAD00000u | (cause & 0xFFFFu));
}
