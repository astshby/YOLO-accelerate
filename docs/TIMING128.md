# 128 MAC 时序优化（2026-10-24）

> 连续像素调度及更新后的周期契约见 [THROUGHPUT128.md](THROUGHPUT128.md)。本页保留流水化阶段的记录。

> 后续独立布局布线已在原 100 MHz 约束下通过 setup/hold，见
> [IMPLEMENTATION128.md](IMPLEMENTATION128.md)。本文件保留前一轮综合与功能验证结果。

开发分支 `fpga/npu128-timing` 从 `main` 的 `08b0402` 开始。根据
[交接文档](HANDOFF.md) 和 [旧综合证据](../reports/npu128-synthesis.json)，
保留 8 输入通道 × 16 输出通道、INT32 回绕累加和现有量化接口。
原始第三方 576 乘积通路保持原样。这里的结果不能归到原版上游。

## 路径与修改

旧版综合估计 Fmax 为 71.8804 MHz，10 ns 下 setup WNS 为 −3.912 ns。
前 100 条最差 setup 路径都进入激活 RAM，起点包括 `stride2_q`；
坐标加减、行地址乘法和通道组地址乘法连续组合。

`adam_conv128` 将读地址生成分为五级，各级每拍接收一组：

| 级 | 寄存结果 | 运算 |
| --- | --- | --- |
| A0 | 输入坐标、group、weight 地址及标签 | stride 移位、tap 加法、padding 减法 |
| A1 | 行偏移、x、padding 判定 | `y * width` |
| A2 | 像素编号及标签 | `row + x` |
| A3 | 像素通道组基址及标签 | `pixel * groups` |
| A4 | RAM 读地址及标签 | `base + group` |

权重按现有 tap/group 遍历顺序连续存放，地址改为每像素从零递增；
无需每拍重新计算 `(ky*K+kx)*groups+group`。group、像素编号及地址乘法
使用按配置上限确定的位宽。padding 的中间地址可以回绕，但不使能激活 RAM，
送入 MAC 的激活仍为零。valid、last、输入尾通道 mask、padding 与权重地址逐级对齐。
复位清空所有 valid，数据寄存器和 RAM 内容不要求清零。

`adam_mac128` 的 8 项加法树改为三层逐层寄存，乘法保留原四拍。
从 MAC 输入采样到输出为七个时钟沿，连续有效输入的 II 仍为 1。
128 个 INT8 乘法单元及四路定点后处理数量不增加。

第一轮综合后，最差路径转到定点后处理。随后把后处理拆为操作数选择、
乘法、绝对值、右移及 guard bit、舍入饱和、同步查表六级（旧版四级）。
用 `floor(abs(product)/2^s) + bit[s-1]` 实现最近舍入、半值远离零，
在窄位宽增量之前检查饱和，避免串接 64 位取负、64 位舍入加法和桶形移位。
正数饱和边界 127、负数边界 128，shift=0 的 guard bit 为零，数值契约不变。
group、mask 和 valid 同步延长两拍；模式 0 直通路径不变。

## 周期与性能边界

接口、数据布局和数值规则不变，固定延迟改变。令 `G=ceil(Cin/8)`、
`N=G*K*K`、`P=输出像素数`，卷积核的计数关系变为：

```text
perf_mac_steps     = P * N
perf_active_cycles = P * (N + 15) + 输出背压拍数
```

旧版固定开销为每像素 8 拍，新版为 15 拍。核仍逐像素排空；
因此不能把 Fmax 的提升倍数直接写成整图加速比。小输入通道的 1×1 卷积
对固定开销最敏感，后处理、缓存装载、DMA、图调度和通信还会增加耗时。
后续跨像素重叠需要独立的累加上下文与输出缓冲验证。

先收敛 128 MAC，避免在原关键路径未消除前加倍权重带宽与扇出。
是否扩到 256 MAC 应比较相同模型的周期、物理频率与缓存预算；
本轮没有 256 MAC 实现或性能结论。

## 复现与验证

```bash
python3 tools/run_adam_tests.py --build-dir build/timing/adam
python3 tools/run_npu_tests.py --build-dir build/timing/npu-final
```

Verilator 使用严格 `-Wall`，单线程和现有内存保护，结果均通过：

| 检查 | 覆盖 |
| --- | --- |
| 原乘法/RAM | 全部 16,777,216 个 INT8 三元组、1,024 次 RAM 检查 |
| 新 MAC 流水 | 7,333 个有效向量、33 次复位、七沿延迟、连续发射、空拍、无效 last、INT8 极值 |
| 卷积 | 121 条命令、6,490 个像素、4 个完整图拼接、9 个非法描述符、19 个复位恢复位置 |
| 后处理 | 1,924 个向量、30,784 个数值、全部 64 个 shift、256 个 LUT 地址 |
| NPU | 33 个 tile、450 个像素、三种模式、背压、非法命令和复位 |
| 测量顶层 | 432 次事务、全部 16 种输出通道数、3×3/stride2/pad1/Ci17 |
| 图片后端 | 16 次卷积、115,392 个数值、4 个非法请求；跨 tile/halo 拼接，标量核对开关均覆盖 |
| 结构 | 64 对 × 2 个 signed INT8 乘法 = 128 |

独立标量卷积与整数舍入参考保留，周期断言更新到新增延迟。
复位测试逐拍覆盖新增地址与 MAC 流水，并等待 20 拍检查旧任务不会重新出现。
详细源码哈希和资源保护采样见本轮 `build/timing/` 摘要及交付报告。

交付证据保存在 [验证汇总](../reports/npu128-timing-validation.json) 和
[综合网表验证](../reports/npu128-timing-netlist-validation.json)。PDS 原始 GTP 模型下，
432 次事务、105 个快照、2,205 个时钟全部匹配。只替代测量壳的串行移位，
不强制 NPU 数据或内部状态；比较数据、模式、mask、坐标、last 和握手状态共 685 位。
压缩扫描空拍后不比较性能计数器，未加载 SDF，因此这项是功能验证。
门级编译采样峰值 1,410.3 MiB，执行 552.3 MiB、395.4 秒，均在原保护限额内。

独立综合沿用 PG2L200H-6IFBB676、PDS 2025.2、10 ns 时钟、原输入/输出延迟及不确定度，
没有 false path 或 multicycle path。新目录为
`$PDS_WORK_ROOT/work/npu128-timing-20261024a`；只限制本任务进程树，单线程，
2 GiB Job 提交上限，物理与提交余量均保留 6 GiB。

第一轮结果保存在 [地址/MAC 流水综合](../reports/npu128-timing-address-synthesis.json)：
93.9496 MHz，setup WNS −0.644 ns，64 个失败端点；NPU 5,515 LUT、5,407 FF、
82 APM、94.5 等效 DRM36K。仍未通过 100 MHz。最终后处理版本使用新的
`$PDS_WORK_ROOT/work/npu128-timing-20261024b`，不覆盖第一轮。

## 最终独立综合结果

证据见 [最终综合报告](../reports/npu128-timing-synthesis.json)。相同器件、工具版本及
10 ns 约束下的比较如下，资源指 NPU 本体，不含串行测量壳：

| 指标 | 旧 128 MAC | 最终流水版 |
| --- | ---: | ---: |
| 综合估计 Fmax / MHz | 71.8804 | 124.6883 |
| 100 MHz setup WNS / ns | −3.912 | +1.980 |
| setup 失败端点 | 1,024 | 0 |
| LUT | 5,398 | 5,259 |
| FF | 3,597 | 5,961 |
| APM | 83 | 82 |
| 等效 DRM36K | 94.5 | 94.5 |

Fmax 估计提升约 73.5%，换取 2,364 个 FF；LUT 减少 139、APM 减少 1。
MAC 仍使用 64 APM，地址乘法由 3 APM 减到 2，后处理仍为 16 APM。
NPU 占器件约 3.29% LUT、1.87% FF、11.08% APM、22.77% DRM。
完整测量顶层为 5,674 LUT、10,203 FF、82 APM、94.5 DRM。

最终最差 setup 路径是测量壳的 `snapshot_q[812] → scan_dout`，
不能把其 124.6883 MHz 估计当作 NPU 内核已经实测的上限。
检查项没有缺失时钟或输入/输出延迟、组合环路或锁存器。
综合 hold 估计仍有负余量，须由物理实现修复并复核；setup 通过不代表全时序已收敛。
本轮尚无布局布线的 setup/hold、未布通数量或板上频率，未生成下载文件。

固定 100 MHz 时，`Cin=8,K=1` 的纯卷积无背压像素耗时由旧版估计 125.2 ns
变为 160 ns；`Cin=768,K=3` 由约 12.13 us 变为 8.79 us。
这些是用周期公式与频率计算的示例，不含后处理和搬运，也不是实测端到端数据。
它说明后续应优先处理短卷积的逐像素排空，而不能只看峰值 MAC 或频率。

```bash
python3 tools/pango/stage.py --work-root "$PDS_WORK_ROOT" --name npu128-timing-new
# Windows PowerShell 执行新目录中的 tools/pango/run_synthesis.ps1，指定 PDS_HOME。
python3 tools/pango/collect_report.py --project "$PROJECT" --output build/timing/synthesis.json
```

厂商网表功能验证入口如下；`PDS_HOME` 在 Linux 中应为安装目录的 `/mnt/...` 路径。
本机 Icarus 11.0 仅解包在 `build/tools/iverilog/root/`，按 [NPU 说明](NPU.md) 追加
`--iverilog`、`--vvp`、`--ivl-base` 指向该副本即可：

```bash
python3 tools/pango/check_netlist.py --project "$PROJECT" --pds-home "$PDS_HOME" \
  --build-dir build/timing/netlist \
  --rtl-executable build/timing/npu-final/measure/Vnpu_measure_top --engine iverilog
python3 tools/build_image_sim.py
python3 tb/check_image_backend.py --output build/timing/image-backend.json
```

图片后端测试还需 NumPy。本轮在 `build/tools/numpy/` 本地解包 Ubuntu 的 NumPy 1.21.5
和所需动态库，通过 `PYTHONPATH`/`LD_LIBRARY_PATH` 使用；没有更改系统 Python 或库。

板卡尚未连接。本轮未执行完整定点 YOLOX 精度评估、CPU/DDR 集成或下载。
