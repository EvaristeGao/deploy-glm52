# mc.pdf 与当前 P Main buffer reuse 的对照审查

日期：2026-11-03。直接依据根目录 `mc.pdf`，不是将后续派生设计文档当作原文。
源码：`src/vllm-ascend-reuse`，HEAD `f9b995c6887ae7062cc40fb9719b8b122deb5c0f`。
本轮未修改生产实现或集群状态；重新运行111项聚焦CPU测试，全部通过。

## 结论与范围

P独立、正常逐层执行的 Main-only reuse + Store 保存／恢复主干与PDF一致，已有1M prefill＋首token硬件证据。
不能将此结论扩大为PDF全部设计已交付：§5.5/§5.8的A/B代次适配、无计算hook恢复后发送及共同集成尚未闭环。
新P/D connector由同事负责，本文只指出P侧需要提供和共同确认的边界，不替代其实现。

PDF共19页。本轮已实际渲染并查看第2–7页：逻辑图、开发图、进程时序图、部署图及跨两页的场景流程图。
尤其第4页区分attention、Store与P/D三个并行读者，第6页要求旧代屏障→新代打开→恢复→登记封口→释放，
不能把图上的控制箭头当作KV数据流，或将Store对象池与D Host pool混为一谈。
图像及原文提取位于`artifacts/p-main-buffer-reuse/mc-pdf-pages/`和`mc-original-review.txt`。

## 原文要求与代码对应

以下源码路径相对`src/vllm-ascend-reuse/`。Store公共前缀为
`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/`。

| PDF要求 | 当前实现位置与行为 | 审查结论 |
|---|---|---|
| §3：显式共享槽数才启用，不另加同义reuse布尔值 | `backend/mooncake_layerwise.py::extract_layout_config`要求`use_layerwise`和显式`layerwise_num_shared_buffers` | 一致 |
| §3：只共享Main，Indexer独立；独立层另占槽 | `layerwise_cache_layout.py::build_main_only_reuse_layout/build_main_only_kv_cache_plan`按spec类型识别组件，Main按槽生成别名，Indexer独立；当前B2＋首尾独立=Main4槽 | 一致；不能将B2解释为全部KV只有2份 |
| §3：runner与Store使用同一布局；不能只按页大小猜组件 | 后端`build_cache_plan`被规划入口与Store消费；校验Main/Indexer spec、dtype、page、stride与原生Indexer别名 | 一致；当前仅单manager group，兼容布局范围更窄且显式拒绝 |
| §4.1：不能把KV-ready当作attention-done | `attention_fence.py::AttentionComputeCompletion`；`pool_worker.py::save_kv_layer`在post-attention记录独立NPU event | 一致；先等host record发布，再同步event，不以Python返回代替设备完成 |
| §4.2：Store源读取结束后才覆盖 | `kv_transfer.py::_handle_range_layer_task`等待旧层save结束，检查发送失败并同步设备事件，然后才GET | 一致；底层返回语义证据和sentinel见既有audit §5.3 |
| §4.3：覆盖前还须等待外部P/D读取 | Store receiver调用`external_slot_release_waiter`；`ascend_multi_connector.py`复合调用provider的`wait_for_layer_reuse` | P侧等待入口和注册一致；P独立1M无真实外部读者，不能据此验收新connector |
| §4.4–5：GET/屏障失败不得执行attention，空GET也过屏障 | `_submit_ready_layer_loads`对Main reuse即使空任务也提交；`wait_for_layer_load`等待完成后检查fatal，range receiver在finally唤醒但先保留错误 | 一致；event置位不被单独解释成成功 |
| §4：source_read_done与snapshot_committed分开 | 每层range PUT完成通知用于同step轮换；最后相关层执行commit；`session_tracker.commit_put_keys`仅提升成功提交key | 一致，不等当前整对象提交才轮换，避免B1循环等待 |
| §5：早／晚注册均有效，无法接线须拒绝 | `set_external_slot_release_waiter`更新已启动receiver；`assert_reuse_wired`检查worker与receiver实际安装同一waiter | 一致，但只验证接线，不验证provider内部epoch语义 |
| §5.5：SlotEpoch、open/reserve/seal/complete/wait | 本地compute用每次执行的新completion对象和跨step槽尾；Store用固定计划、有序队列及完成事件；未提供完整跨A/B的epoch/view适配器 | 部分对应，不能宣称完整等价；见下节 |
| §5.8：无计算hook也恢复并逐层发送 | 当前恢复提交、compute completion和层号推进仍依赖`wait_for_layer_load/save_kv_layer`；layerwise `start_load_kv`自身不推进恢复循环 | 通用无hook路径未实现／验收，不等于已有prefix命中通过 |
| §6：本地prefix命中不能信任共享HBM内容 | scheduler传递完整恢复范围；worker为共享Main从Store恢复，Store miss/短命中/缺LoadSpec/查询后淘汰显式失败 | 一致；独立Indexer可保留自身有效范围优化 |
| §6.1：请求实例、快照代次、有效范围和提交状态 | `metadata.py`的request_instance/snapshot_generation；worker私有snapshot/full keys；tracker pending owners→commit后load entries，失败不提升 | 一致；以现有集合和提交路径表达状态，不强制同名枚举 |
| §6.1：旧版本仍有GET／后续恢复依赖时不得删除 | `session_tracker.py`跟踪pending、load owners和retired keys；`_cleanup_main_private_snapshots`在传输静止后回收；删除失败留重试记录 | 对应；断连后继续服务不代表所有取消故障已验收 |
| §6：TP非保存rank也保护计算和通信 | receiver同步本地设备event；step入口处理前轮尾部保存并TP barrier；非保存rank显式选择已提交旧版本及partial→full来源 | 对应；step barrier是现有实现同步点，不宣称无额外同步开销 |

## 三个需要明确保留的差异／缺口

### 1. 本地事件不能替代完整的A/B SlotEpoch契约

PDF §5.5明确允许复用有序队列，不强制新增同名方法。因此Store没有额外Reader计数器本身不是偏离设计：
正常PUT在有序发送线程中完成源读取，再通知接收者，现有机制可表达该部分依赖。

但是，PDF同时要求在任何GET/scatter写新代之前通知B打开epoch，OPEN不能当作空闲，完整batch计划确定后封口；
不同请求／组件／范围、迟到回调、失败但源尚未静止必须正确区分。
当前本地`AttentionComputeCompletion`保护的是计算代次；`snapshot_generation`保护的是Store版本，二者都不是P/D的SlotEpoch。
全源码未发现完整的`SlotEpoch/get_current_epoch/begin_slot_write/open_epoch/seal_epoch/ReadTicket/source_quiesced`
适配模型；也没有证据证明旧provider的按层pending/event已经覆盖PDF的全部等价语义。

所以新connector接入前，P侧需与同事确认真实槽位／视图身份、旧代等待与新代打开边界，落地等价接口与契约测试。
`assert_reuse_wired()`只能证明回调存在和实际安装，不能证明它不会早返回。

### 2. 无GET与无计算hook是两个不同场景

空GET屏障已实现并有受控测试。prefix命中但仍执行模型层的场景，也已有硬件恢复验证。
PDF §5.8还要求：完全跳过计算的层，依然恢复历史KV，提供ready fence，供B逐层发送，且明确没有attention读者。
当前completion的正常record来自post-attention hook；没有通用的无hook恢复／推进／发送驱动。
这条在P独立首token路径没有被要求触发，但对后续完整prefix命中的P/D集成必须补齐或证明调度等价路径。

### 3. 支持矩阵和验收证据比PDF完整交付范围窄

当前实际复用限制P producer、V1、eager、PP=CP/DCP=1、无MTP/speculation、单manager group及受支持的连续page布局。
DCP限制与当前特性边界一致。更一般的page/manager组合和图执行不在当前通过声明内。

本轮重新通过111项CPU聚焦测试，原始日志：
`artifacts/p-main-buffer-reuse/ut-20261103-193915-238429.log`。
其中覆盖可控外部waiter、空GET、attention延迟、失败传播、跨step compute对象、三代partial、ID重用、commit失败、清理重试等。
这些测试没有覆盖完整的OPEN未封口／多ticket P/D状态机，也不证明原生网络传输卡住后的资源安全退出。

真实NPU证据包括既有B1/2/3 sentinel及短上下文并发／重复前缀／断连后恢复、当前1M冷prefill和同前缀检索。
仍缺PDF §8要求的完整新connector慢读者／取消／双路径故障联调、无计算hook发送、固定容量staging与峰值内存分项、
设备时间线及屏障等待开销；持续本地decode亦未验收且有历史失败记录。不能将这些记为PDF整体验收通过。

## 后续对接的最小检查点

1. A提供实际Main/Indexer视图、槽身份及写入边界；与B共同固定epoch打开／封口的等价适配契约。
2. 注入OPEN未封口、部分ticket完成、迟到旧代callback和失败但源仍在读，证明Store GET和模型写入都不越过屏障。
3. 增加无计算hook的恢复后发送驱动／等价路径及测试，避免等待永远不会record的attention事件。
4. 再接同事的新connector，覆盖空GET、prefix、partial、多请求、慢外部读取与安全退出；P独立1M证据作为其中一项保留。

当前实现没有走错到另一种buffer复用方案；但“P独立1M已通过”与“PDF中reuse相关的所有跨模块契约均已实现”是不同结论。
