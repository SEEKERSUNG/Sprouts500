# riscv-dv + Sail 指令级随机验证

本层用 riscv-dv（pyflow 快照，vendored）生成约束随机 RV32IM 指令序列（C/A 由定向
镜像覆盖；RV32F 不在本层，由官方 146 项 ISA F 测试覆盖），把同一份 ELF 同时交给 Sail 0.16 参考模型和 Sprouts500 RTL（Icarus）
执行，再逐条比较退休指令、寄存器写回、store 流和陷阱。它补充 ISA 签名测试的定向覆盖，用来发现
"组合正确、彼此交互错误"的指令级问题。

## 1. 组成

| 路径 | 作用 |
|---|---|
| `verif/riscv_dv/vendor/pyflow` | riscv-dv 生成器（chipsalliance，pin 到固定 commit） |
| `verif/riscv_dv/vendor/wheels` | 离线 vendored `pyvsc` / `pyucis` |
| `verif/riscv_dv/vendor/_shim` | pandas / toposort / sitecustomize 兼容层 |
| `verif/riscv_dv/postprocess.py` | 把 riscv-dv 输出改写成裸机镜像（见 §4） |
| `verif/riscv_dv/link_rtl.ld` | 唯一的链接脚本：RTL 与 Sail 共用同一地址 |
| `verif/riscv_dv/sail_rv32im.json` | Sail 0.16 配置（RV32IMAC/Zca、与 RTL 相同的存储器映射） |
| `verif/riscv_dv/bin2hex.py` | 平坦二进制 -> `$readmemh` 字格式 |
| `verif/riscv_dv/compare.py` | trace 对齐比较 |
| `sim/tb_lockstep.v` | Icarus testbench，输出退休 trace |
| `scripts/setup_riscv_dv.sh` | 一次性环境准备（无 sudo） |
| `scripts/run_riscv_dv.sh` | 按种子跑生成 -> 编译 -> RTL/Sail -> 比较 |

## 2. 运行

```bash
bash scripts/setup_riscv_dv.sh       # 一次性：.venv-dv、Boolector、Sail 0.16
bash scripts/run_riscv_dv.sh         # 默认种子 1 2 3
bash scripts/run_riscv_dv.sh 7 42 999
INSTR_CNT=500 bash scripts/run_riscv_dv.sh 7   # 每个种子的期望指令数（默认 200）
make riscv-dv                        # setup + 默认种子
```

每次运行还固定执行三个**定向镜像**：

| 镜像 | 内容 |
|---|---|
| `verif/riscv_dv/directed/trap.S` | 非法指令编码（`0xffffffff`、`0x0000007f`、`0x0000700b`）、未对齐 load/store，由它自己的陷阱处理程序记录到 `trap_record` |
| `verif/riscv_dv/directed/amo.S` | 九个 AMO*.W、LR/SC 成功、有界 LR/SC 重试循环（A 扩展） |
| `verif/riscv_dv/directed/compressed.S` | 各种压缩编码、16/32 位混合、lane-1 32 位指令、压缩控制流与链接、非法压缩编码（C 扩展） |

IALIGN=16 后 2-mod-4 的分支/跳转目标是合法的，旧的“未对齐取指（cause 0）”
定向用例已从 `trap.S` 中移除；压缩非法指令的陷阱由 `compressed.S` 覆盖。

退出码为 0 当且仅当所有种子和定向镜像都 `COMPARE_PASS`。日志在
`logs/riscv_dv/seed_<s>.log`、`logs/riscv_dv/directed_*.log`，汇总在
`logs/riscv_dv/summary.txt`；中间产物（生成汇编、改写后汇编、ELF、RTL/Sail
trace、compare 输出）在 `build/riscv_dv/seed_<s>/`、`build/riscv_dv/directed_trap/`。
三个默认种子加三个定向镜像（6/6）在普通桌面机上约 15 秒完成。

`setup_riscv_dv.sh` 可以重复执行：已安装的包、已解压的 Sail 都会跳过。
Sail 从上游 release 下载，并校验大小与 SHA-256；离线环境下需要预先放好归档。

## 3. 比较内容

RTL testbench 按程序顺序写出以下行（流水线顺序退休，MEM 与 WB 顺序一致）：

```
I <pc> <ir> x<rd> <wdata>            退休指令（不写寄存器时 rd=x0）
S <addr> <pc> <ir> <data> <strb>     完成的 store
X <pc> <ir> <mcause> <mtval>         EX 级检测到的异常（该指令不退休）
```

`compare.py` 对 Sail 的 `--trace-instr --trace-arch-regs --trace-mem
--trace-exception` 输出做同样解析，然后：

1. **PC 与指令编码**：逐条对齐，任何重新定向（分支/jalr）错误都会在第一条
   不同 PC 处停止比较。RTL 的退休 IR 是 IF 展开后的规范 32 位编码，Sail 记录
   原始 16 位编码，`compare.py` 用与 RTL 相同的 `rvc_expand` 先做规范化；`c.sw` 的
   rd/store-rd 字段两边写法不同，store 只比较 opcode/funct3/rs1（地址与数据在 store
   流里单独比较）。
2. **寄存器写回**：每条指令写回的 rd/值必须一致（`x0` 不比较；RTL 的
   `WB_rd` 只在 `WB_rd_write_req` 有效时才被信任）。
3. **陷阱**：RTL `X` 事件与 Sail 的 `handling exc#...` 序列逐一比较
   `(mcause, pc, mtval)`。RTL 不退休陷阱指令，Sail 会在陷阱指令的 trace 条目上
   记录异常，因此对齐时跳过两侧的陷阱处理程序，并在 `mepc + 指令长度`（2 或 4）
   重新锚定。
4. **store 流**：按程序顺序比较 `(addr, data)`；RTL 只接受字宽 store
   （`wstrb` 必须为 `f`）。Sail 的 `--trace-mem` 不记录 AMO 写，RTL trace 也不记录；
   AMO 的内存效果由后续 load 的退休值比较。

两个模型跑在**同一地址**上：代码在 ITCM `0x0`，数据页、栈、`trap_record`、
`halt_mailbox` 在 DTCM `0x10000000` 起。`sail_rv32im.json` 把 RAM 配置在同样的
两个区间，因此 PC、寄存器值和 store 地址在两边数值相同，不需要任何基址平移。

运行在 `test_done` 处结束：RTL 在 `test_done` 指令到达 WB 时停止（之前的指令
全部已退休，之后的指令不可能先退休），Sail 用 `--stop-at-pc` 在执行它之前停止。

未映射地址检查针对 `EX_PC`（非投机），不是 `IF_PC`：core 有分支预测，IF 可能
短暂取到错误路径地址，但错误预测在进入 EX 之前就被纠正。

## 4. 生成程序的改写（postprocess.py）

riscv-dv 面向完整的特权级/Spike 仿真环境，改写后才是 Sprouts500 能执行的镜像：

- 固定的 CSR/mret 引导：`mtvec`、`mepc=init`、`mstatus=0x1800`、`mie=0`、
  `mret`；随后把所有 GPR（sp 除外）清零（该 pyflow 快照的 init 列表在某些种子
  里缺项）。
- 陷阱处理程序：用 sp 的 128 字节栈帧保存/恢复 GPR，把 mcause/mepc/mtval
  和陷阱计数写到 `trap_record`，读 `mepc` 处的半字判断指令长度（`opcode[1:0]!=3`
  为压缩），`mepc + 2/4` 后 `mret`。
- `test_done` 改为 `li gp, 1` -> `halt_mailbox` 写 `0x5A5A5A5A` -> 死循环。
- 删除 `csrr/csrw/...`、`fence`、`wfi`、`ebreak`；数据页用种子派生的确定序列
  重新生成在 DTCM 内。
- 不输出 `tohost` 符号（Sail 会据此插入 HTIF 设备，Sprouts500 没有 HTIF）。

## 5. 局限

- **没有数据 load/store 流**：当前 pyflow 快照生成的 main body 只有 ALU /
  M / 分支 / 跳转指令，`compare.py` 的 store 计数恒为 0。生成器自带的定向存储
  流使用不对齐偏移，尚未接入。数据访存由 cocotb `test_mem_rand` / `test_core`
  覆盖。
- **随机生成只有 I/M**：vendored 的 `rv32imc` target 的 `supported_isa` 只含
  I/M，`--disable_compressed_instr=0` 也不会产生 C 指令。C/A 由上面的定向镜像覆盖；
  需要随机 C/A 时得新增 target（不改 vendored 文件）。
- **随机流没有陷阱**：生成的随机程序不包含非法指令/ecall/未对齐访存。
  pyvsc 的非法指令注入（`--illegal_instr_ratio`）在当前环境下求解失败
  （`SolveFailure`），因此随机流不含陷阱；陷阱路径由定向镜像覆盖
  （非法指令 mcause=2/mtval=指令编码、load 未对齐 4、store 未对齐 6；压缩非法
  指令 mcause=2/mtval=零扩展半字），RTL 与 Sail 一致。
- **仅机器模式**，无 PMP、无特权级委派、无中断（`mie=0`，CLINT/定时器中断
  在 Sail 配置中关闭）；中断由 cocotb / demo 覆盖。
- **未比较 CSR 状态**（misa 等）与内存内容；比较的是 `X` 事件与 store 流。
- 改写脚本只处理 riscv-dv 已知的布局；布局变化时会直接报错而不是静默生成
  错误镜像。

## 6. 发现的问题

- 在验证本层的过程中没有发现需要修改 RTL 的功能错误：随机种子的逐指令比较、
  以及定向陷阱镜像（含非零非法指令编码的 mtval）都与 Sail 一致。
- 验证中修复的都是 harness 侧问题（记录在此供后续维护参考）：RTL `WB_rd`
  对分支/存储指令带残留值，必须以 `WB_rd_write_req` 为准；生成器 init 列表
  缺项导致 RTL 出现 X 操作数，需在改写时清零全部 GPR；RTL 与 Sail 必须在同一
  地址映射下运行，否则 `sp`/`mtvec` 等值会因基址不同而发散。
