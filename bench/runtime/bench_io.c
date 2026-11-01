/* Bare-metal I/O and formatting for the benchmarks in bench/.
 *
 * Same UART programming as sw/lib, plus an integer-only printf and a
 * fixed-point printer so no soft-float library is needed.
 */

#include "bench_port.h"

#define BENCH_IO32(a) (*(volatile unsigned int *)(a))

#define BENCH_UART_BASE   0x20002000u
#define BENCH_UART_CTRL   BENCH_IO32(BENCH_UART_BASE + 0x00u)
#define BENCH_UART_STATUS BENCH_IO32(BENCH_UART_BASE + 0x04u)
#define BENCH_UART_BAUD   BENCH_IO32(BENCH_UART_BASE + 0x08u)
#define BENCH_UART_TXDATA BENCH_IO32(BENCH_UART_BASE + 0x0Cu)

#define BENCH_RESULT_ADDR 0x00007FF8u
#define BENCH_HALT_ADDR   0x00007FFCu
#define BENCH_CYCLES_ADDR 0x00007FF4u
#define BENCH_HALT_MAGIC  0x5A5A5A5Au

void
bench_uart_init(void)
{
    BENCH_UART_BAUD = 867u;
    BENCH_UART_CTRL = 0x1u;
}

void
bench_uart_putc(char c)
{
    while (BENCH_UART_STATUS & 0x1u)
        ;
    BENCH_UART_TXDATA = (unsigned char)c;
}

void
bench_uart_flush(void)
{
    while (BENCH_UART_STATUS & 0x1u)
        ;
}

static void
put_uint(unsigned int v)
{
    char buf[10];
    int n = 0;

    if (v == 0) {
        bench_uart_putc('0');
        return;
    }
    while (v != 0) {
        buf[n++] = (char)('0' + v % 10u);
        v /= 10u;
    }
    while (n != 0)
        bench_uart_putc(buf[--n]);
}

static void
put_uint_pad(unsigned int v, unsigned int width)
{
    unsigned int div = 1;

    while (width-- > 1)
        div *= 10u;
    while (div != 0) {
        bench_uart_putc((char)('0' + (v / div) % 10u));
        div /= 10u;
    }
}

void
bench_print_fixed(unsigned int n, unsigned int digits, unsigned int scale)
{
    put_uint(n / scale);
    if (digits != 0) {
        bench_uart_putc('.');
        put_uint_pad(n % scale, digits);
    }
}

void
bench_finish(unsigned int result, unsigned int cycles)
{
    bench_uart_flush();
    BENCH_IO32(BENCH_CYCLES_ADDR) = cycles;
    BENCH_IO32(BENCH_RESULT_ADDR) = result;
    BENCH_IO32(BENCH_HALT_ADDR) = BENCH_HALT_MAGIC;
    for (;;)
        ;
}

char *
strcpy(char *dst, const char *src)
{
    char *d = dst;
    while ((*d++ = *src++) != '\0')
        ;
    return dst;
}

int
strcmp(const char *a, const char *b)
{
    while (*a && (*a == *b)) {
        a++;
        b++;
    }
    return (int)(unsigned char)*a - (int)(unsigned char)*b;
}

/* ------------------------------------------------------------------ */
/* printf subset: %d %i %u %x %X %c %s %%, optional '0' and width.     */
/* ------------------------------------------------------------------ */
static char *
fmt_into(char *p, unsigned int v, unsigned int base, int upper, int width, int zero)
{
    const char *dig = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    char tmp[32];
    int n = 0, i;

    if (v == 0)
        tmp[n++] = '0';
    while (v != 0) {
        tmp[n++] = dig[v % base];
        v /= base;
    }
    for (i = n; i < width; i++)
        *p++ = (char)(zero ? '0' : ' ');
    while (n != 0)
        *p++ = tmp[--n];
    return p;
}

static int
print_int(long v, int width, int zero)
{
    char buf[24];
    char *p = buf;

    if (v < 0) {
        *p++ = '-';
        p = fmt_into(p, (unsigned int)(-(v + 1)) + 1u, 10, 0, width - 1 > 0 ? width - 1 : 0, zero);
    } else {
        p = fmt_into(p, (unsigned int)v, 10, 0, width, zero);
    }
    *p = '\0';
    for (char *q = buf; *q; q++)
        bench_uart_putc(*q);
    return (int)(p - buf);
}

int
printf(const char *fmt, ...)
{
    __builtin_va_list ap;
    int out = 0;

    __builtin_va_start(ap, fmt);
    while (*fmt) {
        unsigned int width = 0;
        int zero = 0;
        int is_long = 0;

        if (*fmt != '%') {
            bench_uart_putc(*fmt++);
            out++;
            continue;
        }
        fmt++;
        if (*fmt == '0') {
            zero = 1;
            fmt++;
        }
        while (*fmt >= '0' && *fmt <= '9')
            width = width * 10u + (unsigned int)(*fmt++ - '0');
        if (*fmt == 'l') {
            is_long = 1;
            fmt++;
        }
        switch (*fmt++) {
        case 'd':
        case 'i': {
            long v = is_long ? __builtin_va_arg(ap, long) : (long)__builtin_va_arg(ap, int);
            out += print_int(v, (int)width, zero);
            break;
        }
        case 'u': {
            unsigned int v = is_long ? (unsigned int)__builtin_va_arg(ap, unsigned long)
                                     : __builtin_va_arg(ap, unsigned int);
            char buf[24];
            char *p = fmt_into(buf, v, 10, 0, (int)width, zero);
            *p = '\0';
            for (char *q = buf; *q; q++) { bench_uart_putc(*q); out++; }
            break;
        }
        case 'x':
        case 'X': {
            unsigned int v = is_long ? (unsigned int)__builtin_va_arg(ap, unsigned long)
                                     : __builtin_va_arg(ap, unsigned int);
            char buf[24];
            char *p = fmt_into(buf, v, 16, *fmt == 'X', (int)width, zero);
            *p = '\0';
            for (char *q = buf; *q; q++) { bench_uart_putc(*q); out++; }
            break;
        }
        case 'c':
            bench_uart_putc((char)__builtin_va_arg(ap, int));
            out++;
            break;
        case 's': {
            const char *s = __builtin_va_arg(ap, const char *);
            if (!s)
                s = "(null)";
            while (*s) { bench_uart_putc(*s++); out++; }
            break;
        }
        case '%':
            bench_uart_putc('%');
            out++;
            break;
        default:
            bench_uart_putc('%');
            out++;
            break;
        }
    }
    __builtin_va_end(ap);
    return out;
}
