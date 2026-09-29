# 文献阅读指南：内存地址映射与 Agent 搜索

更新与链接核验日期：**2026-09-29**。

这份指南统一收录目前与本项目直接相关、值得继续阅读的 **19 篇工作**，另列 **1 篇版本状态需要注意的工作**。每篇说明解决的问题、对本项目的意义和阅读重点；全文、题录、预印本与正式发表分别标注。它是立项阅读清单，不是穷尽性综述。

**先建立三个判断：映射会不会影响性能；合理默认方案之上还有没有收益；Agent 相比已有优化方法增加了什么。** 阅读后如何选择硬件或软件路线，见 [研究定位](RESEARCH_POSITIONING.md)；工程状态见 [项目 README](../README.md)。

## 先读哪几篇

第一轮按下面的顺序读即可，不必一次读完全部文献。Flatfish 目前只有可用题录入口，可先了解问题定位，取得全文后再精读方法。

| 顺序 | 文献 | 优先回答的问题 |
| --- | --- | --- |
| 1 | [R01 · Shuhai](#r01) | 真实 HBM 对映射有多敏感？默认方案是否已经很好？ |
| 2 | [R02 · SDAM](#r02) | 为不同数据选择不同映射，需要哪些软硬件支持？ |
| 3 | [R03 · Flatfish](#r03) | 使用学习方法自动生成映射，别人已经做到哪一步？ |
| 4 | [R04 · XOR 映射数学模型](#r04) | 这个搜索空间能否用数学结构缩小，已有强基线是什么？ |
| 5 | [R05 · AgentDSE](#r05) | “Agent 提出假设—调用仿真—读取反馈”已有多少先例？ |
| 6 | [R06 · ArchEval](#r06) | 怎样区分 Agent 会操作工具、会优化和会可靠判断性能？ |

第二轮按研究兴趣补充：

| 方向 | 阅读入口 |
| --- | --- |
| 地址映射机制与传统方法 | [R07 · DReAM](#r07)、[R08 · GPU 地址位熵与 PAE](#r08) |
| 搜索器与 Agent 的公平比较 | [R09 · ArchGym](#r09)、[R10 · gem5 Co-Pilot](#r10)、[R11 · ChatNeuroSim](#r11) |
| 现有 GPU 上的 KV 软件管理 | [R12 · PagedAttention](#r12)、[R13 · vAttention](#r13) |
| 自定义 NPU/PIM 中的布局与映射 | [R14 · PFM](#r14)、[R15 · PATTON](#r15)、[R20 · UMDAM 状态说明](#r20) |
| 通用的模型辅助搜索方法 | [R16 · AlphaEvolve](#r16)、[R17 · LLAMBO](#r17) |
| 接入真实仿真后端 | [R18 · Ramulator 2.0](#r18)、[R19 · DRAMSys4.0](#r19) |
| 目标硬件实际开放什么能力 | [厂商资料](#vendor-docs) |

## 一、优先阅读：判断课题是否值得继续

<a id="r01"></a>
### R01 · Shuhai：真实 HBM 的映射敏感性

**Shuhai: Benchmarking High Bandwidth Memory on FPGAs** · FCCM 2020。\
入口：[作者提供的全文 PDF](https://wangzeke.github.io/doc/shuhai_fccm_20.pdf)。

- **解决什么：** 在 Alveo U280 FPGA 的 HBM 上测量访问行为，比较步长、突发长度和地址映射对带宽的影响。
- **与本项目的关系：** 提供真实 HBM 证据。尤其要注意：在论文比较的映射选项与 stride/burst 测试中，默认 RGBCG 表现最好；坏映射造成巨大损失，不等于还存在同等幅度的优化空间。
- **重点读：** §V.C 的 Address Mapping。记录默认配置、测试范围与限制，分别回答“映射敏感性有多大”和“超过默认方案的余量有多大”。

<a id="r02"></a>
### R02 · SDAM：按数据结构选择地址映射

**Software-Defined Address Mapping: A Case on 3D Memory** · ASPLOS 2022。\
入口：[作者提供的全文 PDF](https://pages.cs.wisc.edu/~swift/papers/asplos22-sdam.pdf)。

- **解决什么：** 将程序的数据结构及访问需求与地址映射选择连接起来，通过分配器、操作系统和硬件共同支持不同映射。
- **与本项目的关系：** 说明“不同数据适合不同映射”已有系统实现先例。论文包含 FPGA HBM 原型及 Linux/glibc 支持；这些能力依赖平台设计，不能直接假设普通 GPU 已开放。
- **重点读：** 映射选择接口、数据分配路径和原型实现。明确粒度、地址解释与软硬件改动，再考虑扩展本项目 DSL。

<a id="r03"></a>
### R03 · Flatfish：学习生成地址映射的直接相关工作

**Flatfish: A Reinforcement Learning Approach for Application-Aware Address Mapping** · IEEE TCAD 41(11):4758–4770，2022。\
入口：[DBLP 题录，可访问但不含全文](https://dblp.org/rec/journals/tcad/LiYGSZWN22)。\
DOI：`10.1109/TCAD.2022.3146204`。

- **解决什么：** 北大机构库被索引的摘要介绍了以强化学习生成应用相关地址映射、使用可逆二进制矩阵的方案。
- **与本项目的关系：** 它比通用 Agent 搜索论文更直接接近“AI + 可组合映射”。需要对照其问题定义和方法，不能将学习生成映射本身作为新贡献。
- **重点读：** 取得全文后核查状态、动作、奖励、映射约束、训练成本、在线/离线模式与泛化实验。**本轮没有取得全文，这些细节仍待核实。**

链接处理：原机构库入口和 DOI 跳转在本次浏览工具中未能打开，已换成可访问题录。上述技术概述仅依据机构摘要的搜索索引，不依据 DBLP 推断技术细节，也不引用摘要中的性能数字做横向比较。

<a id="r04"></a>
### R04 · XOR 映射数学模型：搜索空间并非只能当黑盒

**A Mathematical Model for XOR-Based Application Specific DRAM Address Mapping Schemes** · MEMSYS 2025。\
入口：[Fraunhofer 机构库全文 PDF](https://publica-rest.fraunhofer.de/server/api/core/bitstreams/04740464-f7d5-4d21-9925-5a0a31a35cb4/content)。

- **解决什么：** 在二元域 GF(2) 上研究 XOR 地址映射，将行失配优化转化为子空间选择问题，讨论硬件复杂度、整数规划和启发式方法。
- **与本项目的关系：** 为 DSL 约束、等价候选消除和强优化基线提供依据。大量候选未必都需要先跑慢仿真才能筛选。
- **重点读：** 优化变量、目标函数、困难性与求解规模。行失配是其核心目标之一，不能直接等同于请求完成时间或推理吞吐。

<a id="r05"></a>
### R05 · AgentDSE：与当前 Agent 流程高度接近

**AgentDSE: Reasoning-Augmented Architectural Design Space Exploration** · MLArchSys Workshop @ ISCA 2026；同年 arXiv 预印本。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2606.21836) · [全文 HTML](https://arxiv.org/html/2606.21836v1)。

- **解决什么：** Agent 在描述设计空间的文件工作区中提出候选、调用评价器、读取历史反馈并修正假设，实验涉及 Timeloop、MAESTRO 和 ChampSim。
- **与本项目的关系：** 文件接口、预算、历史记录和假设反馈循环均与当前方案重叠。需要在具体内存问题或搜索机制上提出增量；“接一个 HBM 后端”本身尚不足以说明方法创新。
- **重点读：** 工作区协议、基线、预算和语义匿名化消融。比较它提供给 Agent 的信息与本项目计划增加的诊断证据。

<a id="r06"></a>
### R06 · ArchEval：怎样评价“Agent 会做体系结构研究”

**ArchEval: Measuring AI Agents as Computer Architects** · Architecture 2.0 Workshop @ ISCA 2026；同年 arXiv 预印本。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2607.03601) · [全文 HTML](https://arxiv.org/html/2607.03601v1)。

- **解决什么：** 用 20 个挑战、8 种仿真器及不同工具支持等级，评价 Agent 的体系结构设计与推理能力。
- **与本项目的关系：** 候选能运行、性能确实改善、模型能可靠预测改善，是三件不同的事。其内存任务包含 DRAMSys 控制器调优与 Ramulator RowHammer 任务，但并非本项目的 HBM/KV 映射基准。
- **重点读：** 任务表、工具支持等级、评价方法与失败案例。将正确性、搜索成绩、预测准确性和成本分开记录。

R05 的 Workshop 状态依据 [作者主页](https://iamchenyuwang.github.io/)；R06 另可核对 [Workshop 官方日程](https://harvard-edge.github.io/isca-26-arch-2-workshop/)。Workshop、预印本与主会论文在本指南中分别标注。

## 二、地址映射的机制与已有优化方法

<a id="r07"></a>
### R07 · DReAM：动态映射及其切换代价

**DReAM: Dynamic Re-arrangement of Address Mapping to Improve the Performance of DRAMs** · 2015 年 arXiv 预印本。\
入口：[题录与摘要](https://arxiv.org/abs/1509.03721) · [全文 PDF](https://arxiv.org/pdf/1509.03721)。

- **解决什么：** 根据应用的访问行为调整 DRAM 地址映射，研究固定映射难以适应不同访问模式的问题。
- **与本项目的关系：** 动态适应负载已有先例；当已有数据仍在内存中时，改变地址解释会带来迁移和一致性问题。
- **重点读：** 访问特征、调整触发条件与数据迁移机制。区分部署前离线选策略和运行中切换策略。

<a id="r08"></a>
### R08 · PAE：用地址位熵理解 GPU 映射

**Get Out of the Valley: Power-Efficient Address Mapping for GPUs** · ISCA 2018。\
入口：[作者提供的全文 PDF](https://users.elis.ugent.be/~leeckhou/papers/isca18.pdf)。

- **解决什么：** 分析窗口内地址位熵与 GPU 存储并行度之间的关系，提出基于二进制可逆矩阵的地址映射方案 PAE。
- **与本项目的关系：** 单个位活动率只能提供部分信息；应比较具有领域知识的启发式，并考虑并行度、局部性与功耗的取舍。当前玩具的位活动启发式不构成该论文的复现。
- **重点读：** “entropy valley”的形成、矩阵映射机制和评价条件，检查哪些统计可以作为低成本诊断信号。

## 三、体系结构搜索与 Agent：建立有竞争力的对照

<a id="r09"></a>
### R09 · ArchGym：统一接口下比较搜索方法

**ArchGym: An Open-Source Gymnasium for Machine Learning Assisted Architecture Design** · ISCA 2023。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2306.08888) · [作者提供的全文 PDF](https://devashreetrip.github.io/publication/Website_ArchGym_ISCA2023.pdf)。

- **解决什么：** 将体系结构仿真器与不同搜索算法连接起来，比较算法、调参和采样对设计空间探索的影响，包含内存控制器任务。
- **与本项目的关系：** 是设计实验框架和强基线的重要参考。算法能否胜出可能取决于其自身参数和预算，不能只与默认设置的随机或局部搜索比较。
- **重点读：** 统一接口、搜索算法设置、调参成本和实验预算。发表信息可核对 [ISCA 2023 官方程序](https://www.iscaconf.org/isca2023/program/)。

<a id="r10"></a>
### R10 · gem5 Co-Pilot：语言模型与已有设计空间数据

**gem5 Co-Pilot: AI Assistant Agent for Architectural Design Space Exploration** · CAMS Workshop 2025。\
入口：[arXiv 题录与摘要](https://arxiv.org/abs/2510.19577) · [作者提供的全文 PDF](https://arg.csl.cornell.edu/papers/gem5-co-pilot-cams-25.pdf)。

- **解决什么：** 将自然语言交互、设计空间数据库 DSDB 和体系结构探索连接起来，论文包含 L2 cache 配置实验。
- **与本项目的关系：** 说明“Agent + 体系结构工具”已有相邻实现；也提醒我们区分查询预先计算的结果与新执行昂贵仿真。
- **重点读：** 数据库来源、Agent 的实际决策范围和计算成本边界。它不是 HBM bank 映射方案。

<a id="r11"></a>
### R11 · ChatNeuroSim：约束检查与仿真流程自动化

**ChatNeuroSim: An LLM Agent Framework for Automated Compute-in-Memory Accelerator Deployment and Optimization** · 2026 年 arXiv 预印本，未在本轮确认正式发表。\
入口：[题录与摘要](https://arxiv.org/abs/2603.08745) · [全文 PDF](https://arxiv.org/pdf/2603.08745)。

- **解决什么：** 使用 LLM Agent 组织存内计算加速器的配置、约束检查和 NeuroSim 仿真，并结合设计空间裁剪。
- **与本项目的关系：** 工具代理、配置检查和自动运行均已有相关工作；这些是必要工程能力，其本身与发现更优策略要分别评价。
- **重点读：** Agent 和确定性程序分别承担什么，裁剪规则从何而来，省下的是人工操作还是后端评价。

## 四、大模型 KV：区分现有 GPU 软件与定制硬件

<a id="r12"></a>
### R12 · PagedAttention：KV 块管理与服务系统

**Efficient Memory Management for Large Language Model Serving with PagedAttention** · SOSP 2023。\
入口：[题录与摘要](https://arxiv.org/abs/2309.06180) · [全文 PDF](https://arxiv.org/pdf/2309.06180)。

- **解决什么：** 通过 KV 分块管理减少内存浪费，支持共享并改善 LLM 服务系统的内存使用和吞吐。
- **与本项目的关系：** 若选择现有 GPU 软件路线，这是理解分配与访问组织的基础。它优化的 KV 块管理层级与控制器内部 channel/bank 位映射不同。
- **重点读：** 逻辑块到物理块的关系、attention kernel 如何访问块，以及收益来自容量、并发还是单请求执行。

<a id="r13"></a>
### R13 · vAttention：虚拟连续与物理动态分配

**vAttention: Dynamic Memory Management for Serving LLMs without PagedAttention** · ASPLOS 2025；预印本首次发布于 2024 年。\
入口：[题录与摘要](https://arxiv.org/abs/2405.04437) · [全文 PDF](https://arxiv.org/pdf/2405.04437)。

- **解决什么：** 借助 CUDA 虚拟内存管理接口，使 KV 的虚拟地址保持连续，同时动态管理物理分配。
- **与本项目的关系：** 展示现有 GPU 软件可利用的具体自由度。虚拟地址连续不等于介质位置连续，也不意味着软件能任意改写 HBM 控制器映射。
- **重点读：** 分配粒度、预分配/异步管理、kernel 兼容性和完整运行开销。与 PagedAttention 成对阅读。

<a id="r14"></a>
### R14 · PFM：NPU/PIM 的两种访问视图

**Rethinking Unified Memory for NPU-PIM Systems: Dual-View Memory for Dynamic Inference of LLM** · 2026；arXiv 作者说明标注“MICRO 2026 接收”，本轮未独立核对正式出版页。\
入口：[题录、摘要与接收说明](https://arxiv.org/abs/2608.06989) · [全文 PDF](https://arxiv.org/pdf/2608.06989)。

- **解决什么：** 面向 NPU/PIM 动态推理，使不同执行单元以不同逻辑视图访问共享数据，协调布局、地址转换与执行。
- **与本项目的关系：** 与“同一份数据服务多种访问模式”的问题接近，同时也构成已有方案。它依赖特定异构架构，不能直接外推到普通 GPU。
- **重点读：** 逻辑视图与物理布局如何对应，硬件支持和动态切换成本是什么；再判断普通 HBM 路线可借用哪些机制。

<a id="r15"></a>
### R15 · PATTON：KV 生命周期与 PIM 数据组织

**PATTON: Enabling Commodity PIM for Production LLM Serving** · 2026-09-10 发布的 arXiv 预印本，未在本轮确认正式发表。\
入口：[题录与摘要](https://arxiv.org/abs/2609.11392) · [全文 HTML](https://arxiv.org/html/2609.11392v1)。

- **解决什么：** 协调 PIM 服务系统中的 KV 生命周期、布局和分配，处理 Value cache 在计算访问、逐 token 写入和容量利用之间的取舍。
- **与本项目的关系：** 有助于把“KV 很复杂”转化为具体且相互冲突的访问需求。对象仍是 PIM，不能据此认定一般 GPU HBM 映射可调或必有额外收益。
- **重点读：** 数据组织粒度、写入暂存与提交、容量代价，以及与 serving 流程的配合。

## 五、通用搜索启发：保留，但不替代直接相关工作

<a id="r16"></a>
### R16 · AlphaEvolve：程序生成、进化搜索与自动评价

**AlphaEvolve: A coding agent for scientific and algorithmic discovery** · Google DeepMind，2025 年技术报告/预印本。\
入口：[题录与摘要](https://arxiv.org/abs/2506.13131) · [全文 PDF](https://arxiv.org/pdf/2506.13131) · [官方介绍](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)。

- **解决什么：** 结合语言模型生成程序、候选库、自动评价与进化搜索，持续改进可执行候选；官方介绍包含经验证的电路代码优化案例。
- **与本项目的关系：** 可借鉴候选库和结构化程序修改，用受限 DSL 表达策略，并使优化与正确性检查配套。这项工作来自 Google DeepMind。
- **重点读：** 评价器决定什么、候选如何被保留和生成、约束如何执行。其应用成功不直接证明小型 XOR DSL 上的搜索优势。

<a id="r17"></a>
### R17 · LLAMBO：让语言模型增强优化器的部分环节

**Large Language Models to Enhance Bayesian Optimization** · ICLR 2024。\
入口：[会议正式题录](https://proceedings.iclr.cc/paper_files/paper/2024/hash/84b8d9fcb4e262fcd429544697e1e720-Abstract-Conference.html) · [会议全文 PDF](https://proceedings.iclr.cc/paper_files/paper/2024/file/84b8d9fcb4e262fcd429544697e1e720-Paper-Conference.pdf) · [作者代码](https://github.com/tennisonliu/LLAMBO)。

- **解决什么：** 在贝叶斯优化中使用语言模型进行起始候选选择、代理建模和候选采样，实验主要围绕超参数优化。
- **与本项目的关系：** 可以先让 Agent 改善某个搜索环节，再测其增益；不必从一开始就让它独立决定所有实验。
- **重点读：** 各模块的消融和先验信息。变长 DSL 仍需合适的编码与合法候选生成；代理预测也不能当作后端实测结果。

## 六、后端工具：选定路线后再深入

<a id="r18"></a>
### R18 · Ramulator 2.0：模块化 DRAM 时序仿真

**Ramulator 2.0: A Modern, Modular, and Extensible DRAM Simulator** · IEEE Computer Architecture Letters 23(1):112–116，2024；预印本与在线发表在 2023 年。\
入口：[arXiv 题录](https://arxiv.org/abs/2308.11030) · [预印本全文 PDF](https://arxiv.org/pdf/2308.11030) · [官方代码](https://github.com/CMU-SAFARI/ramulator2)。

- **解决什么：** 提供可扩展的 DRAM 仿真框架，以模块接口组织内存设备、控制器等部件。
- **与本项目的关系：** 是后续评价器候选。需要选择并固定设备模型、控制器、地址映射和请求输入配置；当前 Python 玩具尚未接入它。
- **重点读：** 模块边界和扩展接口。DRAM 时序仿真输入的请求流如何生成，会直接限制其对端到端推理性能的解释能力。

版本提醒：论文介绍 2.0，本轮访问的官方仓库主分支文档已经介绍 2.1；接入时应固定提交版本，避免把不同版本的模型、接口和依赖混在一起。

<a id="r19"></a>
### R19 · DRAMSys4.0：系统分析与已有映射描述格式

**DRAMSys4.0: An Open-Source Simulation Framework for In-depth DRAM Analyses** · International Journal of Parallel Programming 50:217–242，2022。\
入口：[出版社开放全文](https://link.springer.com/article/10.1007/s10766-022-00727-4) · [官方配置文档](https://github.com/tukl-msd/DRAMSys/blob/master/configs/README.md)。

- **解决什么：** 为 DRAM 系统建模和分析提供框架；官方配置文档已有按字段和 XOR 位组合描述映射的方式。
- **与本项目的关系：** 可作为另一后端及 DSL 设计参考。“用 JSON 写映射”已有基础，当前 DSL 的价值主要在于统一语义、合法性和后端一致性。
- **重点读：** 模型精度、分析能力及配置文档中的地址映射部分。比较它与 Ramulator 的适配成本后再选择。

<a id="vendor-docs"></a>
## 七、厂商资料：确认搜索空间是否真实可用

这些是产品文档，不是论文。三个页面针对不同 IP，结论不能混用；下列版本是本轮页面显示值。

| 官方入口 | 本轮版本 | 值得了解的内容 |
| --- | --- | --- |
| [AMD PG313 · Address Mapping](https://docs.amd.com/r/en-US/pg313-network-on-chip/Address-Mapping) | 1.1 | 区分线性/随机块与纯随机访问，讨论 bank-group 交织的取舍。 |
| [AMD PG150 · Address Map](https://docs.amd.com/r/en-US/pg150-ultrascale-memory-ip/Address-Map) | 1.4 | 其 DDR4 控制器中，随机流对若干映射选项的效率差别较小；顺序小步长有特定建议。 |
| [AMD PG276 · HBM Reordering Options](https://docs.amd.com/r/en-US/pg276-axi-hbm/HBM-Reordering-Options) | 1.0 | 自定义地址映射会关闭许多重排选项，映射与调度未必能自由组合。 |

选定目标平台时，逐项核实：允许改哪些位、配置粒度、是否影响调度、何时能切换，以及数据写入与读取如何保持一致。

<a id="r20"></a>
## 八、需要单独处理的文献：UMDAM

**UMDAM: A Unified Data Layout and DRAM Address Mapping for Heterogeneous NPU-PIM**。\
可访问入口：[arXiv 版本及撤回说明](https://arxiv.org/abs/2511.03293)。\
另检索到 ISCAS 2026 题录，页码 224–228；DOI：`10.1109/ISCAS66217.2026.11562976`。

这里需要纠正旧文档只强调“撤回”的不完整表述：

- **已核实：** arXiv 版本于 2025-11-07 撤回，页面说明涉及提交者无权同意发布许可。此状态针对该 arXiv 版本，不能据此推断研究结论失效或正式会议版本被撤回。
- **检索到但访问核验未完成：** DBLP 搜索索引中存在 ISCAS 2026 会议题录。本轮直接打开题录/DOI 未成功，IEEE 页面出现访问验证，尚未取得正式全文。
- **如何处理：** 保留为高度相关的待核实工作，正式 DOI 以文本提供检索；暂不沿用旧摘要中的性能数字、实验细节或结论。

它涉及布局与映射的联合设计，对 NPU/PIM 路线可能很重要，取得正式全文后应补读。R20 不计入前面的 19 篇常规阅读条目。

## 九、基础概念资料

这些资料用于补齐概念和工具使用，主研究文献仍按上面的顺序阅读。

| 想弄清楚什么 | 入口 |
| --- | --- |
| DSL 是什么，为什么不等于底层语言 | [Martin Fowler · DSL Q & A](https://martinfowler.com/bliki/DslQandA.html) |
| RTL、数字电路综合是什么 | [Yosys · Primer](https://yosyshq.readthedocs.io/projects/yosys/en/v0.65/appendix/primer.html) |
| 如何编译和运行小型 RTL 仿真 | [Icarus Verilog · Getting Started](https://steveicarus.github.io/iverilog/usage/getting_started.html)、[Simulation](https://steveicarus.github.io/iverilog/usage/simulation.html) |

## 十、链接核验与阅读记录

**正文保留的可点击外链均已通过本轮浏览工具打开，并检查返回内容与条目相符。** “全文 PDF/HTML”表示能够取得正文；“题录与摘要”不表示取得全文。代码链接只检查了仓库页面，没有逐篇运行或复现论文。

核验覆盖 README 与 docs 中保留的外部入口。失败的原链接已替换或移出推荐入口，访问限制见下表。工具无法访问或遇到站点验证不代表网站永久失效；核验日期之后的可用性仍可能变化。

主要例外及替换：

| 条目 | 核验结果与处理 |
| --- | --- |
| Flatfish | 可访问题录已提供；原机构库/DOI 入口本环境未打开，全文未取得。 |
| UMDAM | arXiv 状态页可访问；正式出版入口未完成访问核验，单列说明。 |
| ArchGym | arXiv PDF 入口本轮未打开，改用已打开并匹配标题的作者 PDF。 |
| PATTON | arXiv PDF 入口本轮未打开，改用已打开的 arXiv HTML 全文。 |

精读时，建议每篇只先填下面一行。它能帮助我们比较方法，避免把不同平台和不同基线的加速比放在一起排名。

| 文献 | 平台/负载 | 可控制的变量 | 约束与实现代价 | 目标与最强基线 | 搜索/训练成本 | 未解决的问题 |
| --- | --- | --- | --- | --- | --- | --- |
| Rxx | 例如 FPGA HBM / 真实请求流 | 例如 bank 选位 | 例如 XOR 深度与迁移 | 例如完成时间 / 默认控制器映射 | 仿真次数、总时间、离线训练 | 留待本项目验证的具体假设 |

这份清单支持进一步缩小研究问题；它尚不能证明某个具体方向没有先例。下一步的立项标准与两条平台路线的取舍，集中放在 [研究定位](RESEARCH_POSITIONING.md)。
