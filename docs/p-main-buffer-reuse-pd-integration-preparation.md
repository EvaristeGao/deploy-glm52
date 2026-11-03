# 为新 P/D connector 准备的 P reuse 接口与验证

日期：2026-11-03。基于mc.pdf §3–6、§7.4、§8以及当前源码HEAD `f9b995c68`。
这是分析和拟实施计划，不表示下列新接口已经存在。本轮不修改生产代码、集群状态或同事的connector。
设计差异的证据见[PDF对照审查](p-main-buffer-reuse-mc-pdf-review.md)。

## 目标与已有基础

让同事的新connector可以在不理解Store私有key/session内部实现的前提下，安全读取P共享Main和独立Indexer，
并明确反馈何时不再读取源。当前P独立1M的已有实现保留作为回归基线。

已有：Main-only统一规划、实际KV分配与层名、attention-done事件、逐层PUT完成与提交区分、
完整Main前缀恢复、partial版本、外部waiter注册与receiver覆盖前调用、启动接线检查。
已有MultiConnector的KV-written/save hooks先调用provider再调用Store，仍可复用。
这些基础不等于已经提供了完整的SlotEpoch和传输计划接口。

## 1. 实际源视图：先给对方能用的描述

我们提供从规划器和实际分配构造的只读适配视图，不让connector按逻辑层号取模或group编号猜Main/Indexer。
视图至少包含：

- worker实例、逻辑层、MAIN/INDEXER组件、group、allocation身份／代次、物理slot、base地址和容量。
- dtype、shape、字节stride、manager block大小、组件物理page大小及合法访问边界；Indexer量化scale也需明确。
- 请求实例、明确的有效token区间、当前请求实际block映射；组件分别表达，不假定block ID相同。
- 本层没有Indexer时明确为空，不把其他层的Indexer描述冒充本层组件。

Main的slot来自`MainOnlyCachePlan.layout.buffer_slots`并与实际地址核验；Indexer保留独立分配。
不重新分配一套完整Main或复制出全部层供connector读取。独立Indexer同样需要保护请求block生命周期。
本地事件、tensor与allocation引用仅留在worker，不直接塞入wire协议。

落点：`layerwise_cache_layout.py`已有plan、`pool_worker.py`的实际注册／布局状态和一个轻量适配器。
真实张量注册仍复用现有register入口；不要求新增同名框架或服务。

## 2. 槽位代次：补齐覆盖前与写入后的握手

我们拥有实际槽位的单一有序写入调度；B拥有P/D源读取登记和完成。需共同冻结以下语义，方法名可调整：

| 动作 | 主责 | 必须保证 |
|---|---|---|
| 查询当前槽与旧epoch | 我们提供 | 查询的是物理占用者，不是当前请求或Store快照版本 |
| 等待旧epoch | 我们的receiver调用B waiter | attention、Store及B等待全部成功后才允许新写入 |
| begin_slot_write / open_epoch | 我们推进并同步通知B | 在第一个GET/scatter之前打开；回调安装有效、不能默认为空闲 |
| reserve_read | B | 完整发送计划的全部请求／组件／范围先登记，之后才能把任务交给异步线程 |
| seal_epoch | B与协调层共同确定 | 完整batch计划确定后封口；首个请求不能替其他请求提前封口，零发送也封口 |
| complete_read / wait_epoch | B | 全部源读者安全结束且无失败后成功；迟到旧回调不释放新代 |

可用已有有序队列证明等价语义，不强制同名计数API；但必须有契约测试，不能仅凭event初始为set证明安全。
我们需将epoch关联到具体LayerLoadTask/执行视图；当前compute completion对象可继续承担计算依赖，
不能拿Store的snapshot_generation替代slot epoch或P/D transfer_generation。

打开epoch／登记计划回调应是线程安全的本地状态操作，不在receiver中同步等待D目标或网络，避免形成互等。
首次集成保留单一有序写者；不额外引入并发写者或读着同一槽时开始写下一代。

## 3. 数据就绪与范围：区分恢复完成、KV-ready、attention-done

我们向B提供“这一代、这一请求、这些组件范围已经可读”的本地ready依赖。

- restore_done：历史范围已恢复；后面仍可能要写当前chunk的新KV，不代表全部待发送范围已就绪。
- kv_ready：本次计划要读的范围已写好，发送可开始；用NPU event明确设备依赖。
- attention_done：本层计算不再读取Main，只参与覆盖保护；不能用kv_ready代替。
- source_read_done：B不再读源，参与覆盖保护；不等于D整个请求就绪。
- snapshot_committed：仅供后续Store恢复，不成为同step发送或槽位轮换的前置条件。

初版可在本层完整待发送范围就绪后统一放行，不要求实现多个区间的极致提前发送。
正常路径仍允许attention、Store PUT、P/D读取并行，不增加全设备synchronize。

范围必须显式说明：Store新保存范围、当前chunk新计算范围与D所需发送范围并非同一范围。
P本地prefix命中但D无缓存时，不能只发送新chunk而漏掉历史前缀。
我们提供每层有效源范围；B根据D明确required_ranges管理跨chunk累计覆盖、传输顺序和terminal。
初版沿用PDF的D不使用本地prefix省传，不扩大到未协商的目标重叠写入／重传优化。

## 4. 无计算hook：补受控的restore-only编排

当前空GET屏障已有，但完整无计算hook路径未闭环。拟提供restore-only驱动：

1. 用正常metadata准备历史恢复计划，不依赖模型forward发起逐层调度。
2. 对每层等待旧槽三方屏障、打开新epoch、恢复所需范围。
3. 恢复成功后提供视图与ready依赖，通知B准备／发送该层；明确本层没有attention读者。
4. 完整计划封口，按相同槽位规则推进下一层。既有compute completion不能等永远不会发生的record。
5. 失败沿用Store fatal/invalid-block闭环，不发成功ready；释放会话与保留数据依据分开处理。

restore-only不是重新跑attention，也不能直接把compute完成事件提前set来绕过恢复或传输保护。
恢复后的视图由我们提供；发送和D覆盖汇总仍由B负责。

## 5. 失败与取消：先停止新工作，后等待静止，最后回收

P侧需统一维护外部传输对源分配／请求blocks的引用；不能只因客户端断开或Store队列空就允许复用。
B需分别报告请求结果与源是否静止：FAILED且source_quiesced=false可以使等待者报错，但源仍被隔离。
后续确认静止只能释放引用，不能把失败变成成功。旧请求ID重用时使用新的request_instance。

正常shutdown采用drain并保留provider先退出的既有顺序；有底层取消能力则按其实际保证释放。
若同步传输卡住且无法取消，需要明确worker退出和资源销毁责任方，不能在线程仍读取时用event.set假装完成。
我们负责Store/计算一侧的引用与禁止新写入，B负责传输侧终结；D目标block retention由B对接D scheduler。
本轮P独立断连后服务正常，只能作为已有证据，不能替代在途P/D读取取消注入。

## 6. Admission与启动保护

- 复用同事connector，保留旧provider为已有回归路径，不建立另一个生产sender。
- 初始化协商能力；KV分配后检查实际views、槽位映射、epoch回调与Store receiver waiter均已安装。
- 保持同TP、无DCP、PP1、V1/eager、无MTP及当前受支持组件布局，启动失败应说明不兼容项。
- B/D先准备并发布完整目标；资源不足在P进入相关共享槽执行前背压或失败，不等P最后请求metadata才分配。
- Store不负责实现D目标发布，P侧提供最小admission门禁，避免在receiver等待网络目标造成环形依赖。
- 缺少真正接线时显式失败，不静默降级为普通blockwise；不把共享源交给one-shot、P完成后才读取的旧传输路径。

D Host allocation/runtime不是B需要另造的模块。我们可提供现有能力清单及接口说明；测试目标由B在独立阶段构造，
真实TargetLease/Host视图在共同集成阶段绑定现有manager与scheduler。当前需求不要求先完成全套D开发才让B开始。

## 7. 交付顺序和门槛

| 顺序 | 我们交付的具体产物 | B可独立继续的工作 |
|---|---|---|
| P0：契约先行 | 组件／槽位／请求身份／有效范围／ready视图适配骨架，epoch回调定义和成功／失败最小时序 | 用假Store和注册目标实现真实传输，不等我们完整模型路径 |
| P1：正常执行接线 | receiver覆盖前begin/open，KV-ready计划与封口协调，错误传播；保留现有三方等待 | 检查源ticket在发送前建立，多路径完成后释放 |
| P2：restore-only与取消 | 无hook逐层恢复驱动、源引用静止后释放、故障终结入口 | 测完整prefix命中、慢发送、取消与迟到通知 |
| P3：共同验收 | 真实模型/Store/新connector接线及内存和时间线 | 真实D Host/Indexer目标、双路径内容与D放行条件 |

所有新P侧接口先用模拟B reader进行确定性测试：

- OPEN未封口、多个ticket只完成一个、零payload未封口均不允许覆盖。
- attention/Store已完成但B仍读时，真实GET写入与模型写入都阻塞；空GET同样受保护。
- 旧epoch迟到／重复callback不能释放新代；失败且源未静止不允许回收，静止后只清理一次。
- normal和restore-only提供相同有效内容与ready语义；没有GET／没有attention两种分支分别覆盖。
- Main/Indexer stride和token范围正确，partial不读未初始化padding；多请求batch不漏计。
- 使用实际共享源buffer的sentinel测试，发送期间尝试覆盖必须被阻止，发送结束后才允许污染源。

最后再运行新connector的真实Mooncake/NPU联合测试、现有B1/2/3及P独立1M回归。
记录每阶段等待时间和额外内存，不用“接口已存在”或单次1M成功替代联合验收。

## 主要代码落点

- `kv_pool/ascend_store/layerwise_cache_layout.py`：复用布局和组件身份的唯一来源。
- `kv_pool/ascend_store/metadata.py`、`pool_scheduler.py`：请求身份、范围、normal/restore-only任务语义。
- `kv_pool/ascend_store/pool_worker.py`：视图适配、代次写入边界、恢复编排、ready与清理。
- `kv_pool/ascend_store/kv_transfer.py`：receiver写入前的实际等待／新代打开及错误闭环。
- `kv_pool/ascend_store/ascend_store_connector.py`：转发与生命周期入口，不复制worker的能力判断。
- `ascend_multi_connector.py`：与B共同接线，保留provider先登记再发布Store任务的顺序。

除特别说明，以上相对`src/vllm-ascend-reuse/vllm_ascend/distributed/kv_transfer/`。
B的sender内部、wire格式和D请求结果汇总交给B负责；我们不越过这个边界单方面替换。

## 第一版实现进展 2026年11月3日

已提交为`47f983157`并通过r07受控读者的64K模型及人工KV硬件验证；当前服务已恢复r05，不属于之前r05的1M硬件验收版本。最新证据见[P源接口硬件验证](p-main-buffer-reuse-pd-source-hardware-validation.md)。

- 新增`ascend_store/reuse_source.py`：不可变`SourceView/SlotEpoch/LayerSource/RequestSource`及`SourceConsumer`协议、单有序写者registry。
- 注册时从现有Main布局和实际tensor构建组件视图，包括Indexer data/scale入口、字节stride和物理page大小。
- `LayerLoadTask.before_slot_write`在receiver的旧PUT／attention／外部wait之后、GET之前执行；先等待registry旧代成功释放，再打开新代。
- KV-ready位置发布完整batch请求范围及新NPU event，再封口；重复ready不重复发送。范围超过block映射／容量或缺少request instance时拒绝。
- 发布／打开异常使registry保留失败。GET错误经`slot_write_failed`通知consumer；`abort_reuse_sources(error)`可供协调层传递取消／执行失败，不表示DMA已停止。
- `AscendMultiConnector`仅对显式声明`reuse_source_contract_version=1`的provider自动安装协议；v1限定一个provider和一个已接线Store sink。旧connector不自动启用新协议。
- 保留`wait_for_layer_reuse`外部waiter契约；新协议追加完整源代次信息，不能单靠版本声明跳过原有接线检查。
- 显式`AscendStoreConnector.restore_kv_for_transfer(metadata)`驱动逐层恢复，无需model forward；要求每个请求`restore_end_token == token_len_chunk`且身份有效，不允许混入新计算token。
- restore-only记录真实NPU起始事件以放行下一层预取，再走既有KV-ready/save完成路径；不伪造已完成的设备事件。

对方实现以下worker-local接口并声明version=1即可参加接线：

```python
open_source(source)
source_ready(source, requests, ready_event)
seal_source(source)
wait_source(source)
abort_source(source, error)
```

`open_source/source_ready/seal_source/abort_source`必须是线程安全的本地登记操作，不能等待网络或等待ready_event。
`source_ready`在任何异步任务可运行之前登记本次完整batch的全部ticket；发送线程等待ready_event后才读取。
`seal_source`即使零payload也必须关闭新增读者。`wait_source`仅在SEALED、全部源读取静止且成功时返回；否则阻塞或抛异常。
`abort_source`报告失败并保留在途引用，不能提前复用源。wire字段、传输generation和D端完成由同事实现，不能把这里的本地event发到远端。

初版有意保守：step入口及正常shutdown在释放资源前drain外部源；registry一旦失败不尝试原地恢复，禁止继续写入，等待者抛错且分配保留。
这不是原生卡住传输的有界退出实现；底层终止及进程销毁保证仍需双方联调。
restore-only当前是显式调用入口，尚未让scheduler自动选择它；调用方须先完成D目标admission，且不得对同一metadata再执行normal forward。
源范围当前发布`[0, token_len_chunk)`，consumer根据D计划选择子区间，不能假设只含新chunk。

本地测试覆盖原有111项及新增协议／receiver／view／restore-only／接线／失败用例。硬件ST、真实双路径传输、调度器自动restore-only和性能时间线仍未验收。
本轮CPU测试先因隔离环境缺上游MultiConnector依赖失败，补齐仅上游基类stub后执行真实AscendMultiConnector方法通过；没有用复制方法替代实际接线代码。
