# 128 MAC 板级 100 MHz 时序优化

> 最新权重批量桥的 100 MHz 时序及实板数值/吞吐见 [UART_BULK128.md](UART_BULK128.md)，本页保留原硬件基线。

> 后续主机收包优化与同位流实测见 [UART_HOST128.md](UART_HOST128.md)：完整三组卷积
> 77.672 → 18.828 秒，2,759 笔零重传。本页保留 100 MHz 硬件阶段的独立证据。

分支 `fpga/npu128-mmio-timing` 从已实板通过的 `eac44da` 接续。目标是保留
128 MAC、82 APM 和 126.5 DRM 的计算核，把包含 UART/MMIO 的完整板级入口提升到
真实 100 MHz。六个计算及后处理 RTL 文件保持 RAM 压缩基线的字节内容。

27 MHz 的历史实板 PASS 见 [BOARD128.md](BOARD128.md)。它不证明本次新 RTL 或
100 MHz 位流已通过。本阶段物理实现、新位流临时下载/JTAG 回读以及实际 UART 三组卷积
均已通过，物理与实板结果分别保存独立证据。

## MMIO 分级

原板级最差路径从 `weight_index_q[6]` 经过共享访问错误判断，到激活 RAM 写使能，
15.917 ns 中约 88.7% 是布线。现在请求先锁存，再解码；合法核心动作由单独的
寄存有效脉冲提交。索引检查与 RAM 写使能不再是同一条组合路径。

本地总线仍使用原地址和 ABI 1.0，但主机必须遵守 ready/valid，不得依赖旧的单拍应答：

- 握手沿 H 锁存请求，之后公开请求引脚可立即改变。
- H+1 执行寄存器读写、POP、错误检查；普通请求在该沿产生响应。
- 激活、权重、配置、START、SOFT_RESET 在 H+2 被核心消费，随后才产生成功响应。
- EXECUTE/ISSUE 期间不接受下一请求。暂存数据、索引和描述符保持到消费沿，
  不需额外复制 1024 bit 权重或 512 bit bias。
- 可以在消费上一响应的同一沿接受下一请求；吞吐不再是连续每拍一笔。
- 读快照取自 H+1 执行沿。受阻响应内容不变。外部复位取消已接受但未应答的请求；
  软件复位保留自己的应答，且在应答前完成设备复位。

[最终 MMIO 回归](../reports/npu128-mmio-timing-final-validation.json) 已通过：
21 个任务、674 个独立标量核对向量、268,919 次公开总线事务、403,344 拍响应停顿，
16 次非法访问和三次复位恢复。新增连续排队的 COMMIT 后索引/数据覆盖、COMMIT 后立即
SOFT_RESET、START 后描述符/bias 覆盖，以及已接受未执行请求被硬复位取消的数值检查。
中间 19 任务报告和首次仿真失败目录作为历史记录保留。

## 真实 PLL 和复位

新顶层 `npu_uart_board100` 使用原 D18 的 27 MHz 晶振，实例化真实 `GTP_GPLL`。
PFD=27 MHz，反馈除数 25、VCO=675 MHz，输出除数 6.75，输出 100 MHz；
输出分频为 6+6/8，高低半周期对称。KEY0 或 PLL 未锁定时异步保持系统复位，
锁定后经过现有三拍同步释放。UART 使用每 bit 868 拍和 10,000,000 拍帧间超时。
引脚及电压沿用已核对的板卡配置，见 [uart-board100.json](../config/uart-board100.json)。

[PLL 整顶层测试](../reports/npu128-clock100-functional.json) 使用未修改的本机厂商模型，
定时 Verilator 检查两次锁定间合计 200 个 10 ns 周期及 5 ns 高电平，并检查锁定门控、
三拍复位释放、KEY0 复位与重新锁定。二态仿真显式启用初始异步复位沿。
Icarus 11 在同一厂商模型中发生零时间循环，失败日志保留；不能将此报告称为四态 PLL 验证。

## 数字通路与网表验证

[100 MHz 引脚回归](../reports/npu128-uart100-functional.json) 通过三组标量卷积、
2,777 次主机事务、9 类协议拒绝/恢复、三次安全重传，使用每 bit 868 拍和完整帧间超时，
实际运行 647,781,951 个数字时钟；串口波形没有加速旁路。首次 100 MHz 构建因测试服务端
仍输出旧 27 MHz 握手参数而失败；改为按参数输出后重跑，旧目录 `build/uart100-timing-a` 保留。

[映射后回放](../reports/npu128-uart100-netlist-validation.json) 对本次原生 PDS 生成的精确
GTP 网表执行 103 帧、3,936 拍四态测试，卷积与独立标量参考一致。该项明确旁路 UART 位收发
及 PLL 时钟生成，只在对应边界注入字节、理想 100 MHz 和 LOCK；没有强制 MMIO、核心、
RAM/APM、状态或数值结果。实际 PLL 由前述未修改厂商模型单独验证，串口位时序由引脚回归验证。
网表回放还检查 LOCK 丢失后异步复位及三拍重新释放，无 SDF 延迟仿真。

## 物理实现与位流

[完整物理报告](../reports/npu128-uart100-implementation.json) 已通过。
原生目录为 `npu128-uart100-20261024a`，单线程、3 GiB Job 上限、物理及提交内存
均保留 6 GiB；约 1,064 秒完成，峰值作业提交内存 2,102.2 MiB。
对综合网表展开真实层次端口后审计，RX 只到第一个同步 FF，KEY0 只到 GPLL RST 和三个复位释放
FF 的异步引脚，LOCK 也只到同三个异步引脚。所有后续寄存器路径均保留时序检查。

| 项目 | 原 UART 27 MHz 顶层 | 本次真实 PLL 100 MHz 顶层 |
| --- | --- | --- |
| LUT / FF | 6,917 / 9,555 | 6,946 / 9,625 |
| APM / DRM36K | 82 / 126.5 | 82 / 126.5 |
| 最差 setup | +20.614 ns（37.037 ns 周期） | +0.805 ns（10 ns 周期） |
| 最差 hold | +0.039 ns | +0.039 ns |
| 未布通网络 | 0 | 0 |
| 工具估算 Fmax | 60.8902 MHz | 108.7557 MHz |

fast/slow setup 分别 +3.708/+0.805 ns，hold +0.039/+0.066 ns，违规端点均为零；
最小脉宽通过，时钟、输入输出延迟和环路等覆盖检查通过。实际工作目标是 100 MHz，
108.7557 MHz 只是该约束下工具估算，不代表更高 PLL 频率已验收。
最差 setup 已转移到激活 RAM 读口至 APM 输入，不再是原共享 MMIO 错误链。

[新位流报告](../reports/npu128-uart100-bitstream.json) 绑定该次完整布线数据库，
原生快照 `npu128-uart100-bitstream-20261024a`，生成时没有重跑综合/布局布线。
SCBV=High Voltage（3.3 V），usercode=`12810001`，9,188,552 字节，SHA-256：
`f5d339f84b8dba346469d080c98673b471abd58c664c85521bd5a7d7b1cdd468`。

实际 JTAG 单器件 ID=`00603899`、5 MHz 临时配置、回读均通过，
usercode=`12810001`、配置状态=`001ffca0`，DONE=1、错误掩码 `1800001f` 下为零。
板卡记录在 `npu128-uart100-board-20261024a`；用户随后确认已按 KEY0，供电和 USB-UART 保持正常。
[JTAG 报告](../reports/npu128-uart100-board.json) 的状态为 `JTAG_PASS_UART_PENDING`，
该快照产生于本次 KEY0 确认之前，作为历史记录保留；当前串口结果见下一节。
本任务 JTAG Server 已关闭，后续仅运行 UART 自检不需要重新启动它。

仅有 PLL 周期测试不等于物理时序收敛；FDC 必须保留真实 27 MHz 输入及 100 MHz 生成时钟，
不能把物理晶振直接声明为 100 MHz。

## 100 MHz 实板串口验收

[真实 UART 报告](../reports/npu128-uart100-hardware-validation.json) 为 **PASS**。
使用已枚举且驱动正常的 CP2102 COM11（VID:PID=`10c4:ea60`），115200 baud、8N1，
没有使用仿真器。先读 ID=`4e505531`、ABI=`00010000`，再运行 RAW、逐通道量化及 LUT
三组独立标量对照卷积：12 个输出向量、192 个 RAW 和 192 个 INT8 通道槽全部一致。
2,759 次事务、零重传；MAC 发射计数分别为 4、72、72，与 RTL 参考一致。

实际耗时 80.891 秒，包括串口、USB、主机和输出逐寄存器读回开销，不能当作核心计算延迟，
也不能据此宣称整图推理加速比。100 MHz 是本次 PLL/STA 配置；本测试未使用外部频率测量仪。
范围仍为三个合成 tile，尚非完整 YOLOX 模型准确率或 SoC/DDR/DMA 验收。

报告绑定本次 RTL、JTAG、位流与主机源码 SHA-256；原始 JSON 和 UTF-8 日志保留在
`F:\pango-project\work\npu128-uart100-board-20261024a\hardware-functional.{json,log}`。
仓库 JSON 统一为 UTF-8 LF，原 JSON 数值逐项保持一致，并增加原始文件/日志哈希和本次用户 KEY0 确认。
没有修改历史 27 MHz 实板报告和本次较早的 `JTAG_PASS_UART_PENDING` 快照。

后续优先测量批量数据传输与端到端吞吐，再接入图片/完整图调度；当前逐向量串口读回会造成核心等待。

## 复现

以下命令在仓库根目录运行；新的输出路径必须尚不存在，厂商模型只读取本机 PDS 安装。

```bash
python3 tools/run_mmio_tests.py --build-dir build/mmio-new --output build/mmio-new/validation.json
python3 tools/run_uart_tests.py --clock-hz 100000000 \
  --mmio-validation build/mmio-new/validation.json \
  --build-dir build/uart100-new --output build/uart100-new/validation.json
python3 tools/run_clock100_tests.py \
  --vendor-model /mnt/f/pango/PDS_2025.2-ads/arch/vendor/pango/verilog/simulation/GTP_GPLL.v \
  --build-dir build/clock100-new --output build/clock100-new/validation.json
python3 tools/pango/stage_uart100.py --work-root /mnt/f/pango-project \
  --validation build/uart100-new/validation.json \
  --clock-validation build/clock100-new/validation.json --name npu128-uart100-new
```

在 PowerShell 执行新原生工程自带的 `tools/pango/run_synthesis.ps1`，传入对应 `ProjectRoot`、
`PdsHome`、`JobLimitGiB=3`、`ReserveGiB=6`、`TimeoutSeconds=3600`。
待综合生成网表后执行 `tools/pango/check_uart_netlist.py --help` 对相同仿真器、报告和工程回放，
再用 `collect_uart_implementation.py` 收集最终时序。时序、覆盖、数值均通过后，才能以
`config/uart100-bitstream.json` 生成 usercode `12810001` 的位流。

接续本机实板时，无需重跑 PDS 或重新生成位流。保持当前 FPGA 供电，按下再松开 KEY0 后，
在仓库根目录 PowerShell 执行（输出使用新路径）：

```powershell
$env:PYTHONPATH='F:\pango-project\work\npu128-uart-board-20261024a\python-deps\site'
$uartPython='C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $uartPython -X utf8 tools/run_uart_board_test.py --port COM11 `
  --rtl-validation reports/npu128-uart100-functional.json `
  --jtag-validation reports/npu128-uart100-board.json `
  --bitstream-validation reports/npu128-uart100-bitstream.json `
  --output build/uart100-hardware-new.json
```

该测试会软复位 NPU 并替换合成测试数据，核对三组卷积的 12 个输出向量。
若 FPGA 已断电，须先用本次 100 MHz 位流重新临时下载和回读，再按 KEY0；
不能凭历史 JTAG 报告假定当前配置未变化。
