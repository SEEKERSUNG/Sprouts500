# AGENTS.md

Sprouts500 是一个最简的五级 RV32IMACF MCU IP，仅支持 Ubuntu/Linux。

先读：

- `README.md` — 状态、存储器映射、快速开始
- `CLAUDE.md` — 项目流程与不可破坏的不变量
- `docs/architecture.md` — 流水线/总线/陷阱设计
- `docs/verification.md` — 如何运行和调试各道门禁
- `docs/registers.md` — 外设与 CSR 寄存器映射
- `docs/toolchain.md` — Ubuntu 工具链与 OpenSTA
- `docs/c_programming.md` — 自写裸机 C 程序（`sw/lib` + `sim/tb_app.v`）
- `docs/verification_cocotb.md`、`docs/verification_cocotb_core.md` — cocotb 套件
- `docs/verification_riscv_dv.md` — riscv-dv + Sail 指令级随机 lockstep
- `docs/benchmarks.md` — CoreMark/Dhrystone ground rule 与 best 分数

当前状态：I/M/C 官方 ISA 73/73 + A 定向 3/3 + F 146/146（合计 **222/222**）；SoC demo 通过；
Yosys 0.52 Nangate45 综合通过；OpenSTA 3.1.0 **100 MHz pre-CTS MET**
（setup +3.251 ns，hold +0.083 ns，TNS/WNS 0.00）。TCM 是推断阵列，
CPU 时序运行时缩为 2 word；完整签核仍需要 SRAM 宏模型，并在 CTS 后复查 hold。

## 必需命令

```bash
bash scripts/setup_ubuntu.sh
python3 scripts/run_isa.py     # 222/222
bash scripts/build_demo.sh     # TEST_PASS
bash scripts/build_bench.sh    # CoreMark/Dhrystone best；MODE=ground 取 Dhrystone ground
bash scripts/run_cocotb.sh     # soc 72 + core 77 项通过
bash scripts/run_riscv_dv.sh   # riscv-dv + Sail lockstep 6/6 通过
bash scripts/synth.sh
sudo bash scripts/setup_opensta.sh # 一次性安装 CUDD + OpenSTA 到 /opt/opensta
bash scripts/sta.sh             # 期望 100 MHz 下 setup/hold MET
```

## 硬性规则

- 仅支持 Ubuntu/Linux。
- CPU 流水线保持在 `rtl/core.v`，RV32F 数据通路在 `rtl/fp_unit.v`；只使用 Verilog-2001。
- RTL 修改前后：222/222 ISA 测试、SoC demo、cocotb 套件和 riscv-dv lockstep
  6/6 必须通过。
- 不要删除 `IF_ID_*_hold`、`id_flush_q`、`bus_conflict`、x0-before-all-bypasses、
  `EX_funct3[1]` 除法结果选择，或前递/预测的排除条件（load/AMO 不前递、计数器饱和、
  `IF_pred_*_q` 配对）。
- RVC 第二个 IF 读端口、`if_rdata_hi_q`、64 项 BTB 索引 `PC[6:1]`、AMO 两拍写一次、
  LR/SC 重叠逻辑、MMIO 原子陷阱、`mepc` 只清 bit 0；FPU 的 start/busy/done
  握手、FP 寄存器 scoreboard、FS/fflags 规则。原因见 `CLAUDE.md`。
- `tests/isa/env/arch_test.h` 包含本地 `la x0` 兼容补丁；保留它。
- `tests/isa/` 已 vendored；`lib/` 中的 Nangate45 Liberty 仅本地使用（已 gitignore）；
  `build/`、`logs/` 被忽略。
- OpenSTA/CUDD 系统安装在 `/opt/opensta`，命令为 `/usr/local/bin/sta`。
