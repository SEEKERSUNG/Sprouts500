/* Force-included (-include) before dhrystone.h to replace the host headers
 * the upstream benchmark expects.  Sprouts500 has no newlib headers, so the
 * minimum prototypes are supplied here.
 *
 * Timing: dhrystone.h uses `read_csr(mcycle)` when __riscv is defined; HZ is
 * 1 MHz, so Microseconds = ticks and Dhrystones/s is computed correctly from
 * 50 MHz cycles.  tb_bench enforces a long enough run to avoid 32-bit wrap.
 */
#ifndef DHRY_PATCH_H
#define DHRY_PATCH_H

#ifndef __riscv
#define __riscv 1
#endif

void *memcpy(void *dst, const void *src, unsigned int n);
char *strcpy(char *dst, const char *src);
int strcmp(const char *a, const char *b);
int printf(const char *fmt, ...);

static inline unsigned long read_csr_mcycle(void)
{
    unsigned long c;
    __asm__ volatile("rdcycle %0" : "=r"(c));
    return c;
}
#define read_csr(name) read_csr_mcycle()

#endif /* DHRY_PATCH_H */
