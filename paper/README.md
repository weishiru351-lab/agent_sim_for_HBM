# 论文库

收录与核验日期：**2026-10-08**。覆盖[文献阅读指南](../docs/literature.md)的 R01–R20：维护者本地已收齐 **20 篇 PDF**，其中 R03 Flatfish 与 R20 UMDAM 由用户补充正式出版版。**Git 在本目录中只跟踪 `README.md` 和 `references.bib`；PDF 留在本地，克隆仓库后按需获取。** 指南中的厂商手册、工具文档和概念网页不属于这 20 篇论文，仍从指南访问。

这里管理全文、收录来源和引用信息；每篇为什么值得读、先读哪些章节，统一见阅读指南。

## 文件与使用方法

- `README.md`：本地全文索引、版本与来源说明。
- [references.bib](references.bib)：20 条 BibTeX 题录，`file` 字段指定预期的本地 PDF 文件名，引用键与下面的编号对应；可用于 LaTeX 或导入文献管理软件，PDF 尚未下载时仍可使用题录。
- `R编号_简称_版本.pdf`：下载或用户补充的原始全文，每篇保存一个版本；由根目录 `.gitignore` 排除，不提交到 Git。

本次收录的所有 PDF 均检查了文件标识、可解析页数、首页题名和作者。页数指维护者本地文件页数，可能包含附录。取得全文不代表已完成方法精读或实验复现；实际阅读范围、问题台账和证据页码统一见[持续研究笔记](../docs/reading-notes.md)。下表的本地 PDF 链接需在对应文件下载后使用，在 GitHub 上不会打开全文。

## 在新环境恢复论文库

1. 克隆仓库后，`paper/` 中只有本说明和 `references.bib`。题录、引用键与阅读指南已可使用，无需先下载全部论文。
2. 按下面的“收录来源”打开所需论文。18 篇提供本次成功下载的公开 PDF 链接；arXiv 链接固定了版本号，恢复时使用表中版本。
3. R03、R20 从 IEEE Xplore 按下面来源说明中的 DOI 或文献号获取正式全文；可能需要单位订阅或作者提供，无法保证匿名下载。
4. 将 PDF 保存到本目录，文件名严格使用 `references.bib` 中该条目的 `file` 值；下表本地链接也对应这些名称。例如 Flatfish 保存为 `R03_Flatfish_TCAD2022.pdf`。
5. 打开 PDF，核对题名、作者、版本、DOI（如有）和页数。全部获取后，应有 **20 个 PDF、1 个 README 和 1 个 BibTeX 文件**；在仓库根目录运行 `git status --short --untracked-files=all -- paper`，PDF 不应出现在待提交列表中。

这些记录足以按约定重建目录结构和文献版本，但不保证每个来源始终可用。出版社或作者可能替换文件，IEEE 下载件也可能带有不同时间的水印，因此重新下载不保证与维护者当前文件逐字节相同。链接失效时可用题名/DOI 重新获取；若改用不同版本，应同步更新本说明和 BibTeX 的 `file`、`annote` 字段。

## 全文索引

| 编号 / 引用键 | 论文 | 本地全文 | 本地版本与页数 | 收录来源 |
| --- | --- | --- | --- | --- |
| `R01_Shuhai` | Shuhai: Benchmarking High Bandwidth Memory on FPGAs | [PDF](R01_Shuhai_FCCM2020.pdf) | 作者提供的 FCCM 2020 稿，9 页 | [作者主页 PDF](https://wangzeke.github.io/doc/shuhai_fccm_20.pdf) |
| `R02_SDAM` | Software-Defined Address Mapping: A Case on 3D Memory | [PDF](R02_SDAM_ASPLOS2022.pdf) | 作者提供的 ASPLOS 2022 稿，14 页 | [作者所在大学 PDF](https://pages.cs.wisc.edu/~swift/papers/asplos22-sdam.pdf) |
| `R03_Flatfish` | Flatfish: A Reinforcement Learning Approach for Application-Aware Address Mapping | [PDF](R03_Flatfish_TCAD2022.pdf) | TCAD 2022 正式出版版，13 页 | 用户补充的 IEEE Xplore PDF；[来源说明](#source-r03) |
| `R04_XORMapping` | A Mathematical Model for XOR-Based Application Specific DRAM Address Mapping Schemes | [PDF](R04_XOR_Address_Mapping_MEMSYS2025.pdf) | MEMSYS 2025 官网提供稿，15 页 | [会议官网 PDF](https://www.memsys.io/wp-content/uploads/ninja-forms/5/a_mathematical_model_for_xor_based_application_specific_dram_address_mapping_schemes.pdf) |
| `R05_AgentDSE` | AgentDSE: Reasoning-Augmented Architectural Design Space Exploration | [PDF](R05_AgentDSE_arXiv2606.21836v1.pdf) | arXiv:2606.21836v1，8 页 | [arXiv PDF](https://arxiv.org/pdf/2606.21836v1) |
| `R06_ArchEval` | ArchEval: Measuring AI Agents as Computer Architects | [PDF](R06_ArchEval_arXiv2607.03601v1.pdf) | arXiv:2607.03601v1，26 页 | [arXiv PDF](https://arxiv.org/pdf/2607.03601v1) |
| `R07_DReAM` | DReAM: Dynamic Re-arrangement of Address Mapping to Improve the Performance of DRAMs | [PDF](R07_DReAM_arXiv1509.03721v1.pdf) | arXiv:1509.03721v1，12 页 | [arXiv PDF](https://arxiv.org/pdf/1509.03721v1) |
| `R08_PAE` | Get Out of the Valley: Power-Efficient Address Mapping for GPUs | [PDF](R08_PAE_ISCA2018.pdf) | 作者提供的 ISCA 2018 稿，14 页 | [作者所在大学 PDF](https://users.elis.ugent.be/~leeckhou/papers/isca18.pdf) |
| `R09_ArchGym` | ArchGym: An Open-Source Gymnasium for Machine Learning Assisted Architecture Design | [PDF](R09_ArchGym_ISCA2023.pdf) | 作者提供的 ISCA 2023 稿，16 页 | [作者主页 PDF](https://devashreetrip.github.io/publication/Website_ArchGym_ISCA2023.pdf) |
| `R10_gem5CoPilot` | gem5 Co-Pilot: AI Assistant Agent for Architectural Design Space Exploration | [PDF](R10_gem5_CoPilot_arXiv2510.19577v1.pdf) | arXiv:2510.19577v1，7 页 | [arXiv PDF](https://arxiv.org/pdf/2510.19577v1) |
| `R11_ChatNeuroSim` | ChatNeuroSim: An LLM Agent Framework for Automated Compute-in-Memory Accelerator Deployment and Optimization | [PDF](R11_ChatNeuroSim_arXiv2603.08745v1.pdf) | arXiv:2603.08745v1，30 页 | [arXiv PDF](https://arxiv.org/pdf/2603.08745v1) |
| `R12_PagedAttention` | Efficient Memory Management for Large Language Model Serving with PagedAttention | [PDF](R12_PagedAttention_arXiv2309.06180v1.pdf) | arXiv:2309.06180v1，16 页 | [arXiv PDF](https://arxiv.org/pdf/2309.06180v1) |
| `R13_vAttention` | vAttention: Dynamic Memory Management for Serving LLMs without PagedAttention | [PDF](R13_vAttention_arXiv2405.04437v1.pdf) | arXiv:2405.04437v1，15 页 | [arXiv PDF](https://arxiv.org/pdf/2405.04437v1) |
| `R14_PFM` | Rethinking Unified Memory for NPU-PIM Systems: Dual-View Memory for Dynamic Inference of LLM | [PDF](R14_PFM_arXiv2608.06989v1.pdf) | arXiv:2608.06989v1，15 页 | [arXiv PDF](https://arxiv.org/pdf/2608.06989v1) |
| `R15_PATTON` | PATTON: Enabling Commodity PIM for Production LLM Serving | [PDF](R15_PATTON_arXiv2609.11392v1.pdf) | arXiv:2609.11392v1，14 页 | [arXiv PDF](https://arxiv.org/pdf/2609.11392v1) |
| `R16_AlphaEvolve` | AlphaEvolve: A coding agent for scientific and algorithmic discovery | [PDF](R16_AlphaEvolve_arXiv2506.13131v1.pdf) | arXiv:2506.13131v1，44 页 | [arXiv PDF](https://arxiv.org/pdf/2506.13131v1) |
| `R17_LLAMBO` | Large Language Models to Enhance Bayesian Optimization | [PDF](R17_LLAMBO_ICLR2024.pdf) | ICLR 2024 会议全文，33 页 | [会议论文集 PDF](https://proceedings.iclr.cc/paper_files/paper/2024/file/84b8d9fcb4e262fcd429544697e1e720-Paper-Conference.pdf) |
| `R18_Ramulator2` | Ramulator 2.0: A Modern, Modular, and Extensible DRAM Simulator | [PDF](R18_Ramulator2_arXiv2308.11030v1.pdf) | arXiv:2308.11030v1，4 页 | [arXiv PDF](https://arxiv.org/pdf/2308.11030v1) |
| `R19_DRAMSys4` | DRAMSys4.0: An Open-Source Simulation Framework for In-depth DRAM Analyses | [PDF](R19_DRAMSys4_IJPP2022.pdf) | IJPP 2022 出版版，26 页 | [德国国家图书馆 PDF](https://d-nb.info/1262215250/34) |
| `R20_UMDAM` | UMDAM: A Unified Data Layout and DRAM Address Mapping for Heterogeneous NPU-PIM | [PDF](R20_UMDAM_ISCAS2026.pdf) | ISCAS 2026 正式出版版，5 页 | 用户补充的 IEEE Xplore PDF；[来源说明](#source-r20) |

## 用户补充的正式出版版

<a id="source-r03"></a>
### R03 · Flatfish

Xingchen Li、Zhihang Yuan 等，IEEE TCAD **41(11):4758–4770，2022**。DOI：`10.1109/TCAD.2022.3146204`；IEEE 文献号：`9691462`。

重新获取：[IEEE 出版入口（DOI）](https://doi.org/10.1109/TCAD.2022.3146204)。此入口用于检索正式版本，不是保证匿名可用的 PDF 直链。

自动下载时 IEEE 入口受限；用户于 2026-10-08 补充了 IEEE Xplore 正式出版版，现已归档为 [R03_Flatfish_TCAD2022.pdf](R03_Flatfish_TCAD2022.pdf)。已核对题名、作者、DOI、卷期与页码 4758–4770；共 13 页，全部可解析。移动前后 SHA-256 一致，原始 PDF 内容未改动。

<a id="source-r20"></a>
### R20 · UMDAM

Hai Huang、Xuhong Qiang、Weisheng Zhao、Chenchen Liu，**ISCAS 2026:224–228**。DOI：`10.1109/ISCAS66217.2026.11562976`；IEEE 文献号：`11562976`。

重新获取：[IEEE 出版入口（DOI）](https://doi.org/10.1109/ISCAS66217.2026.11562976)。取得正式会议版后按表中名称保存。

用户于 2026-10-08 补充了 IEEE Xplore 正式会议版，现已归档为 [R20_UMDAM_ISCAS2026.pdf](R20_UMDAM_ISCAS2026.pdf)。已核对题名、作者、DOI、会议和页码 224–228；共 5 页，全部可解析，与此前核实的 [Crossref 出版社登记题录](https://api.crossref.org/works/10.1109/ISCAS66217.2026.11562976)一致。移动前后 SHA-256 一致，原始 PDF 内容未改动。

[arXiv:2511.03293 状态页](https://arxiv.org/abs/2511.03293)显示预印本于 2025-11-07 撤回，理由涉及提交者无权同意发布许可。这一状态针对预印本，本地收录的是 ISCAS 2026 正式会议版。后续阅读与引用以该正式版本为准。

## 引用与版本管理

题录按 PDF 首页、出版社登记的 Crossref 记录及会议论文集核对。已确认正式发表的工作优先使用正式题录；只有预印本或 Workshop 信息的条目保留预印本引用，不把 Workshop 写成主会论文。`references.bib` 使用 UTF-8，`file` 路径相对于本目录，`annote` 保存本地版本与来源说明。

本地 PDF 与正式出版版可能不同：例如 R04 的会议官网稿为 15 页，而正式题录为 105–120 页；R13 本地是 2024 年的 arXiv v1，正式论文发表于 ASPLOS 2025；R18 本地是 2023 年的 4 页预印本，正式卷期为 2024 年、112–116 页。引用具体图表、数字和页码前应核对所用版本。arXiv 文件名保留版本号，本目录不声称它们都是最新修订版。

在项目根目录下撰写 LaTeX 时，可使用 `\bibliography{paper/references}`，并按引用键引用，例如 `\cite{R03_Flatfish,R05_AgentDSE}`。导入文献管理软件后，可从本目录关联 PDF；附件自动关联方式取决于软件对相对路径的支持。

后续更新论文版本时，沿用 R 编号，同时修改全文索引、BibTeX 的附件/版本说明以及阅读指南的本地入口，避免为同一工作建立重复题录。
