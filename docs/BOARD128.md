# 27 MHz UART 诊断上板记录

> 后续 `fpga/npu128-mmio-timing` 已完成真实 GPLL 100 MHz 整顶层布局布线、JTAG 下载/回读及
> COM11 三组卷积实测，最新结果见 [MMIO_TIMING128.md](MMIO_TIMING128.md)。
> 下文保留原 27 MHz 版本的板卡、驱动与实测记录。

2026-10-24，分支 `fpga/npu128-uart`，本轮从 `71a5d99` 接续。
**实际 JTAG 配置/回读及 CP2102 串口卷积自检均已通过。**
本轮没有修改 RTL、引脚或时钟约束，原 27 MHz 布线证据继续有效。
计算核独立 100 MHz 结果与完整外围的频率边界仍按 [UART128.md](UART128.md) 区分。

## 最新 UART 数值验收

更换数据线后，CP2102 已枚举；最初错误码 28 表示缺少驱动。经 Windows 管理员授权安装
微软目录提供的 Silicon Labs CP210x `11.5.0.417`，目录和 x64 驱动签名均有效，设备错误码归零。
实际端口为 **COM11**，USB VID:PID 为 `10c4:ea60`。驱动包来源与哈希见
[驱动安装记录](../reports/cp2102-driver-validation.json) 和
[Microsoft Update Catalog 条目](https://www.catalog.update.microsoft.com/ScopedViewInline.aspx?updateid=32293688-4758-4334-98c0-7d634cf3c59e)。

[真实 UART 验收报告](../reports/npu128-uart-hardware-validation.json) 状态 PASS：

| 项目 | 实测结果 |
| --- | --- |
| 链路 | CP2102 COM11，115200 baud、8N1 |
| 识别 | ID `0x4e505531`、ABI `0x10000`，测试前 STATUS=0 |
| 任务 | 三组：1×1 RAW、3×3 量化、3×3 LUT，输入通道 8/9，两个 bank |
| 输出 | 12 个向量，192 个 RAW 和 192 个 INT8 槽位逐值匹配标量参考 |
| 每任务 MAC 步计数 | 4 / 72 / 72，全部符合参考 |
| 实际事务 | 2,759 次，重传 0 |
| 主机端总耗时 | 74.891 秒，包含 USB/串口和软件开销，不能作为 NPU 计算延迟 |

每个向量同时检查有效通道/坐标、mode/last 标志；每任务确认 DONE、BUSY/OUTPUT_VALID/ERROR
和 ERRORS 寄存器的结束状态。数值覆盖 INT32 回绕、量化舍入/饱和、LUT 与尾通道屏蔽。
这些是三组小 tile 的实测，不是完整 YOLOX 推理或精度验收。
物理链路没有人为注入丢包；重试/异常覆盖仍由单独的 RTL 报告提供。

本轮使用既有已下载位流，未重新配置 FPGA、修改 RTL 或约束。
新脚本 [run_uart_board_test.py](../tools/run_uart_board_test.py) 会核对当前源码与功能报告，
仅接受指定的 CP2102 端口，调用原主机客户端和标量参考，并保存独立硬件 JSON。
报告引用此前同一配置的 JTAG 证据，本轮 UART 测试没有重新扫描 JTAG。
此前 [上板报告](../reports/npu128-uart-board.json) 中的 `UART_PENDING` 是当时的历史快照，保留原文件。
本轮原始识别输出、数值报告及驱动安装日志位于 `F:\pango-project\work\npu128-uart-serial-20261024a`。

## 已完成的实际硬件检查

| 项目 | 实测结果 |
| --- | --- |
| JTAG 链 | 一颗 PG2L200H |
| 器件 ID | `0x00603899` |
| 下载方式 | JTAG 临时配置，`cfg_set_device_property -program 1` |
| JTAG 时钟 | 5 MHz |
| 编程及回读 | `cfg_program=1`、`cfg_verify=1`，均成功 |
| 诊断 usercode | `0x12802701` |
| 配置状态寄存器 | `0x001ffca0` |
| DONE | bit 12 = 1 |
| 错误检查 | `status & 0x1800001f = 0`，含配置 CRC、ID、认证、回读 CRC、超时及过温/局部重配错误 |
| KEY0 | 用户已确认按下并松开；本轮 UART 识别读到 STATUS=0 |

这组 JTAG 结果验证 FPGA 配置下载和回读；卷积数值由上面的独立 UART 测试验收。
未写外部 Flash、eFuse 或密钥。断电后本临时配置消失，重新供电需再次下载。
实际日志和 SHA-256 见 [上板报告](../reports/npu128-uart-board.json)，
报告状态明确为 `JTAG_PASS_UART_PENDING`。

原生硬件工作目录：`F:\pango-project\work\npu128-uart-board-20261024a`。
`program-a/program.log` 保留全部编程、回读、usercode 和状态输出；
`program-a/program.tcl` 与 `program-receipt.json` 保留调用和进程限额。
本任务 JTAG Server 使用端口 65421；结束核对时该服务已经退出，未关闭其他用户进程。

## 已验收位流

[位流验收报告](../reports/npu128-uart-bitstream.json) 状态 PASS，实际配置如下：

| 项目 | 数值 |
| --- | --- |
| 顶层/时钟 | `npu_uart_board` / 27 MHz |
| 器件 | `PG2L200H-6IFBB676` |
| 位流大小 | 9,188,552 字节 |
| SHA-256 | `0c0e2650dae4eb0025fa02d78680cada8a0c8ca391b597b73bdc8d859c0d3c96` |
| SCBV | `High Voltage` |
| usercode | `12802701` |
| CRC | 开启 |
| 未用 IO | `NONE` |
| 加密 | 关闭 |

板卡 v1.2 手册 PDF 第 11 页（印刷页 8）确认 L3/L4/L5 由 3.3 V 供电。
SCBV 选择同时参考厂商 [PG2L200H FBB676 管脚说明](https://www.pangomicro.com/en/uploads/34165729_1729132019.pdf)
中配置电压与 SCBV 的对应关系；实际 PDS 设置和无 SCBV 关键告警均已经核对。
配置依据与手册哈希见 [uart-bitstream.json](../config/uart-bitstream.json)。

最终原生位流：

```text
F:\pango-project\work\npu128-uart-bitstream-20261024d\prj_tasks\pnr_1\generate_bitstream\npu_uart_board.sbit
```

项目内另有 `build/board-uart-27m/` 包，包含同一位流、两个报告、主机脚本和 `SHA256SUMS`。
该目录及原始 PDS 工程均为忽略产物，未提交 9 MB 二进制到 Git，换机器需重新生成或单独传递并核对哈希。

生成过程从原验收工程复制新快照，显式指定最终 `npu_uart_board_pnr.adf`：

```tcl
gen_bit_stream -force_to_run \
  -input_db_file {prj_tasks/pnr_1/place_route/npu_uart_board_pnr.adf} \
  -scbv {High Voltage} -usercode 12802701
```

`-force_to_run` 仅用于直接运行位流阶段，输入数据库、原物理报告、RTL 和所有约束都逐项核对 SHA-256。
本次没有重新综合或布线，也没有修改原 `npu128-uart-20261024a` 工程。
保护限额为 3 GiB Job、6 GiB 物理/提交余量；实际峰值 Job commit 2,341.1 MiB、耗时 107.638 秒。

生成尝试全部保留：`bitstream-20261024a` 参数互斥失败；`b` 因 PDS 意外启动依赖重跑而主动中止；
`c` 生成成功但 SCBV 未设置，拒绝用于下载；最终仅下载 `d`。原始日志和停止记录哈希写入上板报告。

## 串口接线排查记录（已解决）

上一阶段 Windows PnP 与 pyserial 只检测到 FTDI `0403:6010` 的 COM9/COM10，以及蓝牙串口。
当时未枚举到 CP2102，也没有打开这些无关串口。后来更换数据线，CP2102 出现错误码 28，
安装上述已签名 VCP 驱动后成为 COM11。COM9/COM10 仍属于下载器。

pyserial 3.5 已隔离安装在原生工作目录，未修改全局 Python。
本次下载的 wheel SHA-256 为
`c4451db6ba391ca6ca299fb3ec7bae67a5c55dde170964c7a14ceefec02f2cf0`。

同一台机器可在 PowerShell 中执行（将 `COMx` 换成新出现的 CP2102 端口）：

```powershell
$env:PYTHONPATH='F:\pango-project\work\npu128-uart-board-20261024a\python-deps\site'
$uartPython='C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $uartPython -X utf8 -m serial.tools.list_ports -v
& $uartPython -X utf8 F:\pango-project\work\npu128-uart-board-20261024a\host\npu_uart.py --port COMx
& $uartPython -X utf8 F:\pango-project\work\npu128-uart-board-20261024a\host\npu_uart.py --port COMx --self-test
```

在仓库根目录运行以下命令可保存新的、绑定源码和前次 JTAG 证据的硬件报告，输出文件必须尚不存在：

```powershell
& $uartPython -X utf8 tools/run_uart_board_test.py --port COM11 --output build/uart-hardware-recheck.json
```

保持 FPGA 本次配置未掉电，配置后先按 KEY0。识别预期 ID `0x4e505531`、ABI `0x10000`。
`--self-test` 会软复位诊断 NPU 并替换测试数据/配置，逐值检查三组卷积；
必须保留实际串口输出，不能用现有 RTL 仿真结果代替上板数值验收。

## 复现生成和 JTAG 下载

在 WSL 用新的目标目录准备快照，再于 PowerShell 运行快照自带脚本：

```bash
python3 tools/pango/stage_bitstream.py \
  --project /mnt/f/pango-project/work/npu128-uart-20261024a \
  --implementation reports/npu128-uart-implementation.json \
  --target /mnt/f/pango-project/work/npu128-uart-bitstream-recheck
```

```powershell
& F:\pango-project\work\npu128-uart-bitstream-recheck\bitstream-input\run_bitstream.ps1 `
  -ProjectRoot F:\pango-project\work\npu128-uart-bitstream-recheck `
  -PdsHome F:\pango\PDS_2025.2-ads
```

随后运行 `tools/pango/collect_uart_bitstream.py --help`，生成新的验收报告。
JTAG 自动脚本为 `tools/pango/program_uart_board.ps1`：需要实际位流、对应 PASS 报告、
PDS 路径、新的原生运行目录，以及本任务 JTAG Server 端口。
脚本会在同一次连接内拒绝多器件链和非 `00603899` 的 ID，再进行临时下载与回读。
Windows RemoteSigned 策略不接受直接执行 UNC 上的未签名脚本时，把该脚本和
`GuardedJob.cs` 复制到新的原生目录执行；不需要更改系统执行策略。
服务启动使用 `Start-Process ... -WindowStyle Hidden`，下载器/串口选择按实际枚举结果核对。

下一步给 MMIO 装载控制增加流水、降低控制扇出，重新评估包含外围的 100 MHz。
当前完整顶层仍是已验收的 27 MHz，JTAG/UART 成功不改变其频率结论。
