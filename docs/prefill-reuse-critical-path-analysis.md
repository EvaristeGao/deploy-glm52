# Prefill reuse：128K关键路径分析

> **2026-11-05：32K／64K逐层实测完成。** B4/P3相对相同INT8／DCP1无transfer参考，额外prefill开销约0.15%／0.51%；64K层间77层GET直接Free合计约0.39ms，剩余主要是第0层约69ms启动恢复。关闭offload后SFA仍约12.46s／64K，计算路径才是大头。48请求全通过，原服务恢复审计PASS，源码原分支不变。详见[完整层时延与优化预算](prefill-reuse-layer-overlap-32k-64k.md)。下方保留早期128K／不同配置口径，不能混用样本或归因。

> **最新边界实测已完成**：固定B2/P1、同次128K TP0 profile的10.361秒CANN Free中，9.553秒位于主线程等待native GET；21个带Indexer层贡献7.402秒GET暴露（包含其Main传输，不能全算可省）。进一步确认block-key没有经过GVA的独立层驻留起点过滤，独立Indexer data/scale仍参与恢复。优先验证previous-step的组件驻留过滤，尚未实施该优化。12请求、64份TP记录与原服务恢复审计均PASS；详见[本轮完整结果](prefill-reuse-boundary-trace-results.md)。下文前半部分保留上一轮阶段分析。

2026-11-04。源码`717f6e6e0`，B2/P1、Main/Indexer INT8、TP16×DP2、DCP1、eager、chunk32784、输出1 token。用户指出应先归因再调参数后，已停止后续参数扫描；本报告基于之前独立录制的128K profile，不新增参数实验。

## 已有证据支持的结论

**未被掩盖的时间主要暴露在层入口、Prolog提交之前，并随chunk历史增长。** 不能把所有Event Synchronize耗时都算成损失，也不能把之前相对原DCP配置的整体上涨都归于offloading。

本次profile实际输入131067 tokens，server prefill为57.924秒、首token正确；其耗时不混入正式性能表。它比本轮B2正式样本中位数55.958秒约高3.5%，包含profile开销和样本差异。只解析了P0的TP0；尚未取得其他rank的关键路径归因或memcache同条件实机对照。

## 时间线量化

解析CANN完整时间线3211935个事件，按区间合并并求交集，避免叠加并发线程和嵌套事件。

| 真实chunk | 78层CPU窗口（秒） | CANN Computing（秒） | 未重叠Communication（秒） | CANN Free（秒） | 其中Prolog提交前Free（秒） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 9.568 | 8.941 | 0.617 | 0.010 | 0.002 |
| 2 | 11.254 | 10.147 | 0.627 | 0.480 | 0.466 |
| 3 | 15.198 | 11.393 | 0.606 | 3.198 | 3.172 |
| 4 | 21.619 | 12.702 | 1.295 | 7.622 | 7.589 |

“CPU窗口”取该chunk第一层`vllm::mla_forward`进入至第78层返回，因此不等于完整scheduler step；表外还有chunk边界和一个没有C8 attention的DP dummy批次。四组各有78个C8 attention kernel，额外dummy组没有C8 kernel，没有把它误作第5个真实chunk。

整个解析窗口按CANN分类合并后：Computing 43.372秒、未重叠Communication 3.311秒、Free 11.466秒，总覆盖58.149秒。**Free是CANN对计算／通信区间的分类，不独立证明所有设备／DMA均空闲；Computing也不能等同于纯模型数学计算。** 它和server prefill的统计边界不同。

把Free区间与主线程实际op边界求交集：

| 主线程所在阶段 | 与CANN Free相交的时间（秒） |
| --- | ---: |
| `mla_forward`进入 → `npu_mla_prolog_v3`调用 | **11.229** |
| Prolog调用 → 量化SFA主机调用 | 0.038 |
| 量化SFA主机调用内 | 0.001 |
| 量化SFA主机调用返回 → `mla_forward`返回 | 0.042 |

**11.229秒约占完整Free的97.9%。** 这定位了暴露阶段，不等于已经把11.229秒全部量成GET耗时。第一chunk在Prolog前也有约8.98秒主机等待／准备窗口，但与设备计算重叠，交集Free只有0.002秒；正好说明“CPU在等”本身并不代表掩盖失败。后两个chunk的同一阶段才出现明显暴露。

## 同步没有被直接算成损失

`aclrtSynchronizeEvent`共1161次，跨线程累计54.680秒，其中两个后台线程分别为41.376秒／312次和13.228秒／839次，大部分与CANN Computing重叠；与Free求交的跨线程累计仅约0.010秒。主线程tid765的6次该API调用耗时很小。

因此不能说“同步54秒导致慢了54秒”。但后台线程的同步仍可能通过load-ready依赖阻塞主线程；本次没有单独标记Python `Event.wait`／reader账本等待，不能因直接API交集很小就排除这种间接依赖。

源码中`_drain_attention_transfers`默认保留预取队列，`fence_drains_recv()`返回false，发送队列允许积压；不是每层attention结束都清空所有GET/PUT。量化SFA主机调用之后只对应约0.042秒Free，也不支持把主要暴露归到该位置的统一drain。

## 计算热点和传输任务

| TP0设备算子 | 次数 | 算子执行累计（秒） |
| --- | ---: | ---: |
| KvQuantSparseFlashAttention | 312 | 24.999 |
| LightningIndexerQuant + LightningIndexer | 64 + 20 | 9.507 |
| MlaPrologV3 | 312 | 3.230 |
| DispatchFFNCombineW4A8 | 375 | 2.850 |
| HixlBatchGet / HixlBatchPut | 27768 / 18720 | 0.982 / 0.711 |

这些是算子累计，跨stream会重叠，不是因果墙钟占比。尤其Hixl任务时长不包含完整GET/PUT的排队、元数据、控制和远端完成链路。底层HIXL任务／API次数也不等于Python Mooncake range API的调用次数。

HIXL Read／Write主机API跨8个native线程累计约3.783／2.917秒，stream同步累计4.517秒，彼此和计算有重叠；不能再加到11.229秒上。它们说明底层展开了大量任务，支持继续检查控制与提交开销，但尚不能单靠次数证明GIL、Master RPC或RoCE带宽是根因。

已有同配置消融在32K得到pure 9.576秒、B2 9.588秒；128K得到pure 45.458秒、B2 55.958秒。前者说明当前计算配置本身已明显慢于原始DCP档，后者说明历史恢复／共享流程又有额外开销。Main INT8与BF16算子路径、DCP、chunk和其他原始配置差别未全部单变量隔离；不能把25秒量化SFA直接宣布为“INT8相对BF16多出来的25秒”。

## 代码落点与仍缺的归因

当前Prolog V3路径的顺序为：

```text
vllm::mla_forward
  SFA.forward：metadata／cache tuple／parallel context
  wait_for_kv_layer_from_connector
    connector.wait_for_layer_load
      PoolWorker.wait_for_layer_load
        提交lookahead load → 等当前层load_finished
  可选KV hook／Prolog参数准备
  npu_mla_prolog_v3
  Indexer／SFA → 输出投影／保存
```

入口见源码`ops/mla.py::mla_forward`、`attention/sfa_v1.py::forward`的Prolog分支（约2010行）、`attention/utils.py::wait_for_kv_layer_from_connector`及`pool_worker.py::wait_for_layer_load`（约3620行）。receiver在`kv_transfer.py::_handle_range_layer_task`中依次处理旧save、旧compute完成、attention gate、外部读者释放、地址构造和range GET，然后发load-finished。

**当前已经定位到“后续chunk的层入口、Prolog前准备／KV就绪阶段没有充分掩盖”，但还没有精确拆开以下部分：**

1. load队列排队，及旧save／compute／外部reader导致的槽位等待；
2. `build_addrs`、range元数据构造与拆批；
3. native GET提交、远端完成和load-finished发布；
4. 主线程Python准备、调度或其他rank的间接拖延。

下一步应固定B2/P1，在同一个128K请求记录这些边界的`perf_counter_ns`和chunk/layer/slot/generation，分开记录enqueue→receiver start、每个gate等待、metadata build、每个range call和load-finished→主线程恢复；按层汇总并与NPU事件对齐。探针应只缓冲时间戳、请求结束后一次导出，避免逐层打印干扰。需要对主要rank核对后，才决定改预取窗口、native提交聚合还是槽位调度；先不继续参数矩阵，也不删除保证I1的屏障。

## 证据和环境

证据目录：[perf-tuning-32k-128k](../artifacts/p-main-buffer-reuse/perf-tuning-32k-128k/)。关键文件：

- `profiler-raw-rank0-b2p1.tar.gz`：解析前原始TP0数据；其他rank原始数据仍在停止的自有r16内。
- `profiler-selected-b2p1-p0.tar.gz`：kernel_details、op_statistic、完整trace_view和metadata。
- `profiler-summary.json`、`critical-path-summary.json`、`critical-path-stage-summary.json`：算子、区间和按层汇总。CANN epoch微秒时间戳先用Decimal减去固定原点，再转相对微秒计算，避免大数浮点丢失子微秒精度；`critical-path-interval-audit.json`验证分类交集／区间边界，PASS。最初离线脚本类型错误日志保留，不是NPU故障。
- `critical_path.py`、`extract_cpu_stages.py`、`classify_stalls.py`：可从保存的trace复算，逐事件流式读取，避免把约1GB JSON同时展开。
- `requests-profile-b2p1.jsonl`：独立profile请求；`requests-*.jsonl`和`evidence-audit.json`保存正式数据与主动中止的参考组部分记录。
- `restoration-audit.json`：PASS。原P r05／D r03四节点ID、image、owner、启动脚本及每节点10文件源码manifest均一致，健康200／空闲，P0/P1新单token数学请求均为4；r16停止退役，proxy保持原未运行状态。D和Master未修改。

本轮没有生产实现改动，源码分支仍`p-main-buffer-reuse`且干净；没有做1M或新PD数据面验收。官方memcache参考差异和已完成参数计时见[对照记录](prefill-reuse-memcache-comparison.md)，它们不能替代本报告的关键路径归因。

## 后续边界探针与memcache时序核对（过程记录，已完成）

用户授权开始固定B2/P1的细分观测。诊断文件放在`artifacts/p-main-buffer-reuse/prefill-boundary-trace`，只生成r17隔离容器overlay，原`p-main-buffer-reuse`源码不改。去掉探针后原传输/worker AST还原一致；仅额外将CANN采集限制到worker rank0（profile方法无collective），其他TP保留轻量边界记录。6项CPU验证覆盖真实receiver成功/异常路径、关闭探针、并发记录/导出、缓冲上限和队列上下文。以下为实验开始时的计划；实测与恢复审计现已完成，结果见上方链接。

进一步核对本地memcache/GVA与Mooncake/range接收时序：

| 阶段 | memcache/GVA | 当前Mooncake/range |
| --- | --- | --- |
| 槽位旧代依赖 | 等旧save及旧compute | 等旧save及旧compute |
| 地址元数据 | 在attention gate前构造本层task metas | 在attention gate及external reader/source begin_write后构造 |
| attention gate之后 | 聚合本层各group地址，再等external reader/source begin_write | 每个group构造range并立即发GET |
| 数据面调用 | 聚合GVA/addrs/sizes后batch_copy（仍可能按限制拆批） | 分group、按限制拆range调用，存在session/publication协议 |
| 完成 | 本层copy完成后发load-finished | 本层range完成后发load-finished |

PUT同样存在差别：memcache先准备/聚合多个group，再等KV-ready同步事件并copy；Mooncake按group构造range、等ready、PUT，在对应group末层执行commit。两者均使用FIFO，不能把FIFO当作Mooncake独有差异。该比较是当前冻结源码的控制流，不是memcache实机时延结论；官方默认参数差别另见memcache对照文档。纯元数据是否值得前移、group聚合是否值得优化，等待边界耗时确认后再改。
