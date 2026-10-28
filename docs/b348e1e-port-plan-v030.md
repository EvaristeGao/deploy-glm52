# 把上游 `b348e1e`（layerwise reuse + gate 修复）移植到 v0.30.0rc1 的方案

> 结论：**不要整体 cherry-pick**。实测在 v0.30.0rc1（`5f828ad63`）上 cherry-pick 得到
> **20 个 UU + 4 个 DU + 5 个 A**；原因是 v0.30 已经**并行实现了同一个功能**
> （upstream #16747 / #13354），文件被改名/重构。真正缺的只有两块：
> **① 每层一个 attention gate；② load 等待的超时→abort→recompute。**

## 1. 实测（可复现）

```bash
git worktree add -f /tmp/port030b -b port/b348e1e 5f828ad63
cd /tmp/port030b
git fetch --no-tags https://gitcode.com/shichangzhang064/vllm-ascend.git b348e1eca3134113c4d890cd2b97abe150a4fe70
git cherry-pick --no-commit b348e1eca3134113c4d890cd2b97abe150a4fe70   # → 冲突
git status --short | awk '{print $1}' | sort | uniq -c
#   5 A
#   4 DU
#  20 UU
```

- 共同祖先：该 commit 的父提交 `73937568`（2026-08-13）**在** v0.30 历史里；
- 但 8/13 → 10/12 之间 upstream 把相关模块整体重构了，所以冲突集中在「同一个功能的两套实现」。

## 2. 重命名映射（8/13 → v0.30.0rc1）

| 他们的路径（commit 基线） | v0.30 的对应路径 |
|---|---|
| `vllm_ascend/memcache_comm_fence.py` | `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/attention_fence.py` (R100) |
| `…/ascend_store/backend/backend.py` | `…/ascend_store/backend/base.py` (R100) |
| `…/ascend_store/config_data.py` | `…/ascend_store/metadata.py` (R099) |
| `…/ascend_store/layerwise_config.py` | 已删除，逻辑并入 `…/ascend_store/layerwise_cache_layout.py` |
| `…/ascend_store/mooncake_session_tracker.py` | `…/ascend_store/backend/session_tracker.py` (R087) |
| `…/kv_p2p/mooncake_layerwise_to_dram_connector.py` | v0.30 无此文件（能力并入 `mooncake_layerwise_connector.py` / `mooncake_d2rh_connector.py`） |
| `tests/ut/…/test_layerwise_config.py` | v0.30 无此测试 |

## 3. v0.30 已经具备、无需移植

| 能力 | v0.30 落点 |
|---|---|
| `is_kv_save_role` 角色判定 | `ascend_store/metadata.py:44`，被引用 27 处 |
| MultiConnector 的 PD completion 优先排序 | `ascend_multi_connector.py:57/175-201` |
| layerwise transfer 栈（数组化 build、session 跟踪、read lease） | `ascend_store/layerwise_transfer.py`、`backend/session_tracker.py` |
| range 调试 | `ascend_store/range_debug.py` |
| mooncake backend 的 reuse 配置 + save-capable 角色守卫 | `ascend_store/layerwise_config.py`（v0.30 的 `layerwise_cache_layout.py` 旁边） |

## 4. 真正要移植的两个 delta

### D1. 每层一个 attention gate（治我们定位的死锁）

v0.30 现状：`attention_fence.py` 是**全局单 gate**

```python
def reset_attention_compute_start_gate() -> AttentionComputeStartGate   # 无层号
def get_attention_compute_start_gate() -> AttentionComputeStartGate     # 无层号
def record_attention_compute_start() -> None                            # 无层名
```

上游 commit：`memcache_comm_fence.py` 改为 **每层一个 gate**：

```python
_gates: list[AttentionComputeStartGate]; _record_cursor = 0
def _extract_layer_index(layer_name: str) -> int | None
def reset_attention_compute_start_gates(num_layers: int) -> None
def get_attention_compute_start_gate(layer_idx: int) -> AttentionComputeStartGate | None
def record_attention_compute_start(layer_name: str = "") -> None   # 空名时按提交顺序走游标
```

v0.30 需要同步改的调用点（行号为 `5f828ad63`）：

| 文件 | 行 | 现状 |
|---|---|---|
| `pool_worker.py` | 1232 | `reset_attention_compute_start_gate()`（每步重置） |
| `pool_worker.py` | 2754 | `get_attention_compute_start_gate()`（prefetch 任务绑 gate） |
| `pool_worker.py` | 2780 | `reset_attention_compute_start_gate()`（wait 内） |
| `attention/mla_v1.py` | 1460 | `record_attention_compute_start()` |
| `attention/sfa_v1.py` | 2099 | `attention_transfer_window()`（**v0.30 新增的上下文管理器，需要加 layer_name 参数**） |
| `attention/context_parallel/dsa_cp.py` | 1979/1995/2015 | `record_attention_compute_start()` |
| `attention/context_parallel/attention_cp.py` | 612 | 同上 |
| `ops/gdn.py` | 402 | 同上 |
| `ops/kimi_kda.py` | 550 | 同上 |
| `models/glm5next/kda.py` | 344 | 同上 |

> 注：他们的 `attention/utils.py:maybe_record_attention_compute_start(layer_name)` 在 v0.30 不存在，
> 直接改各调用点即可；`attention_transfer_window()` 需要新增可选 `layer_name` 参数并透传。

### D2. load 等待的超时 → abort → recompute（治「永久挂死」）

v0.30 现状：`pool_worker.wait_for_layer_load()` 里是**无限循环**

```python
while not self.layer_load_finished_events[self.current_layer].wait(timeout=10):
    self.kv_recv_thread.raise_if_failed()
    logger.info("Layerwise %d load not done, keep waiting", self.current_layer)
```

上游 commit：10s 超时 → `_record_timed_out_layer_load_blocks(layer)` → `_layer_load_aborted.set()`
→ 再等 10s → 仍不行 `raise TimeoutError`；配合 `kv_load_failure_policy: recompute` 自动重算。

这两个 helper 在 v0.30 **不存在**，需要一起移植：

- `_record_timed_out_layer_load_blocks(self, layer_id)`（他们版本 `pool_worker.py:1274`）
- `_finish_current_mooncake_load_sessions(self)`（他们版本 `pool_worker.py:1339`）
- `mooncake` 分支的 session 收尾（`self.backend_name == "mooncake"`）

## 5. 执行计划（3 个 commit）

| 步骤 | 内容 | 验证 |
|---|---|---|
| C1 | `attention_fence.py` 改为 per-layer + 全部调用点透传 layer 名 | 单测/启动；`GSP 4组×4、并发8` 不再停摆 |
| C2 | `wait_for_layer_load` 超时/abort/recompute + 两个 helper | 注入慢 load 或直接跑同一负载；观察 `TimeoutError`/invalid blocks→重算 |
| C3 | 视需要补 `mooncake backend` 的小改动（+90/-4）与测试 | 全量 32/512 条压测 + 精度 |

验证方式（不改镜像）：沿用现有**只读挂载**机制把改过的 3~5 个文件挂进引擎容器，
用我们已复现的 `GSP 16 条/并发 8` 负载验证，再跑 50%/90% 命中压测。

## 6. 风险

- v0.30 的 `attention_transfer_window()` 是上游新增的抽象，per-layer 化后需要确认
  `on_start/on_finish` 回调与 gate 的配对关系；
- 有些 attention 路径拿不到 layer_name（如 `ops/gdn.py`、CP 路径），需要走 commit 里的
  `_record_cursor` 顺序兜底，可能与 prefetch 提交顺序不一致 → 需要在真机上验证；
- 若上游已有基于 v0.30 的 rebase 分支（如 `exp/rebase25-add-block-20260831`），优先直接取用。
