# FPGA UART Bootloader

本功能用于在 FPGA 比特流不变的情况下，通过现有 UART 重新下载用户程序。当前实现是
**易失性 ITCM 下载**：掉电后应用丢失，驻留 Bootloader 仍在比特流中；尚不包含外部
Flash 烧写。

## 1. 存储器布局与启动规则

| 区域 | 地址 | 大小/用途 |
|---|---|---|
| 受保护 Boot 代码 | `0x0000_0000` | 2 KiB，复位入口；CPU store/AMO 写被忽略 |
| 应用头 | `0x0000_0800` | 16 字节：magic、长度、入口、CRC32 |
| 应用二进制 | `0x0000_0810` | 最大 30,688 字节，到 `0x0000_7FF0` 前 |
| 仿真 mailbox | `0x0000_7FF8/0x0000_7FFC` | 保持现有应用约定 |
| DTCM | `0x1000_0000` | 16 KiB，Boot 和应用各自初始化自己的栈/`.bss` |

`fpga/soc_fpga_top.v` 把 `soc_top.ITCM_BOOT_BYTES` 设为 2048，并通过
`ITCM_INIT_FILE` 初始化 Boot 区。普通 `core`/`soc_top` 默认参数仍是 0 和空字符串，
所以现有 ISA、demo、app 和 benchmark 的地址布局不变。

**GPIO[0] 是低有效 BOOT0**：外部上拉，按钮接地。

- 复位时 BOOT0=0：无论应用是否有效，都进入 UART 下载器。
- 复位时 BOOT0=1：验证应用头和整镜像 CRC32，有效则跳到 `0x810`；无效则进入下载器。
- 跳转到应用使用 `mret`，保持 M-mode、关闭全局中断并清除 RAS；应用通过 `crt0`
  正常重新初始化运行环境。

Boot 区和应用共用 UART，但分时使用。应用启动后可以自由重新初始化 UART；要重新下载，
按住 BOOT0 并复位。

## 2. 构建与下载

```bash
# 生成 2 KiB、512 个 word 的 Boot 初始化镜像
bash scripts/build_boot.sh
# build/fpga/boot/{boot.elf,boot.bin,boot.hex}

# 用 Boot 应用链接脚本编译普通用户程序，并生成带 CRC 的 package
MARCH=rv32imafc_zicsr bash scripts/build_fpga_app.sh sw/example
# build/fpga/app/{app.elf,app.bin,app.hex,app.pkg}
```

命令显式使用 `MARCH=rv32imafc_zicsr`，与默认 `rv32imacf_zicsr` 是相同的 ISA 集合，
同时兼容对扩展顺序要求较严格的本机 GCC 13.2。也可通过 `MARCH` 覆盖其他目标配置；
Bootloader 自身的 `BOOT_MARCH` 默认已是规范顺序，不需要额外设置。

将 `fpga/soc_fpga_top.v` 作为 FPGA 顶层，连同 `rtl/*.v` 一起综合。默认初始化文件是
`build/fpga/boot/boot.hex`，路径相对于综合工具工作目录；也可以在顶层参数中传入绝对路径。
把时钟约束为 100 MHz，连接 UART TX/RX，并给 GPIO[0] 接上拉和 BOOT0 按钮。

先通过 JTAG 下载 FPGA 比特流，然后：

```bash
# 按住 BOOT0，按一下复位，再运行；--image 使用 raw bin，不是 app.pkg
python3 scripts/uart_boot.py --port /dev/ttyUSB0 \
    --image build/fpga/app/app.bin

# 只下载和校验，不启动
python3 scripts/uart_boot.py --port /dev/ttyUSB0 \
    --image build/fpga/app/app.bin --no-run
```

脚本只使用 Python 标准库和 Linux `termios`，默认 115200 8N1、无硬件流控。每包等待 ACK，
失败自动重试 3 次。若 Bootloader 已提前打印过 banner 也没关系，脚本用 `INFO` 命令同步，
不依赖读到 banner。

`app.pkg` 是 `16 字节头 + app.bin`，用于预载、打包和仿真；UART 下载脚本内部也会生成同样的
package。可单独执行：

```bash
python3 -I scripts/uart_boot.py --pack-only \
    --image build/fpga/app/app.bin --output build/fpga/app/app.pkg
```

## 3. 帧协议

所有多字节字段均为 little-endian：

```text
偏移   长度   内容
0      2      magic = 53 42 ("SB")
2      1      command
3      1      sequence（响应与请求相同）
4      4      address
8      2      payload length（0..256）
10     2      reserved，必须为 0
12     N      payload
12+N   4      CRC32，覆盖 byte 2 到 payload 末尾，不含 magic 和本字段
```

CRC 是常见的反射 CRC-32（polynomial `0xEDB88320`，初值/最终异或
`0xFFFFFFFF`），与 Python `zlib.crc32` 相同。

| Command | 名称 | 请求/响应 |
|---|---|---|
| `0x01` | INFO | 响应 20 字节：status、版本、应用头地址、入口、最大应用长度 |
| `0x02` | WRITE | 写 `[address, address+length)`；响应 4 字节 status |
| `0x03` | VERIFY | 响应 8 字节：status、设备计算的 CRC32 |
| `0x04` | RUN | 验证后响应 status，排空 UART TX，再跳应用 |

响应 command 是请求 command OR `0x80`。status：0 OK、1 CRC 错、2 地址越界、3 未知命令、
4 帧格式错、5 无有效应用、6 内部接收超时恢复（不作为空闲周期响应发送）。主机每次请求使用新 sequence，重试保持原帧不变，
因此重复 WRITE 是幂等的。

下载顺序为：

1. 把应用头 magic 写为 0，使旧应用失效；
2. 分块写应用字节；
3. 最后写完整应用头；
4. `VERIFY` 比较主机/设备 CRC32；
5. `RUN`。

Bootloader 拒绝任何低于 `0x800`、超过 mailbox 前边界或发生地址回绕的写入。硬件写保护
是第二道防线，即使已启动的应用尝试写 Boot 代码也不会生效。

## 4. 验证

```bash
MARCH=rv32imafc_zicsr bash scripts/test_boot.sh
# 或 make boot-test
```

该脚本运行：

- 主机打包/CRC/帧解析的 5 项标准库单元测试；
- Boot 和应用的真实编译及尺寸检查；
- `sim/tb_boot.v` 端到端仿真，通过真实 UART RTL 发送/接收所有帧；
- INFO、CRC 错误、越界写、未知命令、非 word 对齐分块、VERIFY、RUN；
- 下载后逐字节比较 ITCM，应用尝试写 Boot 区并确认被拒绝；
- 下载后应用 UART 输出及 mailbox；
- BOOT0 高时普通复位自动启动，BOOT0 低时强制返回下载器。

仿真只把 UART divider 从 867 强制改为 31 以缩短时间，不绕过任何协议或存储器写路径。
Boot 使用 16 项 nibble 表计算 CRC32；真实 115200 下每字节约有 8,680 个 core 时钟，
远高于接收/CRC 的处理时间。
日志为 `logs/boot/tb_boot.log`，期望 `BOOT_TEST_PASS`。

## 5. FPGA 集成限制

- 当前仍是推断 ITCM：两个 IF word 读端口加一个数据读端口，不是典型单端口 BRAM。
  上板需要 BRAM 复制、bank 或取指缓冲；若复制 BRAM，所有副本必须接收相同写数据。
- 当前提供的是板卡无关顶层和仿真验证，没有特定 FPGA 的引脚 XDC、比特流或上板结果。
- ITCM 下载易失。需要掉电保留时，后续在 Bootloader 中增加 QSPI Flash 镜像读写与启动。
- CRC32 用于传输检错，不提供身份认证或防止恶意镜像；UART 下载接口应视为物理调试接口。
