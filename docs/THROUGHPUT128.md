# 128 MAC 连续像素调度（2026-10-24）

后续激活双 bank 预装载与新的端口约定见 [PREFETCH128.md](PREFETCH128.md)；本页保留连续像素物理基线。

分支 `fpga/npu128-throughput` 基于已完成 100 MHz 布线的 `e1510bd`。
本轮保持 128 INT8 MAC、5 级地址流水、7 拍 MAC、6 级定点后处理及全部外部接口，
优化像素之间的流水排空开销。上一版物理基线见 [IMPLEMENTATION128.md](IMPLEMENTATION128.md)。

## 调度与流控

旧控制器每个像素走 `PREPARE → ISSUE → DRAIN → OUTPUT`，
无输出停顿时每个像素需要 `N+15` 拍，其中 `N=ceil(Cin/8)×K×K`。
新控制器独立维护发射坐标与交付坐标，允许连续发射相邻像素，
在地址、RAM 和 MAC 流水上传递 first/last 标记。
first 对应的 MAC 结果使用 bias 初始化 INT32 累加；last 将结果写入两项寄存器队列。
单 beat 像素同时 first/last，也只经过一个 INT32 加法器。

两份 credit 在像素第一拍发射前预留，仅在结果握手交付时释放。
因此始终有 `已完成队列项数 + 流水中未完成像素数 <= 2`。
预留成功后，一个像素的全部 taps/groups 连续发射，不需要让不可暂停的 MAC 流水等待下游。
下游无限停顿会阻止下一个像素启动，但不会让已有结果溢出。
读写指针分开，支持同拍出队和入队；输出停顿时，数据、坐标、mask、last 保持稳定。
只有最后一个输出真正交付才完成命令。复位清除全部有效标记、credit 和队列计数，
数据寄存器无需清零，失效数据不会作为结果输出。

卷积核直接连接始终 ready 的接收方时，以命令接收后的第一拍为发射时间 1，
第 `i` 个像素（从 0 开始）的周期契约为：

```text
s[0] = 1
s[1] = s[0] + N
s[i] = max(s[i-1] + N, d[i-2])             (i >= 2)
d[i] = max(s[i] + N + 13, d[i-1] + 1)      (d[-1] = 0)
active_cycles = d[最后一个像素]
```

[独立周期模型](../software/adam128_cycle_model.h) 与 RTL 无状态共享。
单像素需要 `N+14` 拍；`N>=13` 时可以连续按 N 拍间隔启动，短像素受两份 credit 限制。
接入量化/激活后仍受四路后处理吞吐率限制，不能直接用该公式推导完整 NPU 时间。
`perf_conv_cycles` 和 `perf_total_cycles` 继续计入实际等待时间。

## 同频周期对照

[完整统计与原始 tile 计数](../reports/npu128-throughput-profile.json) 使用
历史图片联合仿真的 Tiny 83 层形状，覆盖 416、320 两种输入。
同一测试驱动分别运行 `e1510bd` 的 RTL 与本轮 RTL，测量 447 组 tile/模式组合，
再按真实分块次数加权；不是用新周期公式替代 RTL 测量。
所有 166 个层形状、MAC 步数、分块命令数和端口装载量均与历史实际运行记录核对一致。

| 输入 / 模式 | 原 NPU 周期 | 新 NPU 周期 | 周期减少 | 同频加速比 |
| --- | ---: | ---: | ---: | ---: |
| 416 / INT32 | 35,923,214 | 26,637,722 | 25.85% | 1.349× |
| 416 / 量化或激活 | 35,971,204 | 27,043,273 | 24.82% | 1.330× |
| 320 / INT32 | 21,256,733 | 15,768,130 | 25.82% | 1.348× |
| 320 / 量化或激活 | 21,289,063 | 16,011,993 | 24.79% | 1.330× |

100 MHz 下，416 的量化/激活场景卷积命令总计从 359.712 ms 降到 270.433 ms。
另外按当前本地接口“一拍一个 activation/weight word、各输出通道块装载一次权重、
每 tile 重载激活、每条命令一拍”计入装载，合计从 461.457 ms 降到 372.178 ms，
减少 19.35%。320 相应从 273.759 ms 降到 220.988 ms，减少 19.28%。

这些测试使用零数据、已配置的量化参数和 LUT、始终 ready 的最终接收方。
模式 1/2 是时序场景，尚未运行校准后的 YOLOX 静态量化计算图。
不含 CPU、DDR/DMA、非卷积算子、通信、系数配置，也没有访存与计算重叠。
上述毫秒数由计数器按 100 MHz 换算，不是板上时间或整机 FPS，不能据此宣称完整模型加速或精度通过。

416 激活场景的有效 MAC 利用率由 69.64% 提升到 92.63%，定义为
`原计算图有效 MAC 数 / (128 × NPU 命令周期)`。
`perf_post_stalls` 从 0 增到 711,030 拍，是卷积生产者更快后在后处理入口排队的时间；
这些时间已计入总周期，不能再加一次或解释成总耗时增加。

首层节省 1,291,620 拍（约 45.16%），是绝对收益最大的层。
优化后最热的四个 52×52、96→96 的检测头 3×3 卷积各需 1,754,592 拍，
其中真实 MAC 发射已经占 1,752,192 拍。继续缩短像素控制开销的空间很小；
下一步可评估激活双缓冲与传输/计算重叠，再根据布线和带宽决定是否增加 MAC。

## 验证与物理实现

本轮数值、背压和复位验证包括：

- 16,777,216 组 INT8 三元组、1,024 项 RAM 对照、7,333 个 MAC 向量、33 次 MAC 复位。
- 卷积 130 个任务、8,794 个输出像素、四种整图分块拼接、9 个非法命令、19 次中止恢复；
  首输出连续停顿 128 拍，并覆盖 `N=12/13/14` 的 credit 临界间隔。
- 后处理 1,924 个向量、全部 64 个 shift、256 个 LUT 地址及复位恢复；
  NPU 三种输出模式、非法命令、完成握手与长停顿。
- 图片接口 16 次整图卷积、115,392 个值与独立 NumPy INT64 参考逐项一致，四个非法请求拒绝。
- 测量壳 450 次事务；新增四像素任务在三种模式下分别长时间保持每个输出，
  覆盖队列满、credit 耗尽和释放后的继续发射。

物理实现使用新目录 `npu128-stream-20261024a`，保持原器件、10 ns 时钟、
0.2 ns setup / 0.05 ns hold uncertainty 和全部 IO delay，FDC 字节不变。
本轮逻辑发生变化，不能沿用旧网表的功能 PASS；必须对新综合网表重新执行厂商 GTP 四态仿真。
[新网表验证](../reports/npu128-throughput-netlist-validation.json) **PASS**：
450 次事务、120 份功能快照、2,353 个回放时钟。
串行空拍压缩回放省略性能计数器比较；计数器另由 RTL 测试检查。
不含 SDF 回放、SoC 联调或上板。

[最终物理报告](../reports/npu128-throughput-implementation.json) 与
[验证汇总](../reports/npu128-throughput-validation.json) 均为 **PASS**。
最终报告来自 hold 修复后的 PNR 数据库，不使用综合估计替代。

| 分析 | Fast | Slow |
| --- | ---: | ---: |
| setup WNS / ns | +3.110 | +0.243 |
| setup TNS / ns、失败端点 | 0、0 | 0、0 |
| hold WHS / ns | +0.036 | +0.041 |
| hold THS / ns、失败端点 | 0、0 | 0、0 |
| 最小脉宽余量 / ns | +4.345 | +4.040 |
| 脉宽失败端点 | 0 | 0 |

setup/hold 每组覆盖 38,549 个端点，脉宽每组 7,237 个端点。
末次显式未布通统计为 `Unrouted nets at iteration 36 (0.627 sec): 0`。
全部 15 条原约束实际导入；时钟、输入输出 delay、组合环和锁存器检查通过，
没有普通布线资源承载时钟的 `Route-2036` 告警。
`virtual_clock=1` 仍是工具的“没有虚拟时钟”信息项。

工具报告测量工程 Fmax 为 **102.4905 MHz**，实际验收请求为 **100 MHz**。
最差 setup 仍为测量壳 `snapshot_q[812] → scan_dout`；
最差 hold 是测量壳 `scan_shift → input_shift_q[1514]/CE`。
已报告路径中，内核内部最差 setup 为 `read_padding_q → MAC APM`，余量 +1.820 ns，
布线占该路径数据延迟的 94.7%；外部复位到激活 RAM CE 的路径余量为 +1.741 ns。
不能由这些路径的余量外推更高频率已经收敛，SoC 集成后也需重新布局布线。

| 范围 / 资源 | 原物理基线 | 本轮 hold 修复后 | 增量 |
| --- | ---: | ---: | ---: |
| NPU LUT | 5,268 | 5,267 | −1 |
| NPU FF | 5,961 | 6,505 | +544 |
| NPU APM | 82 | 82 | 0 |
| NPU 等效 DRM36K | 94.5 | 94.5 | 0 |
| 测量顶层 LUT / FF | 5,688 / 10,203 | 5,687 / 10,747 | −1 / +544 |

增加的 FF 主要用于额外一份结果存储和调度控制；没有增加乘法阵列或片上 RAM。
原始第三方 51 个文件的字节哈希全部一致。
物理流程约 1,193 秒，Windows Job 提交内存峰值 2,131.8 MiB，
最低可用物理内存约 10.96 GiB、提交余量约 11.49 GiB，满足 3 GiB 上限与各 6 GiB 余量。

## 复现入口

```bash
python3 tools/run_adam_tests.py --build-dir build/throughput/adam
python3 tools/run_npu_tests.py --build-dir build/throughput/npu
python3 tools/build_image_sim.py --build-dir build/throughput/image
python3 tb/check_image_backend.py --simulator build/throughput/image/obj/Vadam_conv128 \
  --output build/throughput/image-test.json
```

图片接口测试需要 NumPy。每次 profile 使用不同构建目录：

```bash
mkdir -p build/throughput/baseline
git archive e1510bd rtl/adam rtl/npu | tar -x -C build/throughput/baseline
python3 tools/profile_npu.py --rtl-root build/throughput/baseline --build-dir build/throughput/baseline-profile
python3 tools/profile_npu.py --build-dir build/throughput/optimized-profile
python3 tools/compare_npu_profiles.py \
  --before build/throughput/baseline-profile/summary.json \
  --after build/throughput/optimized-profile/summary.json --baseline-commit e1510bd \
  --output build/throughput/comparison.json
```

布局布线沿用原生 Windows PDS 目录，必须使用新名称以保留旧证据：

```bash
python3 tools/pango/stage_implementation.py \
  --synthesis-project "$PDS_WORK_ROOT/work/npu128-timing-20261024b" \
  --work-root "$PDS_WORK_ROOT" --name npu128-stream-new \
  --rtl-validation build/throughput/npu/summary.json
```

在 Windows 运行该工程的 `tools/pango/run_synthesis.ps1`，参数同上一轮物理验证。
综合完成后，用 `tools/pango/check_netlist.py --project "$PROJECT"` 检查这份新网表，
同时指定本轮 `--rtl-executable build/throughput/npu/measure/Vnpu_measure_top`、
`--build-dir build/throughput/netlist` 及实际 PDS/Icarus 路径。
完整物理流程结束后：

```bash
python3 tools/pango/collect_implementation.py --project "$PROJECT" \
  --netlist-validation build/throughput/netlist/summary.json \
  --output build/throughput/implementation.json
python3 tb/check_implementation_report.py
```

新 staging 模式可以让门级仿真与物理后端并行；最终收集器仍要求新网表 SHA 对应的功能 PASS，
并分别判断布通、Fast/Slow setup/hold/脉宽与约束覆盖。工具正常退出不等于时序收敛。

最后可使用 `tools/collect_throughput_validation.py` 汇总本轮构建目录、
`--implementation`、`--profile` 与新的 `--output` 路径；它再次核对当前源码、
测试/网表/实现之间的哈希及全部第三方原始文件，任一项过期都拒绝生成 PASS。
