# adamgallas 128 MAC 卷积后端

> 当前双 bank 接口见 [PREFETCH128.md](PREFETCH128.md)，连续像素周期契约见 [THROUGHPUT128.md](THROUGHPUT128.md)。

> 2026-10-24：`fpga/npu128-timing` 的地址与 MAC 流水已调整；本文件以下保留旧版说明。
> 该流水化阶段每像素固定开销为 15 拍（旧版 8 拍），后续已由连续像素调度替代；历史验证见 [128 MAC 时序优化](TIMING128.md)。

2026-10-23，用户确定采用 adamgallas、128 个并行 MAC，先通过 Verilator。
本次完成的是**可装载 tile、独立执行卷积并返回 INT32 结果的计算后端**。
它尚未连接 AXI/DDR/UART。现已通过 [图片联合仿真](IMAGE_SIM.md) 执行完整 YOLOX-Tiny 图，
其中所有卷积由本后端执行，其余算子及调度由电脑执行。

初始实现固定在提交 `ccf9172`，后续 128 MAC NPU 对照保留在 `fpga/npu128-bringup`。
初始阶段分支已移除，提交仍可从 `main` 历史访问。上游固定为
`6ba312bd220e67a987d3417bacd030db30b15770`。[来源说明](../third_party/adamgallas/README.md)
和完整 Apache-2.0 许可随代码保留。没有修改 hgb、上级三个文档或 AGENTS.md，
没有启动、停止或修改 PDS 综合与布线。

## 128 MAC 的实现

上游将 3×3 空间位置、8 个输入通道和 8 个输出通道展开，共 576 个 INT8 乘法；
不存在可直接选用的 128 配置。当前保留两路输出权重共享一项输入的结构，改为
8 个输入通道×16 个输出通道，并将 3×3 的九个 tap 分时调度。
Verilator 展开结果为 64 个 `adam_mul_pair`，每个两路乘法，合计 128。
这个数字是 MAC 数据通路的乘法数量，不是 Pango APM 消耗。

| 模块 | 实现 |
| --- | --- |
| `adam_mul_pair` | 两个 signed INT8 乘法；四拍延迟与上游一致；不再要求 27 位乘法端口 |
| `adam_mac128` | 64 对乘法、完整 19 位八项求和、INT32 符号扩展；valid/last 流水 |
| `adam_sdp_ram` | 单写口、同步读口、读使能、同址 read-first；无 Xilinx 原语或隐式初始化 |
| `adam_conv128` | tile 缓存、卷积取数、INT32 bias/累加、命令检查、输出背压和性能计数 |

原版的 18 位跨通道累加改为 INT32。signed INT8 的最坏正乘积为 16,384，
3×3×768 的卷积和可到 113,246,208，18 位不足以表达。加上 bias 后超出 INT32 的情况
定义为二进制补码模 2³² 回绕；测试包含该情况。正式量化导出仍需限制数值范围。
这里没有隐式 requant、饱和、SiLU 或 zero-point 校正。

乘法端口已缩小到 8×8，但 Pango 是否映射 APM、RAM 如何拆分以及 Fmax，
仍须单独进行 PDS 检查。没有沿用上游的 296 DSP、250 MHz 或 YOLOv3-tiny 帧率。

## 配置与缓存

默认 `MAX_SIDE=16`、`MAX_CIN=768`，覆盖官方 Tiny 图中最大 768 输入通道。
每条命令计算输入 tile 对应的所有输出像素、1–16 个输出通道。
更大的图像按带 halo 的 tile 分块，更多输出通道分为最多 16 通道一组。

| 项目 | 默认限制 |
| --- | --- |
| 输入宽、高 | 各 1–16 |
| 输入通道 | 1–768；最后一个 8 通道组由硬件屏蔽尾部 |
| 输出通道 | 1–16；无效输出 lane 为零，并给出 mask |
| 卷积 | 1×1 或 3×3；stride 1 或 2 |
| Padding | 3×3 可选 0/1；1×1 只允许 0 |
| 输入缓存 | 24,576×64 bit，192 KiB |
| 权重缓存 | 864×1024 bit，108 KiB |
| 数据存储合计 | 300 KiB，不含小型寄存器；不是综合后的 DRM 用量 |

缓存宽字将由实现工具拆分成物理 RAM。1024 位权重装载口是计算后端的本地口，
需要后续 DMA/组包逻辑适配 SoC 的 256 位 AXI，不是现成的 DDR 接口。

令 `G=ceil(Cin/8)`，输入 layout 为按像素排列的通道组：

```text
act_word_addr = (y * width + x) * G + group
act_word[lane*8 +: 8] = signed input[y][x][group*8 + lane]

weight_word_addr = (ky * kernel + kx) * G + group
weight_word[(oc*8 + lane)*8 +: 8] = signed weight[oc][group*8 + lane][ky][kx]
```

组尾的无效输入/权重字节可以填任意值，硬件会屏蔽；测试故意使用非零垃圾值。
输入必须是已经量化的 signed INT8，不能直接把原始 BGR 字节重新解释为有符号数。
padding 使用这个数值域的零，当前后端不支持非零 activation zero-point。
测试中的跨 tile 参考在完整图像上独立计算，覆盖 halo、奇数尺寸、stride 和 33 输出通道。

## 命令与流控

1. 空闲时，通过 `act_wr_valid/ready` 和 `weight_wr_valid/ready` 装载缓存。
   地址以整个宽字为单位；越界地址 `ready=0`。缓存内容必须由调用方先加载。
2. 写端优先。任一写 valid 拉高时 `cmd_ready=0`；忙时写端和命令端均不接收。
3. `cmd_valid && cmd_ready` 锁存形状、kernel/stride/padding 和 16 个 INT32 bias。
   非法形状产生一拍 `cmd_error`，保持空闲，不读取缓存、不输出结果。
4. 输出按照 y、x 顺序，每拍携带 16 个 INT32 lane、坐标、mask 和 last。
   `out_valid && !out_ready` 时数据及元信息保持；只有握手才前进。
5. 最后一个像素交付后产生一拍 `done`。同步高有效复位丢弃在途任务和 valid 流水，
   缓存不清零；调用方决定复用或重新加载内容。

`perf_mac_steps` 记录实际发出的 128-lane 向量数，包括边界补零和通道尾部。
`perf_active_cycles` 从命令接受后的第一拍计到最后结果交付，包含背压。
令 `N=G*K*K`、`P=输出像素数`，无复位的一条命令有：

```text
perf_mac_steps     = P * N
perf_active_cycles = P * (N + 8) + 输出背压拍数
```

当前逐像素排空流水再开始下一像素；8 拍是准备、尾部排空和结果交付的开销。
这是仿真验证的周期关系，不包含缓存装载、DDR/UART 或软件时间，未给出板上帧率。
`out_*` 仅在 `out_valid=1` 时有效；`done` 表示结果已经被接收。

## Verilator 验证

环境为 Verilator 5.052、g++、Python 3，不依赖 PyTorch、Vivado 或 PDS。

```bash
make lint
make test
# 指定新的构建目录，保留之前的日志：
make test ADAM_BUILD=build/adam128/recheck
# 单独验证内存保护，最高只申请 32 MiB 实际数据：
python3 tb/check_memory_guard.py
```

`make test` 现在执行当前 adamgallas 主线。原有自研 systolic tile 独立保留，
可用 `make tile-test` 运行，默认也受内存保护；本次没有修改或重跑其 404 项原始回归。

| 验证 | 结果 |
| --- | --- |
| 上游与移植乘法逐拍比较 | 16,777,216 个全部 INT8 三元组，与普通整数乘法也一致；四拍延迟一致 |
| 上游与移植 RAM | 1,024 项，覆盖同址读写、读使能关闭后的保持 |
| 卷积 | 110 个任务、6,270 个像素、每像素检查全部 16 lane |
| 分块 | 4 种完整 29×23 图像的空间/输出通道分块，与未分块参考比较 |
| 边界与数值 | Cin=1/3/7/8/9/17/24/31/48/96/192/384/768，最大缓存、极值、零、bias 回绕、非零尾块垃圾 |
| 协议 | 装载间隙、写/命令冲突、忙时请求、输出背压；9 个非法描述符和越界写拒绝 |
| 复位 | 8 个中途/结果等待复位及恢复；无旧结果泄漏 |
| 周期和结构 | 每条命令计数符合上式；64 对×2=128 路乘法 |

本项目 RTL 严格 `-Wall` 通过。仅对原样保留的上游测试文件使用局部 waiver：
文件名与模块名差异、未使用的 packed 位/属性参数、缺少 timescale。
没有对移植 RTL 关闭警告致错。原始日志及命令位于 `build/adam128/final/`，
源码哈希与结果见 [adam128-validation.json](../reports/adam128-validation.json)。
模块没有 XLEN 分支；本次未接入 hgb，因此没有启动 hgb 的 RV32/RV64 回归。

## 内存约束与当前边界

构建和执行串行进行，`-j1`、仿真单线程、单 CPU affinity、nice 19、无波形、禁用 CCache。
每个子进程的地址空间硬上限为 1 GiB；每 50 ms 采样自身进程组 RSS，超过 512 MiB、
Linux 可用内存低于 2 GiB 或执行超时，只停止新建的自身进程组。
RSS 是采样阈值，可能短暂超过；它不是 cgroup 硬上限，也不能代表 Windows 可用内存。

本轮全新构建的进程组采样峰值 **350.6 MiB**，卷积仿真峰值 **3.9 MiB**。
运行前、结束后分别只读查询到 Windows 可用物理内存约 17.3 / 16.5 GiB。
没有更改系统分页、PDS 参数，
也没有操作其他任务的进程。内存保护已用小型子进程验证限额、主动停止和超时行为。

后续仍需：INT8 校准与 BN 折叠、requant/SiLU/SPP/分支算子、模型描述符与缓存分配、
AXI DMA/CSR、软核驱动与 Cache 交接、UART 静态图链路。整图的 CPU/RTL 联合仿真见
[图片仿真说明](IMAGE_SIM.md)；完整板端推理和 Pango RAM/APM 推断、资源、时序、
上板验证均未完成。本页的原始回归结果只覆盖上述计算后端。
