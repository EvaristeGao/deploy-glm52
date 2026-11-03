# P侧传输源接口硬件验证

2026-11-03。本轮验证新增P源视图／代次／ready接口，不包含同事尚未接入的新P/D sender。
原始证据均保存在`artifacts/p-main-buffer-reuse/pd-source-runtime/`。

## 环境与保护

P0/P1使用带owner标签的独立开发容器，D保留r03，Mooncake master未修改。
原有r05在确认两个P running/waiting均0后停止并保留，ID和image记录在`previous-r05-containers.json`。
没有主机信号、设备reset或系统服务变更。测试镜像仅在P构建，不分发到D。

r06/r07基于此前已验收r05，复制本地13个生产Python模块。镜像中的`pool_worker.py`另外有两行测试注入，
在注册源视图前安装`_reuse_test_reader.SourceReader`；此注入不在生产源码工作区。
测试读者在独立线程等真实NPU ready事件，对每请求首尾block的组件page各读取两次并比较，读取完成才释放pending。
它不修改KV、不实现网络发送、不汇总D就绪。空batch也经正常open/seal/release。

模型测试配置：Main B2＋首尾独立、Indexer独立、INT8、TP16 × DP2、eager、无MTP/DCP、
max_model_len131072、max_num_seqs1、chunk8208；请求只生成一个检索数字。
与原1M档相比缩短模型长度和chunk，保留partial边界；本轮不做1M请求。

## 首次实测发现及修复

r06健康后，8192-token请求返回HTTP500，错误为`ReuseSourceRegistry.publish`中的`KeyError: 0`。
根因：首chunk没有历史GET，receiver通过`_copy_gva_layers`的空任务分支完成，
新增`before_slot_write`只接在range GET路径，导致KV-ready发布前没有打开epoch。
修复覆盖空GET、构造后无实际范围及正常batch GET分支；失败分支保留fatal并通知source consumer。
新增测试通过实际`_handle_request`分发空任务，而非直接调用range处理器，避免再次漏测。
修复后聚焦CPU测试122项通过，日志`../ut-20261103-203200-173389.log`。
r06请求未记为通过，64K阶段当时尚未提交。原始失败请求和两节点错误日志均保留。

## 独立NPU与真实Store测试 已通过

使用`restore_only_npu.py`，在模型已停止的P0开发容器device0运行。
它分配人工NPU KV、写入真实Mooncake range对象并提交，污染原源后恢复。
使用生产`KVCacheStoreLayerRecvingThread`、`ReuseSourceRegistry`和`KVPoolWorker.restore_kv_for_transfer`驱动；
synthetic metadata、地址builder及无模型的层回调由测试构造，不冒充真实模型scheduler自动restore-only。

- 有效长度65521 tokens，6层，Main／Indexer不同内容，包含非整block尾部。
- B=1/2/3分别恢复正确，每档12项全量组件内容检查通过。
- 异步读者持有第0层，等待下一写者进入waiter后才再检查源内容并释放，确认等待期间没有覆盖；不靠sleep猜时序。
- 注入GET异常后不发成功ready，registry保留失败，下一次begin_write被拒绝。
- 三档结果均PASS，源数据及阻塞证明见`restore-only-npu-r06.log`。
- 对象删除返回-706（仍有租约）按既有规则延后清理，没有force删除或修改master租约。

首次测试在内容检查后因harness将`batch_get_session_end`的整数返回误当列表而退出；
修正harness后整套重新执行通过，失败记录保留在`restore-hardware-controller-initial.log`。

重跑必须通过`run_restore_hardware.py`的设备占用／容器身份门禁；P正在运行其他开发引擎时拒绝操作。
不能在正常模型服务占用设备时直接运行裸测试脚本。

## 真实模型验证

修正版r07镜像校验13个模块通过，真实模型测试全部通过：

| 请求 | 实际输入tokens | 秒数 | 答案 |
|---|---:|---:|---|
| P0冷请求 | 8190 | 12.89 | 4，正确 |
| P0冷请求，多chunk | 65532 | 43.71 | 9，正确 |
| P0同前缀BETA检索 | 65533 | 10.60 | 7，正确 |
| P1新冷请求，多chunk | 32762 | 22.16 | 9，正确 |

BETA保留64K源nonce和前缀，仅更换末尾问题；重建原prompt的SHA与token数检查通过。
本地prefix hits为65408，external hits为0；这次不是新的独立冷prefill。
初次replay因archive包含8K和64K两次完成记录被探针拒绝，没有提交模型；提取原始64K两行记录后重试通过。

32个P rank都安装并实际调用新协议。P0 open/read各12480、P1各4992，合计17472，全部配对；
两节点均覆盖78层。Main样本34944、Indexer样本16576，合计51520次双读取比较，reader错误0；
记录3617次实际等待。这证明新接口运行且存在计算等待外部读取的情况，不代表传输完全被掩盖。
模型采样按整page检查已分配内存，不把它当作partial有效字节范围的证明；partial有效内容由上述人工KV全量检查覆盖。

最终P0/P1/D0/D1健康200，P running/waiting及错误计数0，D所有成功原因计数0。
从r07启动开始扫描完整P0日志665776303字节、P1日志114492162字节（实际大小以scan JSON为准），
没有ERROR、Traceback、段错误或malloc失败／损坏匹配。首轮扫描将正常“Using default malloc/free”告警误列入，
保留首轮记录后修正错误模式并扫描完整日志至EOF；没有屏蔽真实错误行。

主要证据：`r07-final-audit.json`、`reader-audit-r07.json`、`r07-metrics.json`、`r07-error-scan-{p0,p1}.json`，
原始reader逐行日志压缩保留在`{p0,p1}-reader-logs-r07.json.gz`。完整native日志保留在r07容器内。
本轮修复后122项CPU测试通过，Ruff及diff检查通过。

收尾已恢复之前r05的原容器ID、镜像和启动脚本，10个原生产模块SHA复核一致；r06/r07停止保留并添加退役标记，测试读者不进入r05。
恢复后新8189-token请求3.80秒、答案6正确；四引擎健康200，P空闲且错误0，D请求0，见`restoration-audit.json`、`restored-r05-8k.log`。

## 仍不包含

新connector的真实P→D网络双路径、D目标admission与覆盖汇总、scheduler自动restore-only、
原生传输卡住时有界安全退出、并发长请求和性能基准。测试读者会增加同步和CPU拷贝，不能用本轮耗时评估生产性能。
此前r05的1M验收仍只对应当时版本，不自动覆盖新增接口。

交付包：`artifacts/p-main-buffer-reuse/handoff/pd-source-hardware-review.tar.gz`，附逐文件SHA清单。
