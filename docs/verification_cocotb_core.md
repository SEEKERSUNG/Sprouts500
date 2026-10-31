# 核心 cocotb 随机/定向验证

本文件描述 `cocotb/core/` 下的 **core 级** cocotb 套件。它只测 `rtl/core.v`：
流水线、ITCM/DTCM、CSR/陷阱、中断入口、总线 master 和 M 扩展。SoC 集成、UART、
SPI、GPIO、定时器寄存器等由独立的 `cocotb/soc/` 套件覆盖，两者互不引用。

所有检查基于：

- 一个独立的 RV32IM 参考解释器（`_helpers.RV32Interp`），逐条比较退休 trace、
  寄存器堆、ITCM/DTCM 镜像；
- Python 断言直接采样 RTL 内部信号（`raw_stall`、`IF_ID_hold`、`id_flush_q`、
  `div_busy`、`pipe_freeze`、`bus_wait`、`bus_conflict`、`mem_*_we`、`csr_we` 等）；
- 一个可配置等待/初值的假总线从设备（`_helpers.BusSlave`）。

## 1. 运行方式

前置：cocotb 的 Icarus VPI 需要 Python 开发头文件，先运行一次
`sudo apt-get install python3.14-dev`。然后在仓库根目录：

```bash
bash scripts/run_cocotb.sh cocotb/core    # 推荐：统一日志/退出码
.venv/bin/python -m pytest cocotb/core -o python_files='*.py'
```

期望（默认 3 个随机种子）：

```text
43 passed
```

只跑一个文件或一个用例：

```bash
.venv/bin/python -m pytest cocotb/core/test_tcm_stall.py -o python_files='*.py'
.venv/bin/python -m pytest cocotb/core/test_tcm_stall.py -o python_files='*.py' -k branch
COCOTB_SEEDS=5 .venv/bin/python -m pytest cocotb/core -o python_files='*.py'
```

- `COCOTB_SEEDS`（默认 3）控制随机测试文件的 pytest 参数个数。随机文件是
  `test_exec_rand.py`、`test_branch_rand.py`、`test_m_ext.py`、`test_mem_rand.py`、
  `test_hazard.py`；每个种子启动一次独立仿真。定向文件（`test_csr_trap.py`、
  `test_interrupt.py`、`test_bus.py`、`test_tcm_stall.py`）不受它影响。
- `test_hazard.py` / `test_mem_rand.py` 等还会读取仿真内的 `TB_SEED`；pytest
  参数与仿真种子一致，失败消息里会打印种子。
- 构建产物：`build/cocotb_core/sim_build/`；cocotb 结果 XML：
  `build/cocotb_core/results/`。Icarus 增量编译，RTL/TB 不变时只有第一次慢。
- 验证环境（Ubuntu 26.04，Icarus 12，cocotb 2.1.0，Python 3.14）默认 3 个种子约
  **16 秒**跑完 43 项。

依赖：`.venv` 内安装 cocotb 2.1.0 与 pytest；Icarus 12 需在 `PATH` 中。core 套件的
辅助代码全部在 `cocotb/core/_helpers.py` 内，不 import `cocotb/common/`。

### 1.1 cocotb 2.1 wheel 的 pytest 入口点问题

当前 venv 的 cocotb wheel 没有注册 `pytest11` entry point。若在 pytest 会话里
启动 cocotb 仿真，cocotb 会从环境变量继承 pytest 的 RegressionManager 参数，但
仿真进程内没有加载收集插件，结果是 **collected 0 items、0 tests、假通过**。

`cocotb/core/_runner.py` 在启动仿真前移除 `COCOTB_PYTEST_*` 与 `PYGPI_USERS`，
强制仿真使用 cocotb 经典 RegressionManager；pytest 只负责收集 `test_*.py` 的包装
函数。这样仿真失败会以非零退出码 + results XML 返回，包装函数再把它变成 pytest
失败或严格 xfail。**不要删除这段环境清理**，否则套件会静默变成空跑。

## 2. Testbench 与辅助层

`cocotb/core/tb_core_cocotb.v` 是一个薄封装：

- 例化 `core`，参数 `ITCM_WORDS=4096`、`DTCM_WORDS=1024`、`RESET_PC=0`；
- 端口直连 `bus_*`、`timer_irq`、`ext_irq`、`soft_irq`；
- 无时钟/复位逻辑，全部由 cocotb 驱动。halt mailbox 位于 ITCM 最后一个字
  `0x0000_3FFC`，程序写入 `0x5A5A_5A5A` 后自旋。

`_helpers.py` 提供：

| 组件 | 用途 |
|---|---|
| `Asm` | 迷你汇编器：R/I/S/B/U/J、M、CSR、ecall/ebreak/mret/wfi，`li`/`la`/标签/`org`/`halt` |
| `RV32Interp` | 参考 RV32IM 解释器，记录退休 PC/IR、寄存器、ITCM/DTCM/总线窗口字节 |
| `CoreHarness` | 时钟/复位/IRQ 驱动、层次化读寄存器与 TCM、退休 trace、信号计数 |
| `BusSlave` | 假总线从设备：固定或按地址的 wait、初值、事务记录（地址/we/wstrb/wdata/周期） |
| `check_trace` | 逐项比较退休 trace，失败时打印第一处差异附近的指令 |

时序约定：`step()` 等待 `RisingEdge(clk)` 后再等 `1 ps` 采样，处于读写阶段；此时
非阻塞赋值已稳定，可以安全读信号或驱动输入。`trap_taken` 是组合信号，陷阱状态在
**下一个** posedge 才提交，因此 `run_with_trace(trap_hook=...)` 的 hook 在观察到
`trap_taken` 的下一拍才触发；中断测试靠这一点在正确时刻撤销电平。

## 3. 测试与架构不变量的对应

`docs/architecture.md` §9 的编号如下（本套件覆盖 1–9；10 属于 ISA testbench 的
4 MiB ITCM，与本套件无关）。

| 文件 | 用例数（默认种子） | 主要目标 |
|---|---|---|
| `test_exec_rand.py` | 3 | 随机 ALU/移位/比较/LUI/AUIPC 流；WB→ID 旁路；退休 trace |
| `test_branch_rand.py` | 3 | 随机 BRANCH/JAL/JALR、前向/后向循环、`org` 重定位覆盖镜像；冲刷正确性 |
| `test_m_ext.py` | 3 | mul/mulh/mulhsu/mulhu/div/divu/rem/remu；除零/溢出/负数边界 |
| `test_mem_rand.py` | 3 | ITCM/DTCM 全宽度 load/store 随机流；字节级内存 scoreboard |
| `test_hazard.py` | 3 | 各流水级距离的 RAW、load-use、x0 先于旁路、div 冻结 |
| `test_csr_trap.py` | 9 | CSR 读旧/写新、只读 CSR、ecall/ebreak/illegal、访存/分支/jalr 非对齐 |
| `test_interrupt.py` | 7 | 三源优先级、屏蔽、全局使能、vectored、div 期间不取中断 |
| `test_bus.py` | 5 | 总线事务、等待冻结、`bus_conflict`、慢读、CSR 单次提交 |
| `test_tcm_stall.py` | 7 | load-use 停顿深度、`IF_ID_hold`、`id_flush_q`、jalr link、div 冻结、TCM 写门控 |

逐条对应：

| §9 不变量 | 覆盖用例 |
|---|---|
| 1 `IF_ID_IR_hold`/`IF_ID_PC_hold`/`IF_ID_hold` | `test_tcm_stall[load_use,hold]`、`test_hazard`、`test_mem_rand`、所有 trace 比较 |
| 2 重定向后 `id_flush_q` | `test_tcm_stall[branch]`、`test_branch_rand` |
| 3 x0 判断先于 WB 旁路 | `test_hazard`（定向 `addi x0` 后立即读 x0）、`test_exec_rand` |
| 4 除法结果选择 `EX_funct3[1]` | `test_m_ext`、`test_tcm_stall[div]`、`test_interrupt[div_before_irq]` |
| 5 定时器 W1C bit 2 | 不在本套件：属 `cocotb/soc/` 与 demo |
| 6 `bus_conflict` 分隔连续事务 | `test_bus[back_to_back,rand_stores,rand_loads]` |
| 7 TCM 写由 `!pipe_freeze` 门控 | `test_tcm_stall[div_freeze_store,itcm_store]`、`test_hazard` DTCM 镜像 |
| 8 `csr_we` 与 `trap_taken` 互斥 | `test_csr_trap[csr_ro_trap,illegal]`；`csr_we` 的 `!pipe_freeze` 门控由 `test_bus[csr_double_write]` 覆盖（见 §4） |
| 9 `misa` 只声明 I/M | 本 core 套件只读回 `misa` 值而不检查位域；位域检查属 `cocotb/soc/` |

另外，§6.2 总线行为（单 outstanding、`bus_wait`、读数据捕获、写选通）由
`test_bus.py` 覆盖；§9 第 5 条（定时器 CTRL 的 W1C）不在 core 套件范围内。

### 3.1 几个用例的具体检查

- `test_hazard`：每个种子 120 组生产者/消费者，随机 0–3 个 nop 距离，统计
  `raw_stall`、`IF_ID_hold`、`div_busy`、`pipe_freeze`、`wb_repeat`，并要求全部
  trace、寄存器、DTCM 与参考模型一致。
- `test_branch_rand`：同一仿真内先跑链接镜像，再把以不同 origin 汇编的覆盖镜像
  拷入入口并**再次复位**；复位状态泄漏或冲刷遗漏都会在 trace 上立刻显形。
- `test_bus[slow_read]`：从设备对目标地址保持 6 拍 `bus_ready=0`，核对
  `bus_wait` 周期数等于从设备记录的事务等待周期，且只产生一次读事务。
- `test_tcm_stall[div_freeze_store]`：store 在 MEM、后面的 div 在 EX；div 冻结期间
  store 一直停在 MEM。要求 `mem_dtcm_we` 只在解冻后的一拍为高（整程序含 halt
  mailbox 的 DTCM 写也只计 1 次），并确认 store 值没有丢失。
- `test_tcm_stall[jalr]`：DTCM 预置奇数目标地址，`jalr` 必须清 bit 0；目标第一条
  指令立即消费 link `x1`，要求 `x1 == jalr_pc + 4`（不是解析时的 PC）。
- `test_interrupt[div_before_irq]`：在观察到 `div_busy` 后抬高 timer 电平，要求
  陷阱在 div 完成后才被取，`mepc` 指向 div，`mret` 后 div 重新执行且结果正确。

## 4. 已修复的 RTL 问题

### 4.1 `csr_we` 未受 `!pipe_freeze` 门控（rtl/core.v:1047）

原实现：

```verilog
wire csr_we = EX_valid && EX_csr_write && !trap_taken && !EX_illegal;
```

CSR 提交块用 `csr_we` 写 CSR。当 MEM 中的慢速总线访问让 `bus_wait`/`pipe_freeze`
拉高时，EX 中的 CSR 指令被冻结，但 `csr_we` 仍为高，于是同一条 CSR 指令在每个
冻结周期都重复提交一次。当前 CSR 值恰好幂等，所以数值上看不出来；对 W1C 或有
其他副作用的 CSR 会出错。

修复（rtl/core.v:1047-1050）：

```verilog
// A frozen EX (slow bus, iterative divide) holds the CSR instruction for
// several cycles; without !pipe_freeze it would commit once per cycle.
wire csr_we = EX_valid && EX_csr_write && !trap_taken && !EX_illegal &&
              !pipe_freeze;
```

`test_bus_csr_double_write` 现在验证：csrrs 确实在 `bus_wait` 期间停留在 EX，
但 `csr_we` 只高 1 拍，且 `mscratch` 为预期值。该用例已从 strict xfail 改为
普通通过用例；`cocotb/core/_runner.py` 的 `SIM_XFAIL` 表保持为空，供以后使用。

套件未发现其他与 `docs/architecture.md` §9 冲突的 RTL 行为。

## 6. 限制与未验证项

- **仿真器**：只用 Icarus Verilog 12（Verilog-2001 子集）。没有 SVA/断言，
  检查全部是 cocotb Python 采样 + 参考模型；因此不会捕获只在形式属性下暴露的
  活锁/死锁，也没有功能覆盖率指标。
- **参考模型范围**：`RV32Interp` 不实现 SYSTEM 指令（CSR/ecall/mret/wfi）。CSR
  与陷阱用例使用手工期望值；中断用例通过程序把 `mcause`/`mepc` 写入 DTCM 再由
  Python 检查。浮点、PMP、特权模式、delegation 均不在范围内（RTL 也未实现）。
- **随机性**：随机流集中在 ALU/分支/访存/M 扩展/RAW 距离；CSR、陷阱、总线等待
  是定向场景，没有随机 CSR 指令序列或随机异常注入。总线从设备没有错误响应、
  超时、乱序或多 outstanding 模型。
- **中断时序**：IRQ 电平由 testbench 在固定条件（如首次 `div_busy`、陷阱提交后）
  驱动，没有随机到达相位；定时器外设本身由 SoC 套件覆盖。
- **配置**：core 套件固定 `ITCM_WORDS=4096`、`DTCM_WORDS=1024`、`RESET_PC=0`，
  不是 SoC 的 32 KiB ITCM 配置；TCM 通过层次化赋值预载，不走复位/总线。
- **实现层面**：没有门级/综合后仿真，没有 SDF；时序签核仍由 `scripts/sta.sh`
  负责。cocotb 套件不替代 ISA 46/46、demo 和综合门禁。
- **运行环境**：每项 pytest 用例启动一次独立 vvp；默认 3 种子约 16 秒，种子数
  增大时按随机文件数线性增长。结果 XML 只保留最近一次运行的记录。
