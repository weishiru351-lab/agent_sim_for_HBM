# Ramulator 最小只读适配器

本目录把现有 JSON 轨迹与 DSL 转成 HBM2 坐标，并提供独立 C++ 驱动。**截至 2026-10-08，源码校验、配置导出和 Python 接口测试已完成；C++ 驱动尚未编译或运行，不能据此报告 Ramulator 性能。** 本机尚无可用的 Linux/WSL 或 C++ 构建工具，运行环境待确定。V2/V3 仍使用原来的 `toy-fcfs-v1`，没有自动切换后端。

当前暂停实际运行及环境安装。该适配器为固定流量的内存评价提供基础，不决定研究主线；接入时钟级后端本身不能证明新颖性或 Agent 优势。接下来先明确[最小系统问题](../../docs/RESEARCH_POSITIONING.md#minimal-system-task)，再决定需要怎样的前端、读写契约和校准。

## 文件职责

| 文件 | 作用 |
| --- | --- |
| `lock.json` | 固定 Ramulator 及 yaml-cpp、fmt 的提交、下载地址和压缩包 SHA-256 |
| `prepare.py` | 下载与逐文件核验源码、导出完整 HBM2 配置、调用 CMake 构建 |
| `CMakeLists.txt` | 将固定版本的 Ramulator 库与本项目驱动链接；关闭原生 Python 扩展 |
| `driver.cpp` | 按固定到达时间注入请求、处理各通道反压、等待所有读回调并输出事件 |
| `README.md` | 输入输出契约、复现方法和当前验证边界 |

Python 入口在 [`hbm_toy/ramulator_backend.py`](../../hbm_toy/ramulator_backend.py)，测试在 [`tests/test_ramulator_backend.py`](../../tests/test_ramulator_backend.py)，小轨迹在 [`examples/ramulator_read.json`](../../examples/ramulator_read.json)。第三方源码、构建目录和临时结果均被 Git 忽略。

## 固定版本与输入契约

使用 [Ramulator 2.1 固定提交 `72427a1`](https://github.com/CMU-SAFARI/ramulator2/tree/72427a1bba3771564c4fb0e494ba02242fd1eaa7)。它与文献中的 Ramulator 2.0 不同，接口不能直接混用。配置为 HBM2、`HBM2_2Gb`、`HBM2_2000Mbps`、FRFCFS、开放行策略和 AllBank 刷新。

- **请求：** 首版只接受对齐的 64 字节读请求，每条显式拆为两条 32 字节事务。两半均完成才计为一个逻辑请求完成，逻辑完成时间取两半回调时间的最大值。混合读写轨迹会被拒绝，不能直接重跑旧 V3 轨迹，也不会自动把写改成读。
- **限制只读的原因：** 固定版本的写回调可能在写合并或 WR 命令发出时触发，不能直接用作与读相同的数据完成时间。后续支持写入需要另外定义和验证完成语义。依据见上游 [控制器实现](https://github.com/CMU-SAFARI/ramulator2/blob/72427a1bba3771564c4fb0e494ba02242fd1eaa7/src/ramulator/controller/controller_base.cpp)。
- **映射：** Python 执行一次 DSL，得到 channel/bank/row/column；原生 channel mapper 和 address mapper 均为 pass-through，使用这些坐标。每个逻辑 column 对应两个连续的事务 column；后者已去掉预取因子，范围为 0–31。
- **bank 展开：** 原来的扁平 bank 编号低 2 位映射到 Bank、随后 2 位到 BankGroup、再 1 位到 PseudoChannel；Sid 固定为 0。允许最多 8 通道、每通道 32 bank、16 个逻辑 column、16384 行，另受原 DSL 总位宽限制。默认的小配置只使用其中一个子空间，并非完整 HBM 容量或带宽实验。
- **到达时间：** 轨迹中的整数按 DRAM 周期解释；保留原数值并不表示已经与玩具时间或真实 GPU 时间校准。每周期、每通道按到达顺序提交所有已到达事务，直到队列拒绝，随后重试。通道之间不互相阻塞。延迟包括在驱动端等待入队的时间。
- **可比性：** Ramulator 的 FRFCFS、刷新、bank group 和伪通道行为不同于玩具模型。初期只检查同一轨迹、同一 DSL 在两个模型下的策略排名及差异原因，不能直接比较两者的绝对周期数，也不能把差异全归因于时序精度。

准备步骤生成不可覆盖的 `bundle.json`，包含轨迹、DSL、完整配置、编译后事务、来源版本与内容摘要。运行时检查摘要，保存驱动二进制 SHA-256、标准输出、错误输出和原始事件。计数规则为：未能启动进程是 `not_run`、0 次调用；启动后超时或失败是 `backend_error`、1 次调用；全部事务完成且结果校验通过才是 `ok`。这些调用计数尚未接入 V3 的共享搜索预算。

## 复现步骤

下列构建命令面向具有 Python 3.10+、CMake 及 C++20 编译器的 Linux/WSL 环境；**本项目驱动的构建命令尚待实际验证**。在项目根目录运行，若环境仅提供 `python3`，用它替换 `python`。下载需要能访问 GitHub 的 codeload 域名。

```sh
python backends/ramulator21/prepare.py fetch
python backends/ramulator21/prepare.py verify
python backends/ramulator21/prepare.py build --jobs 2
python -m hbm_toy.ramulator_backend prepare --trace examples/ramulator_read.json --dsl examples/identity.json --output results/run_ramulator_check/identity_bundle.json
python -m hbm_toy.ramulator_backend run --bundle results/run_ramulator_check/identity_bundle.json --driver build/ramulator21/hbm_ramulator --output results/run_ramulator_check/identity_run
```

`fetch` 会把依赖放入 `.local/`，同时核对下载摘要、提交标识及已解压内容；不会安装系统工具。若下载中断留下损坏压缩包，校验会失败，需移走该压缩包后重新下载。已准备的 bundle 和运行目录不可覆盖；重复实验请换输出路径。

无需下载或编译第三方代码即可执行接口单元测试：

```sh
python -m unittest discover -s tests -v
```

本次实测：全部 30 项测试通过，其中新增 6 项覆盖手算坐标、全小地址域无混叠、DSL 只应用一次、乱序回调汇总、非法结果拒绝、缺少驱动和超时的计数。已从固定上游源码导出配置，并把示例的 8 个逻辑请求编译成 16 个事务。缺少二进制时返回 `not_run`、`backend_calls=0`。测试中的回调数据和超时进程为显式测试替身，不是 Ramulator 测量结果。

## 环境就绪后的验收顺序

这是恢复本分支后的条件式验收，不是当前项目立即执行的计划。模型设计还需遵守以下精读结论，出处见[后端契约](../../docs/RESEARCH_NOTES.md#backend-contract)：

- 固定 arrival 加队列反压只决定何时接受既有请求，不会自动产生计算依赖、KV 分配或重计算。优化会改变这些行为时，必须新增相应的逻辑任务前端。
- 命令满足器件时序、控制器符合目标平台、数据内容正确是不同证据；Ramulator 的精细时序不能替代后两项。
- 从玩具切换到 FRFCFS、刷新和更细 bank 结构，改变的不只是精度。复核候选排序与差异机制，并纳入映射实现、转换及缓冲代价后，才能判断低成本模型是否适合指导搜索。

1. 编译并运行小轨迹，逐条核验注入、接受和完成时间，确认 8 个请求、16 个事务全部完成。
2. 加入相同行重复读、同行不同列、跨行冲突、跨通道以及队列拥塞的小轨迹；用手算或独立命令轨迹检查时序及并行关系。
3. 冻结一批明确标注为只读的新任务，对 identity、启发式和候选 DSL 比较排名；异常时先定位映射、注入和调度差异。
4. 通过上述检查后再接统一 Agent 搜索预算，开展独立重复探索，并接入来源明确的真实请求。不能用这条 8 请求示例作为 Agent 优势或真实 KV 性能的证据。
