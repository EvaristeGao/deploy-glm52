# 固定B2/P1：GET暴露与memcache路径差异

> **2026-11-05：32K／64K逐层实测完成。** B4/P3相对相同INT8／DCP1无transfer参考，额外prefill开销约0.15%／0.51%；64K层间77层GET直接Free合计约0.39ms，剩余主要是第0层约69ms启动恢复。关闭offload后SFA仍约12.46s／64K，计算路径才是大头。48请求全通过，原服务恢复审计PASS，源码原分支不变。详见[完整层时延与优化预算](prefill-reuse-layer-overlap-32k-64k.md)。下方保留早期128K／不同配置口径，不能混用样本或归因。

日期：2026-11-04。状态：边界探针、128K实测、同次TP0 CANN归因和原服务恢复审计完成。没有参数扫描、生产实现修改、1M回归或新PD数据面验收。

## 结论与优化优先级

**本次10.361秒CANN Free中，9.553秒直接落在主线程等待native range GET的区间。** FIFO排队、旧PUT／compute／reader等待及Python元数据构造的直接暴露很小。

进一步发现：**Indexer虽然在当前Main-only布局中独立驻留，恢复范围仍包含Indexer data／scale。带Indexer的21层贡献了7.402秒GET暴露。** 这7.402秒包含这些层的Main传输，不等于全部可节省时间；但比“先把元数据移到gate前”更值得优先验证。

下一项有明确依据的开发是：在同一请求上一chunk的独立组件确实仍驻留、块映射未变时，只恢复共享Main；外部prefix hit、新请求或驻留无法证明时完整加载。先补驻留与块映射证明，再做受控实现／回归，不直接关闭GET或删同步。

## 冻结配置、探针及验证

- 基线源码`717f6e6e0`／`p-main-buffer-reuse`，原732个Python文件摘要与上一轮一致。r17使用独立诊断overlay，共733文件；原源码工作区保持干净。
- TP16×DP2、DCP1、eager、Main/Indexer INT8、B2/P1、首尾独立、chunk32784、batch64／16MiB、seq1、模型上限262144。沿用原P镜像与native扩展，新私有模型别名隔离Store key空间。
- 在KVPoolWorker／KVTransferThread记录host墙钟与线程CPU时间；不新增NPU同步。请求内只缓冲，下一条关闭探针的短请求触发统一导出。
- 传输文件剥离包装后AST与基线一致；worker唯一额外差异是CANN只采worker rank0，profile方法无collective。6项CPU行为测试通过，区间减法／交集用1000组离散时间oracle验证通过。
- 单并发P0直连、max_tokens=1；预热、对照、profile及flush共12请求全部校验通过。每次核对实际／computed tokens、答案、零local/external prefix hit、无抢占和单请求计数。

| 128K同prompt对照 | 关闭探针prefill（秒） | 开启探针prefill（秒） |
| --- | ---: | ---: |
| A | 56.714 | 56.551 |
| B | 56.867 | 56.195 |

两对数据未显示明显额外时延，样本不足以作严格开销上限保证。32K开启探针9.586秒；首次32K冷预热19.860秒单列，不用于正式比较。独立profile请求实际131065 tokens，prefill56.736秒、首token9正确，不混入对照。

P0的16个TP rank各有on128-a、on128-b、on32、profile128四份记录，共64文件，无丢记录。P1没有该请求的KV任务，轻量档案为空，不冒称采集了其负载。TP0的312个main load-ready wait全部落在对应CANN `vllm::mla_forward`内（校验容差2µs）；所有记录的wall/monotonic锚点最大误差界约7.2µs。

本次包含312次load-ready wait、234个有历史GET的range层任务和2106次native GET。首chunk无GET，走空task/GVA处理分支；该分支的内部gate没有细分探针，主线程总等待仍有记录。请求snapshot_generation及slot map已保存；当前before_slot_write/source-registry callback未启用，不能把其未出现误解释为“零成本完成了SlotEpoch协议”。

## 同次时间线的互斥归因

以下只统计TP0、同一个profile请求。各receiver阶段先做互斥区间划分，再与主线程load-ready wait及CANN Free求交，避免父子span和并发线程重复累加。

| 主线程等待时receiver所在阶段 | 与CANN Free相交（秒） |
| --- | ---: |
| native GET wrapper内 | **9.552656** |
| range内其他Python／结果处理 | 0.052921 |
| ready发布到主线程恢复 | 0.020137 |
| attention gate | 0.001533 |
| 地址构造 | 0.000464 |
| 旧save | 0.000478 |
| 旧compute | 0.000000 |
| 外部读者释放 | 0.000003 |
| FIFO排队 | 0.000000 |
| 其他／未细分 | 约0.002621 |
| **主线程load-ready wait内总计** | **9.630813** |

完整CANN Free为10.360773秒，native GET约占92.2%；在load-ready wait已解释的Free内占99.2%。剩余约0.73秒不在该等待span内，不能强行划给GET。Free仍是CANN计算／通信分类，不能直接等同物理网络或DMA空闲。

本次与上一轮57.924秒profile是不同请求。上一轮的“11.229秒在Prolog前”与本次的“9.553秒在native GET wrapper内”统计边界也不同，不能混用分母。

## 恢复时间为何随历史增长

| 当前chunk | TP0 GET传入字节（GB，十进制） | native GET累计墙钟（秒） | 地址构造（秒） | 拆分range（秒） |
| --- | ---: | ---: | ---: | ---: |
| 2 | 1.794 | 4.966 | 0.058 | 0.036 |
| 3 | 3.581 | 9.918 | 0.104 | 0.072 |
| 4 | 5.367 | 13.731 | 0.156 | 0.113 |

native调用累计不能当作净损失。其中包含已被计算掩盖的时间；调用线程自身CPU时间合计约1.51秒，也不能代表native线程池的总CPU成本。

当前B2/P1下，后续层GET通常在前一层attention开始后约1–3ms才启动，说明可重叠窗口受到attention gate和lookahead限制。SFA kernel平均约80.09ms。排除每chunk无前驱的第0层，逐层GET的中位数为47.4／95.8／145.7ms；第3、4个chunk分别在前一层attention结束后还剩约18.2／69.5ms。实际还有输出投影、FFN等重叠工作，这不是可优化时延的直接预测，但能解释后续chunk为何越来越难掩盖。

因此“GET区间暴露”有两种可能的优化杠杆：缩短native完成延迟，或在保持槽位／通信顺序安全的前提下扩大可用重叠窗口。它不独立证明物理带宽不足。

## Indexer多段恢复是更具体的候选

Store登记和传入range的组成一致：57层只有Main一段；5层为Main＋BF16 Indexer两段；16层为Main＋INT8 Indexer data＋scale三段。三段层为6、10、14、…、66；两段层为0、1、2、70、74。

| 第4 chunk的层类型 | 层数 | 平均传入字节／层（MB） | 平均native GET／层（ms） |
| --- | ---: | ---: | ---: |
| 单Main段 | 57 | 64.571 | 129.2 |
| Main＋Indexer两段 | 5 | 89.770 | 144.6 |
| Main＋Indexer data／scale三段 | 16 | 77.368 | **352.7** |

带Indexer的21层在三个历史chunk共贡献7.402秒GET暴露，约占全部GET暴露的77.5%。其中三段16层贡献约6.618秒。层类型、payload和底层资源竞争同时变化，这不是独立的“每增加一段必多多少ms”实验；不过较小payload的三段组明显更慢，值得重点查小段及native range处理。

只按这次range描述符计数，过滤所有已证明驻留的Indexer组件会使每个block的segment数由115降至78（约−32.2%），传入字节约减少6.16%。这是未来实现的预期计数变化，不是实测加速比。

## 与memcache/GVA的源码差异

| 项目 | 当前源码memcache/GVA | 当前Mooncake/block-key |
| --- | --- | --- |
| 地址准备 | 旧槽依赖后、attention gate前构造 | attention gate／external reader之后构造 |
| 提交 | 聚合本层各group地址后batch_copy，仍可能拆批 | 按group发range GET，并有session／commit协议 |
| 独立层驻留过滤 | 用independent_layers和vllm_cached_tokens选择load起点，跳过驻留前缀 | `_process_load_for_layer_batch`的block-key分支按load_block_keys构造范围后直接continue，不经过该GVA过滤 |
| 组件 | 通用reuse布局，Indexer不保证独立驻留 | Main-only布局的Indexer独立，但build_addrs仍枚举登记组件生成恢复range |
| 原生接口 | GVA/address/size的batch_copy | keys/buffers/sizes/offsets的session range接口 |

GVA的整层independent优化不能直接视作“memcache已经对所有Indexer做过驻留过滤”；其通用布局可能也复用Indexer。当前应补的是我们Main-only布局对应的组件驻留语义。

代码入口：`pool_worker.py::_process_load_for_layer_batch`（约1861行）、`pool_scheduler.py`中上一step LoadSpec构造（约955行）、`kv_transfer.py::LayerBatchBuilder.build_addrs`（约465行）以及receiver两条分支（约2132／2220行）。scheduler的previous-step LoadSpec明确携带`layerwise_previous_step=True`和`vllm_cached_tokens=num_current_tokens`，可以作为后续判定输入，但仍需证明相应物理块映射／所有权保持。

元数据顺序差异确实存在，但本次地址构造直接暴露不足1ms，不能把前移地址构造当作解决约10秒差距的主方案。FIFO也是两边共性，本次有历史GET任务没有观察到暴露在FIFO排队内的大头。

## 后续应改什么、再查什么

**理论掩盖与直接归因须分开。** 重新核对原`mc.pdf`的第4页§2.4时序图、第7页§3和第8页§4：文档要求共享槽位旧占用者的计算／Store源读／P-D源读结束后才能写新代，允许同层attention、Store PUT和P-D读取并行；它没有规定未来层GET必须等“当前层Indexer之后、SFA开始”才启动，也没有给出保证全掩盖的带宽／时延预算。B3/P1是配置示例，不是性能最优值。三槽A/B/C在层1读者结束后，可按满足安全依赖的调度在层2/3计算期间向A预取层4；这是可行调度的推导，不是PDF已经实现的多层流水图。

因此不能拿当前约80ms SFA窗口作为设计的理论上限。gate在GPU忙时关闭、错过可预取时间，最后损失可能表现为GET等待；本次gate直接Free很小不排除这种机会损失。9.553秒落在native GET也不证明只能通过提高网络速度来解决。应进一步对齐旧槽最早安全释放、实际GET提交及消费需求的时刻，区分迟发与服务率不足；提前DMA仍需验证原生通信／collective约束。稳态全掩盖需要搬运服务率、有效提前量和有限槽位同时满足，首尾填充／排空另计；再多buffer也不能弥补长期服务率低于计算消耗率。

1. **优先验证previous-step的组件恢复过滤。** 记录／验证旧新block map和request instance，独立Indexer及独立Main前缀只有在驻留证明成立时才跳过；外部prefix、新请求、映射变化和恢复不确定都回退完整GET。保留Store PUT、key/object格式和原始remote offsets，仅选择实际需复制的range组件；不要通过删除登记项重排对象布局。
2. **多段native路径。** 若过滤后仍有大头，分 native job排队、提交、HIXL完成和Python返回。若数据／scale满足真实地址连续、remote offset连续且同一注册范围，再评估range合并；不能只靠地址相邻跨注册边界合并。
3. **重叠窗口。** 用这次约80ms attention窗口与后续GET时长预测需要的lookahead，再验证具体方案。移动DMA gate必须保留已有collective顺序及slot读者约束；继续堆B不是默认答案。
4. **TP读流量。** 本次仅TP0 PUT约7.168GB；16个TP各自GET约10.742GB、累计约171.865GB，native调用墙钟约27.4–28.7秒。需查实际replica/segment落点与HIXL内部完成链路。一次GET后TP分发属于可另行设计验证的结构方案，会新增collective／布局／生命周期约束，不是当前已验证优化。

native wrapper包含原生执行／队列／完成等待及返回调度，不能把其中9.553秒称作RoCE线速传输时间。366个GET调用区间内（离边界超过1ms）出现其他Python线程标记，排除了“所有GET全程一直占着GIL”的假设；不排除某些内部阶段或返回时的GIL竞争。当前没有memcache同条件实机延时，未给它与Mooncake分配性能差距占比。

## 证据与恢复

目录：[prefill-boundary-trace](../artifacts/p-main-buffer-reuse/prefill-boundary-trace/)。`probe-manifest.json`、`source-manifest.json`、`ut-controller.log`、`interval-ut.log`保留代码和CPU验证。`executed-cases.json`／`requests-*.jsonl`保留全部12请求。`boundary-traces-p0.tar.gz`为64份各TP记录，`profiler-raw-rank0-b2p1.tar.gz`及`profiler-selected-b2p1-p0.tar.gz`保留原始和导出CANN。

`boundary-summary.json`／`boundary-integrity-audit.json`保留互斥归因与312个主线程span对齐验证；`overlap-window-summary.json`、`segment-count-analysis.json`、`component-group-exposure.json`、`all-rank-transfer-volumes.json`和`python-progress-during-get.json`保留进一步拆分。

原P r05／D r03四节点ID、image、owner、启动脚本及每节点10文件源码manifest恢复审计PASS；四引擎健康200且空闲，P0/P1新单token数学请求均返回4；proxy保持原未运行状态，r17停止退役。D和Master未改，源码仍在原分支且干净。
