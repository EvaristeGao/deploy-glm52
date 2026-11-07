# P Main reuse / D receipt 代码重构审查

审查对象：`src/vllm-ascend-reuse` 的 `p-main-buffer-reuse`，固定代码
`dfb52bd0a`，相对上游基线 `4234a1f18`。这是首轮边界、调用者和热路径审查，
不是全仓所有函数的逐行正确性证明。统计脚本和原始清单在
`artifacts/review-fixes-20261107/audit-inventory.json`。

## 规模与结论

相对基线增加约 22,600 行，其中运行代码新增 10,358 行、删除 132 行，
测试新增 9,953 行、文档新增 2,267 行。统计覆盖整个开发分支，包括 P reuse、
D receipt、前缀复用及其回归用例，不能把全部行数归到某个单独优化。
约一半增量是测试和文档；单纯删测试或压缩写法，不能解决运行代码的复杂度。

有实质重构空间。优先清理已经被生产 RPC 替代的原型、解除传输与 retention
协议耦合，再明确 connector 内请求状态和锁的归属。热路径应继续减少重复工作，
而不是降低地址、代次、异步完成和 stale callback 校验强度。

## 已落实的检视修改

1. `57c1b306e`：在 admission 时完整校验并绑定 owner-local 执行句柄，
   decode 用 live state identity 检查，仍逐请求核对 scheduler 的真实 block map。
   每个实际 target/draft batch 共用一次注册池校验和一组完成事件；每个请求保留
   独立 visibility 依赖。不是按 speculative token 数创建多个执行上下文。
2. `8ecddeb82`：send/receive 线程启动等待增加线程存活检测和 60 秒界限；
   增加日志区分实际 target graph、draft graph、MTP 数和 compilation mode。
   原因是一次真实 ZMQ bind 冲突使主线程永久等待，不能只靠进程健康超时排查。
3. `bb679f01d`：将 runner 的真实 graph parameter update stream 纳入 completion
   fences。target/draft 共用此 stream，启用图但未接线时拒绝绑定 owner。
4. `dfb52bd0a`：拒绝未编译的 receipt full graph，要求解析后的
   `CompilationMode.VLLM_COMPILE`；Ansible 同样在切换容器前拒绝 mode 0。
   这是对未通过语义验收组合的保守限制，不等于已证明异常根因。

CPU 回归 203 passed、1 skipped、112 subtests；skip 是本地缺少完整 vLLM/msgspec
导入环境的 connector 用例，不能把它当成通过。相关原生 NPU 结论见本轮 runbook，
代码提交本身不代表全部 MTP/graph 组合已经验收。

`57c1b306e` 的匹配 55K/700、N40、C10/C20 原生样本中，target+draft receipt
host 时间由 7.40/11.20 降到 2.51/2.59 ms/step，约减少 66%/77%；完整 lease
热路径比较为零，占 step interval 约 2.4%。20 条独立功能请求和 80 条性能请求
均通过全 8-rank receipt 与释放审计。TPOT P50 为 32.26/38.28 ms，相比上一轮
带计时 31.02/36.90 ms 没有改善。因此只确认 CPU 重复工作减少，不宣称端到端加速。

后续无计时 MTP3/mode0 语义回归只有 **17/20** 通过，两条短请求和一条
multichunk 返回异常后缀；同提示、新 salt 的 mode0 对照又是 **18/20**，
两条前缀回放失败。两组各 20 条生命周期均通过，说明 receipt/释放通过不能代替
语义验收。不能再把问题局限为首个冷请求；旧 mode0 性能数据也不能当作已验收的
可用部署基线。已保存原始失败，未用重跑成功覆盖。mode0 会捕获 target graph，
但 runner `_use_aclgraph()` 为 false 且不初始化其编译图参数；这一不一致需要
隔离，尚非根因证明。mode3 的 MTP3 同提示验证及最终现场见 runbook §11.78。

## 建议的拆分顺序

| 顺序 | 位置和已核对证据 | 建议改动 | 回归边界 |
| --- | --- | --- | --- |
| 1 | `target_owner_channel.py` 171 行；生产代码无调用者，只有测试引用；仍有 module-level mutable rendezvous | 把有价值的 co-located 测试夹具移到测试目录，使用显式实例；移除生产全局槽 | 保留 connector 的真实绑定测试；先确认不会继续支持该原型接口 |
| 2 | `target_owner_wire.py` 370 行混合 Pipe、retention records、guard、dispatcher；RPC 继承 `WireRetentionAuthority` 却不调用其构造 | 提取与传输无关的 retention protocol/guard/dispatcher，再隔离 Pipe 参考实现；无调用者的 `PipeOwnerChannel` 可清理 | 保持 RPC record 名称和 codec whitelist 兼容；保留进程间 rollback、uncertain call、teardown 用例 |
| 3 | `mooncake_layerwise_connector.py` 约 3,740 行，本分支增加约 1,500 行；receive/worker/delivery 各持有请求映射 | 先定义 worker-local request record 和锁归属，再提取 receipt admission/visibility/retirement 组件 | admission 当前先持 delivery lock 再持 receive lock；不能在机械合并映射时反向取锁 |
| 4 | `target_provider.py` 的 descriptor/block ownership 状态与 `target_runtime.py` 的 native usage 状态重复记录同一 request | 先共用生命周期状态转换说明、减少 normalization/copy 边界，再判断能否共用部分 record | 两者职责不同：块独占证明和真实 stream completion 不能合并成一个简单布尔值 |
| 5 | `target_owner_codec.py` 和外部 `mooncake_layerwise_wire.py` 都有几何数据处理，但信任/版本边界不同 | 仅共享语义一致的 immutable geometry 和 normalization helper | 保留内部 typed whitelist、外部 schema、地址隔离、frame traversal 限额和 generation 校验 |

推荐每项独立提交。第一批可以缩减数百行生产原型和耦合代码，但目前没有对整个
重构后的行数作可靠估计；connector 拆分首先改善边界，不必然减少总行数。

## 新发现的性能与长期运行问题

### 完成事件历史随 decode 步数增长

`_TargetUse.events` 在每次 target/draft use 后追加 batch 的全部事件，release
遍历并 synchronize 整段历史。当前共享 batch events 已减少创建次数，但每个
request 的引用历史仍随步数增加。

可以研究以实际 stream identity 和单调序号保存每条有序 stream 的最新 fence，
让内存及释放工作接近 stream 数，而不是 token 步数。必须覆盖动态 stream、
record 失败后的恢复，以及并发 context 退出顺序；不能直接用最后一次 tuple
覆盖历史，因为返回顺序不必代表同一 stream 上事件记录的先后。本轮只确认
结构问题，没有测得它对 TPOT 的收益，也没有修改其完成证明。

### retired 请求历史无上界

provider 和 delivery 分别保存 `_retired` fingerprint；delivery 释放后还保留
`_by_request_id` 链接。没有发现 TTL、容量界限或 owner 代次轮换清理路径。
它们保护 stale callback 拒绝和 release 幂等，直接加 LRU 会改变协议语义。

应先定义可证明的 replay horizon / owner-generation rollover，再压缩 tombstones，
并增加长时间、多次 admission/release 的容量回归。真实服务运行时间较短，
本轮尚未量化累计内存增长，不能把它描述为已复现 OOM。

### 注册池检查仍占一部分 host 时间

新路径每个 target/draft batch 分别校验一次共享池，约 1.64/1.65 ms/step。
可以在同一实际模型 step 的完整 target+draft 生命周期内共用验证，但需要先证明
registration、tensor storage、cache mapping 的修改受独占 owner 控制，并有失效机制。
不能仅用缓存 generation 替代现在仍能检测的 storage/mapping/registration 变化。

## 删除模块之前必须修正镜像构建

`ansible/dsa-layerwise/Dockerfile` 把源码 `COPY` 到 SDK 包上，只校验当前源码清单，
不会删除 SDK 中已从开发源码删除的 Python 文件。`build.yml` 同 revision staging
目录重复解包也保留被删除文件。仅在 Git 删除旧模块，最终镜像仍可能 import 到它。

清理模块前，应使用干净 staging 和明确的 managed-Python manifest，或替换受管理的
Python 子树，同时保留 SDK 的 native `.so` 与生成的 `_build_info.py`。需要直接验证
被删模块在镜像中不存在。当前构建复用了未删除模块的源码，因此本轮候选不依赖
这个待修复能力；不要直接清空整个 package 来解决。

## 后续验收门槛

此次 review 的 MTP5/FULL_DECODE_ONLY/async lifecycle 原生组合已经完成验证：
`bb679f01d`，20 条独立功能、8 条 55K/700 请求和 2 次生成中断连共 30 条，
全 8-rank receipt/释放通过。32 worker 实际 target/draft 上下文和 batch>1 已记录，
每次提议实测 5 个 draft token。target graph capture 成功，但当前 GLM family
guard 仍令 draft eager；不宣称 target/draft 双图支持。首次断连探针要求 abort
success counter 增长的假设不成立，原始失败保留；补充用实际少量提议、KV/队列
清空和精确 target release 验证回收。未做 native delayed-DMA/registration-reset
故障注入。详见 runbook §11.78 和 `mtp5-native-verification.json`。

最终 `dfb52bd0a` 新镜像使用无计时 mode3/MTP3，再通过新 nonce 的 20 条及
原失败提示的 20 条语义回归，实际前缀命中均约 49.4%；加一次断连共
41/41 全 rank 生命周期通过。保留 `...-review-safe` D 容器，P/Master/proxy
保持原进程，默认 inventory 未改。该配置尚未重跑正式 C10/C20 性能矩阵。
成功样本不替代异常根因定位，mode0 继续禁止，先前的 eager 异常也未证明同因。

在此基础上开始结构清理。
每次重构保留三层验证：协议与故障 CPU 回归；独立短请求、多 chunk、前缀回放的
语义和全 rank 生命周期审计；固定输入 hash 的 55K/700 性能对照。随机性能输入
只检查 SSE、token、完成状态和生命周期，不检查文本语义。未定位的首轮短请求
重复词异常仍保留为独立问题，不能以随后的成功样本宣称修复。
