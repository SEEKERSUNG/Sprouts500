// Reference user program: exercises ITCM, DTCM, timer, GPIO and a timer
// interrupt, then reports through UART and the result mailbox.
//
// Build/run:  bash scripts/build_app.sh sw/example

#include "soc.h"

static volatile unsigned int irq_count;
static unsigned int dtcm_buf[256];          // .bss in DTCM
static const char banner[] = "Sprouts500 example app\n";

// Strong override of the weak default in sw/lib/soc.c.
void trap_handler(unsigned int cause, unsigned int epc, unsigned int tval)
{
    (void)epc;
    (void)tval;

    if (cause & 0x80000000u) {              // interrupt
        TIMER_CTRL = TIMER_CTRL_EN | TIMER_CTRL_IRQ_EN |
                     TIMER_CTRL_PENDING | TIMER_CTRL_PERIODIC;
        irq_count++;
        return;
    }

    uart_puts("EXCEPTION mcause=");
    uart_puthex(cause);
    uart_puts("\n");
    soc_halt(0xE0000000u | (cause & 0xFFFFu));
}

static int test_dtcm(void)
{
    unsigned int i;

    // .bss must have been zeroed by crt0 before main() runs.
    for (i = 0; i < 256; i++)
        if (dtcm_buf[i] != 0)
            return 1;

    for (i = 0; i < 256; i++)
        dtcm_buf[i] = i * 3u + 1u;
    for (i = 0; i < 256; i++)
        if (dtcm_buf[i] != i * 3u + 1u)
            return 2;

    return 0;
}

static int test_gpio(void)
{
    GPIO_DIR = 0x1;                         // bit 0 output
    GPIO_OUT = 0x1;
    soc_delay_cycles(32);
    if ((GPIO_IN & 0x1u) != 0x1u)
        return 1;

    GPIO_OUT = 0x0;
    soc_delay_cycles(32);
    if ((GPIO_IN & 0x1u) != 0x0u)
        return 2;

    GPIO_DIR = 0x0;
    GPIO_OUT = 0x0;
    return 0;
}

static int test_timer(void)
{
    unsigned int t0;

    TIMER_CMP  = 0xFFFFFFFFu;
    TIMER_CTRL = TIMER_CTRL_EN;
    t0 = TIMER_COUNT;
    while ((TIMER_COUNT - t0) < 1000u)
        ;
    TIMER_CTRL = 0;
    TIMER_COUNT;                            // COUNT is RO; reading is enough

    return 0;
}

static int test_irq(void)
{
    irq_count = 0;
    soc_csrw_mtvec((unsigned int)trap_entry);

    TIMER_CMP  = 5000u;
    TIMER_CTRL = TIMER_CTRL_EN | TIMER_CTRL_IRQ_EN |
                 TIMER_CTRL_PENDING | TIMER_CTRL_PERIODIC;
    soc_csr_set_mie(MIE_MTIE);
    soc_csr_set_mstatus(MSTATUS_MIE);

    while (irq_count < 3)
        ;

    soc_csr_clear_mstatus(MSTATUS_MIE);
    soc_csr_clear_mie(MIE_MTIE);
    TIMER_CTRL = 0;

    return (irq_count == 3) ? 0 : (int)irq_count;
}

int main(void)
{
    int rc;

    uart_init();
    uart_puts(banner);

    rc = test_dtcm();
    uart_puts(rc ? "dtcm FAIL\n" : "dtcm ok\n");
    if (rc)
        goto out;

    rc = test_timer();
    uart_puts(rc ? "timer FAIL\n" : "timer ok\n");
    if (rc)
        goto out;

    rc = test_gpio();
    uart_puts(rc ? "gpio FAIL\n" : "gpio ok\n");
    if (rc)
        goto out;

    rc = test_irq();
    uart_puts(rc ? "irq FAIL\n" : "irq ok\n");
    if (rc)
        goto out;

    uart_puts("example PASS\n");

out:
    uart_flush();
    return rc;
}
