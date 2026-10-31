# AGENTS.md

Sprouts500 是一个最简的五级 RV32IM MCU IP，仅支持 Ubuntu/Linux。

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

当前状态：RV32I/M `riscv-arch-test` 2.x **46/46**；SoC demo 通过；
Yosys 0.52 Nangate45 综合通过；OpenSTA 3.1.0 **50 MHz pre-CTS MET**
（setup +15.94 ns，hold +0.058 ns，TNS/WNS 0.00）。TCM 是推断阵列，
CPU 时序运行时缩为 2 word；完整签核仍需要 SRAM 宏模型，并在 CTS 后复查 hold。

## 必需命令

```bash
bash scripts/setup_ubuntu.sh
python3 scripts/run_isa.py     # 46/46
bash scripts/build_demo.sh     # TEST_PASS
bash scripts/build_bench.sh    # CoreMark + Dhrystone 分数
bash scripts/run_cocotb.sh     # soc 72 + core 43 项通过
bash scripts/run_riscv_dv.sh   # riscv-dv + Sail lockstep 通过
bash scripts/synth.sh
sudo bash scripts/setup_opensta.sh # 一次性安装 CUDD + OpenSTA 到 /opt/opensta
bash scripts/sta.sh             # 期望 50 MHz 下 setup/hold MET
```

## 硬性规则

- 仅支持 Ubuntu/Linux。
- 保持 `rtl/core.v` 为单个文件；只使用 Verilog-2001。
- RTL 修改前后：46/46 ISA 测试、SoC demo、cocotb 套件和 riscv-dv lockstep
  必须通过。
- 不要删除 `IF_ID_*_hold`、`id_flush_q`、`bus_conflict`、x0-before-bypass，
  或 `EX_funct3[1]` 除法结果选择。原因见 `CLAUDE.md`。
- `tests/isa/env/arch_test.h` 包含本地 `la x0` 兼容补丁；保留它。
- `tests/isa/` 与 `lib/` 已 vendored；`build/`、`logs/` 被忽略。
- OpenSTA/CUDD 系统安装在 `/opt/opensta`，命令为 `/usr/local/bin/sta`。
