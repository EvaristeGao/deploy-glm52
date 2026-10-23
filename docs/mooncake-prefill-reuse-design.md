# Mooncake 后端 Prefill 侧 KV Buffer 复用 —— 设计文档（store-only P/D）

| 项 | 值 |
| :--- | :--- |
| **代码基线** | `~/code/python/vllm-ascend` @ `5f828ad63`（v0.30.0rc1），与生产镜像 `glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13` 同版本 |
| **目标后端** | `backend=mooncake`，`LAYERWISE_DATA_PLANE = "block_key"`（`backend/mooncake_layerwise.py:27`） |
| **目标拓扑** | **store-only P/D**：单个 `AscendStoreConnector`，P（`kv_producer`）逐层写 Mooncake 池、D（`kv_consumer` + `consumer_is_to_load: true`）逐层从池读。**无 P2P 直连** |
| **范围** | 仅 **prefill（P 侧）** 的 HBM 物理 buffer 复用；D 侧零改动 |
| **相关既有文档** | `prefill_kvoffload_block_key_reuse_design.md`（接线细节）、`prefill_offload_mooncake_codelevel_design.md`（代码级详版，PD 走 MultiConnector+P2P）、`prefill_kvoffload_two_schemes.md`（收益预估） |

> 本文与既有文档的关系：**复用机制本身**沿用前两份的结论（不重复推导）；本篇补的是
> ① prefill-only 的范围裁剪、② **store-only P/D 的传输契约**（既有文档 §7.2 只覆盖 MultiConnector+P2P）、
> ③ 与本项目实测数据（RoCE、lease TTL、容量/延迟）对齐后的落地参数。

---

## 0. TL;DR

1. **复用机制在当前代码里已经完整存在**，只是 `mooncake_layerwise.extract_layout_config()` 恒 `return None`
   （`backend/mooncake_layerwise.py:77-80`），把 block-key 平面挡在门外。真正要写的代码是
   **「一个显式开关 + 三处门控归属迁移 + 一组防御校验」**，不新增数据面。
2. **store-only P/D 比 MultiConnector+P2P 简单**：D 读的是池对象而不是 P 的 HBM，因此 P 的 buffer 覆写
   与 D **解耦**，**不需要** `external_slot_release_waiter`（该 waiter 由 P2P layerwise 连接器提供）。
   复用只需等「本层 range put 已返回」（同步语义）。
3. **收益是解除 P 侧并发上限**：P 79 层（78 + 1 MTP）→ B=9 时 10~11 个物理 buffer，KV 逻辑容量放大约
   **7.2~7.9x**（641,408 → 约 4.6~5.1M tokens，200K 下并发 3.16 → 约 23~25）；但 **D 侧仍是 697,984 tokens
   （3.44 并发 @200K）**，端到端并发不因本特性提升。
4. **两个前置条件**（否则功能不可用或静默出错）：
   - `default_kv_lease_ttl` 必须覆盖整请求 onload 时长（实测 11s → 128K warm 183.5s；300s → 26.45s）；
   - chunk 边界必须与 `cache_transfer_granularity`（128）对齐（partial block 无法落池，见 §5.2）。
5. **正确性命门是 partial block**：复用后 HBM 只保留最后 B 层，chunk 尾部不满 128 token 的 KV
   既落不了池、又会被覆写 → 下一 chunk 读到空洞。第一版用「启动期强校验 chunk 粒度为 128 的倍数」兜住，
   逐请求对齐/尾部重算作为 Phase 2。

---

## 1. 目标、非目标与收益边界

### 1.1 目标

- P 侧（`kv_producer`）在 **prefill** 期间把 KV 的物理 HBM buffer 从「每层一份」降为「B 份轮转」，
  从而在同一张卡上容纳更长的前缀 / 更多并发请求；
- 复用只改变 **HBM 侧 buffer 管理**，**不改变** pool 对象布局、key 格式、session 语义、D 侧行为。

### 1.2 非目标（第一版不做）

| 非目标 | 原因 |
| :--- | :--- |
| D 侧（decode）复用 | decode 每步都要所有层 KV，复用会退化成「每步从池重载」；decode offload 属另一特性 |
| DCP/PCP > 1 | `validate_topology`（`mooncake_layerwise.py:177`）硬拒；CP 会让 key 需带 CP 坐标 |
| P/D TP mismatch | 同上，硬拒 |
| partial block 落池 | block-key 平面没有 partial key（GVA 平面有），对象必须按完整块内容寻址 |
| SSD offload / 跨实例长期保留 | 属 store 容量议题，见 §4.6 |
| MultiConnector + P2P 逐层投递 | 该组合需要 R2c 的 waiter 配合，见 §4.7；第一版不启用 |

### 1.3 收益边界（必须写清，避免误判）

复用放大的只是 **P 的逻辑 KV 预算**：

| 引擎 | 现状容量 | 复用后（B=9） | 200K 并发上限 |
| :--- | :--- | :--- | :--- |
| P（79 层） | 641,408 tokens / 33.12 GiB | 约 4.6M（含 MTP 排除）/ 5.07M（不排除） | 3.16 → **约 23~25** |
| D（79 层） | 697,984 tokens / 36.05 GiB | **不变** | 3.44 |

> 结论：本特性把「P 先撞墙」变成「D 先撞墙」。要让端到端并发也上去，必须叠加 D 侧 offload（另行立项）。

---

## 2. 现状与门控

### 2.1 两扇门

| 门 | 表达式 | 回答的问题 | 现状 |
| :--- | :--- | :--- | :--- |
| **门 A · 协议 opt-in** | `get_layerwise_reuse_config(...) is not None`（`layerwise_cache_layout.py:76`，内部调 `protocol.extract_layout_config`） | 这个后端**要不要**参与物理 buffer 复用 | mooncake **恒 None**（`:77-80`） |
| **门 B · 数据面判定** | `use_layerwise_transfer = use_layerwise and data_plane == "gva"`（`pool_worker.py:231`、`pool_scheduler.py:82`） | 走哪条数据面代码（GVA / block-key） | mooncake 走 block-key 分支 |

复用部件一半挂在门 A（设计正确）、一半挂在门 B（历史遗留）：

| 部件 | 位置 | 实际门 | 应该是 |
| :--- | :--- | :--- | :--- |
| `apply_layerwise_kv_cache_plan`（合并 tensor 描述符） | `layerwise_cache_layout.py:297` | A | A |
| KV 预算放大 + 日志 | `worker/worker.py:629-647` | A | A |
| `_layerwise_reuse_layout` | `pool_worker.py:490`（`_init_layerwise_config`） | B | A |
| `prefetch_layer_map` / `independent_layers` | `pool_worker.py:611-646` | B | A |
| scheduler 侧复用布局 | `pool_scheduler.py:237-257` | B（外层）+ A（内层） | A |
| `set_external_slot_release_waiter` | `pool_worker.py:722` | B | A（但 store-only 不需要，见 §4.5） |
| `reuse_source = prefetch_layer_map.get(...)` | `pool_worker.py:2748-2750` | A（经 `prefetch_layer_map`） | A |

### 2.2 已经就绪、无需改动的三要素

1. **规划**：`build_layerwise_reuse_layout()`（`layerwise_cache_layout.py:210`）把同 spec 的层分桶，
   `num_shared_buffers` 决定桶内槽位数，产出 `buffer_slots`、`prefetch_layer_map`（层 L 复用层 L−B 的槽位）、
   `has_layer_reuse`；
2. **加载侧门控**：`kv_transfer.py:2043-2060`（`_handle_range_layer_task`）在写共享 buffer 前等
   `layer_save_finished_events[S]` 并 `sync_save_events[S].synchronize()`；
3. **保存侧完成信号**：`kv_transfer.py:1812` 在 range put 结束后置位本层 save 事件。

> 关键推论：**覆写保护是传递性获得的**——层 L 的 attention 写共享 buffer 前，必然经过
> 「等源层 save 完成（+ 加载 L 的前缀）」→ `layer_load_finished_events[L]` → `wait_for_layer_load` 返回。
> 因此不需要额外的「保存侧覆盖门控」。

---

## 3. 实现方式（改动清单）

> 总原则：所有改动收敛在**协议模块 + 门控点**，不侵入传输线程；**默认行为逐字节不变**
> （不配新开关时，mooncake 路径与今天完全一致）。

### R1 · 协议 opt-in（`backend/mooncake_layerwise.py:77-80`）

```python
# 现在
def extract_layout_config(extra_config: dict[str, Any]) -> dict[str, Any] | None:
    """Block-key transfer does not opt into GVA-backed physical reuse."""
    del extra_config
    return None

# 改为
def extract_layout_config(extra_config: dict[str, Any]) -> dict[str, Any] | None:
    """Opt into physical-buffer reuse only when the operator asks for it.

    The reuse planner is backend-agnostic (it only reads KV cache specs); the
    completion signal it needs is produced by the shared save path, not by GVA
    slot accounting.
    """
    if extra_config.get("use_layerwise") and extra_config.get("use_layerwise_buffer_reuse"):
        return extra_config
    return None
```

**为什么用独立开关而不是复用 `use_layerwise`**：该返回值同时驱动「显存预算放大」（`worker.py:629-647`）
与「运行期布局」。无条件返回会让「配了 `layerwise_num_shared_buffers` 但没打算复用」的部署也改变预算。

### R2 · worker 侧门控迁移（`pool_worker.py` 三处）

```python
# R2a  _init_layerwise_config (:490) —— 复用布局对 block-key 也构建
#      现在：if self.use_layerwise_transfer:
#      改为：if self.use_layerwise_reuse_enabled = (get_layerwise_reuse_config(...) is not None)
```

```python
# R2b  运行时状态 (:611-646) —— 只有 has_layer_reuse=True 才接管，否则保持现有 else 分支
if self.use_layerwise_transfer or (
    self.use_layerwise
    and self._layerwise_reuse_layout is not None
    and self._layerwise_reuse_layout.has_layer_reuse
):
    ...   # 原 GVA 分支原样
else:
    ...   # 原 else 分支原样（block-key 非复用默认 prefetch=2）
```

```python
# R2c  set_external_slot_release_waiter (:722) —— store-only 拓扑不需要，但为 MultiConnector 场景
#      把拒绝条件从 `not self.use_layerwise_transfer` 改为 `not self.use_layerwise`
```

> store-only（我们）不启用 R2c：没有 P2P provider，waiter 恒为 None，现有的
> `elif self.external_slot_release_waiter is not None:` 分支自然跳过。

### R3 · scheduler 侧同源开启（`pool_scheduler.py:237-257`）

```python
# 现在：if self.use_layerwise_transfer:
# 改为：if self.use_layerwise:
extra_config = get_layerwise_reuse_config(vllm_config.kv_transfer_config)
if extra_config is not None:
    reuse_layout = build_layerwise_reuse_layout(...)
    self.layerwise_offload = reuse_layout.has_layer_reuse
```

**必须与 R2a 同源同判**：只改一侧会出现「scheduler 认为复用 / worker 认为不复用」，
导致 key 空间分裂、池永不命中且不报错（`hybrid_layout_id` 的 docstring 记录过这类静默 bug）。

### R4 · 防御校验（fail-fast）

`mooncake_layerwise.validate_runtime()`（`:196`）扩参：

```python
def validate_runtime(*, use_hybrid, has_recurrent_state, tp_mismatch,
                     reuse_enabled: bool = False, can_save: bool = True) -> None:
    if tp_mismatch:
        raise ValueError("Mooncake layerwise does not yet support prefill/decode TP mismatch")
    if reuse_enabled and not can_save:
        raise ValueError("Layerwise buffer reuse is prefill-only: kv_role must be able to save")
```

调用点补参：`pool_scheduler.py:192-197`、`pool_worker.py:158-163`（`can_save = is_kv_save_role(self.kv_role, self.consumer_is_to_put)`），
另加三条启动校验：

| 校验 | 条件 | 报错 |
| :--- | :--- | :--- |
| 角色 | 复用开启且 `kv_role` 不能 save | fail-fast |
| decode cache | 复用开启且 `save_decode_cache=True` | fail-fast（语义未定义） |
| CP | 复用开启且 DCP/PCP > 1 | 已有 `validate_topology` 拒绝，补明确文案 |
| P2P 混用 | 复用开启且 MultiConnector 含 `MooncakeConnectorV1` | fail-fast（按块 pull 与复用结构性互斥） |

### R5 · 复用距离 B 与 fence backlog

- 复用距离 `D = layerwise_num_shared_buffers`；
- 门控 stall 的必要条件：`D` 层的前向时间 ≥ 1 层 put 时间；经验公式
  `B ≥ ceil(t_put_layer / t_compute_layer) + 余量`；
- `send_fence_backlog()=8`（`mooncake_layerwise.py:255`）是「attention 边界允许积压的 save 层数」，
  若 `B < 8`，计算流走到 `L+B` 时源层 save 很可能还没做完 → **计算流被门控卡住**；
- **第一版取 B=9**（>8），`send_fence_backlog` 不改；B=8/12/16 作为调参项（见 §7 V6）。

### R6 · GLM 适配

`build_layerwise_reuse_layout()` 在 `has_layer_reuse=True` 时，若发现非 `AttentionSpec`
（Mamba/KDA state、`AscendIndexerKPoolTailSpec` 压缩环）会直接 `NotImplementedError`
（`layerwise_cache_layout.py:273-285`）。

- **我们的 GLM-5.2**（`model_type=glm_moe_dsa`，78 层）：只有 MLA main（576 B/token）与 DSA indexer
  （128 B/token），**无 KDA/Mamba、无 tail spec** → 不需要排除逻辑；
- **风险点**：`indexer_types` 是 full/shared 混合。若 shared 层与 full 层的 indexer spec 不同，
  `_merge_specs()`（`layerwise_cache_layout.py:352-360`）会 `ValueError`。**必须先用 CPU harness 验证分桶**（§7 V0）；
- **glm5next / GLM-5.3（未来）**：需要 R6 的排除逻辑——只让随上下文增长的 group（main+indexer）参与复用，
  tail/KDA group 每层独占 buffer（逻辑上并入 `independent_layers`）。

### 明确不改的部分

| 项 | 原因 |
| :--- | :--- |
| `send_fence_backlog()` / `fence_drains_recv()` | 为重叠调过，改了回归风险最高；第一版用 B>8 规避 |
| `kv_transfer.py` 传输线程 | 门控与 range 收发已就绪 |
| key 格式 / `hybrid_layout_id` | 复用不改变 key 空间（block key 不含层号，层是对象内 offset） |
| `mooncake_backend.py` | session/range 原语足够，无需 GVA 语义 |
| D 侧任何配置 | 复用是 P 专属 |

---

## 4. PD transfer 契约（store-only）

### 4.1 拓扑与时序

```
P（kv_producer，TP16）                                   D（kv_consumer，TP16）
  │  chunk i:                                              │
  │   batch_put_start(keys, size=page×layers)              │   batch_get_start(load_keys)
  │   for L in 0..77:                                      │   for L in 0..77:
  │     batch_put_from_multi_buffer_ranges(                │     batch_get_into_multi_buffer_ranges(
  │       keys, buffers[L], sizes[L], dst_off=L*page)      │       keys, buffers[L], sizes[L], src_off=L*page)
  │   batch_put_end(active_keys)   ← 块对 D 可见           │   … last chunk: batch_get_end(load_keys)
  │                                                        │
  │  HBM: 只有最后 B 层的 buffer 有效，其余已覆写           │  HBM: 全 79 层常驻
```

- P 侧对象：**每 block 一个 object**（MLA 下 `put_step = tp_size`，组内共享 `@{head}`，只有 rank0 写）；
- `size = page_size × num_layers`，层只是对象内 `layer_id × page` 的 byte range；
- 复用**不改变**对象布局、key 格式、session 语义——只改 HBM 侧 buffer 数量。

### 4.2 覆写安全的充分条件

> **INV-1**：层 `L+B` 覆写共享 buffer 前，层 `L` 的 range put 必须已返回。

- `batch_put_from_multi_buffer_ranges` 是**同步**的（`SubmitScatter(...) + operation->wait()`），
  返回即数据已离开源 buffer；
- `commit`（`batch_put_end`）只影响**对其他请求的可见性**，不影响本请求的复用；
- 因此 P 的复用**不需要等 D**——D 读的是池对象，P 后续覆写 HBM 与 D 无关。

### 4.3 为什么 store-only 不需要 slot-release waiter

- `external_slot_release_waiter` 的语义是「告诉 PD 对端：这块 HBM buffer 我已读完，可以覆写」；
- 它的提供方是 **P2P layerwise 连接器**（`kv_p2p/mooncake_layerwise_connector.py:749`
  `supports_layerwise_buffer_reuse = True`，`wait_for_layer_reuse`），由 `ascend_multi_connector.py:76-95` 接线；
- 我们的 `single_store_connector` 拓扑里没有 P2P 对端，D 从池读，**不存在「D 直接读 P 的 HBM」**，
  因此 waiter 恒为 None 是正确状态；
- **何时必须加回来**：改回 `MultiConnector(PD-layerwise + AscendStoreConnector)` 时——D 读的是 P 的 HBM，
  P 覆写前必须等 D 读完（否则读到错数据），此时必须启用 R2c 并验证链路。

### 4.4 lease / TTL 契约

| 项 | 契约 |
| :--- | :--- |
| D 侧读会话 | 每 chunk `batch_get_start` 续约；`batch_get_end` 只在 last chunk |
| TTL 要求 | `default_kv_lease_ttl` **必须覆盖整请求的 onload 时长**（不能只覆盖单层） |
| 我们实测 | `11000`（11s）：128K warm **183.5s**；`300000`（300s）：**26.45s**（6.9x 改善） |
| 失败语义 | 租约过期 → 该 key range get 失败 → 按 load 失败处理（回退重算），不会读到错数据 |
| 建议值 | 300000（按最长请求 onload 时长再留 2x 余量） |

### 4.5 失败与降级

| 事件 | 处理 | 对复用的影响 |
| :--- | :--- | :--- |
| `put_start` 失败 | 该 key 不进本批 `active_keys` | 无（不写不占槽） |
| 某层 range put 失败 | 立即 `batch_put_revoke`，移出 `active_keys`，后续层只传剩余 key | HBM 槽位照常复用；该块不可命中，后续 chunk 重算 |
| `commit` 失败 | 由 Master 超时清理 PROCESSING | 同上（不可命中） |
| range get miss / 过期 | 回退重算该块 | 无（P 侧不受影响） |
| 请求中止 | `layer_load_aborted` + 清理 session | 槽位随请求结束释放 |

> 关键：**复用下「写失败」不会造成静默错数据**——失败的 key 被 revoke，池里不可见；HBM 覆写丢的是
> 已经不可用的数据，代价只是重算。

### 4.6 容量与元数据压力

```
store 容量 ≥ 并发请求数 × 单请求全量 KV
单请求 200K：202,752 × 55,616 B ≈ 11.3 GB（79 层 × 704 B/token）
```

- 复用**不减少** store 侧对象体积（每块仍是 `page × layers`），只减少 HBM；
- 每 chunk 对全前缀做 range get ⇒ 元数据/传输量是 `O(blocks × chunks)`；
  128K/200K 下的实测（warm 26.45s / cold 71.0s）已包含这部分开销；
- 若 store 打满触发 evict：load miss → 重算（性能损失，不是错误），但需实测
  「evict 与在途 session」的交互（Mooncake 可能对已 commit 对象报 `MMC_UNMATCHED_KEY`）。

### 4.7 与 MultiConnector+P2P 的差异

| 维度 | 本篇：store-only | 既有文档 §7.2：MultiConnector+P2P |
| :--- | :--- | :--- |
| D 的数据来源 | Mooncake 池（DRAM segment） | P 的 HBM（逐层 push/pull） |
| P 覆写前置 | 本层 range put 返回 | range put 返回 **+ D 已取走**（`wait_for_layer_reuse`） |
| waiter | 不需要 | 必需（R2c） |
| 禁止 `MooncakeConnectorV1` | 天然不存在 | 必须 fail-fast（按块 pull 会读到已覆写数据） |

---

## 5. 正确性不变量与边界

### 5.1 不变量

- **INV-1**（覆写安全）：层 `L+B` 写共享 buffer 前，层 `L` 的 put 已完成并同步（§4.2）；
- **INV-2**（完成状态单位）：完成信号以**物理 buffer** 为判定单位；block-key 平面复用同一套
  `layer_save_finished_events`（按层号索引，层号与槽位一一映射由 `prefetch_layer_map` 保证）；
- **INV-3**（key 空间不变）：复用不改变 `hybrid_layout_id`（`apply_layerwise_kv_cache_plan` 只改
  `kv_cache_config.kv_cache_tensors`，不碰 `kv_cache_groups`）——这是 block-key 相对 GVA 的结构性优势。

### 5.2 partial block —— 唯一需要额外处理的正确性边界

**场景**：chunk 结束时有 5000 token（128 的倍数 = 4992），尾部 8 token 的 KV：
1. 落不了池（`num_tokens_to_save = 5000//128*128 = 4992`；block-key 无 partial key）；
2. 同 chunk 后半程被层 `L+B` 覆写 → HBM 也没了；
3. 下一 chunk 的前缀 `[0,5000)` 只能 load 到 4992 → 尾部 KV 彻底丢失。

**对照**：

| 配置 | 尾部 token 命运 |
| :--- | :--- |
| 无复用（现行） | buffer 不覆写，KV 留 HBM，下一 chunk 从 5000 继续；问题不存在 |
| 复用 + GVA（memcache） | 用 partial key 落池，可 load 回来 |
| **复用 + block-key（本特性）** | **存不了 + 被覆写 → 必须对齐或重算兜底** |

**第一版落地（D4）**：

1. **启动期强校验**：复用开启时，要求 `max_num_batched_tokens % cache_transfer_granularity == 0`
   （我们 4096 / 16384 均满足），否则 fail-fast——这样**单请求**的 chunk 边界天然对齐；
2. **并发下的风险**：多请求共享 batch 预算时，单请求 chunk 可能提前结束（不对齐）。
   这是 Phase 2 的工作：让 scheduler 把每个请求的 chunk 边界 floor 到 granularity
   （`pool_scheduler.py:622` 已有 `_floor_to_cache_transfer_granularity` 可复用），
   或做「尾部重算」兜底；
3. **文档硬约束**：未完成 Phase 2 前，复用只允许用于「P 的 `max_num_batched_tokens` 为 128 倍数
   且并发请求数受控」的部署。

### 5.3 其他边界

| 情形 | 处理 |
| :--- | :--- |
| 层 L 无 cached prefix | `submit_layer_load` 仍提交空任务承载 `wait_for_save_layer` 门控（`pool_worker.py:2748-2760`，已实现） |
| `independent_layers`（默认 `[0]`） | 不在 `prefetch_layer_map` 中 → 独占 buffer，安全 |
| MTP draft 层 | v1 runner（`model_runner_v1.py:4609`）不排除 → 参与复用；v2 runner（`v2/model_runner.py:374`）`excluded_layer_names=persistent_draft_names` → 独占。两种行为都自洽，但容量系数不同，需 V0 确认 |
| 多 group（MLA main + indexer） | `build_layerwise_reuse_layout` 按 main spec 分桶，indexer 跟随 main 槽位（`:232-247`）；须保证同桶层 indexer spec 一致（§6） |
| 并发写同前缀 | `_filter_pool_existing_keys` + `_put_started_keys` 去重；put_start 被拒只告警，一写赢 |

---

## 6. GLM-5.2 适配

| 项 | 现状 | 结论 |
| :--- | :--- | :--- |
| 模型 | `model_type=glm_moe_dsa`，78 层 + 1 MTP | 79 物理层 |
| KV 组成 | MLA main 576 B/token/层 + DSA indexer 128 B/token/层 | 704 B/token/层；×79 = 55,616 B/token |
| 容量校验 | 641,408 tokens × 55,616 B = 33.2 GiB | 与日志 33.12 GiB 吻合 |
| KDA / Mamba | 无 | 不需要 R6 的 state 排除 |
| tail spec（`AscendIndexerKPoolTailSpec`） | 无（`model_version` 非 `glm5_next`） | 不需要 tail 排除 |
| **indexer full/shared** | `indexer_types` 为 full/shared 混合 | **风险**：同桶层的 indexer spec 可能不同 → `_merge_specs` ValueError；必须 V0 验证 |
| glm5next（未来） | 有 tail + KDA group | 需要 R6 排除逻辑 |

**容量系数（B=9, I=1）**：

```
不排除 MTP：buffers = 1 + 9 = 10  → factor = 79/10 = 7.90 → 641,408 × 7.9  ≈ 5.07M tokens
排除 MTP：  buffers = 1 + 9 + 1 = 11 → factor = 79/11 = 7.18 → 641,408 × 7.18 ≈ 4.61M tokens
```

---

## 7. 验证计划

### 7.1 单测（`tests/ut/distributed/ascend_store/`）

| 编号 | 内容 | 通过标准 |
| :--- | :--- | :--- |
| U1 | `extract_layout_config` 三种输入（无 `use_layerwise` / 有但无 `use_layerwise_buffer_reuse` / 有） | None / None / 配置；且非复用时 factor == 1 |
| U2 | R2/R3 两侧门控一致 | `prefetch_layer_map` 与 `layerwise_offload` 同源同判 |
| U3 | `save_partial_block=True` + block-key 下的块对齐行为 | 与 §5.2 一致 |
| U4 | 非复用 block-key 回归 | prefetch 仍为 2，行为不变 |
| U5 | R4 四条防御校验 | 全部 fail-fast |
| U6 | GLM-5.3 风格四 group：main+indexer 参与、tail/KDA 独占 | 布局与原行为等价 |

### 7.2 准入门槛 V0（不改代码即可跑）

把仓库根目录的 `verify_block_key_reuse.py`（CPU harness，monkeypatch `extract_layout_config`）
改造成我们模型的 spec 集（78 × main + 78 × indexer + 1 MTP），回答：

| 问题 | 判据 |
| :--- | :--- |
| Q1 分桶 | 每物理层 main×1 + indexer×1；无 `extra_main_specs` 落入无人管的分支 |
| Q2 合并丢层 | 合并后所有层仍被某个 descriptor 覆盖（不丢层） |
| Q3 key 空间 | 合并前后 `hybrid_layout_id` 一致 |

> 三项全 PASS 才允许动 R1–R3；任一 FAIL 说明复用规划与我们的 cache spec 不兼容。

### 7.3 实机（A3，4 节点 2P+2D）

| 编号 | 内容 | 命令/判据 |
| :--- | :--- | :--- |
| V1 | 容量与日志 | 日志出现 `Layerwise KV cache reuse maps 79 layers onto N buffer assignments; scale ... by X`；`Available KV cache memory` 与基线比值 ≈ X |
| V2 | 正确性 | 同 prompt 两次输出逐 token 一致；needle 探针；开/关复用对拍（8K/32K/128K） |
| V3 | chunk 边界 | 4096（对齐）通过；故意配 4000（不对齐）应被启动校验拒绝 |
| V4 | 会话/淘汰 | 长请求跨 TTL 的行为；store 打满触发 evict 时降级为重算 |
| V5 | PD | D 侧 `External prefix cache hit rate` 正常、`Range read failed == 0`；P 侧 `wait_for_layer_load` 阻塞时长 |
| V6 | 性能 | 32K/64K/128K 冷热 A/B；B = 8/9/12/16 扫参；对比普通池化（128K 60.5s / 11.1s） |

**日志过滤**（沿用生产口径）：

```bash
grep -a 'Layerwise KV cache reuse maps'            vllm_p0.log | tail -1
grep -a 'Available KV cache memory'               vllm_p0.log | tail -1
grep -aE 'Range (write|read) failed|Segfault'     vllm_p*.log vllm_d*.log | tail
grep -a 'External prefix cache hit rate'          vllm_d*.log | tail -3
```

---

## 8. 风险与开放问题

| # | 风险 | 等级 | 缓解 |
| :--- | :--- | :--- | :--- |
| K1 | **partial block 丢 KV**（§5.2） | **高（正确性）** | 第一版启动校验 + 部署约束；Phase 2 对齐/兜底 |
| K2 | **indexer full/shared spec 不一致** → 复用规划 ValueError | 中 | V0 CPU harness 先验证 |
| K3 | 时序硬化：put 完成进入关键路径，Mooncake 尾延迟传导 TTFT | 中 | B=9 起步（> backlog 8）；V6 实测 |
| K4 | store eviction 与在途 session 交互（无 lease 的 block-key） | 中 | V4；容量规划 + 开 SSD offload |
| K5 | 每 chunk 对全前缀 range get，master QPS `O(blocks×chunks)` | 中 | V6 观测；必要时调 `layerwise_max_transfer_blocks` |
| K6 | 逆上游意图（`extract_layout_config` docstring 明确排除 reuse） | 中 | 提 issue/RFC 对齐；把 U1–U6/V1–V6 纳入 CI |
| K7 | 上游 KVPP 系列正在重构 layerwise 管线，落点会漂移 | 中 | 改动收在协议模块 + 门控点，diff 面积最小 |
| K8 | lease TTL 配置缺失 → warm 退化（已实测 6.9x） | **高（部署）** | 硬性配置契约：300000 |
| K9 | D 侧仍为瓶颈，端到端并发不提升 | — （预期内） | §1.3 明示；需另立 D 侧 offload |

---

## 9. 里程碑与工作量

| 阶段 | 内容 | 估算 |
| :--- | :--- | :--- |
| **Phase 1** | R1–R4 + U1–U5 + V0–V2（打通 P 侧复用、正确性对拍） | 1.5–2 人周 |
| **Phase 2** | chunk 对齐（或尾部重算兜底）+ R6 + 调参（B、fence） | 1–2 人周 |
| **Phase 3** | V4–V6（evict/压测/性能）+ 上线评审 | 1–1.5 人周 |

前置依赖（不计入）：GLM-5.2 的 indexer spec 与复用规划兼容（V0）；lease TTL 与 RoCE 配置就绪（已有）。

---

## 10. 本仓库落地配置

### 10.1 P / D 配置差异（只 P 侧加开关）

```jsonc
// P（kv_producer）
"kv_connector_extra_config": {
  "lookup_rpc_port": "0",
  "backend": "mooncake",
  "use_layerwise": true,
  "use_layerwise_buffer_reuse": true,      // ★ 新增：opt-in
  "layerwise_num_shared_buffers": 9,       // ★ B=9（> send_fence_backlog=8）
  "layerwise_prefetch_layers": 2,
  "layerwise_max_transfer_blocks": 64,
  "layerwise_max_transfer_bytes": 16777216
}

// D（kv_consumer）：保持现状，不加 use_layerwise_buffer_reuse
```

Ansible 落点：`ansible/inventories/a3/group_vars/all.yml` 的 `_1m_v030_hyb.pd_cluster.layerwise`，
模板 `ansible/templates/run_dp_prefill_template.sh.j2` 的 `single_store_connector` 分支
（`{% if lw.enabled | default(true) %}` 段内追加两个可选键，D 模板不加）。

### 10.2 必须同时满足的部署前置

| 项 | 值 | 说明 |
| :--- | :--- | :--- |
| `default_kv_lease_ttl` | **300000** | 11s 实测导致 128K warm 183.5s（vs 26.45s） |
| RoCE | `HCCL_INTRA_ROCE_ENABLE=1` + `HCCL_INTRA_PCIE_ENABLE=0` + `hixl_preset: roce` | 不开启会退到 ACL IPC，全量 `-800` + segfault（已验证） |
| `use_layerwise` | true | 复用是 layerwise 的增强 |
| P chunk 粒度 | 4096 / 16384（128 的倍数） | §5.2 的正确性要求 |
| 拓扑 | `single_store_connector`（无 P2P） | 本篇结论只对 store-only 成立 |

---

## 附录 A：代码锚点索引（基线 `5f828ad63`）

| 关注点 | 位置 |
| :--- | :--- |
| block_key 平面声明 | `backend/mooncake_layerwise.py:27` |
| **复用 opt-in（R1）** | `backend/mooncake_layerwise.py:77-80` |
| 拓扑/运行时校验 | `backend/mooncake_layerwise.py:177` / `:196` |
| `hybrid_layout_id`（key 空间） | `backend/mooncake_layerwise.py:215` |
| `send_fence_backlog` | `backend/mooncake_layerwise.py:255` |
| opt-in 判定入口 | `layerwise_cache_layout.py:76` |
| 布局规划（GVA 版） | `layerwise_cache_layout.py:128` |
| **复用布局（slot/prefetch）** | `layerwise_cache_layout.py:210` |
| **合并 tensor 描述符** | `layerwise_cache_layout.py:297` |
| 连续页校验 | `layerwise_cache_layout.py:347` |
| 模式判定（GVA） | `pool_worker.py:231` |
| 复用状态构建（R2） | `pool_worker.py:490` / `:611-646` |
| slot waiter 注册（R2c） | `pool_worker.py:722` |
| 加载任务提交（门控传递） | `pool_worker.py:2748-2760` |
| scheduler 复用布局（R3） | `pool_scheduler.py:237-257` |
| chunk 粒度与 floor 工具 | `pool_scheduler.py:147` / `:622` |
| KV 预算放大 + 日志 | `worker/worker.py:629-647` |
| 布局应用（v1 / v2） | `worker/model_runner_v1.py:4609` / `worker/v2/model_runner.py:374` |
| 加载侧门控实现 | `kv_transfer.py:2043-2060` |
| 保存完成信号 | `kv_transfer.py:1812` |
| P2P waiter 提供方 | `kv_p2p/mooncake_layerwise_connector.py:749` |
| MultiConnector 接线 | `ascend_multi_connector.py:76-95` |
