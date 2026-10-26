# UART 主机收包与实板吞吐（2026-10-26）

> 后续权重批量写已完成并实板通过，见 [UART_BULK128.md](UART_BULK128.md)。本页保留主机收包阶段的独立对照。

分支 `fpga/npu128-uart-host` 从 `00ce465` 接续。保持已验收的真实 PLL 100 MHz 固件，
本轮只优化电脑端收包并测量吞吐。RTL、约束、配置和位流均未改变，仍使用
[100 MHz 硬件阶段](MMIO_TIMING128.md)的 128 MAC、82 APM、126.5 DRM。

## 实际收益

[同日同位流 A/B 报告](../reports/npu128-uart-host-throughput.json)和
[优化版实际卷积报告](../reports/npu128-uart-host-hardware-validation.json)均为 **PASS**。
CP2102 COM11、115200 baud、8N1，分别使用原始客户端及新客户端，未使用仿真器。

| 项目 | 原始客户端 | 本次客户端 |
| --- | --- | --- |
| 三组卷积完整流程 | 77.672 秒 | 18.828 秒 |
| 主机事务 | 2,759 | 2,759 |
| 重传 | 0 | 0 |
| RAW / INT8 检查 | 192 / 192 个值 | 192 / 192 个值 |
| ID 读微基准：三轮事务率中位数 | 36.36 次/秒 | 150.06 次/秒 |
| 正常回复的 `read()` 请求长度 | 64 字节 | 12 字节 |

完整流程耗时减少 **75.76%**，约 **4.13 倍**；微基准事务率约 **4.13 倍**。
三组卷积共有 12 个输出向量，两个客户端的 RAW、INT8、坐标、mask、mode/last 及
MAC 发射计数逐项一致。MAC 计数仍为 4、72、72。

这是包含串口、USB、主机、数据装载和逐寄存器读回的端到端合成测试收益，不能当作
NPU 核心频率/计算吞吐提升或整图推理加速比。本轮未实现新批量帧，也未提高波特率。

## 修正的问题和验证

协议应答固定 12 字节。原 `SerialWire.exchange()` 调用 `read(64)`，pyserial 收到完整
12 字节之后仍等待读超时（20 ms），使每个停等事务付出不必要的空等时间。
现在每次只读取凑齐 12 字节所缺的长度，完整帧立即进入原有 header/sequence/CRC 检查。
若有噪声、坏 CRC 或过期 sequence，继续重同步并读取剩余尾部。

发送帧、CRC、客户端状态机、标量参考、超时后原样重试 START/POP、设备错误处理均保持原样；
以原客户端 SHA-256 及 AST 对比证明，唯一修改的方法是 `SerialWire.exchange()`。
没有在事务之间清空接收缓冲区，也没有向只允许停等的旧桥连续灌入请求。

[主机功能报告](../reports/npu128-uart-host-functional.json)的九项测试覆盖完整帧及时返回、
分片、噪声/头部重同步、坏 CRC 与过期回复、丢失 START 回复、坏 POP 回复、
重试耗尽、MMIO 错误和短写/关闭。使用可控计时的串口模型，实际数值和速度另由实板验收。

`run_uart_board_test.py` 新增显式 `--host-validation`：固件时代功能报告保持不可变，
只有绑定该报告旧客户端哈希、当前主机测试和源码的主机证据才允许客户端哈希变化；
全部 RTL 和其余功能输入仍须匹配原固件报告。

## 本轮配置恢复与测量范围

开始时 COM11 正常，但 UART 无应答。只读 JTAG 检查为 ID=`00603899`、DONE=0、
usercode=`00000000`，上次临时配置已丢失。已重新临时下载同一份既有位流，SHA-256：
`f5d339f84b8dba346469d080c98673b471abd58c664c85521bd5a7d7b1cdd468`。
回读通过，usercode=`12810001`，DONE 与错误位检查通过；用户随后确认本次 KEY0 已按。
没有重新生成位流或运行 PDS，也没有写 Flash/eFuse。结束时串口已关闭，本任务 JTAG 服务已退出。

三轮微基准每版每轮 64 次 ID 读取，交替顺序为旧/新、新/旧、旧/新；每次打开串口后先 SYNC
并检查 ID/ABI，样本重传均为零。完整卷积各运行一次，新版在前、旧版在后，因此仍存在
USB 调度、主机负载和单次完整流程的测量不确定性；三轮微基准用于交叉核对。

第一次 A/B 前置检查发现 WSL Python 3.10 与 Windows Python 3.12 的 `ast.dump` 字段不同，
修正为同一解释器内比较新旧 AST，并以跨环境一致的源码 SHA-256 绑定输入。
下一轮使用 Windows `monotonic` 时钟，短事务被量化为 0 ms，主动停止并保存中止记录。
正式接受的是高精度 `perf_counter` 测量（报告分辨率 100 ns）；原始日志和失败/中止记录均保留。

原生目录：`F:\pango-project\work\npu128-uart-host-20261026a`。
`optimized-hardware.json/log`、`throughput-c.json/log` 为本轮通过结果；
`preflight.log`、`scan.log`、`program-a/`、原始旧客户端及较早测量记录一并保存。
仓库报告保留原始 JSON 数值，增加原始文件/日志及关联证据的 SHA-256。

## 下一步批量协议的目标

实际旧版完整流程的 2,759 笔事务中，2,194 笔写、564 笔读、1 笔 SYNC。
权重暂存/索引/提交占 1,258 笔（45.6%），配置三次写占 786 笔，另有配置检查读。
因此下一轮优先实现 `WEIGHT_DATA` 的连续 32 字批量写，保留索引和 COMMIT 的有序语义，
随后再处理 LUT 装载和结果向量批量读回。

当前协议每事务共 26 字节，整组流程共 71,734 个串口字节，在名义 115200 8N1 下
纯线上传输下界约 6.227 秒，实际新版为 18.828 秒。仍有协议与 USB/主机调度开销，
不应将剩余时间全部归于 FPGA 计算。未来批量帧须独立验收完整 CRC 后才生效、
整个请求/响应的重传缓存、部分失败语义及复位恢复，并重新验证完整顶层时序和新位流。

## 复现

保持板卡供电；若临时配置丢失，先恢复上述位流并按 KEY0。在仓库根目录 WSL 生成
原客户端和新的主机功能报告，路径必须尚不存在：

```bash
git show 00ce465:tools/npu_uart.py > build/baseline-npu-uart.py
python3 tools/run_uart_host_tests.py --baseline-client build/baseline-npu-uart.py \
  --build-dir build/uart-host-new --output build/uart-host-new/validation.json
```

Windows Python 须使用现有独立 pyserial 环境；具体路径沿用 [100 MHz 记录](MMIO_TIMING128.md)。
实际卷积使用 `tools/run_uart_board_test.py` 的 100 MHz 三个参照报告参数，并增加
`--host-validation build/uart-host-new/validation.json`，输出使用新的原生路径。
`tools/benchmark_uart_host.py --help` 提供同位流对照参数；需要原客户端、主机功能报告、
新版实际卷积报告和本次下载回读收据。A/B 中旧版完整卷积会复位 NPU 并替换合成测试数据。
