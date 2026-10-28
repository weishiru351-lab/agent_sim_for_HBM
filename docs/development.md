# 运行与开发

2026-10-28。统一入口 `MemorySearchTask` 冻结负载、硬件和执行约定，仅搜索声明可变的策略。研究判断见[研究定位](research-direction.md)；初学者先读[张量到 DRAM](tensor-to-dram.md)，配置语义见[布局](kv-layout.md)与[地址位教程](dsl-tutorial.md)。

建模沿用**一个完整候选组合多层策略**，不因教学链条有多层就逐层新增 DSL。布局、分配和介质规则仍各自维护约束，统一执行器生成实际操作。后续开发遵守以下约定：

- 负载、硬件和平台开放权限是固定输入，候选只能修改 `vary` 中的组件；同一控制器映射组遵守一致规则。
- 当前直接建模目标内地址，省略 OS 虚拟地址、页表、TLB 和数据缓存。只有这些机制提供待研究的控制点或成本时再增加模型，不把区域偏移改名成虚拟地址。
- 布局、组合/条带、当前分配共同影响最终地址。新增任意分段或实际格式适配时，必须明确消费者能否执行，以及复制/转换成本。
- 评价实际事务、顺序、掩码、状态与所计成本。相同最终位置不代表包含迁移/转换/GC 的两个过程等价；现有规范化不宣称通用跨层等价判定。

## 输入与运行

Python 3.10+ 标准库即可运行。完整示例为 [`memory_search.json`](../examples/memory_search.json)，引用 [`kv_workload.json`](../examples/kv_workload.json) 和 [`memory_hardware.json`](../examples/memory_hardware.json)。引用路径相对搜索配置文件，而非终端目录；也可以将硬件对象和 `workload.data` 内联。

| 搜索配置字段 | 含义与约束 |
| --- | --- |
| `version` | 单资源任务为 2（兼容 v1）；显式拓扑任务为 3。各自保留版本与计分单位 |
| `workload` | `kind=kv_events` 配逻辑事件；`kind=address_trace` 配已确定地址。指定 `file` 或内联 `data` |
| `hardware` | JSON 文件路径或硬件对象；v2 为 DRAM 几何，v3 为下述模组/目标/区域/池/链路 |
| `execution` | KV 为 `serial_groups`；生命周期指定 `allocation=group_first_fit/policy_first_fit`，后者可配迁移；裸 NAND 可启用下述 `nand` 并行模式，详细 SSD 必须显式配置它；单资源可配 `base_address`；轨迹为 `fixed_arrivals` |
| `design_space` | `vary` 指定可变空间；`mapping_family` 默认 linear，也可 permutation；`max_mapping_operations` 默认 8；`layout_options / migration_options / nand_options / gc_options` 限定布局、迁移、NAND 页条带和 GC 规则 |
| `fixed_design` | 可选，仅指定本轮不搜索的组件；默认每个 token 内 dimension 连续、identity 映射 |
| `initial_design` | 可选完整起点，须满足固定组件和全部约束；容量不均时可显式提供可行放置 |
| `evaluation` | 后端、目标及可选模型参数；不接受不支持的组合或无效指标 |
| `budget` | 默认 20 次实际性能评价；唯一任务预算，包含公共起点 |

硬件计数在当前位映射模型下须为正的 2 次幂，总块地址宽度最多 24 bit。`columns` 是每行含多少个 `granule_bytes` 槽位，因此容量由各计数乘积推导；不是实际 HBM 命令列的直接复刻。队列与抽象时延位于 `evaluation.parameters`，搜索操作上限位于 `design_space`，二者不混入硬件几何。

两种负载分别规定“逻辑工作及固定访问组织”和“固定地址流”；不能从丢失对象身份的地址流自动恢复布局搜索。KV 的 `group_by` 是固定执行约定，不是可搜索字段。具体语法、未满状态和提前结束见[负载说明](kv-layout.md#任务固定什么)。

单资源任务的 `layout_options` 为 `{"alignment_bytes":[64,256],"chunk_bytes":[64,256,4096]}` 这样的固定白名单；省略时两项均只有硬件传输粒度。每项非空、无重复、为粒度整数倍且不大于容量；固定基址须满足所列对齐。拓扑任务必须显式配置白名单和池，见下节。旧 workload v1 转成对象 `V`，旧 layout v1 只在单对象任务中转成拼接；原有计分值保持，协议/后端身份升级。JSON 重复字段被拒绝，不能静默覆盖对象。

```powershell
python -m agent_design_search inspect-task examples/memory_search.json
python -m agent_design_search inspect-workload examples/kv_workload.json
python -m agent_design_search session-init results/my_session --task examples/memory_search.json
python -m agent_design_search session-step results/my_session --proposal examples/kv_layout_proposal.json
python -m agent_design_search session-finish results/my_session
```

初始化评价公共设计并计入预算：默认每个 token 内 dimension 连续、名称顺序、最小对齐、identity 映射、创建时预留及不迁移。Agent 读取 `task.json`、`feedback.json`，提交 `hypothesis`（假设）、`expected`（预测）、`dsl`（完整候选）。KV 任务含 `kv_layout`、存在 DRAM 时的映射，以及启用的分配/迁移/NAND 组件；地址轨迹仅含映射。候选须包含固定组件并保持其规范化写法不变。

拓扑任务默认按最小允许条带轮转到池的全部区域；静态负载默认一个组合组，生命周期负载默认每对象单独一组。若起点无法容纳存活对象，需提供可行 `initial_design`。该字段被冻结并占用一次起点评价，不偷偷做装箱搜索。

单次评价：`python -m agent_design_search evaluate <完整设计.json> --task <search.json>`。设计文件只含 `dsl` 内容，不含提案外壳；此命令不累积跨进程预算，正式探索应使用会话。地址轨迹任务为 [`address_mapping_search.json`](../examples/address_mapping_search.json)，提案见 [`proposal.json`](../examples/proposal.json)；教学文件 `identity.json`、`stride_xor.json` 仅含映射组件，需包装为 `{"dram_address_mapping": ...}`。

加 `--include-events` 可在这一次计分中返回详细来源与计时，不另跑一次不计预算的性能工具。小例子及输出解释见[张量地址教程](tensor-to-dram.md#run-example)。Python 可用 `task.measure(design, include_events=True)`；需要统一验证/预算/缓存时，使用 `Evaluator(task, measure=lambda d: task.measure(d, include_events=True))`。这个可选回调只切换输出细节，不应改变评价语义；默认会话仍只保留普通指标。

`task.json` 保存解析后的完整快照，不再依赖源文件；`state.json` 保存任务摘要、预算与历史，`feedback.json` 提供 Agent 可读信息；结束时保存 `result.json` 并关闭会话，不自动评价测试集。修改冻结快照会被摘要检查拒绝。模型来自外部对话，程序没有模型 API 或自动纠错循环；指令限制也不是文件权限隔离。

### 显式拓扑任务 v3

可运行的八模组配置见 [`module_search.json`](../examples/module_search.json)，完整候选见 [`module_layout_proposal.json`](../examples/module_layout_proposal.json)。硬件对象自身为 `version: 1`，字段如下：

| 硬件字段 | 固定含义 |
| --- | --- |
| `modules` | 模组 ID 列表；模块标签本身不隐含带宽或缓存 |
| `targets` | ID → 资源描述；`dram` 使用 module、geometry、clock_period_ps、mapping_group、links；`nand`、`ssd` 字段见下节 |
| `regions` | ID → `{target, base_address, size_bytes}`；按该 target 粒度对齐、局部不越界，同 target 的区域禁止重叠 |
| `pools` | 名称 → 区域 ID 列表，引用不得重复；不同池可引用同一区域，但每项任务只选择一个池，不相加计容 |
| `links` | ID → `{bytes_per_beat, beat_ps, latency_ps}`；target 的 links 列出从主机到该资源依次经过的 1–8 条链路 |

`layout_options` 必须含 `pool`、`alignment_bytes`、`chunk_bytes`。公共布局粒度取所选资源传输粒度与对象元素大小的最小公倍数，允许尺寸均为它的整数倍；保证资源条带不切开元素，目的 NAND 页仍可能切开元素。对象按公共粒度预留，事务按目的 target 的实际粒度生成。布局语法见[跨资源放置](kv-layout.md#跨资源放置)。不同 target ID 代表不同实际资源，不支持物理设备别名。

映射候选为 `{"version":1,"groups":{"shared":{"version":0,"operations":[]}}}`。组名必须恰好等于硬件声明；同组 target 的几何须相同，所有 target 复用该组策略。独立映射只有在输入已声明不同组时才开放，候选不能增删组或修改归属。

<a id="nand-controller"></a>
### 裸 NAND：页分布策略与合法执行

[`hybrid_search.json`](../examples/hybrid_search.json) 保留原串行控制器；[`nand_mapping_search.json`](../examples/nand_mapping_search.json) 启用页分布 DSL 和并行计时。两者均为任务 v3、布局 v3。NAND target 使用以下固定字段：

| 字段 | 语义 |
| --- | --- |
| `module / medium / links` | medium 为 `nand`；链路通到控制器，沿用模组/共享连接 |
| `geometry` | channels、dies_per_channel、planes_per_die、blocks_per_plane、pages_per_block、page_bytes；均为正整数，不要求 2 次幂 |
| `logical_pages` | 向布局暴露的页数；须小于物理页数，差额作为异地更新空闲页，不额外作为池容量 |
| `management_bytes` | 每 target 独立管理 SRAM 预算；不暗中借用池内 DRAM。至少覆盖元数据和一个整页暂存 |

物理页数为前五项几何乘积，最多 2²⁴；页大小最多 65536 B。区域按页对齐，只能位于 `logical_pages × page_bytes` 内。元数据占用为 `8 × 逻辑页数 + 物理页数 + 4 × 擦除块数` B，另加声明的整页缓冲；这是抽象计费规则，不是实际固件布局或 Python 内存用量。

固定初态全擦除；物理页号解码为 channel/die/plane/block/page，page 最快变化。首次写组装整页，未写字节为空标签；部分更新读旧页、保留其余字节，再编程新页、完成后发布绑定，旧页失效但仍占空间。完整覆盖省掉旧页读取。没有跨请求数据缓存，每页在一次擦除后只能编程一次。

`execution/nand.py` 功能重放 lookup/read/merge/program/bind/erase 操作串，`performance/nand.py` 保持依赖并计时。状态层禁止提前绑定、跳过块内页序编程、擦除活数据、待发布数据或仍有访问的块。不配置 `execution.nand` 时，保留全局物理页递增分配、每 target 一页缓冲和串行计时；该旧模式不发起 GC。

启用新模式需同时配置以下固定约束，并在 `vary` 中允许 `nand_page_mapping` 才可搜索：

```json
"execution": {"kind": "serial_groups", "nand": {"kind": "channel_die", "buffer_pages": 4}},
"design_space": {
  "vary": ["nand_page_mapping"], "nand_options": {"stripe_pages": [1, 2, 4]},
  "layout_options": {"pool": "memory", "alignment_bytes": [8], "chunk_bytes": [8]}
}
```

`buffer_pages` 为每个裸 NAND 或详细 SSD 内部控制器固定的 1–64 页缓冲，管理预算必须覆盖元数据加全部缓冲，候选不能修改。`stripe_pages` 白名单非空、无重复，每项为 1–2²⁴ 的整数。新模式的完整候选增加：

```json
"nand_page_mapping": {"version": 1, "targets": {
  "n0": {"axis_order": ["channel", "die"], "stripe_pages": 1}
}}
```

`targets` 必须恰好列出可控目标：全部裸 NAND，以及 `controller_access=open` 的详细 SSD；封闭 SSD 不得出现。以 **2 channel × 2 die、每页一条带**为例，逻辑页 0、1、2、3 分别选择 `(0,0)、(1,0)、(0,1)、(1,1)`；交换 `axis_order` 则成为 `(0,0)、(0,1)、(1,0)、(1,1)`。`stripe_pages=4` 表示连续四个逻辑页先选同一 channel/die，再轮转。规则作用于加上区域基址后的 target 内逻辑页号，不是按写入次数轮转；更新同一逻辑页仍选同一 die。

算法取 `q = 逻辑页号 // stripe_pages`，按 `axis_order` 从快到慢以硬件数量做混合进制取余，再循环使用所有 channel/die。所选 die 内取最小合法空页，依次填完 block、plane。**这不是直接把逻辑页号当物理页号**：旧版本仍占空间，更新必须另取空页。未启用下述 GC 时，所选 die 用尽返回 `nand_lane_exhausted`；不跨 die 回退。plane 顺序仍固定；回收规则由下述独立 `nand_gc` 组件选择。

新后端 `fcfs-nand-policy-v1` 按如下固定资源计时，也可与现有分配/迁移策略组合：

- 每 channel 一条内部数据总线，每 die 一个阵列/寄存器；同 die 的 planes 不独立执行。编程先传数据再占阵列，读取先占阵列再传数据；等待读出时仍占用 die。
- lookup/merge/bind 共用一个管理端口。就绪步骤按就绪时间、请求 ID 排队；同逻辑页按请求 ID 顺序完成，同块编程保持物理页序。调度不参与搜索。
- 每个在途逻辑请求占一页缓冲直到完整操作链结束，组间排空。报告 `peak_inflight_pages`、`channel_busy_ps`、`die_busy_ps`（含寄存器等待）、暂存和有效/待发布页峰值；阵列/总线忙时可以重叠，不能直接相加当总时间。

默认选择 channel 优先和白名单最小条带。不把“更分散必然更优”写入评分；缓冲不足、通道竞争和访问组织都会限制并行。候选语法、独立页分配参考和计时验证分别负责权限、内容与资源依赖；例子与手算见[验收记录](experiments.md#nand-page-mapping-checks)。

`evaluation.nand_parameters` 必须按每个裸 NAND 和详细 SSD 的外层 target ID 声明正整数 `read_ps/program_ps/erase_ps/transfer_ps/lookup_ps/merge_ps/bind_ps`。transfer 是每个整页在控制器与 NAND 阵列之间的内部总线时间，与拓扑链路分别计费；擦除只计 erase_ps。管理步骤也计时。功能检查独立核对原始操作串、绑定和整页保留内容，遗漏编程或读改写步骤会在计分前失败。模型不覆盖 TLC/QLC、ECC、坏块、磨损、掉电恢复或实际封装时序。

<a id="nand-gc"></a>
### NAND 前台 GC：固定规则与可搜索规则

在 `execution.nand` 中增加 `"gc": "foreground_greedy"`，后端变为 **`fcfs-nand-gc-v1`**；示例为 [nand_gc_search.json](../examples/nand_gc_search.json)。这是固定执行配置，候选仍只使用已有 DSL，不能修改 GC。省略该字段仍为旧模式；null、未知模式及每 die 少于两个擦除块的硬件被拒绝。

- **容量与触发：** 每 die 保留一块大小的容量余量，存活绑定最多为该 die 物理页数减 `pages_per_block`；增加绑定超限返回 `nand_gc_reserve`，不能借其他 die 的空页。每次写入前，若所选 die 的已擦除页不超过一块，则扫描该 die。
- **选块：** 在有失效页且外部空页足以容纳全部有效页的块中，选失效页最多者，同分选物理块号最小者；允许回收部分写满的块。没有可回收块但还有空页时继续写入；全满且无法安全回收返回 `nand_gc_no_victim`。余量是存活容量限制，不保证每个时刻都有完整空块。
- **执行：** 有效页按物理页序逐页读取，在同 die、回收块之外合法编程，完成后发布该逻辑页的新绑定；全部发布后擦除旧块，再执行前台写入。复用一页缓冲，复制与前台读改写不同时占缓冲。逻辑释放只解绑，等待后续写入触发回收。
- **计时：** 扫描每块计一次 `lookup_ps`；复制、绑定、擦除使用已有参数，不走主机链路。含 GC 的请求先排空该 target 的早先请求，再独占执行；后续请求等待它完成，其他 target 不加此屏障。没有 GC 的连续请求仍可并行。扫描与管理成本是抽象计费，并非真实固件 CPU/元数据实现。

`per_target` 增加 `gc_cycles`（擦除块数）、`gc_copied_pages`、`gc_reclaimed_pages`（净回收失效页）、`gc_reserve_pages_per_die`、前台编程数/有效写字节数。`write_amplification = 全部编程页数 × 页字节数 / 前台有效写字节数`，包含部分页写放大及 GC；无写入为 null，迁移写入也属于前台。`gc_blocking_ps` 为扫描/复制/发布/擦除占用的时间，不含排空旧请求或普通请求自身服务时间，也不是所有请求等待时间之和。

详细 `nand_operations` 以 `purpose=gc` 标注回收动作，复制/绑定另给 `logical_page`，`request_id` 关联触发它的前台请求。`verification/nand.py` 独立枚举位置、选块和内容，不使用执行器的空页游标或 GC 函数；核对实际操作串、页数、绑定和保留字节。手算、故障注入与会话验收见[实验总览](experiments.md#nand-gc-checks)。这仍是串行前台回收模型，没有后台 GC、磨损均衡或掉电恢复，也未校准实际器件性能。

**可搜索模式**使用 `execution.nand.gc="policy"`，并声明 `design_space.gc_options`：

```json
{"trigger_free_blocks": [1, 2], "victims": ["most_invalid", "oldest_reclaimable"]}
```

完整候选增加 `{"nand_gc":{"version":1,"targets":{"n0":{"trigger_free_blocks":1,"victim":"most_invalid"}}}}`；可加入 `vary`，也可固定。目标集合与 `nand_page_mapping` 相同。阈值是整块数，须小于每个可控 die 的块数；空页数不超过阈值×每块页数时扫描。**阈值不是备用容量**，安全余量始终为一块。

`most_invalid` 选失效页最多的安全块；`oldest_reclaimable` 选最早出现失效页的安全块。年龄取第一次解绑/更新产生失效页的递增序号，擦除后重置，不是最后访问时间或磨损。均按物理块号破同分；不改变复制/发布/擦除约束。策略模式为每物理块额外计 8 B 年龄元数据；规则、元数据与后端版本均进入任务身份。示例见 [nand_gc_policy_search.json](../examples/nand_gc_policy_search.json)。

<a id="ssd-interface"></a>
### 封装 SSD：抽象模式与详细模式

[`pool_search.json`](../examples/pool_search.json) 与 [`pool_layout_proposal.json`](../examples/pool_layout_proposal.json) 组合 DRAM 与 SSD。当前为主机接口抽象：target 使用 `module / medium="ssd" / geometry / max_transfer_blocks / host_buffer_bytes / links`；`geometry` 仅含 `logical_blocks / block_bytes`，最多 2²⁴ 块、每块 65536 B。区域按块对齐计容；本模式不接受 die/plane 或 FTL 候选。需要内部执行时，在同一 target 增加下述 `nand` 和 `controller_access`；两种模式保留不同后端身份。

- `max_transfer_blocks` 限定单个命令可含的块数，须不超过容量；当前 KV 适配器每条命令只访问一块，不合并邻接 LBA。底层计时接口检查多块命令的上限。
- `host_buffer_bytes` 为池外专用主机缓冲预算，至少覆盖一块；固定适配器只预留一块，用于读改写，报告实际峰值，不把预算虚算成池容量。
- `evaluation.ssd_parameters` 必须恰好覆盖所有 SSD target：`read_ps / write_ps / queue_depth / merge_ps`。读写服务时间为块数乘对应时间；队深 1–65536，作为并发命令上限，未建模 NVMe 队列或内部闪存调度。所有时间为正整数 ps，外部链路另外计费。

对象地址转换为 LBA 后，整块写直接提交；部分块写固定执行 **读整块 → 数据沿链路返回 → 主机合并 → 整块沿链路写入 → 写完成**。首次部分写同样读取，未写字节保留为未知标签，不假设设备承诺清零。整块内容和实际命令串独立核验；没有掩码写、隐式缓存或免费读改写。

抽象模式固定调度：每个逻辑组先按请求 ID 串行完成全部 SSD 部分块读改写，再提交组内剩余请求；剩余 SSD 请求可按队深并发。该全局屏障保证一个暂存块足够，**是本后端的简化执行约定，不是 SSD 的必然约束**。更大缓冲当前不会改善并行度；抽象模式不建模内部 GC；两种模式都不支持异步主机调度、命令合并、持久缓存、TRIM/flush 或掉电恢复。

抽象 SSD 报告逻辑块读写/命令数、服务时间、排队等待、并发峰值、主机合并与暂存开销；详细事件包含 LBA、命令 ID 和 `rmw_read/rmw_write` 阶段。不报告无法观察的 NAND 页数或内部写放大。配置参数未校准，`completion_ps` 是此确定性模型内的完成时间。

**详细模式**见 [ssd_nand_search.json](../examples/ssd_nand_search.json)。SSD target 在主机字段外增加：

```json
{"controller_access":"open",
 "nand":{"geometry":{"channels":1,"dies_per_channel":2,"planes_per_die":1,
                     "blocks_per_plane":3,"pages_per_block":4,"page_bytes":8},
         "logical_pages":8,"management_bytes":256}}
```

这是内部资源，不是另一个可分配 target；主机容量不能超过内部逻辑容量。`closed` 表示设备控制器封闭：不允许候选包含其内部映射/GC，固定 channel→die、stripe=1；任务启用 GC 时使用原固定规则。`open` 才允许搜索。全封闭 SSD 任务不提供 `nand_options/gc_options`，但必须配置 `execution.nand`；不能偷偷把设备权限当作 Agent 决策。

详细 SSD 的 `ssd_parameters[id]` 改为正整数 `command_ps/completion_ps/merge_ps`；内部 `nand_parameters[id]` 与裸 NAND 相同。主机整块拆成一到多条内部页掩码访问，支持块比页小、相等、大以及跨页；页共享时保留邻接 LBA。未绑定页的主机读返回未知标签，只收查表费用，不虚构 NAND 读取。对象释放不发 TRIM，内部绑定仍保留。

功能执行与计时都保持主机命令顺序：详细 SSD 命令（含完整 RMW）按请求 ID 串行，随后处理其他组内请求。命令内部的多个页仍可按 NAND 资源并行。接口费、内部页读改写/GC、主机 RMW 与外部链路分别计费；这是保守的全局屏障近似，不代表真实 SSD 队列并行。内部写放大的分母为进入 NAND 的主机写字节，不能与上层 KV 有效写字节混用。

<a id="lifecycle-allocation"></a>
### 显式生命周期与分配时机

[`lifecycle_search.json`](../examples/lifecycle_search.json) 使用负载 v3：对象初始未创建，新增 `{"op":"create","object":"A"}` 与 `{"op":"invalidate","object":"A"}`。时机由固定负载规定，候选不能修改；重建同名对象产生新代次、清空已写前缀，禁止创建前/失效后访问。`end` 不隐式释放，允许带着存活对象结束。

这一路径要求拓扑任务 v3，可组合 DRAM/NAND/SSD。`execution.allocation` 选择执行契约，`fcfs` 自动解析为对应后端：

| 契约 | 候选与计费 | 后端 |
| --- | --- | --- |
| `group_first_fit` | 保留旧固定规则；`allocation_parameters` 仅需正整数 `create_ps/invalidate_ps`，不单列预留/释放费用 | `fcfs-lifecycle-v1` |
| `policy_first_fit` | 新增 `allocation_policy`；另需正整数 `reserve_ps/release_ps`，每次实际预留/释放整组分别加收一次 | `fcfs-allocation-v1` |

表中后端名适用于未启用迁移或 NAND 并行的情况；组合模式按下文[评价路径](#评价路径)选择身份，完整任务仍保留所有契约和参数。

新契约中，`design_space.vary` 可列入 `allocation_policy`；也可通过 `fixed_design` 固定该组件。默认全部为 `create`。完整提案见 [`allocation_proposal.json`](../examples/allocation_proposal.json)，组件写法为：

```json
{"allocation_policy": {"version": 1, "reserve_on": {"A": "create", "B": "first_access"}}}
```

`reserve_on` 必须覆盖全部对象；每个选择适用于该对象所有代次。`create` 在创建时触发，`first_access` 在首次实际访问前触发；当前先写后读约束意味着首次访问必为追加写。候选不能改逻辑生命周期或按未来事件编号触发。`kv_layout` 仍决定组内相对位置、区域、条带和整组尺寸。

1. 任一成员触发时，若该组尚未预留，就一次预留**整组**填充后尺寸；每区域找最低地址的对齐连续空闲区，全部满足才提交。失败不部分占用，也不自动改目标或搬移。
2. 已有预留时，其他成员创建/访问沿用基址，不重复收费。最后一个存活成员失效后才释放整组；即使某存活成员从未访问，也会阻止已分配组提前释放。全程未触发的组不分配、不释放。
3. 所有事件等待前面访问完成。每个 create/invalidate 都计管理延迟，即使没有实际分配；新契约额外计实际 reserve/release。首次访问分配记为独立 `reserve` 事件，并推迟该访问。没有异步预分配或隐藏延迟。
4. 释放 NAND 逻辑范围时，对已绑定页额外执行查表/解绑，使用 `lookup_ps/bind_ps`，不同 target 可并行。旧物理页仍占空间；启用前台 GC 后由后续写入触发回收。DRAM 不清零，SSD 不发 TRIM。

正确性参考端独立计算条带、空闲区和生命周期，核对实际分配记录；数据标签为 `(对象, 代次, 元素, 字节)`，因此复用同名对象/同一地址也不能读到上一代数据。初始容量检查只要求每组单独能放下，执行时再检查同时存活组和碎片；非法候选不消耗性能预算。

`allocated_bytes` 是**结束时仍预留的逻辑容量**，`peak_allocated_bytes` 是同时占用峰值，均包含填充；分区域/目标峰值不能相加当全局峰值。另报实际 `group_allocations/group_releases`、逻辑 `lifetime_events`（不含 reserve）、主机管理总时延 `allocation_management_ps`。详细 `allocation_events` 含起止时间、代次及区间；`changed` 表示实际预留/释放，未预留时区间为空。NAND 解绑时间、物理占用和 SSD 暂存另列。

最小例子：A/B 各 8 B，先创建 A，再完整使用并失效 B，最后使用 A；B 还会重建一次但未访问就失效。创建时预留的峰值为 16 B，首次访问分配为 8 B，后者省去未使用代次的一次分配/释放。可运行 [`allocation_search.json`](../examples/allocation_search.json)，命令沿用上面的文件会话接口；结果见[工程验收](experiments.md#development-checks)。

当前管理时间是按操作计费的抽象常数；没有分配器元数据容量预算、碎片搜索耗时、并发分配、部分对象释放或引用共享。若每个代次都访问且地址行为相同，两种时机可能总耗时相同；此模型不能检验异步预分配收益。`allocation_policy` 只管理上层逻辑区域，NAND 内部分布由独立的 `nand_page_mapping` 配置。

<a id="fixed-migration"></a>
### 整对象迁移：策略与执行契约

[`migration_search.json`](../examples/migration_search.json) 给出 DRAM → NAND 的完整例子。迁移不改变负载的读写或生命周期。只允许生命周期 v3、`policy_first_fit`；发生迁移的对象须单独一组、初始只占一个区域，目的区域来自同一池且与源不同。内部布局不变，目标基址保持条带对齐。不迁移的对象仍可共享组或跨区域条带。

启用搜索时，任务分别声明执行能力和可选范围（以下是配置片段）：

```json
{
  "execution": {"kind": "serial_groups", "allocation": "policy_first_fit",
                "migration": {"kind": "policy", "buffer_bytes": 8}},
  "design_space": {"vary": ["migration_policy"],
                   "layout_options": {"pool": "memory", "alignment_bytes": [8], "chunk_bytes": [8]},
                   "migration_options": {"triggers": ["on_full", "before_read"], "destinations": ["cold"]}}
}
```

完整候选再包含 `"migration_policy": {"version": 1, "objects": {"A": {"trigger": "before_read", "destination": "cold"}}}`。`objects` 必须逐一覆盖负载对象；`never` 始终允许，其他触发和目的区域从任务白名单选择，未知字段拒绝。未列入 `vary` 的策略可由 `fixed_design` 冻结。

| 对象规则 | 执行语义 |
| --- | --- |
| `{"trigger":"never"}` | 不迁移，不填 destination；也是默认值 |
| `{"trigger":"on_full","destination":"cold"}` | 完成将本代写满的最后一组写入后迁移 |
| `{"trigger":"before_read","destination":"cold"}` | 本代写满后的首次读取之前迁移；未满时照常从源读取，写满后再无读取则不迁移 |

每代最多一次，不允许任意事件编号、重复搬移或偷看未来。失效后重建从初始区域重新开始。规则只消费已发生的写入和当前读取；离线 Agent 仍可读冻结负载。候选的布局和迁移规则联合检查，目标容量在触发时检查；提前或延后都可能导致容量不足。

旧固定计划仍可使用，输入和计分身份保留：

```json
{"migration": {"on_full": {"A": "cold"}, "buffer_bytes": 8}}
```

该片段放在 `execution` 中，不再提交 `migration_policy` 或 `migration_options`。它与候选规则共用下面的执行链，首次适配选择实际基址。启用迁移的起点须满足上述布局限制，默认跨池条带不能直接用于迁移。

| 阶段 | 状态与成本 |
| --- | --- |
| 预留目标 | 整组尺寸含填充；保留源区间，全部空间足够才预留。计 `reserve_ps`，双份预留计入峰值 |
| 分窗复制 | 按相对字节位置切成缓冲大小的窗口；逐窗源读完成 → 缓冲复制 → 目标写完成，再进入下一窗。只复制有效元素，填充占容量但不冒充数据 |
| 发布新位置 | 所有目标写完成后计 `publish_ps`，后续访问改走目的区域；对象身份、代次与内容不变 |
| 释放源 | 计 `release_ps`，NAND 另计解绑。只解除源逻辑预留，不擦除 NAND、不发 SSD TRIM |

`buffer_bytes` 是固定的主机迁移缓冲预算，须为池中介质粒度与元素大小的公倍数，最多 1 MiB；它不包含 NAND/SSD 自身暂存。`evaluation.migration_parameters` 必须显式提供正整数 `publish_ps` 和 `copy_ps_per_byte`；后者按有效复制字节计主机处理时间。读写、读改写及往返链路仍计费。未启用 NAND 并行时，固定计划/策略模式分别使用 `fcfs-migration-v1 / fcfs-migration-policy-v1`；启用后统一为 `fcfs-nand-policy-v1`。全选 never 不发生迁移计费。缓存包含触发、目的区域及允许范围，不按初始地址或偶然同分合并。

`copied_bytes` 按有效数据单计一次，`migration_frontend_bytes` 包含复制读和写的整粒度请求、但不含额外 RMW；实际流量仍看总量和各目标。`migration_management_ps` 包含目标预留、缓冲处理、发布及源释放，NAND 解绑另列；原 `allocation_management_ps` 只含生命周期路径。容量指标包含迁移时两份预留，`group_allocations/group_releases` 包含迁移预留/释放。详细 `migration_events` 记录阶段时刻，`request_sources.purpose` 区分 workload/migration。正常读写事务统计包含复制事务。

参考端独立展开迁移时机、位置和窗口，检查源读回值、缓冲内容、目标写入、发布顺序及后续读取。目标不足或复制失败不会发布新位置，非法候选不进入性能评价；失败结束整次候选，不支持从半途恢复，已编程 NAND 页不保证回滚。本路径是串行、字节标签级验证，不含并发访问、部分对象迁移、布局转换、DMA/缓存一致性或掉电恢复，参数也未校准。

<a id="从一次评价读代码"></a>
## 从一次评价读代码

`__main__.py` 解析命令，`MemorySearchTask` 组合固定输入与策略范围；`session.py` 恢复会话并调用 `Evaluator`。一次未命中缓存的合法候选经过两次独立运行：

1. `normalize → candidate_id → check`：检查语法/权限；生成实际事务，由 `verification/memory_contents.py` 的独立参考核对布局、内容、分配和迁移。这里不做性能计时。
2. `measure`：从新建状态重新执行同一候选，由 `execution/memory_system.py` 驱动管理状态、介质执行和 `performance/`，返回完成时间、流量与占用。
3. `session.py` 记录成绩、反例、缓存和预算；外部 Agent 读取反馈后决定下一候选。程序不自行调用模型或修复 DSL。

`spaces/` 定义“可以选择什么”，`execution/` 实现“选择之后如何读写/分配”，`performance/` 计算“何时完成”，`verification/` 核对“是否正确”，`exploration/` 组织任务和探索。内容检查会调用实际执行器，再与独立参考比较。

设计空间直接使用 `spaces/` 下的 `*_dsl.py`：`kv_layout_dsl.py` 管逻辑对象组织和初始放置；`memory_management_dsl.py` 管跨介质预留与迁移；`dram_dsl.py`、`nand_dsl.py` 是并列的介质设计空间，HBM 归入 DRAM。**每个文件集中原语定义、解析、序列化与合法性约束；候选搜索、状态执行、计时和系统/RTL 验证独立维护。** 当前每个空间只需一个实现文件，不保留中间目录；以后出现可独立维护的职责再考虑拆包。NAND 回收策略在 `nand_dsl.py`，状态和计时分别在 `execution/nand.py`、`performance/nand.py`。SSD 是设备接口，详细模式复用 NAND 层；没有独立 SSD 策略，不建立空 DSL 文件。

| 文件或目录（相对 `agent_design_search/`） | 实际职责 |
| --- | --- |
| `__main__.py` | inspect、evaluate、文件会话、映射 RTL 导出的命令入口；不是自动 Agent 循环 |
| `contracts.py` | 请求、页/块操作、分配/迁移事件、结构化错误、目标和小型 `DesignTask` 接口 |
| `json_io.py` | JSON 读写、重复字段拒绝、稳定内容摘要；不负责会话锁或事务恢复 |
| `inputs/workload.py` | 固定 KV/地址轨迹解析、来源导入、范围限制、访问组展开和统计 |
| `inputs/hardware.py` | DRAM/NAND/SSD 几何、模组/目标/区域/池/链路及容量与引用检查 |
| `spaces/kv_layout_dsl.py` | 对象 tile/order、拼接/交织、分组、区域选择和条带，计算偏移与预留需求 |
| `spaces/memory_management_dsl.py` | 预留与迁移原语：创建/首次访问预留；不迁移、写满后或写满后首次读取前迁移及目的区域；不改变负载生命周期 |
| `spaces/dram_dsl.py` | swap/XOR 顺序解释、逆变换、矩阵编译、坐标解码和数学检查 |
| `spaces/nand_dsl.py` | 校验可控目标的 channel/die 页条带、GC 阈值和选块；不执行原始命令 |
| `execution/memory_system.py` | 展开逻辑访问、生成带目标/字节标签的事务；驱动分配/迁移及计时；详细模式解释实际事务的地址来源 |
| `execution/allocation.py` | 上层逻辑区域的整组首次适配、存活代次、迁移双份预留/发布/释放；不分配 NAND 物理页 |
| `execution/nand.py` | 裸 NAND 与详细 SSD 共用的页状态、合法分配、读改写、发布/解绑、前台 GC |
| `execution/ssd.py` | 主机块/读改写；详细 SSD 把完整主机命令拆成内部页访问，复用 NANDExecutor |
| `performance/dram.py` | 简化 DRAM 排队、开行状态、bank/通道竞争；不解释 DSL |
| `performance/nand.py` | 旧串行模式和新有限缓冲模式的管理端口、通道总线、die 引擎与操作依赖计时 |
| `performance/ssd.py` | 抽象块计时，或详细模式的接口开销＋共享 NAND 计时；不模拟 NVMe |
| `performance/memory_system.py` | 组合各目标、共享链路、读回路径和 SSD 主机读改写屏障；拓扑时间统一为 ps |
| `performance/ramulator/` | 只读适配和未编译验证的原生驱动；六个非包标记文件逐一见[目录说明](../agent_design_search/performance/ramulator/README.md#文件职责) |
| `verification/memory_contents.py` | 独立枚举位置和源事件，参考分配/迁移；调用介质检查，核对字节标签读回 |
| `verification/nand.py` | 独立 NAND 页分配、选块/搬移参考；逐请求核对实际操作串、绑定与整页内容 |
| `verification/ssd.py` | 独立枚举主机块覆盖的 NAND 页，复用页参考检查内部操作与保留数据 |
| `performance/mqsim/` | SSD trace/XML 导出、运行/结果校验；裸 NAND 定制驱动协议，见[接口说明](../agent_design_search/performance/mqsim/README.md) |
| `verification/dram_rtl.py` | 生成位映射组合电路、向量和 testbench；可选调用 Icarus 检查实现一致性 |
| `exploration/memory_search_task.py` | 加载/冻结输入，分步配置布局与后端；组合 DSL、固定组件，实现规范化、候选身份、检查、计分和反馈 |
| `exploration/evaluation.py` | 统一规范化/缓存/正确性检查和性能预算；开始计分的失败也扣预算 |
| `exploration/session.py` | 持久化任务、提案历史、反馈、剩余预算和最好候选；调用者提供 Agent 决策 |
| `exploration/search_algorithms.py` | 通用随机/局部搜索循环，领域注入候选/变异函数；不负责模型推理 |
| `exploration/dram_mapping_candidates.py` | DRAM 位活动、联合分布等启发式，以及随机候选/邻域修改；使用已有 DSL，不定义新原语 |
| 各层 `__init__.py` | Python 包标记和模块说明；根文件另声明包版本，没有隐藏 Agent 服务 |

`inputs/` 只描述固定条件，`spaces/` 不各建任务栈，`performance/` 不导入 DSL，核心不导入实验/结果/历史适配器；依赖规则有测试覆盖。预算循环可复用，但新联合空间尚无配套候选生成器，不能把地址位启发式当成所有空间的强对照。文件会话会记录非法提案；普通搜索循环遇到评价异常会终止，尚无统一重试策略。

## 评价路径

| 固定输入 / 后端 | 搜索范围 | 指标与含义 |
| --- | --- | --- |
| KV / `line-traffic-v2` | 仅布局，映射须固定 identity | transferred_bytes，按实际事务计数，无缓存、无时间 |
| 地址轨迹 / `fcfs` | 介质映射 | 解析为 `toy-fcfs-v1`，completion_cycles，保持旧固定到达语义 |
| KV / `fcfs` | 布局、映射任一或联合 | 解析为 `fcfs-serial-kv-v2`，completion_cycles，额外报告传输量 |
| KV＋全 DRAM 拓扑 / `fcfs` | 对象布局、静态放置、声明范围内映射 | `fcfs-system-v1`，completion_ps，分资源/链路报告开销 |
| KV＋含 NAND、无 SSD 拓扑 / `fcfs` | 未启用 NAND 并行时：布局、静态放置、已有 DRAM 的映射 | `fcfs-hybrid-v1`，completion_ps，固定串行页管理 |
| KV＋含 SSD 拓扑 / `fcfs` | 布局、静态放置、已有 DRAM 的映射；可同时含 NAND | `fcfs-block-system-v1`，completion_ps，含主机读改写与队列代价 |
| KV＋`execution.nand.kind=channel_die` / `fcfs` | 增加 NAND 页分布；可叠加生命周期、分配和迁移 | `fcfs-nand-policy-v1`，completion_ps，有限缓冲与通道/die 竞争；未启用者仍用旧后端 |
| 上述模式＋`gc=foreground_greedy` | 搜索范围不变，GC 规则固定 | `fcfs-nand-gc-v1`，completion_ps，另计复制/擦除、前台阻塞和写放大 |
| 上述模式＋`gc=policy` | 增加 `nand_gc` 阈值与选块 | `fcfs-nand-gc-policy-v1`，固定安全余量、计入年龄元数据 |
| 含详细 SSD | 内部 NAND 仅按控制器开放权限搜索 | `fcfs-ssd-nand-v1`；启用可搜索 GC 为 `fcfs-ssd-nand-policy-v1` |
| KV 生命周期 v3＋拓扑 / `fcfs` | 布局、目标分布、已有 DRAM 的映射；分配规则固定 | `fcfs-lifecycle-v1`，completion_ps，另报管理与容量峰值 |
| KV 生命周期＋`policy_first_fit` / `fcfs` | 增加创建/首次访问预留策略 | `fcfs-allocation-v1`，completion_ps，分列实际预留/释放费用 |
| 上述任务＋迁移 / `fcfs` | 固定迁移，或搜索触发与目的区域 | 固定迁移为 `fcfs-migration-v1`；候选策略为 `fcfs-migration-policy-v1`，completion_ps |

组合时后端身份的优先级为：详细 SSD → NAND GC → NAND 并行 → 迁移 → 分配/生命周期 → 静态资源类别；完整冻结输入区分其余配置。流量模型不能评价 channel/bank 映射，轨迹不能搜索 KV 布局，当前 Ramulator / MQSim 不能选作任务后端。不支持的组合在创建会话前拒绝，不静默忽略候选组件。

KV 组内只合并同 target/region 的同一粒度，按 target/region/地址排序分配请求 ID；下一组等全部完成，SSD 另有读改写屏障。DRAM 掩码保留未访问字节，NAND/SSD 展开整页/块操作，均如实计费。`end` 不表示释放；v2 全程静态预留，v3 由显式事件分配和释放，预留均包含未写槽位及填充。

FCFS 按 channel 独立排队，默认队深 16、每周期最多发一条，队首等待目标 bank，后续不能越过。命中准备 4 周期，空行另加激活 8，冲突再加预充电 8；传输 4 周期。同 channel 总线按发出顺序预约，bank 占用到传输完成。读写使用相同时延。这些是抽象参数，没有经过硬件校准。

执行器在批次间保留 open row、bank、总线及发出时间，只接受前批排空后的下一批；没有任意依赖图或并行算子。单资源计时的 `measure(design, include_events=True)` 返回 `events` 中的 `arrival/admitted/issue/transfer/finish`；前两项分别表示本模型的 ready 和入队时刻。拓扑事件另用下述 ps 字段。KV 另有 `request_sources` 关联源事件与掩码，默认反馈只保存指标。

详细 KV 来源还包含实际提交组 `group`、读写种类、target/region、`granule_bytes` 和逐有效字节的 `byte_origins`（二维下标、元素内字节、对象内偏移、区域内偏移、目标地址、块内偏移）。对象偏移在组合/条带之前计算，目标地址来自实际事务，迁移前后分别记录；padding 不冒充有效元素。`address` 仍是对齐事务起点。`address_space` 区分 `dram_target_bytes / nand_logical_bytes / ssd_logical_bytes`，DRAM 才带 `dram_location`；NAND 物理页须关联 `nand_operations` 或 `ssd_nand_operations` 查看。`virtual_memory=not_modeled` 明示没有 VA 翻译。详细输出随访问字节数增长，仅按需开启。

周期目标为最后完成减最早 ready/arrival；延迟包含上游背压。单独的行命中率和传输量用于诊断，不能替代整条执行链的目标，也不能把各组排空时间相加。未覆盖真实缓存、计算、刷新、读写切换、bank group 或完整 HBM 协议。

**拓扑后端统一使用整数 ps。** DRAM target 共用 FCFS 周期参数，再乘各自 `clock_period_ps`，队深不变；不做时钟边沿取整。NAND/SSD 分别使用其参数。写数据逐链路传入后访问目标，读数据完成后沿反向链路返回。链路按到达时间、请求 ID 排队，双向互斥；每次占用 `latency_ps + ceil(传输字节/bytes_per_beat) × beat_ps`，包括传播延迟。除 SSD 读改写及详细模式串行屏障外，组内不同资源可并行，组间排空。没有流水链路、命令包、有限转发缓冲或链路反压，不能据此判断真实互连吞吐。

`transferred_bytes` 汇总各目标接口流量：DRAM 传输、裸 NAND 原始页操作、SSD 主机块读写；**不是 SSD 内部闪存流量**。`per_link` 另计每跳字节、忙时和等待，不能重复计为逻辑读写。`per_target` 提供事务、末次介质/设备完成时间、内部批次数和 Python 耗时；SSD 的事务数包含额外 RMW 读取。完整候选仍只扣一次预算。详细模式返回 `media_events/link_events/group_events`，时间字段带 `_ps`，`request_sources` 关联原始事件与目标。

NAND/SSD 后端另报 `frontend_transferred_bytes`（展开读改写前的事务流量），实际流量包含额外读取和启用的 GC 复制。`allocated_bytes` 是布局逻辑预留，不是闪存物理占用。NAND 单列页状态、物理占用、管理和暂存成本，`nand_operations` 关联物理坐标；SSD 单列主机块访问和暂存，`ssd_host_events` 记录合并；详细 SSD 的内部状态/流量放在 `per_target[id].nand`，操作放在 `ssd_nand_operations`，不重复加到主机链路或池容量。旧 DRAM/NAND 后端身份、字段和分数保持不变。

<a id="model-limits"></a>
## 当前近似与能力缺口

这是一套能执行和核验候选策略的研究原型。下列限制决定结果能回答哪些问题；通过内容检查不等于时间准确，局部时间下降也不等于真实推理吞吐提高。

| 层面 | 当前实现与近似 | 尚不能据此判断 |
| --- | --- | --- |
| 负载 | 有界二维对象的追加、矩形读取、创建/失效；固定分组并逐组排空，示例为合成输入 | 真实模型算子依赖、计算/通信重叠、共享前缀、任意更新、动态批次和 tokens/s |
| 布局与管理 | 静态分块/组合/放置；首次适配；受限整对象迁移；管理时间按常数计费 | 通用分页、细粒度共享、未满对象迁移、布局转换、在线重排，以及实际分配器耗时和元数据占用 |
| DRAM | 小规模幂次几何、扁平 bank、固定 FCFS 行缓冲模型，读写等时延 | 完整 HBM 命令时序、刷新、bank group/伪通道、读写切换及真实控制器调度 |
| NAND | 抽象 SLC、channel/die 分布、有限缓冲并行；前台 GC 可选阈值和选块，独立持续更新检查 | 后台回收、磨损/坏块/ECC、TLC/QLC、掉电恢复及实际器件稳态性能；plane 在 die 内串行 |
| SSD | 抽象块服务，或复用 NAND 的详细模式；显式控制器权限，主机/内部流量分账 | 完整 FTL/NVMe、异步命令、TRIM/flush、磨损、持久性与真实尾延迟；详细模式全局串行主机命令 |
| 互连与并行 | 链路双向互斥，传播延迟也占总线；组间屏障，SSD 部分写另有串行屏障 | 流水互连、有限转发缓冲、完整反压、任意异步依赖与队列调度；屏障不是硬件必然约束 |
| 正确性 | 字节标签、独立位置/生命周期/页分配参考、实际操作重放；DRAM 映射可导出 RTL | 数值 attention 正确性、通用并发/掉电安全证明、完整控制器 RTL 及 PPA；功能操作先重放再计时，不是逐时刻并发内容仿真 |
| 后端可信度 | Python 确定性计时；参数未校准；Ramulator / MQSim 有接口但未实际编译运行 | 真实硬件收益、跨后端排名稳定性和端到端推理收益 |
| Agent 与实验 | 外部对话提交候选；有预算、缓存与历史；旧地址位实验仍可追溯 | 自动模型服务、母/子 Agent 常驻编排、完整推理成本，以及新联合空间上的 Agent 泛化优势 |

工程上仍按单进程、单写入者使用文件会话，未提供原子落盘、并发锁或崩溃恢复；任务摘要不自动包含源码摘要。`exploration/memory_search_task.py` 与 `verification/memory_contents.py` 是目前改动最集中的文件，增加空间仍需显式接线。后续按实际新增职责拆分，暂不搭建空的插件框架。

## 正确性与错误反馈

布局检查字段、分块、组合次序、区域权限、对齐和容量。参考端独立枚举位置、条带和源访问组，对比执行器实际生成的事务和内容；生命周期任务另核对分配、代次、迁移触发与复制/发布链。物理内容键为 `(target, 映射后位置, 字节)`，region 标签不能掩盖别名。映射另用 GF(2) 满秩及解释/编译/逆变换检查。检查会运行控制器功能重放，再与参考计算比较；不是两套完全独立的系统实现，也不证明所有并发时序或数值 attention。

错误返回 `code/path/message/details`，例如 `$.dsl.kv_layout.objects.V.tile.token`；执行反例关联对象和原始 workload event。非法候选不进入性能工具，Agent 修改后必须另行提交；程序不会偷偷修复。RTL 在独立入口验证电路是否实现映射，和上述数学/内容检查是不同证据。

<a id="budget"></a>
## 预算、缓存与协议

新会话协议为 `agent-memory-search-v3`。完整设计共用一个预算；非法候选和语义缓存命中不计性能调用，但内容检查仍有计算成本；进入 measure 后即计一次，失败或返回无效目标也消耗预算，不进入最佳结果。文件会话最多 `10 × budget` 份提案，预算耗尽后不再接受提案或缓存查询；普通搜索默认最多 `30 × budget` 次尝试。底层 `Evaluator` 可在预算耗尽后返回已有缓存，不等于文件会话继续开放。

缓存包含任务快照、后端版本、DRAM 映射函数和启用的 NAND/分配/迁移策略；静态布局按有效地址/预留量合并，生命周期布局保留规范化分组和对齐，避免合并会影响预留/释放的设计。相同分数不合并。任务摘要不会自动追踪源码改动，跨实现复用会话仍需版本纪律。`validation_seconds` 与 `backend_seconds` 分开记录；模型 token、费用、文件操作和端到端时间尚未完整计量，缺失不等于零。

旧 `--domain/--config/--workload` 核心命令已由 `--task` 替代；`agent-memory-search-v2` 及更早会话不直接续跑。旧 KV 后端名在新任务入口解析为 v2；历史适配器仍保留旧后端、候选身份和分数。历史任务的两个公共起点、候选格式、成绩与协议保持原样，由实验适配/固定版本重放，不套用新协议。

### RTL、Ramulator 与 MQSim

`python -m agent_design_search export-rtl <完整设计.json> --task <search.json> --output <新目录>` 导出组合地址映射电路与 testbench。输入候选会先规范化，但这条命令不执行完整布局/迁移的内容检查。包含字节偏移的地址宽度不超过 12 bit 时穷举，否则使用边界和固定种子的至多 2048 个随机样本。PATH 同时有 Icarus 的 `iverilog/vvp` 才运行；缺少工具为 `not_run`，不能当作通过。当前归档没有 RTL 实际仿真、综合面积/频率/功耗结果。

拓扑声明多个映射组时须加 `--mapping-group <组名>`；只导出该组的地址位电路，不包含整个布局、链路或模组控制器。

Ramulator 适配器现在消费已经路由的请求，再转换为其事务格式，不解释策略 DSL。只接受完整粒度的读，未接入统一预算，继续暂停 C++ 构建与实际运行。具体契约、构建及恢复验收只在[适配器说明](../agent_design_search/performance/ramulator/README.md)维护。

MQSim 与 Ramulator 并列：[SSD 原生接口及裸 NAND 驱动契约](../agent_design_search/performance/mqsim/README.md)。原生 SSD 输入是固定到达的主机 sector 请求，内部控制器由其 XML 决定；不能宣称执行了当前 NAND DSL。裸 NAND 已有坐标/操作/依赖导出和完成校验协议，仍需定制驱动。两者均未安装、运行或校准，不能直接作为联合搜索的替换后端。

### 负载来源与后端校准准备

KV 输入可带 `provenance`，记录 `kind=synthetic/captured`、`source`、`source_sha256`、`execution=serial_groups` 和转换说明 `note`，进入任务快照/摘要。现有输入可省略，旧身份保持不变。导入已标准化的事件：

```powershell
python -m agent_design_search import-kv examples/kv_workload.json --kind synthetic --source repository-example --note "Fixed serial groups; no model capture" --output results/imported_kv.json
```

导入检查完整语义并记录原文件摘要；**不会采集模型，也不会自动把任意 trace 转成 KV 对象**。`captured` 是调用者的来源声明。未知时间戳/依赖字段被拒绝，不静默丢弃；真实异步算子仍需新的前端。跨层验收用重复生命周期、前缀读取、提前结束和容量压力增加覆盖，仍明确标为 synthetic。

后续实际校准先冻结：逻辑工作/到达契约、硬件/控制器权限、完整候选、初态、配置/源码摘要、计量单位和可比指标。Ramulator 目前只验证完整粒度读；MQSim 原生结果只能校验聚合请求/字节及截断微秒延迟。先手算小例并检查操作覆盖，再比较同契约下的策略排序及差异；不能将独立仿真时间相加或把模型生成的到达时间冒充真实依赖。此次完成接口准备，未产生校准结论。

<a id="historical-replay"></a>
## 历史复现与回归

`experiments/legacy_cases.py` 保存旧配置、合成负载、候选成绩身份和单对象流量计数，复用当前语义与 DRAM 计时；仅用于已有实验。规则发现锁定 Git `6904e008`，V2/V3 锁定 `48f2476b`；历史源码身份核验和新代码行为回归分别执行。

```powershell
python tools/replay_history.py all --check-only
python tools/replay_history.py all
python -m experiments.analyze_archives regression results/agent_v2_pilot
python -m experiments.analyze_archives regression results/agent_v3_pilot
python -m experiments.layout_study --output results/layout_recheck
```

重放从本地 Git 历史导出临时目录；浅克隆需先取得相应提交。`--tests` 跑当时测试，`--export <新目录>` 只恢复源码与数据，供旧 `hbm_toy.*` 命令使用。V2 核对原字节哈希，规则/V3 核对统一换行文本哈希。`all` 重放 V2/V3、仅核验规则实验，不改原档。

当前回归比较指标、候选身份、验证、缓存、预算和分批反馈，忽略墙钟耗时；它不是新 Agent 探索。阶段统计与绘图在 `experiments/analyze_archives.py`，绘图用 `python -m experiments.analyze_archives render <归档副本>`。`experiments.benchmark` 保留旧 `--config` 及四种合成负载，每次搜索后的两次变体评价单列费用。

<a id="archive"></a>
## 维护规则

新增语义同时增加手算和反例验证；改变计分或执行协议时单独版本化，不能改旧哈希使检查通过。任务和模型成功与研究贡献成立分别验收。新结果默认忽略，选定归档在 `.gitignore` 白名单登记，解读统一进入 [experiments.md](experiments.md)。

PDF、依赖、临时会话和备份留本地。旧基线已去除可重建附件，来源见 [provenance.json](../results/provenance.json)；历史角色文件和 `examples/agent_v3_prompt.md` 仍参与证据追踪，后者还在 V3 的源码哈希中，保留原文。新实验只维护必要输入、一份完整记录和集中报告。

<a id="implementation-plan"></a>
## 开发计划：多对象、混合模组与存储池

2026-10-20：P0–P4、有限分配/迁移、NAND 页分布与前台 GC 已接通。本轮 **1–4 的工程范围已完成**，包含可搜索 GC、详细 SSD、跨层验收与负载/外部后端接口。真实轨迹、实际仿真和校准仍未完成；第 5 步留待讨论。这里是实施清单的唯一维护位置，不提前开展 Agent 优化竞赛。

### 已确定的设计约定

| 信息或决策 | 归属与边界 |
| --- | --- |
| 对象、形状、读写、依赖、逻辑生命周期 | 固定负载；不搜索“要完成什么工作”。负载 JSON 直接解析，无须增加中间文本 DSL |
| 模组、介质、设备、容量、连接与可控接口 | 固定硬件/运行环境；模型时延单列于评价配置，不由候选扩大资源或修改 |
| 对象内部排列、多对象组合、静态目标选择 | 扩展现有 `kv_layout`，暂不新建 `memory_placement` DSL |
| 介质内部位置 | DRAM 与 NAND 各自解释；HBM 属于 DRAM 类，但具体几何和时序独立配置 |
| 分配、绑定、回收、迁移 | 运行时状态与管理策略；先采用明确的固定实现，再开放有限策略。职责独立不等于另造一种语言 |
| 生命周期与回收时机 | 负载规定数据何时可失效；物理空间回收还须满足未完成访问和介质约束。输入若固定 allocate/free 调用时机，就不再搜索这些时机 |

两种目标场景共享上层负载和布局，按暴露接口选择执行路径：

- **混合模组**：例如 8 个 module，每个内部包含 HBM 与裸 NAND。保留模组内/模组间的连接与共享资源；垂直堆叠本身不代表自动缓存、统一 load/store 或免费搬运。
- **封装设备组成的池**：例如 DDR/HBM 资源与封装 SSD。池是资源集合，不是一种介质。主机接口保持逻辑块，内部 NAND/FTL 是否可搜索取决于是否开放设备控制器设计权限；抽象模式封闭，详细模式按 `controller_access` 显式开放或封闭。池成员引用实际资源，不重复计算容量。

地址契约为 `(target_id, region_id, byte_offset)`，offset 相对 region，target 由 region 引用唯一确定。加区域基址后，DRAM 解码介质坐标，NAND 通过逻辑页绑定到物理页，SSD 转为主机 LBA。三元组不是全局地址的固定位切分；不允许同一物理资源同时登记成裸 NAND 与 SSD 两份容量。

现阶段做离线设计探索，Agent 可读冻结负载；未来运行时策略只能使用已发生事件及声明可见的信息，不能默认偷看未来结束时刻。布局 tile、交织 chunk、NAND 页、擦除块和 SSD 逻辑块分别命名，不共用含义模糊的“块大小”。

### 批次与验收

以下“完成”指所列原型契约，不代表完整器件、生产固件或论文复现。详细字段只在前文维护，历史数字集中在[实验总览](experiments.md#development-checks)。

| 批次 | 已接通的能力 | 验收与剩余边界 |
| --- | --- | --- |
| P0 | 固定输入、完整候选、统一预算及版本/身份隔离 | 旧实验通过历史适配/固定源码重放，不把新成绩写回旧归档 |
| P1 | 多对象内部布局、拼接/交织与独立字节标签检查 | 不等长对象、尾部填充、错误掩码/别名、容量及读前写均有检查；见[布局契约](kv-layout.md) |
| P2 | 模组、目标、区域、池、共享链路和静态条带 | 逐区域计容、同目标区域不重叠、DRAM 映射按固定组共享；2/8 模组案例见[验收记录](experiments.md#development-checks) |
| P3 | 抽象 SLC NAND、异地更新、内容/操作重放与旧串行计时 | 非 2 次幂几何、部分更新、发布、非法擦除/编程检查；当时页分配固定，新 DSL 见 P5.2 |
| P4 | SSD 主机逻辑块、计费读改写及池组合 | 抽象模式保留；详细模式复用 NAND，仍无 NVMe、TRIM、磨损或持久性保证 |
| P5.1 | 显式生命周期、首次适配、创建/首次访问预留策略 | 代次、整组释放、碎片和容量峰值、NAND 解绑；见[分配契约](#lifecycle-allocation) |
| P5.2，有限策略已完成 | 页条带、有限缓冲并行、可搜索前台 GC、详细 SSD 复用 | 独立检查与成本分账；后台 GC、plane 策略和异步 SSD 后置 |
| P5.3 | 整对象迁移触发和目的区域 | 九种介质组合、复制/发布/源释放、双份容量及失败检查；仍限写满、单对象单区域、每代一次 |

**P6：可信后端与研究评价，条件满足后再推进。**

代表性负载、后端校准与内容验证可随前面各批补充。Ramulator / MQSim 实际安装/运行仍按用户要求暂停；恢复后 Ramulator 覆盖 DRAM/HBM，MQSim 原生接口覆盖 SSD 主机请求，裸 NAND 仍需定制驱动。两者均须验证与当前执行语义的对应关系，再接入联合评价。RTL 按实际改变的硬件逻辑验证，软件布局不强行生成 RTL。达到目标取舍所需的保真度后，再冻结 Agent 主导探索、强对照与反馈消融协议；不会用新增玩具模型提前宣称 Agent 优势。

固定 K/V 生命周期组合验收已完成：同一事件序列在 256/384 B DRAM 区域上运行八个预设候选，覆盖未满对象、代次重建、布局、预留、迁移及静态放置替代方案。`experiments/kv_lifecycle_study.py` 复用正式文件会话，保存任务、完整提案、反例和预算；运行方式与证据集中在[实验总览](experiments.md#kv-lifecycle-checks)。这是合成输入，未接真实模型轨迹或校准后端，P6 仍未完成。

### 本轮 1–4 交付与下一步

| 步骤 | 已交付与验收 | 保留边界 |
| --- | --- | --- |
| 1. 有限 GC 策略 | 阈值/选块 DSL、固定安全余量、年龄元数据成本、独立参考和标准文件会话 | 前台回收；不含后台调度/磨损 |
| 2. 详细 SSD | LBA→逻辑页适配、复用 NAND 状态/GC/计时、控制器权限、独立块/页检查 | 主机命令串行，无完整 NVMe/FTL |
| 3. 跨层验收 | 两种容量、重复生命周期与 13 个声明候选，布局/预留/迁移/页分布/GC 联合运行；记录拒绝和缓存 | 合成工程用例，无 Agent 或最优性结论 |
| 4. 可信评价准备 | 标准 KV 导入与来源冻结；Ramulator 现有只读接口、MQSim SSD 导出/结果校验、裸 NAND 驱动协议 | 未采集真实模型、未安装运行仿真器、未校准；没有把接口准备写成完整 P6 |
| 5. 留待讨论 | 结合上述缺口决定下一项开发和研究验证范围 | 本轮不执行，也不预设开始 Agent 对照 |

本轮结果集中在[跨层验收](experiments.md#cross-layer-checks)，MQSim 能力边界只在其接口 README 维护。未满对象迁移、共享组拆分、布局转换、并发迁移仍后置；需要这些能力时，必须同时定义已写前缀复制、完整槽位预留、继续追加与计费，不能修改负载生命周期腾空间。

2026-10-28 补充：沿用完整候选，增加可选的逐字节地址来源诊断与[张量到 DRAM 教程](tensor-to-dram.md)。没有新增 VA/页表 DSL，也没有改变评分模型；后续是否显式模拟分页、缓存或格式转换，应由待研究的控制点与成本决定。

### 统一完成标准

逐文件职责统一见[代码导读](#从一次评价读代码)，不再另列重复的文件计划表。扩展只增加有实际执行语义的组件；不要提前建立空的通用固件、插件或子 Agent 框架。

各批完成须同时满足：一个可运行例子、手算正例与有意义的反例、独立内容验证、明确计费与能力边界、完整候选的文件会话、相关回归，以及当前文档更新。性能工具只消费明确操作；错误定位到对象、原始事件、target 和相关字段，非法提案不给性能分数，程序不偷偷纠错。

缓存包含冻结任务、资源/映射作用域、布局、管理策略、初始状态和执行/后端版本；有运行时策略时不能仅按初始地址表合并。一次完整混合候选评价消耗一次任务预算，同时分账各子后端调用与耗时；已开始评价的失败仍计费，不能把多个子后端误算成多个独立候选。

测试用 `python -m unittest discover -s tests -v`，相关历史回归沿用本文命令；只有计分/公共执行路径变化时才重跑受影响的整批归档。每批自审删除被替代分支，不建立空架构；README 只保留能力摘要，详细语法更新到本文和 [kv-layout.md](kv-layout.md)。新诊断集中在 `results/`，默认不入 Git，不改冻结证据。

本次讨论新增的 NAND 依据：[KIOXIA 地址转换](https://www.kioxia.com/content/dam/kioxia/shared/business/memory/mlc-nand/asset/productbrief/KIOXIA_Understanding_Logical_Block_Address_Tech_Brief.pdf)、[垃圾回收](https://apac.kioxia.com/content/dam/kioxia/shared/business/memory/mlc-nand/asset/productbrief/KIOXIA_Understanding_Garbage_Collection_Tech_Brief.pdf)、[Linux 闪存接口](https://www.kernel.org/doc/html/latest/filesystems/ubifs.html)。它们支持接口与状态约束，不能当作本模组已选定的具体器件参数。
