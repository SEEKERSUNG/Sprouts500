# 工具链与构建环境（Ubuntu）

本项目仅支持 Ubuntu/Linux。OpenSTA/CUDD 现在系统安装在 `/opt/opensta`，由
`scripts/setup_opensta.sh` 创建。

## 1. 主机软件包

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake ninja-build git wget curl python3 \
  iverilog yosys gtkwave \
  tcl-dev swig bison flex automake autotools-dev \
  libgtest-dev libeigen3-dev libfmt-dev \
  gcc-riscv64-unknown-elf
```

如果需要使用本地镜像，可运行 `bash scripts/setup_apt_mirror.sh`（默认镜像可用
`MIRROR=` 覆盖）；常规安装直接 `bash scripts/setup_ubuntu.sh`。

已验证的工具版本：

| 工具 | 版本 | 用途 |
|---|---|---|
| `riscv64-unknown-elf-gcc` | 14.2.0 | 裸机 RV32 编译器 |
| Icarus Verilog | 12.0 | RTL 仿真 |
| Yosys | 0.52 | 综合 |
| Python | 3.14 | 测试 runner/抓取脚本 |
| 主机 GCC/G++ | 15.2 | 构建 CUDD/OpenSTA |
| TCL / SWIG / Bison / Flex | 8.6.17 / 4.4 / 3.8 / 发行版 | 构建 OpenSTA |
| CMake / Ninja | 4.2 / 发行版 | 构建 OpenSTA |
| CUDD | 3.0.0 | OpenSTA 依赖 |

`gcc-riscv64-unknown-elf` 位于 `universe`。脚本使用
`RISCV_PREFIX=riscv64-unknown-elf-`；如果安装其他工具链，覆盖它：

```bash
RISCV_PREFIX=riscv-none-elf- python3 scripts/run_isa.py
```

## 2. 项目本地环境

```bash
source scripts/env.sh
```

它设置 `SPROUTS500_ROOT`、`RISCV_PREFIX`（默认 `riscv64-unknown-elf-`）和
`PYTHON`（默认 `python3`），并把 `OPENSTA_HOME`（默认 `/opt/opensta`）的 `bin`
加入 PATH。

## 2.1 ISA 字符串

core 是 RV32IMACF + Zicsr；默认脚本用 `rv32imacf_zicsr -mabi=ilp32`：F 寄存器/指令
可用，参数仍走整数寄存器的软浮点 ABI（浮点表达式由 GCC 生成 F 指令，结果经
fmv 往返）。
各脚本默认的 `-march`：

| 脚本 | 默认 `-march` | 说明 |
|---|---|---|
| `run_isa.py` | I=`rv32i`、M=`rv32im`、C=`rv32ic_zicsr`、A=`rv32ia_zicsr`、F=`rv32if_zicsr`（另加 `-DFLEN=32`） | 每个套件用对应 ISA 编译（arch-test 源码里明确写了 `RVTEST_ISA`） |
| `build_demo.sh`、`build_app.sh`、`build_bench.sh` | `rv32imacf_zicsr` | 可用 `MARCH=...` 覆盖，例如 A/B 对比 `MARCH=rv32im_zicsr` |
| `run_riscv_dv.sh` | 随机种子 `rv32im_zicsr`；定向 A `rv32ia_zicsr`、定向 C `rv32ic_zicsr` | 随机生成器的 `rv32imc` target 只支持 I/M |
| `run_cocotb.sh` | 不涉及编译器；指令由 Python 编码器生成 | — |

Sail（`verif/riscv_dv/sail_rv32im.json`）启用了 M/A/C（`Zca`）；RTL 还支持 Zcf
（压缩浮点 load/store）和 RV32F，但 Sail 配置未启用 Zcf/F，lockstep 也不比较浮点：
RV32F 语义由 146 项 ISA F 测试覆盖，Zcf 由 `sw/example` 的 C 浮点测试覆盖。

## 3. Icarus Verilog

testbench 使用 Verilog-2012 特性（`$value$plusargs`、`$test$plusargs`、层次化存储器
`$readmemh`、`join_any`、`disable fork`）。始终使用 `-g2012`：

```bash
iverilog -g2012 -o build/tb_core.vvp sim/tb_core.v rtl/core.v rtl/fp_unit.v
vvp build/tb_core.vvp +ITCM_HEX=... +SIG_ADDR=... +SIG_LEN=... +REF_FILE=...
```

Icarus 支持把 `$readmemh` 读入子模块数组（`dut.ITCM`），ISA testbench 依赖这一点。
本流程不使用 Verilator。

## 4. Yosys 综合

`scripts/synth.sh`：

```
read_verilog rtl/fp_unit.v rtl/core.v
chparam -set ITCM_WORDS 2 -set DTCM_WORDS 2 core
synth -top core -flatten
dfflibmap -liberty lib/NangateOpenCellLibrary_typical.lib
abc -fast -liberty lib/NangateOpenCellLibrary_typical.lib
write_verilog -noattr -noexpr build/syn/core_netlist.v
stat
python3 scripts/upsize_fanout.py build/syn/core_netlist.v 40
```

TCM 是推断寄存器阵列；CPU 时序运行时缩为 2 word。真实 ASIC/FPGA 签核时，请替换为
SRAM/BRAM 宏和 Liberty 模型。`abc -fast` 用映射质量换运行时间；
`scripts/upsize_fanout.py` 提升高扇出扁平多路选择/使能驱动的强度（一阶 pre-CTS 修正；
正式流程应使用 resizer + CTS 替代）。该时序配置（ABC 后）约 97.0k 个 Nangate45 单元（含分级流水 FPU）；
面积报告见 `logs/syn/yosys.log`。

如果你的 Yosys 版本需要单独的 `abc` 可执行文件，安装发行版软件包；Ubuntu 软件包会
同时提供 `yosys-abc` 和 `yosys`。

## 5. Nangate45 Liberty

`lib/NangateOpenCellLibrary_typical.lib` 不随仓库分发（已 gitignore）；本地自备或重新抓取：

```bash
bash scripts/fetch_lib.sh
```

网络受限时可设置 `GH_RAW_MIRROR=<前缀>` 覆盖下载地址。

这是 typical corner 库（TT，25 C，1.1 V）。时序签核必须说明 corner；需要时加入
SS/FF corner。

## 6. cocotb 与 riscv-dv 环境

两套验证环境都只装在仓库内的 Python 虚拟环境里，不需要 sudo，也不写系统目录：

- `bash scripts/setup_cocotb.sh` 创建 `.venv`，安装 `cocotb` 和 `pytest`。
  cocotb 走 Icarus 的 VPI，需要 **Python 开发头文件**：
  `sudo apt-get install python3.14-dev`（版本与 `python3 --version` 一致）。
- `bash scripts/setup_riscv_dv.sh` 创建 `.venv-dv`，安装 `pyboolector`（PyPI）和
  仓库内 vendored 的 `pyvsc`/`pyucis` 轮子，并把 Sail RISC-V 0.16 预编译模型
  解压到 `build/tools/sail`。

两个 venv 都在 `.gitignore` 里。riscv-dv 的生成器源码已 vendored 在
`verif/riscv_dv/vendor/`，离线可复现；Sail tarball 首次需要联网下载。

用法见 `docs/verification_cocotb.md`、`docs/verification_cocotb_core.md` 和
`docs/verification_riscv_dv.md`。

## 7. OpenSTA + CUDD

OpenSTA 的 CMake 需要 CUDD（`cmake/FindCUDD.cmake`）。官方
`Dockerfile.ubuntu22.04` 从源码构建 CUDD 3.0.0，再构建 OpenSTA。脚本遵循该流程：

```bash
sudo bash scripts/setup_opensta.sh
bash scripts/sta.sh
```

说明：

- 安装前缀：`/opt/opensta`（可用 `OPENSTA_PREFIX=/path` 覆盖）。
- OpenSTA 源码：`/opt/opensta/OpenSTA`（git clone；可用 `OPENSTA_GIT=` 覆盖仓库 URL）。
- CUDD 源码：`/opt/opensta/cudd-3.0.0`（tarball）。
- 构建输出：`/opt/opensta/OpenSTA/build/sta`；安装副本为 `/opt/opensta/bin/sta`，
  命令链接为 `/usr/local/bin/sta`。
- 登录 shell 通过 `/etc/profile.d/opensta.sh` 设置 `OPENSTA_HOME=/opt/opensta`，
  并把 `$OPENSTA_HOME/bin` 加入 PATH。
- 当脚本找到 `/usr/lib/x86_64-linux-gnu/libtcl8.6.so` 和
  `/usr/include/tcl8.6/tcl.h` 时会传入显式 TCL 路径。
- `USE_TCL_READLINE=OFF` 避免可选的 tclreadline 依赖。
- `BUILD_TESTS=OFF` 跳过基于 gtest 的单元测试。
- 强制重建：`sudo OPENSTA_REBUILD=1 bash scripts/setup_opensta.sh`。
- 覆盖仓库 URL/下载前缀时用 `sudo -E` 保留 `OPENSTA_GIT`/`GH_RAW_MIRROR` 环境变量。

已验证结果：OpenSTA 3.1.0，100 MHz pre-CTS，setup +3.251 ns，hold +0.083 ns，
TNS/WNS 0.00。时钟为理想时钟（无 CTS）；CTS 后需重新检查 hold。

### 手动构建 OpenSTA（备用）

```bash
# CUDD
sudo mkdir -p /opt/opensta && cd /opt/opensta
sudo wget https://raw.githubusercontent.com/davidkebo/cudd/main/cudd_versions/cudd-3.0.0.tar.gz
sudo tar xf cudd-3.0.0.tar.gz
cd cudd-3.0.0 && sudo ./configure && sudo make -j$(nproc)

# OpenSTA
cd /opt/opensta
sudo git clone --depth 1 https://github.com/The-OpenROAD-Project/OpenSTA.git
cd OpenSTA && sudo mkdir -p build && cd build
sudo cmake -DCUDD_DIR=../../cudd-3.0.0 -DCMAKE_BUILD_TYPE=Release \
      -DUSE_TCL_READLINE=OFF -DBUILD_TESTS=OFF ..
sudo make -j$(nproc)
sudo install -m 755 sta /usr/local/bin/sta
./sta -no_splash -exit /path/to/Sprouts500/scripts/sta.tcl
```

### 时序约束

`constraints/core.sdc`：

```
create_clock -name clk -period 20.0 [get_ports clk]
set_clock_uncertainty -setup 0.5 [get_clocks clk]
set_clock_uncertainty -hold 0.0 [get_clocks clk]
set_input_delay 2.0 -clock clk [get_ports {bus_rdata_i[*] bus_ready_i timer_irq_i ext_irq_i soft_irq_i}]
set_output_delay 2.0 -clock clk [all_outputs]
set_load 0.05 [all_outputs]
```

目标为 100 MHz，setup uncertainty 0.5 ns，hold uncertainty 0 ns（理想时钟，无 CTS），
板级/IO 延迟 2 ns。当前 pre-CTS 结果：setup slack +3.251 ns，hold slack +0.083 ns，
TNS/WNS 0.00。CTS 后重新跑 hold 签核；按目标频率调整周期。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `missing tool 'riscv64-unknown-elf-gcc'` | 运行 `scripts/setup_ubuntu.sh`；检查 `RISCV_PREFIX` |
| `la x0` 汇编错误 | 重新应用 `arch_test.h` 补丁（见 `docs/verification.md`） |
| Icarus `$readmemh` range warning | 无害；hex 比 4 MiB ITCM 短 |
| Yosys `ABC: merged SCL conversion failed` | 仅警告；会回退到 Liberty 格式 |
| OpenSTA CMake "CUDD library: not found" | 检查 `-DCUDD_DIR` 是否指向 CUDD 源码/构建树 |
| OpenSTA 找不到 TCL | 按 `scripts/setup_opensta.sh` 传入 `-DTCL_LIBRARY` / `-DTCL_HEADER` |
| UART demo 最后一字节不匹配 | 确保 `main.c` 在 halt 前等待 TX_BUSY；testbench 在 halt mailbox 后等待 100 us |
| 综合很慢 / 规模巨大 | TCM 是推断阵列；`scripts/synth.sh` 在 CPU 时序运行时会缩为 2 word |

## 9. 迁移说明

- 项目内不再保留 `tools/` 目录；OpenSTA/CUDD 现在安装在 `/opt/opensta`，可用
  `scripts/setup_opensta.sh` 重建。
- 不要复制 `build/` 或 `logs/`；重新生成。
- `tests/isa/` 已 vendored；Nangate45 Liberty 需本地自备，不随仓库分发。
- 脚本使用 LF 换行；检出时请保留。
