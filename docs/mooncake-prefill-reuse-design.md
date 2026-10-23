# Mooncake 后端 Prefill 侧 KV Buffer 复用 —— 设计文档（D2D P/D）

| 项 | 值 |
| :--- | :--- |
| **代码基线** | `~/code/python/vllm-ascend` @ `5f828ad63`（v0.30.0rc1），与生产镜像 `glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13` 同版本 |
| **目标后端** | `backend=mooncake`，`LAYERWISE_DATA_PLANE = "block_key"`（`backend/mooncake_layerwise.py:27`） |
| **PD 交接** | **D2D（P→D 点对点）**，池（AscendStore + Mooncake）只承担跨请求 prefix cache，**不作为** P/D 交接通道 |
| **目标拓扑** | P：`MultiConnector[PD 逐层连接器(producer), AscendStoreConnector(producer, mooncake, use_layerwise)]`<br>D：`MultiConnector[PD 逐层连接器(consumer), AscendStoreConnector(consumer, consumer_is_to_load)]` |
| **范围** | 仅 **prefill（P 侧）** 的 HBM 物理 buffer 复用；D 侧 KV 布局零改动 |
| **相关既有文档** | `prefill_kvoffload_block_key_reuse_design.md`（接线细节）、`prefill_offload_mooncake_codelevel_design.md`（代码级详版）、`prefill_kvoffload_two_schemes.md`（收益预估） |

> 与既有文档的关系：复用机制沿用前两份的结论（不重复推导）。本篇的增量是
> ① **D2D 为 PD 主路径**（池不参与交接）后的双消费者门控与 waiter 链路；
> ② 与本项目实测（RoCE、lease TTL、容量/延迟）对齐的落地参数；
> ③ 把「哪些部分被证实、哪些仍待验证」写清。

---

## 0. TL;DR

1. **复用机制在当前代码里已经完整存在**，唯一堵点是 `mooncake_layerwise.extract_layout_config()` 恒 `return None`
   （`backend/mooncake_layerwise.py:77-80`）。要写的代码是「一个显式开关 + 门控归属迁移 + 防御校验」，
   不新增数据面。
2. **PD 交接走 D2D**（走池太慢）：P 每层算完就把 KV 点对点推给 D 的 HBM
   （`MooncakeLayerwiseConnector`），或由 D 从 P 的 HBM 拉走（`SfaRemoteD2HConnector`）。
   池只负责**跨请求 prefix cache**：P 仍把每层 range put 进 Mooncake，D 命中时仍从池读前缀。
3. 于是 P 的共享 buffer 有**两个消费者**：**PD 推送**和**池写入**。槽位覆写前必须两者都完成：
   - PD 侧：`wait_for_layer_reuse(layer)` → 等发送线程的 `storage_send_done_events[slot]`；
   - 池侧：`batch_put_from_multi_buffer_ranges` 返回（同步语义，数据已离开 HBM）。
   ⇒ **R2c（把 slot-release waiter 接到 block-key 平面）从"可选"变成"必需"**。
4. **收益是解除 P 侧并发上限**：P 79 层（78 + 1 MTP）→ B=9 时 10~11 个物理 buffer，KV 逻辑容量放大约
   **7.2~7.9x**（641,408 → 约 4.6~5.1M tokens，200K 下并发 3.16 → 约 23~25）；D 仍是 697,984 tokens
   （3.44 并发 @200K），端到端并发不因本特性提升。
5. **两个硬前置**：
   - `default_kv_lease_ttl` ≥ 单请求 onload 时长（实测 11s → 128K warm 183.5s；300s → 26.45s）；
   - chunk 边界与 `cache_transfer_granularity`(128) 对齐（partial block 无法落池，见 §5.2）。
6. **两个准入门槛**：partial block（正确性）；连接器选择（兼容性）——官方文档只把
   `SfaRemoteD2HConnector` 列为"prefill offload + reuse"的支持连接器，`MooncakeLayerwiseConnector`
   虽在代码里声明 `supports_layerwise_buffer_reuse = True`（`kv_p2p/mooncake_layerwise_connector.py:749`），
   但属于**未验证组合**（§4.1/§8）。

---

## 1. 目标、非目标与收益边界

### 1.1 目标

- P 侧（`kv_producer`）在 prefill 期间把 KV 的物理 HBM buffer 从「每层一份」降为「B 份轮转」，
  同卡容纳更长的前缀 / 更多并发请求；
- 复用只改变 **HBM 侧 buffer 管理**：**不改变** pool 对象布局、block key 格式、session 语义；
- PD 交接保持 D2D 直传（不经池）：交接落点是 **D 的 HBM**，与 P 的 buffer 覆写相对独立，
  但 P 的覆写必须等 PD 推送完成（§4.4）。

### 1.2 非目标（第一版不做）

| 非目标 | 原因 |
| :--- | :--- |
| D 侧（decode）复用 | decode 每步都要所有层 KV，复用会退化成「每步从池重载」；decode offload 属另一特性 |
| DCP/PCP > 1 | `validate_topology`（`mooncake_layerwise.py:177`）硬拒；CP 需给 block key 加坐标 |
| P/D TP mismatch | 同上硬拒；D2D 连接器另有「P TP ≥ D TP 且整除」的约束 |
| partial block 落池 | block-key 平面没有 partial key（GVA 有），对象必须按完整块内容寻址 |
| 池作为 P/D 交接通道 | 延迟/带宽不可接受，列为对照方案（§4.7） |
| `MooncakeConnectorV1` 按块 pull | 与复用结构性互斥（请求算完才 pull，早期层已被覆写），必须 fail-fast |

### 1.3 收益边界（避免误判）

| 引擎 | 现状容量 | 复用后（B=9） | 200K 并发上限 |
| :--- | :--- | :--- | :--- |
| P（79 层） | 641,408 tokens / 33.12 GiB | 约 4.6M（含 MTP 排除）/ 5.07M（不排除） | 3.16 → **约 23~25** |
| D（79 层） | 697,984 tokens / 36.05 GiB | **不变** | 3.44 |

> 本特性把「P 先撞墙」变成「D 先撞墙」；端到端并发提升需叠加 D 侧 offload（另行立项）。

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
| `set_external_slot_release_waiter` | `pool_worker.py:722` | B | **A（D2D 必需）** |
| `reuse_source = prefetch_layer_map.get(...)` | `pool_worker.py:2748-2750` | A（经 `prefetch_layer_map`） | A |

### 2.2 已经就绪、无需改动的三要素

1. **规划**：`build_layerwise_reuse_layout()`（`layerwise_cache_layout.py:210`）按 main spec 分桶，
   `num_shared_buffers` 决定桶内槽位数，产出 `buffer_slots`、`prefetch_layer_map`（层 L 复用层 L−B 的槽位）；
2. **加载侧门控**：`kv_transfer.py:2043-2060` 在写共享 buffer 前等 `layer_save_finished_events[S]` 并
   `sync_save_events[S].synchronize()`；
3. **保存侧完成信号**：`kv_transfer.py:1812` 在 range put 结束后置位本层 save 事件。

> 覆写保护是传递的：层 L 的 attention 写共享 buffer 前，必经「等源层 save 完成（+ 加载 L 的前缀）」→
> `layer_load_finished_events[L]` → `wait_for_layer_load` 返回。**store-only 下这就够了；D2D 下还差一步
> ——等 PD 把源层推送给 D 完成（§4.4）。**

---

## 3. 实现方式（改动清单）

> 总原则：改动收敛在协议模块 + 门控点，不侵入传输线程；**默认行为逐字节不变**
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
    """Opt into physical-buffer reuse only when the operator asks for it."""
    if extra_config.get("use_layerwise") and extra_config.get("use_layerwise_buffer_reuse"):
        return extra_config
    return None
```

用独立开关而不是复用 `use_layerwise`：返回值同时驱动「显存预算放大」（`worker.py:629-647`）与运行期布局，
无条件返回会让「配了 `layerwise_num_shared_buffers` 但没打算复用」的部署也改变预算。

### R2 · worker 侧门控迁移（`pool_worker.py` 三处）

```python
# R2a  _init_layerwise_config (:490)：复用布局对 block-key 也构建
#      现在：if self.use_layerwise_transfer:
#      改为：if get_layerwise_reuse_config(...) is not None:
```

```python
# R2b  运行时状态 (:611-646)：仅 has_layer_reuse=True 才接管，否则保持现有 else 分支
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
# R2c  set_external_slot_release_waiter (:722) —— D2D 复用必需
#      现在：if not self.use_layerwise_transfer: return False
#      改为：if not self.use_layerwise: return False
```

> R2c 的意义：D2D 下 P 的槽位要被层 L+B 覆写，必须确认层 L 已被推送给 D 且 D 已收完。
> `MultiConnector` 会把 PD producer 的 `wait_for_layer_reuse` 包装成
> `_wait_for_external_slot_release(layer_idx)` 注册进来（`ascend_multi_connector.py:85-95`），
> store connector 接单后由 recv 线程在覆写前调用（`kv_transfer.py:2060`）。
> 不接单时退化为 `MultiConnector.wait_for_layer_load` 里的逐 provider 等待（可用但更慢，需实测）。

### R3 · scheduler 侧同源开启（`pool_scheduler.py:237-257`）

```python
# 现在：if self.use_layerwise_transfer:
# 改为：if self.use_layerwise:
extra_config = get_layerwise_reuse_config(vllm_config.kv_transfer_config)
if extra_config is not None:
    reuse_layout = build_layerwise_reuse_layout(...)
    self.layerwise_offload = reuse_layout.has_layer_reuse
```

必须与 R2a 同判：只改一侧会出现「scheduler 认为复用 / worker 认为不复用」，key 空间分裂、
池永不命中且不报错。

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

调用点补参：`pool_scheduler.py:192-197`、`pool_worker.py:158-163`（`can_save = is_kv_save_role(...)`），另加：

| 校验 | 条件 | 动作 |
| :--- | :--- | :--- |
| 角色 | 复用开启且 `kv_role` 不能 save | fail-fast |
| decode cache | 复用开启且 `save_decode_cache=True` | fail-fast |
| CP | 复用开启且 DCP/PCP > 1 | 已有 `validate_topology` 拒绝，补明确文案 |
| P2P 块拉 | MultiConnector 内含 `MooncakeConnectorV1` | fail-fast（§4.5） |

### R5 · 复用距离 B 与 fence backlog

- 复用距离 `D = layerwise_num_shared_buffers`；
- 不 stall 的必要条件：`D` 层的前向时间 ≥ 1 层的「PD push + 池 put」时间；
- `send_fence_backlog()=8`（`mooncake_layerwise.py:255`）是 attention 边界允许积压的 save 层数，
  若 `B < 8`，计算流走到 `L+B` 时源层很可能还没发完 → 计算流被门控卡住；
- **第一版取 B=9**（>8），`send_fence_backlog` 不改；B=8/12/16 作为调参项（§7 V6）。

### R6 · GLM 适配

`build_layerwise_reuse_layout()` 在 `has_layer_reuse=True` 时遇到非 `AttentionSpec`
（Mamba/KDA state、`AscendIndexerKPoolTailSpec` 压缩环）会 `NotImplementedError`
（`layerwise_cache_layout.py:280-285`）。

- **我们的 GLM-5.2**（`model_type=glm_moe_dsa`，78 层）：只有 MLA main（576 B/token）与 DSA indexer
  （128 B/token），**无 KDA/Mamba、无 tail spec** → 不需要排除逻辑；
- 风险点：`indexer_types` 是 full/shared 混合；若 shared 层与 full 层的 indexer spec 不同，
  `_merge_specs()`（`:357-361`）会 `ValueError` → **必须先用 CPU harness 验证**（§7 V0）；
- **glm5next / GLM-5.3（未来）**：只让随上下文增长的 group（main+indexer）参与复用，
  tail/KDA group 每层独占 buffer。

### 明确不改的部分

| 项 | 原因 |
| :--- | :--- |
| `send_fence_backlog()` / `fence_drains_recv()` | 为重叠调过；第一版用 B>8 规避 |
| `kv_transfer.py` 传输线程 | 门控与 range 收发已就绪（D2D 的 waiter 调用点位已存在：`:2060`） |
| block key 格式 / `hybrid_layout_id` | 复用不改变 key 空间（层是对象内 offset） |
| `mooncake_backend.py` | session/range 原语足够 |
| D 侧 KV 布局 | 复用是 P 专属 |

---

## 4. PD transfer（D2D）

### 4.1 连接器选型

| 方案 | 连接器 | 交接方式 | reuse 支持 | 官方文档 | 对 D 的要求 | 依赖 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **A（推荐）** | `MooncakeLayerwiseConnector` | P 逐层 **push** 到 D 的 HBM | `supports_layerwise_buffer_reuse=True`（`:749`）+ `wait_for_layer_reuse`（`:831`） | ❌ 未列入 offload 支持清单 | D 保持现有全层 HBM 布局，**无需改 decode** | 镜像自带 `mooncake_layerwise_connector.py` |
| **B（文档支持）** | `SfaRemoteD2HConnector` | D 从 P 的 HBM **pull**（memfabric/SDMA） | `supports_layerwise_buffer_reuse=True`（`sfa_pd_rd2h/connector.py:58`）+ `wait_for_layer_reuse`（`:303`） | ✅ 官方列为 prefill/decode offload 唯一支持连接器 | D 需 `sparse_kv_offload_config.enabled=true`，主 KV 必须 BF16（indexer 可 int8） | `memfabric_hybrid`（镜像已装 1.2.0）；P TP ≥ D TP 且整除 |
| **C（禁止）** | `MooncakeConnectorV1` | 请求算完后按块 pull | ❌ | — | — | 与复用结构性互斥（§4.5） |

**推荐 A**：不改 D 的 decode 内存布局（我们 D 现在是 int8 主 KV、全层驻留），改造面最小；
但要接受"文档未验证"的风险，并把 V5 的 waiter 链路验证作为准入门槛。
若 A 验证失败，退到 B（需要 D 侧切 BF16 + sparse offload，属额外改动）。

### 4.2 配置形状

```jsonc
// P（kv_producer）
"kv_connector": "MultiConnector",
"kv_connector_extra_config": {"connectors": [
  { "kv_connector": "MooncakeLayerwiseConnector", "kv_role": "kv_producer",
    "kv_connector_extra_config": { "engine_id": "0", "kv_port": "26001" } },
  { "kv_connector": "AscendStoreConnector", "kv_role": "kv_producer",
    "kv_connector_extra_config": {
      "lookup_rpc_port": "0", "backend": "mooncake",
      "use_layerwise": true,
      "use_layerwise_buffer_reuse": true,     // ★ R1
      "layerwise_num_shared_buffers": 9,      // ★ B=9（> backlog 8）
      "layerwise_prefetch_layers": 2,
      "layerwise_max_transfer_blocks": 64,
      "layerwise_max_transfer_bytes": 16777216
    } }
] }

// D（kv_consumer）
"kv_connector": "MultiConnector",
"kv_connector_extra_config": {"connectors": [
  { "kv_connector": "MooncakeLayerwiseConnector", "kv_role": "kv_consumer",
    "kv_connector_extra_config": { "engine_id": "0", "kv_port": "26001" } },
  { "kv_connector": "AscendStoreConnector", "kv_role": "kv_consumer",
    "kv_connector_extra_config": {
      "lookup_rpc_port": "0", "backend": "mooncake",
      "use_layerwise": true, "consumer_is_to_load": true
      // 不加 use_layerwise_buffer_reuse
    } }
] }
```

### 4.3 时序（P 侧两阶段 save）

`AscendMultiConnector` 已保证顺序（`ascend_multi_connector.py:144-168`）：

```
compute layer L
  → on_kv_cache_written(L)     Phase1: PD provider 先关闸（记录本层可发）
                               Phase2: 其它 connector（AscendStore）再挂钩
  → save_kv_layer(L)           Phase1: PD push 入队（发送线程逐层推给 D）
                               Phase2: AscendStore range put 入队（写池，供 prefix cache）
compute layer L+1 … L+B-1      （各自用不同槽位）
  → wait_for_layer_load(L+B)   等本层槽位可用：
       ├─ 池侧：等 L+B-B=L 的 save 事件（range put 已完成）
       └─ PD 侧：wait_for_layer_reuse(L) → 等 storage_send_done_events[slot]
  → attention 写 L+B 的共享槽位
```

### 4.4 覆写安全的双条件与 waiter 链路

> **INV-1'**：层 `L+B` 写共享槽位前，必须同时满足
> (a) 层 `L` 的**池 range put 已返回**（数据已离开 HBM）；
> (b) 层 `L` 的**PD 推送已完成**（D 已收完，`storage_send_done_events[slot]` 置位）。

- (a) 由 `layer_save_finished_events[L]` + `sync_save_events[L]` 保证（`kv_transfer.py:2043-2060`）；
- (b) 由 waiter 链路保证：
  `ascend_multi_connector.py:85-95` 把 PD provider 的 `wait_for_layer_reuse` 包成
  `_wait_for_external_slot_release` → `pool_worker.set_external_slot_release_waiter`（R2c 放开）
  → `KVCacheStoreLayerRecvingThread` 在覆写前调用（`kv_transfer.py:2060`），
  或 `pool_worker.wait_for_layer_load` 的 fallback 分支（`:2804-2805`）；
- PD provider 内部：`MooncakeLayerwiseConnectorWorker.wait_for_layer_reuse`（`:1327`）
  按 `layer_storage_slots[layer]` 的槽位等 `storage_send_done_events`，失败会把错误抛出来。

### 4.5 为什么禁止 `MooncakeConnectorV1`

按块 pull 的语义是「P 算完整个请求后，D 再按 block 拉」；复用下层 `L+B` 早已覆写层 `L` 的 HBM，
等"算完再拉"时早期层已不存在 → 必然读到错数据。R4 需在启动期 fail-fast。

### 4.6 TTL、失败与容量

| 项 | 契约 |
| :--- | :--- |
| lease TTL | D 从池读前缀时仍需 lease；`default_kv_lease_ttl` 必须覆盖整请求 onload（实测：11s → 128K warm 183.5s；300s → 26.45s） |
| PD push 失败 | 层事件置错，`wait_for_layer_reuse` 抛出 → 请求失败/降级重算，不静默 |
| 池 put 失败 | `batch_put_revoke` + 移出 `active_keys`；该块不可命中，后续 chunk 重算 |
| 容量 | store 容量 ≥ 并发 × 单请求全量 KV（200K ≈ 11.3 GB/请求）；D2D 不减少 store 占用 |
| 元数据 | 每 chunk 对全前缀做 range get ⇒ `O(blocks × chunks)`；128K/200K 实测已含该开销 |

### 4.7 store-only 对照（何时可退回）

| 维度 | store-only（当前生产） | D2D（本设计） |
| :--- | :--- | :--- |
| P→D 路径 | 池中转（P 写 DRAM segment，D 读） | P→D 直传（HBM） |
| P 覆写门控 | 只需池 put 返回 | 需池 put 返回 **+ PD 送达** |
| waiter | 不需要 | **必需（R2c）** |
| 交接延迟 | 高（池中转 + master 元数据） | 低（点对点） |
| 复杂度 | 低 | 中（两连接器 + 顺序 + waiter） |

> store-only 仍保留为**回退方案与对照组**：若 D2D 的 waiter 链路验证不通过，可先回到 store-only
> （功能可用、性能次优），不影响复用本身的验证。

---

## 5. 正确性不变量与边界

### 5.1 不变量

- **INV-1'**（覆写安全，D2D）：见 §4.4；
- **INV-2**（完成状态单位）：以**物理 buffer** 判定；`prefetch_layer_map` 保证层号↔槽位一一映射；
- **INV-3**（key 空间不变）：`apply_layerwise_kv_cache_plan` 只改 `kv_cache_config.kv_cache_tensors`，
  不碰 `kv_cache_groups` → `hybrid_layout_id` 不变，key 空间稳定。

### 5.2 partial block —— 唯一需要额外处理的正确性边界

复用后 HBM 只保留最后 B 层；chunk 结束时不满 128 token 的尾部：
1. 落不了池（`num_tokens_to_save = 5000//128*128 = 4992`；block-key 无 partial key）；
2. 同 chunk 后半程被层 `L+B` 覆写 → HBM 也没了；
3. 下一 chunk 的前缀 `[0,5000)` 只能 load 到 4992 → 尾部 KV 彻底丢失。

**第一版落地**：
1. **启动期强校验**：复用开启时要求 `max_num_batched_tokens % cache_transfer_granularity == 0`
   （我们 4096/16384 满足），否则 fail-fast；
2. **Phase 2**：让 scheduler 把每个请求的 chunk 边界 floor 到 granularity
   （`pool_scheduler.py:622` 已有 `_floor_to_cache_transfer_granularity`），或做尾部重算兜底；
3. 未完成 Phase 2 前，复用只允许用于「单请求/受控并发」的部署。

### 5.3 其他边界

| 情形 | 处理 |
| :--- | :--- |
| 层 L 无 cached prefix | `submit_layer_load` 仍提交空任务承载 `wait_for_save_layer` 门控（`pool_worker.py:2748-2762`，已实现） |
| `independent_layers`（默认 `[0]`） | 不参与复用 → 独占 buffer |
| MTP draft 层 | v1 runner（`model_runner_v1.py:4609`）不排除 → 参与复用；v2 runner（`v2/model_runner.py:374`）排除 → 独占；容量系数不同，需 V0 确认 |
| 多 group（MLA main + indexer） | 按 main spec 分桶，indexer 跟随；须保证同桶 indexer spec 一致 |
| 并发写同前缀 | `_filter_pool_existing_keys` + `_put_started_keys` 去重；put_start 被拒只告警，一写赢 |

---

## 6. GLM-5.2 适配

| 项 | 现状 | 结论 |
| :--- | :--- | :--- |
| 模型 | `model_type=glm_moe_dsa`，78 层 + 1 MTP | 79 物理层 |
| KV 组成 | MLA main 576 B/token/层 + DSA indexer 128 B/token/层 | 704 B/token/层；×79 = 55,616 B/token |
| 容量校验 | 641,408 tokens × 55,616 B = 33.2 GiB | 与日志 33.12 GiB 吻合 |
| KDA / Mamba | 无 | 不需要 R6 的 state 排除 |
| tail spec | 无（`model_version` 非 `glm5_next`） | 不需要 tail 排除 |
| **indexer full/shared** | 混合 | **风险**：同桶层的 indexer spec 可能不同 → `_merge_specs` ValueError；必须 V0 验证 |
| glm5next（未来） | 有 tail + KDA group | 需要 R6 排除逻辑 |

**容量系数（B=9, I=1）**：

```
不排除 MTP：buffers = 1 + 9      = 10 → factor = 79/10 = 7.90 → 641,408 × 7.9  ≈ 5.07M tokens
排除 MTP：  buffers = 1 + 9 + 1  = 11 → factor = 79/11 = 7.18 → 641,408 × 7.18 ≈ 4.61M tokens
```

---

## 7. 验证计划

### 7.1 单测（`tests/ut/distributed/ascend_store/`）

| 编号 | 内容 | 通过标准 |
| :--- | :--- | :--- |
| U1 | `extract_layout_config` 三种输入 | None / None / 配置；非复用时 factor == 1 |
| U2 | R2/R3 两侧门控一致 | `prefetch_layer_map` 与 `layerwise_offload` 同源同判 |
| U3 | `save_partial_block=True` + block-key 的块对齐 | 与 §5.2 一致 |
| U4 | 非复用 block-key 回归 | prefetch 仍为 2 |
| U5 | R4 四条防御校验 | 全部 fail-fast |
| U6 | GLM-5.3 风格四 group | main+indexer 参与、tail/KDA 独占 |
| U7 | **R2c waiter 注册** | `set_external_slot_release_waiter` 在 block-key 返回 True，recv 线程拿到回调 |

### 7.2 准入门槛 V0（不改代码即可跑）

把仓库根目录的 `verify_block_key_reuse.py`（CPU harness，monkeypatch `extract_layout_config`）
改造成我们模型的 spec 集（78 × main + 78 × indexer + 1 MTP），回答：

| 问题 | 判据 |
| :--- | :--- |
| Q1 分桶 | 每物理层 main×1 + indexer×1；无 `extra_main_specs` 落入无人管分支 |
| Q2 合并丢层 | 合并后所有层仍被某个 descriptor 覆盖 |
| Q3 key 空间 | 合并前后 `hybrid_layout_id` 一致 |

> 三项全 PASS 才允许动 R1–R3。

### 7.3 实机（A3，4 节点 2P+2D）

| 编号 | 内容 | 判据 |
| :--- | :--- | :--- |
| V1 | 容量与日志 | 出现 `Layerwise KV cache reuse maps 79 layers onto N buffer assignments; scale ... by X`；`Available KV cache memory` 比值 ≈ X |
| V2 | 正确性 | 同 prompt 两次输出逐 token 一致；needle 探针；复用开/关对拍（8K/32K/128K） |
| V3 | chunk 边界 | 4096（对齐）通过；4000（不对齐）应被启动校验拒绝 |
| V4 | 会话/淘汰 | 长请求跨 TTL；store 打满触发 evict 时降级为重算 |
| **V5** | **D2D waiter 链路** | P 侧出现 `Waiting for Mooncake transfer to release physical KV slot` 且等待时长受控；D 侧无 `Range read failed`；`External prefix cache hit rate` 正常；注入 D 侧延迟可观察到 P 的 `wait_for_layer_reuse` 等待 |
| V6 | 性能 | 32K/64K/128K 冷热 A/B；B=8/9/12/16 扫参 |
| **V7** | **D2D vs store-only** | 同 prompt 的 TTFT/端到端时延对比（D2D 应显著优于池中转） |

**日志过滤**：

```bash
grep -a 'Layerwise KV cache reuse maps'                       vllm_p0.log | tail -1
grep -a 'Waiting for Mooncake transfer to release physical KV slot' vllm_p0.log | tail -3
grep -aE 'Range (write|read) failed|Segfault'                 vllm_p*.log vllm_d*.log | tail
grep -a 'External prefix cache hit rate'                      vllm_d*.log | tail -3
```

---

## 8. 风险与开放问题

| # | 风险 | 等级 | 缓解 |
| :--- | :--- | :--- | :--- |
| K1 | **partial block 丢 KV**（§5.2） | **高（正确性）** | 启动校验 + 部署约束；Phase 2 对齐/兜底 |
| **K2** | **官方文档只把 `SfaRemoteD2HConnector` 列为 offload 支持连接器**；`MooncakeLayerwiseConnector` + reuse 属未验证组合 | **高（兼容性）** | V5 先把 waiter 链路打通；失败则退 B 方案（D 侧 BF16 + sparse offload） |
| K3 | `indexer_types` full/shared 导致复用分桶 spec 不一致 | 中 | V0 CPU harness 先验证 |
| K4 | 时序硬化：PD push 尾延迟进入关键路径 → TTFT 抖动 | 中 | B=9 起步；V6 实测；必要时调 `send_fence_backlog` |
| K5 | store eviction 与在途 session 交互（block-key 无 lease） | 中 | V4；容量规划；开 SSD offload |
| K6 | 每 chunk 对全前缀 range get，master QPS `O(blocks×chunks)` | 中 | V6 观测；调 `layerwise_max_transfer_blocks` |
| K7 | 逆上游意图（`extract_layout_config` docstring 明确排除 reuse） | 中 | 提 issue/RFC 对齐；U1–U7/V1–V7 纳入 CI |
| K8 | 上游 KVPP 系列重构 layerwise 管线，落点漂移 | 中 | 改动收在协议模块 + 门控点 |
| K9 | `SfaRemoteD2H` 要求 P TP ≥ D TP 且整除；D 侧主 KV 必须 BF16 | 中（若选 B） | 选 A 可绕开；选 B 需改 D 的 dtype 与 sparse offload 配置 |
| K10 | lease TTL 缺失 → warm 退化（实测 6.9x） | **高（部署）** | 硬性配置契约：300000 |

---

## 9. 里程碑与工作量

| 阶段 | 内容 | 估算 |
| :--- | :--- | :--- |
| **Phase 1** | R1–R4（含 R2c）+ U1–U7 + V0–V2（P 侧复用、store-only 打通） | 1.5–2 人周 |
| **Phase 2** | D2D 接线（方案 A）+ V5–V7 + chunk 对齐 | 1.5–2 人周 |
| **Phase 3** | evict/压测/调参 + 上线评审 | 1–1.5 人周 |

前置依赖：V0 通过；lease TTL 与 RoCE 配置就绪（已有）；D2D 方案选型（A/B）由 V5 结果决定。

---

## 10. 本仓库落地配置

### 10.1 前置（必须同时满足）

| 项 | 值 | 说明 |
| :--- | :--- | :--- |
| `default_kv_lease_ttl` | **300000** | 11s 实测导致 128K warm 183.5s（vs 26.45s） |
| RoCE | `HCCL_INTRA_ROCE_ENABLE=1` + `HCCL_INTRA_PCIE_ENABLE=0` + `hixl_preset: roce` | 不开会退到 ACL IPC，全量 `-800` + segfault |
| `use_layerwise` | true | 复用是 layerwise 的增强 |
| P chunk 粒度 | 4096 / 16384（128 倍数） | §5.2 正确性要求 |
| 连接器 | `MultiConnector[MooncakeLayerwiseConnector, AscendStoreConnector]` | 禁止 `MooncakeConnectorV1` |

### 10.2 Ansible 落点

- `ansible/inventories/a3/group_vars/all.yml` → `_1m_v030_hyb.pd_cluster.layerwise`：
  新增 `buffer_reuse: true`、`num_shared_buffers: 9`；
- `ansible/templates/run_dp_prefill_template.sh.j2`：`single_store_connector` 分支改为
  MultiConnector 形状（或新增 `d2d: true` 分支），P 侧渲染两个 connector；
- `ansible/templates/run_dp_decode_template.sh.j2`：D 侧渲染 `MooncakeLayerwiseConnector(kv_consumer)`
  + `AscendStoreConnector(kv_consumer, consumer_is_to_load)`；
- 新增档位 `1m-v030-hyb-d2d`，保留 `1m-v030-hyb`（store-only）作为回退与对照。

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
| 非 attention spec 拒绝 | `layerwise_cache_layout.py:280-285` |
| 同桶 spec 一致性校验 | `layerwise_cache_layout.py:357-361` |
| 模式判定（GVA） | `pool_worker.py:231` |
| 复用状态构建（R2a/R2b） | `pool_worker.py:490` / `:611-646` |
| **slot waiter 注册（R2c）** | `pool_worker.py:722` |
| 加载任务提交（门控传递） | `pool_worker.py:2748-2762` |
| waiter fallback 调用 | `pool_worker.py:2804-2805` |
| scheduler 复用布局（R3） | `pool_scheduler.py:237-257` |
| chunk 粒度与 floor | `pool_scheduler.py:147` / `:622` |
| KV 预算放大 + 日志 | `worker/worker.py:629-647` |
| 布局应用（v1 / v2） | `worker/model_runner_v1.py:4609` / `worker/v2/model_runner.py:374` |
| 加载侧门控实现 | `kv_transfer.py:2043-2060` |
| 保存完成信号 | `kv_transfer.py:1812` |
| **MultiConnector 两阶段 save + waiter 注册** | `ascend_multi_connector.py:85-95` / `:144-168` |
| PD 连接器 reuse 能力 | `kv_p2p/mooncake_layerwise_connector.py:749` / `:831` / `:1327` |
| SfaRemoteD2H reuse 能力 | `kv_p2p/sfa_pd_rd2h/connector.py:58` / `:303` |
