# CLAUDE.md — Sprouts500 项目说明（RTL 不变量与流程）

## 项目简介

Sprouts500 是一个最简的五级顺序 **RV32IM** MCU IP：

- `rtl/core.v`：一个自包含的 core 文件（流水线、ITCM/DTCM、CSR/陷阱、M 扩展）。
- `rtl/bus.v`：一个简易的单 outstanding 总线。
- `rtl/timer.v`、`rtl/gpio.v`、`rtl/uart.v`、`rtl/spi.v`：外设。
- `rtl/soc_top.v`：SoC 集成。
- `sim/`：ISA 套件和 SoC demo 的 Icarus testbench。
- `sw/isa/`：riscv-arch-test 目标适配层；`sw/demo/`：裸机 demo。
- `tests/isa/`：官方 RV32I/M 测试源码 + 参考签名。
- `docs/`：架构、验证、寄存器、工具链、路线图。

本项目**仅支持 Ubuntu/Linux**。`build/`、`logs/` 已被 gitignore；OpenSTA/CUDD 系统安装在
`/opt/opensta`，不在仓库内。

## 先读这些

1. `README.md` — 状态、存储器映射、快速开始。
2. `docs/architecture.md` — 流水线、总线、陷阱，以及不变量。
3. `docs/verification.md` — 如何运行/调试各道门禁。
4. `docs/registers.md` — 外设/CSR 寄存器映射。
5. `docs/toolchain.md` — Ubuntu 工具链与 OpenSTA 构建。
6. `docs/c_programming.md` — 自己写裸机 C 程序测试（`sw/lib` + `tb_app.v`）。
7. `docs/verification_cocotb.md`、`docs/verification_cocotb_core.md` — cocotb 套件。
8. `docs/verification_riscv_dv.md` — riscv-dv + Sail 指令级随机 lockstep。

## 命令（Ubuntu）

```bash
bash scripts/setup_ubuntu.sh     # 主机工具
python3 scripts/fetch_isa.py     # 可选：tests/isa 已 vendored
python3 scripts/run_isa.py       # 必须是 46/46
bash scripts/build_demo.sh       # 必须打印 TEST_PASS
bash scripts/build_app.sh sw/example  # 自写 C 程序模板（期望 TEST_PASS）
bash scripts/build_bench.sh       # CoreMark + Dhrystone 性能分数
bash scripts/setup_cocotb.sh     # 一次性：.venv + cocotb/pytest（需 python3.14-dev）
bash scripts/run_cocotb.sh       # cocotb/soc + cocotb/core 回归
bash scripts/setup_riscv_dv.sh   # 一次性：.venv-dv + Sail
bash scripts/run_riscv_dv.sh     # riscv-dv + Sail lockstep 回归
bash scripts/fetch_lib.sh        # 可选：lib/ 已 vendored
bash scripts/synth.sh            # Yosys -> build/syn/core_netlist.v
sudo bash scripts/setup_opensta.sh # 一次性安装 CUDD + OpenSTA 到 /opt/opensta
bash scripts/sta.sh              # OpenSTA 报告
```

`make help` 列出目标。`make isa T=jalr-01` 运行单个测试。

## 验证门禁（不可跳过）

在声称某项修改可用之前：

1. `python3 scripts/run_isa.py` -> **46/46 通过**。
2. `bash scripts/build_demo.sh` -> **TEST_PASS**，且输出 40 字节 UART 字符串。
3. `bash scripts/synth.sh` -> Yosys 完成，无推断 latch/多驱动，
   已写出 `build/syn/core_netlist.v`。
4. 如果改动涉及时序/SDC：运行 `bash scripts/sta.sh`，并如实报告 WNS/TNS。
5. 如果改动 `sw/lib/`、`sim/tb_app.v` 或 `scripts/build_app.sh`：运行
   `bash scripts/build_app.sh sw/example`，必须 `TEST_PASS`。
6. 任何 RTL 改动：运行 `bash scripts/run_cocotb.sh`，必须
   `cocotb regression PASSED`（当前 soc 72 + core 43 项）。
7. 任何 RTL 改动：运行 `bash scripts/run_riscv_dv.sh`，必须全部种子 lockstep PASS。

如果测试失败，读 `logs/isa/<name>.log`；在 testbench 上使用 `+TRACE`、
`+TRACE_PIPE` 和 `+VCD`。ISA runner 会打印前 16 个签名不匹配项。

## 不可破坏的 RTL 不变量

这些地方很容易改坏，并造成只在测试中出现的隐蔽失败：

1. `IF_ID_IR_hold` / `IF_ID_PC_hold` / `IF_ID_hold` 在停顿期间保存待进入 ID 的 IF
   指令。删除它们会在一段停顿后丢掉每隔一条的指令。
2. `id_flush_q` 在重定向后让 ID 多保持一个周期无效。删除它会把过期的顺序执行指令
   当作分支目标执行。
3. 在 `id_rs1_data`/`id_rs2_data` 中，x0 的判断在 WB 旁路**之前**。
4. 除法结果选择是 `EX_funct3[1]`（0=div/divu，1=rem/remu），不是 bit 0。
5. 定时器 CTRL 的 bit 2 是 W1C；要在保持 EN/IE/PERIODIC 的同时清 pending，写 `0x0f`。
6. `bus_conflict` 在连续总线访问之间插入一个气泡。删除它会把两个事务合并，并丢掉
   第二次访问。
7. TCM 写由 `!pipe_freeze` 门控；否则除法冻结期间会重复写入。
8. `csr_we` 由 `!trap_taken` 和 `!pipe_freeze` 门控；产生陷阱的指令绝不能写 CSR，
   冻结在 EX 的 CSR 指令只能提交一拍（否则慢速总线/除法期间会重复写 CSR）。
9. ISA testbench 的 ITCM 是 4 MiB（`jal-01` 需要约 1.7 MiB 对齐填充）；SoC ITCM 是
   32 KiB。不要缩小 testbench 的 ITCM。
10. `tests/isa/env/arch_test.h` 包含针对 `TEST_JALR_OP` 中 `la x0` 的本地补丁。
    重新抓取测试会覆盖它；请重新应用补丁。

## 风格与结构

- 使用 Verilog-2001，core 保持单个文件，不使用 SystemVerilog 语法。
- 除非有实测理由要加前递，否则保持基于停顿的流水线。
- 与周围风格一致：紧凑的连续赋值，组合译码用 `always @(*)`，状态用
  `always @(posedge clk or negedge rstn)`。
- 注释应解释非显然控制存在的原因（例如 `bus_conflict`、`IF_ID_hold`），而不是复述
  显而易见的赋值。
- 新增外设时：增加一个文件、一组总线端口/strobe、一个 SoC 实例、
  `docs/registers.md` 中的寄存器映射章节，以及 demo 中的测试。
- 验证套件分层：`tests/isa`（指令语义）、`sim/tb_*.v` + `sw/`（集成/用户程序）、
  `cocotb/`（微架构/外设随机与定向）、`verif/riscv_dv`（指令级随机）。
  只改 `cocotb/` 或 `verif/` 下共享文件（`common/`、`_helpers.py`、生成器）时，
  必须整体重跑对应套件。
- `sw/lib/` 是自写 C 程序的共用运行时（crt0/trap/UART/string/链接脚本）。
  约定：镜像在 ITCM，`.bss`/栈在 DTCM，`0x7FF8` 结果码、`0x7FFC` halt magic。
  改这些约定时同步更新 `sim/tb_app.v`、`scripts/build_app.sh` 和
  `docs/c_programming.md`。
- 保持 `rtl/core.v` 是唯一的 core 文件；不要把 CPU 拆到多个文件。

## 当前状态 / 待办

- 46/46 RV32I/M ISA 测试在 Ubuntu 26.04 通过（GCC 14.2，Icarus 12）。
- SoC demo 在 Ubuntu 26.04 通过（UART hello、GPIO、定时器中断）。
- cocotb 套件通过：`cocotb/soc` 72 项（总线 + 外设 + core 程序）、`cocotb/core`
  43 项（流水线相关/CSR/陷阱/中断/总线/停顿）。
- riscv-dv + Sail 指令级随机 lockstep 通过（默认 3 个种子 + 定向陷阱镜像，4/4）。
- 性能基线：CoreMark **79.86**（1.60 CoreMark/MHz）；Dhrystone
  **41,945 Dhrystones/s**（23.9 DMIPS，0.477 DMIPS/MHz）。
- Yosys + Nangate45 综合在 Yosys 0.52 上运行通过（ABC fast + 驱动强度提升，
  2-word TCM 时序配置）。OpenSTA 3.1.0 50 MHz pre-CTS 为 **MET**：setup +15.94 ns，
  hold +0.058 ns，TNS/WNS 0.00（理想时钟；CTS 后需复查 hold）。
- TCM 是推断阵列；完整签核需要 SRAM 宏 + Liberty RAM 模型。
- Privilege 套件 / PMP / delegation 尚未实现。
- 后续优先级见 `docs/roadmap.md`。

## 环境说明

- 构建和仿真使用原生 Linux 文件系统上的代码树（不要用网络/9p 挂载目录）。
- 使用 `python3`，不要用 `python`。
- `RISCV_PREFIX` 默认 `riscv64-unknown-elf-`（Ubuntu 软件包）。
- OpenSTA 需要 CUDD；`scripts/setup_opensta.sh` 会把两者安装到 `/opt/opensta`。
- `lib/NangateOpenCellLibrary_typical.lib` 和 `tests/isa/` 已 vendored，保证离线可复现。
