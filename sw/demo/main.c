/* Bare-metal hello for Sprouts500. */

#define REG32(a) (*(volatile unsigned int *)(a))

#define TIMER_BASE 0x20000000u
#define GPIO_BASE  0x20001000u
#define UART_BASE  0x20002000u
#define SPI_BASE   0x20003000u
#define I2C_BASE   0x20004000u

#define TIMER_CTRL  REG32(TIMER_BASE + 0x00)
#define TIMER_CNT   REG32(TIMER_BASE + 0x04)
#define TIMER_CMP   REG32(TIMER_BASE + 0x08)

#define GPIO_DIR    REG32(GPIO_BASE + 0x00)
#define GPIO_OUT    REG32(GPIO_BASE + 0x04)
#define GPIO_IN     REG32(GPIO_BASE + 0x08)

#define UART_CTRL   REG32(UART_BASE + 0x00)
#define UART_STATUS REG32(UART_BASE + 0x04)
#define UART_BAUD   REG32(UART_BASE + 0x08)
#define UART_TXDATA REG32(UART_BASE + 0x0C)
#define UART_RXDATA REG32(UART_BASE + 0x10)

#define SPI_CTRL    REG32(SPI_BASE + 0x00)
#define SPI_STATUS  REG32(SPI_BASE + 0x04)
#define SPI_CLKDIV  REG32(SPI_BASE + 0x08)
#define SPI_TXDATA  REG32(SPI_BASE + 0x0C)
#define SPI_RXDATA  REG32(SPI_BASE + 0x10)
#define SPI_CS      REG32(SPI_BASE + 0x14)

#define I2C_CTRL    REG32(I2C_BASE + 0x00)
#define I2C_STATUS  REG32(I2C_BASE + 0x04)
#define I2C_CLKDIV  REG32(I2C_BASE + 0x08)
#define I2C_TXDATA  REG32(I2C_BASE + 0x0C)
#define I2C_RXDATA  REG32(I2C_BASE + 0x10)
#define I2C_CMD     REG32(I2C_BASE + 0x14)

static void uart_init(void)
{
    UART_BAUD = 867;       /* 100 MHz / 115200 - 1 */
    UART_CTRL = 0x1;       /* TX enable */
}

static void uart_putc(char c)
{
    while (UART_STATUS & 0x1)
        ;
    UART_TXDATA = (unsigned char)c;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc(*s++);
}

static unsigned timer_read(void)
{
    TIMER_CMP = 0xffffffffu;
    TIMER_CTRL = 0x1;      /* enable, free running */
    return TIMER_CNT;
}

static unsigned i2c_command(unsigned data, unsigned cmd)
{
    I2C_TXDATA = data;
    I2C_CMD = cmd;
    while (!(I2C_STATUS & 0x2))
        ;
    return I2C_STATUS;
}

/* Write and read back one byte in the testbench EEPROM at address 0x50. */
static int i2c_test(void)
{
    I2C_CTRL = 0;
    I2C_CLKDIV = 124;      /* nominal 400 kHz at 100 MHz */
    I2C_CTRL = 0x1;

    if (i2c_command(0xa0, 0x9) & 0x4)  /* START + address W */
        return 1;
    if (i2c_command(0x20, 0x8) & 0x4)  /* memory pointer */
        return 2;
    if (i2c_command(0xa5, 0xa) & 0x4)  /* data + STOP */
        return 3;

    if (i2c_command(0xa0, 0x9) & 0x4)
        return 4;
    if (i2c_command(0x20, 0x8) & 0x4)
        return 5;
    if (i2c_command(0xa1, 0x9) & 0x4)  /* repeated START + address R */
        return 6;
    (void)i2c_command(0, 0x16);        /* last READ: NACK + STOP */
    return (I2C_RXDATA == 0xa5) ? 0 : 7;
}

/* ------------------------------------------------------------------ */
/* timer interrupt                                                     */
/* ------------------------------------------------------------------ */
extern void trap_entry(void);

volatile unsigned irq_count;

void timer_isr(void)
{
    irq_count++;
    TIMER_CTRL = 0x0f;     /* enable | irq | W1C pending | periodic */
}

static inline void csrw_mtvec(unsigned x)
{
    __asm__ volatile("csrw mtvec, %0" : : "r"(x));
}

static inline void csrs_mie(unsigned x)
{
    __asm__ volatile("csrs mie, %0" : : "r"(x));
}

static inline void csrc_mie(unsigned x)
{
    __asm__ volatile("csrc mie, %0" : : "r"(x));
}

static inline void csrs_mstatus(unsigned x)
{
    __asm__ volatile("csrs mstatus, %0" : : "r"(x));
}

static inline void csrc_mstatus(unsigned x)
{
    __asm__ volatile("csrc mstatus, %0" : : "r"(x));
}

int main(void)
{
    unsigned t0, t1;

    uart_init();
    uart_puts("Hello Sprouts500!\n");

    /* exercise ITCM/DTCM, timer and GPIO */
    GPIO_DIR = 0x1;
    GPIO_OUT = 0x1;

    t0 = timer_read();
    do {
        t1 = timer_read();
    } while ((t1 - t0) < 100);

    GPIO_OUT = 0x0;

    uart_puts("peripherals ok\n");

    if (i2c_test() == 0)
        uart_puts("i2c ok\n");
    else
        uart_puts("i2c fail\n");

    /* timer interrupt test: take three periodic interrupts */
    irq_count = 0;
    csrw_mtvec((unsigned)trap_entry);
    TIMER_CMP  = 10000;    /* ~100 us at 100 MHz */
    TIMER_CTRL = 0x0f;      /* enable, irq, clear pending, periodic */
    csrs_mie(0x80);         /* MTIE */
    csrs_mstatus(0x8);      /* MIE */

    while (irq_count < 3)
        ;

    csrc_mstatus(0x8);
    csrc_mie(0x80);
    TIMER_CTRL = 0x0;
    uart_puts("irq ok\n");

    while (UART_STATUS & 0x1)   /* let the last stop bit finish before halt */
        ;
    return 0;
}
