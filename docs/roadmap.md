# 路线图与已知差距

## 已完成

- 五级 RV32IM core，带 ITCM/DTCM、基于停顿的相关处理、WB->ID 旁路。
- 机器模式 CSR、陷阱、定时器/软件/外部中断、`mret`。
- M 扩展：单周期乘法、迭代除法/余数。
- 简易总线，带字节 strobe、ready 握手、连续事务分离。
- 定时器、GPIO、UART、SPI 外设和 SoC top。
- 官方 `riscv-arch-test` 2.x RV32I/M：46/46 通过。
- 裸机 SoC demo：UART hello、GPIO、三次定时器中断。
- cocotb 套件：SoC/外设 72 项 + core 微架构 43 项，含随机与定向测试。
- riscv-dv + Sail 指令级随机 lockstep 验证（含定向陷阱镜像）。
- Yosys + Nangate45 综合流程和 Nangate45 Liberty。
- OpenSTA 3.1.0 pre-CTS 50 MHz timing MET（setup +15.94 ns，hold +0.058 ns，
  TNS/WNS 0.00）。
- Ubuntu-first 脚本和文档。

## 后续优先级

1. **Post-CTS / 完整时序签核**
   - 当前 50 MHz pre-CTS 结果：setup +15.94 ns，hold +0.058 ns，TNS/WNS 0.00
     （OpenSTA 3.1.0，理想时钟，2-word TCM 时序配置，ABC fast + upsize）。
   - CTS 后重新跑 hold；加入真正的 resizer/buffer tree。
   - 用 SRAM 宏 + Liberty RAM 模型替换推断 TCM 并重跑。
   - 如果 post-CTS 乘法器/除法器路径失败，把乘法改为迭代或流水化除法器。

2. **TCM 存储器加固**
   - 用 SRAM 宏 / FPGA BRAM 替换推断阵列，获得真实时序和面积。
   - 增加带干净读/写接口和 Liberty 模型的存储器 wrapper。
   - 为 FPGA/ASIC 流程参数化大小和初始化文件。

3. **Privilege / 平台测试**
   - 2.x `privilege` 套件需要可写的 `mtvec` trampoline、`medeleg`、`mideleg`、
     PMP CSR、`mtval` 和 S-mode stub。决定产品是否需要这些。
   - 如果不需要，记录支持的 profile，并以 I/M 作为合规目标。

4. **外设验证**
   - SPI 回环测试（testbench 中 MOSI 接 MISO），覆盖四种模式。
   - 用 testbench 发送器做 UART RX 测试，覆盖 overrun/framing。
   - 用 testbench 驱动引脚做 GPIO 边沿中断测试。
   - 定时器预分频/单次/周期边界情况。

5. **性能（可选）**
   - 增加 EX/MEM->ID 前递，消除 2 周期 RAW 停顿。
   - 考虑在 ID 解析分支，或加一个小分支缓冲。
   - 保持设计最简；性能工作不能破坏 46/46 门禁。

6. **调试 / 集成（可选）**
   - JTAG 调试模块或简单的 halt/step 调试端口。
   - 如果 SoC 需要接入更大的 fabric，加 AXI-Lite 桥。
   - FPGA 板级 top 和约束（不属于当前 Linux-only 流程）。

## 明确的非目标（暂时）

- A/F/D/C 扩展、MMU、cache、浮点。
- 多核 / 一致性互连。
- DMA。
- 完整 Linux 启动。

## 验证债务

- 46/46 ISA 测试和 SoC demo 已在 Ubuntu 26.04 验证（GCC 14.2，Icarus 12，
  Yosys 0.52）。OpenSTA 3.1.0 pre-CTS 50 MHz 通过；后续记录 post-CTS 和 CI 结果。
- 尚未签入 CI。最小 GitHub Actions 任务可运行 `setup_ubuntu.sh`、`fetch_isa.py`、
  `run_isa.py`、`build_demo.sh`；综合/STA 需要 OpenSTA，最好在 self-hosted runner
  上运行。
- 已有 cocotb 随机/定向测试和 riscv-dv + Sail 指令级随机测试；
  仍缺功能覆盖率收集/闭合、SVA/形式属性、门级 SDF 仿真和 CI。
