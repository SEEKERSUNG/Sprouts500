# 存储器映射与外设寄存器

## 1. 总线窗口

| 区域 | 基地址 | 大小 | Tag（`addr[31:28]`） |
|---|---|---|---|
| ITCM | `0x0000_0000` | 32 KiB（SoC），4 MiB（ISA 仿真） | `0x0` |
| DTCM | `0x1000_0000` | 16 KiB | `0x1` |
| 定时器 | `0x2000_0000` | 4 KiB | `0x2` |
| GPIO | `0x2000_1000` | 4 KiB | `0x2` |
| UART | `0x2000_2000` | 4 KiB | `0x2` |
| SPI | `0x2000_3000` | 4 KiB | `0x2` |

总线译码 `addr[31:12]`：

```
0x20000 -> timer
0x20001 -> gpio
0x20002 -> uart
0x20003 -> spi
```

未映射的总线读返回 0；未映射写被忽略。所有外设寄存器为 32 位、按 word 寻址。
`wstrb` 选择写的字节 lane。

## 2. 总线协议

Core 侧：

```
bus_req_o, bus_we_o, bus_addr_o, bus_wdata_o, bus_wstrb_o
bus_rdata_i, bus_ready_i
```

- core 在 MEM 中为总线访问拉高 `req`，并保持到 `bus_ready_i`。
- `bus_addr_o` 按 word 对齐。
- 总线在 `req && !req_q` 时启动事务，并向选中的外设发出单周期 `we` 或 `rd` strobe。
- 读数据组合多路选择后寄存到 `rdata_hold`；`req` 保持高时驱动保持值。这样 core 可在
  总线前冻结，而不会重复有副作用的读。
- `ready` 当前恒为 1。未来的慢外设可以拉低它；core 的 `bus_wait` 会冻结流水线。
- core 在连续总线访问之间插入气泡（`bus_conflict`），使总线为每个事务看到新的 `req`
  边沿。

## 3. 定时器（`0x2000_0000`）

| 偏移 | 名称 | 访问 | 复位 | 说明 |
|---|---|---|---|---|
| 0x00 | CTRL | RW | 0 | [0] EN，[1] IRQ_EN，[2] PENDING（W1C），[3] PERIODIC |
| 0x04 | COUNT | RO | 0 | 当前计数值 |
| 0x08 | CMP | RW | `0xffff_ffff` | 比较值 |
| 0x0C | PRE | RW | 0 | 预分频；每 `PRE+1` 个时钟计一次 |

- `EN=1` 时，计数器每个预分频 tick 递增。
- `COUNT >= CMP` 时置位 `PENDING`。周期模式下 `COUNT` 清 0，定时器继续运行；
  否则清除 `EN`。
- `irq = PENDING && IRQ_EN`。
- 写 CTRL 且 bit 2 为 1 会清除 `PENDING`（write-one-to-clear）。bit 0、1、3 由同一次
  写更新。
- 要在保持定时器使能 + 周期模式的同时清 pending，写 `0x0f`。
- `COUNT` 只读；写被忽略。

典型配置：

```c
TIMER_CMP  = 5000;
TIMER_CTRL = 0x0f;      // EN|IE|W1C|PERIODIC
```

## 4. GPIO（`0x2000_1000`）

| 偏移 | 名称 | 访问 | 复位 | 说明 |
|---|---|---|---|---|
| 0x00 | DIR | RW | 0 | 1 = 输出 |
| 0x04 | OUT | RW | 0 | 输出值 |
| 0x08 | IN | RO | 0 | 两级触发器同步后的引脚值 |
| 0x0C | IRQ_EN | RW | 0 | 逐引脚中断使能 |
| 0x10 | IRQ_PEND | W1C | 0 | 逐引脚边沿 pending |

- `gpio_o = OUT`，`gpio_oe = DIR`；SoC 把 `gpio_pin` 驱动为三态。
- `IN` 同步到 `clk`；它反映 pad 值（对于输出，就是驱动值）。
- 任一引脚变化（`in_s0 ^ in_s1`）且对应 `IRQ_EN` 位为 1，会置位 `IRQ_PEND`。
- `irq = |IRQ_PEND`；软件通过向 `IRQ_PEND` 写 1 清除 pending。
- 通过 `wstrb` 支持字节写。

## 5. UART（`0x2000_2000`）

| 偏移 | 名称 | 访问 | 复位 | 说明 |
|---|---|---|---|---|
| 0x00 | CTRL | RW | 0 | [0] TX_EN，[1] RX_EN，[2] TX_IRQ，[3] RX_IRQ |
| 0x04 | STATUS | RO | 0 | [0] TX_BUSY，[1] RX_VALID，[2] TX_DONE，[3] RX_OVERRUN |
| 0x08 | BAUD | RW | 433 | 每比特时钟数 - 1 |
| 0x0C | TXDATA | WO | - | 写入待发送字节 |
| 0x10 | RXDATA | RO | 0 | 接收字节；读取清除 RX_VALID/OVERRUN |

- 8N1，LSB first，空闲为高。
- `BAUD = clk_freq / baud - 1`；复位值 433 对应 50 MHz 下的 115200。
- TX：在 `TX_EN=1` 且 `TX_BUSY=0` 时写 TXDATA 会启动一帧。`TX_DONE` 在停止位结束
  时置位，并保持到下一次写 TXDATA 或关闭 TX。
- RX：下降沿启动一帧；起始位在半比特处采样，随后数据位和停止位按完整比特间隔采样。
  有效停止位后置位 `RX_VALID`；读 RXDATA 清除它。
- 如果 `RX_VALID` 仍为 1 时新字节完成，会置位 `RX_OVERRUN`，新字节覆盖旧字节。
  没有 FIFO。
- `irq = (TX_DONE && TX_IRQ) || (RX_VALID && RX_IRQ)`。
- 软件应在每次写前轮询 `TX_BUSY`，或在 TX_DONE 中断中写下一个字节。

典型配置：

```c
UART_BAUD = 433;
UART_CTRL = 0x1;        // 使能 TX
while (UART_STATUS & 1) ;
UART_TXDATA = 'H';
```

## 6. SPI（`0x2000_3000`）

| 偏移 | 名称 | 访问 | 复位 | 说明 |
|---|---|---|---|---|
| 0x00 | CTRL | RW | 0 | [0] EN，[1] CPOL，[2] CPHA，[3] IRQ，[4] LSB_FIRST |
| 0x04 | STATUS | RO | 0 | [0] BUSY，[1] DONE |
| 0x08 | CLKDIV | RW | 24 | SCLK 半周期（时钟数）- 1 |
| 0x0C | TXDATA | WO | - | 写入字节以启动传输 |
| 0x10 | RXDATA | RO | 0 | 接收字节；读取清除 DONE |
| 0x14 | CS | RW | 0 | bit n = 1 拉低 `cs_n[n]` |

- 仅主机，8 位，模式 0-3。`SCLK = clk / (2 * (CLKDIV+1))`。
- 软件控制片选：写 TXDATA 前置位 `CS` 对应位，传输完成后清除。
- `EN=1` 且 `BUSY=0` 时，写 `TXDATA` 启动传输；`DONE` 在结束时置位，并保持到下一次
  写 TXDATA 或读 RXDATA。
- `DONE` 后 `RXDATA` 有效；读它清除 DONE。
- `irq = DONE && IRQ`。
- MISO 用两级触发器同步。
- 数据按 CPHA 在前沿/后沿采样；CPOL 选择空闲时钟电平。

典型的模式 0 回环：

```c
SPI_CLKDIV = 24;
SPI_CTRL   = 0x1;       // 使能，模式 0，MSB first
SPI_CS     = 0x1;       // 拉低 cs_n[0]
SPI_TXDATA = 0xa5;
while (!(SPI_STATUS & 0x2)) ;
rx = SPI_RXDATA;
SPI_CS = 0;
```

## 7. 中断连接

```
timer_irq  -> core.timer_irq_i  -> mip.MTIP (bit 7)
gpio_irq   -\
uart_irq    > OR -> core.ext_irq_i -> mip.MEIP (bit 11)
spi_irq    -/
soft_irq   -> 0
```

`mie` 位：MTIE=bit 7，MEIE=bit 11。`mstatus.MIE` 是全局使能。外部线是共享的；
软件读取外设状态/pending 寄存器来识别来源。

## 8. 复位值

| 寄存器 | 复位值 |
|---|---|
| 定时器 CTRL/COUNT/PRE | 0 |
| 定时器 CMP | `0xffff_ffff` |
| GPIO DIR/OUT/IRQ_EN/PEND | 0 |
| UART CTRL/STATUS | 0 |
| UART BAUD | 433 |
| SPI CTRL/STATUS/CS | 0 |
| SPI CLKDIV | 24 |
| CSR | 0 |
