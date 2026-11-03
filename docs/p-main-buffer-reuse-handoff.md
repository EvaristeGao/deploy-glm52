# P 侧 Main KV buffer reuse：代码交接与评审入口

更新：2026-11-03。本文件描述当前可评审的实现；实验过程见 runbook。

## 1. 代码在哪里

| 内容 | 位置／版本 |
|---|---|
| 当前实现源码 | [`src/vllm-ascend-reuse`](../src/vllm-ascend-reuse/) |
| 源码分支 | `p-main-buffer-reuse` |
| 固定基线 | `4234a1f1815252e8b52c75d6956d0bdf9ae80cc0` |
| 功能提交 | `9824ec91c`：Prefill Main reuse 实现及测试 |
| 当前 HEAD | `47f98315724770372462399d390068cebb7c3c16`：P源视图、槽位代次、ready契约与restore-only入口（带sign-off） |
| 部署与验证工具 | 本仓库 `tools/`，包含本轮P独立容量、前缀复用及受控读者验证入口 |
| 已验收配置 | [`ansible/profiles/p-main-buffer-reuse.yaml`](../ansible/profiles/p-main-buffer-reuse.yaml) |
| 旧实现 | `src/vllm-ascend` / `m1-prefill-reuse`，保留历史，未合入当前分支 |

本次核对源码工作区干净。相对固定基线共 **21 个文件，+2644/−51**：13 个生产模块、8 个测试／mock 文件。
源码工作区被部署仓库 gitignore；仅提交或复制部署仓库，不会自动携带这些源码。

为避免源码遗漏，已额外导出到当前目录：

- [最新Git bundle](../artifacts/p-main-buffer-reuse/handoff/p-main-buffer-reuse-latest.bundle)：三个新增提交，保留提交历史；接收仓库须已有固定基线。
- [最新完整补丁](../artifacts/p-main-buffer-reuse/handoff/p-main-buffer-reuse-latest.patch)：固定基线到当前 HEAD 的代码及测试差异；SHA见`latest-manifest.json`。
- [文件统计](../artifacts/p-main-buffer-reuse/handoff/diff-stat.txt)；同目录 `manifest.json` 记录版本、文件清单与产物 SHA256。

只读查看改动：

```bash
git -C src/vllm-ascend-reuse log --oneline 4234a1f18..HEAD
git -C src/vllm-ascend-reuse diff --stat 4234a1f18 HEAD
git -C src/vllm-ascend-reuse diff 4234a1f18 HEAD -- vllm_ascend
```

## 2. 已实现的行为

目标是在已有 Mooncake layerwise offload 上，只让 **Main KV** 跨层共享少量 HBM 槽位。
Indexer 不折叠，保持原有独立缓存。`num_shared_buffers` 控制共享槽数，独立层另外占槽。
例如 B=2、首尾独立时，Main 总共使用 4 个物理槽。

一次槽位轮换的关键约束是：覆盖前等待上一使用者的 attention 计算读、Store PUT 源读取、
P/D 远端读取结束，然后才能恢复当前层前缀并执行当前层。
实现复用已有 sender/receiver 完成机制，并补独立的 attention completion、真实 waiter 接线检查；
没有为所有同步路径额外引入一套同义的 `reserve_read/complete_read` 计数 API。

多 chunk 必须恢复共享 Main 所需的完整前缀。非整 block 的尾部使用私有快照，
key 包含请求实例与 generation，避免同请求 ID 重用或下一 chunk 覆盖上一代快照。
非保存 rank 也会在 partial 转为完整 block 后更新恢复来源。

容量预算按物理 Main 槽与私有 Indexer 的实际字节数计算；runner 分配与 Store 使用同一布局规划。
启用实际复用但缺少 Store receiver 的外部 release waiter 时启动失败，不允许只折叠地址而跳过屏障。

## 3. 建议按这个顺序看代码

下表的路径相对 `src/vllm-ascend-reuse/`；除最后一项，生产模块均在
`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/`。

| 文件 | 主要入口 | 评审重点 |
|---|---|---|
| `layerwise_cache_layout.py` | `MainOnlyCachePlan`、`build_main_only_reuse_layout`、`build_main_only_kv_cache_plan` | 按 spec 识别 Main／Indexer；共享槽、独立层、字节预算和别名一致性 |
| `backend/mooncake_layerwise.py` | `extract_layout_config`、`build_cache_plan`、`get_reuse_memory_info` | 显式 opt-in；拒绝不支持的运行组合 |
| `pool_worker.py` | `_init_layerwise_config`、`assert_reuse_wired`、`prepare_layerwise_step` | Store 消费布局；校验 receiver waiter；每次执行的状态推进 |
| `attention_fence.py` | `AttentionComputeCompletion` | 单次执行的计算完成事件，不把 KV-ready 当成 attention-done |
| `kv_transfer.py` | `KVCacheStoreLayerRecvingThread` | GET／空 GET 前等待旧占用者，异常传播，完成通知顺序 |
| `pool_worker.py` | `_prepare_main_reuse_put_session`、`_open_main_reuse_get_sessions`、`_cleanup_main_private_snapshots` | 完整前缀、partial 快照、非保存 rank 与引用回收 |
| `metadata.py`、`pool_scheduler.py` | `request_instance`、`snapshot_generation` | chunk 间传递快照身份；调度状态延续 |
| `backend/session_tracker.py` | `bind_request_instance`、`register_private_snapshot` | 请求实例绑定、私有快照生命周期 |
| `backend/mooncake_backend.py` | `remove_private_snapshots` | 私有对象清理；不强删受 lease 保护的对象 |
| `vllm_ascend/worker/worker.py` | 复用内存预算入口 | 按物理布局估算可用 blocks，避免重复放大 |

P/D connector 和 MultiConnector 的相关 release 接口来自基线，当前特性没有把它们重写成新协议。
设计与逐入口核对见 [实现核对文档](p-main-buffer-reuse-audit.md)；其中早期段落属于历史记录，以末尾验收结果为准。

## 4. 支持范围与验证边界

当前实现限定 **P producer、V1 runner、eager、PP=CP=1、无 MTP/speculation、单 KV manager group**。
已验收的是 GLM-5.2 / A3 / TP16 × DP2 的目标组合，不代表所有模型或图执行组合均支持。

| 验证项 | 结果 |
|---|---|
| B=1／2／3 的真实模型请求 | 单／双／三 chunk、并发同前缀、重复请求、客户端断连后继续服务均通过 |
| r03 B=2 自动容量 | 32 个 P rank 均为 52042 blocks；Main 4 槽、私有 Indexer 21 |
| r03 实际请求长度 | 535／5017／9656 输入 tokens；不是 262K 或 1M 请求验收 |
| NPU Store sentinel | B=1／2／3，6 层 × 3 轮；PUT 返回后污染源，GET 校验通过 |
| 聚焦 CPU 测试 | 111 项通过；另 205 项 metadata/backend 测试通过 |
| 仓库级 CI | 存在已记录的基线 lint／拼写／工具问题；未宣称全绿，未完成 secret scan |
| P独立1M | 冷prefill1048541 tokens通过，3375.79秒；两次同前缀检索通过 |
| P/D 1M及持续decode | 未验收，不在本轮P独立prefill完成声明内 |

聚焦测试入口：`python3 tools/run_prefill_reuse_ut.py`。使用当前仓库内的隔离 Python 环境，
日志、缓存也写入 `artifacts/p-main-buffer-reuse/`；CPU mock 测试不能替代硬件验收。

## 5. 当前P独立1M验收与交付

当前P运行r05、D保持r03，四引擎最终健康均200。P无运行／等待请求与错误，D无请求流量。
Main B=2、首尾独立，Indexer私有，INT8、eager、chunk32784、max_num_seqs=1。
冷prefill实际1048541 tokens、3375.79秒，ALPHA正确；同一archive的BETA/GAMMA前缀复用分别189.36／189.54秒，均正确。
P0/P1后续新短请求通过。该轮源码`f9b995c68`与运行模块AST一致，重新生成脚本与运行脚本SHA相同。

当前配置：`ansible/profiles/p-main-buffer-reuse-p-only-1m.yaml`。
完整验收边界、复现入口和证据见[P独立1M记录](p-main-buffer-reuse-p-only-1m.md)。
交付包：`artifacts/p-main-buffer-reuse/handoff/p-only-1m-review.tar.gz`，附逐文件SHA清单。
旧1M交付包的源码bundle包含两个新增提交；本次最新bundle包含三个提交，均需已有固定基线。
源码分支已提交且干净。新增P源接口的64K硬件记录见[P源接口验证](p-main-buffer-reuse-pd-source-hardware-validation.md)，当前服务已回到r05，不包含这些新增接口。

## 6. 历史P/D实验与边界

r04 SFA/MemFabric替代连接器实验已停止保留，详见[历史记录](p-main-buffer-reuse-1m-validation.md)。
`RaInit=328002`是崩溃前告警，未证实为根因，不能据此宣称Mooncake传输失败。
P/D transfer connector由同事实现，本轮未开发或替换。原基线容器停止保留，master未动，
没有主机／设备reset或系统服务变更。

持续本地decode未验收：历史r03的32输出请求存在完整Store前缀恢复失败，详见当前P独立记录。
本次完成声明严格限定P独立长prefill与首token，不代表P/D 1M、连续生成或全仓CI完成。
