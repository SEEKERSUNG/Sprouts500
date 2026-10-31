# Sprouts500

一个最简的五级流水线 RV32IM MCU IP，包含一个简易总线，以及定时器、GPIO、UART、SPI 外设。

本仓库**仅支持 Ubuntu/Linux**。`scripts/setup_ubuntu.sh` 安装主机工具，
`scripts/setup_opensta.sh` 将 CUDD 3.0.0 + OpenSTA 安装到系统目录 `/opt/opensta`，
并在 `/usr/local/bin/sta` 建立命令链接。

为保证构建/仿真性能，请把代码树放在原生 Linux 文件系统上（不要放在网络/9p 挂载目录下）。

## 状态

| 项目 | 结果 |
|---|---|
| RV32I/RV32M `riscv-arch-test` 2.x | **46/46 通过**（Ubuntu 26.04，GCC 14.2，Icarus 12） |
| SoC demo：UART hello + 定时器中断 + GPIO | 通过（Ubuntu 26.04） |
| cocotb 随机/定向验证 | SoC/外设 72 项 + core 微架构 64 项，全部通过 |
| riscv-dv + Sail 指令级随机 | RV32IM 随机程序 lockstep 比对通过 |
| Yosys + Nangate45 门级综合 | Yosys 0.52，ABC `-fast`，2-word TCM 时序配置 + 驱动强度提升 |
| OpenSTA 时序 | **50 MHz pre-CTS MET**：setup +16.01 ns，hold +0.058 ns，TNS/WNS 0.00（OpenSTA 3.1.0，理想时钟） |
| CoreMark 1.0 | **130.12 CoreMark**（**2.60 CoreMark/MHz**，50 MHz） |
| Dhrystone 2.1 | **65,696 Dhrystones/s**（**37.4 DMIPS**，**0.747 DMIPS/MHz**，50 MHz） |

原始单体 core 保存在 `archive/core_v1.v`。

## 快速开始

```bash
# 1. 主机工具（RISC-V GCC、iverilog、yosys、TCL/SWIG/...）
bash scripts/setup_ubuntu.sh

# 2. 官方 ISA 测试
python3 scripts/fetch_isa.py       # tests/isa 已 vendored；此步可选
python3 scripts/run_isa.py         # 期望 46/46

# 3. 裸机 SoC demo（UART + 定时器中断 + GPIO）
bash scripts/build_demo.sh         # 期望 TEST_PASS

# 4. 自己的 C 测试程序（运行时在 sw/lib，参考程序 sw/example）
bash scripts/build_app.sh sw/example   # 期望 TEST_PASS

# 5. 性能分数（CoreMark + Dhrystone，可选）
bash scripts/build_bench.sh        # 约 2 分钟；详见 docs/benchmarks.md

# 6. 扩展验证（可选；cocotb 需要 python3.14-dev）
bash scripts/setup_cocotb.sh       # 一次性：.venv + cocotb/pytest
bash scripts/run_cocotb.sh         # cocotb/soc + cocotb/core
bash scripts/setup_riscv_dv.sh     # 一次性：.venv-dv + Sail 0.16
bash scripts/run_riscv_dv.sh       # riscv-dv + Sail lockstep

# 7. 综合 + 时序
bash scripts/fetch_lib.sh          # Nangate45 Liberty（lib/ 中已附带）
bash scripts/synth.sh              # 生成 build/syn/core_netlist.v
sudo bash scripts/setup_opensta.sh # 一次性安装 CUDD + OpenSTA 到 /opt/opensta
bash scripts/sta.sh                # 50 MHz STA 报告
```

`make help` 列出相同目标。`make isa T=jalr-01` 运行单个测试。

### 改完 RTL 后怎么回归

不需要手动编译，也不需要重跑 `fetch_isa.py`（它只下载测试源码）；各 runner 每次
都会用当前 RTL 重新编译：

```bash
python3 scripts/run_isa.py       # core 指令语义
bash scripts/build_demo.sh       # SoC 集成
bash scripts/build_app.sh sw/example
bash scripts/run_cocotb.sh       # 微架构/总线/外设
bash scripts/run_riscv_dv.sh     # 指令级随机 lockstep
# 涉及时序/结构时，顺序不能反：
bash scripts/synth.sh && bash scripts/sta.sh
```

按改动类型选择门禁、单测命令和日志位置见 `docs/verification.md` §1.1。

## 文件布局

```
rtl/core.v       五级 RV32IM 核心 + ITCM/DTCM + CSR/陷阱 + M 扩展
rtl/bus.v        地址译码 / 读多路选择 / 单次 strobe
rtl/timer.v      32 位定时器
rtl/gpio.v       32 位 GPIO，带边沿中断
rtl/uart.v       8N1 UART
rtl/spi.v        8 位 SPI 主机，模式 0-3
rtl/soc_top.v    SoC 集成

sim/tb_core.v    仅核心的 ISA testbench
sim/tb_soc.v     SoC demo testbench（UART 解码器 + halt mailbox）
sim/tb_app.v     通用用户程序 testbench（result mailbox + UART 日志）
sw/isa/          riscv-arch-test 目标适配层（model_test.h、link.ld）
sw/demo/         裸机 hello + 定时器中断 demo
sw/lib/          自写 C 程序的运行时（crt0/trap/UART/string/链接脚本）
sw/example/      参考用户程序（DTCM/定时器/GPIO/中断）
bench/           CoreMark / Dhrystone 源码、移植层和共用运行时
cocotb/          cocotb 验证：soc/（总线+外设）、core/（微架构）、common/（BFM/模型）
verif/riscv_dv/  riscv-dv 生成器（vendored）与 lockstep 比对脚本
scripts/         安装/抓取/构建/运行/综合/STA 脚本
tests/isa/       官方 ISA 测试源码 + 参考签名
lib/             Nangate45 Liberty
constraints/     SDC
docs/            架构、验证、寄存器、工具链、路线图、benchmark
archive/         原始 core
```

## 存储器映射

| 区域 | 地址 | 大小 |
|---|---|---|
| ITCM | `0x0000_0000` | 32 KiB（ISA testbench 为大型测试使用 4 MiB） |
| DTCM | `0x1000_0000` | 16 KiB |
| 定时器 | `0x2000_0000` | 4 KiB |
| GPIO | `0x2000_1000` | 4 KiB |
| UART | `0x2000_2000` | 4 KiB |
| SPI | `0x2000_3000` | 4 KiB |

## 外设寄存器映射

### 定时器（`0x2000_0000`）

| 偏移 | 名称 | 访问 | 位 |
|---|---|---|---|
| 0x00 | CTRL | RW | [0] EN，[1] IRQ_EN，[2] PENDING（W1C），[3] PERIODIC |
| 0x04 | COUNT | RO | 当前计数值 |
| 0x08 | CMP | RW | 比较值 |
| 0x0C | PRE | RW | 预分频（每 PRE+1 个时钟计一次） |

IRQ = pending & IRQ_EN。要在保持定时器使能的同时清除 pending，写 `0x0f`
（EN|IRQ_EN|W1C|PERIODIC），而不是 `0x0b`。

### GPIO（`0x2000_1000`）

| 偏移 | 名称 | 访问 | 说明 |
|---|---|---|---|
| 0x00 | DIR | RW | 1 = 输出 |
| 0x04 | OUT | RW | 输出值 |
| 0x08 | IN | RO | 同步后的引脚值 |
| 0x0C | IRQ_EN | RW | 逐引脚中断使能 |
| 0x10 | IRQ_PEND | W1C | 逐引脚边沿 pending |

### UART（`0x2000_2000`）

| 偏移 | 名称 | 访问 | 位 |
|---|---|---|---|
| 0x00 | CTRL | RW | [0] TX_EN，[1] RX_EN，[2] TX_IRQ，[3] RX_IRQ |
| 0x04 | STATUS | RO | [0] TX_BUSY，[1] RX_VALID，[2] TX_DONE，[3] RX_OVERRUN |
| 0x08 | BAUD | RW | 每比特时钟数 - 1（复位值 433 = 50 MHz 下的 115200） |
| 0x0C | TXDATA | WO | 写入待发送字节 |
| 0x10 | RXDATA | RO | 读取接收字节（清除 RX_VALID） |

### SPI（`0x2000_3000`）

| 偏移 | 名称 | 访问 | 位 |
|---|---|---|---|
| 0x00 | CTRL | RW | [0] EN，[1] CPOL，[2] CPHA，[3] IRQ，[4] LSB_FIRST |
| 0x04 | STATUS | RO | [0] BUSY，[1] DONE |
| 0x08 | CLKDIV | RW | SCLK 半周期（时钟数）- 1 |
| 0x0C | TXDATA | WO | 写入字节以启动传输 |
| 0x10 | RXDATA | RO | 接收字节（清除 DONE） |
| 0x14 | CS | RW | bit n = 1 拉低 cs_n[n] |

软件在写 TXDATA 前拉低 CS，传输完成后再释放。

## 核心架构（简版）

- IF/ID/EX/MEM/WB。EX/MEM->ID 前递消除 ALU 相关停顿；只有 load-use 需要停顿
  （2 周期，数据在 WB 才可用）。x0 硬连线，优先于所有前递。
- IF 级分支预测：32 项 BTB + 2-bit 饱和计数器。预测正确的 taken 分支/跳转无惩罚；
  预测错误仍走 EX 重定向与冲刷（约 3 周期）。
- `mul` 为单周期；`div/divu/rem/remu` 迭代 32 步并冻结流水线。
- 陷阱在 EX 处理。MEM/WB 中更老的指令正常完成；出错/被中断的指令被取消，并在
  `mret` 后重新执行。
- ITCM 有两个读端口（取指 + 数据）；DTCM 有一个数据读端口。字节写使用 `wstrb`
  和逐字节写使能。
- 总线为单 outstanding。事务在 `req` 上升沿开始；`req` 保持高时总线保持读数据。
  核心在连续总线访问之间插入一个气泡（`bus_conflict`），使每次访问都有干净的边沿。

完整说明、不变量和信号级细节见 `docs/architecture.md`。

## 验证

- `python3 scripts/run_isa.py` 用官方参考签名编译并运行每个 `rv32i_m/I` 和
  `rv32i_m/M` 测试，然后比较 RTL 导出的签名。
- `sim/tb_core.v` 包含 4 MiB 仿真 ITCM（`jal-01` 的对齐填充需要），halt mailbox 位于
  `0x003F_FFF0`，并支持可选的流水线跟踪（`+TRACE`、`+TRACE_PIPE`）。
- `sim/tb_soc.v` 启动 `sw/demo`，以 115200 波特率解码 UART，并检查
  `Hello Sprouts500!\nperipherals ok\nirq ok\n`。
- `sim/tb_app.v` 是通用用户程序 testbench：程序在 `0x7FF8` 写结果码、
  `0x7FFC` 写 halt magic，testbench 打印 UART 并比较结果码。
  `scripts/build_app.sh <dir>` 编译 `sw/lib` + 程序目录并运行；
  详见 `docs/c_programming.md`。
- `tests/isa/env/arch_test.h` 包含针对旧 `jalr-01` 宏的本地兼容补丁（新版 binutils
  拒绝 `la x0,...`；补丁改为发出 `nop`）。
- `cocotb/soc/` 用 cocotb + Icarus 覆盖总线协议（`ready` 拉低、背靠背访问、
  `bus_conflict`）、定时器/GPIO/UART/SPI 全部寄存器与边界，带独立参考模型和
  scoreboard。`cocotb/core/` 覆盖流水线相关停顿、CSR/陷阱、中断时序、除法冻结、
  TCM 写门控。运行：`bash scripts/setup_cocotb.sh && bash scripts/run_cocotb.sh`。
- `verif/riscv_dv/` 用 riscv-dv 生成 RV32IM 随机程序，在 RTL 与 Sail 参考模型上
  逐条比对退休指令、寄存器写和存储，并固定跑一个定向陷阱镜像。运行：`bash scripts/setup_riscv_dv.sh &&
  bash scripts/run_riscv_dv.sh`。

testbench plusarg、调试流程和预期结果见 `docs/verification.md`；cocotb 见
`docs/verification_cocotb.md`、`docs/verification_cocotb_core.md`；指令级随机见
`docs/verification_riscv_dv.md`。

## 性能

50 MHz、`-O2 -march=rv32im_zicsr`、RV32IM（无 FPU、无 cache），数据位于 DTCM。
带 EX/MEM->ID 前递和 IF 级分支预测（相比前递前的 79.86 / 41,945 约提升 63% / 57%）：

| Benchmark | 分数 | 归一化 |
|---|---|---|
| CoreMark 1.0 | **130.12** | **2.60 CoreMark/MHz** |
| Dhrystone 2.1 | **65,696 Dhrystones/s** | **37.4 DMIPS**，**0.747 DMIPS/MHz** |

```bash
bash scripts/build_bench.sh coremark     # CoreMark
bash scripts/build_bench.sh dhrystone    # Dhrystone
bash scripts/build_bench.sh              # 两个都跑
make bench                               # 同上
```

分数由 core 的 `cycle` CSR 精确计数，不是仿真器 wall-clock。默认运行长度很短
（Dhrystone 500 轮 ≈ 0.008 秒，CoreMark 1 次迭代 ≈ 0.008 秒），**不满足**
Dhrystone 的 2 秒惯例和 CoreMark 的 10 秒官方规则，因此这些是周期归一化分数，
不是认证分数。运行次数、官方长度命令和限制见 `docs/benchmarks.md` §4.3。

## 综合与时序

- `scripts/synth.sh` 在 CPU 时序运行时把 ITCM/DTCM 缩为 2 word，因为 RTL 使用推断
  寄存器阵列，而不是编译好的 SRAM 宏。这样既保留有代表性的 CPU 逻辑和除法器，又避免
  产生数百万个推断 FF。完整签核请使用存储器编译器与 Liberty RAM 模型。
- `constraints/core.sdc` 目标为 20 ns 时钟（50 MHz）、2 ns I/O 延迟、0.05 pF 负载。
- 当前 pre-CTS 结果（OpenSTA 3.1.0，理想时钟）：setup +16.01 ns，hold +0.058 ns，
  TNS/WNS 0.00。CTS 后以及换用 SRAM 宏模型后需重新检查 hold。
- `scripts/setup_opensta.sh` 按 OpenSTA 官方 Ubuntu Dockerfile 的流程，在 `/opt/opensta`
  下从源码构建 CUDD 3.0.0 和 OpenSTA，安装为 `/opt/opensta/bin/sta` 和
  `/usr/local/bin/sta`。登录 shell 通过 `/etc/profile.d/opensta.sh` 把
  `/opt/opensta/bin` 加入 PATH。`scripts/sta.sh` 运行 `scripts/sta.tcl`。

## 已知限制

- 不支持 A/F/D/C 扩展，无 cache、无 MMU、无调试模块。
- 非对齐 load/store 和非对齐控制流跳转会产生陷阱。
- `csrrs/csrrc` 且 `rs1=x0`、`csrrsi/csrrci` 且 `uimm=0` 时不写。
- UART 只有一个接收保持寄存器（带 overrun 状态）。
- SPI 仅支持主机模式，片选由软件控制。
- 总线为单 outstanding、顺序执行。
- 尚未运行 ISA `privilege` 套件（需要完整陷阱 trampoline、PMP、delegation）。

## 仓库说明

- `build/`、`logs/` 已被忽略。OpenSTA/CUDD 安装在系统目录 `/opt/opensta`，不在仓库内。
- `tests/isa/` 与 `lib/` 已 vendored，保证离线可复现验证。
- 项目目标环境为 Ubuntu 24.04/26.04，使用 `gcc-riscv64-unknown-elf`、
  Icarus Verilog 12+、Yosys 和 OpenSTA。已验证环境为 Ubuntu 26.04。

## 写在最后

本项目始于2023年，前序项目是[Sprouts100](https://github.com/SEEKERSUNG/Sprouts100)。Sprouts100是我大三时为了学习RISC-V处理器设计、数字集成电路、计算机原理等相关知识而开发的。后来，我又设计了一个2级流水、性能更强的核心，并独立完成了除法器、数据前递（forwarding）等针对性优化。但受限于时序，该核心在Xilinx A7 35T FPGA上最高只能跑到48 MHz。于是，2023年读研期间，我利用业余时间开发了采用5级流水的本项目，以进一步优化时序；不过，分支预测错误带来的额外开销也会对性能造成不小影响。后来由于学业繁忙，我无暇继续推进本项目。所幸AI时代到来，它可以帮我完成大量编码和验证工作，我只需作为“架构师”补全和完善项目。未来，本项目将在保持简洁设计的同时，继续向更高性能探索，努力实现MCU“小钢炮”的目标。