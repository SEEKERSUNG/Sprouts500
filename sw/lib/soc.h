#ifndef SPROUTS500_SOC_H
#define SPROUTS500_SOC_H

// Minimal bare-metal runtime interface for Sprouts500.
//
// Memory map:
//   0x0000_0000  ITCM (32 KiB), halt/result mailboxes at the top
//   0x1000_0000  DTCM (16 KiB), .bss and stack live here in sw/lib/app.ld
//   0x2000_0000  timer
//   0x2000_1000  gpio
//   0x2000_2000  uart
//   0x2000_3000  spi

#define SOC_IO32(addr) (*(volatile unsigned int *)(addr))

#define SOC_ITCM_BASE   0x00000000u
#define SOC_DTCM_BASE   0x10000000u
#define SOC_TIMER_BASE  0x20000000u
#define SOC_GPIO_BASE   0x20001000u
#define SOC_UART_BASE   0x20002000u
#define SOC_SPI_BASE    0x20003000u

#define SOC_RESULT_ADDR 0x00007FF8u
#define SOC_HALT_ADDR   0x00007FFCu
#define SOC_HALT_MAGIC  0x5A5A5A5Au

// timer
#define TIMER_CTRL  SOC_IO32(SOC_TIMER_BASE + 0x00)
#define TIMER_COUNT SOC_IO32(SOC_TIMER_BASE + 0x04)
#define TIMER_CMP   SOC_IO32(SOC_TIMER_BASE + 0x08)
#define TIMER_PRE   SOC_IO32(SOC_TIMER_BASE + 0x0C)

#define TIMER_CTRL_EN       0x1u
#define TIMER_CTRL_IRQ_EN   0x2u
#define TIMER_CTRL_PENDING  0x4u   // write 1 to clear
#define TIMER_CTRL_PERIODIC 0x8u

// gpio
#define GPIO_DIR     SOC_IO32(SOC_GPIO_BASE + 0x00)
#define GPIO_OUT     SOC_IO32(SOC_GPIO_BASE + 0x04)
#define GPIO_IN      SOC_IO32(SOC_GPIO_BASE + 0x08)
#define GPIO_IRQ_EN  SOC_IO32(SOC_GPIO_BASE + 0x0C)
#define GPIO_IRQ_PEND SOC_IO32(SOC_GPIO_BASE + 0x10)

// uart
#define UART_CTRL   SOC_IO32(SOC_UART_BASE + 0x00)
#define UART_STATUS SOC_IO32(SOC_UART_BASE + 0x04)
#define UART_BAUD   SOC_IO32(SOC_UART_BASE + 0x08)
#define UART_TXDATA SOC_IO32(SOC_UART_BASE + 0x0C)
#define UART_RXDATA SOC_IO32(SOC_UART_BASE + 0x10)

#define UART_CTRL_TX_EN  0x1u
#define UART_CTRL_RX_EN  0x2u
#define UART_CTRL_TX_IRQ 0x4u
#define UART_CTRL_RX_IRQ 0x8u

#define UART_STATUS_TX_BUSY 0x1u
#define UART_STATUS_RX_VALID 0x2u
#define UART_STATUS_TX_DONE 0x4u
#define UART_STATUS_RX_OVERRUN 0x8u

/* BAUD = f_clk / baud - 1.  The SoC runs at 100 MHz. */
#define UART_BAUD_100MHZ_115200 867u

// spi
#define SPI_CTRL   SOC_IO32(SOC_SPI_BASE + 0x00)
#define SPI_STATUS SOC_IO32(SOC_SPI_BASE + 0x04)
#define SPI_CLKDIV SOC_IO32(SOC_SPI_BASE + 0x08)
#define SPI_TXDATA SOC_IO32(SOC_SPI_BASE + 0x0C)
#define SPI_RXDATA SOC_IO32(SOC_SPI_BASE + 0x10)
#define SPI_CS     SOC_IO32(SOC_SPI_BASE + 0x14)

#define SPI_CTRL_EN        0x1u
#define SPI_CTRL_CPOL      0x2u
#define SPI_CTRL_CPHA      0x4u
#define SPI_CTRL_IRQ       0x8u
#define SPI_CTRL_LSB_FIRST 0x10u

#define SPI_STATUS_BUSY 0x1u
#define SPI_STATUS_DONE 0x2u

// mstatus/mie bits used by the timer example
#define MSTATUS_MIE     0x00000008u
#define MIE_MSIE        0x00000008u
#define MIE_MTIE        0x00000080u
#define MIE_MEIE        0x00000800u

#ifndef __ASSEMBLER__

// sw/lib/soc.c
void uart_init(void);
void uart_putc(char c);
void uart_puts(const char *s);
void uart_puthex(unsigned int v);
void uart_putdec(unsigned int v);
void uart_flush(void);
void soc_halt(unsigned int code) __attribute__((noreturn));

// sw/lib/string.c
void *memcpy(void *dst, const void *src, unsigned int n);
void *memmove(void *dst, const void *src, unsigned int n);
void *memset(void *dst, int c, unsigned int n);
int memcmp(const void *a, const void *b, unsigned int n);
unsigned int strlen(const char *s);

// sw/lib/trap.S
extern void trap_entry(void);

// weak default lives in soc.c; define a strong one in your app to override
void trap_handler(unsigned int cause, unsigned int epc, unsigned int tval);

static inline unsigned int soc_rdcycle(void)
{
    unsigned int c;
    __asm__ volatile("rdcycle %0" : "=r"(c));
    return c;
}

// Busy-wait for n clocks using the cycle CSR (wrap-safe).
static inline void soc_delay_cycles(unsigned int n)
{
    unsigned int t0 = soc_rdcycle();
    while ((soc_rdcycle() - t0) < n)
        ;
}

static inline void soc_csrw_mtvec(unsigned int v)
{
    __asm__ volatile("csrw mtvec, %0" : : "r"(v));
}

static inline void soc_csr_set_mie(unsigned int v)
{
    __asm__ volatile("csrs mie, %0" : : "r"(v));
}

static inline void soc_csr_clear_mie(unsigned int v)
{
    __asm__ volatile("csrc mie, %0" : : "r"(v));
}

static inline void soc_csr_set_mstatus(unsigned int v)
{
    __asm__ volatile("csrs mstatus, %0" : : "r"(v));
}

static inline void soc_csr_clear_mstatus(unsigned int v)
{
    __asm__ volatile("csrc mstatus, %0" : : "r"(v));
}

#endif /* __ASSEMBLER__ */

#endif /* SPROUTS500_SOC_H */
