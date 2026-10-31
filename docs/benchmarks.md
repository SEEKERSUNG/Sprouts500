# CoreMark 与 Dhrystone 性能测试

本文说明如何在 Sprouts500 上编译、运行 CoreMark 和 Dhrystone，报告的含义，以及
重新测量时的注意事项。

## 1. 快速运行

```bash
bash scripts/build_bench.sh coremark              # CoreMark best，约 95 秒
bash scripts/build_bench.sh dhrystone             # Dhrystone best，约 41 秒
MODE=ground bash scripts/build_bench.sh dhrystone # Dhrystone ground rule，约 48 秒
bash scripts/build_bench.sh                       # 两个都跑（各自 best）
make bench                                        # 同上
```

参数：

| 变量 | 默认 | 作用 |
|---|---|---|
| `MODE=best\|ground` | `best` | 优化级别策略，见 §2 |
| `ITERATIONS=<n>` | 1 | CoreMark 迭代次数 |
| `DHRY_RUNS=<n>` | 500 | Dhrystone 每轮的运行次数 |
| `MAX_CYCLES=<n>` | 120000000 | 仿真超时（时钟周期） |
| `BENCH_OPT_FLAGS="..."` | 空 | 覆盖 best 模式的优化参数（调参用） |

产物在 `build/bench/`，日志在 `logs/bench/`（`<name>.log` 为完整输出，
`<name>.uart` 为 UART 原始字符）。

## 2. 当前结果

环境：50 MHz（`sim/tb_bench.v` 的时钟），数据在 DTCM，RV32IM（无 FPU、无 cache），
带 EX/MEM->ID 前递与 IF 级分支预测。GCC 14.2.0。

| Benchmark | 模式 | 编译 | 分数 | 归一化 | 周期 |
|---|---|---|---|---|---|
| CoreMark 1.0 | best | `-O2 -flto -funroll-loops -fipa-pta` | **142.14** | **2.84 CoreMark/MHz** | 351,767（1 次迭代） |
| Dhrystone 2.1 ground | ground | `-O2`（保留源码的 `no-inline` pragma） | **65,696 Dhrystones/s** | **37.4 DMIPS**，0.747 DMIPS/MHz | 380,537（500 轮） |
| Dhrystone 2.1 best | best | `-O2 -flto`（去掉 `no-inline` pragma） | **124,345 Dhrystones/s** | **70.8 DMIPS**，**1.415 DMIPS/MHz** | 201,053（500 轮） |

换算：CoreMark = 迭代数 × 频率 / 周期；DMIPS = Dhrystones/s ÷ 1757；
DMIPS/MHz = DMIPS ÷ 50。

### 2.1 ground rule 与 best 的含义

- **Ground rule**：benchmark 源码完全未修改（包括 Dhrystone 自己写死的
  `#pragma GCC optimize("no-inline")`），只使用 `-O2`。这是可复现、可与其他核公平
  比较的基线。
- **Best**：不修改 benchmark 的算法和测试数据，只调整编译方式：
  - Dhrystone：把 `dhrystone.c`/`main.c` 复制到 `build/bench/dhrystone_best_src/`，
    仅删除那一条非算法性的 `no-inline` pragma，再以 `-O2 -flto` 编译；
  - CoreMark：源码原样，使用 `-O2 -flto -funroll-loops -fipa-pta`。
  - 两者都没有改测试逻辑、没有改结果校验、没有手写替代函数。

可复现的实验（同一 RTL，只换编译参数）：

| 版本 | 周期 | 说明 |
|---|---|---|
| Dhrystone `-O2`（源码原样） | 380,537 | ground |
| Dhrystone `-O2 -flto` | 201,053 | 去掉 `no-inline` pragma 后 |
| Dhrystone `-O3` | 321,040 | 明显更慢，未采用 |
| Dhrystone `-O2 -flto -mtune=sifive-e76` | 234,050 | 调度模型调优反而变慢 |
| CoreMark `-O2` | 384,273 | 前递/预测后 |
| CoreMark `-O3 -flto` | 355,534 | |
| CoreMark `-O2 -flto -funroll-loops` | 351,767 | |
| CoreMark `-O2 -flto -funroll-loops -fipa-pta` | 350,337–351,767 | 采用（LTO 布局有约 0.4% 波动，文档取较保守的 142.14） |
| CoreMark `-O3 -flto` + 默认 unroll | > 32 KiB | ITCM 放不下，弃用 |

## 3. 运行机制

### 3.1 仿真与时间基准

两个 benchmark 都在 `sim/tb_bench.v` 上运行完整的 SoC（core + 总线 + 外设）：

- 时钟 50 MHz，与 `docs/toolchain.md` 的 SDC 一致；
- 时间基准是 core 的 `cycle` CSR（`rdcycle`），不是定时器外设；
- UART 以 115200 解码，benchmark 的报告就是 UART 输出；
- 结束时程序写 ITCM `0x7FFC` 的 halt magic，testbench 打印
  `BENCH_RESULT`、`BENCH_CYCLES` 和 `BENCH_PASS`。

因此报告中的“秒”是**仿真周期 ÷ 50 MHz**，与实际硬件在 50 MHz 下运行的结果相同，
前提是 RTL 和综合网表行为一致。

### 3.2 CoreMark

- 上游源码（EEMBC，Apache-2.0）未修改，放在 `bench/raw/coremark/`；
- 使用上游 `barebones` 移植层，改动在 `bench/coremark/port/`：
  - `core_portme.h/.c`：无 stdio/time.h、`HAS_FLOAT=0`、`CLOCKS_PER_SEC=50000000`、
    `ITERATIONS` 由编译参数给出；
  - `ee_printf.c`：只把 `uart_send_char` 换成写 UART；
  - `main.c`：`core_main.c` 以 `-Dmain=coremark_main` 编译，wrapper 在它之后用
    同样的 start/stop 计时算出带小数的 CoreMark 分数（上游在 `HAS_FLOAT=0` 时只打印
    整数秒）。
- 数据区是静态数组（`MEM_STATIC`，约 2 KB），放在 DTCM。

### 3.3 Dhrystone

- 上游源码取自 `riscv-software-src/riscv-tests` 的 `benchmarks/dhrystone`
  （BSD 许可），放在 `bench/raw/dhrystone/`；
- 改动在 `bench/dhrystone/port/`：
  - `dhrystone.h`：`HZ` 改为 50 MHz，`NUMBER_OF_RUNS` 可由编译参数覆盖；
  - `dhry_patch.h`：强制包含，提供 `read_csr(mcycle)`（`rdcycle`）、`memcpy`/
    `strcpy`/`strcmp`/`printf` 原型；
  - `main.c`：去掉 `util.h`（测试框架头文件），补 `bench_uart_init()`，用 64 位中间值
    计算 Dhrystones/s 和 DMIPS，最后调用 `bench_finish`。
- `HZ` 改为 50 MHz 后，`Dhrystones/s = HZ × Runs / User_Time` 直接由 50 MHz 周期
  数得到；`Microseconds` 由 64 位中间值计算，避免 ilp32 下 `long` 溢出。

### 3.4 共用运行时

`bench/runtime/`：

| 文件 | 作用 |
|---|---|
| `bench_io.c` | UART 初始化/输出、整数 printf、定点打印、`bench_finish` |
| `string.c` | `memset`/`memcpy`/`memmove`/`memcmp`/`strlen` |
| `bench_port.h` | 供两个移植层共用的原型 |

`bench/include/` 提供最小的 `string.h`、`stdio.h`、`alloca.h`（工具链没有 newlib
头文件）。

`bench/start.S` + `bench/bench.ld`：

- 镜像（`.text/.rodata/.data`）在 ITCM 32 KiB 内；
- `.bss` 和栈在 DTCM 16 KiB；
- `_start` 设栈、清 `.bss`、调用 `main`，返回值交给 `bench_finish`。

## 4. 结果解读

### 4.1 CoreMark

上游输出（best）：

```text
2K performance run parameters for coremark.
CoreMark Size    : 666
Total ticks      : 351767
[0]crclist       : 0xe714
[0]crcmatrix     : 0x1fd7
[0]crcstate      : 0x8e3a
[0]crcfinal      : 0xe714
CoreMark 1.0 : 142.139 CoreMark
```

- CRC 值与 EEMBC 公布的 2K 参考值一致，说明结果正确；
- `ERROR! Must execute for at least 10 secs` 是官方报告规则，仿真不跑 10 秒
  （50 MHz 下需要约 5 亿周期），但分数本身对任意迭代数基本一致（加入前递/预测后
  1 次迭代 142.139 CoreMark（best））；
- 官方规则要求报告时必须注明编译器、标志和迭代数，并用 >=10 秒的运行；
  这里的结果用于 RTL 性能对比，不是 EEMBC 认证分数。

### 4.2 Dhrystone

ground：

```text
Microseconds for one run through Dhrystone: 15
Dhrystones per Second:                      65696
DMIPS:                                      37
DMIPS/MHz:                                  0.747
Dhrystone validation: PASS
```

best：

```text
Microseconds for one run through Dhrystone: 8
Dhrystones per Second:                      124345
DMIPS:                                      70
DMIPS/MHz:                                  1.415
Dhrystone validation: PASS
```

- `Dhrystone validation: PASS` 检查官方要求的结果值（`Bool_Glob==1`、
  `Ch_1/Ch_2=='A'/'B'`、`Arr_1_Glob[8]==7`）；
- DMIPS 归一化到 VAX 11/780 的 1757 Dhrystones/s；
- 编译器优化会显著影响 Dhrystone，比较时请注明 `-O2`、`__GNUC__`、运行次数。

### 4.3 是否符合官方运行规则

**公式和校验符合，运行长度不符合。** 这点需要明确：

| 要求 | 本测试 | 是否满足 |
|---|---|---|
| Dhrystone 2.1 源码 + 官方结果校验 | `Dhrystone validation: PASS` | 是 |
| Dhrystones/s、DMIPS、DMIPS/MHz 公式 | 由 50 MHz 周期数计算 | 是 |
| 报告编译器/优化标志 | ground `-O2`、best `-O2 -flto`，GCC 14.2.0 | 是 |
| 运行时间 ≥ 2 秒（惯例） | ground 500 轮 ≈ **0.0076 秒**（380,537），best ≈ **0.004 秒**（201,053） | **否** |
| CoreMark ≥ 10 秒（EEMBC 规则） | 1 次迭代 ≈ **0.007 秒**（351,767 cycles） | **否** |

Dhrystone 2.1 的通行报告惯例是让测量至少持续约 2 秒，以摊薄计时器和循环
启动开销；官方 CoreMark 报告规则要求 ≥10 秒。当前默认运行是为了让 Icarus
仿真在可接受时间内完成，分数按周期精确归一化，**不是官方认证分数**。

如果要接近官方运行长度：

```bash
# Dhrystone ≈ 2 秒（约 2.5 亿周期，Icarus 约 1 小时）
DHRY_RUNS=165000 MODE=best bash scripts/build_bench.sh dhrystone

# CoreMark ≈ 10 秒（约 3.5 亿周期，Icarus 约 2.6 小时）
ITERATIONS=1000 bash scripts/build_bench.sh coremark
```

Icarus 在这套 SoC 上约 37k 周期/秒，因此官方长度主要受仿真速度限制；真实硬件或
Verilator/FPGA 上可直接跑官方时长。本 port 把 `Too_Small_Time` 从
`2*HZ`（2 秒）改成 `2`（周期），否则默认运行会自动放大到约 500,000 轮。


## 5. 影响分数的因素

| 因素 | 说明 |
|---|---|
| 时钟频率 | 分数按 50 MHz 归一化；提高频率需同时更新 `bench/*/port` 中的 HZ/CLOCKS_PER_SEC 和 `sim/tb_bench.v` |
| 指令集 | 只有 RV32IM；无压缩指令，代码体积更大 |
| 流水线 | 5 级，EX/MEM->ID 前递（ALU 相关 0 停顿），load-use 2 周期，除法冻结约 33 周期 |
| 分支预测 | 32 项 BTB + 2-bit 计数器，预测正确无冲刷 |
| 存储器 | ITCM/DTCM 推断阵列，在仿真中无访问延迟（1 周期） |
| 编译器 | GCC 14.2.0；ground `-O2`，best 见 §2.1 |

## 6. 重新测量

```bash
# best（默认）
bash scripts/build_bench.sh coremark
bash scripts/build_bench.sh dhrystone

# ground rule
MODE=ground bash scripts/build_bench.sh dhrystone

# 更稳定的 Dhrystone（更长运行，仿真时间按比例增加）
DHRY_RUNS=2000 bash scripts/build_bench.sh dhrystone

# 只看数字
grep -E 'CoreMark 1.0|Dhrystones per Second|DMIPS/MHz' logs/bench/*.log
```

周期数由 core 的 `cycle` CSR 给出，不依赖仿真器的 wall-clock，因此换机器重跑结果
应完全一致；如果 RTL 改动，`BENCH_CYCLES` 会变化，分数随之更新。

运行时间与指令数成正比：CoreMark best 默认 1 次迭代约 95 秒，Dhrystone 500 次约 41 秒（ground 约 48 秒）
（Icarus，单线程）。
