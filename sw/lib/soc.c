// Minimal bare-metal runtime for Sprouts500 user programs.
// UART helpers, halt/result mailbox, and a weak default trap handler.

#include <stdarg.h>

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

static void uart_repeat(char c, unsigned int count)
{
    while (count != 0) {
        uart_putc(c);
        count--;
    }
}

static unsigned int uart_digits(char *buf, unsigned int v,
                                unsigned int base, int upper)
{
    const char *map = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    unsigned int n = 0;

    if (v == 0) {
        buf[n++] = '0';
        return n;
    }
    while (v != 0) {
        buf[n++] = map[v % base];
        v /= base;
    }
    return n;
}

// Print one unsigned number with an optional sign and printf-style width
// and precision.  Zero padding goes after the sign; space padding goes
// before it.  A precision of -1 means "not specified".
static void uart_number(unsigned int v, unsigned int base, int upper,
                        char sign, unsigned int width, int left, int zero,
                        int precision)
{
    char buf[32];
    unsigned int len = uart_digits(buf, v, base, upper);
    unsigned int zeros = 0;
    unsigned int total;
    unsigned int pad;

    if (precision == 0 && v == 0)
        len = 0;
    if (precision >= 0) {
        if ((unsigned int)precision > len)
            zeros = (unsigned int)precision - len;
        zero = 0;              // precision disables zero-padding
    }
    total = len + zeros + (sign ? 1u : 0u);
    pad = width > total ? width - total : 0;

    if (!left && !zero)
        uart_repeat(' ', pad);
    if (sign)
        uart_putc(sign);
    if (!left && zero)
        uart_repeat('0', pad);
    uart_repeat('0', zeros);
    while (len != 0)
        uart_putc(buf[--len]);
    if (left)
        uart_repeat(' ', pad);
}

static void uart_string(const char *s, unsigned int width, int left,
                        int precision)
{
    unsigned int len = 0;
    unsigned int pad;

    if (!s)
        s = "(null)";
    while (s[len] && (precision < 0 || len < (unsigned int)precision))
        len++;
    pad = width > len ? width - len : 0;
    if (!left)
        uart_repeat(' ', pad);
    while (len != 0) {
        uart_putc(*s++);
        len--;
    }
    if (left)
        uart_repeat(' ', pad);
}

static void uart_pointer(const void *p, unsigned int width, int left)
{
    // 0x plus eight digits, with width covering the whole field.
    unsigned int total = 10;
    unsigned int pad = width > total ? width - total : 0;

    if (!left)
        uart_repeat(' ', pad);
    uart_puts("0x");
    uart_number((unsigned int)p, 16, 0, 0, 8, 0, 1, -1);
    if (left)
        uart_repeat(' ', pad);
}

// Consume a 64-bit integer argument without formatting it.  The length
// modifiers are echoed so the caller can see which conversion is missing.
static void uart_unsupported_num(va_list *ap, int is_signed, int j_mode,
                                 int long_count, char conv)
{
    if (is_signed)
        (void)va_arg(*ap, long long);
    else
        (void)va_arg(*ap, unsigned long long);
    uart_putc('%');
    if (j_mode)
        uart_putc('j');
    while (long_count > 0) {
        uart_putc('l');
        long_count--;
    }
    uart_putc(conv);
}

static void uart_unsupported_float(va_list *ap, int long_double, char conv)
{
    if (long_double)
        (void)va_arg(*ap, long double);
    else
        (void)va_arg(*ap, double);
    uart_putc('%');
    if (long_double)
        uart_putc('L');
    uart_putc(conv);
}

// Subset of printf for the bare-metal runtime.  Supported conversions are
// %d %i %u %o %x %X %c %s %p and %%; flags - 0 + space # ', width and
// precision (digits or *), and the h/l/z/t length modifiers.  Floating-point
// and 64-bit (ll/j) conversions are consumed but printed literally (e.g.
// "%f", "%lld") so following arguments stay aligned and no soft-float or
// 64-bit division helpers are pulled in.
void uart_printf(const char *fmt, ...)
{
    va_list ap;

    va_start(ap, fmt);
    while (*fmt) {
        int left = 0, zero = 0, plus = 0, space = 0;
        int long_mode = 0, long_count = 0, j_mode = 0, long_double = 0;
        int precision = -1;
        unsigned int width = 0;

        if (*fmt != '%') {
            uart_putc(*fmt++);
            continue;
        }
        fmt++;
        if (*fmt == '%') {
            uart_putc('%');
            fmt++;
            continue;
        }

        for (;;) {
            if (*fmt == '-') {
                left = 1;
                fmt++;
            } else if (*fmt == '0') {
                zero = 1;
                fmt++;
            } else if (*fmt == '+') {
                plus = 1;
                fmt++;
            } else if (*fmt == ' ') {
                space = 1;
                fmt++;
            } else if (*fmt == '#' || *fmt == '\'') {
                fmt++;          // accepted but not implemented
            } else {
                break;
            }
        }
        if (*fmt == '*') {
            int w = va_arg(ap, int);
            if (w < 0) {
                left = 1;
                w = -w;
            }
            width = (unsigned int)w;
            fmt++;
        } else {
            while (*fmt >= '0' && *fmt <= '9') {
                width = width * 10u + (unsigned int)(*fmt - '0');
                fmt++;
            }
        }
        if (*fmt == '.') {
            fmt++;
            precision = 0;
            if (*fmt == '*') {
                int p = va_arg(ap, int);
                precision = p < 0 ? -1 : p;
                fmt++;
            } else {
                while (*fmt >= '0' && *fmt <= '9') {
                    precision = precision * 10 + (*fmt - '0');
                    fmt++;
                }
            }
        }
        while (*fmt == 'l') {
            long_mode = 1;
            long_count++;
            fmt++;
        }
        if (*fmt == 'j') {
            j_mode = 1;
            fmt++;
        }
        while (*fmt == 'h' || *fmt == 'z' || *fmt == 't')
            fmt++;
        if (*fmt == 'L') {
            long_double = 1;
            fmt++;
        }

        switch (*fmt) {
        case 'd':
        case 'i': {
            char sign = 0;
            unsigned int mag;

            if (j_mode || long_count > 1) {
                uart_unsupported_num(&ap, 1, j_mode, long_count, *fmt);
                break;
            }
            if (long_mode) {
                long v = va_arg(ap, long);
                if (v < 0) {
                    sign = '-';
                    mag = 0u - (unsigned int)(unsigned long)v;
                } else {
                    mag = (unsigned int)(unsigned long)v;
                    if (plus)
                        sign = '+';
                    else if (space)
                        sign = ' ';
                }
            } else {
                int v = va_arg(ap, int);
                if (v < 0) {
                    sign = '-';
                    mag = 0u - (unsigned int)v;
                } else {
                    mag = (unsigned int)v;
                    if (plus)
                        sign = '+';
                    else if (space)
                        sign = ' ';
                }
            }
            uart_number(mag, 10, 0, sign, width, left, zero, precision);
            break;
        }
        case 'u': {
            unsigned int v;

            if (j_mode || long_count > 1) {
                uart_unsupported_num(&ap, 0, j_mode, long_count, *fmt);
                break;
            }
            v = long_mode ? (unsigned int)va_arg(ap, unsigned long)
                          : va_arg(ap, unsigned int);
            uart_number(v, 10, 0, 0, width, left, zero, precision);
            break;
        }
        case 'o': {
            unsigned int v;

            if (j_mode || long_count > 1) {
                uart_unsupported_num(&ap, 0, j_mode, long_count, *fmt);
                break;
            }
            v = long_mode ? (unsigned int)va_arg(ap, unsigned long)
                          : va_arg(ap, unsigned int);
            uart_number(v, 8, 0, 0, width, left, zero, precision);
            break;
        }
        case 'x':
        case 'X': {
            unsigned int v;

            if (j_mode || long_count > 1) {
                uart_unsupported_num(&ap, 0, j_mode, long_count, *fmt);
                break;
            }
            v = long_mode ? (unsigned int)va_arg(ap, unsigned long)
                          : va_arg(ap, unsigned int);
            uart_number(v, 16, *fmt == 'X', 0, width, left, zero, precision);
            break;
        }
        case 'c': {
            char c = (char)va_arg(ap, int);
            unsigned int pad = width > 0 ? width - 1u : 0u;

            if (!left)
                uart_repeat(' ', pad);
            uart_putc(c);
            if (left)
                uart_repeat(' ', pad);
            break;
        }
        case 's':
            uart_string(va_arg(ap, const char *), width, left, precision);
            break;
        case 'p':
            uart_pointer(va_arg(ap, void *), width, left);
            break;
        case 'f':
        case 'F':
        case 'e':
        case 'E':
        case 'g':
        case 'G':
        case 'a':
        case 'A':
            uart_unsupported_float(&ap, long_double, *fmt);
            break;
        case 'n':
            (void)va_arg(ap, int *);
            uart_putc('%');
            uart_putc('n');
            break;
        case '\0':
            uart_putc('%');
            va_end(ap);
            return;
        default:
            uart_putc('%');
            uart_putc(*fmt);
            break;
        }
        if (*fmt)
            fmt++;
    }
    va_end(ap);
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
