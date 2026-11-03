// Sprouts500 UART Bootloader.  No libc or shared application runtime needed.

typedef unsigned char  u8;
typedef unsigned int   u32;

#define IO32(a)        (*(volatile u32 *)(a))
#define IO8(a)         (*(volatile u8 *)(a))

#define GPIO_DIR       IO32(0x20001000u)
#define GPIO_IN        IO32(0x20001008u)
#define GPIO_IRQ_EN    IO32(0x2000100Cu)
#define UART_CTRL      IO32(0x20002000u)
#define UART_STATUS    IO32(0x20002004u)
#define UART_BAUD      IO32(0x20002008u)
#define UART_TXDATA    IO32(0x2000200Cu)
#define UART_RXDATA    IO32(0x20002010u)

#define APP_HEADER     0x00000800u
#define APP_ENTRY      0x00000810u
#define APP_LIMIT      0x00007ff0u
#define APP_MAX        (APP_LIMIT - APP_ENTRY)
#define APP_MAGIC      0x53555031u
#define BOOT_VERSION   1u

#define CMD_INFO       0x01u
#define CMD_WRITE      0x02u
#define CMD_VERIFY     0x03u
#define CMD_RUN        0x04u

#define ST_OK          0u
#define ST_CRC         1u
#define ST_RANGE       2u
#define ST_COMMAND     3u
#define ST_FRAME       4u
#define ST_NO_APP      5u
#define ST_TIMEOUT     6u

#define RX_TIMEOUT     50000000u
#define MAX_PAYLOAD    256u

static u8 payload[MAX_PAYLOAD];

extern void boot_jump(u32 entry) __attribute__((noreturn));

static u32 rdcycle(void)
{
    u32 v;
    __asm__ volatile("rdcycle %0" : "=r"(v));
    return v;
}

static void uart_putc(u8 b)
{
    while (UART_STATUS & 1u)
        ;
    UART_TXDATA = b;
}

static void uart_puts(const char *s)
{
    while (*s)
        uart_putc((u8)*s++);
}

static void uart_flush(void)
{
    while (UART_STATUS & 1u)
        ;
}

/* One holding register is sufficient at 115200 baud: every byte is consumed
 * and checksummed long before the next stop bit. */
static int uart_getc(u8 *out)
{
    u32 start = rdcycle();
    for (;;) {
        u32 status = UART_STATUS;
        if (status & 2u) {
            *out = (u8)UART_RXDATA;
            return 1;
        }
        if (status & 8u)
            (void)UART_RXDATA;
        if ((rdcycle() - start) > RX_TIMEOUT)
            return 0;
    }
}

static const u32 crc_table[16] = {
    0x00000000u, 0x1db71064u, 0x3b6e20c8u, 0x26d930acu,
    0x76dc4190u, 0x6b6b51f4u, 0x4db26158u, 0x5005713cu,
    0xedb88320u, 0xf00f9344u, 0xd6d6a3e8u, 0xcb61b38cu,
    0x9b64c2b0u, 0x86d3d2d4u, 0xa00ae278u, 0xbdbdf21cu
};

static u32 crc_byte(u32 crc, u8 b)
{
    crc ^= b;
    crc = (crc >> 4) ^ crc_table[crc & 15u];
    crc = (crc >> 4) ^ crc_table[crc & 15u];
    return crc;
}

static void put16(u8 *p, u32 v)
{
    p[0] = (u8)v;
    p[1] = (u8)(v >> 8);
}

static void put32(u8 *p, u32 v)
{
    p[0] = (u8)v;
    p[1] = (u8)(v >> 8);
    p[2] = (u8)(v >> 16);
    p[3] = (u8)(v >> 24);
}

static u32 get32(const u8 *p)
{
    return (u32)p[0] | ((u32)p[1] << 8) |
           ((u32)p[2] << 16) | ((u32)p[3] << 24);
}

static int app_verify(u32 *crc_out)
{
    u32 length, crc, i;
    const volatile u8 *image = (const volatile u8 *)APP_ENTRY;

    if (IO32(APP_HEADER) != APP_MAGIC)
        return 0;
    length = IO32(APP_HEADER + 4u);
    if ((length == 0u) || (length > APP_MAX) ||
        (IO32(APP_HEADER + 8u) != APP_ENTRY))
        return 0;

    crc = 0xffffffffu;
    for (i = 0; i < length; i = i + 1)
        crc = crc_byte(crc, image[i]);
    crc ^= 0xffffffffu;
    if ((crc != IO32(APP_HEADER + 12u)) || (APP_ENTRY & 1u))
        return 0;
    if (crc_out)
        *crc_out = crc;
    return 1;
}

/* Response frames use the request command with bit 7 set. */
static void send_frame(u8 cmd, u8 seq, const u8 *data, u32 length)
{
    u8 header[10];
    u32 crc = 0xffffffffu;
    u32 i;

    header[0] = cmd;
    header[1] = seq;
    put32(header + 2, 0u);
    put16(header + 6, length);
    put16(header + 8, 0u);

    uart_putc(0x53u);
    uart_putc(0x42u);
    for (i = 0; i < 10u; i = i + 1) {
        uart_putc(header[i]);
        crc = crc_byte(crc, header[i]);
    }
    for (i = 0; i < length; i = i + 1) {
        uart_putc(data[i]);
        crc = crc_byte(crc, data[i]);
    }
    crc ^= 0xffffffffu;
    for (i = 0; i < 4u; i = i + 1)
        uart_putc((u8)(crc >> (i * 8u)));
}

static void send_status(u8 cmd, u8 seq, u32 status)
{
    u8 data[4];
    put32(data, status);
    send_frame((u8)(cmd | 0x80u), seq, data, 4u);
}

/* Return 0 after a complete valid frame, ST_TIMEOUT when the host is idle,
 * or a negative-independent protocol error that the caller can NACK. */
static u32 read_frame(u8 *cmd, u8 *seq, u32 *address, u32 *length)
{
    u8 header[10];
    u8 b;
    u32 crc, received, i;
    int have_magic = 0;

    for (;;) {
        if (!uart_getc(&b))
            return ST_TIMEOUT;
        if (!have_magic) {
            have_magic = (b == 0x53u);
        } else if (b == 0x42u) {
            break;
        } else {
            have_magic = (b == 0x53u);
        }
    }

    for (i = 0; i < 10u; i = i + 1) {
        if (!uart_getc(&header[i]))
            return ST_TIMEOUT;
    }
    *cmd = header[0];
    *seq = header[1];
    *address = get32(header + 2);
    *length = (u32)header[6] | ((u32)header[7] << 8);
    if ((*length > MAX_PAYLOAD) || (header[8] != 0u) || (header[9] != 0u))
        return ST_FRAME;

    crc = 0xffffffffu;
    for (i = 0; i < 10u; i = i + 1)
        crc = crc_byte(crc, header[i]);
    for (i = 0; i < *length; i = i + 1) {
        if (!uart_getc(&payload[i]))
            return ST_TIMEOUT;
        crc = crc_byte(crc, payload[i]);
    }
    received = 0u;
    for (i = 0; i < 4u; i = i + 1) {
        if (!uart_getc(&b))
            return ST_TIMEOUT;
        received |= (u32)b << (i * 8u);
    }
    crc ^= 0xffffffffu;
    return (crc == received) ? ST_OK : ST_CRC;
}

static void handle_frame(u8 cmd, u8 seq, u32 address, u32 length)
{
    u8 data[20];
    u32 crc, i, status;

    if (cmd == CMD_INFO) {
        put32(data, ST_OK);
        put32(data + 4, BOOT_VERSION);
        put32(data + 8, APP_HEADER);
        put32(data + 12, APP_ENTRY);
        put32(data + 16, APP_MAX);
        send_frame(0x81u, seq, data, 20u);
        return;
    }

    if (cmd == CMD_WRITE) {
        /* Both comparisons are ordered so a wrapping address+length cannot
         * turn into an in-range write.  The header lives at 0x800, just above
         * the hardware-protected Boot code region. */
        if ((address < APP_HEADER) || (address > APP_LIMIT) ||
            (length > (APP_LIMIT - address))) {
            send_status(cmd, seq, ST_RANGE);
            return;
        }
        for (i = 0; i < length; i = i + 1)
            IO8(address + i) = payload[i];
        send_status(cmd, seq, ST_OK);
        return;
    }

    if (cmd == CMD_VERIFY) {
        if (!app_verify(&crc)) {
            send_status(cmd, seq, ST_NO_APP);
            return;
        }
        put32(data, ST_OK);
        put32(data + 4, crc);
        send_frame(0x83u, seq, data, 8u);
        return;
    }

    if (cmd == CMD_RUN) {
        if (!app_verify(&crc)) {
            send_status(cmd, seq, ST_NO_APP);
            return;
        }
        send_status(cmd, seq, ST_OK);
        uart_flush();
        UART_CTRL = 0u;
        boot_jump(APP_ENTRY);
    }

    status = ST_COMMAND;
    send_status(cmd, seq, status);
}

void boot_main(void)
{
    u8 cmd, seq;
    u32 address, length, status;

    GPIO_DIR = GPIO_DIR & ~1u;       /* GPIO[0]: BOOT0, active low */
    GPIO_IRQ_EN = GPIO_IRQ_EN & ~1u;
    if ((GPIO_IN & 1u) && app_verify(0))
        boot_jump(APP_ENTRY);

    UART_BAUD = 867u;                /* 100 MHz, 115200 8N1 */
    UART_CTRL = 3u;                  /* TX and RX, polling mode */
    uart_puts("S500BOOT\r\n");

    for (;;) {
        status = read_frame(&cmd, &seq, &address, &length);
        if (status == ST_TIMEOUT)
            continue;
        if (status != ST_OK) {
            send_status(cmd, seq, status);
            continue;
        }
        handle_frame(cmd, seq, address, length);
    }
}
