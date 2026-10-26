# 128 MAC 独立布局布线（2026-10-24）

后续连续像素调度改动及新的物理验证见 [THROUGHPUT128.md](THROUGHPUT128.md)；本页保留原物理基线。

在 `fpga/npu128-timing` 的 `7b9e894` 基础上验证物理实现，计算 RTL 不变。
本轮只针对 `npu_measure_top` 测量壳内的 NPU，不连接板卡、不下载，也不生成位流。
上一轮综合与数值验证见 [TIMING128.md](TIMING128.md)。

## 固定条件

- PDS 2025.2 build 211867，PG2L200H-6IFBB676。
- `MAX_SIDE=16`、`MAX_CIN=768`、128 INT8 MAC、四路定点后处理。
- 原 10 ns 时钟、setup uncertainty 0.2 ns、hold uncertainty 0.05 ns。
- 所有非时钟输入及输出的外部 delay 仍为 0–1 ns；FDC 字节与原综合一致。
- 无 false path、multicycle path 或降低频率的约束修改。
- 单线程；本工程 Windows Job 提交内存上限 3 GiB，物理和提交余量至少各 6 GiB。
- IO 由工具为独立诊断工程自动布局；没有板级 pinout、PLL、CPU、DDR 或 CDC 集成结论。

## 工程及来源

`tools/pango/stage_implementation.py` 核对原综合 guard、staging 文件哈希、当前 RTL，
并要求原综合网表与已保存的厂商四态功能 PASS 对应。每次使用新的原生 Windows 目录。
工程保存原网表、综合 manifest、guard 和功能验证作为参照。

首次 `$PDS_WORK_ROOT/work/npu128-route-20261024a` 尝试直接以 VM 运行后端。
PDS 在加入原 FDC 时以 `Flow-0150` 拒绝，未进入映射或布局；没有产生时序结果。
该目录和错误日志保留。

随后 `$PDS_WORK_ROOT/work/npu128-route-20261024b` 原样复用已通过的 ADS 综合 Tcl，
再接器件映射、布局、单线程布线、启用 hold 修复及最终时序报告。
这样不需要转换约束格式；重跑综合的网表需要与原功能验证网表核对。

重跑网表核对通过：仅去除生成时间及 SDC 约束来源路径两类注释后，
其余文本逐字相同，规范化 SHA-256 为
`98686ff318c41f9f59917cad09002ae4104133fe909ef52c519e7571acd4302a`。
FDC 字节与原综合完全一致，器件映射的实际约束报告保留了全部 15 条命令，
并使用 `clkbufg_0` 全局时钟缓冲。因此没有因后端接口问题丢失原时序约束。

```text
compile → synthesize → dev_map → place → route
  → route_optimize -fix_hold_violation_in_route TRUE → report_timing
```

`tools/pango/run_synthesis.ps1` 现在也支持 implementation manifest。
原综合默认路径不变，物理工程使用 `implement.tcl` 和 `implementation.log`。
保护器仍只管理自己创建的进程树，拒绝覆盖旧结果，启动前检查其他 PDS 任务与内存余量。
正常进程退出和全部流程完成标记只说明工具执行完毕；时序是否收敛由最终报告另行判断。

## 最终结果

[物理实现证据](../reports/npu128-route-implementation.json) 的结论为 **PASS**。
最终物理数据库为 `prj_tasks/pnr_1/place_route/npu_measure_top_pnr.adf`，
报告来自 `prj_tasks/pnr_1/report_timing/report_db/`，不是综合估计。

| 分析 | Fast | Slow |
| --- | ---: | ---: |
| setup WNS / ns | +3.076 | +0.208 |
| setup TNS / ns、失败端点 | 0、0 | 0、0 |
| hold WHS / ns | +0.036 | +0.043 |
| hold THS / ns、失败端点 | 0、0 | 0、0 |
| 最小脉宽余量 / ns | +4.345 | +4.040 |
| 脉宽失败端点 | 0 | 0 |

setup/hold 每组覆盖 36,541 个端点，脉宽每组 6,703 个端点。
布线末次显式统计为 `Unrouted nets at iteration 38 (0.220 sec): 0`，
随后 route optimize 完成，保存最终 PNR 数据库。没有时钟经普通布线资源的 `Route-2036` 告警。
检查项的无时钟、缺失/部分输入输出延迟、延迟一致性、不可展开时钟、组合环和锁存器均为零。
`virtual_clock=1` 的详细文字是 `No virtual clock was found.`，是该工具的信息项，不是一个未约束端点。

布线后工具报告 Fmax **102.1242 MHz**，请求频率 **100 MHz**。
这只证明这份自动 IO 测量工程在原约束下的 100 MHz STA 通过，尚非板上实测频率。

### 测量壳与内核路径

- 最差 setup：`snapshot_q[812] → scan_dout`，Slow 余量 +0.208 ns，属于测量壳输出。
- 最差 hold：`scan_update → drive_q[1467]/CE`，Fast 余量 +0.036 ns，属于测量壳输入控制。
- 已报告路径中最差 NPU 内部 setup：激活 RAM `mem_54/CLKB[0]` 到 MAC 的 APM `Y[15]`，
  Slow 余量 +2.327 ns。数据延迟 7.019 ns，其中布线 5.023 ns，约 71.6%。

因此，不能把测量壳的 Fmax 直接当成 NPU 内部的频率上限，也不能从内部一条路径的余量
外推更高频率已经收敛。更换测量接口或接入 CPU/DMA/PLL 后必须重做物理时序分析。

### 物理资源与运行保护

以下为 hold 修复后 `route_opt_resource_utilization.db` 的实际资源：

| 范围 | LUT | FF | APM | 等效 DRM36K |
| --- | ---: | ---: | ---: | ---: |
| 测量顶层合计 | 5,688 | 10,203 | 82 | 94.5 |
| NPU 本体 | 5,268 | 5,961 | 82 | 94.5 |
| 卷积及缓存 | 3,596 | 3,849 | 66 | 92.5 |
| 定点后处理 | 1,585 | 2,043 | 16 | 2 |

相对上一轮综合，NPU LUT 增加 9，其余三项不变；128 个 INT8 乘法仍保留。
全流程保护器耗时约 1,029 秒（17 分 9 秒），Job 提交内存峰值 2,118.2 MiB，
最低可用物理内存约 10.66 GiB，最低提交余量约 12.88 GiB，均满足预设保护。

计算 RTL 未变化；重跑综合的逻辑文本核对通过，因此复用上一轮的数值与厂商网表功能验证。
本轮额外验证了证据解析器：拒绝仅凭工具成功标记推断布通，使用最后一次显式未布通计数，
且不会把实际逻辑变化当作元数据忽略。尚未执行布线后 SDF 回放、SoC 联合验证或上板。

## 复现

在 Linux/WSL 中设置 `PDS_WORK_ROOT`，源综合目录应为上一轮有效目录：

```bash
python3 tools/pango/stage_implementation.py \
  --synthesis-project "$PDS_WORK_ROOT/work/npu128-timing-20261024b" \
  --work-root "$PDS_WORK_ROOT" --name npu128-route-new
```

若原综合是新复现的网表，使用 `--netlist-validation <该网表的功能验证 summary.json>`
指定相应的厂商门级 PASS 文件，不需要覆盖历史报告。

在 Windows PowerShell 中，`$project` 为新工程的 Windows 路径：

```powershell
& "$project\tools\pango\run_synthesis.ps1" -ProjectRoot $project -PdsHome $env:PDS_HOME -JobLimitGiB 3 -ReserveGiB 6 -TimeoutSeconds 1800
```

完成后在 Linux/WSL 收集证据，并运行解析器检查：

```bash
python3 tools/pango/collect_implementation.py --project "$PROJECT" \
  --output build/implementation/route-new.json
python3 tb/check_implementation_report.py
```

收集器核对 Fast/Slow 的 setup、hold、最小脉宽和约束覆盖，并独立判断闭合状态。
若保留失败尝试，可追加 `--rejected-project <失败工程目录>`；输出文件必须为新文件。

实际上板需另外提供板级引脚、时钟和接口约束。
