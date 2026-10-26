# YOLO-accelerate

> 当前 `fpga/npu128-uart-bulk` 已实现 128 字节权重批量帧，最终功能、隔离反例、四态网表和
> 100 MHz 布线通过（setup +0.707 ns、hold +0.039 ns）。新位流 `12810002` JTAG 下载/回读通过，
> 同位流旧/批量模式三组卷积均通过、零重传：19.091 → 11.401 秒；权重有效吞吐约 6.97 倍，
> 见 [UART_BULK128.md](docs/UART_BULK128.md)。
>
> 2026-10-26 前一阶段 `fpga/npu128-uart-host` 完成主机收包优化，同位流三组卷积实测
> **77.672 → 18.828 秒（约 4.13 倍）**，2,759 次事务、零重传，输出逐值一致。
> RTL/100 MHz 位流未变，完整记录见 [UART_HOST128.md](docs/UART_HOST128.md)。
>
> 前一硬件阶段 `fpga/npu128-mmio-timing` 已将完整板级入口提升至真实 PLL 100 MHz：
> 分级 MMIO 21 任务/674 向量、100 MHz 分频的完整 UART 引脚回归、厂商 PLL 锁定/复位测试、
> 新综合网表 103 帧四态回放均通过。完整顶层 100 MHz 布线 setup +0.805 ns、hold +0.039 ns、未布通 0；
> 新位流 JTAG 下载/回读及 COM11 三组卷积实测通过：12 向量、384 个 RAW/INT8 值一致，
> 2,759 次事务、零重传，见 [MMIO_TIMING128.md](docs/MMIO_TIMING128.md)。
>
> 2026-10-24 前一阶段 `fpga/npu128-uart` 已增加真实管脚顶层、UART→MMIO 桥和 Python 主机客户端。
> 完整引脚 RTL 仿真、字节级 GTP 网表回放及 27 MHz 真实管脚 PDS 通过；实际 JTAG 下载/回读和 CP2102 COM11 三组卷积自检通过：12 向量、384 个 RAW/INT8 值一致，见 [BOARD128.md](docs/BOARD128.md)。
> 计算核保持 [RAM 优化版本](docs/RAM128.md) 原样：126.5 DRM、独立 100 MHz setup/hold 通过。下文保留各阶段分支对照。

RISC-V 控制的静态图像目标检测加速器。电脑传入图片，软核调度 FPGA 卷积计算，
再返回类别、置信度与检测框。当前以 **YOLOX-Tiny 为主、Nano 为备选**，
当前硬件主线采用 **adamgallas 原始 576 路 INT8 卷积数据通路**，不使用摄像头。

本仓库独立开发；不修改上级三个文档、任何 `AGENTS.md` 或 `hgb-aisystem_riscv`。
共同开发基线为 `main`，当前完整上游实现来自 `fpga/adam-upstream-faithful`；
128 MAC 实现保留在 `fpga/npu128-bringup`。后续功能从 `main` 新建分支，通过 PR 合入。
原图片仿真固定在
`baseline/adam128-image-cosim-20261023`（`658b547`）。

仓库保留根目录的 [MIT License](LICENSE)。第三方代码继续保留各自许可证，
其中 adamgallas 源码的 Apache-2.0 许可见 [第三方 LICENSE](third_party/adamgallas/LICENSE)。

接手开发先读 [交接文档](docs/HANDOFF.md)：源码来源与版本边界、完整卷积架构、RV32IM/RT-Thread
分工、DMA 与计算重叠的条件、已有验证和后续顺序。本次剩余综合、布局布线与上板工作暂缓。

## 分支与比较入口

| 分支 | 用途 | 状态 |
| --- | --- | --- |
| `main` | 共同开发与文档入口，采用完整上游 576 路 INT8 卷积通路 | 新功能从这里创建分支，经 PR 合入 |
| `fpga/adam-upstream-faithful` | 完整上游移植的固定对照，源码与接口说明见 `docs/UPSTREAM.md` | 保留 `948c74f`；卷积仿真通过，暂无有效 Pango 综合结果 |
| `fpga/npu128-bringup` | 128 MAC、INT32 累加及定点量化/激活的对照实现 | 保留 `f0134a0`；已有独立综合，100 MHz 尚未收敛 |
| `fpga/npu128-timing` | 128 MAC 地址、加法树及量化流水优化 | 独立布局布线 100 MHz 通过：setup +0.208 ns、hold +0.036 ns、未布通 0；见 `docs/IMPLEMENTATION128.md` |
| `fpga/npu128-throughput` | 128 MAC 连续像素发射、两项结果队列及 credit 流控 | 100 MHz 布线通过：setup +0.243 ns、hold +0.036 ns；Tiny 416 加权周期减少 24.82%，NPU 增加 544 FF；见 `docs/THROUGHPUT128.md` |
| `fpga/npu128-prefetch` | 双 bank 激活缓存，装载与计算重叠，图片服务已使用 | 100 MHz 布线通过：setup +0.193 ns、hold +0.037 ns；总周期进一步减少 18.31%，RAM 158.5 DRM36K；见 `docs/PREFETCH128.md` |
| `fpga/npu128-ramcompact` | 每个激活 bank 分为三段 8K 字，保持容量和预装载 | 100 MHz 布线 setup +0.214 ns、hold +0.037 ns、未布通 0；126.5 DRM，比预装载基线节省 32，功能与周期等价通过，见 `docs/RAM128.md` |
| `fpga/npu128-mmio` | 控制寄存器、激活/权重暂存提交、单向量读回槽和 C11 传输接口 | 19 任务、672 向量、269,859 次公开总线事务通过；外围 PDS/上板待做，见 `docs/MMIO128.md` |
| `fpga/npu128-uart` | 板载 27 MHz、真实 UART/KEY0 管脚、CRC 停等桥与 Python 客户端 | 引脚仿真及字节级门级回放通过；27 MHz PDS setup +20.614 ns、hold +0.039 ns、未布通 0；126.5 DRM，见 `docs/UART128.md` |
| `fpga/npu128-mmio-timing` | MMIO 请求/提交流水及真实 27→100 MHz GPLL 板级入口 | 完整顶层 100 MHz setup +0.805 ns、hold +0.039 ns；6,946 LUT / 9,625 FF / 82 APM / 126.5 DRM；JTAG 与 COM11 三组卷积实测通过，见 `docs/MMIO_TIMING128.md` |
| `fpga/npu128-uart-host` | 固定帧收包与同位流吞吐测量 | 三组卷积 77.672 → 18.828 秒，2,759 笔零重传；RTL/位流不变，见 `docs/UART_HOST128.md` |
| `fpga/npu128-uart-bulk` | 完整权重字批量写、全帧 CRC 与缓存去重、帧错隔离 | 功能/网表及 100 MHz setup +0.707 ns、hold +0.039 ns 通过；JTAG 12810002 和实板旧/批量模式均通过；2,759 → 1,538 笔，完整流程约 1.67 倍，见 `docs/UART_BULK128.md` |

早期 Nano 评估、候选选型、128 MAC 初始移植和图片仿真的阶段分支已移除，相关提交仍在
`main` 历史中。图片复现使用标签 `baseline/adam128-image-cosim-20261023`（`658b547`）。
比较两种硬件实现时分别切换上述保留分支；两者的接口和累加位宽不同，测试入口以各分支文档为准。

## 当前上游移植

`rtl/upstream/adam_upstream.sv` 直接例化原 `Accel_Conv`，保留全部 50 个上游 RTL 文件的
原始字节、参数、寄存器接口、行缓存、576 路乘法、加法树和 18 位累加/量化。
软件适配原接口的数据布局和任务顺序，见 [上游移植说明](docs/UPSTREAM.md)。
Verilator 已通过原 CSR/AXIS 端到端卷积：24 帧、186,144 个输出值，覆盖六种行宽、
8/16 输入通道、原版回绕/饱和及 ReLU/查表。`make test` 运行此项测试。
完整上游的 PDS 综合尚未完成：1/2 GiB 配额边界附近退出，3 GiB 尝试被内存预检拦下；
暂没有本版本的资源或时序结果，详见 [综合尝试记录](reports/upstream-synthesis-attempts.json)。
这条路径尚未接入完整 YOLOX 图片推理或 RISC-V SoC；原图片命令仍使用下述 128 MAC 参考。

## 128 MAC 参考实现

2026-10-23：已完成 8 输入通道×16 输出通道的 128 MAC 后端与 Verilator 验证。
上游 27×18 打包乘法已改为两路 signed 8×8，四拍延迟与全部输入组合逐项一致；
新增 tile 缓存、1×1/3×3、stride/padding、INT32 累加及背压控制。
[实现与接口](docs/ADAM128.md) 给出数据布局、分块、数值、验证和内存限制。
[选型报告](docs/ACCELERATOR_SELECTION.md) 保留模型和板卡评估，
[移植约束](docs/PORTING.md) 保留完整 YOLOX、SoC 与 UART 的后续工作。

新增 `npu_conv128`：四路逐通道 INT32→INT8 重新量化、可装载 SiLU 的激活表、
最终输出交付后的完成握手与周期计数。图调度继续交给 CPU。
[NPU 接口与综合评估](docs/NPU.md) 记录定点规则、配置协议、测试与独立 PDS 工程。
Windows PDS 综合测得 NPU 本体 **5,398 LUT、3,597 FF、83 APM、94.5 等效 DRM36K**；
100 MHz 约束下估算 Fmax **71.88 MHz**、setup WNS **−3.912 ns**，尚未时序收敛。
最差路径位于激活 RAM 地址计算；尚无布局布线或上板结果。
NPU 的 Verilator 回归与 GTP 网表功能核对均通过；门级核对包含 432 次事务和 105 份快照，
通过直接装载测量寄存器省去串行空拍，范围见 [网表验证记录](reports/npu128-netlist-validation.json)。

已接通 **图片 → YOLOX-Tiny 全部 83 层 RTL 卷积 → 带框图片** 的联合仿真。
SiLU、池化、图调度、量化/反量化、框解码及 NMS 在电脑上执行；
[图片仿真说明](docs/IMAGE_SIM.md) 给出运行命令、数值路径和软硬件边界。

| 已完成 | 结果 |
| --- | --- |
| 官方 YOLOX-Nano CPU FP32 参考 | 上游 dog.jpg 检出 3 个目标；保存权重/图片哈希与检测框 |
| 官方 YOLOX-Tiny CPU FP32 参考 | 上游 dog.jpg 的标签为 bicycle、car、cat；狗被模型误判为 cat，JSON 中为 class_id=15 |
| Tiny 图片联合仿真 | 416 输入、83 层 RTL 卷积、9,760,257 个 INT32 输出全部匹配独立参考，生成 3 个带标签检测框 |
| 两个官方计算图实际前向分析 | Tiny：5,055,855 参数，416 输入约 3.206G MAC，INT8 卷积权重约 4.805 MiB；Nano 约 0.523G MAC、0.855 MiB |
| adamgallas 算术移植 | 16,777,216 个全部 INT8 三元组与原版及整数参考逐拍一致；RAM 1,024 项通过 |
| 128 MAC 卷积后端 | 110 个任务、6,270 个输出像素；最大 768 输入通道、四种整图分块拼接通过 |
| 协议与结构 | 背压、非法命令、8 次复位恢复、执行周期检查通过；展开结构确认为 128 路乘法 |
| Verilator 严格 lint | 移植 RTL `-Wall` 通过；上游对照文件仅使用局部 waiver |
| 内存 | 原后端构建采样峰值 350.6 MiB、单独卷积仿真 3.9 MiB；416/320 图片联合仿真为 745.4/746.9 MiB，单 CPU 运行 |

源码哈希、实际命令和原始日志位置见 [128 MAC 验证报告](reports/adam128-validation.json)。

图片联合仿真仍使用 INT32 卷积结果，通过电脑上的动态 INT8 量化和 FP32 剩余算子衔接；
新增的定点后处理已经单独验证，尚未接入图片路径或完成 YOLOX 静态量化校准。
旧 128 MAC 图片路径没有 DMA/AXI 控制口或 UART 固件。当前完整上游虽然保留原生 AXI-Lite/AXIS，
也尚未接入 hgb、未上板。后续以 [交接文档](docs/HANDOFF.md) 中的完整上游适配顺序为准。
原有自研 systolic tile 及其 404 项历史 GEMM 验证保留作独立参考。

## 运行

已有 PyTorch/torchvision 环境、固定源码与权重时，一条命令输出带框图片：

```bash
PYTHONPATH=build/python-deps make image-detect TORCH_PYTHON=python \
  IMAGE=build/upstream/YOLOX/assets/dog.jpg OUTPUT=build/dog-adam128.png
```

将 `IMAGE` 换为自己的图片即可；默认 416 输入，也支持 `SIZE=320`。
图片旁的 JSON 记录每层校验和检测框，运行日志与内存统计也保留在 `build/`。
见 [图片仿真说明](docs/IMAGE_SIM.md) 和 [验证记录](reports/image-cosim-validation.json)。
下面的依赖准备部分适用于首次运行。

当前上游回归需要 Python 3、Verilator 5.052（使用 JSON 展开检查）、g++，
以及用于 ABI 检查的 `riscv64-unknown-elf-gcc`（`-march=rv32im -mabi=ilp32`）：

```bash
make lint
make test
# 可选：单独验证内存保护机制
python3 tb/check_memory_guard.py
```

当前上游回归默认日志位于 `build/upstream-faithful/verified/`。
`make adam-test npu-test` 可执行旧后端测试，日志位于 `build/adam128/` 和 `build/npu128/`，
旧后端结果见 [NPU 说明](docs/NPU.md)。
构建固定 `-j1`，只使用一个 CPU，设进程地址空间上限和自身进程组 RSS 监测；默认不生成波形。
超过内存/时间阈值只终止本次子进程组，不操作 hgb/PDS。
`make tile-test` 单独执行旧矩阵 tile 回归。`make clean` 仅删除仿真构建，保留源码、权重与模型分析。

模型分析需要已有 PyTorch/torchvision 环境，额外依赖可仅装入本仓库：

```bash
git clone https://github.com/Megvii-BaseDetection/YOLOX.git build/upstream/YOLOX
git -C build/upstream/YOLOX checkout --detach 6ddff4824372906469a7fae2dc3206c7aa4bbaee
python -m pip install --target build/python-deps --no-deps loguru==0.7.3 tabulate==0.10.0 thop==0.1.1.post2209072238 opencv-python-headless==5.0.0.93 psutil==7.2.2
PYTHONPATH=build/python-deps make audit-both PYTHON=python
```

PyTorch 环境同时需要 numpy、matplotlib、tqdm；本次使用 PyTorch 2.14.0+cu126，
模型运行设备为 CPU。`make audit` 默认 Tiny，可用 `MODEL=yolox-nano` 切换。
`make audit-both` 生成两模型逐层统计及 `build/model-comparison.json`。
分析使用随机权重和零输入，统计形状及 MAC，不测精度。

预训练静态图片参考：

```bash
mkdir -p build/models
curl --fail --location https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.pth --output build/models/yolox_tiny.pth
PYTHONPATH=build/python-deps python tools/reference_detect.py \
  --source build/upstream/YOLOX --model yolox-tiny --weights build/models/yolox_tiny.pth \
  --image build/upstream/YOLOX/assets/dog.jpg --output build/dog-reference-tiny.json
```

[Tiny 来源与哈希](config/yolox-tiny.lock.json)、[模型比较](reports/model-comparison.json)、
[Tiny 检测框](reports/dog-reference-tiny.json) 和 [本轮验证](reports/selection-validation.json) 随源码保留。
[Nano 来源](config/upstream.lock.json)、[原有 RTL/Nano 验证](reports/validation.json)
和 [Nano 检测框](reports/dog-reference.json) 保留作原始基线；第三方源码、权重和原始构建日志
在忽略的 `build/` 中，不纳入提交。下载后须按 lock 核对哈希；脚本强制检查上游提交
及工作树状态，并在输出中记录实际权重和图片哈希。

## 文件

- `rtl/upstream/`：原始完整加速器的参数/端口透传封装，当前硬件主线。
- `software/`：原生寄存器 ABI、任务发起与输入/权重/bias 打包，整数 C 接口。
- `rtl/adam/`：保留的 128 MAC 卷积后端；`rtl/systolic*` 为原有矩阵参考。
- `rtl/npu/`：定点后处理、NPU 封装和独立测量顶层。
- `third_party/adamgallas/`：固定版本的上游原样源码、许可证与逐文件哈希。
- `tb/`：上游算术/RAM 对照、独立标量卷积和分块参考、流控与复位测试。
- `sim/`：通过真实 RTL 端口执行完整特征图卷积的 Verilator 程序。
- `tools/`：RTL 回归、官方模型算子分析、预训练参考和图片联合仿真。
- `tools/pango/`：独立 PDS 工程生成、Windows 内存保护、报告提取与网表功能检查。
- `docs/ACCELERATOR_SELECTION.md`：当前模型/加速器选择、板卡容量与资源门槛。
- `docs/ADAM128.md`：128 MAC 后端实现、接口与 Verilator 验证。
- `docs/IMAGE_SIM.md`：图片输入/画框输出命令、完整图联合仿真与数值边界。
- `docs/NPU.md`：NPU 定点接口、原版 baseline、综合资源与时序限制。
- `docs/PORTING.md`：赛题解释、算子、SoC 接口与原有 tile 契约。
- `config/`、`reports/`：固定来源和本次可核对结果。
