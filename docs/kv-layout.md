# KV 负载与布局：工作固定，存放方式可变

2026-10-20 更新。**负载规定必须完成什么读写，布局 DSL 决定元素放在哪里。** 两者都可用 JSON 表达，只有声明可变的策略进入搜索。当前可评价 DRAM、裸 NAND 页分布和封装 SSD 逻辑块资源；没有实现 GPU/PIM kernel 或完整 attention。[旧布局诊断](experiments.md#kv-layout-v1) · [统一运行接口](development.md)

如果“布局偏移、虚拟地址、介质地址”还不直观，先读[一个张量元素怎样到达 DRAM](tensor-to-dram.md)，再回到本文查配置。

## 任务固定什么

固定负载 v2 声明多个 `objects`，每个对象都是独立的 `[token, dimension]` 张量，可有不同形状和元素大小。[负载示例](../examples/kv_workload.json)包含 K/V，各为 16×128、每元素 2 B；各追加两次、每次 4 个 token，读取后提前结束。K 按 token 分组读取，V 按 dimension 分组读取。内容用唯一字节标签检查，不执行 BF16 算术。

| 负载字段/事件 | 语义 |
| --- | --- |
| `objects` | 对象名 → `{tokens, dimensions, element_bytes}`；名称以字母开头，只含字母、数字、下划线，最多 64 字符 |
| `append` + `object/count` | 追加指定对象的 count 个 token；每 token 单独形成一组完整维度写入 |
| `read` + `object/tokens/dimensions` | 读取指定对象的矩形范围；两轴均为 `[start, stop)`，不能读取该对象尚未写入的前缀 |
| `group_by: token` | 每 token 的选定维度为一组，逐行访问 |
| `group_by: dimension` | 每 dimension 的选定 token 为一组，逐列访问 |
| `create` + `object`，仅负载 v3 | 创建一个新代次，已写前缀归零；该对象必须尚未存活 |
| `invalidate` + `object`，仅负载 v3 | 使当前代次失效；之后不能读写，直到重新创建 |
| `end` | 必须是最后一个事件；允许未填满就结束，不表示释放、迁移或回收 |

事件按数组顺序执行，每组完成后才执行下一组。`group_by` 是本版本负载携带的**固定访问组织约定**，不表示应用数学语义必然要求这一分组，也不能由候选修改。`partial/full` 按固定槽位数与已写 token 数推导，不能手动声明阶段；未来若搜索分配块大小，需要另行区分逻辑长度与存储块容量。

负载解析检查类型、范围、每对象读前写、生命周期和结束顺序。最多 32 个对象，每对象 64×256 个元素；声明的逻辑数据不超过 1 MiB、4096 个事件、一百万次元素访问及 16 MiB 字节访问。旧 v1 在入口转成 v2 的对象 `V`；v2 保持全程存活、静态预留。v3 中所有对象初始未创建，须显式 `create`，不能继承上一代的已写前缀。它不是完整模型计算图或任意到达时间/依赖语言。

生命周期示例：`create A → append A → read A → invalidate A → create A`。第二次创建的 A 与第一次形状相同，但代次不同；读取新 A 前仍须写入。**创建/失效时机是固定负载，不是候选 DSL。** 当前仅支持整对象失效，`end` 不隐式释放。完整输入见 [`lifecycle_search.json`](../examples/lifecycle_search.json)，预留时机及执行规则见[分配契约](development.md#lifecycle-allocation)。

更完整的[生命周期用例](../examples/kv_lifecycle_search.json)使用 K0/V0 与 K1/V1 两组槽位：前者各容纳 2 个 token、后者各 4 个，每 token 32 B。K0/V0 写满后仍被读取，K1/V1 只写到一半；随后释放并重建 K0/V0，新代次也未写满。K 按 dimension 分组、V 按 token 分组，均是本合成用例的固定选择，不能解释为真实 GPU 的必然访存方式。槽位名称不决定 full 状态，状态仍由实际追加量推导；输入不含迁移动作。

这是串行 gather/scatter 执行器，只在**同一访问组、同一目标内**合并同一粒度的访问；一次 `read` 可能展开成多组，组间不复用缓存。DRAM 掩码写按整粒度计费；NAND 部分页更新读旧页、编程新页；SSD 部分块写由主机读改写。元素与 DRAM 粒度为 2 的幂，当前抽象 NAND 页/SSD 块不要求。布局粒度取各资源粒度与元素大小的最小公倍数；条带不切开元素，目的页/块可以，执行器拆分字节事务。

因此，按 token 连续存放通常减少追加写流量；按 dimension 连续存放通常减少这里的前缀读流量。实际 GPU 的缓存、线程访存分组或 PIM 命令可能改变这个取舍，不能直接沿用结果。

## 候选决定什么

每个对象继续使用两个内部布局构造：`tile` 切分逻辑轴，`order` 排列切分后的四个轴。下面是**单个对象的内部描述**，保留版本 1；单资源外层布局为 v2，多资源外层为 v3。每套地址位策略仍为 v0。

```json
{
  "version": 1,
  "tile": {"token": 4, "dimension": 8},
  "order": ["token_outer", "dimension_outer", "token_inner", "dimension_inner"]
}
```

对元素 `V[t,d]`，`token_outer=t//4`、`token_inner=t%4`；dimension 同理按 8 切分。`order` **从变化最慢到最快**排列这四个坐标，再按普通连续数组计算字节偏移。任意四轴排列均可，不仅是“token 优先”和“dimension 优先”两个预设。

不整除时保留 tile 尾部填充，各对象占用先向布局使用的传输粒度取整。单资源 v2 的总占用不能超过容量减去固定 `execution.base_address`（默认 0）；拓扑 v3 则逐区域核对全部填充和对齐空隙。有效元素不能重叠，填充不作为有效数据读写。

外层 `kv_layout` 的完整写法见[提案示例](../examples/kv_layout_proposal.json)：`objects` 中每个名字对应上述内部布局，`composition` 决定它们如何组合。

| 组合 | 字段与行为 |
| --- | --- |
| 拼接 | `{"kind":"concat","order":["K","V"],"alignment_bytes":64}`；依次放置对象，每个起点向指定对齐取整 |
| 交织 | `{"kind":"interleave","order":["K","V"],"chunk_bytes":256}`；各对象向 chunk 取整，按次序每轮取一块，耗尽的对象跳过 |

v2 的组合须列出所有对象且恰好一次；v3 中则列出该组的全部对象。对齐和 chunk 只能选任务 `design_space.layout_options` 中的值。chunk 是布局粒度的整数倍，不切开元素；它不是 token、tile、虚拟页或 NAND 页。组合不会改变负载事件顺序。

例如 K/V **各占 8 KiB**，各分成两个 4 KiB chunk：

| chunk | 拼接后的偏移 | 交织后的偏移 |
| --- | ---: | ---: |
| K0 | 0 | 0 |
| K1 | 4096 | 8192 |
| V0 | 8192 | 4096 |
| V1 | 12288 | 12288 |

两种设计占用都是 16 KiB，只改变存放位置。如果对象长度不同，交织会跳过已取完的对象，但各自最后一个 chunk 的填充仍计入容量。对象里的元素次序由各自 `tile/order` 决定，跨对象排列由 `composition` 决定。

KV 候选包含 `kv_layout`，有 DRAM 时另含 `dram_address_mapping`；按任务启用情况再包含 `allocation_policy`、`migration_policy`、`nand_page_mapping`。完整组件范围由 `inspect-task` 返回，固定组件也须提交，整个候选共用一次评价预算。旧单对象布局只在单对象任务中兼容；`read_every` 只保留在历史实验侧，新任务直接给出事件。

<a id="跨资源放置"></a>
## 跨资源放置

拓扑任务使用布局 v3：`objects` 仍保存上述内部布局，外层用 `groups` 替代单个 `composition`。每个对象只能属于一个组，每组包含 `composition` 与 `placement`。**这里的布局组只是存放关系，不是并行算子或新的负载事件。**

下面是一个组的片段；完整可运行提案见 [`module_layout_proposal.json`](../examples/module_layout_proposal.json)：

```json
{
  "composition": {"kind": "concat", "order": ["K", "V"], "alignment_bytes": 64},
  "placement": {"regions": ["r0", "r1"], "stripe_bytes": 4096}
}
```

先把组内对象组成连续的字节流，再每次取 `stripe_bytes` 字节，轮流放到 `regions`。只有一个区域就是整组放置；区域列表的顺序也是候选决策。目标资源由硬件的 region 引用确定，不能提交新的资源或容量。`composition.chunk_bytes` 决定**对象之间怎么交织**，`placement.stripe_bytes` 决定**组合流怎么分布到资源**，两者作用不同。

沿用 K/V 各 8 KiB 的例子，两个区域的基址均为 0：

| 对象 chunk | 拼接后按 4 KiB 条带分布 | 先对象交织，再按 4 KiB 条带分布 |
| --- | --- | --- |
| K0 | r0，偏移 0 | r0，偏移 0 |
| K1 | r1，偏移 0 | r0，偏移 4096 |
| V0 | r0，偏移 4096 | r1，偏移 0 |
| V1 | r1，偏移 4096 | r1，偏移 4096 |

若 r0/r1 属于不同 target，它们的偏移 0 是不同存储位置；若属于同一 target 的两个区域，则加上各自固定基址后才解码介质地址。一个 target 的区域不得重叠，也不会因为区域更多就多出控制器或带宽。

v2 负载的多个组按候选顺序静态预留；v3 负载按固定首次适配选择空闲位置，最后一个存活成员失效后释放整组。默认首个成员创建时预留；`allocation_policy` 可延至首次访问，任一成员触发仍预留整组。`kv_layout` 决定相对排列、初始目标及共同分配的组，成员关系也影响可回收粒度。另有[迁移策略](development.md#fixed-migration)：选择是否迁移、写满后或写满后首次读取前迁移，以及目的区域；迁移对象必须单独一组且只占一个初始区域，复制完成后改位置，布局不变。

组起点向 stripe 对齐，尾部不足一条带也完整计容。stripe 从 `chunk_bytes` 白名单选取，须整除区域基址，并为 concat 对齐值的整数倍。区域越界或存活组无法找到连续空闲区均非法；不自动拆分、压缩或改换目标。

NAND 区域中的地址属于**逻辑页空间**。下层 `nand_page_mapping` 可选择通道/die 条带，控制器负责合法分配新物理页；不启用新模式则沿用固定递增分配。释放布局组只解除逻辑绑定，不擦除旧页，所以仍可能耗尽物理空页。异地更新、发布和块内编程顺序由执行器约束；字段与计费见[NAND 控制器](development.md#nand-controller)。这与上层选择哪个 region、怎样排列 K/V 是不同职责。

SSD 区域中的字节地址按逻辑块大小转换为 **LBA**。抽象模式可选 SSD 区域和条带，不开放内部 NAND；详细模式复用 NAND 层，并按硬件声明开放或封闭控制器策略。部分块写即使是首次写入，也执行整块读取、主机合并和整块写入；抽象模式先处理读改写；详细模式按请求顺序串行完成主机命令，内部页可并行。字段和限制见[SSD 接口](development.md#ssd-interface)，提案见 [`pool_layout_proposal.json`](../examples/pool_layout_proposal.json)。

## 一个能手算的例子

单独观察一个对象：4×4 个元素、每元素 2 B、传输粒度 8 B、容量 32 B，先写完四个 token，再读取一次。每次读固定取一个 dimension 的四个 token。

| 布局（`order` 沿用示例） | 写传输次数 | 读传输次数 | 总传输字节 |
| --- | ---: | ---: | ---: |
| `tile={token:1, dimension:4}`，token 连续 | 4 | 16 | 160 |
| `tile={token:4, dimension:1}`，dimension 连续 | 16 | 4 | 160 |
| `tile={token:2, dimension:2}`，折中分块 | 8 | 8 | 128 |

2×2 分块的元素字节地址如下，每行是一个 token：

```text
 0   2   8  10
 4   6  12  14
16  18  24  26
20  22  28  30
```

例如第一行写入涉及 `[0,8)` 和 `[8,16)` 两个粒度，所以是两次传输；第一列读取涉及 `[0,8)` 和 `[16,24)`，也是两次。字节计数来自这些访问，没有人为给某种布局加奖励。

## 验证与计分边界

`normalize` 检查内部布局、对象分组、组合/条带、区域权限及与迁移规则的兼容性。`check` 独立枚举元素和条带位置，从源事件重建预期访问，再重放实际事务；检查动态容量、对象/target 身份、路由、覆盖、读回值、填充保护及迁移触发/复制/发布。它不证明并发迁移或任意未来输入正确；见[验证边界](development.md#正确性与错误反馈)。

单资源可选 `line-traffic-v2`（只搜布局）或 `fcfs-serial-kv-v2`（周期）。拓扑按整数 ps 排名，`fcfs` 按资源和执行模式选择版本；NAND 页分布使用 `fcfs-nand-policy-v1`，固定前台 GC 为 `fcfs-nand-gc-v1`，可搜索 GC 为 `fcfs-nand-gc-policy-v1`；详细 SSD 使用独立的 `fcfs-ssd-nand[-policy]-v1` 身份。它们可与分配/迁移组合，均保留串行逻辑组。对应关系见[评价路径](development.md#评价路径)。旧成绩保持原身份，当前没有真实带宽或 token/s 结论。

缓存包含冻结任务、后端及 DRAM/NAND 策略；静态布局按有效地址与预留量识别等价，生命周期布局保留完整规范化分组及分配/迁移策略。不能因初始偏移相同就合并状态行为不同的设计。非法候选不进入性能评价，反例关联源事件并返回 Agent。

已实现多对象布局、混合资源、显式生命周期、有限预留、整对象迁移、NAND 页分布/前台 GC 策略及详细 SSD；共享/淘汰、数值归约、部分对象/并发迁移与布局转换尚未实现。当前 NAND 管理器不等于完整 FTL，也不能据此声称 PATTON/PFM 已复现。下一批见[开发计划](development.md#implementation-plan)。

## 运行

```powershell
python -m agent_design_search session-init results/layout_session --task examples/memory_search.json
python -m agent_design_search session-step results/layout_session --proposal examples/kv_layout_proposal.json
python -m agent_design_search session-finish results/layout_session
python -m experiments.layout_study
```

新会话的公共起点计入预算，`task.json` 保存完整输入。只测布局传输量时，将 `vary` 改为 `["kv_layout"]`、后端改为 `line-traffic-v2`、目标改为 `transferred_bytes`，移除时序参数并保持 identity 映射。`evaluate` 接收完整设计，不能直接使用带 `hypothesis/expected/dsl` 外壳的提案。

`layout_study` 比较三种固定布局、解析选择和完整合法空间枚举；输出一份 `study.json`，包含任务、候选、计数、源码哈希和枚举分数摘要。输出目录必须是新目录。它是开发诊断，已经揭示最优值，不能再称为盲测或新的 Agent 实验。

代码入口：`inputs/workload.py` 解析对象和事件；`spaces/kv_layout_dsl.py` 定义内部布局与组合；`exploration/memory_search_task.py` 组合策略；`execution/memory_system.py` 生成事务并执行；`verification/memory_contents.py` 独立重放；`performance/` 提供计时工具。旧布局计数和诊断由 `experiments/` 调用，不作为新任务入口。
