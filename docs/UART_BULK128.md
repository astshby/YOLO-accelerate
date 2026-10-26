# 权重批量写与实板验收（2026-10-26）

分支 `fpga/npu128-uart-bulk` 从已实测的 `a35cd65` 接续；原 100 MHz 硬件及主机优化
分支均保留。本轮新增串口桥 op3，以一次整帧替代一整个 1024 bit 权重字的 34 次主机事务。
计算核、MMIO、PLL、引脚和约束保持原样。最终隔离修正版的功能、PLL、四态网表、
完整 100 MHz 时序、JTAG 临时下载/回读及两种模式实板数值/吞吐均通过。
首个候选因新增帧错反例被拒绝，原因与修复记录保留在后文；原稳定基线仍保留。

## 帧和执行顺序

旧 14 字节读/写/SYNC 帧继续有效。op3 请求固定 **142 字节**，多字节数值小端：

| 偏移 | 字节数 | 内容 |
| --- | --- | --- |
| 0 | 2 | `A5 5A` |
| 2 | 1 | version=1 |
| 3 | 1 | sequence |
| 4 | 1 | op=3 |
| 5 | 1 | 保留=0 |
| 6 | 2 | 固定地址 `0100` |
| 8 | 4 | 权重字索引，0–863 |
| 12 | 128 | 权重数据，低字节在前 |
| 140 | 2 | CRC16/CCITT-FALSE，覆盖偏移 2–139 |

桥先完整接收并校验格式及 CRC，再有序执行：WEIGHT_INDEX 写一次、
WEIGHT_DATA[0–31] 各写一次、WEIGHT_COMMIT=1 写一次。每次写都等待 MMIO 响应，
COMMIT 应答代表核心已经消费提交，不提前确认成功。应答仍为原 12 字节格式，
status=0、data=34 表示全部完成。

若第 k 笔总线写报错（k 从 0 开始），立即停止后缀，status=1、data=k 表示此前成功的操作数。
不回滚已完成的索引/暂存区写；COMMIT 仅在最后一笔出现。现有 MMIO 报错不产生目标副作用，
因此忙时 COMMIT 被拒绝时 RAM 不被该批量帧修改，data=33。软件应检查/清除 ERRORS，
确认设备状态，再以新 sequence 显式重装整个权重字。高层 Client 会抛出原 RuntimeError；
低层 `reply()` 可读取完成计数。部分失败不意味着整批原子回滚。

## 重传与恢复

缓存保存最后一个已完成请求的完整头部、128 字节载荷和完整响应，包括失败响应。
同 sequence、同完整内容的重传只返回缓存；不同内容返回 status=4。载荷先按 32 bit 分组
比较并寄存，再归约结果，避免把整 1024 bit 比较放在同一条控制组合链中。
相同 CRC16 不作为相同请求的依据。

坏 CRC、坏格式、半帧和超时均不发起 MMIO 写，也不破坏上一个已完成请求的缓存。
UART 帧错后进入隔离，来字节会重新计时，连续无新接收字节/帧错满帧间超时（100 ms）后才允许同步新帧；
这样错误载荷中嵌入的合法旧帧不能绕过外层 CRC 执行。
SYNC 清桥缓存；KEY0 清整个桥状态，外部复位可能取消尚未完成的后缀，但不承诺回滚前缀。
仍只允许一个有序客户端和一个停等帧；在未收完整应答时不得开始新的不同请求。

主机以 `Client(..., bulk_weights=True)` 或 `--bulk-weights` 显式启用。
旧位流不支持 op3，不能自动尝试后回退。批量位流将使用独立 usercode `12810002`，
SCBV 继续为 3.3 V；普通基线为 `12810001`。

## 验证计划与已有检查

- 桥级测试：34 笔地址/数据、请求与响应停顿、全载荷重复/冲突、坏 CRC、越界索引、
  在第 0/1/16/33 笔注入失败并重放失败响应、半帧超时、执行中复位。
- 串口收包单测：原九项加完整 142 字节丢回复重传，以及非法形状在 I/O 前拒绝。
- 完整引脚：868 拍/bit、10,000,000 拍超时，保留旧帧检查；新增 CRC 碰撞载荷、
  最高索引 863、坏格式/半帧/帧错、忙时 COMMIT 失败后再次卷积验证 RAM 未被毒化。
- 厂商 GPLL 整顶层的锁定、KEY0 和三拍释放测试重新绑定当前桥 RTL。
- 随后独立执行新 GTP 网表回放、完整 100 MHz 双角 setup/hold/覆盖、位流、实板数值和吞吐。

三组合成卷积的主机事务由 2,759 降至 1,538，协议计数推导已由实板对照验证。
实际速度还受 USB、配置装载和输出读回影响，必须使用同位流新旧模式实测，不能由事务数
直接换算成推理加速比。

## 首轮功能检查（历史，已被后续反例拒绝）

[完整引脚报告](../reports/npu128-uart-bulk-functional.json)通过三组主标量卷积，
另有忙时批量 COMMIT 失败后的四向量重跑，证明活跃 RAM 未被暂存毒化。
旧帧九类错误恢复与批量十类错误/冲突恢复通过，包括真正的 CRC16 碰撞载荷。
主 Client 记录 1,665 笔事务、四次安全重传；兼容性探针另用旧模式写一个完整权重字。
共运行 485,573,263 个 100 MHz 数字时钟，868 拍/bit，未缩短超时。
桥级测试接受 125 笔受检查的 MMIO 请求、960 拍 TX 停顿；串口模型单测 11 项通过。

[新 PLL 源码绑定](../reports/npu128-uart-bulk-clock.json)也已通过，周期、占空比、锁定与
KEY0 三拍释放均检查。72 帧公开引脚黄金字节轨迹已通过；首轮网表回放因候选被拒绝而中止。综合估计为 7,665 LUT / 11,713 FF / 82 APM / 126.5 DRM，
setup +0.773 ns；这些综合估计不能替代最终布线验收。

## 首次候选被拒绝及修正

[反例记录](../reports/npu128-uart-bulk-initial-rejection.json)保存一次确定失败：
在批量载荷后半嵌入 CRC 有效的 ACT_DATA 写，并在载荷前半注入 UART 停止位错误，
首次候选错误地把 ACT_DATA 从 `11223344` 改成 `deadbeef`。该候选虽有正的布线时序，
不作为交付版本，没有生成或下载它的位流；原工程、二进制、日志和原反例源码快照保留。
旧网表回放也已主动中止，不能作为最终 PASS。

修正为帧错后的连续空闲隔离。[隔离桥级报告](../reports/npu128-uart-bulk-quarantine.json)
已通过嵌套有效帧忽略、来字节延长隔离、空闲恢复和硬复位清除四项检查。
修正后的完整引脚、独立反例重跑、PLL、PDS 和新网表必须重新绑定当前源码。

## 最终修正版功能与物理验收

[最终引脚回归](../reports/npu128-uart-bulk-final-functional.json)、
[最终 PLL 检查](../reports/npu128-uart-bulk-final-clock.json)、
[最终四态网表](../reports/npu128-uart-bulk-netlist-validation.json)和
[最终物理报告](../reports/npu128-uart-bulk-implementation.json)均为 PASS。
嵌套有效旧帧的独立位级反例重跑通过，ACT_DATA 保持原值，坏载荷不执行命令。
最终四态使用未修改的 GTP 原语和精确综合网表，72 帧、3,297 拍数值/协议回放通过；
只旁路 UART 位收发和 PLL 生成时钟，没有强制 MMIO、核心状态或结果。
大载荷在 Icarus 强度向量传播中较慢，实际运行约 775 秒；诊断证实仿真时间持续推进，
没有为加速而修改网表或厂商原语。无 SDF 延迟仿真。

| 项目 | 原 100 MHz 基线 | 最终批量修正版 |
| --- | --- | --- |
| LUT / FF | 6,946 / 9,625 | 7,733 / 11,715 |
| APM / DRM36K | 82 / 126.5 | 82 / 126.5 |
| 最差 setup / hold | +0.805 / +0.039 ns | +0.707 / +0.039 ns |
| 未布通网络 | 0 | 0 |

修正版 fast/slow setup +3.403/+0.707 ns，hold +0.039/+0.085 ns，违规端点均为零；
最小脉宽与覆盖检查通过。Fmax 估计 107.6087 MHz，仅验收实际 100 MHz 配置。
原生最终工程 `npu128-uart-bulk-20261026b`，约 618 秒完成，峰值作业提交 2,133.2 MiB。
启动时 3/2.5 GiB 上限曾被内存预检阻止；按上一轮峰值收紧为 2.25 GiB，上层系统余量
仍保留 6 GiB。相关预检 JSON 和所有原生输出保留，没有降低系统保留量。

[批量位流](../reports/npu128-uart-bulk-bitstream.json)从最终 b 工程的精确布线数据库生成，
未重跑实现，SCBV=High Voltage、usercode=`12810002`，9,188,552 字节，SHA-256：
`a3ac2342d09d9367d4eeb1780d7edc0a3108c767bbcb5149ca2f3080b07732ee`。
实际 JTAG/数值/吞吐独立记录如下，未用仿真或 STA 代替实板结果。

## 当前实际配置与接续

[JTAG 报告](../reports/npu128-uart-bulk-board.json)确认单器件 PG2L200H，ID=`00603899`，
5 MHz 临时配置、回读和 usercode=`12810002` 均通过，状态=`001ffca0`，DONE=1、
错误掩码 `1800001f` 下为零。该快照为 `JTAG_PASS_UART_PENDING`，生成于本次 KEY0 确认之前，按历史记录保留；当前数值/吞吐已通过。
没有写 Flash/eFuse；本任务 JTAG 服务已关闭。原生板卡记录位于
`F:\pango-project\work\npu128-uart-bulk-board-20261026a`。

用户已确认本次 KEY0 与连接正常。后续保持配置未掉电，可用新的原生测量目录复测；在仓库根目录 PowerShell：

```powershell
$env:PYTHONPATH='F:\pango-project\work\npu128-uart-board-20261024a\python-deps\site'
$uartPython='C:\Users\HP\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$bulkRecheck='F:\pango-project\work\npu128-uart-bulk-recheck'
New-Item -ItemType Directory -Path $bulkRecheck
& $uartPython -X utf8 tools/benchmark_uart_bulk.py --port COM11 `
  --rtl-validation reports/npu128-uart-bulk-final-functional.json `
  --bitstream-validation reports/npu128-uart-bulk-bitstream.json `
  --jtag-validation reports/npu128-uart-bulk-board.json `
  --run-directory $bulkRecheck `
  --key0-confirmed
```

输出必须尚未存在。脚本分别运行旧模式和批量模式的三组完整标量卷积，并交替测量三轮
权重装载；使用高精度计时，结果逐值比较，保存每轮协议字节和事务数。两种模式均使用
同一个新批量位流。帧错恢复要求连续无接收字节/新帧错满 100 ms，客户端默认 1 秒超时已覆盖。
若板卡已断电，需重新临时下载本次位流、回读并按 KEY0，再运行实测。

## 实板数值与吞吐结果

[旧模式实板报告](../reports/npu128-uart-bulk-legacy-hardware.json)、
[批量模式实板报告](../reports/npu128-uart-bulk-hardware-validation.json)和
[同位流吞吐报告](../reports/npu128-uart-bulk-throughput.json)均为 **PASS**。
CP2102 COM11、115200 8N1，使用同一份 `12810002` 位流及同一个当前客户端；
旧模式关闭批量开关，批量模式显式开启。两种模式各执行三组卷积、12 个向量，
192 个 RAW 和 192 个 INT8 值与独立标量参考一致，两种模式的结果也逐项相同。

| 项目 | 旧模式 | 批量模式 |
| --- | --- | --- |
| 完整三组卷积流程 | 19.091 秒 | 11.401 秒 |
| 主机事务 | 2,759 | 1,538 |
| 重传 | 0 | 0 |
| 完整流程协议字节 | 71,734 | 44,724 |
| 权重有效载荷吞吐中位数 | 557.3 B/s | 3,886.4 B/s |
| 每个权重字的主机事务 | 34 | 1 |

完整流程约 **1.67 倍**、耗时下降 **40.28%**，主机事务下降 **44.26%**。
权重装载有效吞吐约 **6.97 倍**；这不是核心 MAC 吞吐或整图 YOLOX 的加速比。
微基准三轮、每轮每种模式八个权重字，顺序旧/批量、批量/旧、旧/批量。
每轮旧模式 272 笔、3,808 TX+3,264 RX 字节；批量模式八笔、1,136 TX+96 RX 字节，
各轮零重传，索引和暂存尾字读回一致。使用 `perf_counter` 高精度计时。
完整流程每种模式只测一次、旧模式在前，仍受 USB/主机调度影响；报告保留全部逐轮数据。

全部原始 JSON、日志和较早 JTAG 服务记录仍在原生板卡目录。
仓库 JSON 保留原始值，仅增加原文件/日志、模式报告和物理内存预检的哈希关联。
所有历史基线与被拒绝候选均未覆盖。稳定标签 `baseline/npu128-uart-bulk-20261026`
用于冻结本次可用结果；旧 `baseline/npu128-100mhz-uart-20261026` 也保留。

下一步优先处理 LUT/配置装载：它仍包含 786 次配置写及相应检查读；随后接入完整图片/图调度。
当前已完成权重传输阶段，完整 YOLOX、CPU/DDR/DMA 和整图准确率尚需后续独立验收。
