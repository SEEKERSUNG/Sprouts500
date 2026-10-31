# cocotb SoC 级验证

本文件描述 `cocotb/soc/` 下的 SoC 级 cocotb 套件：它把 `rtl/core.v`、`rtl/bus.v`、
四个外设和 `tb_soc_bus` 一起跑在 Icarus 下，用定向 + 随机测试覆盖总线、外设寄存器和
core→总线的握手。core 流水线内部的定向测试在 `cocotb/core/` 套件中
（见 `docs/verification_cocotb_core.md`），两者互不依赖。

## 1. 环境与运行

```bash
bash scripts/setup_cocotb.sh        # 一次性：在仓库内创建 .venv 并安装 cocotb+pytest
bash scripts/run_cocotb.sh          # 默认跑 cocotb/soc（存在 cocotb/core 时一并跑）
bash scripts/run_cocotb.sh cocotb/soc
SEED=12345 bash scripts/run_cocotb.sh cocotb/soc      # 固定随机种子
bash scripts/run_cocotb.sh cocotb/soc -k uart -x       # 其余参数传给 pytest
```

- 前置：cocotb 的 Icarus VPI 需要 Python 开发头文件，先运行一次
  `sudo apt-get install python3.14-dev`（版本与 `python3 --version` 一致）。
- `setup_cocotb.sh` 不需要 sudo，也不在系统 Python 中安装任何东西。它只用
  `python3 -m venv .venv`（发行版缺 ensurepip 时退化为 `--without-pip` 再引导
  get-pip.py）；`.venv` 已存在且能 `import cocotb, pytest, cocotb_tools` 时直接跳过。
- `run_cocotb.sh` 设置 `SIM=icarus`、`PYTHONPATH=<仓库根>`，以
  `pytest -o python_files='*.py' -p cocotb_tools._pytest.plugin` 运行，汇总写入
  `logs/cocotb/run-<时间戳>-<pid>.log`；JUnit XML 按目标写入 `logs/cocotb/junit*.xml`
  （第一个目标 `junit.xml`，其余 `junit-<目录名>.xml`）；
  失败时退出码非 0。
- 直接调用 pytest 也可以（在仓库根）：

  ```bash
  PYTHONPATH=$PWD SIM=icarus .venv/bin/python -m pytest cocotb/soc \
      -o python_files='*.py' -p cocotb_tools._pytest.plugin
  ```

  `cocotb/conftest.py` 会加载插件并把 `cocotb/` 加到 `sys.path`，因此 `common.*`
  可导入。

### 随机种子

- `SEED=<n>` 环境变量会同时传给 cocotb（`COCOTB_RANDOM_SEED`）和测试内的
  `common.misc.make_rng()`；取值为十进制或 `0x` 十六进制（如 `SEED=0xDEADBEEF`），
  `run_cocotb.sh` 把十六进制换算成十进制再传给 cocotb，并回显 `SEED=...`，每个 cocotb 测试的
  JUnit `<testcase>` 里也记录了 `random_seed`（`logs/cocotb/junit*.xml`），失败时
  从 XML 或 `Captured stderr` 的 `COCOTB_RANDOM_SEED=<n>` 读取。随机数只用
  `random.Random`，与 Python 的 `hash()` 无关，同类进程之间可复现。
- 不设 `SEED` 时每轮使用新种子。要查看随机测试的逐步操作，用
  `bash scripts/run_cocotb.sh cocotb/soc/test_timer.py -q -s`（`-s` 关闭捕获）；
  随机测试会打印 `random op: ...`，便于按种子复现。
- 已实测的种子：`0 1 2 3 4 5 17 99 108 777 999 2026 12345 123456789 987654321
  4242424242 31415926535897932384626433832795028841`，以及批量扫描
  `4..40 55 108 200 777 888 999 1234 4321 271828 314159 161803 999983
  123456789 987654321 2147483647`，全部 72 项通过（无一失败）；
  `SEED=12345` 连续两次运行，125 条随机操作（周期、地址、数据）逐条一致。

## 2. 结构

| 文件 | 作用 |
|---|---|
| `cocotb/soc/tb_soc_bus.v` | SoC testbench：`soc_top` 的平铺复制 + 外部总线主控 + 慢速/假从设备 + ITCM 镜像加载 |
| `cocotb/soc/fake_slave.v` | `0x3000_xxxx` 上的确定性从设备（64 word，`addr[7:2]` 索引，字节选通，`hold` 延后 `ready`） |
| `cocotb/common/clock_reset.py` | 50 MHz 时钟（20 ns）、16 周期复位、`CycleCounter` |
| `cocotb/common/bus_master.py` | `BusMaster.write/read`（遵守 `req && !req_q` 边沿语义）、`read_held`/`write_held`、`drive_ready` |
| `cocotb/common/uart_bfm.py` | UART TX 帧解码（校验位网格）、RX 位敲 |
| `cocotb/common/spi_bfm.py` | SCLK 采样、SPI 回环传输 |
| `cocotb/common/models.py` | `TimerModel`、`GpioModel`（逐边沿重放）和 `Scoreboard` |
| `cocotb/common/program.py` | 极简 RV32I/M 汇编器、`halt_sequence`、镜像生成 |
| `cocotb/common/runner.py` | Icarus 构建（`sim_build/tb_soc_bus`）与 `run_tests` |

### 每个测试模块一次仿真

cocotb 2.x 中一个 `@pytest.mark.cocotb_runner` 外层测试只启动一个仿真进程，模块内
所有 `@cocotb.test()` 按顺序共享它。因此每个测试开头都调用 `reset_soc()`；外设测试
用 `reset_soc(dut, core=False)` 把 core 按住、由 `ext_master` 驱动总线，program
测试用 `reset_soc(dut)` 让 core 从 ITCM 取指。

### `tb_soc_bus.v` 的偏差（重要）

`rtl/soc_top.v` 没有对外暴露总线端口，所以 testbench 按 `soc_top.v` 原样平铺复制了
core、`bus`、timer/gpio/uart/spi 和 GPIO pad 模型，再额外加入：

- `ext_master` 多路选择的外部总线主控，带独立 `ext_ready`；
- 假从设备 `fake_slave`（`addr[31:28] >= 3`），用于连续访问、慢 `ready`、
  结果回读；
- `core_stall`：对任意 core 访问拉低 `bus_ready_i`；
- `core_req/we/addr/wdata/wstrb`、`fake_wr*`、四路 IRQ 可观测端口；
- `+ITCM_HEX=<file>` 加载镜像；末尾 ITCM word（`0x7FFC`）作为 halt mailbox。

RTL 本身没有被修改。代价是：如果 `soc_top.v` 的连线变了，`tb_soc_bus.v` 必须同步
修改（文件头有说明）。`fake_hit` 使用多路选择后的 `bus_addr`，因此 core 和外部主控
看到的慢速条件一致。

### 周期编号约定

- 50 MHz，周期 20 ns；复位保持 16 个周期，`rstn` 与 `core_rstn` 在同一个边沿释放。
- 周期 1 = 复位释放后的第一个上升沿。`BusResult.commit_cycle` 表示处理该事务的那个
  上升沿（写在这一沿被外设采样，读数据也在这一沿捕获）；这个编号由仿真时间推导，
  不受协程调度影响。
- 模型约定：在周期 *k* 提交的读看到第 *k* 个边沿之前的状态（即 *k-1* 次 tick 后）；
  在周期 *k* 提交的写在边沿 *k* 生效。
- `rtl/timer.v` 中 `ctrl/cmp/pre` 与计数逻辑在同一个 `always` 块里用非阻塞赋值，
  所以计数逻辑在同一沿上看到的是**旧**寄存器值；`TimerModel` 按此实现。

### Program 镜像

`cocotb/soc/test_core.py` 在模块导入时用 `common/program.py` 生成
`sim_build/core_program.hex`（补零到 8192 word），通过 `+ITCM_HEX=` 加载。程序把结果
写到假从设备 word `0x80..0xB8`，最后写 halt mailbox 并自旋；测试用外部主控回读结果。
每个测试重新复位，testbench 在 `core_rstn` 为低时把 mailbox 清零。

## 3. 覆盖矩阵

| 模块 | 测试（要点） |
|---|---|
| `test_smoke.py` | 复位值、时钟、外部主控基本读写、别名 |
| `test_bus.py` | 每个窗口可读；外设/假从设备字节选通；未对齐地址透传；`read_held` 保持数据；`write_held` 只触发一次；`ext_ready` 停滞读和写；`hold` 只影响假从设备窗口；假从设备别名 |
| `test_timer.py` | 复位值；COUNT 只读；PRE 分频；单次触发（EN 自清）；周期模式；W1C（写 `0x0f` 保持 EN/IE/PERIODIC）；IE 门控 IRQ；`addr[3:0]` 别名；120 步随机写/读对阵 `TimerModel` |
| `test_gpio.py` | 复位值；DIR/OUT/IN 语义；外部驱动只在输入脚生效；字节选通；W1C；边沿中断与 IE 门控；`addr[7:0]` 别名（IN 写被忽略）；两级同步；80 步随机测试对阵 `GpioModel` |
| `test_uart.py` | 复位值（BAUD=433）；TX 关闭/忙时忽略 TXDATA；TX 帧格式与位网格；TX_DONE 置位与保持；自定义波特率；RX 位敲（含自定义波特率）；帧错误；溢出；TX/RX IRQ；字节选通、`addr[7:0]` 别名、STATUS 只读；8 字节随机 TX→RX 回环 |
| `test_spi.py` | 复位值；CS 语义；四种 CPOL/CPHA × 两种位序回环；SCLK 半周期与 8 个时钟；BUSY/DONE、RXDATA 读清 DONE；IRQ；关闭/忙时忽略 TXDATA；CLKDIV 字节选通与别名；STATUS 只读；20 次随机回环；CLKDIV=1 接收错位记录 |
| `test_core.py` | 汇编程序在真实 core 上跑：连续总线访问（`bus_conflict`）；不同外设连续 load；word/byte/half 存储后各种 load 扩展；TIMER 轮询 + W1C；GPIO 回读；假从设备 `hold` 与 `core_stall` 下 `bus_wait` 冻结、`req` 保持、不重复写 |

未覆盖（有意留给其他套件/后续）：

- core 流水线内部冒险、CSR、中断入口：`cocotb/core/` 和 `python3 scripts/run_isa.py`；
- 编译后的 C 程序与 UART 输出：`bash scripts/build_demo.sh`；
- 覆盖率闭合（代码/功能覆盖）与形式化属性；
- 功耗/时序：`scripts/synth.sh`、`scripts/sta.sh`。

## 4. 新增测试

1. 外设级：在 `cocotb/soc/test_<periph>.py` 中加 `@cocotb.test()`，开头调用
   `await _setup(dut)`；需要 scoreboard 时用 `common.models.Scoreboard`。
2. 总线级：用 `BusMaster.write/read`；注意 `BusMaster` 返回时处于 `ReadOnly`，
   驱动 TB 输入（`gpio_ext_*`、`ext_ready`、`hold`、`core_stall`）前要先
   `await RisingEdge(dut.clk)`。
3. core 级程序：在 `test_core.py` 的 `build_core_program()` 里加指令，结果写到
   `RES[...]`，在 `EXPECTED` 里写期望值；`x30/x31` 留给 `halt_sequence`。
4. 随机测试：使用 `make_rng("<name>")`，并在日志中打印关键输入，便于 `SEED=` 复现。
5. 改了 `rtl/soc_top.v` 的连线时，同步修改 `cocotb/soc/tb_soc_bus.v`。

## 5. 已知限制

- Icarus Verilog 12：不支持 SystemVerilog class/interface，不能用 cocotb 的 SV 总线
  agent；只能通过层级引用直接读信号（如 `u_core.ITCM`）。
- 每个模块一次仿真、每个测试自行复位；测试之间不共享状态。
- spi 回环依赖 testbench 里的 `spi_miso = spi_mosi`；UART 回环依赖 `uart_tx_d`
  额外一级触发器（见“发现的问题”）。
- 假从设备是 testbench 模型，不是真实存储器；真实 SoC 只到 `0x2000_3FFF`，更高地址
  读 0。
- 没有覆盖率收集，也没有形式化验证。

## 6. 发现的问题

下面各项都没有改 RTL；测试记录了实测行为（通过断言或注释）。

1. **`rtl/spi.v`：CLKDIV=1 时接收错位一位。**
   `miso_s1 <= miso_s0;` 之后 `rx_shift <= miso_s1;` 在同一沿取到的是上一拍的
   `miso_s1`；MISO 经过两级同步，CLKDIV<=1 时采样点在位边界之后。回环下
   `0x96` 收到 `0x4B`。CLKDIV>=2（含复位值 24）所有 CPOL/CPHA/位序都正确。
   由 `test_spi_loopback_clkdiv_one_bit_skew` 记录（该测试在 RTL 修复后会失败，
   提示更新文档）。
2. **`rtl/uart.v`：起始位短一拍。** 50 MHz、BAUD=433 时起始位 433 个时钟，数据位
   每 bit 434 个时钟；发送网格整体偏一格。`decode_uart_tx`/`UartFrame` 按实测网格
   校验，功能（LSB first、停止位、TX_DONE 在停止位结束）正确。
3. **UART 回环需要额外一级触发器。** testbench 直接把 `uart_tx` 接到 `uart_rx` 时，
   RX 的两级同步器晚两拍看到起始沿，高速率下丢掉前几位；`tb_soc_bus.v` 增加了
   `uart_tx_d <= uart_tx`，模拟一根线。真实板上的外部连线天然有延迟，这不是 RTL
   缺陷，但说明 RX 同步器对“起始沿与第一个数据位采样点之间”的余量敏感。
4. **帧错误时 RXDATA 保持 0。** `uart.v` 只在停止位为 1 时提交数据；帧错误只置
   RX_OVERRUN。`test_uart_rx_framing_error` 按实测行为断言。
5. **地址别名（文档化行为，非缺陷）。** `gpio.v`/`uart.v`/`spi.v` 用 `addr[7:0]`
   解码（每 256 字节别名），`timer.v` 用 `addr[3:0]`（每 16 字节别名）；IN 和
   COUNT 的写被忽略。测试按此断言。
