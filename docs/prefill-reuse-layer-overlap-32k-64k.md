# 32K／64K Prefill：计算、搬运与 buffer 预取实测

日期：2026-11-05。状态：**全部测量与恢复审计完成**。源码`p-main-buffer-reuse / 717f6e6e0`；诊断容器r18。本轮没有生产源码修改、branch切换、push、1M请求或新PD数据面验收。

## 结论

- 当前计算配置下，B4/P3的offloading额外开销已降至32K约0.15%、64K约0.51%，相对于本轮相同INT8／DCP1的无transfer参考。
- B4/P3的64K中位数20.419秒，比B2/P1的20.926秒快507.5ms（2.43%）。仅增buffer而保持prefetch1的B4/P1没有明显收益。
- **64K层间GET稳态已基本掩盖。** B4/P3同次profile中，GET与主线程load-ready等待及CANN Free的交集约69.3ms；其中第0层约68.9ms，其余77层合计约0.39ms，接近计时误差量级。首层恢复气泡另计，不能称整个请求零开销。
- 当前大头是模型计算：B2/P1的64K真实core区间并集18.901秒，其中SFA12.466秒、Indexer2.445秒、Prolog1.616秒。纯计算64K的SFA设备区间并集为12.459秒，core为18.906秒；与复用组对照，计算大头在关闭全部KV transfer后仍然存在。

## 范围与公平对照

四组使用同一镜像／native扩展、冻结的同一源码、同一权重；TP16×DP2、DCP1、eager、Main／Indexer dtype参数INT8、chunk32784、seq1、无MTP、MoE预算524544、传输batch64／16MiB、首尾Main独立、全部Indexer独立。B2/P1是共享槽2／预取1；B4/P1仅改共享槽4；B4/P3再改预取3。pure移除全部KV transfer配置，保留当前计算参数，并使用完整独立KV分配，作为计算参考。

统一`max_model_len=131072`以覆盖32K／64K并容纳pure。每组每长度1次预热＋3次正式请求；另有独立32K／64K profile及两条4K flush，共48请求。正式样本固定同prompt，独立cache_salt，输出1 token，校验答案、实际tokens、computed tokens、零local/external prefix hit、单请求及无抢占，48/48有效。首次编译／预热与profile不进入中位数。模型上限调整是本轮公平对照设置，不构成1M验收。

只访问P0，请求计算在P0的16个TP rank执行；P1参与DP通信，没有该请求的KV任务。采集P0全部16 TP的host边界，**设备CANN仅TP0**。没有真实P/D源读者，不能外推PD发送加入后的槽位占用时间或吞吐。

## 正式请求耗时

server prefill，中位数（min–max），单位秒。每格3个有效正式样本；不是整套原始DCP配置的对照。

| 配置 | 32K | 64K | 相对pure：32K / 64K |
| --- | ---: | ---: | ---: |
| pure | 9.571（9.566–9.573） | 20.316（20.298–20.318） | +0.00% / +0.00% |
| b2p1 | 9.585（9.582–9.587） | 20.926（20.893–21.359） | +0.15% / +3.00% |
| b4p1 | 9.583（9.580–9.584） | 20.952（20.888–21.124） | +0.13% / +3.13% |
| b4p3 | 9.585（9.585–9.587） | 20.419（20.413–20.465） | +0.15% / +0.51% |

顺序执行、未交错随机，不能把几毫秒差异或本轮0.15%的32K变化当成稳定收益。正式中位数与下方独立profile来自不同请求，不能把两者的差值逐毫秒相加归因。

## 真正的每层计算与恢复时延

使用完整decoder层`record_function`，通过CPU enqueue/dequeue correlation、`Node@launch`／ACL connection ID关联到NPU任务。计算使用AI_CORE／AI_VECTOR_CORE／MIX_AIC／MIX_AIV的区间并集；CPU forward、Event synchronize和AI_CPU collective包络均不能当作该层计算时长。这里的core执行区间包含算子内部访存／停顿，不等于芯片算力利用率。保留DP dummy scopes，但按真实请求与SFA kernel识别78／156个真实层执行。

B2/P1分chunk的实测如下，均为TP0，时间列单位ms；Indexer列是该chunk全部LightningIndexer系列主核的累计区间，不包括其所有前后处理；Prolog同样只统计MlaProlog命名主核。Other含投影、FFN、归一化及未归类的前后处理。

| 请求 / 当前累计tokens | 恢复历史tokens | 每层core均值 / 中位数 | 每层SFA均值 | Indexer累计 | 有GET层的GET中位数 / 最大 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 32K / 32762 | 0 | 113.7 / 104.7 | 79.9 | 657.5 | 无历史 GET |
| 64K / 32784 | 0 | 113.9 / 104.5 | 79.9 | 658.4 | 无历史 GET |
| 64K / 65531 | 32784 | 128.4 / 104.8 | 79.9 | 1786.2 | 50.7 / 131.9 |

正常无Indexer层约101–105ms core；含Indexer的层更重。64K第二chunk的Indexer累计由第一chunk约658ms升至1786ms，而SFA每层仍约80ms。历史增长既增加恢复量，也增加Indexer评分／Top-K计算；不能把第二chunk所有上涨都归给搬运。

64K完整profile对照：core为模型计算区间并集，GET／PUT为native接口完成时长，最后一列才是主线程等GET时直接暴露的Free。**这些列有并发重叠，不能相加。**

| 配置 | core（秒） | native GET（秒） | native PUT（秒） | GET等待Free（ms） |
| --- | ---: | ---: | ---: | ---: |
| pure | 18.906 | 0.000 | 0.000 | 0.0 |
| b2p1 | 18.901 | 5.272 | 1.517 | 516.0 |
| b4p1 | 18.903 | 5.151 | 1.483 | 484.8 |
| b4p3 | 18.901 | 4.742 | 2.115 | 69.3 |

例如B2的GET累计5.272秒，其中约4.575秒与实际模型core重叠，约0.154秒与未重叠通信分类相交，GET等待Free为0.516秒。B4/P3的PUT native累计反而更长，但端到端更短，说明累计搬运调用时间不足以判断性能。PUT与GET也能同时出现在Free中，不能把各自Free交集相加；按主线程互斥阶段划分，B2 final-save直接Free仅约12.9ms。

## 为什么P1没掩盖好，P3能做到

以64K第二chunk的第6层为例：B2/P1 GET约131.9ms，前一层第5层core约101.1ms，且GET还要等该层attention-start gate才真正发起，暴露约33.1ms。第6层自己的core虽约179.3ms，其中Indexer约68.9ms，但当前路径须先恢复KV再进入本层计算，这段计算不能倒过来掩盖自己的GET。

B4/P3把同一层GET提前到更早层的计算期间；该次GET约144.2ms，但直接Free约0.019ms。更多slot和lookahead共同提供提前量，不需要删掉旧槽计算／Store／外部读者屏障。以下是实测区间，颜色代表同一层；core轨道仅画实际core任务，白隙保留，native GET包络也包含内部调度／完成等待。

![64K实测跨层流水](../artifacts/p-main-buffer-reuse/perf-layer-overlap-32k-64k/layer-timeline-64k.svg)

对有旧共享占用者的层，取旧占用者compute-done设备事件、保守Store source完成、receiver上一GET完成的最大值，再与实际GET开始比较：

| 配置 | 可关联层数 | 可启动约束满足→真实GET开始的中位数（ms） |
| --- | ---: | ---: |
| b2p1 | 74 | 27.0 |
| b4p1 | 72 | 65.7 |
| b4p3 | 72 | 25.9 |

这量出尚未利用的调度窗口，并不证明任意时刻提前DMA都满足底层collective约束。B4/P1更早有空槽，却仍受P1/gate时序限制；B4/P3才利用了提前量。它支持mc.pdf的稳态流水设计，不能将当前80ms SFA窗口当成理论上限。

## 搬运还有什么可优化

1. **先使用已验证的B4/P3。** 本轮追回64K约0.51秒。保持传输batch、首尾独立和其他参数不变，仅将Ansible的`pd_cluster.layerwise.num_shared_buffers`改4、`prefetch_layers`改3；本轮没有自动改生效inventory。Main物理槽从4（2共享＋2独立）变6（4＋2），固定KV预算下日志num_blocks由30010降至24614，约−18%。这是容量换提前量，不是最小buffer数的证明。B2/P2能否取得相近收益尚未测试；没有本轮1M回归。
2. **首层驻留恢复过滤。** B4/P3剩余GET直接暴露几乎全在第二chunk第0层，预算约69ms（约64K总时延0.34%）。第0层Main和Indexer都独立，若证明同一请求／上一snapshot的block map、owner与有效范围不变，可跳过已经驻留的历史恢复；新请求、外部prefix命中、重映射和不确定情况须完整GET。当前探针没有提供该驻留证明，不能直接删GET。
3. **减少Indexer重复range。** B2的57个单Main层GET平均48.5ms；16个Main＋Indexer data／scale三段层平均129.1ms，payload仅约多20%。这是分组相关性，尚未隔离底层原因。block-key路径绕过了GVA的independent驻留过滤，需以组件为粒度补证明；保留对象格式和remote offsets，不能删登记项重排布局。B4/P3已掩盖多数层，不能把这16层的全部GET累计都称作端到端收益。
4. **native内部延迟分解。** 每个完整batch的HIXL API数中位数约15；单Main／三段的native中位数约12.2／31.5ms，首次HIXL调用前约2.4／6.0ms，HIXL任务包络约9.5／25.7ms。设备AICPU任务busy并集仅约0.5–0.6ms，不能把该busy、包络或native wrapper任何一个直接当RoCE数据飞行时间；约0.1ms的末尾边界差也不足以判断源读取安全性。后续可查SDK job排队、分段处理与完成链路，但本轮没有断言带宽饱和或GIL是根因。

当前B4/P3相对pure的64K正式增量约102.9ms。首层69ms、其他协议准备／末尾drain的profile预算与其同量级；来自不同样本，不能硬拆成精确因果份额。**继续只优化offload，已不能追回原始配置相差的十几秒。**

## 更大的收益应从计算路径找

当前SFA占64K约12.47秒。若保持其余条件、真实把SFA缩短10%，串行计算预算约1.25秒；这只是条件估算，不是已实现收益。Indexer还有约2.45秒，值得按真实query分片、dtype和kernel路径继续拆解。

源码`device/device_op.py::execute_sparse_flash_attention_process`按实际KV dtype选择量化SFA或BF16 SFA。原始Ansible兼容配置还使用DSA-CP／DCP16、BF16 Main、不同chunk与MTP设置，历史32K／64K约3.30／6.76秒；这是不同配置整体比较，不能单独归因DCP、INT8或reuse。普通TP16仍在，DCP1不等于模型退化为单卡。本特性当前不能直接开启DCP；若继续追原始性能，下一项应是同DCP1条件下Main BF16与INT8的单变量计算对照，或另行设计计算分片与共享Main解耦，不能仅改开关承诺兼容。Main BF16还会增加KV字节数／HBM预算，并缩短可用于掩盖搬运的计算窗口；计算对照有收益后仍须带offload重测，不能承诺改dtype就能追平原始。参考[原始配置对照](prefill-reuse-dcp-performance.md)。

## 与memcache时序的关系

| 项目 | 当前memcache／GVA | 当前Mooncake block-key |
| --- | --- | --- |
| 逐层调度 | 共用worker、收发线程与FIFO、旧slot事件 | 相同框架 |
| 预取启动 | 当前源码同样等attention-start gate | 同样等gate；P1实际到前驱SFA才启动 |
| 地址构造 | gate前构造 | gate后构造；本轮直接暴露很小 |
| 驻留过滤 | 按independent_layers及vllm_cached_tokens选恢复起点 | block-key分支提前continue，未走该GVA过滤 |
| 原生数据面 | GVA/address/size batch_copy | key/buffer/size/offset的session range接口 |
| 官方参考配置 | 指南B3／首层独立，本地源码默认P3；A3示例device_sdma | 本轮RoCE preset、B2/P1起步，验证B4/P3 |

因此可以借鉴更积极的lookahead和正确的驻留过滤，不能说memcache靠忽略计算／读者屏障获得性能。GVA通用布局也不保证所有Indexer独立；这里需要适配的是Main-only布局的组件驻留语义。本轮没有部署memcache同条件实测，不能给它分配精确速度优势。代码／官方资料及口径见[memcache对照](prefill-reuse-memcache-comparison.md)，设计约束见`mc.pdf`第4页§2.4、第7页§3、第8页§4。

## 验证、产物与限制

- 本轮48个请求全部有效；每组设备关联覆盖率超过99%，复用组各702个已有事件record全部匹配，真实层78／156个。三组共96份host traces（2长度×16TP×3），无drop；每个历史层16rank的GET均齐全。16rank完成偏差中位数B2/P1约3.8ms、B4/P1约3.7ms、B4/P3约3.4ms，设备关键路径仍只测TP0。
- 诊断overlay不新增device同步／poll，不移动gate。剥离探针后4个文件AST与冻结基线一致；完整层hook只在profile开启。单位／异步关联测试保留。初版离线reader漏收Node入口、一次cache版本交接失败都已修复并重算；不属于模型请求失败，原记录保留在`ANALYSIS_REVISIONS.md`。
- 初始本地监督进程退出后，按实际容器ID／owner和存活模型进程，从pure已启动阶段续接，未重新运行前三组或重启该模型；监督与解析改为独立会话。采样记录未丢失或重放。
- 原P r05／D r03四节点ID、image、owner、启动脚本及每节点10文件源码manifest恢复审计PASS；四引擎健康200且空闲，P0/P1新单token数学请求均返回4；proxy保持原未运行状态。r18停止退役，D与Master未改，原源码分支／HEAD／732文件摘要不变。

主要证据位于[本地实验目录](../artifacts/p-main-buffer-reuse/perf-layer-overlap-32k-64k/)：`timing-summary.json`、`requests-*.jsonl`、`latency-budget.json`、`real-layers-*.csv`、`timeline-*.csv`、`transfers-*.csv`、原始／导出CANN、host archives及`completion-audit.json`。目录被gitignore；正文已包含主要结果，推送正文不会自动推送原始trace。CSV同时保留CPU demand、GET实际提交／完成、ready发布、设备Prolog／Indexer／SFA开始时刻；观察到的消费时刻已含等待，不能当作无等待反事实deadline。

![每层计算与恢复时延](../artifacts/p-main-buffer-reuse/perf-layer-overlap-32k-64k/per-layer-64k.svg)
