# Receipt交接实现缺口核对

2026-11-04。核对的是远端 `mazifan0120/vllm-ascend:feat/layerwise-receipt` 的
`6c233175d95909ec8df1175f85e5e212496fa6ec`；文档自述实现版本为 `bc51f47`，最新提交只更新文档。
本地 `p-main-buffer-reuse` 仍为 `06e16d717`，只fetch／读取，没有merge、checkout或部署远端代码。

结论：**TargetProvider四方法核心已交付；真实D runtime尚未接上，此外connector协议实现还有需要先修的正确性问题。**
不能理解成“只补三条runtime回调就能直接联调”。

## 已实现与剩余工作

| 项目 | 当前状态 | 接下来要做 |
| --- | --- | --- |
| RegisteredTargetProvider四方法、布局工厂、17项UT | 已实现，远端与本地代码相同 | 不必重写核心 |
| 共享契约、双账本、metadata规格、provider注入、release入口 | 有实现 | 修正下述协议／生命周期问题，补集成回归 |
| retention token字段签发 | 已补非空token和scheduler内记录 | 真实owner核验、block保留及跨进程查询仍需落实；不等于真实retention已验收 |
| TargetRuntime | 只有Protocol | 实现retain／make_visible／release，接实际manager、注册和计算／写回生命周期 |
| scheduler-worker facade／RPC | 未实现 | acquire、prepare、release共享同一租约状态；真实内存和事件留在owner进程 |
| provider自动构造、挂载 | 未实现 | 注册完成后导出catalog并绑定；处理constructor先扫描、catalog后就绪的两阶段初始化 |
| D注册与writer约束 | 核心会校验，但真实输出未绑定 | 一backing一region；Main Host／Indexer device；实际注册代次和writer plan一致 |
| 远端写入可见性及安全回收 | 未真机验收 | 写后立读、消费stream依赖、取消／抢占、远端DMA及本地计算／写回静止后回收 |
| 首次模型联调 | 未完成 | 双方BF16 Main、相同布局；当前P独立INT8档不能直接匹配D BF16 Host Main |
| P五方法sender、scheduler restore-only | 文档明确后续轮次 | 不能据此宣称完整设计已完成；无需重复开发已交付的P源接口 |

`provider_from_registered_dsa_layouts()`在生产树没有调用点；`TargetRuntime`也没有生产实现。
RegisteredTargetProvider的租约状态是进程内状态，不能在scheduler acquire之后，换一个worker实例就直接prepare。
需要owner端实现和真正的跨进程facade。MultiConnector在构造时就扫描provider，
因此也不能只在稍后的KV注册阶段临时加属性而不处理初始化次序。

## 额外发现的协议问题

下面是固定提交源码及CPU复现结果，不是已部署NPU故障的声明。行号均指远端快照。

### 1. 普通模式也被带入receipt分支，优先修

`mooncake_layerwise_connector.py:1314`附近，在D请求分配后无条件生成并传播request_instance，
不检查layerwise_receipt_mode或provider。没有provider时，target发布函数直接返回，没有创建receipt规格。
P却按非空instance参与receipt，版本化DONE在D没有账本时也不会回到旧完成路径。

CPU复现：receipt_mode=false、provider=None时，发给P的参数仍含非空instance。
所以“开关关闭即可保留普通模式”的兼容性目前不成立，必须补真实connector普通模式回归。
这不涉及此前只测试本地06e16d717旧流程的集群结果。

### 2. 空backing身份与共享契约冲突

`TargetScope`禁止空backing_id；P的`_build_transfer_key()`（1853）却直接传空字符串，
实际方法调用会抛ContractError。发送消息中的Indexer backing同样为空，D只对Main补默认backing，
因此Indexer解析仍会被契约拒绝。

需要传递／绑定真实目标身份，或共同定义合法的源ticket表示；不要填任意假ID来绕过检查。

### 3. 覆盖账本缺少逐层计划，失败结果也未阻止ready

`mooncake_layerwise_receipt.py:108`及`_data_ready()`（198）只按Main backing或Indexer consumer rank合并token区间。
虽然TransferKey含logical_layer，覆盖判断却没有required layers／entries计划。

CPU复现：只给第0层Main和Indexer的完整区间，再给rank terminal成功，就能build出ready receipt；
账本无法表达“第1层仍缺数据”。另一个用例中，第0层FAILED、第1层SUCCESS且terminal成功，仍得到ready。
需要按真实required views／层／组件／rank／范围验收，失败必须保留为失败；writer及backing generation也应按目标计划校验。

### 4. 历史失败在发送terminal前被清掉

`KVCacheSendingLayerThread._handle_request()`（325）在最后层给旧callback报告失败之后，
先移除failed_reqs，再调用`_emit_layer_results()`（364）。后者重新检查该集合，可能发送success terminal。

CPU复现：旧callback得到trans_flag=false，同时receipt terminal却是success。
旧FAILED消息路径可能另行阻止整请求完成，**本复现不声称完整网络路径必定错误放行**；
但receipt协议本身已出现互相矛盾的结果，不能依赖另一条旧消息兜底。

### 5. 可见性调用缺少强制门禁

接收线程`_maybe_publish()`（844）在provider或lease缺失时跳过prepare_decode，随后仍mark_visible并发布done。
CPU复现了lease存在、provider=None时请求被发布。即使启动检查通常能挡住缺provider，发布处仍应拒绝不完整状态，
尤其是在真实跨进程接线尚未完成时。

prepare_decode抛错的分支目前只记录日志并返回，也需要明确失败通知和资源保留／回收，不能只依赖日志。

### 6. P启动通知早于target admission，缺少worker账本就绪屏障

`update_state_after_alloc()`先executor.submit向metaserver发布P启动参数，随后才acquire target。
CPU复现：目标申请最终失败时，发布任务已经提交。真实并发下P可能早于目标持有或D worker账本安装开始工作。
未知instance的LAYER_RESULT会被丢弃，而接收循环仍发送ACK。

需要把目标admission／retention／worker账本安装完成作为P启动前提，或实现等价的可靠缓冲／握手。
单纯把receipt_specs放进下一次connector metadata，并不构成这条先后保证。

### 7. 清理时内部／外部request ID不一致

worker `get_finished()`（2124）把完成ID映射为内部ID后调用forget_request；
接收线程的instance_to_req及receipt_mode_reqs却保存外部ID，forget_request（879）做原样比较。
源码的get_external_request_id（2931）明确去掉内部9字符后缀。

CPU复现：传入内部ID后，外部ID对应的ledger和receipt marker仍保留。
需要统一取消／失败／完成路径的身份，并覆盖外部request ID重用和迟到回调。

## 验证与边界

- 远端原有27项纯CPU receipt UT实际复跑通过：`receipt-ut.log`。
- 另运行固定源码ledger及AST提取的6个真实connector方法，复现以上问题：`protocol-gap-repro.py`与JSON结果。
  AST仅用于避开本机缺少的NPU／vLLM依赖；没有以模拟实现替换这些被检查的方法。
- 最初复现脚本因构造空Indexer scope触发契约拒绝；随后用合法scope隔离账本问题。
  一次断言误用枚举的大写名称也已纠正，两个尝试日志均保留；不把harness失败算成模型故障。
- 未部署该分支，未运行真实receipt P/D端到端；mock runtime和CPU状态机不能证明TE可见性或安全回收。
- 快照及证据：`artifacts/p-main-buffer-reuse/receipt-handoff-audit/`，含snapshot.json固定SHA和文件摘要。
- 当前集群已恢复原P r05／D r03，身份、镜像、脚本、manifest、健康和8K／64K冷请求复验通过。

## 推荐推进顺序

1. Connector侧先补普通模式门控、合法target身份、逐层覆盖／失败状态及正确的visibility与启动门禁，补真实方法接线测试。
2. 我们实现D owner-side TargetRuntime及scheduler／worker facade，明确retention核验和初始化时序，可与第1步并行。
3. 共同接入实际catalog／TE区域、消费stream和取消／写回／DMA回收；补有界失败处理，不能把P槽位300秒等待当作D watchdog。
4. 用同精度BF16 Main做短上下文网络内容对比、连续decode及普通模式回归，再做长上下文。

来源：[远端交接文档](https://github.com/mazifan0120/vllm-ascend/blob/6c233175d95909ec8df1175f85e5e212496fa6ec/RECEIPT_HANDOFF.md)、
[connector源码](https://github.com/mazifan0120/vllm-ascend/blob/6c233175d95909ec8df1175f85e5e212496fa6ec/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_layerwise_connector.py)、
[receipt账本](https://github.com/mazifan0120/vllm-ascend/blob/6c233175d95909ec8df1175f85e5e212496fa6ec/vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_layerwise_receipt.py)。


## 两分支整体清单补充

按后续要求已另行整理[完整代码地图与剩余工作](../src/vllm-ascend-reuse/PD_TRANSFER_IMPLEMENTATION_STATUS.md)，
并在完整receipt独立worktree复跑：共同聚焦139项通过，纯账本27项通过，前述真实方法复现仍成立。
另确认sender以primary tensor数量猜组件，在单Main tensor＋独立Indexer data/scale、同group结构下漏发Indexer回执；
真实connector测试类均以Tests结尾，而当前pytest默认前缀为Test，依赖占位后的收集检查为0项。
两项证据在artifacts/p-main-buffer-reuse/two-branch-inventory；收集检查未执行测试体，不作为NPU测试。
64K定位按用户要求暂不继续；本轮没有集群操作或生产实现改动。
