# 自己写 C 程序测试 Sprouts500

这份文档讲怎么在这个 core 上**写自己的裸机 C 程序、编译、在仿真里跑、看结果**。
官方 ISA 套件见 `docs/verification.md`，外设寄存器见 `docs/registers.md`。

仓库自带一套最小运行时（`sw/lib/`）和一个通用 testbench（`sim/tb_app.v`），
不需要 libc、不需要操作系统，也不要求改 testbench 的期望字符串。

---

## 0. 最快路径

```bash
# 1. 跑参考程序，确认环境没问题（期望最后一行 TEST_PASS）
bash scripts/build_app.sh sw/example

# 2. 复制一份，改成自己的测试
cp -r sw/example sw/lab
$EDITOR sw/lab/main.c

# 3. 编译 + 运行
bash scripts/build_app.sh sw/lab

# 4. 需要波形时
bash scripts/build_app.sh sw/lab +VCD
gtkwave build/tb_app.vcd
```

产物都在 `build/app/`：

```
app.elf   链接后的程序
app.bin   flat binary
app.hex   testbench 读的 word hex
tb_app.vvp 编译好的 testbench
run.log   本次运行的完整输出
```

`build_app.sh` 会编译 `sw/lab/` 下所有 `.c` / `.S`，自动带上 `sw/lib/` 的启动代码，
所以测试目录里**只写测试本身**即可。

---

## 1. 运行时契约（最重要）

### 1.1 存储器布局

| 区域 | 地址 | 用途 |
|---|---|---|
| ITCM | `0x0000_0000`–`0x0000_7FFF` | 指令、只读数据、已初始化数据 |
| ITCM 顶部 | `0x0000_7FF8` / `0x0000_7FFC` | result mailbox / halt mailbox |
| DTCM | `0x1000_0000`–`0x1000_3FFF` | `.bss`、栈（16 KiB） |
| 定时器 | `0x2000_0000` | |
| GPIO | `0x2000_1000` | |
| UART | `0x2000_2000` | |
| SPI | `0x2000_3000` | |

`sw/lib/app.ld` 就是这样放的：

- `.text` / `.rodata` / `.data` 在 ITCM，由 `$readmemh` 预载；
- `.bss (NOLOAD)` 在 DTCM，**不会**被预载，`crt0.S` 会先清零；
- `_stack_top = 0x1000_4000`（DTCM 末端，栈向下生长）；
- 链接时会断言镜像不越过 `0x7FF0`、`.bss` 与栈之间至少留 2 KiB。

### 1.2 启动与结束

`sw/lib/crt0.S` 做四件事：

1. `sp = _stack_top`；
2. 写 `mstatus.FS = Initial`（0x2000），使 C 代码生成的 RV32F 指令可以执行；
3. 清零 `_sbss .. _ebss`；
4. `call main`，把 `main` 的返回值交给 `soc_halt()`。

所以：

```c
int main(void) { ... return 0; }   // 0 = 通过，非 0 = 失败
```

`main` 返回后，`soc_halt(code)` 会：

```c
*(volatile unsigned *)0x7FF8 = code;        // result
*(volatile unsigned *)0x7FFC = 0x5A5A5A5A;  // halt magic
for (;;) ;
```

testbench 看到 `0x7FFC` 的 magic 就停止，并比较 `0x7FF8` 与 `+EXPECT`（默认 0）。
也可以随时在程序里手动 `soc_halt(code)`。

> 注意：不要在自己的测试里定义 `_start` 或 `trap_entry`，`build_app.sh` 会检查并报错。
> `trap_handler()` 可以覆盖（见 1.3）。

### 1.3 陷阱/中断

`sw/lib/trap.S` 保存 caller-saved 寄存器，然后调用：

```c
void trap_handler(unsigned cause, unsigned epc, unsigned tval);
```

`sw/lib/soc.c` 里有一个 **weak** 默认实现：任何陷阱都会以
`0xBAD00000 | cause` 作为结果码 halt。

要处理定时器中断，就在自己的程序里定义一个强符号：

```c
void trap_handler(unsigned cause, unsigned epc, unsigned tval)
{
    (void)epc; (void)tval;
    if (cause & 0x80000000u) {          // 中断
        TIMER_CTRL = 0x0F;              // EN|IE|W1C|PERIODIC，清 pending
        /* ... */
        return;                          // 返回后 trap.S 执行 mret
    }
    soc_halt(0xE0000000u | (cause & 0xFFFFu));  // 异常：终止
}
```

启用中断的顺序（参考 `sw/example/main.c`）：

```c
soc_csrw_mtvec((unsigned)trap_entry);
TIMER_CMP  = 10000;
TIMER_CTRL = 0x0F;
soc_csr_set_mie(MIE_MTIE);
soc_csr_set_mstatus(MSTATUS_MIE);
```

`sw/lib/soc.h` 提供了 `soc_csrw_mtvec`、`soc_csr_set_mie` 等内联函数，以及
`MIE_MTIE` / `MSTATUS_MIE` 等宏。

---

## 2. 最小测试程序

新建 `sw/lab/main.c`：

```c
#include "soc.h"

int main(void)
{
    uart_init();
    uart_puts("lab start\n");

    /* ... 你的测试 ... */

    uart_puts("lab PASS\n");
    uart_flush();
    return 0;             // 非 0 会让 testbench 报 TEST_FAIL
}
```

运行：

```bash
bash scripts/build_app.sh sw/lab
```

参考输出（`sw/example`）：

```text
Sprouts500 example app
dtcm ok
timer ok
gpio ok
fp ok
irq ok
example PASS

APP_RESULT=0 (0x00000000) expected=0
APP_CYCLES=665558 UART_RX_BYTES=74
TEST_PASS
```

`APP_CYCLES` 是 halt 时的周期数（不含之后等待 UART 排空的时间）。

---

## 3. `build_app.sh` 实际做了什么

```bash
"${RISCV_PREFIX}gcc" \
    -march=rv32imacf_zicsr -mabi=ilp32 \
    -mcmodel=medany -msmall-data-limit=0 \
    -O2 -g -Wall -Wextra -ffreestanding -fno-common \
    -nostdlib -nostartfiles \
    -Wl,--no-relax -T sw/lib/app.ld \
    -I sw/lib -I <app-dir> \
    sw/lib/crt0.S sw/lib/trap.S sw/lib/soc.c sw/lib/string.c <app-sources> \
    -lgcc -o build/app/app.elf
```

关键参数：

| 参数 | 原因 |
|---|---|
| `-march=rv32imacf_zicsr` | core 是 RV32IMACF + Zicsr；默认启用 F 指令与 Zcf 压缩 load/store（`-mabi=ilp32` 仍是软浮点 ABI，浮点表达式由 GCC 生成 F 指令、结果经 fmv 往返） |
| `-mabi=ilp32` | 32 位 ABI |
| `-mcmodel=medany` | 不用 `gp` 基址，PC 相对寻址 |
| `-msmall-data-limit=0` | 与上一条配套；否则小数据会走未初始化的 `gp` |
| `-ffreestanding` | 没有 libc |
| `-nostdlib -nostartfiles` | 用 `sw/lib` 的启动代码 |
| `-Wl,--no-relax` | 与 demo 一致，避免链接器 relaxation |
| `-lgcc` | 提供 `__udivdi3` 等 64 位辅助函数 |

`sw/lib/string.c` 提供了 `memset` / `memcpy` / `memmove` / `memcmp` / `strlen`，
因为 `-O2` 常把循环或结构体初始化改写成对这些函数的调用。其中的
`no-tree-loop-distribute-patterns` 属性防止它们被再次优化成自我调用。

---

## 4. 常用模板

### 4.1 打印数字

```c
uart_puthex(0xDEADBEEF);    // 8 位十六进制
uart_putdec(123456);        // 无符号十进制
uart_puts("\n");
```

### 4.2 断言

```c
#define CHECK(cond, msg)                                        \
    do {                                                        \
        if (!(cond)) {                                          \
            uart_puts("FAIL: ");                                \
            uart_puts(msg);                                     \
            uart_puts("\n");                                    \
            uart_flush();                                       \
            soc_halt(1);                                        \
        }                                                       \
    } while (0)
```

### 4.3 延时

```c
soc_delay_cycles(1000);     // 用 cycle CSR，wrap-safe，不碰定时器
```

### 4.4 用定时器测时间

```c
TIMER_CMP  = 0xFFFFFFFFu;
TIMER_CTRL = TIMER_CTRL_EN;
unsigned t0 = TIMER_COUNT;
/* ... */
unsigned dt = TIMER_COUNT - t0;   // 无符号减法，溢出安全
TIMER_CTRL = 0;
```

### 4.5 把结果留在存储器里

除了返回值，也可以往 DTCM 写数组，让自定义 testbench 用
`dut.u_core.DTCM[index]` 检查（官方 ISA 套件就是读 ITCM 签名）：

```c
volatile unsigned *result = (volatile unsigned *)0x10000000;
result[0] = 0x12345678;
```

---

## 5. 外设怎么用

完整位定义和时序见 `docs/registers.md`，这里只给最小片段。

### 5.1 UART

```c
uart_init();                    // BAUD=867 (100 MHz/115200), TX_EN
uart_puts("hello\n");
uart_flush();                   // halt 前必须等最后一个停止位发完
```

接收：

```c
UART_CTRL |= UART_CTRL_RX_EN;
if (UART_STATUS & UART_STATUS_RX_VALID) {
    unsigned char c = UART_RXDATA;   // 读操作会清 RX_VALID
}
```

### 5.2 GPIO

```c
GPIO_DIR = 0x1;      // bit0 输出
GPIO_OUT = 0x1;
soc_delay_cycles(32);
unsigned v = GPIO_IN; // 同步后的引脚值
```

边沿中断：

```c
GPIO_IRQ_EN = 0x1;
GPIO_IRQ_PEND = 0x1;      // W1C：写 1 清 pending
```

`sim/tb_app.v` 支持用 `+GPIO_IN=<hex>` 驱动**配置为输入**的引脚，
例如 `+GPIO_IN=5a5a`。

### 5.3 SPI

```c
SPI_CLKDIV = 49;
SPI_CTRL   = SPI_CTRL_EN;        // 模式 0，MSB first
SPI_CS     = 0x1;                // 拉低 cs_n[0]
SPI_TXDATA = 0xA5;
while (!(SPI_STATUS & SPI_STATUS_DONE))
    ;
unsigned rx = SPI_RXDATA;        // 读清除 DONE
SPI_CS = 0;
```

`sim/tb_app.v` 加 `+SPI_LOOPBACK` 会把 `spi_mosi` 直接接回 `spi_miso`，
可以自测模式/位序。

### 5.4 定时器中断

见 1.3 和 `sw/example/main.c`。要在保持使能的同时清 pending，写
`TIMER_CTRL = 0x0F`（不是 `0x0B`）。

---

## 6. 仿真与调试

### 6.1 `tb_app.v` plusargs

| plusarg | 含义 |
|---|---|
| `+ITCM_HEX=<file>` | 镜像（`build_app.sh` 自动传） |
| `+MAX_CYCLES=<dec>` | 超时，默认 2,000,000；也可用环境变量 `MAX_CYCLES=...` |
| `+EXPECT=<dec>` | 期望结果码，默认 0 |
| `+DRAIN_NS=<dec>` | halt 后等待 UART 排空的时间，默认 1,000,000 ns |
| `+UART_FILE=<file>` | 把收到的 UART 字节另存一份（前 256 字节会被保存并写出） |
| `+GPIO_IN=<hex>` | 驱动输入引脚 |
| `+SPI_LOOPBACK` | MOSI 接回 MISO |
| `+VCD` | 生成 `build/tb_app.vcd` |

直接调用：

```bash
vvp build/app/tb_app.vvp +ITCM_HEX=build/app/app.hex +MAX_CYCLES=500000 +VCD
```

### 6.2 挂死

`tb_app.v` 超时会打印：

```text
APP_TIMEOUT cycles=<n> pc=<pc>
```

常见原因：没写 halt mailbox、镜像溢出 ITCM、访问未对齐地址触发异常
（默认 handler 会 halt，结果码是 `0xBAD00000|cause`）、中断没清 pending。

### 6.3 波形

```bash
bash scripts/build_app.sh sw/lab +VCD
gtkwave build/tb_app.vcd
```

建议先看 `dut.u_core.IF_PC`、`dut.u_core.ID_IR`、`dut.u_core.WB_PC`、
`dut.u_bus.req/addr/we/rdata`、`uart_tx`。

### 6.4 UART 输出保存

```bash
bash scripts/build_app.sh sw/lab +UART_FILE=build/app/uart.txt
cat build/app/uart.txt
```

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `APP_RESULT` 非 0 / `TEST_FAIL` | `main` 返回值不是 0；或程序显式 `soc_halt(code)` |
| 一直超时 | 没 halt；或镜像超过 32 KiB（链接断言会先报错） |
| 结果码 `0xBAD0000x` | 发生了未处理异常（x = mcause）；检查未对齐访问/非法指令 |
| `undefined reference to __udivdi3` | 确认链接了 `-lgcc`（脚本已带） |
| `undefined reference to memset/memcpy` | 用 `sw/lib/string.c`，或自己实现并加 `no-tree-loop-distribute-patterns` |
| UART 最后一字节丢失 | halt 前调用 `uart_flush()`；或加大 `+DRAIN_NS` |
| 程序改 `main.c` 后 demo 失败 | `tb_soc.v` 期望字符串写死，demo 流程不适合任意程序，用 `tb_app.v` |
| 想测 UART RX | 当前 tb 固定 `uart_rx=1`；在 `tb_app.v` 里加一个发送 task 或外部激励 |
| 想测 GPIO 输入 | `+GPIO_IN=<hex>`，注意对应 pin 要先设 `DIR=0` |
| 想测 SPI 从机 | `+SPI_LOOPBACK`；更真实的从机模型需要自己写 |

---

## 8. 不用脚本时的等价命令

```bash
RISCV_PREFIX=riscv64-unknown-elf-
APP=sw/lab

${RISCV_PREFIX}gcc -march=rv32imacf_zicsr -mabi=ilp32 \
    -mcmodel=medany -msmall-data-limit=0 -O2 -ffreestanding \
    -nostdlib -nostartfiles -Wl,--no-relax -Wl,-T,sw/lib/app.ld \
    -I sw/lib -I $APP \
    sw/lib/crt0.S sw/lib/trap.S sw/lib/soc.c sw/lib/string.c $APP/*.c \
    -lgcc -o build/app/app.elf

${RISCV_PREFIX}objcopy -O binary build/app/app.elf build/app/app.bin
# 再按 build_app.sh 里的 python 片段打包成 app.hex，然后：
iverilog -g2012 -o build/app/tb_app.vvp sim/tb_app.v \
    rtl/soc_top.v rtl/core.v rtl/bus.v rtl/timer.v rtl/gpio.v rtl/uart.v rtl/spi.v
vvp build/app/tb_app.vvp +ITCM_HEX=build/app/app.hex
```

---

## 9. 下载到 FPGA 运行

普通 `build_app.sh` 链接到地址 0，供 `tb_app.v` 直接预载；FPGA UART Boot 应用需要使用
`sw/boot/app.ld`，入口改为 `0x810`：

```bash
bash scripts/build_boot.sh
bash scripts/build_fpga_app.sh sw/lab
python3 scripts/uart_boot.py --port /dev/ttyUSB0 \
    --image build/fpga/app/app.bin
```

`build_fpga_app.sh` 通过 `RUN_SIM=0`、`APP_LD=...` 和 `OUT=build/fpga/app` 复用同一套
`crt0/trap/soc/string` 运行时，并生成带 magic/长度/入口/CRC32 的 `app.pkg`。按住低有效
BOOT0（GPIO[0]）复位后运行主机脚本即可。地址布局、协议、写保护和限制见
`docs/fpga_boot.md`。

---

## 10. 官方参考

通用 RISC-V 裸机资料（不涉及本仓库的存储器映射和外设）：

- **RISC-V ELF psABI Specification**：调用约定、启动约定。
- **riscv-arch-test**（`tests/isa/` 的来源）：签名机制、`RVMODEL_*` 宏。
- **RISC-V 指令集手册**与 `riscv-opcodes`（`tests/isa/env/encoding.h` 的来源）。

本仓库相关的权威信息以 `docs/architecture.md`、`docs/registers.md` 和
`sw/example/main.c` 为准。
