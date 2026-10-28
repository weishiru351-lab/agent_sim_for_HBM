# 文献阅读指南：内存数据组织、系统取舍与 Agent 探索

研究内容与外链集中核验日期：**2026-09-29**；全文下载与题录补充日期：**2026-10-08**。

这份指南统一收录目前与本项目直接相关、值得继续阅读的 **20 篇工作**，其中 R20 同时保留预印本与正式会议版的状态说明。每篇说明解决的问题、对本项目的意义和阅读重点；全文、题录、预印本与正式发表分别标注。它是立项阅读清单，不是穷尽性综述。

**本地全文：** 维护者已保存并验证 **20 篇 PDF**；[references.bib](../paper/references.bib)包含全部 20 篇题录及预期附件路径。R03 Flatfish 与 R20 UMDAM 已由用户补齐正式出版版。**PDF 不随 Git 仓库分发**，克隆后请按 [paper/ 论文库说明](../paper/README.md)按需获取；下面的本地 PDF 链接在下载对应文件后生效，版本以论文库记录为准。

**阅读要建立三个判断：目标平台上存在什么真实的系统取舍；现有强方案还留下什么优化余量；Agent 的持续决策增加了什么。** 映射敏感性只是其中一项机制证据。当前以 KV 生命周期为候选切入方向，纯地址位搜索作为基础对照；跨文献结论及开发原则见[研究定位](research-direction.md#literature-to-principles)，工程入口见[项目 README](../README.md)。

2026-10-28 范围提醒：这 20 篇主要覆盖 DRAM、KV/PIM 和 Agent 搜索，**不是 NAND/SSD 控制器的完整文献集**。页分布、前台 GC 和详细 SSD 已有原型实现，真实建模仍需补充 FTL、并行约束和回收成本资料；已有基础资料入口在[开发指南](development.md#nand-controller)及其末尾。本轮文档核对不改变下面的外链与出版状态核验日期。

**正文精读统一维护在[持续研究笔记](reading-notes.md)**：保存最初的问题、V1–V3 实验疑惑、逐篇证据和认识修订。[第四轮](reading-notes.md#round-4)核查 KV 生命周期；[第五轮](reading-notes.md#round-5)区分 Agent 与优化器贡献；[第六轮](reading-notes.md#round-6)明确仿真与 RTL 验证边界；[第七轮](reading-notes.md#round-7)复核映射等价关系、统计已有任务的有效空间，并依据 LLAMBO 附录细化反馈对照。目前 20 篇均有明确范围的正文记录，**不表示全部正文/附录核查完毕或已复现论文**。各轮入口及[最新进度](reading-notes.md#reading-next)集中在笔记中，本指南不重复维护详细证据。

## 先读哪几篇

下面是精读后重新整理的问题导向路线。新读者先看系统问题，再看 Agent 的可检验贡献；既有 R 编号和后面的文献条目保留，编号不表示当前研究优先级。

| 阅读路线 | 建议顺序 | 带着什么问题读 |
| --- | --- | --- |
| 1. 找到真实系统取舍 | [R12 PagedAttention](#r12) → [R13 vAttention](#r13)；再对照 [R15 PATTON](#r15)、[R14 PFM](#r14)、[R20 UMDAM](#r20) | 软件分块、虚拟地址与介质映射有何区别？读写、计算和容量为何冲突？已有机制解决了什么？分清 GPU 与 NPU/PIM 的权限及收益口径 |
| 2. 明确 Agent 的研究增量 | [R05 AgentDSE](#r05) → [R06 ArchEval](#r06) → [R17 LLAMBO](#r17)；对照 [R10 gem5 Co-Pilot](#r10)、[R11 ChatNeuroSim](#r11)、[R16 AlphaEvolve](#r16) | 谁实际提出和选择候选？哪些消融真的移除了新反馈？如何区分先验、生成、选择、工具和决策贡献？不能把工作流相似当成新颖性 |
| 3. 建立强机制与搜索对照 | [R01 Shuhai](#r01) → [R02 SDAM](#r02) → [R04 XOR 模型](#r04)、[R08 PAE](#r08)、[R03 Flatfish](#r03) → [R09 ArchGym](#r09) | 默认之上还有多少余量？有效空间是否远小于表达式数？数学结构、随机矩阵、学习搜索及调参后的算法分别能做到什么？ |
| 4. 约束动态策略和评价模型 | [R07 DReAM](#r07)，结合 R14/R15；再读 [R18 Ramulator](#r18)、[R19 DRAMSys4](#r19)及[厂商资料](#vendor-docs) | 切换如何维护数据位置？搬移和暂存成本如何计入？固定流量、依赖前端、服务模型和 RTL 各提供什么证据？ |

阅读每篇应记下：改变的组件、硬件权限、负载来源、目标指标、基线重建方式、离线/在线成本、验证层次及未覆盖项。例如 PATTON 的局部 attention/额外 prefill 口径不是完整服务吞吐，PFM 的联合方案收益不是纯地址位收益，UMDAM 的权重证据不能直接代表 KV 生命周期。详细出处见[第四轮笔记](reading-notes.md#round-4)。

## 一、映射问题与 Agent 流程的直接相关工作

<a id="r01"></a>
### R01 · Shuhai：真实 HBM 的映射敏感性

**Shuhai: Benchmarking High Bandwidth Memory on FPGAs** · FCCM 2020。\
入口：[作者提供的全文 PDF](https://wangzeke.github.io/doc/shuhai_fccm_20.pdf)。

本地：[PDF](../paper/R01_Shuhai_FCCM2020.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 在 Alveo U280 FPGA 的 HBM 上测量访问行为，比较步长、突发长度和地址映射对带宽的影响。
- **与本项目的关系：** 提供真实 HBM 证据。尤其要注意：在论文比较的映射选项与 stride/burst 测试中，默认 RGBCG 表现最好；坏映射造成巨大损失，不等于还存在同等幅度的优化空间。
- **重点读：** §V.C 的 Address Mapping。记录默认配置、测试范围与限制，分别回答“映射敏感性有多大”和“超过默认方案的余量有多大”。

<a id="r02"></a>
### R02 · SDAM：按数据结构选择地址映射

**Software-Defined Address Mapping: A Case on 3D Memory** · ASPLOS 2022。\
入口：[作者提供的全文 PDF](https://pages.cs.wisc.edu/~swift/papers/asplos22-sdam.pdf)。

本地：[PDF](../paper/R02_SDAM_ASPLOS2022.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 将程序的数据结构及访问需求与地址映射选择连接起来，通过分配器、操作系统和硬件共同支持不同映射。
- **与本项目的关系：** 说明“不同数据适合不同映射”已有系统实现先例。论文包含 FPGA HBM 原型及 Linux/glibc 支持；这些能力依赖平台设计，不能直接假设普通 GPU 已开放。
- **重点读：** 映射选择接口、数据分配路径和原型实现。明确粒度、地址解释与软硬件改动，再考虑扩展本项目 DSL。

<a id="r03"></a>
### R03 · Flatfish：学习生成地址映射的直接相关工作

**Flatfish: A Reinforcement Learning Approach for Application-Aware Address Mapping** · IEEE TCAD 41(11):4758–4770，2022。\
入口：[DBLP 题录，可访问但不含全文](https://dblp.org/rec/journals/tcad/LiYGSZWN22)。\
DOI：`10.1109/TCAD.2022.3146204`。

本地：[正式出版版 PDF](../paper/R03_Flatfish_TCAD2022.pdf)（13 页；[来源说明](../paper/README.md#source-r03)）。

- **解决什么：** 以强化学习生成应用相关地址映射，用可逆二进制矩阵表示候选，并以请求平均响应时间作为奖励依据。离线主体动作是交换位，正文另有加入 XOR 的扩展实验。
- **与本项目的关系：** 它比通用 Agent 搜索论文更直接接近“AI + 可组合映射”。需要对照其问题定义和方法，不能将学习生成映射本身作为新贡献。
- **重点读：** §III 的状态、动作与训练，§V 的在线估计器与迁移约束，§VI 的评价及 XOR 扩展。已完成首轮方法与实验正文核查，具体边界及摘要/正文数字差异见[精读记录](#close-reading-flatfish)。

2026-10-08 补充：此前自动下载受限，用户已提供 IEEE Xplore 正式出版版。已核对题名、作者、DOI、卷期和页码；上述概述与全文摘要相符，不引用摘要中的性能数字做横向比较。

<a id="r04"></a>
### R04 · XOR 映射数学模型：搜索空间并非只能当黑盒

**A Mathematical Model for XOR-Based Application Specific DRAM Address Mapping Schemes** · MEMSYS 2025。\
入口：[Fraunhofer 机构库全文 PDF](https://publica-rest.fraunhofer.de/server/api/core/bitstreams/04740464-f7d5-4d21-9925-5a0a31a35cb4/content)。

本地：[PDF](../paper/R04_XOR_Address_Mapping_MEMSYS2025.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 在二元域 GF(2) 上研究 XOR 地址映射，将行失配优化转化为子空间选择问题，讨论硬件复杂度、整数规划和启发式方法。
- **与本项目的关系：** 为 DSL 约束、差向量统计和结构化强基线提供依据。固定 bank 后的 row 子问题有可利用结构；不能忽略 bank 分流就合并联合 kernel 相同的策略。NP-hard 与强贪心可同时成立，都不直接证明 Agent 优势，见[等价关系审计](reading-notes.md#mapping-equivalence)。
- **重点读：** 优化变量、目标函数、困难性与求解规模。行失配是其核心目标之一，不能直接等同于请求完成时间或推理吞吐。

<a id="r05"></a>
### R05 · AgentDSE：与当前 Agent 流程高度接近

**AgentDSE: Reasoning-Augmented Architectural Design Space Exploration** · MLArchSys Workshop @ ISCA 2026；同年 arXiv 预印本。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2606.21836) · [全文 HTML](https://arxiv.org/html/2606.21836v1)。

本地：[PDF](../paper/R05_AgentDSE_arXiv2606.21836v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** Agent 在描述设计空间的文件工作区中提出候选、调用评价器、读取历史反馈并修正假设，实验涉及 Timeloop、MAESTRO 和 ChampSim。
- **与本项目的关系：** 文件接口、预算、历史记录和假设反馈循环均与当前方案重叠。需要在具体内存问题或搜索机制上提出增量；“接一个 HBM 后端”本身尚不足以说明方法创新。
- **重点读：** 工作区协议、基线、预算和语义匿名化消融。比较它提供给 Agent 的信息与本项目计划增加的诊断证据。

<a id="r06"></a>
### R06 · ArchEval：怎样评价“Agent 会做体系结构研究”

**ArchEval: Measuring AI Agents as Computer Architects** · Architecture 2.0 Workshop @ ISCA 2026；同年 arXiv 预印本。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2607.03601) · [全文 HTML](https://arxiv.org/html/2607.03601v1)。

本地：[PDF](../paper/R06_ArchEval_arXiv2607.03601v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 用 20 个挑战、8 种仿真器及不同工具支持等级，评价 Agent 的体系结构设计与推理能力。
- **与本项目的关系：** 候选能运行、性能确实改善、模型能可靠预测改善，是三件不同的事。其内存任务包含 DRAMSys 控制器调优与 Ramulator RowHammer 任务，但并非本项目的 HBM/KV 映射基准。
- **重点读：** 任务表、工具支持等级、评价方法与失败案例。将正确性、搜索成绩、预测准确性和成本分开记录。

R05 的 Workshop 状态依据 [作者主页](https://iamchenyuwang.github.io/)；R06 另可核对 [Workshop 官方日程](https://harvard-edge.github.io/isca-26-arch-2-workshop/)。Workshop、预印本与主会论文在本指南中分别标注。

## 二、地址映射的机制与已有优化方法

<a id="r07"></a>
### R07 · DReAM：动态映射及其切换代价

**DReAM: Dynamic Re-arrangement of Address Mapping to Improve the Performance of DRAMs** · 2015 年 arXiv 预印本。\
入口：[题录与摘要](https://arxiv.org/abs/1509.03721) · [全文 PDF](https://arxiv.org/pdf/1509.03721)。

本地：[PDF](../paper/R07_DReAM_arXiv1509.03721v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 根据应用的访问行为调整 DRAM 地址映射，研究固定映射难以适应不同访问模式的问题。
- **与本项目的关系：** 动态适应负载已有先例；当已有数据仍在内存中时，改变地址解释会带来迁移和一致性问题。
- **重点读：** 访问特征、调整触发条件与数据迁移机制。区分部署前离线选策略和运行中切换策略。

<a id="r08"></a>
### R08 · PAE：用地址位熵理解 GPU 映射

**Get Out of the Valley: Power-Efficient Address Mapping for GPUs** · ISCA 2018。\
入口：[作者提供的全文 PDF](https://users.elis.ugent.be/~leeckhou/papers/isca18.pdf)。

本地：[PDF](../paper/R08_PAE_ISCA2018.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 分析窗口内地址位熵与 GPU 存储并行度之间的关系，提出基于二进制可逆矩阵的地址映射方案 PAE。
- **与本项目的关系：** 少量随机合法矩阵也可能接近好策略，巨大空间不一定需要复杂搜索；先核查有效难度。其收益同时涉及缓存/片上网络，不能只用固定 DRAM 轨迹复现。当前位活动启发式不构成 PAE 复现，见[精读记录](reading-notes.md#reading-pae)。
- **重点读：** “entropy valley”的形成、矩阵映射机制和评价条件，检查哪些统计可以作为低成本诊断信号。

## 三、体系结构搜索与 Agent：建立有竞争力的对照

<a id="r09"></a>
### R09 · ArchGym：统一接口下比较搜索方法

**ArchGym: An Open-Source Gymnasium for Machine Learning Assisted Architecture Design** · ISCA 2023。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2306.08888) · [作者提供的全文 PDF](https://devashreetrip.github.io/publication/Website_ArchGym_ISCA2023.pdf)。

本地：[PDF](../paper/R09_ArchGym_ISCA2023.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 将体系结构仿真器与不同搜索算法连接起来，比较算法、调参和采样对设计空间探索的影响，包含内存控制器任务。
- **与本项目的关系：** 是设计实验框架和强基线的重要参考。算法能否胜出可能取决于其自身参数和预算，不能只与默认设置的随机或局部搜索比较。
- **重点读：** 统一接口、搜索算法设置、调参成本和实验预算。发表信息可核对 [ISCA 2023 官方程序](https://www.iscaconf.org/isca2023/program/)。

<a id="r10"></a>
### R10 · gem5 Co-Pilot：语言模型与已有设计空间数据

**gem5 Co-Pilot: AI Assistant Agent for Architectural Design Space Exploration** · CAMS Workshop 2025。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2510.19577) · [作者提供的全文 PDF](https://arg.csl.cornell.edu/papers/gem5-co-pilot-cams-25.pdf)。

本地：[PDF](../paper/R10_gem5_CoPilot_arXiv2510.19577v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 将自然语言交互、设计空间数据库 DSDB 和体系结构探索连接起来，论文包含 L2 cache 配置实验。
- **与本项目的关系：** 说明“Agent + 体系结构工具”已有相邻实现；也提醒我们区分查询预先计算的结果与新执行昂贵仿真。
- **重点读：** 数据库来源、Agent 的实际决策范围和计算成本边界。它不是 HBM bank 映射方案。具体指标、批轮数和提示消融的限制见[精读记录](reading-notes.md#reading-gem5-copilot)。

<a id="r11"></a>
### R11 · ChatNeuroSim：约束检查与仿真流程自动化

**ChatNeuroSim: An LLM Agent Framework for Automated Compute-in-Memory Accelerator Deployment and Optimization** · 2026 年 arXiv 预印本，未在本轮确认正式发表。\
入口：[题录与摘要](https://arxiv.org/abs/2603.08745) · [全文 PDF](https://arxiv.org/pdf/2603.08745)。

本地：[PDF](../paper/R11_ChatNeuroSim_arXiv2603.08745v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 使用 LLM Agent 组织存内计算加速器的配置、约束检查和 NeuroSim 仿真，并结合设计空间裁剪。
- **与本项目的关系：** 工具代理、配置检查和自动运行均已有相关工作；这些是必要工程能力，其本身与发现更优策略要分别评价。
- **重点读：** Agent 和确定性程序分别承担什么，裁剪规则从何而来，省下的是人工操作还是后端评价。脚本成功率、SA/GA/TPE 与迁移成本的区别见[精读记录](reading-notes.md#reading-chatneurosim)。

## 四、大模型 KV：区分现有 GPU 软件与定制硬件

<a id="r12"></a>
### R12 · PagedAttention：KV 块管理与服务系统

**Efficient Memory Management for Large Language Model Serving with PagedAttention** · SOSP 2023。\
入口：[题录与摘要](https://arxiv.org/abs/2309.06180) · [全文 PDF](https://arxiv.org/pdf/2309.06180)。

本地：[PDF](../paper/R12_PagedAttention_arXiv2309.06180v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 通过 KV 分块管理减少内存浪费，支持共享并改善 LLM 服务系统的内存使用和吞吐。
- **与本项目的关系：** 是现有 GPU 软件路线的机制基线。更高的容量利用和并发可能抵消 kernel 额外开销，说明单个访存/kernel 指标不足以评价服务收益；其 KV 块管理与控制器位映射不同，见[分层与评价](reading-notes.md#reading-kv)。
- **重点读：** 逻辑块到物理块的关系、attention kernel 如何访问块，以及收益来自容量、并发还是单请求执行。

<a id="r13"></a>
### R13 · vAttention：虚拟连续与物理动态分配

**vAttention: Dynamic Memory Management for Serving LLMs without PagedAttention** · ASPLOS 2025；预印本首次发布于 2024 年。\
入口：[题录与摘要](https://arxiv.org/abs/2405.04437) · [全文 PDF](https://arxiv.org/pdf/2405.04437)。

本地：[PDF](../paper/R13_vAttention_arXiv2405.04437v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 借助 CUDA 虚拟内存管理接口，使 KV 的虚拟地址保持连续，同时动态管理物理分配。
- **与本项目的关系：** 展示现有 GPU 软件可利用的具体自由度。虚拟地址连续不等于介质位置连续，也不意味着软件能任意改写 HBM 控制器映射。
- **重点读：** 分配粒度、预分配/异步管理、kernel 兼容性和完整运行开销。与 PagedAttention 成对阅读。

<a id="r14"></a>
### R14 · PFM：NPU/PIM 的两种访问视图

**Rethinking Unified Memory for NPU-PIM Systems: Dual-View Memory for Dynamic Inference of LLM** · 2026；arXiv 作者说明标注“MICRO 2026 接收”，本轮未独立核对正式出版页。\
入口：[题录、摘要与接收说明](https://arxiv.org/abs/2608.06989) · [全文 PDF](https://arxiv.org/pdf/2608.06989)。

本地：[PDF](../paper/R14_PFM_arXiv2608.06989v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 面向 NPU/PIM 动态推理，使不同执行单元以不同逻辑视图访问共享数据，协调布局、地址转换与执行。
- **与本项目的关系：** 预先选择共享布局，再由地址转换、命令调度和设备选择服务双访问者，提供联合优化的机制基线；并非每次切换都重排张量。它依赖特定异构架构，不能直接外推到普通 GPU，见[精读记录](reading-notes.md#reading-pfm)。
- **重点读：** 两种访问视图的数据一致性、解析/整数求解器的目标范围、控制器与缓冲成本、RTL 延迟怎样反馈性能模型。联合收益不能归为纯映射收益。

<a id="r15"></a>
### R15 · PATTON：KV 生命周期与 PIM 数据组织

**PATTON: Enabling Commodity PIM for Production LLM Serving** · 2026-09-10 发布的 arXiv 预印本，未在本轮确认正式发表。\
入口：[题录与摘要](https://arxiv.org/abs/2609.11392) · [全文 HTML](https://arxiv.org/html/2609.11392v1)。

本地：[PDF](../paper/R15_PATTON_arXiv2609.11392v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 协调 PIM 服务系统中的 KV 生命周期、布局和分配，处理 Value cache 在计算访问、逐 token 写入和容量利用之间的取舍。
- **与本项目的关系：** 将“KV 很复杂”具体化为 K/V 归约维度、追加写入、partial/full 状态与容量/复用冲突，是候选系统任务的依据及已有专门方案。其 AiM/GDDR6 PIM 条件不能直接充当普通 HBM GPU 的能力证明，见[精读记录](reading-notes.md#reading-patton)。
- **重点读：** 暂存和目标同时占用容量、填满搬移后发布位置、早停与共享回收；区分写入、attention 加速和完整服务指标。更高原始 GEMV 效率可能伴随更差容量利用。

## 五、通用搜索启发：保留，但不替代直接相关工作

<a id="r16"></a>
### R16 · AlphaEvolve：程序生成、进化搜索与自动评价

**AlphaEvolve: A coding agent for scientific and algorithmic discovery** · Google DeepMind，2025 年技术报告/预印本。\
入口：[题录与摘要](https://arxiv.org/abs/2506.13131) · [全文 PDF](https://arxiv.org/pdf/2506.13131) · [官方介绍](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)。

本地：[PDF](../paper/R16_AlphaEvolve_arXiv2506.13131v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 结合语言模型生成程序、候选库、自动评价与进化搜索，持续改进可执行候选；官方介绍包含经验证的电路代码优化案例。
- **与本项目的关系：** 可借鉴候选库和结构化程序修改，用受限 DSL 表达策略，并使优化与正确性检查配套。这项工作来自 Google DeepMind。
- **重点读：** 评价器决定什么、候选如何被保留和生成、约束如何执行。其应用成功不直接证明小型 XOR DSL 上的搜索优势。

<a id="r17"></a>
### R17 · LLAMBO：让语言模型增强优化器的部分环节

**Large Language Models to Enhance Bayesian Optimization** · ICLR 2024。\
入口：[会议正式题录](https://proceedings.iclr.cc/paper_files/paper/2024/hash/84b8d9fcb4e262fcd429544697e1e720-Abstract-Conference.html) · [会议全文 PDF](https://proceedings.iclr.cc/paper_files/paper/2024/file/84b8d9fcb4e262fcd429544697e1e720-Paper-Conference.pdf) · [作者代码](https://github.com/tennisonliu/LLAMBO)。

本地：[PDF](../paper/R17_LLAMBO_ICLR2024.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 在贝叶斯优化中使用语言模型进行起始候选选择、代理建模和候选采样，实验主要围绕超参数优化。
- **与本项目的关系：** 借鉴模块化归因，将 Agent 主导探索中的初始先验、候选生成与选择分开检验；不将项目主线缩成一次性建议。无上下文/无指令消融仍保留历史，不能代替无新反馈组；合法性与多样性也可能解释提示收益，见[附录核查](reading-notes.md#llambo-appendix)。
- **重点读：** 公共初始化、候选池与选中值、代理校准、跨任务失败和真实调用成本。连续参数提示不能照搬离散 DSL；模型预测和不确定性不能当作后端实测或置信保证。

## 六、后端工具：选定路线后再深入

<a id="r18"></a>
### R18 · Ramulator 2.0：模块化 DRAM 时序仿真

**Ramulator 2.0: A Modern, Modular, and Extensible DRAM Simulator** · IEEE Computer Architecture Letters 23(1):112–116，2024；预印本与在线发表在 2023 年。\
入口：[arXiv 题录](https://arxiv.org/abs/2308.11030) · [预印本全文 PDF](https://arxiv.org/pdf/2308.11030) · [官方代码](https://github.com/CMU-SAFARI/ramulator2)。

本地：[PDF](../paper/R18_Ramulator2_arXiv2308.11030v1.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 提供可扩展的 DRAM 仿真框架，以模块接口组织内存设备、控制器等部件。
- **与本项目的关系：** 是后续评价器候选；已有固定 2.1 提交的只读适配器源码，但 C++ 尚未编译/运行，V2/V3 仍使用玩具后端。需要固定设备模型、控制器、地址映射和请求输入配置。
- **重点读：** 模块边界、请求生成和命令验证范围。具体证据见[精读记录](reading-notes.md#reading-ramulator)，现有桥接与闭环负载的差别见[评价契约](reading-notes.md#backend-contract)。

版本提醒：论文介绍 2.0，本地保留 arXiv v1，arXiv 还提供 v2；项目适配器固定 2.1 提交。论文版本、软件版本和具体提交分别记录，不能混用接口或验证结论。

<a id="r19"></a>
### R19 · DRAMSys4.0：系统分析与已有映射描述格式

**DRAMSys4.0: An Open-Source Simulation Framework for In-depth DRAM Analyses** · International Journal of Parallel Programming 50:217–242，2022。\
入口：[出版社开放全文](https://link.springer.com/article/10.1007/s10766-022-00727-4) · [官方配置文档](https://github.com/tukl-msd/DRAMSys/blob/master/configs/README.md)。

本地：[PDF](../paper/R19_DRAMSys4_IJPP2022.pdf)（版本与来源见 [论文库](../paper/README.md)）。

- **解决什么：** 为 DRAM 系统建模和分析提供框架；官方配置文档已有按字段和 XOR 位组合描述映射的方式。
- **与本项目的关系：** 可作为另一后端及 DSL 设计参考。“用 JSON 写映射”已有基础，当前 DSL 的价值主要在于统一语义、合法性和后端一致性。
- **重点读：** 模型精度、分析能力及配置文档中的地址映射部分。目标控制器校准、elastic traces 与 RTL 嵌入见[精读记录](reading-notes.md#reading-dramsys)。2022 年文中的工具比较不代表后续版本的当前能力。

<a id="vendor-docs"></a>
## 七、厂商资料：确认搜索空间是否真实可用

这些是产品文档，不是论文。三个页面针对不同 IP，结论不能混用；2026-10-16 再次核对了下列页面的版本与对应描述。

| 官方入口 | 本轮版本 | 值得了解的内容 |
| --- | --- | --- |
| [AMD PG313 · Address Mapping](https://docs.amd.com/r/en-US/pg313-network-on-chip/Address-Mapping) | 1.1 | 区分线性/随机块与纯随机访问，讨论 bank-group 交织的取舍。 |
| [AMD PG150 · Address Map](https://docs.amd.com/r/en-US/pg150-ultrascale-memory-ip/Address-Map) | 1.4 | 其 DDR4 控制器中，随机流对若干映射选项的效率差别较小；顺序小步长有特定建议。 |
| [AMD PG276 · HBM Reordering Options](https://docs.amd.com/r/en-US/pg276-axi-hbm/HBM-Reordering-Options) | 1.0 | 自定义地址映射会关闭许多重排选项，映射与调度未必能自由组合。 |

选定目标平台时，逐项核实：允许改哪些位、配置粒度、是否影响调度、何时能切换，以及数据写入与读取如何保持一致。

<a id="r20"></a>
## 八、UMDAM：正式会议论文与预印本版本说明

**UMDAM: A Unified Data Layout and DRAM Address Mapping for Heterogeneous NPU-PIM**。本地收录 [ISCAS 2026 正式版](../paper/R20_UMDAM_ISCAS2026.pdf)，5 页；引用与页码以此版为准。预印本撤回不等于正式版撤回，完整题录、撤回背景与核验来源统一见[论文库](../paper/README.md#source-r20)。

重点结合 R14、R15 阅读布局与映射联合设计、仿真基线及实机验证范围。它主要处理权重共享和重排，不能作为 KV 生命周期已经解决的证据；具体范围见[精读记录](#close-reading-umdam)。R20 计入本指南的 20 篇工作。

## 九、基础概念资料

这些资料用于补齐概念和工具使用，主研究文献仍按上面的顺序阅读。

| 想弄清楚什么 | 入口 |
| --- | --- |
| DSL 是什么，为什么不等于底层语言 | [Martin Fowler · DSL Q & A](https://martinfowler.com/bliki/DslQandA.html) |
| RTL、数字电路综合是什么 | [Yosys · Primer](https://yosyshq.readthedocs.io/projects/yosys/en/v0.65/appendix/primer.html) |
| 如何编译和运行小型 RTL 仿真 | [Icarus Verilog · Getting Started](https://steveicarus.github.io/iverilog/usage/getting_started.html)、[Simulation](https://steveicarus.github.io/iverilog/usage/simulation.html) |

## 十、链接核验与阅读记录

外链于 2026-09-29 集中核验；2026-10-16 复核了上述三份 AMD 资料与 DRAMSys 映射配置说明，未重新遍历全部外链或复现论文。全文获取与题录可访问是不同状态，链接可用性也可能变化。2026-10-08 收齐 20 篇 PDF，其中 Flatfish、UMDAM 由用户补充；版本、下载入口与访问限制统一在[论文库](../paper/README.md)维护。

当前阅读范围见下方笔记。跨论文比较应记录平台/负载、控制权限、约束与实现代价、最强基线、搜索/训练成本和未解决问题，避免按不同口径的加速比排名；立项标准见[研究定位](research-direction.md)。

<a id="close-reading-1"></a>
## 十一、持续精读记录

正文精读、问题台账和认识修订已集中移入[研究笔记](reading-notes.md)。该文档持续关联最初研究问题、初版玩具与 V2/V3 的疑惑和新文献证据；本指南保留论文目录及阅读入口，避免重复维护。

<a id="close-reading-flatfish"></a>
- [Flatfish：搜索空间、奖励、成本与实验边界](reading-notes.md#close-reading-flatfish)

<a id="close-reading-xor"></a>
- [XOR 数学模型：困难性、结构化方法与适用条件](reading-notes.md#close-reading-xor)

<a id="close-reading-agentdse"></a>
- [AgentDSE：已有流程、语义消融与成本口径](reading-notes.md#close-reading-agentdse)

<a id="close-reading-umdam"></a>
- [UMDAM / SDAM：布局、映射与可部署范围](reading-notes.md#close-reading-umdam)
