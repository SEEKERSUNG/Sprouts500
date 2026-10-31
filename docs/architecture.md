# Sprouts500 架构

本文档描述 `rtl/core.v`、`rtl/bus.v` 及各外设的实际 RTL 实现，同时记录了后续开发者
需要遵守的约束：“不变量”一节列出不能破坏的非显然细节。

## 1. 目标

- 最简的五级顺序 RV32IM core。
- 一个自包含的 core 文件（`rtl/core.v`）；TCM 在内部。
- 简易的单 outstanding 总线，一个文件。
- 小而可读的外设：定时器、GPIO、UART、SPI。
- 确定性、可综合的 Verilog-2001（对 Yosys/OpenSTA 友好），不使用 SystemVerilog。
- 通过官方 `riscv-arch-test` RV32I/M 套件。

## 2. 流水线

```
IF        ID             EX                 MEM              WB
PC/ITCM -> decode/regs -> ALU/branch/CSR -> TCM/bus access -> load align/reg write
```

流水线寄存器：

| 级 | 主要寄存器 |
|---|---|
| IF | `IF_PC`、`IF_PC_q`、`if_rdata_q` |
| ID | `ID_PC`、`ID_IR`、`ID_valid`、`id_flush_q`、`IF_ID_*_hold` |
| EX | `EX_*`（PC/IR/valid/opcode/funct/rs/rd/imm/operands/control） |
| MEM | `MEM_*`（valid/rd/result/mem controls/addr/data/wstrb/load src） |
| WB | `WB_*`（valid/rd/write req/result/load info） |

每一级都有 valid 位。副作用（CSR 写、存储器请求、寄存器写）由相应 valid 位门控。

### 2.1 IF 与 ITCM 读取级

`IF_PC` 驱动 ITCM 取指读端口。ITCM 是同步的，所以地址 `A` 的指令会在 `A` 被驱动后的
一个周期出现在 `if_rdata_q`。`IF_PC_q` 与 `IF_PC` 同步寄存，对应当前 `if_rdata_q` 的
地址。在 IF/ID 边界：

```
ID_IR <= if_rdata_q
ID_PC <= IF_PC_q
```

因此 `ID_PC` 与 `ID_IR` 始终成对。

`IF_PC` 在前端停顿或流水线冻结时保持。遇到陷阱、`mret` 或 taken 分支/跳转时跳到
`redirect_pc`。

### 2.2 为什么需要 `IF_ID_*_hold`

如果 ID 被保持，同时 `IF_PC` 也被保持，ITCM 输出会变化（它会重新读取保持的 `IF_PC`，
而该地址比 ID 指令*超前*一条）。如果没有 hold 寄存器，本应在释放周期进入 ID 的那条
指令就会丢失。

在任何 ID 不前进的周期（`raw_stall`、`pipe_freeze`、`bus_conflict`），core 把待处理的
IF 指令（`if_rdata_q` / `IF_PC_q`）捕获到 `IF_ID_IR_hold` / `IF_ID_PC_hold`，并置位
`IF_ID_hold`。当 ID 最终前进时，使用保存的这一对。这是正确性所必需的；删除它会导致
每次停顿后丢掉每隔一条指令。

### 2.3 重定向冲刷

重定向（`trap_taken`、`mret_taken` 或 taken 控制流跳转）时，IF/ID 侧由 `id_flush_q`
冲刷。

`id_flush_q` 在复位后也初始为置位。时序为：

1. 重定向边沿：`IF_PC <= target`，`ID_valid <= 0`，`id_flush_q <= 1`。
2. 下一个边沿：目标读取已发出，但数据尚未出现在 `if_rdata_q`；`id_flush_q` 让 ID
   保持无效并清除自身。
3. 再下一个边沿：ID 捕获目标指令。

这带来约 3 个周期的控制流跳转惩罚。不要删除 `id_flush_q`；否则会把过期的顺序执行
指令译码为目标。

## 3. 译码

`ID_IR` 组合译码为：

- `id_rd_write_req`、`id_uses_rs1`、`id_uses_rs2`
- `id_mem_read`、`id_mem_write`、`id_mem_size`、`id_mem_unsigned`
- `id_branch`、`id_jump`、`id_jalr`
- `id_csr_read/write`、`id_csr_uimm`
- `id_illegal`、`id_ecall`、`id_ebreak`、`id_mret`、`id_fence`
- `id_imm`（按 opcode 选择 I/S/B/U/J）、`id_shamt`

`id_ir` 在 `ID_valid=0` 时强制为 `NOP`，因此气泡会被无害译码。

CSR 合法性在 ID 中由 `csr_valid()` / `csr_writable()` 检查：

- `csrrs/csrrc` 且 `rs1=x0` 时不写。
- `csrrsi/csrrci` 且 `uimm=0` 时不写。
- 对只读 CSR（`misa`、`mstatush`、`mip`、vendor/arch ID、计数器）的写为非法
  （例外：`mip` 可写但忽略，以避免软件陷入陷阱）。

## 4. 相关与停顿

无前递，只有 WB->ID 旁路。

```
id_dep_ex  = ID uses rsN and rsN == EX_rd  and EX_rd_write_req and EX_rd != 0
id_dep_mem = ID uses rsN and rsN == MEM_rd and MEM_rd_write_req and MEM_rd != 0
raw_stall  = id_dep_ex | id_dep_mem
```

在 `raw_stall` 时：

- IF/ID 保持（借助 `IF_ID_*_hold`），
- EX 填入气泡，
- MEM/WB 继续。

因此消费者会等待，直到生产者到达 WB 并可被旁路。Load-use 由同一规则覆盖（load 在 2 个
停顿周期后到达 WB）。

`x0` 是硬连线的：`id_rs1_data` / `id_rs2_data` 在 WB 旁路*之前*对寄存器 0 返回 0。
不要把 x0 判断移到旁路之后；否则对 `rd=0` 的写可能泄漏到 x0 的读。

## 5. 执行

### 5.1 ALU

`ex_alu_result` 实现 LUI/AUIPC、所有 OP-IMM 和 OP 运算。算术右移使用显式掩码形式：

```
(ex_rs1 >> shamt) | ({32{ex_rs1[31]}} & ~(32'hffffffff >> shamt))
```

这样可避免条件表达式中的符号性意外。

### 5.2 分支/跳转

- 分支比较按需使用 `$signed` / 无符号。
- 目标：JAL/分支为 `PC + imm`；JALR 为 `(rs1 + imm) & ~1`。
- taken 传输时 `ex_target_misaligned` 产生陷阱（cause 0，tval=target）。
- JAL/JALR 链接值为 `EX_PC + 4`。

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
- 非对齐在 EX 产生陷阱（load cause 4，store cause 6，tval=address）。
- 无 read-modify-write：字节/半字 store 使用 strobe。

## 6. MEM

### 6.1 TCM

- ITCM 有两个独立读端口：`IF_PC`（取指）和 `MEM_addr`（数据）。
- DTCM 有一个读端口（`MEM_addr`）。
- 读输出是寄存的（`if_rdata_q`、`mem_itcm_rdata_q`、`mem_dtcm_rdata_q`）。
- 写按字节：`if (MEM_wstrb[i]) mem[index][i*8 +: 8] <= MEM_wdata[...]`。
- TCM 写由 `!pipe_freeze` 门控，因此冻结的 MEM 级不会重复写入同一 store。

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
- `instret` 在有效指令离开 MEM 时递增（`!pipe_freeze && MEM_valid`）。
- `cycle` 每个时钟递增。

## 8. CSR 与陷阱

CSR：`mstatus`、`misa`（0x4000_1100）、`mie`、`mtvec`、`mstatush`（0）、`mscratch`、
`mepc`、`mcause`、`mtval`、`mip`、`mvendorid/marchid/mimpid/mhartid`、
`cycle/time/instret`（低/高）。

`mip` 由电平 IRQ 输入组合而成：

- bit 3：软件中断
- bit 7：定时器
- bit 11：外部中断

中断优先级：MEI > MSI > MTI。仅当 `mstatus.MIE=1`、`mip&mie != 0`、`EX_valid=1`，且
流水线未被除法或总线等待冻结时才接受。`mepc` 是 EX 中指令的 PC，因此被中断的指令在
`mret` 后重新执行。

异常来源（在 EX）：

- 非法指令（cause 2，tval=instruction）
- ecall（11）、ebreak（3）
- load/store 非对齐（4/6，tval=address）
- 指令地址非对齐（0，tval=target）

陷阱进入：

```
mepc    <= EX_PC & ~3
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
9. `misa` 只声明 I 和 M。CSR 支持属于 privileged spec 行为；demo 使用
   `-march=rv32im_zicsr` 编译。
10. ISA testbench 的 ITCM 是 4 MiB，因为 `jal-01` 使用了约 1.7 MiB 对齐填充。
    SoC ITCM 是 32 KiB。

## 10. 复位

- `IF_PC = RESET_PC`（默认 0），`IF_PC_q = RESET_PC`。
- 所有 valid 位为 0；`id_flush_q=1`。
- TCM 和寄存器堆不复位（x0 读为 0；其他寄存器由软件初始化）。
- CSR 复位为 0；`mepc`/`mtvec` 在陷阱前由软件写入。

## 11. 性能汇总

- ALU 操作：稳态 1 周期/指令。
- RAW 相关：2 个停顿周期（生产者 EX -> 消费者 EX）。
- taken 控制流跳转：含取指冲刷约 3 周期。
- `mul`：1 周期。
- `div/rem`：约 33 周期，整个流水线冻结。
- UART/SPI/定时器为内存映射；无 DMA。
