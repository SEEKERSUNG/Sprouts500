# CLAUDE.md — Sprouts500 项目说明（RTL 不变量与流程）

## 项目简介

Sprouts500 是一个最简的五级顺序 **RV32IMACF** MCU IP：

- `rtl/core.v`：流水线、ITCM/DTCM、CSR/陷阱、M/A/C 扩展。
- `rtl/fp_unit.v`：RV32F 浮点单元（分级流水线；与 core.v 一起构成 CPU）。
- `rtl/bus.v`：一个简易的单 outstanding 总线。
- `rtl/timer.v`、`rtl/gpio.v`、`rtl/uart.v`、`rtl/spi.v`：外设。
- `rtl/soc_top.v`：SoC 集成。
- `sim/`：ISA 套件、SoC demo、用户程序、benchmark 和 lockstep 的 Icarus testbench。
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
9. `docs/fpga_boot.md` — FPGA BOOT0/UART 下载、协议与集成。

## 命令（Ubuntu）

```bash
bash scripts/setup_ubuntu.sh     # 主机工具
python3 scripts/fetch_isa.py     # 可选：tests/isa 已 vendored
python3 scripts/run_isa.py       # 必须是 222/222（I 38 + M 8 + C 27 + A 3 + F 146）
bash scripts/build_demo.sh       # 必须打印 TEST_PASS
bash scripts/build_app.sh sw/example  # 自写 C 程序模板（期望 TEST_PASS）
bash scripts/build_bench.sh       # CoreMark + Dhrystone 性能分数
bash scripts/build_boot.sh        # 2 KiB FPGA UART Boot 初始化镜像
MARCH=rv32imafc_zicsr bash scripts/build_fpga_app.sh sw/example  # 0x810 应用 + CRC package
MARCH=rv32imafc_zicsr bash scripts/test_boot.sh  # 协议单测 + UART 端到端 BOOT_TEST_PASS
bash scripts/setup_cocotb.sh     # 一次性：.venv + cocotb/pytest（需 python3.14-dev）
bash scripts/run_cocotb.sh       # cocotb/soc + cocotb/core 回归
bash scripts/setup_riscv_dv.sh   # 一次性：.venv-dv + Sail
bash scripts/run_riscv_dv.sh     # riscv-dv + Sail lockstep 回归
bash scripts/fetch_lib.sh        # 可选：本地下载 Nangate45 Liberty（不提交）
bash scripts/synth.sh            # Yosys -> build/syn/core_netlist.v
sudo bash scripts/setup_opensta.sh # 一次性安装 CUDD + OpenSTA 到 /opt/opensta
bash scripts/sta.sh              # OpenSTA 报告
```

`make help` 列出目标。`make isa T=jalr-01` 运行单个测试。

## 验证门禁（不可跳过）

在声称某项修改可用之前：

1. `python3 scripts/run_isa.py` -> **222/222 通过**。
2. `bash scripts/build_demo.sh` -> **TEST_PASS**，且输出 40 字节 UART 字符串。
3. `bash scripts/synth.sh` -> Yosys 完成，无推断 latch/多驱动，
   已写出 `build/syn/core_netlist.v`。
4. 如果改动涉及时序/SDC：运行 `bash scripts/sta.sh`，并如实报告 WNS/TNS。
5. 如果改动 `sw/lib/`、`sim/tb_app.v` 或 `scripts/build_app.sh`：运行
   `bash scripts/build_app.sh sw/example`，必须 `TEST_PASS`。
6. 任何 RTL 改动：运行 `bash scripts/run_cocotb.sh`，必须
   `cocotb regression PASSED`（当前 soc 72 + core 79 项）。
7. 任何 RTL 改动：运行 `bash scripts/run_riscv_dv.sh`，必须 6/6 lockstep PASS
   （3 个随机种子 + 定向陷阱 + 定向 A + 定向 C）。
8. 如果改动 `sw/boot/`、`fpga/`、`sim/tb_boot.v`、UART 下载脚本或 ITCM Boot 参数：
   运行 `bash scripts/test_boot.sh`，必须 `BOOT_TEST_PASS`。

如果测试失败，读 `logs/isa/<name>.log`；在 testbench 上使用 `+TRACE`、
`+TRACE_PIPE` 和 `+VCD`。ISA runner 会打印前 16 个签名不匹配项。

## 不可破坏的 RTL 不变量

这些地方很容易改坏，并造成只在测试中出现的隐蔽失败：

1. `IF_ID_IR_hold` / `IF_ID_PC_hold` / `IF_ID_hold` 在停顿期间保存待进入 ID 的 IF
   指令。删除它们会在一段停顿后丢掉每隔一条的指令。
2. `id_flush_q` 在重定向后让 ID 多保持一个周期无效，并保持 IF 取指地址。删除它会把
   过期的顺序执行指令当作分支目标执行；它那一拍还要重新查 BTB（重定向边沿刚训练的项）。
3. 在 `id_rs1_data`/`id_rs2_data` 中，x0 的判断在 WB 旁路**之前**。
4. 除法结果选择是 `EX_funct3[1]`（0=div/divu，1=rem/remu），不是 bit 0。
5. 定时器 CTRL 的 bit 2 是 W1C；要在保持 EN/IE/PERIODIC 的同时清 pending，写 `0x0f`。
6. `bus_conflict` 在连续总线访问之间插入一个气泡。删除它会把两个事务合并，并丢掉
   第二次访问。
7. TCM 写由 `!pipe_freeze` 门控；否则除法冻结期间会重复写入。
8. `csr_we` 由 `!trap_taken`、`!pipe_freeze` 和 `!mem_amo_active` 门控；产生陷阱的
   指令绝不能写 CSR，冻结在 EX 的 CSR 指令只能提交一拍。
9. ISA testbench 的 ITCM 是 4 MiB（`jal-01` 需要约 1.7 MiB 对齐填充）；SoC ITCM 是
   32 KiB。不要缩小 testbench 的 ITCM。
10. `tests/isa/env/arch_test.h` 包含本地 `TEST_JALR_OP` 的 `la x0` 兼容补丁；重新抓取
    测试会覆盖它，请重新应用。F 套件通过 `RVTEST_FP_ENABLE` 设置 `mstatus.FS`。
11. RVC 前端（Zca+Zcf）：`if_rdata_q` 是包含 `IF_PC_q` 的 word，`if_rdata_hi_q` 是下一个 word；
    `IF_PC_q` 是呈现指令的 PC（2 字节粒度），`IF_PC` 是取指地址。压缩指令展开为规范
    32 位 IR；非法压缩编码展开为 `{16'b0, ci}`（cause 2，mtval 为零扩展半字）。
    不要删除第二个 IF 读端口，否则 lane-1 的 32 位指令会重新产生装配气泡。
12. BTB 是 64 项，索引 `PC[6:1]`、tag `PC[31:7]`（IALIGN=16）；训练用 `EX_PC`。
    链接值和陈旧项纠正用 `EX_PC + (EX_is_compressed ? 2 : 4)`。重定向取指地址是
    目标所在 word（`{redirect_pc[31:1],1'b0}`）。
13. AMO 在 MEM 占两拍且只写一次；相位转换与 `mem_amo_we` 由 `!pipe_freeze` 门控；
    `instret` 只在相位 1 计数；`mem_amo_active` 保持 IF/ID/EX，并暂停 CSR 提交、
    `mret` 和预测纠错/训练。
14. LR/SC：保留粒度 4 字节；任何 SC、同 word 的已提交 store/AMO、任何陷阱都清除
    保留；`ex_sc_ok` 的组合重叠逻辑（MEM 中的 LR / 同拍 store / 更老的 SC）不能删。
15. AMO/LR/SC 访问 tag `>=2` 在 EX 陷阱（LR 5、AMO/SC 7），绝不驱动总线。
16. `mepc` 只清 bit 0（陷阱写和 CSR 写），因为 IALIGN=16；`mtvec` BASE 仍要求
    4 字节对齐。

17. 所有 OP-FP/FMA 指令都是慢速指令：`fp_start` 只在指令第一次进入 EX 时脉冲一次，
    `fp_done`/结果保持到下一次 start。FPU 分四级（解包/算术/FMA 加或迭代/舍入）；
    不要把它们改回组合输出，否则 100 MHz 时序会失败。
18. FP 寄存器文件没有 EX/MEM 前递：EX/MEM 的 FP 写者通过 `id_fp_stall` 停住 ID，
    数据从 WB 旁路取得；`f0` 是可写寄存器，不能套用整数 x0 的规则。
19. `mstatus.FS` 复位为 Off；FS=Off 的浮点/fcsr 访问必须陷阱。只有 FP 指令执行或
    fcsr 写才把 FS 置 Dirty；陷阱和 `mret` 不改 FS。
20. `fcsr`/`frm` 的保留模式写入是非法指令；rm=111（DYN）从 `frm` 取有效模式。
21. `FLW`/`FSW` 分别使用 I/S 立即数；它们的非对齐陷阱是 cause 4/6。
22. NaN 统一为规范 qNaN `0x7fc00000`；NV/DZ/OF/UF/NX 和 FMIN/FMAX/比较的 NaN 规则
    必须与 RISC-V SoftFloat 一致（见 `rtl/fp_unit.v`）。FMA 是单次舍入，FDIV/FSQRT
    是迭代精确实现。
23. 4 项 RAS 的架构状态只在 EX 的真实 call/return 上 push/pop；IF 预测视图组合应用
    ID/EX 中更老操作，避免短函数读取旧栈顶。陷阱和 `mret` 清空 RAS，错误目标仍由 EX
    重定向纠正。
24. 普通 TCM load 在 EX->MEM 边沿捕获读数据，distance-0 load-use 只停 1 拍，MEM 可
    直接前递；同拍更老 store 写同 word 必须按字节合并，WB 保存本 load 原始 word。
    总线 load 和 AMO/LR/SC 不使用该前递，仍保持原停顿和 WB 数据路径。
25. FPGA `ITCM_BOOT_BYTES` 写保护必须同时门控 ITCM 数组写和同拍 store/load 合并；
    保护区内的 SC 必须返回失败，AMO 写必须被忽略。默认 0 保持普通自修改 ITCM 行为。
    `ITCM_INIT_FILE` 默认为空，现有 testbench 的显式 `$readmemh` 不变；非空时先把
    整个 ITCM 清 0，再装载 Boot 镜像，避免缺失应用头在仿真中为 X。FPGA Boot 布局/
    协议见 `docs/fpga_boot.md`。

## 风格与结构

- 使用 Verilog-2001，不使用 SystemVerilog 语法。CPU 流水线保持在 `rtl/core.v`，
  RV32F 数据通路在 `rtl/fp_unit.v`；两者一起构成 core，不要把流水线拆到更多文件。
- 软件默认 `-march=rv32imacf_zicsr`；`MARCH=` 可覆盖。
- 流水线已启用 EX/MEM->ID 前递、普通 TCM load 的 MEM 前递，以及 IF 级 BTB + 4 项
  RAS；新增优化必须有实测收益并保持全部门禁。
- 与周围风格一致：紧凑的连续赋值，组合译码用 `always @(*)`，状态用同步复位
  `always @(posedge clk)` + `if (!rstn)`（core 和 FPU 都是同步低有效复位）。
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

## 当前状态 / 待办

- 222/222 ISA 测试（I/M/C 官方 + A 定向 + F 官方）在 Ubuntu 26.04 通过（GCC 14.2，Icarus 12）。
- SoC demo 在 Ubuntu 26.04 通过（UART hello、GPIO、定时器中断）。
- FPGA UART Boot：2 KiB 驻留 Boot、GPIO[0] 低有效 BOOT0、分帧 CRC32 下载和
  `0x810` 应用；协议单测与端到端仿真通过。详见 `docs/fpga_boot.md`。
- cocotb 套件通过：`cocotb/soc` 72 项（总线 + 外设 + core 程序）、`cocotb/core`
  79 项（流水线相关/前递/分支预测与 RAS/CSR/陷阱/中断/总线/停顿/RVC/A，以及 F 的
  hazard/fflags/FS=Off 3 项）。F 语义由 146 项 ISA 测试覆盖。
- riscv-dv + Sail 指令级随机 lockstep 通过（3 个随机种子 + 定向陷阱 + 定向 A +
  定向 C，6/6）。
- 性能基线（100 MHz，best 为本机 GCC 13.2 实测）：CoreMark best **308.175**
  （3.08 CoreMark/MHz，324,490 周期，RV32IMACF）；Dhrystone best **262,387 Dhrystones/s**
  （149.3 DMIPS，1.493 DMIPS/MHz，190,558 周期）。Dhrystone ground 的 GCC 14.2 历史参考为
  **135,660 Dhrystones/s**（77.2 DMIPS）。`MODE=ground|best` 只改编译方式，不改 benchmark
  源码算法；`MARCH=rv32im_zicsr` 可复现旧基线。
- Yosys + Nangate45 综合在 Yosys 0.52 上运行通过（ABC fast + 驱动强度提升，
  2-word TCM 时序配置，97,225 单元）。OpenSTA 3.1.0 100 MHz pre-CTS 为 **MET**：
  setup +3.148 ns，hold +0.083 ns，TNS/WNS 0.00（理想时钟；CTS 后需复查 hold）。
- TCM 是推断阵列；完整签核需要 SRAM 宏 + Liberty RAM 模型。
- Privilege 套件 / PMP / delegation 尚未实现。
- 后续优先级见 `docs/roadmap.md`。

## 环境说明

- 构建和仿真使用原生 Linux 文件系统上的代码树（不要用网络/9p 挂载目录）。
- 使用 `python3`，不要用 `python`。
- `RISCV_PREFIX` 默认 `riscv64-unknown-elf-`（Ubuntu 软件包）。
- OpenSTA 需要 CUDD；`scripts/setup_opensta.sh` 会把两者安装到 `/opt/opensta`。
- `tests/isa/` 已 vendored；`lib/NangateOpenCellLibrary_typical.lib` 仅本地使用，
  已在 `.gitignore` 中，不能重新提交。
