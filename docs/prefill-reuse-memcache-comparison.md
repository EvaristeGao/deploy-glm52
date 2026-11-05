# P侧memcache offloading与Mooncake Main reuse性能差异核对

> **2026-11-05：32K／64K逐层实测完成。** B4/P3相对相同INT8／DCP1无transfer参考，额外prefill开销约0.15%／0.51%；64K层间77层GET直接Free合计约0.39ms，剩余主要是第0层约69ms启动恢复。关闭offload后SFA仍约12.46s／64K，计算路径才是大头。48请求全通过，原服务恢复审计PASS，源码原分支不变。详见[完整层时延与优化预算](prefill-reuse-layer-overlap-32k-64k.md)。下方保留早期128K／不同配置口径，不能混用样本或归因。

> 后续固定B2边界实测已完成：native GET为主要直接暴露，独立Indexer仍被一起恢复；GVA与block-key还存在驻留过滤差异。完整时序、分段耗时与安全优化条件见[边界实测结果](prefill-reuse-boundary-trace-results.md)。下方六组性能数据保留原口径。

日期：2026-11-04。状态：代码与官方参考已核对，6组32K/64K/128K计时完成；用户指出应先做关键路径分析，后续参数扫描已中止；既有B2/P1 profile已解析、阶段归因完成，原服务恢复审计PASS。用户明确跳过本轮1M请求回归。当前没有memcache实机对照结果，也没有证明任一后端在当前集群无性能损失。

## 用户给出的官方参考

- [Layerwise and Sparse KV Cache Offloading Guide](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/layerwise_and_sparse_kv_cache_offloading.html)：P侧示例为memcache、eager、B3、independent[0]；A3配置选择device_sdma。该页没有给出同模型冷prefill延时表。
- [对应设计](https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/layerwise_and_sparse_kv_cache_offloading.html)：P侧逐层搬运并与计算重叠；D侧按Top-K miss回读，是不同优化。文档里的fused LIM／Copy-SFA不能用于解释P-only首token耗时。
- [RFC #48203](https://github.com/vllm-project/vllm/issues/48203)：作者报告2–4个buffer可掩盖大部分加载时间。这支持优先检查重叠是否有效，不能视为当前RoCE对象池、全部输入长度的无损性能保证。

## 相同与不同

当前源码固定`717f6e6e0`，memcache后端入口为`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/memcache_backend.py`。它并非完全不同的模型执行器：和Mooncake共用`pool_worker.py`、`kv_transfer.py`的逐层任务、收发线程、事件等待和批次控制。

| 项目 | memcache参考／本地代码 | 当前Mooncake B2/P1 |
| --- | --- | --- |
| 数据面 | GVA，按全局地址+本地地址+长度调用native batch_copy | block-key session/range，传keys+buffer/size/offset |
| 参数来源 | 指南B3、仅第0层独立，未显式指定prefetch／批次 | B2、首尾独立、prefetch1、64 blocks、16MiB |
| 当前源码解释默认值 | prefetch=min(B,8)=3；两个transfer限制默认0（不额外拆批） | 均显式覆盖为上述数值 |
| Main物理槽 | 当前同质布局下1+3=4 | 2+2=4 |
| 数据移动条件 | 指南A3使用device_sdma；实际落点／远端行为需SDK与运行配置确认 | 本轮固定RoCE；不与SDMA条件视作相同 |
| 对象管理 | step内解析／分配GVA，copy阶段直接使用地址；有lease和write_finish | start/get/put sessions、range、commit/end，含private snapshot与生命周期管理 |
| 计算并行 | 指南只有增量参数，未提供完整P计算并行配置，不能假定与我们相同 | DCP1、enable_dsa_cp=false；原始性能基线开启DSA-CP |
| 布局 | 通用planner与Main-only不同，应核对具体Indexer descriptor，不能仅比Main槽数 | 仅Main复用，21个实际Indexer独立 |

代码依据：`layerwise_cache_layout.py::build_layerwise_cache_layout`决定默认预取；`pool_worker.py`第301–303行默认不限制传输批次；`kv_transfer.py::_batch_copy_with_limits`直接调用memcache native `store.batch_copy`，Mooncake则经`_handle_range_request`及`backend/mooncake_backend.py::batch_copy_get/put`调用session range API。两者都有保存完成等待，不能说memcache“靠不等待读者”获得性能。

Mooncake session API的设计是会话开始时解析对象元数据，range阶段复用已缓存的信息；不应声称每个层次range调用都发Master查询。[Mooncake session/range API说明](https://github.com/kvcache-ai/Mooncake/blob/main/docs/source/api-reference/python/mooncake-store.md)。当前安装wheel内部调度／GIL释放及具体耗时未用profile证实，不把这些猜测当作已定位根因。

补充核对：当前Mooncake配置已经启用`preferred_segment=true`和`prefer_alloc_in_same_node=true`，不能把“打开同节点优先”当成尚未做的优化；实际对象落点和传输协议仍需区分。上游当前[store_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/main/mooncake-integration/store/store_py.cpp)的两个range接口均显式释放GIL，因此也不能凭“Python收发线程”断言native搬运期间必然阻塞模型线程。下载源码与Git blob摘要保存在upstream-code-manifest.json，但尚未证明它与当前安装wheel是同一个commit；实际热点仍以profile为准。

## 可以直接借鉴的部分

最明确的差别是：相同4个Main物理槽，B3／只留首层独立比B2／首尾独立拥有更大的共享环；默认prefetch3比1更积极；默认不拆批比64-block拆批减少逐层native调用，并能进入`_range_transfer_batches`的不限额fast path。是否转化为实际收益，取决于传输延时、collective竞争和生命周期屏障，需要实测。

因此追加`b3p3-guide`：Main B3/P3、independent[0]、transfer_blocks=0/bytes=0，其他项与本轮Mooncake组相同。仍走Mooncake，不启动memcache MetaService，不改同事PD connector，也不把它称为memcache性能测试。该组合同时改多个参数，只用于参考组合比较；B4/P2的64／256批次对照用于单独量化批次影响。

初步pure／private结果已经表明，32K的绝大多数耗时存在于当前DCP1计算路径，普通逐层Store的额外耗时很小。128K的B2初值比pure多约10.5秒，值得继续优化恢复／重叠。这个观测不能把计算配置差距全部归给DCP，Main精度、chunk、MoE预算等仍有差别；具体Indexer/SFA热点等待独立profile。最终成对数值见收尾结果。

## 本轮实测范围与状态

原始配置身份与每节点10文件manifest均已只读核验，原P r05保留，D r03和Master保持。r16只复制当前源码到自有候选容器，source manifest覆盖732文件；未改生产源码或生效inventory。

统一TP16×DP2、DCP1、eager、Main/Indexer INT8、无MTP、chunk32784、seq1、MoE524544、模型上限262144。统一模型上限是为让全独立Main对照容纳128K；本轮不验收1M能力。

- pure：完全移除KV transfer配置，作为当前DCP1计算路径参考。
- private：逐层Store、Main全独立，检查共同Store路径代价。
- b2p1／b4p2／b8p4：固定批次64，改变共享环／预取。
- b4p2-batch256：与b4p2只差传输批次。
- b3p3-guide：上述参考组合，测量过程中按用户要求中止；部分请求原样保留，未作为完整对照。
- b3p3-guide-bf16：计划只改Main dtype=auto（BF16）、Indexer仍INT8；按用户要求未启动，没有实测结论。

这里单独测Main dtype有代码依据：`device/device_op.py::execute_sparse_flash_attention_process`按实际KV dtype选择量化Sparse Flash Attention或BF16 Sparse Flash Attention；`attention/sfa_v1.py::_sfa_preprocess_prolog_v3`的cache量化模式也随之变化。因此Main INT8既影响搬运字节数，也改变计算路径，不能先验假定它一定更快。官方指南明确要求的是D侧稀疏offload使用BF16 Main；本轮BF16组是P侧受控消融，不把D侧限制扩大解释成P侧强制要求。

32K／64K／128K，每长度1次预热+3次计时，沿用上轮相同case0–3，max_tokens=1，核对实际tokens、零prefix hit、无抢占、答案和请求数量。失败保留；不展开先前暂停的64K数值根因定位。所有计时仅为P-only，不是新PD数据面性能。

原始配置、生成脚本、源码快照、逐请求记录、结果和恢复审计位于[perf-tuning-32k-128k](../artifacts/p-main-buffer-reuse/perf-tuning-32k-128k/)。自动汇总见该目录`RESULTS.md`／`summary.json`。原环境恢复审计PASS；不能把空白行当作通过。B2另行profile了128K case4，其耗时不混入性能表；已离线导出并分析，见[关键路径分析](prefill-reuse-critical-path-analysis.md)。参考组没有继续profile，BF16没有启动。`pipeline-failure.json`保留了主动终止本地测量SSH的传播记录，对应`local-measurement-cancelled.json`，不把它误报为NPU／模型崩溃。
## 本轮收尾实测结果

下面为server prefill中位数，括号为正式样本min–max，单位秒。每个单元格3次正式计时；原始请求和全部校验均保留。

| 配置 | 32K | 64K | 128K | 正式样本正确／有效 |
| --- | ---: | ---: | ---: | --- |
| pure | 9.576 (9.573–9.583) | 20.337 (20.315–20.339) | 45.458 (45.425–45.466) | 9/9；9/9 |
| private | 9.601 (9.586–9.602) | 20.367 (20.334–20.375) | 45.806 (45.797–45.827) | 9/9；9/9 |
| b2p1 | 9.588 (9.583–9.597) | 20.790 (20.776–21.081) | 55.958 (55.932–56.440) | 9/9；9/9 |
| b4p2 | 9.584 (9.583–9.586) | 20.458 (20.449–20.663) | 53.517 (53.443–53.739) | 9/9；9/9 |
| b8p4 | 9.592 (9.584–9.600) | 20.456 (20.434–20.459) | 51.981 (51.724–52.510) | 9/9；9/9 |
| b4p2-batch256 | 9.580 (9.580–9.585) | 20.424 (20.415–20.430) | 52.907 (52.622–53.142) | 9/9；9/9 |

客户端首内容TTFT中位数（秒），单并发、输出1 token：

| 配置 | 32K | 64K | 128K |
| --- | ---: | ---: | ---: |
| pure | 9.697 | 20.465 | 45.582 |
| private | 9.720 | 20.489 | 45.935 |
| b2p1 | 9.719 | 20.918 | 56.092 |
| b4p2 | 9.709 | 20.577 | 53.641 |
| b8p4 | 9.721 | 20.587 | 52.105 |
| b4p2-batch256 | 9.706 | 20.551 | 53.036 |

相对本轮B2/P1的成对prefill变化（负数表示更快；只纳入双方均有效的同case）：

| 配置 | 32K | 64K | 128K |
| --- | ---: | ---: | ---: |
| pure | -0.13% | -2.18% | -18.76% |
| private | +0.14% | -2.04% | -18.14% |
| b2p1 | +0.00% | +0.00% | +0.00% |
| b4p2 | -0.04% | -1.60% | -4.36% |
| b8p4 | +0.04% | -1.61% | -7.11% |
| b4p2-batch256 | -0.08% | -1.76% | -5.45% |

所有正式样本均按固定prompt摘要、实际tokens、computed tokens、零local/external prefix hit、无抢占、单请求和数字检索答案校验；详细checks见原始JSONL。数字检索通过只覆盖本轮样本，不等于完整模型精度评估。

本轮只测P-only首token，没有做1M请求或新PD connector端到端测试。6组顺序执行、每长度3次正式样本，未随机交错；很小的差异应视为噪声范围，不能外推并发吞吐或长期稳定性。用户要求先完成关键路径分析后，中止参考组合的请求循环，部分记录保留但不作为完整调优组；BF16组未启动。

原P r05／D r03的ID、image、owner、脚本及每节点10文件源码manifest恢复审计PASS；四引擎健康200且空闲，P0/P1新单token数学请求均返回4，proxy保持原未运行状态。r16已停止退役，D和Master未修改。源码仍为717f6e6e0／p-main-buffer-reuse，本轮没有生产源码变更或push。
