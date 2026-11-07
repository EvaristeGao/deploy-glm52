# Receipt 重构执行与验证

**最终状态：本轮计划已完成并通过验收。运行代码d634632f6；D保留refactor-r3，P/Master/proxy不变。**

用户目标：按前一轮审查开始重构，做好测试。基于源码 `5f410991d`，部署仓库
`423c0f9`；保留 mode3/FULL_DECODE_ONLY、async、P/D MTP1/3、Main B2 和
现有跨请求公共前缀功能。mode0 继续拒绝。只操作本项目的 vLLM 容器。

## 执行清单

| 项目 | 完成证据要求 | 当前状态 |
| --- | --- | --- |
| 清理旧全局 channel 和 Pipe 组装原型 | 生产无依赖；显式独立 owner、双向 Pipe/进程故障回归 | `080220d72`，并入后续全套217项通过/1跳过，含新增v1/v2 legacy frame用例 |
| retention 协议与传输解耦 | RPC 不再继承 Pipe 类；v1/v2 schema 保持兼容 | `target_retention_protocol.py`，Pipe 仅在测试目录；同上 |
| 干净镜像源替换 | 陈旧 Python/字节码消失，SDK native/operator/metadata 校验不变 | 文件系统测试6/6；两D空闲镜像1395个SDK哈希不变，旧模块不可import |
| connector 请求状态和锁归属 | 一个 worker-local 请求索引；安装/退役/取消/延迟消息、并发测试；保持锁序 | cf4ad2e31，CPU217项/1跳过、旧ledger37项通过；本轮d634632f6原生40语义+断连/41生命周期通过 |
| 拆分 connector receipt 组装 | 明确 scheduler/worker/receive 边界，减少主 connector 职责；原生 PD 回归 | cf4ad2e31提取ReceiptReceiver/ReceiveRequestIndex；本轮d634632f6原生40语义+断连/41生命周期通过 |
| provider/runtime 状态重复 | 核对独立证明和外部 mutation 边界 | 已核对：保留独立机器和防御复制；移除无人读取的异常/traceback留存，新增GC回归 |
| wire 几何 normalization 重复 | 共用语义相同的纯函数，保留两套 trust/version 边界和 whitelist | a32335a55：dtype与block范围共用contract纯函数；frame/构造校验继续分层 |
| 完成事件历史 | 按真实 stream 有序 frontier 压缩，覆盖乱序退出、动态 stream、失败恢复 | f72583434：227项CPU通过；双stream原生探针和41项PD生命周期通过 |
| 退休记录 | 去掉可证明冗余记录；保持 stale callback 拒绝与 release 幂等 | a32335a55：释放即删live alias，64次复用/旧回放/变化lease测试通过；compact tombstone保留 |
| 最终测试与现场 | 聚焦CPU、完整语义/prefix/并发/取消/全rank释放与固定输入性能对照 | CPU/SDK/41项原生通过，固定hash性能前后各80项通过，现场健康空闲 |

## 当前证据

集群只读快照 `artifacts/receipt-refactor-20261108/before-health.json` 确认原
`dfb52bd0a` 服务仍健康空闲。没有把旧记录直接当成本轮现场证据。

实际已安装包相对 Git 的生成文件主要是 `_version.py`、`_build_info.py`、
两份根 shared libraries、lib64、include 以及 1,381 个 `_cann_ops_custom`
文件。构建器按明确 SDK 边界保留并校验，其他源码目录先删除再复制；构建
上下文每次新建，避免重复解包引入陈旧文件。完整原始清单和测试日志位于
`artifacts/receipt-refactor-20261108/`。

尚未完成全目标；每项单独提交并记录测试，原生验收前不宣称重构版可用。

首次真实构建因源码内_cann_ops_custom/.gitkeep被过严的SDK保护规则拒绝，
失败发生在构建器而非服务切换。已补允许该占位文件、禁止实际operator覆盖
的回归，重新构建成功；删除模块与原生SDK的1395项校验在两台D上通过。
原dfb52bd0a服务未切换；新r1容器只用于独立源码/SDK检查和真实导入测试。

## 第二批结构与完成事件

b2c4c5e1d修正纯协议测试bootstrap：避免真实SDK测试被CPU backend mock替换。
phase1真实SDK connector测试44通过；phase2首次collection因测试夹具依赖源码
相对路径失败，原始日志保留；修正测试bootstrap后48通过。候选r1/r2都未启动
模型服务；不能把这些导入/控制测试算作完整PD验收。

f72583434把每请求事件列表改为按owner/实际stream handle和记录序号的frontier。
不同stream/owner、未知opaque依赖不丢弃；同stream乱序返回不能覆盖更新的tail。
record失败保留可能仍在用的stream，即使manager后续移除它；无法枚举/异卡stream
时隔离owner，不猜测完成。淘汰event引用在runtime锁外释放，避免析构驱动工作
进入状态锁。1000次use、延迟release、失败恢复、析构锁检查均覆盖。

真实安装torch_npu为2.10.0.post7，revision3363ea59d49f3e08cfd8d4a1f532b756ccda0682。
固定版本的[NPUEvent.cpp](https://github.com/Ascend/pytorch/blob/3363ea59d49f3e08cfd8d4a1f532b756ccda0682/torch_npu/csrc/core/npu/NPUEvent.cpp)
和[NPUEventManager.cpp](https://github.com/Ascend/pytorch/blob/3363ea59d49f3e08cfd8d4a1f532b756ccda0682/torch_npu/csrc/core/npu/NPUEventManager.cpp)
说明event析构提交lazy destroy；实际回收仍依赖SDK/API分支。没有以源码阅读替代
原生NPU和完整模型测试，计划补两stream/1000步真实event探针。

a32335a55移除provider保存的无人读取exception，避免traceback额外持有失败回调
局部对象；异常仍直接向调用方抛出，失败phase和重试规则不变。新增GC测试。
provider/block ownership和runtime/reader completion职责不同，且runtime可注入；
防御lease副本保护mutable WriterPlan免受回调或外部调用方修改，不能机械共用。
内部codec的typed whitelist和外部JSON/schema也不合并；仅抽出相同dtype大小、
block范围纯函数，覆盖/唯一性仍由各调用者按原规则检查。

释放后删除delivery的内部request-id链接，64代次回归确认别名复用、旧admit拒绝、
精确release幂等、变化lease拒绝及stale scheduled执行拒绝。provider与delivery的
fingerprint记录分别保护获取/释放与数据面admission，并可独立组合；未定义重放
期限时不能用TTL/LRU移除。当前仍随已退休唯一身份增长；这是协议边界，不声称
已经实现固定内存的永久去重。

目前聚焦CPU227 passed、1 skipped、112 subtests；旧ledger37通过，scoped Ruff
通过。原dfb/mode3/MTP3正式C10/C20对照已完成：80/80全rank生命周期通过，
TTFT中位13.32/23.57秒、TPOT中位32.81/41.95ms。基线结束后才开始构建
最终d634632f6镜像，未在正式性能期间构建/分发。源码重构尚待新容器原生验收。

GitHub PR#1重新只读查询：review comments仍为3条，无新增或修改；issue comments
为0。原始响应保存review-comments-latest.json、issue-comments-latest.json。
未向PR发送消息，也未push分支。

最终d634632f6候选已在两D完成镜像源码/1395项SDK哈希校验和旧模块缺失检查；
真实SDK connector48项通过。双stream/1000步真实NPU事件探针通过，frontier
保留2个屏障且实际tensor值正确。该探针不是完整模型验收；后续使用Ansible
只切换捕获的两个D旧容器，P/Master/proxy保持原进程，进行完整PD测试。

## 完整模型验收（d634632f6，D mode3/MTP3）

仅切换D为`...-refactor-r3`，镜像
`glm52-vllm-ascend:0.30.0-layerwise-guide-d634632f6-cann92`，两D image ID均为
`sha256:0ac34e9dd889ed5c254fa9285d4521298c9730c45e345a316012672b9435aede`。
profile为`artifacts/receipt-refactor-20261108/phase3.yml`；仍是TP8×DP4、
Host100GiB/DP、seq8/token32、async、FULL_DECODE_ONLY/VLLM_COMPILE、MTP3，
未启用receipt timer/NPU profiler。P保留e9bd1ce19/eager/Main B2/MTP1；
P0/P1容器ID、image、StartedAt与before-manifest逐项一致，Master2435912和
proxy2470638的PID/启动时刻/命令行也逐字一致。

新nonce20/20和原mode0失败提示、新salt20/20均通过语义及全8-rank回执/释放，
覆盖四D，包含8并发multichunk和prefix回放。两组整体外部prefix命中分别为
267264/540732、267264/540692（约49.4%）；这是整组冷+回放请求的比率。
这批成功样本不证明历史eager/mode0异常根因已经修复，mode0继续拒绝。

断连在8个content chunk后触发，仅8次提议/24个draft token（输出上界32，
远小于强制4096），正常success未增加、队列和KV使用归零。精确ID的全8-rank
回执和释放通过。合计41/41生命周期通过。证据functional-verdict.json、
compare-verdict.json、prefix-proof.json、cancel.log、audit41.json、
after-manifest.json和前后control-processes.txt。固定hash C10/C20最终样本运行中。

本轮未新增MTP5、1M输入、native delayed-DMA/registration-reset故障注入；
这些不能从MTP3/55K或CPU模拟外推。旧MTP5实测证据仍为runbook§11.78的
bb679f01d，与本轮新镜像区分。

规模按5f410991d至d634632f6的`vllm_ascend/`diff统计：运行代码+756/-899，
净减少143行；主connector为3738->3492行（减少246）。测试和文档增量单独保留，
不把移动文件或压缩写法当成大幅减少整个特性的代码量。完成事件按stream数量
保留是复杂度与内存界限改进，尚不代表端到端latency提升。

## 最终性能与保留现场

55K随机输入/700输出、seed20261106、每档N40、C10/C20，前后prompt hash向量
及实际input_lens完全一致，每档2197493输入/28000输出tokens，独立salt、外部
prefix命中0。性能请求只检查SSE/token/生命周期，不对随机文本作语义判定。

| 指标 | C10旧dfb | C10新d634 | C20旧dfb | C20新d634 |
| --- | ---: | ---: | ---: | ---: |
| TTFT P50(s) | 13.32 | 13.46 | 23.57 | 25.12 |
| TTFT P99(s) | 31.72 | 34.98 | 53.15 | 57.04 |
| TPOT P50(ms) | 32.81 | 31.44 | 41.95 | 39.60 |
| TPOT P99(ms) | 40.02 | 38.97 | 55.86 | 50.13 |
| 输出吞吐(tokens/s) | 169.14 | 169.89 | 214.56 | 218.34 |

两组各80/80请求全8-rank receipt/释放通过。TPOT单轮中位数降低约4.2%/5.6%，
TTFT略升；输出吞吐只小幅变化，不能据此宣称稳定端到端提速。确定的改进是
生产原型清理、状态职责简化，以及每请求完成屏障保留由步数增长改为stream前沿。
证据performance-comparison.json、before/、
after/、before-audit80.json、after-audit80.json及固定SHA检查客户端。

最终八引擎和proxy均健康空闲、KV使用归零；四份vLLM D日志ERROR、Traceback、
Exception in thread及RECEIPT_HOST_TIMING均0，32 worker记录mode3/MTP3、
draft eager，四D完成target图捕获。该零ERROR结论不覆盖底层ACL日志。
P容器身份与Master/proxy原PID/命令行再次核对不变。旧D和r1/r2空闲候选停止
保留，CPU压测客户端停止，当前新D保持运行。默认inventory仍为旧默认档，
实际使用phase3.yml，不自动将实验配置提升为默认。

最终检查：final-health.json、after-manifest.json、final-owned-containers.json、
final-log-audit.json、final-control-processes.txt；主connector3738->3492行，
运行代码相对5f410991d净减少143行。源码/部署仓库分别本地提交，未push。
