# 128 MAC UART 板级诊断入口

2026-10-24，分支 `fpga/npu128-uart`，父提交 `a906380`。已增加实际板卡引脚顶层、
UART→MMIO 桥和电脑端 Python 客户端。计算核和 MMIO 的七个 RTL 文件与父提交逐字节一致。
本入口用于单 tile 卷积通路的板级诊断。后续已生成位流、完成真实 JTAG 临时下载和回读；
CP2102 COM11 的三组卷积数值自检也已通过，最新记录见 [BOARD128.md](BOARD128.md)。

顶层直接使用板载 **27 MHz**，无 PLL，整个 UART/MMIO/NPU 使用同一时钟。
原 [RAM 优化计算核](RAM128.md) 的独立 **100 MHz** 时序基线仍然保留；
这里的 27 MHz 板级结果不能作为包含新外围的 100 MHz 验收。

## 板卡和复位

目标 MES2L676-200HP，器件 `PG2L200H-6IFBB676`。引脚来自本机参考资料中的
《MES2L676-200HP 硬件使用手册 v1.2》，已检查 PDF 第 13、34、40 页
（印刷页 10、31、37），SHA-256 为
`b045d4a3bb436a5ccd499d107081b4de0da26f84c19ff20bfe53e4c4d66d9e4c`。
这些是文档核对结果，尚未检查实际连接的板卡修订版。

| 顶层端口 | 实际管脚 | 用途 |
| --- | --- | --- |
| `clk_27m` | D18 | 27 MHz 单端晶振 U11 |
| `board_reset_n` | C22 | KEY0，低有效，板上拉高 |
| `uart_rxd` | B24 | CP2102 USB-C UART 到 FPGA |
| `uart_txd` | C24 | FPGA 到 CP2102 USB-C UART |

四个引脚均为 LVCMOS33、VCCIO 3.3 V。完整配置见
[uart-board.json](../config/uart-board.json) 和 [FDC](../constraints/npu_uart_board.fdc)。
不要将板载 DDR 的 125 MHz 差分时钟套用于该顶层。

**配置 FPGA 后先按下再松开 KEY0。** 复位异步置位、经三拍释放；不依赖 RTL 声明初始化。
KEY0 复位 UART、桥、去重缓存和 MMIO/NPU；MMIO CONTROL 的软复位只复位设备，保留串口桥缓存。
两者都不清空计算 RAM，开始任务前必须装载所需输入、权重和配置。

## 连接协议

115200 baud、8N1、低位先发、无流控。默认每 bit 234 个时钟，实际约 115384.615 baud，
相对请求值偏差 +0.160%。一次发送一帧，完整收到对应应答后才发送下一帧。
桥在执行总线请求和发送应答期间忽略 RX，不能连续灌入多帧。

请求固定 14 字节，所有多字节数值均为小端：

| 偏移 | 长度 | 内容 |
| --- | --- | --- |
| 0 | 2 | `A5 5A` |
| 2 | 1 | 版本 `01` |
| 3 | 1 | sequence，逐事务递增、模 256 |
| 4 | 1 | op：0 读、1 写、2 LINK_SYNC |
| 5 | 1 | 写字节使能，仅低四位有效 |
| 6 | 2 | MMIO 字节地址 |
| 8 | 4 | 写数据或 LINK_SYNC nonce |
| 12 | 2 | 对偏移 2–11 的 CRC16 |

读操作要求使能和数据为零；LINK_SYNC 要求使能和地址为零。寄存器布局与提交、POP、
错误和任务完成语义均沿用 [MMIO128.md](MMIO128.md)。

应答固定 12 字节：`5A A5 | 01 seq status 00 | dataLE32 | crcLE16`。
CRC 覆盖偏移 2–9。CRC 为 CCITT-FALSE，初值 `FFFF`、多项式 `1021`、不反射、
无最终异或；测试串 `123456789` 的 CRC 为 `29B1`。

| status | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | MMIO 总线错误，按 MMIO 文档读取 ERRORS |
| 2 | 版本、操作或字段格式错误 |
| 3 | 请求 CRC 错误，未发出 MMIO 请求 |
| 4 | sequence 与上一事务相同而请求内容不同 |

桥只缓存**最后一个已完成 MMIO 请求及其完整应答**，包括 MMIO 错误应答。
超时或应答损坏时，客户端使用相同 sequence 和相同整帧重试；重复 START/POP 直接返回缓存，
不会再次执行操作。Python 客户端默认最多重试两次。重试耗尽后，应视事务结果为未知，
先诊断链路和设备状态，不可换一个 sequence 盲目重发有副作用的命令。

LINK_SYNC 回显随机 nonce 并清空链路缓存，不复位 NPU。客户端连接后先执行 SYNC；
重新连接时仍须根据 STATUS 判断设备状态。单个串口仅允许一个有序客户端。
去重保证只适用于最后事务以及复位前的有序停等会话，不覆盖历史帧重放、掉电、KEY0 或并发客户端。
帧间字节超时为 100 ms；非法停止位或半帧超时丢弃当前帧，不执行 MMIO。

## 已有验证

[公开引脚 RTL 报告](../reports/npu128-uart-validation.json) 使用顶层四个公开引脚，
保持默认 234 分频和 2,700,000 拍超时；发送与接收两端分别实现 8N1 编解码。
2,777 次主机事务、3 个标量逐值对照卷积任务、12 个输出向量通过，累计 174,692,848 个时钟。
覆盖 1×1/3×3、8/9 输入通道、RAW/量化/LUT、尾通道毒值、INT32 回绕和两 bank。
9 类协议拒绝/恢复及 3 次安全重发通过，含丢失 START/POP 应答、损坏应答、错误 CRC、
半帧、错误停止位、短起始毛刺、KEY0 中途复位及主机 bit 周期 230/238 的偏差。
字节桥独立测试还覆盖总线延迟和 240 拍 TX 背压。严格 `-Wall` lint 通过。

[GTP 四态综合网表回放](../reports/npu128-uart-netlist-validation.json) 的 103 组完整帧应答一致。
包含高位地址拒绝、最高权重/偏置窗口、16 通道计算及量化、START/POP 去重、CRC 拒绝和错误状态。
这项测试在 UART 字节边界注入 RX valid/data/error，并将 PHY TX busy 固定为零，
不强制桥、MMIO、NPU、RAM/APM 或计算输出。UART 的串行位时序由前述完整引脚 RTL 仿真覆盖；
门级回放没有 SDF，也不表示板上电气验证。

原始日志和二进制位于忽略目录 `build/uart-test-a/`、`build/uart-gate-a/`。
报告绑定源码、测试、模型及网表 SHA-256。仿真运行时间不是板上推理延时，
三组小任务也不代表完整 YOLOX 的精度或性能。

## 实际布局布线结果

[联合物理验收报告](../reports/npu128-uart-implementation.json) 状态 PASS。
PDS 2025.2 ADS 在原生工程 `F:\pango-project\work\npu128-uart-20261024a` 完成
综合、映射、带模块预规划的布局、布线、hold 修复和最终 STA；未请求 bitstream。
实际时钟约束为 37.037037 ns，setup/hold uncertainty 为 0.2/0.05 ns。

| 角 | setup WNS | hold WHS | setup/hold 失败端点 |
| --- | --- | --- | --- |
| fast | +27.059 ns | +0.039 ns | 0 / 0 |
| slow | +20.614 ns | +0.055 ns | 0 / 0 |

未布通 0，最小脉宽检查通过，无时钟 SRB 路由警告。
无时钟、缺失/部分输入输出延迟、min/max 一致性、组合环和锁存器检查均为零；
`virtual_clock=1` 为该工具的提示项，不作为缺失约束处理。
最终 IO 和 pinout 两份报告独立核对四个实际管脚、方向、电压及标准，全部为已约束。

| 层级 | LUT | FF | APM | 等效 DRM36K |
| --- | ---: | ---: | ---: | ---: |
| 完整 UART 顶层 | 6,917 | 9,555 | 82 | 126.5 |
| 其中计算 NPU | 5,430 | 6,513 | 82 | 126.5 |
| 字节桥 | 315 | 441 | 0 | 0 |
| UART PHY | 45 | 56 | 0 | 0 |

层级资源存在包含关系，不能把各行再次相加。激活缓存仍为两 bank、每 bank 三段，
六段各映射为 16 个 8K×4 DRM36K；其余 RAM 使总数达到 126.5。
顶层物理映射实际使用 124 个 DRM36K 和 5 个 DRM18K。

FDC 仅对异步 UART RX 与 KEY0 原始端口设 false path。
综合网表连接审计确认 RX 只进入第一级同步 FF，再进入第二级；KEY0 只进入三个复位释放 FF
的异步 preset 脚。下游寄存器路径全部参与 STA。输入 0/1 ns、输出 0/1 ns 延迟是诊断顶层
覆盖约束，不代表异步 UART 的外部设备具有同步时序关系，也不能替代实际板上电气测试。

本次工具估算 Fmax 为 **60.8902 MHz**，尚未按该频率实施验收。
最差 setup 路径从 `u_device/weight_index_q[6]` 到激活 RAM 写使能，数据延迟 15.917 ns，
其中布线 14.122 ns（88.7%）。综合共享后的装载控制路径已成为新外围提频的重点；
要让完整顶层达到 100 MHz，应先把 MMIO 提交地址/使能锁存并分级，控制扇出，
再使用适当 PLL、重新验证分频、数值/握手和实际 100 MHz STA。

Windows guard 单线程、Job 上限 3 GiB、物理和提交余量各保留 6 GiB；
本次峰值 Job commit 2,093.5 MiB，guard 总耗时 1,696.253 秒，正常退出。
原生工程的 manifest、guard、IO、约束、网表、资源和各角时序报告均保留 SHA-256。

## 复现和电脑端入口

WSL 中执行功能测试（建议单任务构建，保留 6 GiB 系统余量）：

```bash
make uart-lint
make uart-test UART_BUILD=build/uart-recheck
```

使用已有完整引脚仿真服务复现主机自检：

```bash
python3 tools/npu_uart.py --simulator build/uart-test-a/board/Vnpu_uart_board --self-test
```

后续生成并下载该顶层的 bitstream、按 KEY0 且确认实际 COM 口后，在 Windows Python 中使用：

```powershell
python -m pip install pyserial
python tools/npu_uart.py --port COMx
python tools/npu_uart.py --port COMx --self-test
```

将 `COMx` 换成实际 CP2102 端口。第一条客户端命令执行链路 SYNC 并读取 ID/ABI/STATUS；
`--self-test` **会软复位诊断 NPU 并替换输入、权重和配置**，然后核对三组卷积和计数器。
当前尚未执行物理串口命令。Python 使用 `Client.read/write/configure/activation/weight/output`
即可通过相同寄存器 ABI 调用；C11 传输适配头保留在 `software/npu128_mmio.h`。

物理复现使用当前源码对应的功能 PASS 报告，先在 WSL 准备新工程：

```bash
python3 tools/pango/stage_uart.py --work-root /mnt/f/pango-project \
  --validation reports/npu128-uart-validation.json --name npu128-uart-recheck
```

随后在 PowerShell 运行该工程自带的保护脚本；路径按本机 PDS 安装位置调整。
本次 1,800 秒内完成，复现可将运行上限设为 3,600 秒以留出机器负载波动余量，内存保护保持不变。

```powershell
& F:\pango-project\work\npu128-uart-recheck\tools\pango\run_synthesis.ps1 `
  -ProjectRoot F:\pango-project\work\npu128-uart-recheck `
  -PdsHome F:\pango\PDS_2025.2-ads -JobLimitGiB 3 -ReserveGiB 6 -TimeoutSeconds 3600
```

该流程以单线程、3 GiB Job 限额、6 GiB 物理/提交余量运行。
字节网表回放入口为 `tools/pango/check_uart_netlist.py`；最终证据由
`tools/pango/collect_uart_implementation.py` 同时核对 RTL、mapped PASS、真实管脚和 STA。
所有重复尝试使用新目录，保留原始工程及 guard 日志。

后续位流、实际 JTAG 下载和串口待测状态见 [BOARD128.md](BOARD128.md)。
`config/uart-board.json` 中的位流/上板字段保留原 RTL/PDS 输入快照，最新状态以新位流和上板报告为准。
DDR、DMA、RISC-V/RT-Thread 接入、完整 YOLOX 静态量化和整图调度仍是后续工作。
