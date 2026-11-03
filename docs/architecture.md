# Sprouts500 架构

本文档描述 `rtl/core.v`、`rtl/bus.v` 及各外设的实际 RTL 实现，同时记录了后续开发者
需要遵守的约束：“不变量”一节列出不能破坏的非显然细节。

## 1. 目标

- 最简的五级顺序 RV32IMACF core（RVC 在 IF 展开为 32 位指令；A 为 LR.W/SC.W +
  AMO*.W；F 为 RV32F，浮点单元在 `rtl/fp_unit.v`）。
- 一个自包含的 core 文件（`rtl/core.v`）；TCM 在内部。
- 简易的单 outstanding 总线，一个文件。
- 小而可读的外设：定时器、GPIO、UART、SPI。
- 确定性、可综合的 Verilog-2001（对 Yosys/OpenSTA 友好），不使用 SystemVerilog。
- 通过官方 `riscv-arch-test` RV32I/M/C 套件与项目自带的 A 定向测试。

## 2. 流水线

```
IF        ID             EX                 MEM              WB
PC/ITCM -> decode/regs -> ALU/branch/CSR -> TCM/bus access -> load align/reg write
```

流水线寄存器：

| 级 | 主要寄存器 |
|---|---|
| IF | `IF_PC`、`IF_PC_q`、`if_rdata_q`、`if_rdata_hi_q` |
| ID | `ID_PC`、`ID_IR`、`ID_valid`、`id_flush_q`、`IF_ID_*_hold` |
| EX | `EX_*`（PC/IR/valid/opcode/funct/rs/rd/imm/operands/control） |
| MEM | `MEM_*`（valid/rd/result/mem controls/addr/data/wstrb/load src） |
| WB | `WB_*`（valid/rd/write req/result/load info） |

每一级都有 valid 位。副作用（CSR 写、存储器请求、寄存器写）由相应 valid 位门控。

### 2.1 IF 与 ITCM 读取级

IF 每个周期读 ITCM 的**两个 word**：`IF_PC_q` 所在的 word（`if_rdata_q`）和下一个
word（`if_rdata_hi_q`）。ITCM 因此有 3 个读端口（两个 IF、一个由 EX 提供地址的
数据端口）；TCM 是推断阵列，换成 SRAM 宏时需要 3R1W 或等价的双周期方案。
`IF_PC_q` 是“正在送给 ID 的指令”的 PC（2 字节粒度），
`IF_PC` 是下一个取指地址（`IF_PC_q + 2/4`，或预测目标）。

`if_cur_half = IF_PC_q[1] ? if_rdata_q[31:16] : if_rdata_q[15:0]`：低两位是 `11` 时
是 32 位指令（lane 0 直接用 `if_rdata_q`；lane 1 用
`{if_rdata_hi_q[15:0], if_rdata_q[31:16]}`）；否则用 `rvc_expand()` 展开为规范 32 位
IR（Zca + Zcf：含 C.FLW/C.FSW/C.FLWSP/C.FSWSP；保留/不支持的编码展开为
`{16'b0, ci}`，由 ID 默认非法译码产生 cause 2，
`mtval` 为零扩展的半字）。所有压缩指令都以 2 字节前进，因此 lane-1 的 32 位指令
**不需要**装配气泡；第二个读端口只为它存在。

在 IF/ID 边界：

```
ID_IR <= if_insn_ir   (规范 32 位 IR)
ID_PC <= if_insn_pc   (= IF_PC_q)
ID_is_compressed <= if_insn_compressed
```

因此 `ID_PC` 是真实的 2 字节对齐 PC，而 `ID_IR` 永远是 32 位；ID/EX/MEM 对指令宽度
无感知，只有链接值（JAL/JALR 的 `EX_PC + (EX_is_compressed ? 2 : 4)`）、陈旧 BTB 项
的 `EX_PC + len` 和 `mepc` 需要真实长度。

`IF_PC` 在 `if_hold`（`pipe_freeze`、`raw_stall`、`bus_conflict`、`mem_amo_active`、
`id_flush_q`）时保持 `IF_PC_q`。重定向时跳到 `{redirect_pc[31:1], 1'b0}`
（目标所在 word），ITCM 读是 word 对齐的。

分支预测在 IF 级：64 项直接映射 BTB，索引 `IF_PC[6:1]`、tag `IF_PC[31:7]`（IALIGN=16
后 index 必须包含 PC[1] 并排除 PC[0]，同一 word 的两个 lane 各自有项），每项一个
2-bit 饱和方向计数器。命中且计数器 `>=2` 时预测 taken，下一 `IF_PC` 直接取 BTB
目标；否则顺序前进。预测元数据与 `IF_PC_q` 同步寄存为
`IF_pred_taken_q`/`IF_pred_target_q`（只在正常前进或重定向边沿更新，停顿期间保持），
经 `IF_ID_pred_*_hold`、`ID_pred_*` 传到 `EX_pred_*`。EX 用该指令自己的预测（不是
新查表）与真实结果比较，训练使用 `EX_PC`（指令 PC，不是取指 word）。

BTB 之外还有一个 **4 项返回地址栈（RAS）**，预测 `rd=x0`、`rs1=x1/x5`、立即数为 0
的 JALR 返回；RVC 已在 IF 展开，因此 `C.JR/C.JALR` 也走同一分类。call 压入
`EX_PC + 指令长度`，return 弹出，架构状态只在 EX 中真实执行且未陷阱时更新。IF 的
预测视图会组合应用 ID/EX 中更老 call/return 的效果，避免短函数的 return 读到旧栈顶。
RAS 只改变预测，错误目标仍由 EX 的同一重定向路径纠正；陷阱和 `mret` 清空 RAS。

### 2.2 为什么需要 `IF_ID_*_hold`

如果 ID 被保持，同时 `IF_PC` 也被保持，ITCM 输出会变化（它会重新读取保持的 `IF_PC`，
而该地址比 ID 指令*超前*一条）。如果没有 hold 寄存器，本应在释放周期进入 ID 的那条
指令就会丢失。

在任何 ID 不前进的周期（`raw_stall`、`pipe_freeze`、`bus_conflict`、`mem_amo_active`），
core 把待处理的 IF 指令（`if_insn_ir` / `if_insn_pc` / `if_insn_compressed`）捕获到 `IF_ID_IR_hold` / `IF_ID_PC_hold`，并置位
`IF_ID_hold`。当 ID 最终前进时，使用保存的这一对。这是正确性所必需的；删除它会导致
每次停顿后丢掉每隔一条指令。

### 2.3 重定向冲刷

重定向（`trap_taken`、`mret_taken` 或 taken 控制流跳转）时，IF/ID 侧由 `id_flush_q`
冲刷。

`id_flush_q` 在复位后也初始为置位。时序为：

1. 重定向边沿：`IF_PC_q <= target（位 0 清零）`，读取目标 word，`ID_valid <= 0`，
   `id_flush_q <= 1`。
2. 下一个边沿：`id_flush_q` 保持 `IF_PC`（目标 word 已呈现），ID 仍无效，并清除
   `id_flush_q`；若 BTB 目标在重定向边沿被训练，这一拍重新查表取到新的预测。
3. 再下一个边沿：ID 捕获目标指令。

这带来约 3 个周期的控制流跳转惩罚。不要删除 `id_flush_q`；否则会把过期的顺序执行
指令译码为目标。

分支预测正确（方向与目标都命中）时，EX 不产生 `redirect`，因此没有冲刷和惩罚；
预测错误时仍走上面的 `redirect`/`id_flush_q` 序列。陈旧 BTB 项命中非控制指令时，
EX 以 `EX_PC + 指令长度` 纠正。预测只影响取指，错误路径指令永远不会到达 MEM。

## 3. 译码

`ID_IR` 组合译码为：

- `id_rd_write_req`、`id_uses_rs1`、`id_uses_rs2`
- `id_mem_read`、`id_mem_write`、`id_mem_size`、`id_mem_unsigned`
- `id_branch`、`id_jump`、`id_jalr`
- `id_csr_read/write`、`id_csr_uimm`
- `id_illegal`、`id_ecall`、`id_ebreak`、`id_mret`、`id_fence`
- `id_imm`（按 opcode 选择 I/S/B/U/J）、`id_shamt`

`id_ir` 在 `ID_valid=0` 时强制为 `NOP`，因此气泡会被无害译码。ID 只看规范 32 位 IR，
RVC 展开在 IF 完成。

A 扩展（opcode `0101111`，funct3 必须为 `010`）：

- `id_amo` 覆盖 LR/SC/AMO，作为 load 类指令处理结果与相关性。
- `LR.W`：`id_is_lr`、`id_mem_read`；`rd=old`；`rs2` 必须为 0，否则非法。
- `SC.W`：`id_is_sc`、`id_mem_write`；`rd` 为 0（成功）/1（失败）。
- `AMO*.W`（funct5 00000/00001/00100/01000/01100/10000/10100/11000/11100）：
  `id_amo_rmw`、`id_mem_write`，`rd=old`。
- 其它 funct5（含 Zacas 的 AMOCAS.W）为非法。`aq`/`rl` 接受并忽略（单 hart、顺序
  执行，内存效果总是程序有序）。

CSR 合法性在 ID 中由 `csr_valid()` / `csr_writable()` 检查：

- `csrrs/csrrc` 且 `rs1=x0` 时不写。
- `csrrsi/csrrci` 且 `uimm=0` 时不写。
- 对只读 CSR（`misa`、`mstatush`、`mip`、vendor/arch ID、计数器）的写为非法
  （例外：`mip` 可写但忽略，以避免软件陷入陷阱）。

## 4. 相关、前递与停顿

操作数读多路选择优先级：`x0` 硬连线 > EX 前递 > MEM 前递 > WB 旁路 > 寄存器堆。
EX 比 MEM 年轻，所以 EX 优先；同一周期有多个同名写者时取最年轻的值。

```
ex_fwd_ok       = EX_valid  and EX_rd_write_req and !EX_mem_read and !EX_amo
                  and !(ex_is_div and div_state != DIV_DONE)
mem_fwd_ok      = MEM_valid and MEM_rd_write_req and !MEM_mem_read and !MEM_amo
mem_load_fwd_ok = MEM_valid and MEM_rd_write_req and MEM_mem_read and !MEM_amo
                  and (MEM_load_src == ITCM or MEM_load_src == DTCM)
                  and !pipe_freeze and !mem_amo_active
```

普通 TCM load 的读地址在 EX 产生，数据在 EX->MEM 边沿捕获；MEM 因此可以直接对字节/
半字/word 做对齐与符号扩展并前递。若同拍 MEM 中更老的 store 写同一 word，捕获时按
`wstrb` 逐字节合并新数据，避免 store->load 读到旧值。WB 还保存本 load 的原始 word，
保证背靠背 load 不串用数据。除法结果在 `DIV_DONE` 前也是 0，显式排除。其余 ALU、
LUI/AUIPC、JAL/JALR link、CSR 旧值、mul 都可以从 EX 或 MEM 前递。

总线 load 仍只在 MEM->WB 边沿捕获 `bus_rdata_q`，AMO/LR/SC 的结果（旧内存值或 SC
状态）也保持原来的 MEM/WB 路径；这些生产者不能走普通 TCM load 的 MEM 前递。

只有不可前递的生产者才停顿：

```
id_dep_ex  = ID uses rsN and rsN == EX_rd and EX_rd_write_req and EX_rd != 0
             and (EX_mem_read or EX_amo or divider not DONE)
id_dep_mem = ID uses rsN and rsN == MEM_rd and MEM_rd_write_req and MEM_rd != 0
             and ((MEM_mem_read and not mem_load_fwd_ok) or MEM_amo)
raw_stall  = id_dep_ex | id_dep_mem
```

在 `raw_stall` 时：

- IF/ID 保持（借助 `IF_ID_*_hold`），
- EX 填入气泡，
- MEM/WB 继续。

结果是：ALU 相关 0 停顿；普通 TCM load-use 在 distance 0 只停 1 拍，distance 1 起由
MEM 前递或 WB 旁路覆盖；总线 load 和 AMO/LR/SC 仍是 2 拍。div 在 `DIV_DONE` 后可
直接前递。注意 `raw_stall` 是电平信号：消费者在 ID 等待未完成除法时，它在整个
`div_busy` 期间保持高，所以统计 load-use 停顿时要减去/排除 `div_busy` 周期。

`x0` 是硬连线的：`id_rs1_data` / `id_rs2_data` 在最外层对寄存器 0 返回 0，
先于所有前递/旁路。不要把 x0 判断移到后面；否则对 `rd=0` 的写可能泄漏到 x0 的读。

## 5. 执行

### 5.1 ALU

`ex_alu_result` 实现 LUI/AUIPC、所有 OP-IMM 和 OP 运算。算术右移使用显式掩码形式：

```
(ex_rs1 >> shamt) | ({32{ex_rs1[31]}} & ~(32'hffffffff >> shamt))
```

这样可避免条件表达式中的符号性意外。

### 5.2 分支/跳转

- 分支比较按需使用 `$signed` / 无符号。
- 目标：JAL/分支为 `PC + imm`；JALR 为 `(rs1 + imm) & ~1`（只清 bit 0）。
- IALIGN=16：分支/JAL 偏移是偶数、JALR 清 bit 0，所以 `ex_target[0]` 永远为 0，
  cause 0 在架构上不可达；`ex_target_misaligned` 只检查 bit 0，用于防御性检查。
- JAL/JALR 链接值为 `EX_PC + 指令长度`（压缩指令为 `EX_PC + 2`）。

### 5.3 M 扩展

乘法为单周期：

```
mul_ss = signed64(rs1) * signed64(rs2)
mul_su = signed64(rs1) * signed64(zero_ext(rs2))
mul_uu = zero_ext(rs1) * zero_ext(rs2)
```

- `mul` 使用 `mul_ss[31:0]`（所有符号性下低 32 位相同）。
- `mulh/mulhsu/mulhu` 使用高半部分。

除法是 `div_state` 中的 32 步迭代恢复除法器：

- IDLE 捕获操作数、符号、除零/溢出标志。
- BUSY 每周期移一位并相减。
- DONE 输出 `div_result_comb`；随后流水线继续。
- `div_busy` 在 IDLE-start 和 BUSY 期间冻结整个流水线。
- 结果选择是 `EX_funct3[1]`（0 = div/divu，1 = rem/remu）。使用 bit 0 是 bug：
  会交换 divu/rem。
- 有符号特殊情况：除零 -> 商全 1，余数 = 原 rs1；`INT_MIN / -1` -> 商
  `0x8000_0000`，余数 0。

### 5.4 存储器地址/数据

- Load 地址：`rs1 + imm_i`；store 地址：`rs1 + imm_s`。
- Store 对齐到 word lane，并带有 `EX_wstrb`：
  - `sb`：一个字节，移位到选定 lane。
  - `sh`：两个字节，由 `addr[1]` 选择。
  - `sw`：整个 word。
- Load 携带 `EX_load_src`（0 无，1 ITCM，2 DTCM，3 总线）、大小、无符号标志。
- 非对齐在 EX 产生陷阱（load/LR cause 4，store/AMO/SC cause 6，tval=address）。
- 字节/半字 store 使用 strobe；AMO 的读改写只发生在 TCM（见 §6）。
- AMO/LR/SC 访问 tag `>= 2`（总线/MMIO）时在 EX 直接产生访问错误（LR cause 5，
  AMO/SC cause 7，tval=address），绝不驱动总线：简单总线没有原子事务，MMIO 读
  可能有副作用，不能伪装成非原子的读+写。


### 5.5 RV32F 浮点单元

`misa` 的 F 位置 1（`0x4000_1125`）。浮点状态包括 32 个 32 位 FP 寄存器
（`f0` 是普通寄存器，不是硬连线的零）、`fcsr`（`0x003`：`frm[7:5]`、`fflags[4:0]`）
及其别名 `frm`（`0x002`）与 `fflags`（`0x001`），以及 `mstatus.FS[14:13]`。

- 复位时 `mstatus.FS=00`（Off）；FS=Off 时任何浮点指令或 fcsr/ffrm/fflags 访问都在
  EX 产生非法指令（cause 2）。执行任何浮点指令把 FS 置为 Dirty（11）；写 fcsr/frm/
  fflags 也置 Dirty。陷阱和 `mret` 不改变 FS。
- `fcsr`/`frm` 写入保留的 `frm=5..7` 是非法指令；rm 字段 101/110 保留，111（DYN）
  使用 `frm`。
- 浮点寄存器文件没有 EX/MEM 前递。FP 写者在 EX 或 MEM 时，ID 中的 FP 读者停顿到
  写者进入 WB，再从 WB 旁路取数；FP→整数结果按普通整数结果前递，整数→FP 源
  （`FCVT.S.W/WU`、`FMV.W.X`）按普通整数 load/前递规则处理。
- `FLW`/`FSW` 使用 I/S 型立即数，经普通 MEM/TCM/总线路径；非对齐分别产生 cause 4/6。
- 浮点算术在 `rtl/fp_unit.v` 中分四级执行：A 解包/规格化，B 特殊值判断与算术核心，
  C（FMA 的加/规格化、除法/开方的迭代），R 舍入/打包。core 用一次 `fp_start` 脉冲
  启动它，`fp_busy` 期间冻结 IF/ID/EX（与除法器相同），`fp_done` 有效那拍退休；结果
  和 fflags 在 done 期间保持，直到下一条慢速浮点指令。所有 OP-FP 和 FMA 指令都走这条
  路径；只有 FLW/FSW 仍在 MEM 完成。
- 舍入模式支持 RNE/RTZ/RDN/RUP/RMM。NaN 结果统一为规范 qNaN `0x7fc00000`；
  信号 NaN 或非法操作置 NV；`FSGNJ`/`FMV`/`FCLASS` 不置标志。`FMIN`/`FMAX`
  按 RISC-V 规则返回非 NaN 操作数（双 NaN 返回规范 NaN）。FMA 为单次舍入的融合乘加，
  `FDIV`/`FSQRT` 为迭代精确实现（56/32 次迭代），fflags 在指令退休时只累积一次。
  延迟：普通 OP-FP 约 3 拍，FMA 约 4 拍，FDIV/FSQRT 约 60/35 拍；期间 EX 冻结，
  但整数流水线在浮点指令之后才恢复（单发射、按序）。

## 6. MEM

### 6.1 TCM

- ITCM 有三个读端口：两个取指 word（`IF_PC`），以及一个由 `EX_addr` 提供地址、
  在 MEM 使用数据的数据端口。
- DTCM 数据读端口同样由 `EX_addr` 提供地址。
- 读输出是寄存的（`if_rdata_q`、`mem_itcm_rdata_q`、`mem_dtcm_rdata_q`）。普通 TCM
  load 在 EX->MEM 边沿捕获数据；同拍更老 MEM store 写同 word 时按字节合并。
- 写按字节：`if (mem_wstrb_eff[i]) mem[index][i*8 +: 8] <= mem_wdata_eff[...]`。
- TCM 写由 `!pipe_freeze` 门控，因此冻结的 MEM 级不会重复写入同一 store。
- 可选 `ITCM_INIT_FILE` 用于 FPGA 初始化驻留 Boot 镜像：非空时先把 ITCM 清 0 再装载，
  默认空字符串仍由 testbench 加载。可选 `ITCM_BOOT_BYTES` 忽略低地址 Boot 区普通
  store/AMO 写，默认 0 不保护；保护区内的 SC 在 EX 直接返回失败，不能假装提交。该写
  门控同时作用于同拍 store/load 合并，被拒绝的写不能经 MEM 前递变得可见。完整 UART
  Boot 布局见 `docs/fpga_boot.md`。

### 6.1.1 RV32A：两周期读改写与 LR/SC 保留

AMO 在 MEM 占两拍：相位 0 读（`mem_amo_old` 取 `mem_itcm_rdata_q`/`mem_dtcm_rdata_q`），
相位 1 用 `mem_wdata_eff = mem_amo_new`、`mem_wstrb_eff = 4'hf` 写一次，随后插入一个
MEM 气泡；WB 在相位 1 的边沿寄存旧值与 `MEM_rd`。相位转换和写都由 `!pipe_freeze`
门控，所以除法/总线冻结只会推迟、不会重复写；`instret` 只在相位 1 计数一次。
`mem_amo_active` 同时保持 IF/ID/EX，并暂停 `csr_we`、`mret_taken`、预测纠错/训练。

LR 是普通 load，在 MEM 提交时设置保留：`lr_resv_valid`/`lr_resv_addr`（4 字节粒度）。
任何 SC（成功或失败）清除保留；对保留 word 的已提交 store/AMO 写清除它；任何陷阱
保守地清除它（规范允许的伪失败）。SC 在 EX 用组合逻辑判断成功：

```
resv_eff_valid = mem_commit_sc ? 0 : mem_commit_lr ? 1 : lr_resv_valid
resv_eff_addr  = mem_commit_lr ? {MEM_addr[31:2],2'b00} : lr_resv_addr
ex_sc_conflict = mem_commit_store && ({MEM_addr[31:2],2'b00} == {EX_addr[31:2],2'b00})
ex_sc_ok       = resv_eff_valid && (resv_eff_addr == word(EX_addr)) && !ex_sc_conflict
```

覆盖三个危险的流水线重叠：MEM 中刚提交的 LR 必须对 EX 中的 SC 可见；与 SC 同拍
提交的同 word store/AMO 必须让它失败；MEM 中更老的 SC 会消费保留。SC 失败不写内存。

TCM 是推断阵列。ASIC/FPGA 签核时请替换为 SRAM 宏或 block RAM；见
`docs/toolchain.md`。

### 6.2 总线

```
bus_req_o, bus_we_o, bus_addr_o, bus_wdata_o, bus_wstrb_o -> bus
bus_rdata_i, bus_ready_i <- bus
```

- 当 `MEM_valid` 且地址 tag `>= 0x2` 时，总线访问有效。
- 总线地址按 word 对齐（`{addr[31:2],2'b00}`）。
- 当 `bus_ready_i` 为高时，core 在 MEM->WB 边沿把总线读数据捕获到 `bus_rdata_q`。
- 如果从设备拉低 ready，`bus_wait` 会冻结流水线。
- **`bus_conflict`**：如果当前 MEM 和下一个 EX 指令都是总线访问，core 在 EX 中保持
  较年轻的那条，并在 MEM 插入一个气泡。这样 `bus_req_o` 会下降一个周期，使总线基于
  边沿检测的事务逻辑为每次访问看到新的请求。没有它，连续总线访问可能合并为一个事务，
  第二次访问丢失。

总线（`rtl/bus.v`）在 `req && !req_q` 时启动事务，发出单周期 `we`/`rd` strobe，并在
`req` 保持高时保持读数据。这就是为什么总线前的 core 冻结（例如迭代除法）不会重复
UART/FIFO 副作用。

## 7. WB

- `WB_rd_write_data` 选择 load lane/符号扩展或 `WB_result`。
- 寄存器写：`if (WB_valid && WB_rd_write_req && WB_rd != 0)`。
- `instret` 在有效指令离开 MEM 时递增（`!pipe_freeze && MEM_valid`，AMO 的相位 0
  除外，所以一条 AMO 只计一次）。
- `cycle` 每个时钟递增。

## 8. CSR 与陷阱

CSR：`mstatus`、`misa`（0x4000_1125：I/M/A/C/F）、`mie`、`mtvec`、`mstatush`（0）、`mscratch`、
`mepc`、`mcause`、`mtval`、`mip`、`mvendorid/marchid/mimpid/mhartid`、
`cycle/time/instret`（低/高），以及浮点 `fcsr`（0x003）、`frm`（0x002）、
`fflags`（0x001）。`mstatus.FS` 为 Off 时浮点指令和这三个 CSR 的访问都产生非法指令；
FS 语义见 §5.5。

`mip` 由电平 IRQ 输入组合而成：

- bit 3：软件中断
- bit 7：定时器
- bit 11：外部中断

中断优先级：MEI > MSI > MTI。仅当 `mstatus.MIE=1`、`mip&mie != 0`、`EX_valid=1`，且
流水线未被除法或总线等待冻结时才接受。`mepc` 是 EX 中指令的 PC，因此被中断的指令在
`mret` 后重新执行。

异常来源（在 EX）：

- 非法指令（cause 2，tval=instruction；压缩非法指令的 tval 是零扩展的半字）
- ecall（11）、ebreak（3，tval=PC，规范允许且 riscv-arch-test 参考签名使用）
- load/LR 非对齐（4），store/AMO/SC 非对齐（6），tval=address
- LR/SC/AMO 访问总线/MMIO（LR 5，AMO/SC 7，tval=address）
- 指令地址非对齐（0）在 IALIGN=16 下不可达（保留检查）

陷阱进入：

```
mepc    <= EX_PC & ~1      (mepc 只清 bit 0；CSR 写 mepc 同样只清 bit 0)
mcause  <= cause
mtval   <= tval
mstatus.MPIE <= mstatus.MIE
mstatus.MIE  <= 0
mstatus.MPP  <= 3
```

`mret`：

```
mstatus.MIE  <= mstatus.MPIE
mstatus.MPIE <= 1
mstatus.MPP  <= 3
PC <= mepc
```

`mtvec` 支持直接模式和向量中断（`mtvec[1:0]==1`）。

陷阱会取消 EX 指令（`MEM_valid <= 0`），而更老的 MEM/WB 指令正常完成。不要让产生陷阱
的指令在同一周期写 CSR 或存储器：`csr_we` 由 `!trap_taken` 门控。`csr_we` 同时由
`!pipe_freeze` 门控：慢速总线或除法冻结 EX 时，CSR 指令会停留多拍，只能提交一次。

## 9. 不变量 / 易错点

1. `IF_ID_IR_hold`、`IF_ID_PC_hold`、`IF_ID_hold` 是必需的；删除它们会在停顿后丢指令。
2. 每次重定向后都需要 `id_flush_q`；删除它会执行过期的顺序执行指令。
3. 在操作数读取多路选择中，x0 的判断先于 WB 旁路。
4. 除法结果选择是 `EX_funct3[1]`，不是 bit 0。
5. 定时器 W1C 是 bit 2；使能状态下清 pending 用 CTRL=0x0f（不是 0x0b）。
6. `bus_conflict` 必须保留：它把连续总线事务分开。
7. TCM 写由 `!pipe_freeze` 门控；不要让它们在除法冻结期间变成电平敏感。
8. 陷阱写和 CSR 写通过 `csr_we`/`trap_taken` 互斥；`csr_we` 还受 `!pipe_freeze`
   门控，冻结的 CSR 指令只提交一次。
9. `misa` 为 `0x4000_1125`（I/M/A/C/F）。CSR 支持属于 privileged spec 行为；软件使用
   `-march=rv32imacf_zicsr` 编译（`build_*.sh` 的 `MARCH` 可覆盖）。
10. ISA testbench 的 ITCM 是 4 MiB，因为 `jal-01` 使用了约 1.7 MiB 对齐填充。
    SoC ITCM 是 32 KiB。
11. 分支预测是纯性能特性：所有纠正都走唯一的 `redirect`/`redirect_pc`
    （优先级 trap > mret > 预测错误），错误路径指令永远到不了 EX/MEM。
12. `IF_ID_pred_*_hold` 必须保存寄存后的 `IF_pred_*_q`（与 `if_rdata_q` 配对），
    不能保存对 `IF_PC` 的实时查表结果（它比 ID 指令超前一条）。
13. 2-bit BTB 计数器必须饱和；`btb_valid` 和计数器要复位，tag/target 由 valid
    门控。陈旧项命中非控制指令时由 `EX_PC + 指令长度` 纠正。
14. 普通 TCM load 只有 distance-0 消费者停 1 拍，distance 1 起由 MEM 前递/WB 旁路
    覆盖；总线 load、AMO/LR/SC 保持原来的 2 拍停顿。x0 判断在所有前递/旁路之前。
15. RVC：`if_rdata_q` 是包含 `IF_PC_q` 的 word；`if_rdata_hi_q` 是其下一个 word；
    `IF_PC_q` 是呈现指令的 PC，`IF_PC` 是取指地址。压缩指令展开为规范 32 位 IR，
    非法压缩编码展开为 `{16'b0, ci}`。
16. `if_hold` 期间保持 `IF_PC_q` 与 `IF_pred_*_q`；`id_flush_q` 同样保持 IF，并在
    重定向后的那一拍重新查表（BTB 在重定向边沿被训练）。
17. 重定向取指地址是目标所在 word（`{redirect_pc[31:1],1'b0}`）；`IF_PC_q` 本身
    保留 bit 1。
18. 链接值与陈旧 BTB 纠正使用 `EX_PC + 指令长度`。
19. BTB 索引是 `PC[6:1]`、tag `PC[31:7]`；训练用 `EX_PC`。
20. AMO 在 MEM 占两拍、只写一次；相位转换与写由 `!pipe_freeze` 门控；`instret`
    只在相位 1 计数。
21. SC 成功/失败必须看到 MEM 中的 LR、同拍提交的同 word store/AMO 与更老的 SC
    （组合重叠逻辑）；保留粒度是一个 word，任何陷阱都会清除保留。
22. AMO/LR/SC 访问 tag `>=2` 在 EX 陷阱（5/7），绝不驱动总线。


23. 所有 OP-FP/FMA 指令都是慢速指令：`fp_start` 只在指令第一次进入 EX 时脉冲一次，
    `fp_done`/结果保持到下一次 start。不要把它们改回单周期组合输出，否则会重新引入
    EX→MEM 的超长路径（100 MHz 时序门禁会失败）。
24. FP 寄存器文件没有前递：EX/MEM 的 FP 写者通过 `id_fp_stall` 停住 ID，WB 旁路提供
    数据；`f0` 是可写寄存器，不能套用整数 x0 的规则。
25. `mstatus.FS` 复位为 Off；FS=Off 的浮点/fcsr 访问必须陷阱。只有 FP 指令执行或
    fcsr 写才把 FS 置 Dirty；陷阱/mret 不改 FS。
26. `fcsr`/`frm` 的保留模式写入是非法指令；rm=111 从 `frm` 取有效模式。
27. `FLW`/`FSW` 分别使用 I/S 立即数；它们的非对齐陷阱是 cause 4/6。
28. NaN 统一为规范 qNaN，NV/DZ/OF/UF/NX 的置位和 FMIN/FMAX/比较的 NaN 规则必须与
    RISC-V SoftFloat 一致（见 `rtl/fp_unit.v`）。
29. RAS 架构状态只在 EX 的真实 call/return 上更新；IF 预测视图必须组合应用 ID/EX
    中更老操作。陷阱/`mret` 清空 RAS，错误预测仍走统一 EX 重定向。
30. 普通 TCM load 的数据在 EX->MEM 边沿捕获；同拍更老 store 必须按字节合并，WB 保存
    本 load 的原始 word。不要把该 MEM 前递扩展到总线 load 或 AMO/LR/SC。
31. `ITCM_BOOT_BYTES` 的写保护必须同时门控真实数组写和同拍 store/load 合并；保护区
    内的 SC 必须返回失败，AMO 写必须被忽略。默认 0 才能保持普通 ISA/应用自修改 ITCM
    的行为。FPGA 初始化文件非空时先清 0 再装载，默认空不能取代 testbench 的显式
    `$readmemh`。

## 10. 复位

- `IF_PC_q = RESET_PC`（默认 0）。
- 所有 valid 位为 0；`id_flush_q=1`。
- TCM 和寄存器堆不复位（x0 读为 0；其他寄存器由软件初始化）。
- BTB 的 `btb_valid` 与方向计数器复位为 0；tag/target 不复位，由 valid 保证只在
  写入后才被使用。预测元数据寄存器复位为 0，RAS 计数器也复位为 0。
- CSR 复位为 0；`mepc`/`mtvec` 在陷阱前由软件写入。

## 11. 性能汇总

- ALU 操作：稳态 1 周期/指令；压缩指令同样 1 周期（IF 展开，无气泡）。
- ALU 相关：0 停顿（EX/MEM->ID 前递）。
- 普通 TCM load-use：distance 0 为 1 个停顿周期，distance 1 起由 MEM 前递/WB 旁路
  覆盖；总线 load、AMO/LR/SC 仍为 2 个停顿周期。
- 预测正确的 taken 分支/跳转：无冲刷惩罚；首次执行/预测错误约 3 周期。
- 4 项 RAS 预测常规 `x1`/`x5` 返回，短函数通过 ID/EX 视图前递避免旧栈顶。
- `mul`：1 周期。
- `div/rem`：约 33 周期，整个流水线冻结；结果在 DONE 后可前递。
- AMO：MEM 占 2 拍 + 1 个气泡（3 周期/条）；LR 同普通 load；SC 1 周期。
- 压缩代码：静态体积约减少 25–30%，动态指令数减少，取指每拍都能送出一条 16 位
  指令（32 位的 lane-1 指令也不再额外占拍）。
- CoreMark best（本机 GCC 13.2，RV32IMACF，benchmark 不含浮点）308.175（324,490 周期，
  3.08 CoreMark/MHz，100 MHz）；同编译器优化前为 281.687（355,003 周期）。Dhrystone best
  262,387（190,558 周期，1.493 DMIPS/MHz），同编译器优化前为 248,067（201,558 周期）。
  Dhrystone ground 的 GCC 14.2 历史参考为 135,660（368,567 周期，0.772 DMIPS/MHz）。
- UART/SPI/定时器为内存映射；无 DMA。
