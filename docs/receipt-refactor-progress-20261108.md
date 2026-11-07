# Receipt 重构执行与验证

用户目标：按前一轮审查开始重构，做好测试。基于源码 `5f410991d`，部署仓库
`423c0f9`；保留 mode3/FULL_DECODE_ONLY、async、P/D MTP1/3、Main B2 和
现有跨请求公共前缀功能。mode0 继续拒绝。只操作本项目的 vLLM 容器。

## 执行清单

| 项目 | 完成证据要求 | 当前状态 |
| --- | --- | --- |
| 清理旧全局 channel 和 Pipe 组装原型 | 生产无依赖；显式独立 owner、双向 Pipe/进程故障回归 | `080220d72`，并入后续全套217项通过/1跳过，含新增v1/v2 legacy frame用例 |
| retention 协议与传输解耦 | RPC 不再继承 Pipe 类；v1/v2 schema 保持兼容 | `target_retention_protocol.py`，Pipe 仅在测试目录；同上 |
| 干净镜像源替换 | 陈旧 Python/字节码消失，SDK native/operator/metadata 校验不变 | 文件系统测试6/6；两D空闲镜像1395个SDK哈希不变，旧模块不可import |
| connector 请求状态和锁归属 | 一个 worker-local 请求索引；安装/退役/取消/延迟消息、并发测试；保持锁序 | cf4ad2e31，CPU217项/1跳过、旧ledger37项通过；原生PD待验 |
| 拆分 connector receipt 组装 | 明确 scheduler/worker/receive 边界，减少主 connector 职责；原生 PD 回归 | cf4ad2e31提取ReceiptReceiver/ReceiveRequestIndex；原生PD待验 |
| provider/runtime 状态重复 | 明确共享转换契约，减少重复拷贝，保留 descriptor/block/native-completion 各自证明 | 待实现 |
| wire 几何 normalization 重复 | 共用语义相同的纯函数，保留两套 trust/version 边界和 whitelist | 待实现 |
| 完成事件历史 | 按真实 stream 有序 frontier 压缩，覆盖乱序退出、动态 stream、失败恢复 | 待实现 |
| 退休记录 | 去掉可证明冗余的重记录；保持 stale callback 拒绝与 release 幂等 | 先核对协议约束；不引入任意 TTL/LRU |
| 最终测试与现场 | 全部聚焦 CPU、图配置 gate、完整语义/prefix/并发/取消/全 rank receipt 释放和固定输入性能对照 | 待实现 |

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
