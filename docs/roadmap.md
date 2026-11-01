# 路线图与已知差距

## 已完成

- 五级 RV32IMACF core，带 ITCM/DTCM、EX/MEM->ID 前递、IF 级 BTB 分支预测，
  以及分级流水线的 RV32F 浮点单元。
  RVC 在 IF 展开为 32 位（IALIGN=16，双 word 取指窗口无装配气泡）；RV32A 为
  LR.W/SC.W + 九个 AMO*.W，支持 TCM。
- 机器模式 CSR、陷阱、定时器/软件/外部中断、`mret`。
- M 扩展：单周期乘法、迭代除法/余数。
- F 扩展：RV32F 浮点单元（`rtl/fp_unit.v`，分级流水线），支持 FLW/FSW、算术、
  FMA、除法/开方、比较、分类、转换和五种舍入模式；fcsr/fflags/mstatus.FS 完整。
- 简易总线，带字节 strobe、ready 握手、连续事务分离。
- 定时器、GPIO、UART、SPI 外设和 SoC top。
- 官方 `riscv-arch-test` 2.x RV32I/M/C：73/73 通过；项目自带 A 定向测试 3/3。
  官方 F 套件 146/146。合计 ISA 222/222。
- 裸机 SoC demo：UART hello、GPIO、三次定时器中断。
- cocotb 套件：SoC/外设 72 项 + core 微架构 77 项（含 RVC 6 项、A 4 项、F 3 项），
  含随机与定向测试；F 不在 RV32Interp 中，由 146 项 ISA F 测试覆盖。
- riscv-dv + Sail 指令级随机 lockstep 验证（3 个随机种子 + 定向陷阱 + 定向 A +
  定向 C，6/6）。随机生成器仍只产生 I/M；C/A 由定向镜像覆盖，F 由 ISA 套件覆盖。
- benchmark：CoreMark best（RV32IMACF）与 Dhrystone ground/best，
  分数见 `docs/benchmarks.md`。
- Yosys + Nangate45 综合流程和 Nangate45 Liberty。
- OpenSTA 3.1.0 pre-CTS 100 MHz timing MET（数值见 `docs/verification.md`）。
- Ubuntu-first 脚本和文档。

## 后续优先级

1. **Post-CTS / 完整时序签核**
   - 当前 100 MHz pre-CTS 结果见 `docs/verification.md`（OpenSTA 3.1.0，理想时钟，
     2-word TCM 时序配置，ABC fast + upsize）。
   - CTS 后重新跑 hold；加入真正的 resizer/buffer tree。
   - 用 SRAM 宏 + Liberty RAM 模型替换推断 TCM 并重跑。
   - 当前 FPU 已做四级流水并满足 100 MHz；如果 post-CTS 仍失败，进一步拆分
     FMA 对齐/加法级。

2. **TCM 存储器加固**
   - 用 SRAM 宏 / FPGA BRAM 替换推断阵列，获得真实时序和面积。
   - 增加带干净读/写接口和 Liberty 模型的存储器 wrapper。
   - 为 FPGA/ASIC 流程参数化大小和初始化文件。

3. **Privilege / 平台测试**
   - 2.x `privilege` 套件需要可写的 `mtvec` trampoline、`medeleg`、`mideleg`、
     PMP CSR、`mtval` 和 S-mode stub。决定产品是否需要这些。
   - 如果不需要，记录支持的 profile，并以 I/M 作为合规目标。

4. **F 扩展验证深化（可选）**
   - 给 riscv-dv 接入 F 随机指令（Sail 已支持 F）或增加定向 F lockstep 镜像。
   - 在 cocotb 中增加 FP 寄存器 scoreboard 与 FMA/除法/NaN 的定向用例。

5. **外设验证**
   - SPI 回环测试（testbench 中 MOSI 接 MISO），覆盖四种模式。
   - 用 testbench 发送器做 UART RX 测试，覆盖 overrun/framing。
   - 用 testbench 驱动引脚做 GPIO 边沿中断测试。
   - 定时器预分频/单次/周期边界情况。

6. **性能（可选）**
   - ~~增加 EX/MEM->ID 前递~~（已完成）。
   - ~~小分支缓冲~~（已完成：64 项 BTB + 2-bit 计数器）。
   - 后续可选：返回地址栈（RAS）改善函数返回的预测、更大/关联 BTB。
   - 保持设计最简；性能工作不能破坏 ISA/cocotb/lockstep 门禁。

7. **调试 / 集成（可选）**
   - JTAG 调试模块或简单的 halt/step 调试端口。
   - 如果 SoC 需要接入更大的 fabric，加 AXI-Lite 桥。
   - FPGA 板级 top 和约束（不属于当前 Linux-only 流程）。

## 明确的非目标（暂时）

- Zacas/AMOCAS、Zabha 子字原子、A 的 .D 变体、原子指令访问 MMIO、非对齐原子。
- D 扩展、MMU、cache。
- 多核 / 一致性互连。
- DMA。
- 完整 Linux 启动。

## 验证债务

- 222/222 ISA 测试和 SoC demo 已在 Ubuntu 26.04 验证（GCC 14.2，Icarus 12，
  Yosys 0.52）。OpenSTA 3.1.0 pre-CTS 100 MHz 通过；后续记录 post-CTS 和 CI 结果。
- 尚未签入 CI。最小 GitHub Actions 任务可运行 `setup_ubuntu.sh`、`fetch_isa.py`、
  `run_isa.py`、`build_demo.sh`；综合/STA 需要 OpenSTA，最好在 self-hosted runner
  上运行。
- 已有 cocotb 随机/定向测试（含 RVC/A）和 riscv-dv + Sail 指令级随机测试；
  随机 C/A 生成尚未接入（定向镜像覆盖）；F 目前只有 ISA 覆盖，没有随机/lockstep；
  仍缺功能覆盖率收集/闭合、SVA/形式属性、门级 SDF 仿真和 CI。
