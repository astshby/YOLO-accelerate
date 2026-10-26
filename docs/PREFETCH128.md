# 128 MAC 激活双 bank 预装载（2026-10-24）

后续在保持双 bank 容量和吞吐的前提下压缩 RAM 映射，见 [RAM128.md](RAM128.md)；本页保留未分段物理基线。

分支 `fpga/npu128-prefetch` 基于上一轮已完成 100 MHz 布线的 `8315580`。
保持 128 INT8 MAC、地址/MAC/后处理流水级数和两项结果队列，
新增第二个激活 bank，把下一块输入的装载与当前块计算重叠。
上一轮连续像素调度及物理基线见 [THROUGHPUT128.md](THROUGHPUT128.md)。

## 接口及所有权

`adam_conv128` 与 `npu_conv128` 新增两个一位输入：

| 信号 | 使用方式 |
| --- | --- |
| `act_wr_bank` | 当前激活写入的目标 bank，0 或 1；地址仍是 bank 内地址 |
| `cmd_act_bank` | 本次命令读取的 bank，仅在命令握手时锁存 |

旧使用方式可将两个输入都接 0。bank 0/1 分别是独立同步双端口 RAM，
每个 bank 的逻辑尺寸为 24,576×64 bit，即 192 KiB；实际物理占用见下方资源表。
每个 bank 仍采用原来的像素、输入通道组布局和五级地址流水。
活动 bank 的 RAM 输出经 64 bit 选择送往原 MAC，数据读延迟未增加。
由于同一时间只执行一个命令，bank 选择在整个地址与读流水期间保持不变。

- 空闲时可以装载任一 bank；计算时仅接受对非活动 bank 的写入。
- 活动 bank 在命令握手时确定，之后改变 `cmd_act_bank` 不会切换正在执行的任务。
- NPU 在后处理尚未交付最后一个输出时仍保护该 bank，避免卷积核先完成后提前解锁。
- 权重、量化系数、LUT 和新命令仍只在 NPU 空闲时接受；本轮不预装载下一组权重。
- 写入优先于命令；发起命令前必须撤销写 valid。复位期间所有握手禁止。
- 没有描述符队列或 bank 完整性位。驱动负责装满本命令会使用的所有字，等待 ready 握手，
  并在当前命令完成后才提交下一条命令。复位清除任务状态，RAM 数据按原约定保留。

诊断测量壳的输入扫描宽度从 1714 改为 1716 bit，输出仍为 813 bit。
两个 bank 位放在原输入结构的最高位，事务压缩器及厂商回放同步更新。
这是诊断接口变化，不是新增板级总线或 DMA 控制口。

## 已接入的图片卷积路径

`sim/adam_image_server.cpp` 现在执行以下实际 RTL 端口时序：

```text
装载本输出通道块的权重
装载 tile 0 → bank 0
启动 tile 0；同时装载 tile 1 → bank 1，并持续收集 tile 0 输出
等待当前完成和下一块装满
启动 tile 1；同时装载 tile 2 → bank 0
……最后一块交付后，再装载下一输出通道块的权重
```

输入装载和结果接收由同一个逐拍服务函数处理。
若计算提前完成，完成脉冲和计数器会立即保存，装载剩余输入时不会丢失脉冲。
若装载提前完成，则等待当前计算结束。每块重新确认数值、元数据、MAC 计数和独立周期契约。
二进制图片请求/响应协议、NCHW/OIHW 布局、halo 生成及返回 INT32 数值保持兼容。

16 次整图卷积、115,392 个值与独立 NumPy INT64 参考一致，4 个非法请求拒绝。
与上一轮相同随机种子、相同非零张量相比，全部输出 SHA-256、MAC 步数、活动计算周期和分块数一致。
例如 23×29、Ci17、Co33、1×1/stride1 的总周期从 22,095 降为 18,396；
3×3/stride2 从 22,653 降为 17,832。只有一个空间块的任务保持原周期，不能从预装载受益。
本轮未重跑预训练 YOLOX 全图的检测精度。

## 全部 Tiny 分块的周期实测

[性能报告](../reports/npu128-prefetch-profile.json) 遍历历史图片记录的全部 83 层形状，
实际执行每一个 tile 的权重/激活写入、命令和输出交付，比较串行装载与预装载两种驱动方式。
覆盖 416/320 输入及三种模式，共 12 个完整场景；未用理想 `max(装载,计算)` 估算代替 RTL 执行。
串行实测总周期与上一轮逐 tile 加权结果完全相同，计算活动周期及传输字数前后相同。

| 输入 / 模式 | 串行总周期 | 预装载总周期 | 进一步减少 | 同频加速比 |
| --- | ---: | ---: | ---: | ---: |
| 416 / INT32 | 36,812,249 | 30,014,793 | 18.47% | 1.226× |
| 416 / 量化或激活 | 37,217,800 | 30,404,220 | 18.31% | 1.224× |
| 320 / INT32 | 21,854,926 | 18,286,549 | 16.33% | 1.195× |
| 320 / 量化或激活 | 22,098,789 | 18,525,998 | 16.17% | 1.193× |

100 MHz 换算下，416 量化/激活场景由 372.178 ms 降至 304.042 ms；
320 由 220.988 ms 降至 185.260 ms。
416 有 6,813,580 个激活写入字与计算重叠，正好对应减少的总周期；
总激活写入仍为 10,130,269 字、权重为 39,459 字，MAC 步数仍为 26,029,380。
原计算阶段的 27,043,273 拍保持不变。对更早的 `e1510bd` 串行物理基线，
两轮优化合计将同一口径的 46,145,731 拍降为 30,404,220 拍，减少 34.11%。

测试采用零张量、已配置的量化/identity LUT、始终 ready 的最终输出，
本地端口每拍一个 64 bit 激活字或一个 1024 bit 权重字。
只在同一输出通道块内预装载，没有跨块或跨层预取。
这些毫秒数不含 DDR/DMA、CPU、非卷积算子、通信及系数配置，不是板上实测或整机 FPS。
真实系统的传输带宽和图依赖仍需独立验证。

## 验证及物理结果

- 128 路乘法结构不变；16,777,216 组算术穷举、1,024 项 RAM、7,333 个 MAC 向量及 33 次复位通过。
- 通用卷积回归 131 个任务、9,050 个像素，包括 bank 1 全容量及最高读地址、
  四种整图拼接、9 个非法命令、19 次中止恢复。
- 双 bank 专项：80 次启动、1,188 个输出向量、4,190 次忙时非活动 bank 写入、
  2,304 拍冲突写保护、4 次“计算与部分预装载同时中止”的复位恢复，覆盖三种模式。
- 测量壳 480 次事务，包含忙时 bank 1 装载、活动 bank 毒值写入被拒绝、
  bank 1 结果核对，以及重跑 bank 0 证明原数据未被覆盖。
- 独立后处理、NPU 旧模式回归及上述图片端口回归均通过。

新原生物理工程为 `npu128-prefetch-20261024a`，保持原器件、10 ns 时钟、
0.2 ns setup / 0.05 ns hold uncertainty 和全部 IO delay，FDC 字节与原基线一致。
新网表已重新通过 [厂商 GTP 四态功能仿真](../reports/npu128-prefetch-netlist-validation.json)：
480 次事务、141 份功能快照、2,623 个回放时钟。
仅压缩诊断串行空拍，比较输出数据、状态和元数据；性能计数器另由 RTL 测试验证。

[最终物理报告](../reports/npu128-prefetch-implementation.json) 和
[验证汇总](../reports/npu128-prefetch-validation.json) 均为 **PASS**。
时序来自 hold 修复后的 PNR 数据库，全部 15 条原约束实际导入，
末次显式统计为 `Unrouted nets at iteration 37 (0.282 sec): 0`。

| 分析 | Fast | Slow |
| --- | ---: | ---: |
| setup WNS / ns | +3.055 | +0.193 |
| setup TNS / ns、失败端点 | 0、0 | 0、0 |
| hold WHS / ns | +0.037 | +0.037 |
| hold THS / ns、失败端点 | 0、0 | 0、0 |
| 最小脉宽余量 / ns | +4.345 | +4.040 |
| 脉宽失败端点 | 0 | 0 |

setup/hold 每组覆盖 40,742 个端点，脉宽每组 7,369 个端点。
时钟、输入输出 delay、组合环和锁存器检查通过，没有 `Route-2036` 时钟普通布线告警；
`virtual_clock=1` 的详细文字仍为 `No virtual clock was found.`，是信息项。
工具报告测量工程 Fmax **101.9680 MHz**，实际验收请求仍为 **100 MHz**。
最差 setup 仍位于测量壳 `snapshot_q[812] → scan_dout`，最差 hold 位于
`scan_update → drive_q[110]/CE`。已报告的内核内部最差 setup 为
`active_bank_q → MAC APM`，余量 +1.308 ns，布线占数据延迟的 95.6%。
这些结果不能外推为更高频率或 SoC 集成已经收敛。

| NPU 资源 | 上轮单 bank | 本轮双 bank | 增量 |
| --- | ---: | ---: | ---: |
| LUT | 5,267 | 5,299 | +32 |
| FF | 6,505 | 6,507 | +2 |
| APM | 82 | 82 | 0 |
| 等效 DRM36K | 94.5 | 158.5 | +64 |

测量顶层为 5,720 LUT、10,753 FF、82 APM、158.5 等效 DRM36K。
PDS 实际把每个激活 bank 映射为 64 DRM36K，权重为 28.5、后处理为 2；
不能直接用逻辑位数除以 36K 估算物理资源。
本轮 RAM 低于仓库 NPU 规划上限 160，但只剩 **1.5 DRM36K** 余量。
这不是当前 SoC 剩余资源的实测值。上一轮 94.5 DRM36K 的较省 RAM 分支仍保留。
下一步应优先试验激活 RAM 分段推断以改善映射利用率，并重验时序与数值，
再考虑增加缓存或 MAC；本轮不把尚未验证的 RAM 节省量记作结果。

原始第三方 51 个文件的字节哈希全部一致。
物理流程约 994 秒，Windows Job 提交内存峰值 2,069.9 MiB，
最低可用物理内存约 9.94 GiB、提交余量约 9.45 GiB，满足预设保护。

本轮未上板、未生成位流，未接入 CPU/DDR/DMA/PLL，也不包含布线后 SDF 回放。

## 复现

```bash
python3 tools/run_adam_tests.py --build-dir build/prefetch/adam
python3 tools/run_npu_tests.py --build-dir build/prefetch/npu
python3 tools/build_image_sim.py --build-dir build/prefetch/image
# 需要 NumPy；完整保护参数见前两轮文档。
python3 tb/check_image_backend.py --simulator build/prefetch/image/obj/Vadam_conv128 \
  --output build/prefetch/image-test.json
python3 tools/profile_prefetch.py
```

默认 profile 复用本轮 `prefetch` 测试可执行程序，启动前核对整个 NPU 回归的源文件哈希；
全部工作负载、12 场景计数及运行保护数据保存在 `build/prefetch/profile/`。
原图片命令继续使用相同协议；在本分支重新构建图片模拟器即启用预装载。

物理工程每次使用新名称，沿用已验证的综合约束基线：

```bash
python3 tools/pango/stage_implementation.py \
  --synthesis-project "$PDS_WORK_ROOT/work/npu128-timing-20261024b" \
  --work-root "$PDS_WORK_ROOT" --name npu128-prefetch-new \
  --rtl-validation build/prefetch/npu/summary.json
```

在 Windows 执行新工程中的 `tools/pango/run_synthesis.ps1`，保持单线程、
Job 提交内存 3 GiB、物理及提交余量各 6 GiB、超时 1800 秒。
综合完成后可并行运行 `tools/pango/check_netlist.py`，指定本轮
`--rtl-executable build/prefetch/npu/measure/Vnpu_measure_top`、
`--build-dir build/prefetch/netlist` 及实际 PDS/Icarus 路径。
物理完成且新网表 PASS 后再运行 `collect_implementation.py --netlist-validation` 收集证据。

`collect_throughput_validation.py --build-dir build/prefetch` 支持本轮 profile，
配合 `--implementation`、`--profile` 和新的 `--output` 路径汇总。
它会核对源码、二进制、网表、物理证据、非零图片结果和重叠计数，
并检查仓库规划的 NPU 资源上限；该上限不是当前 SoC 实测剩余资源。
