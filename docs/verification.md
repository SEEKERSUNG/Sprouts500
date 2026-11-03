# 验证指南

所有命令都假定在仓库根目录执行，且 Ubuntu 主机已用 `bash scripts/setup_ubuntu.sh`
准备完毕。

## 1. 回归门禁

任何 RTL 修改后运行：

```bash
python3 scripts/run_isa.py     # 期望：222/222 通过（I/M/C/F 官方 + A 定向）
bash scripts/build_demo.sh     # 期望：TEST_PASS
bash scripts/build_app.sh sw/example   # 自写 C 运行时；期望 TEST_PASS
MARCH=rv32imafc_zicsr bash scripts/test_boot.sh  # FPGA UART Boot；期望 BOOT_TEST_PASS
bash scripts/setup_cocotb.sh   # 一次性：.venv + cocotb/pytest（需要 python3.14-dev）
bash scripts/run_cocotb.sh     # 期望：cocotb/soc 72 + cocotb/core 79 全部通过
bash scripts/setup_riscv_dv.sh # 一次性：.venv-dv + Sail；见 docs/verification_riscv_dv.md
bash scripts/run_riscv_dv.sh   # 期望：6/6 lockstep PASS（3 随机 + 定向陷阱/A/C）
bash scripts/synth.sh          # 期望：Yosys 完成并写出 build/syn/core_netlist.v
bash scripts/sta.sh            # 在 sudo bash scripts/setup_opensta.sh 之后；期望 setup/hold MET
```

`scripts/sta.sh` 在 100 MHz 下当前报告 setup **+3.148 ns**、hold **+0.083 ns**、
TNS/WNS **0.00**（OpenSTA 3.1.0，理想时钟，2-word TCM 时序配置）。时钟尚未做 CTS，
因此 CTS 后以及换用 SRAM 宏后需要重新跑 hold 签核。

ISA 套件是主要的功能门禁。SoC demo 是集成门禁（总线、UART、定时器中断、GPIO）。
cocotb 套件补充流水线/总线/外设的随机与边界测试，riscv-dv + Sail 做指令级随机
lockstep（随机流的 main body 不生成数据访存；访存与陷阱分别由定向镜像和
cocotb `test_mem_rand` / `test_core` 覆盖，详见 `docs/verification_riscv_dv.md` §5）。综合用于捕获推断 latch /
多驱动 / 未映射单元问题。

验证分层：

| 层 | 工具/数据 | 文档 |
|---|---|---|
| 指令语义 | riscv-arch-test 2.x 签名 | 本文 §2 |
| SoC 集成 | `sw/demo`、`sw/lib` 裸机程序 | 本文 §3–§4 |
| 微架构/总线/外设 | cocotb（Icarus，随机 + 定向） | `docs/verification_cocotb_core.md`、`docs/verification_cocotb.md` |
| 指令级随机 | riscv-dv 生成 + Sail 参考模型 lockstep | `docs/verification_riscv_dv.md` |
| 可综合性/时序 | Yosys、OpenSTA | 本文 §7、`docs/toolchain.md` |
| 性能分数 | CoreMark、Dhrystone（`cycle` CSR 计时） | `docs/benchmarks.md` |

### 1.1 修改 RTL 后的流程

不需要手动编译，也不需要 `make clean`：每个 runner 都会用当前 RTL 重新编译。
`python3 scripts/fetch_isa.py` 只是下载测试源码（且已 vendored），与重编译无关。

按改动类型选择门禁：

| 改了什么 | 需要跑 |
|---|---|
| `rtl/core.v`、`rtl/fp_unit.v` | `run_isa.py`、`build_demo.sh`、`build_app.sh`、`run_cocotb.sh`、`run_riscv_dv.sh`、`synth.sh`（涉及时序再 `sta.sh`） |
| `rtl/bus.v`、`rtl/timer.v`/`gpio.v`/`uart.v`/`spi.v`、`rtl/soc_top.v` | `build_demo.sh`、`build_app.sh`、`run_cocotb.sh`（SoC 套件）；ISA 只编译 core，覆盖不到这些 |
| `sim/tb_core.v` | `run_isa.py` |
| `sim/tb_soc.v` | `build_demo.sh` |
| `sim/tb_app.v`、`sw/lib/` | `build_app.sh sw/example` |
| `cocotb/` | `run_cocotb.sh` |
| `verif/riscv_dv/`、`sim/tb_lockstep.v` | `run_riscv_dv.sh` |
| `bench/`、`sim/tb_bench.v` | `build_bench.sh`（性能分数，不是功能门禁） |
| `constraints/core.sdc` | `synth.sh` 后 `sta.sh` |

注意：`scripts/synth.sh` 目前只综合 `rtl/fp_unit.v` + `rtl/core.v`，不综合总线/外设/`soc_top.v`；
`scripts/sta.sh` 读的是 `build/syn/core_netlist.v`，所以必须先 `synth.sh` 再
`sta.sh`。

单测与日志：

```bash
python3 scripts/run_isa.py jalr-01
bash scripts/run_cocotb.sh cocotb/soc -k timer -x
bash scripts/run_riscv_dv.sh 7
```

- ISA 失败：`logs/isa/<name>.log`（前 16 个签名不匹配项）。
- cocotb 失败：`logs/cocotb/run-*.log`、`logs/cocotb/junit*.xml`；用 `SEED=<n>` 复现。
- riscv-dv 失败：`logs/riscv_dv/seed_<n>.log`、`build/riscv_dv/seed_<n>/compare.log`。
- 手工波形：用生成的 hex 直接跑 testbench 并加 `+VCD`。
- 总线访问记录：在手工运行 core testbench 时加 `+BUS_TRACE=<file>`，core 会为每个
  总线事务写一行 `R/W <addr> <data> <wstrb>`（性能测试用它导出 UART 输出）。

## 2. 官方 ISA 测试

### 2.1 源码与参考输出

`tests/isa/` 是 [riscv-arch-test](https://github.com/riscv/riscv-arch-test)
`old-framework-2.x` 分支的 vendored 子集：

- `tests/isa/rv32i_m/I/src/*.S` 与 `references/*.reference_output`（38 个测试）
- `tests/isa/rv32i_m/M/src/*.S` 与 `references/*.reference_output`（8 个测试）
- `tests/isa/rv32i_m/C/src/*.S` 与 `references/*.reference_output`（27 个测试）
- `tests/isa/rv32i_m/F/src/*.S` 与 `references/*.reference_output`（146 个测试，
  上游 old-framework-2.x 的 RV32F 套件；`fetch_isa.py` 会增量抓取，部分源文件很大）
- `tests/isa/rv32i_m/A/src/*.S` 与 `references/*.reference_output`（项目自带的 3 个
  定向测试：`amo_w-01`、`lrsc-01`、`amo_trap-01`；上游 old-framework 树没有 A 套件）
- `tests/isa/env/arch_test.h`、`encoding.h`

`scripts/fetch_isa.py` 需要时重新下载。默认使用 `raw.githubusercontent.com`；
网络受限时可设置 `ISA_MIRROR=<前缀>` 覆盖下载地址。

### 2.2 测试执行流程

`scripts/run_isa.py` 对每个测试：

1. 使用 `riscv64-unknown-elf-gcc -march=rv32i|rv32im|rv32ic_zicsr|rv32ia_zicsr|rv32if_zicsr -mabi=ilp32
   -nostdlib -nostartfiles -static -mcmodel=medany -DXLEN=32` 编译（F 套件另加
   `-DFLEN=32`，使 `arch_test.h` 选择 `flw/fsw`）。
2. 使用 `sw/isa/link.ld` 链接：代码位于 0，data/signature 位于代码之后的
   `ALIGN(0x1000)`，不使用固定的 0x8000。
3. 把 ELF 转换成扁平二进制，并打包成 32 位 word 的 hex 文件。
4. 从 `nm` 读取 `begin_signature`。
5. 在 Icarus 下运行 `sim/tb_core.v`。
6. 比较 `dut.ITCM[begin_signature + 4*i]` 与参考 word `i`。F 套件的参考签名较大，
   testbench 的 `ref_mem` 已扩到 131072 word。

### 2.3 Testbench 细节

`sim/tb_core.v`：

| plusarg | 含义 |
|---|---|
| `+ITCM_HEX=` | 加载到 ITCM 地址 0 的扁平 word hex |
| `+SIG_ADDR=` | `begin_signature` 的字节地址 |
| `+SIG_LEN=` | 签名字数 |
| `+REF_FILE=` | 参考签名文件 |
| `+MAX_CYCLES=` | 超时（默认 5,000,000；runner 使用 2,000,000） |
| `+TRACE` | 把退休/存储 trace 写入 `build/trace.txt` |
| `+TRACE_PIPE` | 把前 120 个周期的流水线状态写入 `build/pipe.txt` |
| `+VCD` | 导出 `build/tb_core.vcd` |

仿真 ITCM 为 **4 MiB**（`ITCM_WORDS = 1048576`）。`jal-01` 的对齐填充使镜像约
1.7 MiB，因此必须这么大。halt mailbox 位于 ITCM 末尾保留字 `0x003F_FFF0`；
`sw/isa/model_test.h` 在那里写入 `0x5A5A5A5A` 并循环。

`sw/isa/model_test.h` 还定义了空的 IO 宏和签名区域，因此测试框架的自检宏退化为
签名存储。

### 2.4 `jalr-01` 兼容性补丁

2.x 的 `TEST_JALR_OP` 宏在 `rd=x0` 测试用例中展开为 `la x0, 5b`。较新的 binutils
会拒绝 `la x0`（“illegal operands”）。`tests/isa/env/arch_test.h` 带有本地补丁：

```asm
.ifc rd, x0
nop
.else
LA(rd,5b)
.endif
```

这在语义上正确：`la x0` 本来就不可能写 x0。如果重新抓取测试，请重新应用补丁
（或保留已提交的 `tests/isa/env/arch_test.h`）。

### 2.5 已知预期结果

222/222（I 38 + M 8 + C 27 + F 146 官方套件，加上项目自带的 A 定向 3 个）在 Ubuntu 26.04
通过（GCC 14.2，Icarus 12）。每个测试的日志在
`logs/isa/<name>.log`。失败日志会打印 `SIG_MISMATCH idx=... got=... exp=...`。

## 3. SoC demo

`bash scripts/build_demo.sh`：

1. 使用 `-march=rv32imacf_zicsr` 编译 `sw/demo/start.S`、`trap.S`、`main.c`。
2. 把镜像打包成 ITCM word。
3. 在 100 MHz 下运行 `sim/tb_soc.v`。

预期 UART 输出：

```text
Hello Sprouts500!
peripherals ok
irq ok
UART_RX_BYTES=40
TEST_PASS cycles=<n>
```

它验证：

- ITCM 取指/load/store、栈（demo 的 `.data`/`.bss`/栈都在 ITCM）。
- 总线读/写、连续总线访问（`bus_conflict`）。
- UART TX 轮询，以及 115200 下的时序（100 MHz 时每比特 868 个时钟）。
- 定时器计数和 W1C pending。
- GPIO 方向/输出。
- 机器定时器中断：`mtvec`、`mstatus.MIE`、`mie.MTIE`、`mepc`、`mcause`、
  陷阱进入/返回、`mret`；C ISR 由汇编陷阱处理程序调用。

`sim/tb_soc.v` 以 868 个时钟的位周期解码 `uart_tx`，把字节追加到 `rx_buf`，并与
40 字节预期字符串比较。halt mailbox 是 `0x7FFC`（32 KiB ITCM 顶部）；demo 在 UART
排空后写入。

Plusarg：`+ITCM_HEX=`、`+MAX_CYCLES=`、`+VCD`。

## 4. 自写 C 程序

`bash scripts/build_app.sh <app-dir>` 编译 `sw/lib/` 运行时和 `<app-dir>` 下的所有
`.c`/`.S`，打包后在 `sim/tb_app.v` 中运行。与 demo 不同，这个 testbench 不比较
固定字符串，而是：

- 监听 ITCM `0x7FFC` 的 halt magic `0x5A5A5A5A`；
- 读取 ITCM `0x7FF8` 的结果码，与 `+EXPECT`（默认 0）比较；
- 把 UART 收到的字节实时打印出来；
- 超时打印 `APP_TIMEOUT cycles=... pc=...`。

运行时约定：

| 项目 | 约定 |
|---|---|
| 镜像 | `.text/.rodata/.data` 在 ITCM（32 KiB），由 `$readmemh` 预载 |
| `.bss`/栈 | DTCM（16 KiB），`crt0.S` 清零 `.bss`，`sp = 0x1000_4000` |
| 结束 | `main` 返回值写 `0x7FF8`，`0x7FFC` 写 magic |
| 陷阱 | `trap.S` 调用 `trap_handler(cause, epc, tval)`，弱默认实现会 halt |
| 运行时文件 | `sw/lib/{crt0.S,trap.S,soc.c,string.c,soc.h,app.ld}` |

`sim/tb_app.v` plusarg：

| plusarg | 含义 |
|---|---|
| `+ITCM_HEX=` | 镜像文件（脚本自动传） |
| `+MAX_CYCLES=` | 超时，默认 2,000,000 |
| `+EXPECT=` | 期望结果码，默认 0 |
| `+DRAIN_NS=` | halt 后等待 UART 排空，默认 1,000,000 ns |
| `+UART_FILE=` | 另存收到的 UART 字节（前 256 字节会被保存并写出） |
| `+GPIO_IN=<hex>` | 驱动配置为输入的 GPIO 引脚 |
| `+SPI_LOOPBACK` | MOSI 接回 MISO |
| `+VCD` | 生成 `build/tb_app.vcd` |

参考程序 `sw/example` 覆盖 DTCM、定时器、GPIO 回读和定时器中断。完整教程见
`docs/c_programming.md`。

> 注意：`sim/tb_soc.v` 的期望字符串写死为 demo 输出，改 `sw/demo/main.c` 的打印
> 内容会失败；写自己的程序请用 `build_app.sh` + `tb_app.v`。

## 5. 调试

- 重跑单个 ISA 测试：`python3 scripts/run_isa.py jalr-01`。
- Core trace：在 `vvp` 调用中加入 `+TRACE`（runner 没有暴露该参数，因此需要用生成的
  `build/isa/<test>.hex` 手动运行 testbench）。
- 流水线 trace：`+TRACE_PIPE`；查看 `build/pipe.txt`。
- 波形：`+VCD`，然后 `gtkwave build/tb_core.vcd`（或 `tb_soc.vcd`）。
- 如果挂死，检查超时行：它会打印 `cycles`、`pc` 和 halt word。
- 如果签名不匹配，会打印前 16 个不匹配项。
- ISA runner 会把完整仿真输出写入 `logs/isa/<test>.log`。

## 6. 添加测试

- 在 `tests/isa/rv32i_m/<I|M>/` 下增加测试 `.S` 及其参考文件。
- runner 会发现 `src/*.S`，并匹配 `references/<name>.reference_output`。
- 链接脚本/harness 假定代码在 0、签名在 `.data`；保持该布局。
- 如果测试需要 CSR/陷阱，2.x 的 `privilege` 套件需要完整的陷阱 trampoline 和
  PMP/delegation 支持（尚未实现）。

## 7. 综合健全性检查

`bash scripts/synth.sh` 在 CPU 时序运行时把 ITCM/DTCM 设为 2 word，依次运行
`synth`、`dfflibmap`、`abc -fast -liberty`，写出结构网表，然后运行
`scripts/upsize_fanout.py` 提升高扇出驱动的强度。当前该时序配置为 **97,225** 个
Nangate45 单元。`bash scripts/sta.sh` 随后报告 100 MHz pre-CTS 结果：setup
+3.148 ns / hold +0.083 ns，TNS/WNS 0.00。完整签核必须用 SRAM 宏替换 TCM，并在 CTS
后重新检查 hold。如果 Yosys 报告推断 latch、多驱动或 `$mem` 单元，请先修 RTL 或脚本。

## 8. cocotb 与 riscv-dv 套件

两套扩展验证有独立文档，这里只列入口：

- `bash scripts/setup_cocotb.sh && bash scripts/run_cocotb.sh`
  - `cocotb/soc`：总线协议、`ready` 拉低、背靠背访问、`bus_conflict`，以及
    timer/GPIO/UART/SPI 的寄存器、边界、随机测试和 reference model scoreboard。
  - `cocotb/core`：随机 ALU/M/访存/分支流、相关停顿、CSR/陷阱、中断时序、
    除法冻结、TCM 写门控、RVC 展开/控制流/trap、RV32A（AMO/LR/SC）。RV32F 的
    语义由官方 146 项 F ISA 测试覆盖，cocotb 的 RV32Interp 不含浮点。
  - 详见 `docs/verification_cocotb.md`、`docs/verification_cocotb_core.md`。
- `bash scripts/setup_riscv_dv.sh && bash scripts/run_riscv_dv.sh`
  - riscv-dv 生成 RV32IM 随机程序，在 RTL 与 Sail 0.16 之间逐条比对退休指令、
    寄存器写、存储和陷阱记录；每次运行还固定执行定向陷阱、RV32A 与 RV32C 镜像。
    F 没有加入随机生成器或 lockstep 比较，由 ISA F 套件覆盖。
  - 详见 `docs/verification_riscv_dv.md`。

当前基线：cocotb **72 + 79 项通过**；riscv-dv + Sail 默认 3 个种子加三个定向镜像
**6/6 `COMPARE_PASS`**。

## 9. FPGA UART Boot 回归

`bash scripts/test_boot.sh` 先运行 `tests/boot/test_uart_boot.py` 的 7 项协议/打包单元测试，
再构建 2 KiB Bootloader 和 `sw/boot_test` 应用，在 `sim/tb_boot.v` 上通过真实 UART RTL
完成下载。检查包括 CRC/越界/未知命令拒绝、非对齐分块、逐字节 ITCM 比较、Boot 区首字/
末字/末字节写保护、保护区 SC 失败和 AMO 不改变内存、损坏镜像 CRC 不执行、应用运行、
普通复位自动启动和 BOOT0 强制下载。成功标志为 `BOOT_TEST_PASS`，日志在
`logs/boot/tb_boot.log`。协议和 FPGA 集成见 `docs/fpga_boot.md`；该仿真不替代实际上板、
BRAM 和引脚时序签核。
