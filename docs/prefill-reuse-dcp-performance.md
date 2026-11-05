# P Main buffer reuse 与原始 DCP 配置性能对照

日期：2026-11-04。状态：六档成对测量、原环境恢复审计与离线证据审计均完成。

## 测试范围与口径

本轮比较用户指定的 Ansible 原始 `deploy_config: 1m` 与当前 P Main buffer reuse 配置，另保留 `1m-v030` 辅助对照。固定当前源码 `717f6e6e0d3e327e292c3b3d0d95da116b5319fe`，各组使用相同权重、相同节点上的 native 扩展、TP16×DP2 与 eager 模式。原始组关闭我们的复用特性，开启 DCP16；reuse 组 DCP1、Main 两个共享 buffer 加首尾独立层、Indexer 独立。

这是实际配置整体比较。DCP、chunk、MTP、精度与 Store 协议等配置也有差别，不能把全部耗时差归为共享 buffer 的净开销。原始配置的 Main 保留默认 KV dtype，没有为了凑齐配置强行指定 INT8；Indexer 按旧配置的 C8 开关迁移成显式 INT8。集群运行必需的 RoCE 选择在三组中共同固定。

仅测试 **P 独立冷 prefill＋首 token**：通过跳板访问 P0，单并发，输出 1 token。D、Master 不切换，未使用 proxy，未启用新的 receipt／Host Target 数据面；不代表 P/D 端到端或持续 decode 性能。

输入为 4K、8K、16K、32K、64K、128K（4096–131072 tokens），实际 tokenize 长度与目标偏差不超过 8 tokens。每长度 1 次预热、5 次计时。三组逐 case 使用完全相同的 body、salt 和提示词摘要；每 case 的前缀唯一，模型目录私有别名指向同一权重，用于隔离 Store namespace，没有清理 Master／既有 Store 对象。

每个提示词在 10%、50%、90% 位置埋入三个数字标签，轮换询问其中一个；以是否返回预期数字核对基本正确性，不要求两组逐 token 等同。记录实际 token 数、本地／外部 prefix hits、抢占、请求数量与答案。失败样本保留，不以重试替换；本轮不恢复先前暂停的 64K 数值根因定位。

客户端 TTFT 从发起请求至第一个非空内容／reasoning SSE delta，跳过纯 role／空 delta。服务端 prefill、TTFT、queue 来自请求前后 Prometheus histogram 的增量。指标等待请求完成且计数稳定再采集。预热、进程加载和启动编译不计入统计。

耗时报告中位数，完整 JSON 同时保留 min/max。延时变化为 `(reuse / 原始配置 - 1) × 100%`；负值表示 reuse 更快。正式比较只取双方均通过所有校验的相同 case；另外列出全部已完成请求的 prefill 观测和正确数，避免把错误输出当成有效性能结果。

## 原始配置的兼容迁移

未迁移的原始 `1m` 已实际启动测试，在配置初始化阶段失败：当前 v0.30 的 `AscendConfig` 不再接受 `c8_enable_reshape_optim`，未执行模型请求。失败证据见 `startup-original1m-p0.log`／`watch-original1m.log`。

主基线改用独立的 `original1mcompat` 档：删除这一过期键；将旧 `enable_sparse_li_c8=true` 等价迁移为 `--attention_config.indexer_kv_dtype int8`。这是必要的语义迁移：当前 `ascend_config.py:926–927` 会从 dtype 重算 C8 开关，覆盖旧开关，直接删除过期键仍不能保留旧 Indexer 精度。Main 保持默认，DCP16、chunk8192、MTP1、max_num_seqs64、内存比例0.9等性能设置不变。

失败与迁移前后的配置均保留，没有覆盖原 Ansible inventory。迁移说明见 `original1m-compatibility.json`。所以报告中的“原始1m”是这一兼容迁移档，不能称为旧配置逐字原样运行。

## 配置与结果

| 关键配置 | 原始1m兼容档 | reuse |
| --- | --- | --- |
| DCP | 16 | 1 |
| Main共享槽 | 无 | 2＋首尾独立 |
| Main KV dtype | auto（模型BF16） | INT8 |
| Indexer KV dtype | INT8 | INT8 |
| chunk | 8192 | 32784 |
| max_num_seqs | 64 | 1 |
| P MTP | 1 token | 关闭 |
| Store | blockwise | layerwise |
| gpu_memory_utilization | 0.9 | 0.9 |
| P运行模式 | eager | eager |

服务端 prefill 中位数如下，两组每行均为5/5个正确且通过全部采样校验的成对样本。

| 输入 | 原始1m兼容档 | reuse | 延时增幅 | 耗时倍数 |
| --- | ---: | ---: | ---: | ---: |
| 4K | 0.60s | 1.18s | +96.3% | 1.96× |
| 8K | 0.82s | 2.30s | +181.2% | 2.81× |
| 16K | 1.62s | 4.67s | +187.7% | 2.88× |
| 32K | 3.30s | 9.58s | +190.3% | 2.90× |
| 64K | 6.76s | 20.80s | +207.7% | 3.08× |
| 128K | 14.19s | 56.42s | +297.5% | 3.98× |

以同一输入长度除以prefill时间估算，单请求有效prefill吞吐约下降49%–75%；这不代表多并发服务吞吐。

客户端首内容 TTFT：

| 输入 | 原始1m兼容档 | reuse | 延时增幅 |
| --- | ---: | ---: | ---: |
| 4K | 0.94s | 1.30s | +38.3% |
| 8K | 1.16s | 2.42s | +108.1% |
| 16K | 1.98s | 4.79s | +142.1% |
| 32K | 3.65s | 9.71s | +166.0% |
| 64K | 7.12s | 20.92s | +193.9% |
| 128K | 14.91s | 56.54s | +279.3% |

服务端prefill样本最小／最大值：

| 输入 | 原始1m兼容档 min–max | reuse min–max |
| --- | ---: | ---: |
| 4K | 0.580–0.716s | 1.175–1.184s |
| 8K | 0.793–0.832s | 2.297–2.300s |
| 16K | 1.610–1.638s | 4.656–4.676s |
| 32K | 3.292–3.307s | 9.576–9.586s |
| 64K | 6.756–6.772s | 20.791–21.099s |
| 128K | 14.180–14.210s | 56.127–56.853s |

主基线与reuse的36个请求（含每长度1次预热）均正确；各30个计时样本全部满足真实tokens一致、零本地／外部prefix hit、无抢占和每次恰好1条请求等校验。没有替换失败或挑选更快样本。

辅助`1m-v030`虽然36次均HTTP200，其30个计时样本正确数依次为5/5、5/5、4/5、0/5、0/5、1/5。未使用它得出上述主结论；它和主基线还有Main精度、MoE/环境等差别，本轮没有隔离错误根因，也不恢复64K数值定位。

配置详细表及辅助组耗时见 [RESULTS.md](../artifacts/p-main-buffer-reuse/perf-dcp-comparison/RESULTS.md)。原始1m兼容档Main为`auto`、模型dtype为BF16、Indexer显式INT8；reuse Main和Indexer均INT8。三组运行日志均确认eager；原始两组DCP16/MTP1，reuse DCP1/无MTP。

顺序为辅助1m-v030→reuse→原始1m启动失败→恢复审计→兼容原始1m补测。各组未交错随机运行；样本量为每长度5次，没有做仅切换共享槽、其余完全一致的消融。上述结果支持“当前两套实际配置的差距”，不能单独量化offload拷贝／屏障／DCP各自占比。

## 时延上涨原因分析（代码与已有测量，未做算子profile）

目前最有依据的解释是**计算并行路径改变、共享槽保存／等待、跨chunk历史恢复**共同作用。还没有同配置消融和profiler，不能给它们分配百分比，也不能证明其中任一项已占据最大份额。本节仅分析已保存数据及同一717f6e6e0源码，没有切换服务或追加NPU请求。

### 1. DCP16基线还开启了DSA-CP，prefill计算确实不同

原始1m配置开启`enable_dsa_cp=true`，reuse配置为默认false。`attention/context_parallel/sfa_cp.py`中的`resolve_sfa_impl()`根据这两个开关选用不同实现；`AscendSFADSACPImpl._prepare_native_hidden_states()`按TP rank截取本地token片段，`_get_parallel_forward_context()`使用本地query长度。`attention/indexer.py`也生成本地query/key长度，Indexer处理本地query shard。

因此，这里的DCP档不只是改变decode KV布局：它同时使用prefill的DSA-CP token分片。关闭后，尤其Indexer这类未按普通TP头分片的工作，可能在多个TP rank重复处理更多query；上下文越长，历史评分／Top-K相关成本越值得关注。普通TP16仍在，不能把这等同于整个模型退化成单卡，也不能推断整网计算量直接增加16倍；SFA的head/token分配和collectives同时改变，收益需单独实测。

这是优先验证的计算侧主因候选。Main BF16与INT8还会选择不同缓存／量化／attention kernel，不能仅凭INT8就假定reuse计算更快。

### 2. 首chunk已经变慢，历史GET不能解释短请求差距

当前reuse chunk32784，4K/8K/16K/32K均能在一个chunk内完成，冷请求没有前序chunk的Main历史可恢复，却已分别出现0.58/1.48/3.05/6.28秒的prefill额外耗时。

单chunk仍要逐层把当前KV存入Store，并在复用前等待旧层读者。`pool_worker.py:3580`提交load/slot-write任务，`:3620`在计算侧等待本层ready；`kv_transfer.py:2132`的range receiver依次等待旧层save、previous_compute_done和external slot release，再允许新代写入。即使没有GET任务，也要走槽位安全交接，见`:2247`的空任务分支。发送侧`:1790`等待KV-ready event，再执行`batch_copy_put`；最后一层由`pool_worker.py:3726`排空尚未完成的PUT。

两个shared buffer、prefetch1保留了逐层重叠，但重叠窗口有限；PUT或恢复不能在窗口内完成时，计算会等它。以上是事件／线程级等待，不应描述成“每层全设备同步”或“完全没有流水”。保存所有逻辑层的KV是容量复用的代价，buffer数变少并不会让逻辑KV数据量同等下降。

8K到32K，reuse耗时随输入增大约4.17倍，说明每token的计算／搬运成本明显；单纯的固定Python hook开销不足以解释这段曲线。计算侧与传输侧各占多少仍未知。

### 3. 128K的跨chunk恢复量比64K增长更快

共享Main无法常驻所有层的旧prefix。`pool_worker.py:3055`明确选择完整Store历史prefix，而独立层可跳过已驻留HBM的部分；`:1907`从0开始构造共享层恢复范围。

按固定32784-token chunk预算推算，每个共享Main层：

| 输入 | chunk数 | 累计历史恢复token行（理论范围） |
| --- | ---: | ---: |
| 4K–32K | 1 | 0 |
| 64K | 2 | 32784，约32K |
| 128K | 4 | 32784+65568+98352=196704，约192K |

输入从64K翻倍到128K，历史恢复量约增加6倍。对于78个Main逻辑层中的76个共享层，这个重复恢复贯穿逐层执行。该表根据token预算与恢复代码推算，没有逐chunk trace，不能当作实测DMA字节计数。

以32K的9.58秒线性外推，128K约38.31秒；实测56.42秒，多18.11秒。这个差额是非线性成本的线索，**不是GET计时**：更长历史下的Indexer评分／Top-K、不同query/key形状、恢复／会话／CPU元数据及通信竞争都可能贡献。基线128K相对其32K线性外推也有增加，但较小。

### 4. 搬运调用粒度和CPU调度还有优化候选

`kv_transfer.py:741`按当前64 blocks上限拆批。发送和接收各一个线程，以循环同步调用Mooncake的`batch_put_from_multi_buffer_ranges`／`batch_get_into_multi_buffer_ranges`，后端返回后才继续下一批；调用映射见`backend/mooncake_backend.py:387`和`:412`。这不意味着Mooncake内部没有批内并行，但多批调用及Python地址构造／队列调度可能暴露开销。当前16MiB上限按单segment拆分，并非整次调用的总字节限制。

值得测试更大的block批次、更多shared buffer配合合适预取深度，以减少调用次数或扩大掩盖窗口。但它们会影响HBM容量、通信竞争和安全依赖，不能直接删同步或越过I1读者生命周期。

### 5. 已有数据能排除及尚不能确定的部分

两组服务端queue中位数约0.01–0.02毫秒，和秒级prefill差距不在同一量级；双方均eager，零prefix hit、无抢占，D指标未变。本轮没有新PD transfer数据面流量，因此这些涨幅不能归因于同事正在开发的PD receipt/Host链路。

`max_num_seqs64→1`不会在单请求、无排队时直接解释批处理吞吐差距；chunk8192→32784会改变算子形状和恢复次数，不能假设更大chunk总是更快。MTP差异存在，但本轮仅测首token，没有测持续decode的投机收益，不能拿后者解释本轮prefill倍数。

下一步应先做**相同INT8／无MTP／相同chunk和能容纳128K的模型上限下，DCP1逐层Store全独立Main对比B2**，隔离共享槽及历史恢复代价；另做相同纯计算配置下的DSA-CP/DCP16与关闭两者对照，量化计算路径影响。对B2的32K和128K采timeline，分别看Indexer/SFA算子、PUT/GET调用、槽位等待及最终drain在关键路径上的暴露时间。异步调用时间与计算时间有重叠，不能把CPU计时简单相加当作总贡献。

机器可读的queue中位数、chunk／历史恢复推算和线性外推见`latency-cause-analysis.json`。本轮没有新增性能实测或修改生产代码。

## 参数调优与后续优化候选（尚未实测）

B和prefetch都可调整。当前解析器仅验证正整数，没有强制`prefetch < B`；默认prefetch甚至可等于B。因此不能把P<B说成硬约束。实际并行度仍由共享槽数量、旧层PUT／attention／外部读者完成事件约束，较大的P不能凭空产生空闲槽；配置被接受不等于运行时已正确验收。

首轮候选（首尾两个Main独立、Indexer仍全部独立）：

| 配置 | 总Main物理槽 | 用途 |
| --- | ---: | --- |
| B2/P1 | 4 | 已实测的对照 |
| B2/P2 | 4 | 单独检查增加lookahead的效果 |
| B3/P2 | 5 | 较小容量代价，增加重叠窗口 |
| B4/P2 | 6 | 优先测的平衡候选 |
| B4/P3 | 6 | 保持B4，单独增加预取深度 |
| B8/P4 | 10 | 探索更大重叠窗口是否仍有收益 |

这些候选已执行源码中的纯布局解析函数，覆盖78层且物理槽数量符合预期；没有做新的NPU性能、正确性或死锁验收。机器可读记录为`tuning-candidates.json`。

固定num_blocks时，HBM预算为`num_blocks × [(B+独立Main数)×Main页字节数 + 全部私有Indexer页字节数]`（当前Main规格一致）。所以B2→B4是Main槽4→6，总KV显存还包含不变的Indexer部分，并非总显存翻倍。固定显存利用率自动重算blocks时，更多槽会降低可用逻辑容量，必须核对新num_blocks能覆盖1M＋运行预留，再对胜出配置做1M正确性验收。

优化顺序建议：

1. **先确定DCP1路径的参考下限。** 在能容纳128K的共同模型上限下，固定dtype/chunk/MTP，分别测无Store的纯计算、逐层Store全独立Main与B2；另用纯计算对照测DSA-CP/DCP16的收益。若DCP1纯计算本身就明显慢于原始，单调buffer无法追回全部差距。
2. **B／prefetch扫描。** 先用32K（单chunk）和128K（多chunk）筛选上述候选，记录正确性、真实capacity及PUT／GET／槽位等待／step drain的关键路径暴露时间。先固定传输批次和chunk，避免同时改多项。
3. **扩大传输批次。** 对选出的B/P，单独比较max_transfer_blocks=64/128/256，减少串行Mooncake调用。当前max_transfer_bytes是单segment分片上限，不是整批总字节限制；更大批次是否挤占collective带宽需要实测，不能直接无限放大。
4. **测试更大的chunk。** 例如32784→65552可减少128K的历史恢复轮次，但activation、MoE token/workspace预算和算子形状同时改变。不能只改max_num_batched_tokens，也不能假定越大越快；保持正确的预算校验并核对剩余KV容量。
5. **有热点证据后改实现。** 可优先考虑地址元数据复用、降低Python队列／调用交接、后端支持下的有界异步批量传输、减少阻塞式host等待。I1、真实DMA完成与publication边界必须保留；不以跳过PUT、提前报告完成或把drain移出统计区间冒充优化。
6. **若Indexer占主导，研究计算分片与共享Main解耦。** 当前block-key拓扑明确拒绝DCP/PCP>1，不能直接开原DCP。可研究只把Indexer query计算按token分片并正确聚合结果，保持Main源布局／生命周期契约；这属于新的计算／通信适配，聚合量和收益均需测量，不是配置开关。

其他长期候选是减少全历史Main回读，例如按真实访问块恢复；应先统计Top-K块并集覆盖率，若接近全量便收益有限，且须保留PD源视图完整性。若业务允许独立资源池，也可以让短请求走原DCP实例、长请求走reuse实例；这是路由策略，不提高reuse单实例性能。

本节只准备候选和操作顺序，没有修改生效Ansible配置或操作集群；不承诺B4/P2或其他组合已能追平原始DCP。优先目标是查明可追回的offload额外开销，再决定是否需要计算分片开发。

## 环境收尾

最终恢复审计 **PASS**：原P r05／D r03四节点的容器ID、image、owner、启动脚本与每节点10文件源码manifest均一致，四引擎健康200且空闲；P0/P1各一条新单token请求均正确返回4。proxy维持测试前未运行状态，自有r15测试容器已停止并标记退役。测试过程D r03和Master未改动，三组D前后指标一致。

离线证据审计 **PASS**：三组各36条原始结果完整、计时各30条，输入摘要／salt／真实tokens一致、指标增量可重算、测量代码摘要一致，配置与源码快照可追溯。这里的PASS是完整性审计，辅助1m-v030错误答案仍保留，不代表其正确性通过。本轮未修改或提交生产源码，源码工作区仍为原`p-main-buffer-reuse`分支且干净。


## 原始证据与复现

所有内容保存在 [perf-dcp-comparison](../artifacts/p-main-buffer-reuse/perf-dcp-comparison/)：

- `PLAN.md`、`profile-{original1m,original1mcompat,reuse,dcp}.json`、`generated-*/`：计划、独立测试配置与实际启动脚本。
- `source-head.txt`、`source-manifest.json`、`source-snapshot.tar.gz`：源码版本与 732 个 Python 文件快照；候选容器加载前后核对摘要。
- `fixtures.json`、`fixture-index.json`：原始成对请求、实际 tokens、正确答案与内容摘要。
- `requests-*.jsonl`：全部预热／计时请求、原始答案、客户端计时、服务端指标前后快照与校验结果。
- `comparison.csv`：六档主对照的prefill／TTFT及延时增幅，便于表格导入。
- `FINAL_STATUS.json`：本轮收尾状态及历史失败说明。
- `summary.json`、`RESULTS.md`：成对有效样本与全部完成样本分别汇总。
- `engine-*.log`、`engine-final-tail-*.log`、`startup-*.log`：启动与结束日志；后续日志只取尾部以限制体积。
- `memory-*.txt`：启动／收尾时的静态 NPU 使用量，不能当作请求峰值显存。
- `original-identities.json`、`original-containers.json`、`original-*.sh`：原容器身份、镜像与脚本。
- `evidence-audit.json`：离线核对全部请求、配对摘要、配置和测量代码的完整性；其PASS不表示辅助组所有答案正确。
- `restoration-audit.json`：收尾时核对原四节点身份、脚本、源码 manifest、健康空闲、P0/P1 单 token 数学题、原 proxy 状态和测试容器停止状态。

测试驱动为同目录 `control.py`、`run_benchmark.py`、`run_all.py`、`run_compatible_original.py`、`analyze.py`、`verify_evidence.py`。这是一次性已捕获身份的实验驱动，不能直接在未知环境重跑；计时请求文件禁止覆盖。每次切换均核对自有容器 owner／ID／image 及服务空闲，只操作开发容器及其内部目录。原 P r05 保留，原 D r03 与 Master 不做变更；测试结束恢复原 P。
