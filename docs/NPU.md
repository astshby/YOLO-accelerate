# NPU 定点后处理与独立 PDS 工程

> 双 bank 分支新增 `act_wr_bank`、`cmd_act_bank`，忙时允许装载非活动 bank；见 [PREFETCH128.md](PREFETCH128.md)。

> 2026-10-24：本文件保留 `fpga/npu128-bringup` 的历史结果。
> `fpga/npu128-timing` 的流水修改与新结果见 [128 MAC 时序优化](TIMING128.md)。

当前分支 `fpga/npu128-bringup` 在 128 MAC 卷积后端增加逐通道重新量化和可编程激活表。
图片联合仿真的 baseline 固定在 `baseline/adam128-image-cosim-20261023`，对应 `658b547`。
图遍历、分块、搬运安排、层间依赖、框解码与 NMS 继续由 CPU 软件负责。
本版没有 CPU/AXI/DMA 集成；计算时仍禁止改写当前 tile 缓存。

## 数据路径与接口

`npu_conv128` 包含原卷积核 `u_conv` 和新后处理 `u_post`。
卷积尺寸、64-bit 输入字、1024-bit 权重字和 512-bit bias 的布局沿用
[ADAM128.md](ADAM128.md)。新增接口如下：

| 接口 | 契约 |
| --- | --- |
| `cmd_mode=0` | INT32 直通；`out_int8` 全零 |
| `cmd_mode=1` | 逐通道 INT32→INT8 重新量化 |
| `cmd_mode=2` | 重新量化后按字节索引 256 项激活表，可装载 SiLU |
| `cmd_mode=3` | 命令握手后返回一拍 `cmd_error`，不启动计算 |
| `cfg_kind=0` | 地址 0–15 写对应输出通道的 UINT31 乘数，最高位必须为 0 |
| `cfg_kind=1` | 地址 0–15 写右移量 0–63，其他数据位必须为 0 |
| `cfg_kind=2` | 低 8 位写 LUT，地址必须顺序从 0 到 255；其他数据位必须为 0 |
| `cfg_kind=3` | 非法，握手后返回一拍 `cfg_error` |
| `lut_ready` | 256 项完成后置位；复位或有效的地址 0 重载使其清零 |
| `out_raw` | 当前向量原始 INT32，三个模式都保留，便于核对 |
| `out_int8` | 16 个 INT8 字节，通道 n 对应 `[n*8 +: 8]`；无效通道恒为 0 |
| `out_mask/x/y/last` | 随同数据保持，只有 `out_valid && out_ready` 才交付 |

配置只在整个 NPU 空闲时接受，配置优先于新命令；装载优先级沿用原卷积核。
乘数复位为 1，右移量复位为 0。LUT RAM 不复位，但复位清除 `lut_ready`；
表未完整装载时，模式 2 命令报错。非法配置不改变有效配置。
LUT 开始有效重载后，旧表不能再用于模式 2。单独使用 `npu_postprocess` 时，
调用方必须检查模式合法性和 `lut_ready`；集成顶层已经完成这项检查。

对每个通道，先计算 `p = signed_int32 * unsigned_31bit_multiplier`。
右移量为 s 时，将 `abs(p)/2^s` 舍入到最近整数，恰好半值时远离零，恢复符号并饱和到
`[-128,127]`。s=0 不加舍入项。模式 2 用该 INT8 的二进制字节索引 LUT，表项也为 INT8。
无效通道在查表后清零，因而 LUT[0] 非零也不会污染尾通道。

后处理有四条乘法流水线，16 通道分四拍发射；操作数选择、乘法、舍入饱和、同步查表
分别有寄存器边界。一次只容纳一个向量，输出背压会逐级阻止卷积交付结果。
`done` 在最后一个后处理向量真正交付后拉高一拍；原卷积核提前结束不代表整个命令完成。
计数器记录 MAC 发射拍、卷积忙周期、总命令周期和卷积等待后处理的拍数。

全部硬件运算使用整数。CPU 可直接写入离线生成的系数和表，无须 F 扩展。
测试中的 SiLU 表采用输入/输出 scale=1/16，只用于验证接口和数值实现。
正式 YOLOX-Tiny 的逐层静态 scale、LUT 和精度校准尚未完成。
当前 `make image-detect` 仍走 baseline 的 INT32 卷积与电脑浮点剩余算子，
没有改为新后处理；本版的半值舍入规则也与该 Python 路径的 ties-to-even 不同。

## 综合兼容修正

ADS 2025.2 首轮网表将 `int'(cmd_cout)` 的窄无符号输入按符号扩展处理：
16 的最高位为 1，第 16 通道因此被裁为常量，通道 0 的掩码逻辑也含 `!cmd_cout[4]`。
该网表及其资源数没有用作有效结果。
新分支在 `adam_conv128` 和后处理的窄无符号转换前显式补零，例如
`int'({1'b0, cmd_cout})`。这覆盖通道计数、padding、stride、地址和分组索引。
原 baseline 标签保留原代码；当前卷积算法、接口和流水拍数不变。
修正后原卷积回归仍为 483,554 拍，第 16 通道在新网表中保留。

## Verilator 验证

```bash
make lint
make test
```

所有编译均为单线程，RTL 测试默认 RSS 上限 512 MiB。各项结果如下：

| 测试 | 结果 |
| --- | --- |
| 后处理 | 1,924 向量、30,784 数值；全部 64 种右移量、256 LUT 地址；8 个非法配置、12 次复位恢复 |
| NPU 卷积衔接 | 33 个 tile、450 个输出像素、三种模式；12 个非法命令、3 次复位恢复；164 拍后处理背压 |
| 串行测量接口 | 432 次事务；全部 16 种输出通道数、三种模式；3×3/stride2/pad1/Ci17；非法命令 |
| 原卷积回归 | 110 个卷积任务、6,270 像素、4 种整图分块；9 个非法描述符、8 次恢复；483,554 拍 |
| 未修改的乘法/RAM | 本轮通过 16,777,216 个 INT8 三元组、1,024 次 RAM 检查；128 路乘法结构检查通过 |

数值参考使用独立标量卷积，以及 `__int128` 的除法/余数舍入。
覆盖 INT32 极值、bias 回绕、乘数边界、饱和、负数半值、尾通道和输出长期背压。
详细哈希、运行命令与日志位置见 `reports/npu128-validation.json`。

综合网表使用 PDS 安装目录的原始 GTP 模型进行四态核对，结果为 **432 次事务、105 份
快照、1,780 个时钟全部匹配**，包含 51 个卷积命令。门级编译峰值 1,397.1 MiB，
仿真峰值 537.2 MiB，仿真约 371 秒。输入和期望值来自已通过独立数值参考检查的
Verilator 测试；回放 TB 本身也在 Verilator 上通过同样的事务。

为缩短厂商 RAM 模型的运行时间，门级 TB 只强制测量壳的 `input_shift_q`，
跳过串行移位；正常执行 update/apply、NPU 运算、RAM/APM 和 capture。
数据、模式、掩码、坐标、last、ready/error/busy/done 共 685 位逐位匹配，
删去空拍后发生变化的 128 位周期计数器不参与门级比较。原始 830,864 拍串行接口
已经通过 RTL Verilator，完整串行门级回放未完成。
[网表记录](../reports/npu128-netlist-validation.json) 保存网表、GTP 库、输入、TB 与日志哈希。

完整 GTP 网表的 Verilator 编译曾触发 800 MiB 和 2 GiB 上限；用户允许提高限制后，
4 GiB 上限下峰值 3,495.6 MiB，仍触发 600 秒超时。因此最终门级结果采用
Icarus Verilog 11.0，RTL 验证仍用 Verilator 5.052。没有将上述超时记为通过。

## PDS 工程与结果

测试顶层 `npu_measure_top` 通过同步串行移位控制完整输入并读回完整输出，
避免端口过多和常量输入裁剪。1714-bit 输入移位寄存器、输入保持寄存器和 813-bit
输出快照均可观察；输入/输出 MSB 先行，`scan_update` 的下一拍执行一次事务，
`scan_capture` 优先于输出移位。这个顶层用于测量，不包含板卡引脚、PLL 或 CPU。

有效工程：`$PDS_WORK_ROOT/work/npu128-post-20261023b`，本机对应
`F:/pango-project/work/npu128-post-20261023b`。
使用 Windows PDS 2025.2 build 211867，器件 PG2L200H-6IFBB676，单线程。
综合用外部 100 MHz 测量时钟，输入/输出 delay 为 0–1 ns，没有 false path 或 multicycle path。

| 范围 | LUT | FF | APM | 等效 DRM36K |
| --- | ---: | ---: | ---: | ---: |
| NPU 本体 | 5,398 | 3,597 | 83 | 94.5 |
| 其中卷积及缓存 | 3,667 | 2,039 | 67 | 92.5 |
| 其中定点后处理 | 1,644 | 1,489 | 16 | 2 |
| 测试接口增量 | 415 | 4,242 | 0 | 0 |
| 综合顶层合计 | 5,813 | 7,839 | 83 | 94.5 |

128 个 INT8 乘法映射到 64 个 APM，卷积地址计算另用 3 个 APM；四路宽后处理乘法使用
16 个 APM。激活缓存使用 64 DRM36K，权重缓存 28.5，四份 LUT 合计 2。
表中子模块与顶层的差值还包含命令控制和性能计数器。
NPU 本体约占器件 LUT 的 3.38%、APM 的 11.22%、等效 DRM36K 的 22.77%。
与 CPU 共存还需要 SoC 集成及布局布线验证，当前没有 CPU+NPU 整体结果。

100 MHz 下综合估算 Fmax **71.8804 MHz**，setup WNS **−3.912 ns**，hold WHS **−2.523 ns**。
最差路径是 `u_conv/stride2_q` 到激活 RAM 的读地址，包含坐标/地址加法和连续的两级
组合 APM 乘法；报告的前 100 条 setup 路径均进入激活 RAM。
后续优先考虑把地址生成分拍，并同步延迟 valid、padding、mask 和 last。
本次没有按时序结果继续改变流水结构，也没有降低约束来报告通过。
没有执行布局布线，所以 routed Fmax、routed WNS、未布通网络数和板端结果均无数据。

有效综合耗时约 72 秒，Windows Job 报告提交内存峰值 **734.3 MiB**，
运行中最低物理余量约 14.33 GiB、最低提交余量约 9.79 GiB。
启动前检查其他 PDS 任务与内存；本次任务及其子进程总提交上限 2 GiB，
运行中物理/提交分别保留至少 6 GiB。保护机制只结束本次创建的进程树。
[Windows Job 限制定义](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)
说明提交超限的分配会失败。原生小程序已验证限制读回、超限分配拒绝和子进程终止。
这轮保护测试中，Windows 的峰值计数包含过被拒绝的分配尝试，不能据此反推实际驻留内存。

实际共执行两次同配置综合：首轮发现上述转换问题，第二轮验证修正。
两份原始工程和报告均保留，未运行参数扫描或布局布线。
原始日志、网表和报告哈希见 [综合记录](../reports/npu128-synthesis.json)。

## 复现独立综合

设置 `PDS_WORK_ROOT` 为 Windows 工程根目录的 WSL 路径，`PDS_HOME` 为 PDS 安装路径。
本机参考值分别是 `/mnt/f/pango-project` 和 `/mnt/e/pango/PDS_2025.2-ads`。
每次必须换新的 `--name`，脚本拒绝覆盖已有结果。

```bash
python3 tools/pango/stage.py --name npu128-post-new
```

在 Windows PowerShell 中，`PDS_HOME` 使用 Windows 路径，`$project` 指向刚生成的目录：

```powershell
$project = Join-Path $env:PDS_WORK_ROOT 'work\npu128-post-new'
& "$project\tools\pango\run_synthesis.ps1" -ProjectRoot $project -JobLimitGiB 2
```

脚本核对 staging 字节哈希，再运行 `pds_shell.exe`；开始前需要至少
`JobLimitGiB + 6` GiB 的物理和提交余量，默认任务上限为 4 GiB，可收紧至 1–3 GiB。
没有许可证、源文件不一致、已有 PDS 进程或余量不足时直接退出。
输出只在新工程目录中生成，不修改 hgb 或参考文件。
汇总结果并验证当前 RTL 与实际综合字节一致：

```bash
python3 tools/pango/collect_report.py \
  --project "$PDS_WORK_ROOT/work/npu128-post-new" --output build/npu128/synthesis.json
```

综合网表的功能检查使用安装目录的 GTP 仿真库，不复制厂商源码到 Git。
先执行 `make npu-test`，再使用已安装的 Icarus `iverilog` 和 `vvp`：

```bash
python3 tools/pango/check_netlist.py --project "$PDS_WORK_ROOT/work/npu128-post-new" \
  --engine iverilog --build-dir build/npu128/netlist --rss-mib 2048 --as-mib 3072
```

本机把 Ubuntu 的 `iverilog_11.0-1.1_amd64.deb` 解包在忽略的
`build/tools/iverilog/root/`，没有安装系统软件。使用此副本时，额外传入：

```bash
--iverilog build/tools/iverilog/root/usr/bin/iverilog \
--vvp build/tools/iverilog/root/usr/bin/vvp \
--ivl-base build/tools/iverilog/root/usr/lib/x86_64-linux-gnu/ivl
```

若使用不同 RTL 测试目录，传入对应的 `--rtl-executable`；例如本次为
`build/npu128/pango-casts/measure/Vnpu_measure_top`。脚本检查其验证摘要是否对应当前源码。
默认压缩扫描空拍；`--full-pin-replay` 保留所有串行时钟，厂商模型执行很慢。
`--engine verilator` 保留直接编译整份网表的入口，可设置 `--seconds`，本次未完成该方式。
两种入口都有进程内存限制和 6 GiB Linux 余量。
NPU RTL 独立严格 `-Wall` 通过；使用 force 的回放 TB 和厂商模型保留单独的警告日志。
