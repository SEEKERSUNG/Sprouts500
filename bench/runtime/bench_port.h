#ifndef SPROUTS500_BENCH_PORT_H
#define SPROUTS500_BENCH_PORT_H

void bench_uart_init(void);
void bench_uart_putc(char c);
void bench_uart_flush(void);

/* Print n/scale with `digits` fractional digits (scale == 10^digits). */
void bench_print_fixed(unsigned int n, unsigned int digits, unsigned int scale);

/* Write the result/cycle mailboxes watched by sim/tb_bench.v, then halt. */
void bench_finish(unsigned int result, unsigned int cycles) __attribute__((noreturn));

/* Integer-only printf for the benchmark ports. */
int printf(const char *fmt, ...);

char *strcpy(char *dst, const char *src);
/* Cycles measured by the most recent start_time()/stop_time(). */
unsigned int bench_last_ticks(void);
int strcmp(const char *a, const char *b);

#endif /* SPROUTS500_BENCH_PORT_H */
