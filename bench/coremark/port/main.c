/* CoreMark runner for Sprouts500.
 *
 * core_main.c is compiled with -Dmain=coremark_main and keeps its unmodified
 * reporting code.  This wrapper adds one fractional line computed from the
 * same start_time()/stop_time() measurement, because the HAS_FLOAT=0 build
 * reports integer seconds only.
 */
#include "coremark.h"
#include "bench_port.h"

#define BENCH_HZ 50000000u

int coremark_main(void);

int
main(void)
{
    unsigned int ticks;
    unsigned int iters = (unsigned int)ITERATIONS;
    unsigned long long score_milli;

    coremark_main();

    ticks = bench_last_ticks();
    score_milli = ticks ? (iters * (unsigned long long)BENCH_HZ * 1000ull) / ticks : 0ull;

    ee_printf("\nCoreMark 1.0 : ");
    bench_print_fixed((unsigned int)score_milli, 3, 1000);
    ee_printf(" CoreMark\n");
    ee_printf("Iterations: %u, cycles: %u\n", iters, ticks);
    bench_finish(0, ticks);
}
