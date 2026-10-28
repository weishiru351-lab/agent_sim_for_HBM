# MQSim 接口：SSD 主机流与裸 NAND 边界

2026-10-20：已核对固定版本源码、实现请求导出、bundle 校验、可选运行与结果解析，并以测试替身验证。**没有安装、编译或运行 MQSim，没有真实仿真成绩，也未接入统一搜索计分。**

MQSim 原生接收 SSD 主机请求，再由它自己的控制器、FTL 和 NAND 模型处理。它不会直接解释本项目的 `nand_page_mapping / nand_gc`。原生 GC/plane 规则与当前 DSL 不等价，不能因为名字相近就自动转换。

| 路径 | 已实现 | 当前限制 |
| --- | --- | --- |
| SSD 原生入口 | `BlockIO` 或固定主机轨迹 → sector trace＋配置 XML；运行/导入后校验汇总 | 固定到达、单 SSD 单 flow、NVMe、512 B sector；原生内部控制器由 XML 固定 |
| 裸 NAND / 精确执行 DSL | 显式物理操作与串行依赖导出；自定义驱动结果校验 | 状态为 `needs_driver`，仍需绕过原生主机/FTL 的定制驱动；原生 trace 不能代替 |

`adapter.py` 实现上述接口；`lock.json` 固定上游提交和单位；`__init__.py` 为包标记。与 [Ramulator](../ramulator/README.md) 平行，均只消费明确请求或操作，不在适配器内重新解释上层布局。完整联合任务仍使用 Python 模型。

## SSD 输入与准备

示例见 [mqsim_host_trace.json](../../../examples/mqsim_host_trace.json)。字段为 `version=1`、`kind=ssd_host`、`arrival_mode=fixed_arrivals`、`capacity_bytes`、`requests` 和 `provenance`。每条请求显式给出 ID、到达 ps、字节地址/大小和 read/write；来源须声明 synthetic、captured 或 frozen_model_replay，以及转换说明。

- 地址/大小须为 512 B 整数倍，到达 ps 须能**无损**转换为整数 ns；不自动取整、放大教学几何或丢弃依赖。
- 原生 trace 为 `到达ns 设备0 起始sector sector数 操作码`；写为 0，读为 1。按到达时间和 ID 排序。
- 使用用户提供的完整 XML，冻结其原文；检查关键接口、几何和保守容量界限，防止地址被原生模型折返。设置单 flow、关闭数据缓存、关闭预处理、初始占用 0、只回放一次。
- `host_trace(commands, target, provenance)` 将已确定的 SSD `BlockIO` 转为该格式，保留读改写产生的额外主机命令。它拒绝裸 NAND target。
- 如果到达时间取自本地模型，它只能代表 **冻结到达的回放**，不能视为另一后端中重新执行同一 KV 依赖任务。当前小字节/皮秒教学任务经常不满足原生粒度，接口会拒绝。

使用固定提交的 [官方配置](https://github.com/CMU-SAFARI/MQSim/blob/51f0f2d3fed92d88ef4a0fa61a38024b07bf9d16/ssdconfig.xml)，保存到本地后即可准备；以下第一步不运行仿真器：

```powershell
python -m agent_design_search.performance.mqsim.adapter prepare --trace examples/mqsim_host_trace.json --config .local/mqsim/ssdconfig.xml --output results/mqsim_bundle
```

新目录包含 `bundle.json / ssdconfig.xml / workload.xml / requests.trace`；bundle 固定输入、配置摘要、上游版本和能力边界。目录不可覆盖，运行前重新校验内容。

将来环境就绪时才执行：

```powershell
python -m agent_design_search.performance.mqsim.adapter run --bundle results/mqsim_bundle --executable .local/mqsim/MQSim --output results/mqsim_run
python -m agent_design_search.performance.mqsim.adapter import --bundle results/mqsim_bundle --xml results/mqsim_run/workload_scenario_1.xml --output results/mqsim_import.json
```

未启动进程为 `not_run`、0 次调用；启动后超时/失败为 `backend_error`、1 次。成功解析为 `aggregate_only`。导入已有 XML 不增加调用次数；原生 XML 没有 bundle 摘要，因此人工关联的导入标记为 `user_associated_unverified_run`，不能冒充经过运行器关联的结果。

## 结果与校准契约

固定版本是 [MQSim `51f0f2d`](https://github.com/CMU-SAFARI/MQSim/tree/51f0f2d3fed92d88ef4a0fa61a38024b07bf9d16)。依据其 [trace 实现](https://github.com/CMU-SAFARI/MQSim/blob/51f0f2d3fed92d88ef4a0fa61a38024b07bf9d16/src/host/IO_Flow_Trace_Based.cpp)与[结果实现](https://github.com/CMU-SAFARI/MQSim/blob/51f0f2d3fed92d88ef4a0fa61a38024b07bf9d16/src/host/IO_Flow_Base.cpp)：

- request count 是生成数；完成字节数另报。解析器同时核对读/写数和完成字节总量，不能只看总请求数。
- 平均/最小/最大延迟是**截断后的 uint32 整数微秒**；保留 `_us`，不能写成 ns 或伪造更高精度。
- 原生结果没有逐请求完成事件。汇总匹配不等于内容正确或逐请求完成证明；不推导精确 makespan、p95 或当前 `completion_ps`。

实际校准仍未进行。恢复后先检查原生请求覆盖、单位、初态和缓存设置，再比较同一固定主机流的平均延迟/流量。要比较当前 DSL 的策略排名，还须实现等价控制器规则或自定义驱动；不能把原生 MQSim 自己的 FTL 决策收益记给 Agent。跨介质联合评价还需让完成事件驱动后续请求，不能把独立运行的完成时间简单相加。

## 裸 NAND 的自定义驱动接口

`prepare_raw_nand(geometry, actions, provenance, output)` 接收固定 NAND 几何和已选择的 `read/program/erase`、`physical_page`，导出介质坐标和 `after` 依赖；擦除必须指向块首。CLI 为 `prepare-raw --input <JSON> --output <新文件>`，输入对象包含这三个字段。

当前接口刻意使用串行物理回放，并返回 `needs_driver / backend_calls=0`。它不包含动态 GC 决策、管理步骤和上层链路成本，不能当作全系统评价。`validate_raw_result` 要求 bundle 身份一致、单位 ps、所有操作恰好完成一次，且时间满足依赖；内容正确性仍由独立参考检查。尚无实现这个协议的 MQSim 定制驱动。

接口测试见 [test_mqsim_backend.py](../../../tests/test_mqsim_backend.py)，其中 XML、时间和进程均为明确的测试夹具。
