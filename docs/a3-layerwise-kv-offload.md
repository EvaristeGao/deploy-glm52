# A3 Mooncake 分层 KV offload（layerwise / hybrid attention）

对应官方文档（v0.30.0rc1，developer preview）：
- [Mooncake Layerwise Hybrid Attention](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/mooncake_hybrid_attention.html)
- [Mooncake Layerwise Adaptation and Optimization](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/mooncake_layerwise_adaptation_and_optimization.html)
- [Layerwise and Sparse KV Cache Offloading](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/layerwise_and_sparse_kv_cache_offloading.html)

本项目实现：`ansible/inventories/a3/group_vars/all.yml` 的 `deploy_config: 1m-v030-lw`。

---

## 1. 它是什么

**分层（layerwise）KV 传输**：把一个 block 的**所有层**打包成 **1 个**远端对象，每层算完只 PUT 该层的
byte range，用 session 协议（`put_session_start` → 逐层 range put → `put_session_end`）保证"提交后才可读"。

- 远端对象数从 `B × L × R` 降到 `B × R`（B=block、L=层、R=storing rank），
  Mooncake 的 metadata 对象 / key / session / exists 查询项都等比下降；**总 KV 数据量不变**
- 传输时机改为挂在**每层的 attention 计算窗口**上：`cache-ready NPU event` 之后才 PUT/GET，
  与后续 output-projection / MoE 通信重叠（默认最多 8 个发送任务不排空，属内部调度策略）

**hybrid attention 扩展（这份文档的重点）**：支持**多个 KV-group**——DeepSeek-V4 那类混合注意力里
sliding-window KV / compressed KV / indexer cache / compressor state 各属不同 group，各有自己的
block 大小与提交边界。key 命名空间变成：

```
model@mooncake_hybrid_v1:<layout-digest>@group:<id>@block:<size>@<hash>@<head>
```

**镜像里的落点**（已确认存在）：
`vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_layerwise.py`
（`LAYERWISE_DATA_PLANE = "block_key"`，含 `mooncake_hybrid_v1` 的 key 构造）、
`layerwise_cache_layout.py`、`pool_worker.py` / `pool_scheduler.py`；
`backend/__init__.py` 里 `backend: "mooncake"` 的 layerwise protocol 就指向它 ——
所以 **`backend: "mooncake"` + `use_layerwise: true` 走的就是 block_key/hybrid 这条**，不需要额外键。

---

## 2. 前置条件（缺一不可）

| # | 条件 | 现状 |
|---|---|---|
| 1 | **mooncake client 必须带 PR #2881 的 7 个接口**：`batch_put_session_start`、`batch_put_from_multi_buffer_ranges`、`batch_put_session_end`、`batch_put_session_revoke`、`batch_get_session_start`、`batch_get_into_multi_buffer_ranges`、`batch_get_session_end` | ❌ **镜像里现有的 mooncake 包没有**（python wrapper 未暴露、`.so` 符号计数为 0）；vllm-ascend 启动时会 fail fast 报 `Mooncake client does not support batch_*`。需要自己打一个带 PR #2881 的镜像 |
| 2 | p0 上有 mooncake_master（本项目 kvpool 单主）+ `mooncake.json` | ✅ 已有（`mooncake-p0` 容器） |
| 3 | 官方标注 **real-NPU E2E 尚未完成**（只有 CPU mock 285 例 + 静态检查），"No latency or throughput improvement is claimed without hardware measurements" | ⚠️ 属 developer preview，别当生产特性 |

---

## 3. 三条硬约束（镜像源码，`use_layerwise` 时启动即校验）

| 约束 | 出处 | 违反后果 | 本档处理 |
|---|---|---|---|
| **CP/DCP 必须 = 1** | `mooncake_layerwise.validate_topology()`：`prefill_context_parallel_size`/`decode_context_parallel_size` > 1 直接 `ValueError("...supports TP/PP, but not context parallelism")` | 启动失败 | 本档**不渲染任何 CP 参数**（无 `--prefill-context-parallel-size`/`--decode-context-parallel-size`/`--cp-kv-cache-interleave-size`），也不给 `context_parallel_size` / `cp_kv_cache_interleave_size` 键 |
| **P/D 的 TP 必须一致** | `validate_runtime()`：`Mooncake layerwise does not yet support prefill/decode TP mismatch`；PD 分离下 producer/consumer 的 dcp/pcp 也必须一致（否则 pool 里 layerwise GVA shard 布局错位） | 启动失败 | P/D **都 TP8** |
| **先 eager 验证** | 文档："Start validation in eager mode. Graph-mode execution and additional attention backends require separate integration validation." | 未验证路径 | decode `enforce_eager: true` + `async_scheduling: false` → 渲染 `--enforce-eager`，**不渲染** `--compilation-config` |

> 顺带：`validate_pp_groups()` 还要求 PP 的每个 stage 在每个 KV group 里都有层（本档无 PP，不受影响）；
> layerwise 与 **TP mismatch** 也不兼容（本档 TP 一致）。

---

## 4. `1m-v030-lw` 与 `1m-v030` 的差异

| 项 | `1m-v030`（已验证跑通） | `1m-v030-lw`（本档） |
|---|---|---|
| prefill dp/tp/local | 2 / 16 / 1 | **4 / 8 / 2** |
| decode dp/tp/local | 4 / 8 / 2 | 4 / 8 / 2（不变） |
| P/D TP 是否一致 | 16 vs 8 ❌ | **8 vs 8 ✅** |
| CP / DCP / interleave | 1 / 16 / 8 / 128 | **全部不渲染（=1）** |
| prefill `additional_config` | 含 `enable_dsa_cp: true` | **去掉 `enable_dsa_cp`**（CP=1 时无意义），其余不变 |
| prefill `--max-num-batched-tokens` | 8192 | **4096**（layerwise 文档示例值） |
| prefill `--enable-chunked-prefill` | 不渲染 | **渲染**（layerwise 的 chunk 续传语义需要它） |
| decode 图模式 | `--compilation-config FULL_DECODE_ONLY` + `--async-scheduling` | **`--enforce-eager`**，两者都不渲染 |
| `AscendStoreConnector` extra | `{lookup_rpc_port, backend}`（+decode `load_async`） | 额外注入 **`use_layerwise` / `layerwise_prefetch_layers` / `layerwise_max_transfer_blocks` / `layerwise_max_transfer_bytes`** |
| 其余（v030 env 集、int8 KV、prefetch、additional-config 的 fused_mc2/flashcomm1/mlapo/mega_moe、kv_port、api-server-count…） | —— | 与 `1m-v030` 一致 |

配置形态（`group_vars`）：

```yaml
_1m_v030_lw:
  pd_cluster:
    max_model_len: 1048576
    v030_env: true
    layerwise: { enabled: true, prefetch_layers: 2,
                 max_transfer_blocks: 64, max_transfer_bytes: 16777216 }
    prefill: { dp_size: 4, tp_size: 8, dp_size_local: 2, ..., enable_chunked_prefill: true }
    decode:  { dp_size: 4, tp_size: 8, dp_size_local: 2, ..., enforce_eager: true, async_scheduling: false }
```

渲染结果（已由单测锁定）：

```json
"kv_connector_extra_config": {
  "lookup_rpc_port": "0", "backend": "mooncake",
  "use_layerwise": true, "layerwise_prefetch_layers": 2,
  "layerwise_max_transfer_blocks": 64, "layerwise_max_transfer_bytes": 16777216
}
```

---

## 5. 怎么跑（4 节点 2P+2D）

```bash
cd ansible
# 0) 先把带 PR #2881 的 mooncake 镜像打到节点上，并把 group_vars 的 image / mooncake.image 指过去
# 1) 切档
#    inventories/a3/group_vars/all.yml:  deploy_config: 1m-v030-lw
.venv/bin/python simulate_modes.py --inv a3                      # 先看渲染
ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/check.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/status.yml
```

预期形状：P 2 节点 × 2 引擎（9081/9082）+ D 2 节点 × 2 引擎（9900/9901），
`wait_ready` 探测目标 9（P4 + D4 + proxy）。

调试开关（排查每层 range/commit 时用，默认关，开了有每层日志开销）：

```yaml
# 需要时把引擎容器的 env 加上（当前模板未渲染该变量，属手工排查手段）
export VLLM_ASCEND_KVPOOL_RANGE_DEBUG=1
```

---

## 6. 最小验证：先单节点 co-located（推荐第一步）

正式跑 4 节点之前，用 `ansible/layerwise_smoke_single_node.sh` 在**一个节点**上跑
**单实例 co-located**（`kv_role: kv_both`、TP8、eager、`prefetch_layers=1`）——正好绕开三条硬约束。

```bash
# 在节点上（或 ssh 过去）执行；IMAGE 换成你打好 PR #2881 的镜像
IMAGE=<your-image-with-mooncake-PR2881> ./ansible/layerwise_smoke_single_node.sh
```

> 官方说"首次硬件验证从 TP=1 开始"，但 GLM-5.2 单卡放不下，所以用本模型单节点最小的 TP8。
> 如果脚本报 `Mooncake client does not support batch_put_session_start`，就是镜像的 mooncake
> client 还没带 PR #2881（见 §2）。

**为什么不做成 Ansible 档**：本套件是 P/D 分离架构（`launch_online_dp.py` + 按 role 渲染模板 +
proxy），单实例 `kv_both` 的 co-located 形态不在其中，所以要单独脚本跑。需要的话可以再补一个
inventory + playbook，但那是另一块工作。

---

## 7. 验收清单（官方要求的那几条）

| 检查 | 做法 |
|---|---|
| 冷/温 token 一致性 | 同一长 prompt 连发两次，第 2 次 prefix cache 命中，两次输出 token 完全一致 |
| remote hit 为正 | 看日志/metrics 里远端命中计数 > 0（不是全 0） |
| 多个 chunk 边界 | prompt 长度跨多个 `max-num-batched-tokens`（单节点脚本是 4096）；确认每个 chunk 都能续上已提交的层 |
| 每个 group 都有有效 load range | 打开 `VLLM_ASCEND_KVPOOL_RANGE_DEBUG=1`，对照 range-debug 日志与 device trace（尤其 sparse state / indexer group） |
| PUT/GET 落在 cache-ready 事件之后、后续 HCCL 之前 | 看 device trace 的 stream/rank（layerwise 的传输尾会和 output-projection / MoE 通信重叠，这是它明确承认的风险） |
| TTFT/吞吐/传输尾 vs 非 layerwise | 同模型、同 TP、同 prompt、同 pool 条件下与非 layerwise（`1m-v030`）对比；测性能时关掉 range-debug 与 profiling |

---

## 8. 风险与回退

- **实验性**：官方写明 real-NPU E2E 未完成、不对性能做任何承诺；单 group 版还有 P0 待办
  （pin mooncake commit、key 里加 layout fingerprint、真实 NPU 故障注入测试）
- **传输尾与 HCCL/MoE 通信竞争**：默认策略允许最多 8 个发送任务不排空，短 attention kernel 下
  传输尾可能明显；必须用 device trace 验证
- **graph mode 未验证**：本档 decode 用 eager；若要用图模式需另行集成验证（会改 `enforce_eager`）
- **回退**：把 `deploy_config` 改回 `1m-v030` 重跑 `gen` + `start` 即回到已验证的形状
  （`1m-v030` 的实机状态见 `a3-v030rc1-vs-official.md` §0）
