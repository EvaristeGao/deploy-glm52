# Mooncake 逐层 P/D + 两端 KV Offload：详细设计（落地版）

> 上游设计依据：`mooncake_layerwise_pd_offload_design_v2.md`（`src/vllm-ascend` 的 `design-doc` 分支，579 行）
> 对应 PDF：`mc.pdf`
> 设计基线：`4234a1f`（本地分支 `dsa-base`）；我方现网基线：`5f828ad`（v0.30.0rc1）
> 本文标注：✅ 已在源码中核实 ｜ 🟡 本次设计提议 ｜ ❗ 风险/未决
> 日期：2026-11-02

---

## 0. 本文与上游文档的关系

上游文档是**两人分工的设计意图 + 验收矩阵**；本文是**可执行的落地方案**：

- 明确「哪些能力上游已有、直接用」，避免重复实现（我们上一版的最大错误就是重造了一套）；
- 明确「哪些必须新增」，给出数据结构、接口签名、时序与状态机；
- 明确「我们基线要改哪几行」，附文件与行号索引。

一句话结论：**M1（P 侧复用正确）不需要重新设计，只需要把上游既有的
"槽位映射 + 每槽 send-done 屏障 + MultiConnector 仲裁"接上，并补两处上游修复的语义；
M2（D 侧 Host offload）才需要真正的新开发。**

---

## 1. 范围与里程碑

| 里程碑 | 内容 | 依赖 | 交付判据 |
|---|---|---|---|
| **M0** | 关闭复用，MultiConnector 双连接器跑通 | 无 | 4 引擎 200、长短请求正确、等价于现网 `1m-v030-lw` |
| **M1** | **P 侧 buffer reuse + Store 保存/恢复正确** | M0 | 双 chunk 长 prompt 正确、prefix 命中正确、取消/抢占不串数据 |
| **M2** | D 侧 Host Main + HBM Indexer 的双路径 offload | `dsa-base` 的 `MooncakeHostPool` / `host_backend` | D 侧 `kv_load_failure_policy="fail"`，缺数据不 decode |
| **M3** | 边界与失败矩阵 | M1+M2 | 上游 §8 测试矩阵逐条通过 |

上游范围明确不包含：D 侧复用、异构 P/D TP、混合 attention/recurrent、Mamba/KDA、量化传输、speculative/MTP。

---

## 2. 术语与不变量

### 2.1 术语

| 术语 | 定义 | 源码对应 |
|---|---|---|
| **物理槽（slot）** | 一块可被多个逻辑层复用的 HBM KV 缓冲，由**实际注册地址**去重得到 | `_init_layerwise_reuse_state()`：`slot_by_addr[addr]` ✅ |
| **层→槽映射** | 一个逻辑层占用的全部 slot（Main/Indexer 可能多个） | `layer_storage_slots: dict[int, tuple[int, ...]]` ✅ |
| **槽代次（epoch）** | 同一 slot 的第 k 次占用。层 L 与 M 共槽时，M 是 L 的后继代 | 🟡 建议显式化（见 §4.3） |
| **读者（reader）** | 对某 slot 当前代发起读取的执行体：compute / store_put / pd_transfer / store_get_h2d | 部分现有（`layer_save_finished_events`、`storage_send_done_events`） |
| **源可释放（source_read_done）** | 当前代所有读者都已停止读取且无关联失败 | `wait_for_layer_reuse()` ✅ |
| **快照已提交（snapshot_committed）** | Store 对象整组提交完成，可被下一 step 恢复 | `batch_put_session_end` / `try_finish_and_delete_stored_request` ✅ |

### 2.2 不变量（M1 验收口径）

**I1** 写共享槽前，必须等到该槽**上一代的所有读者**结束（compute / store_put / pd_transfer）。
**I2** "提交异步任务"、"发送入队"、"RPC 返回"**都不算**读者结束。
**I3** 即使本层**没有任何远程 GET**（首 chunk / 零加载范围 / 本地 prefix 命中 / 空 payload），也必须过屏障。
**I4** 屏障不得等待整对象提交（`snapshot_committed`），否则形成
「下一层等提交 → 提交等最后层 → 最后层等槽位」环。
**I5** 失败必须传播：`error != None` 时禁止继续计算/覆盖，且不得把异常后的 `event.set()` 当成功。

---

## 3. 组件与职责（M1 视角）

### 3.0 先纠正一个关键事实：我们的链路里没有"屏障提供者"

现网 `1m-v030-lw` 的 P 侧连接器是（生成脚本 + 运行日志双重确认）：

```jsonc
"kv_connector": "MultiConnector",
"kv_connector_extra_config": { "connectors": [
    { "kv_connector": "MooncakeConnectorV1",  "kv_role": "kv_producer" },   // ① P/D 交接（blockwise）
    { "kv_connector": "AscendStoreConnector", "kv_role": "kv_producer",    // ② Store 保存/恢复（layerwise）
      "kv_connector_extra_config": { "backend": "mooncake", "use_layerwise": true, ... } }
] }
```

```
运行日志：Creating v1 connector with name: AscendMultiConnector
          （子连接器：MooncakeConnectorV1 ×2、AscendStoreConnector）
```

而 `supports_layerwise_buffer_reuse = True` 只在
`kv_p2p/mooncake_layerwise_connector.py:749` 与 `kv_p2p/sfa_pd_rd2h/connector.py:58` 上：

| 连接器 | 在链上？ | 提供 `wait_for_layer_reuse`？ |
|---|---|---|
| `MooncakeConnectorV1` | ✅ | ❌（blockwise 交接，无逐层槽位概念） |
| `AscendStoreConnector` | ✅ | ❌（Store 侧，只有 `set_external_slot_release_waiter` 接收端） |
| **`MooncakeLayerwiseConnector`** | ❌ **不在** | ✅ 唯一实现者 |

推论（`ascend_multi_connector._configure_layerwise_reuse_completion`）：

```
_layerwise_slot_release_providers = []          # 没有 provider
_external_slot_release_sink_configured = False  # 没有 sink
⇒ 不注入 waiter，无任何屏障
```

**所以只把 `layerwise_num_shared_buffers` 调到 < num_layers，只是"让布局共享槽位"，
却完全没有屏障**——这正是我们那版"复用开着、屏障在、但从来没接线"的最底层原因：
不是"忘记调用"，而是**提供屏障的连接器根本不在链路里**。

### 3.1 目标链路（M1 必须换的那一个连接器）

```
现在： MultiConnector[ MooncakeConnectorV1 (blockwise 交接) , AscendStoreConnector (layerwise Store) ]
目标： MultiConnector[ MooncakeLayerwiseConnector (逐层 P→D push) , AscendStoreConnector (layerwise Store) ]
                        └─ 提供 wait_for_layer_reuse / storage_send_done_events
```

这与上游设计的"逐层 P/D 传输"一致：P 算完一层就把该层推给 D（Main→D Host、Indexer→D HBM），
而不是等整段 prefill 再走 `MooncakeConnectorV1` 的块级交接。

### 3.2 组件与职责

```
                     AscendMultiConnector（仲裁者）
                     ├─ _configure_layerwise_reuse_completion()   决定 provider
                     ├─ _wait_for_external_slot_release(layer_idx) 汇总等待
                     └─ wait_for_layer_load(layer_name)            模型侧入口
                              │ 注入 waiter
        ┌─────────────────────┴──────────────────────┐
        │                                            │
MooncakeLayerwiseConnector                 AscendStoreConnector
（P/D 逐层传输 + 槽位屏障）                 （Store 保存/恢复 + 复用布局）
  worker: MooncakeLayerwiseConnectorWorker    worker: KVPoolWorker
  ✅ layer_storage_slots                      ✅ build_layerwise_cache_layout
  ✅ storage_send_done_events                 ✅ prefetch_layer_map / buffer_slots
  ✅ _mark/_complete_layer_reuse              ✅ _prepare_layerwise_{get,put}_session
  ✅ wait_for_layer_reuse                     ❗ 缺 reader 登记（本文 §8 新增）
```

| 组件 | 文件 | M1 是否改 | 说明 |
|---|---|---|---|
| MultiConnector | `kv_transfer/ascend_multi_connector.py` | 少量 | 已有 provider 选择与 waiter 注入 ✅；需加"启用复用但未接线 → 启动失败" |
| P/D 连接器 | `kv_p2p/mooncake_layerwise_connector.py` | 少量 | 屏障已完整 ✅；需与企业 Store 侧的 reader 汇合 |
| Store 连接器 | `kv_pool/ascend_store/ascend_store_connector.py` | 少量 | `set_external_slot_release_waiter()` 已存在 ✅ |
| Store worker | `kv_pool/ascend_store/pool_worker.py` | **核心** | 加 reader/epoch（§8）；修 prefix 命中起点（§9） |
| 布局规划器 | `kv_pool/ascend_store/layerwise_cache_layout.py` | **不改** | ✅ 已具备 `num_shared_buffers` / `independent_layers` / `prefetch_layer_map` |

> 结论：M1 的改动面是 **2 个文件的核心逻辑 + 2 个文件的接线校验**，不是重写。

---

## 4. 数据结构

### 4.1 已有可直接复用（✅ dsa-base 行号）

```python
# mooncake_layerwise_connector.py:1254-1257
self.layer_storage_slots: dict[int, tuple[int, ...]]   # 层 → 物理槽
self.storage_send_done_events: list[threading.Event]   # 每槽：所有 P/D 读者结束
self._storage_send_errors: dict[int, str]              # 每槽：传输失败
self._pending_reuse_layers: set[int]                   # 已登记未完成的层
```

### 4.2 布局规划器输出（✅ `layerwise_cache_layout.py:163-215`）

```python
class LayerwiseReuseLayout:
    num_shared_buffers: int
    independent_layers: list[int]
    buffer_slots: tuple[tuple[int, ...], ...]     # 每槽包含哪些层
    prefetch_layer_map: dict[int, int]            # 层 M → 它的上一代占用者 L
    num_prefetch_layers: int
```

`prefetch_layer_map` 就是**槽位冲突图**：`M` 写之前必须等 `prefetch_layer_map[M]` 的读者结束。
规划器是槽位映射的唯一来源——**不得按层号取模推导**（上游 §3 明确要求）。

### 4.3 🟡 新增：槽代次 + 读者登记（M1 的 Store 侧核心）

```python
# kv_pool/ascend_store/pool_worker.py（新增，进程内单例即可，按 slot_id 索引）
@dataclass
class SlotEpoch:
    slot_id: int
    epoch: int = 0                  # 第几次被占用
    readers: int = 0                # 未完成的读者计数（compute/store_put/pd_transfer/store_get）
    sealed: bool = False            # 本代读者计划已封口，不再新增
    failed: str | None = None       # 首个失败原因（一旦置位不可清除，直到下一代）
    quiescent: threading.Event      # readers == 0 且 sealed
```

不变式：
- `readers > 0` ⇒ 禁止 `begin_slot_write` 推进 epoch；
- `sealed == False` 且 `readers == 0` ⇒ **仍需等待**（读者计划可能还没登记完，对应上游"已打开但未封口不能返回"）；
- `failed` 只能由 `complete_read(error)` 设置，且**不能**被下一代的成功掩盖。

---

## 5. 接口

### 5.1 已有（✅ 直接用）

| 接口 | 位置 | 语义 |
|---|---|---|
| `supports_layerwise_buffer_reuse = True` | `mooncake_layerwise_connector.py:749` | 声明本连接器负责槽位释放 |
| `set_external_slot_release_waiter(waiter) -> bool` | `ascend_store_connector.py:282/303` | Store 接收线程装 waiter；**返回 False 表示没装上** |
| `wait_for_layer_reuse(layer_idx) -> None` | `mooncake_layerwise_connector.py:839/1336` | 等该层所有槽的 P/D 读者结束；失败抛异常 |
| `_mark_layer_reuse_pending(layer)` | 同文件 1317 | 开代：清事件、清错误、登记 pending |
| `_complete_layer_reuse(layer, error)` | 同文件 1327 | 闭代：置事件、记错误 |
| `reuse_completion_callback` | 同文件 237/321 | 发送线程**真正完成**时回调 |

### 5.2 🟡 新增（M1）

```python
# 1) 槽写前：推进代次（由 Store 的 load 路径与模型写路径共同调用）
def begin_slot_write(self, slot_id: int) -> None:
    """断言上一代已 quiescent 且无失败；epoch += 1；重置 readers/sealed。"""

# 2) 读者登记：任何"要读这块 slot"的动作在**发起前**登记
def reserve_read(self, slot_id: int, reader: str) -> None:
    """readers += 1；若 sealed 则抛错（说明时序错）。"""

# 3) 计划封口：本层所有读者都已登记完
def seal_epoch(self, slot_id: int) -> None:
    """sealed = True；若 readers == 0 则置 quiescent。"""

# 4) 读者完成
def complete_read(self, slot_id: int, reader: str, error: str | None = None) -> None:
    """readers -= 1；error 记入 failed；readers == 0 且 sealed 时置 quiescent。"""

# 5) 接线自检（启动期）
def assert_reuse_wired(self) -> None:
    """启用复用（num_shared_buffers < num_layers）但 external waiter 未安装 → 抛异常终止启动。"""
```

### 5.3 与上游命名对齐（按 `1898107` 的语义）

- Store 发送线程里的 `pd_transfer_waiter` 一律改名 `external_slot_release_waiter`；
- 未注入时**不要静默降级**：M1 要求 `assert_reuse_wired()` 直接失败；
  （上游 `1898107` 的兜底 `_wait_for_pd_transfer()` 用共享事件表，是 ToDram 线的做法，
  我们只有在确需兼容 ToDram 时才实现。）

---

## 6. 逐层时序（M1 主路径）

以「层 L（上一代占用 slot S）→ 层 M（下一代，同 S）」为例，一个 prefill step 内：

```
 #  执行体                        动作                                          等待/条件
──────────────────────────────────────────────────────────────────────────────────────────
 1  MultiConnector._wait_for_external_slot_release(M)   ← Store 侧入口
       └→ LayerwiseConnector.wait_for_layer_reuse(M)
            └→ 对 S: 等 storage_send_done_events[S]     ← 上一代 P/D 读者结束
 2  Store worker: begin_slot_write(S)                    ← 断言 I1，推进 epoch
 3  Store worker: reserve_read(S, "compute")             ← 本代计算读者登记（模型 attention 会读）
 4  若本层需要历史 KV（chunk≥2 / prefix 命中）:
       open_epoch → 生成 GET 会话（§9 的三条规则）
       reserve_read(S, "store_get") → H2D → complete_read(S, "store_get")
 5  Store worker: reserve_read(S, "store_put")           ← 本代 Store 上传读者
 6  Store worker: seal_epoch(S)                          ← 不再新增读者
 7  P/D: _mark_layer_reuse_pending(M)                    ← ❗必须先于 Store 发布 save-done
 8  模型 attention 计算（写 M 的 KV 到 S）
       record sync_save_events[M]（NPU event，见 §11 风险）
 9  发布保存任务（Store PUT + P/D 传输），入队
10  各读者完成时 complete_read：store_put（PUT 返回）、pd_transfer（发送线程回调）
11  wait_for_layer_load(M) 返回 → 模型可安全读 M
12  下一代层 M' 到来时回到第 1 步
──────────────────────────────────────────────────────────────────────────────────────────
关键顺序约束（上游 §4 步骤 1 + 源码注释）：
  第 7 步必须在第 9 步之前 —— 否则 Store 接收线程可能消费到上一代已 set 的事件，
  在本代传输登记为 pending 之前就开始 H2D。
```

对应源码：第 7 步的注释在 `mooncake_layerwise_connector.py:2032-2036` ✅：

> "This must happen before AscendStore publishes StoreSaveDone. Otherwise its receiver may
> consume the previous set state and start H2D before this transfer is registered as pending."

---

## 7. 状态机

### 7.1 槽位

```
        begin_slot_write
FREE ───────────────────► ACTIVE(epoch=k)
  ▲                         │ reserve_read / seal_epoch
  │                         ▼
  │                     SEALED(readers 递减)
  └──── quiescent ◄─────────┘   （readers==0 且 sealed）
        │
        └─ failed != None ⇒ 进入 FAILED，只能整体失败退出，禁止复用
```

### 7.2 层

```
PENDING(reuse) ──pd_transfer 完成──► DONE
      │                              │
      └── 任一读者失败 ──► FAILED ────┘（错误保留到该层生命周期结束）
```

### 7.3 请求（与槽位解耦）

请求级就绪 ≠ 槽位可释放：
- 槽位可释放看 `quiescent`（本 step）；
- 请求可 decode 看上游 §6.2.3 的"D 请求可执行"（M2 才涉及）。

---

## 8. Store 侧 reader/epoch：为什么必须新增

### 8.1 缺口

现状（rc1 与 dsa-base 一致）：

- P/D 传输读者：✅ 有（`storage_send_done_events`）；
- 计算读者：✅ 有（模型侧 `wait_for_layer_load`，且 P/D 屏障间接覆盖）；
- **Store PUT 读者：❌ 无**。`kv_transfer.py` 的 range PUT 把 device 指针交给传输层后即返回，
  槽位屏障 `wait_for_layer_reuse()` 完全不知道它读过这块。

这正是我们实测到的现象来源：`B=9`（共享槽）时池内容错乱，而"完全串行"也救不了——
因为串行只约束了层间距离，没有约束"上传是否读完"。

### 8.2 最小实现（M1，约 120 行）

```python
# pool_worker.py
def _on_store_put_submitted(self, layer_id: int) -> None:
    for slot in self._slots_of_layer(layer_id):
        self._epochs[slot].reserve_read(slot, "store_put")

def _on_store_put_done(self, layer_id: int, error: str | None) -> None:
    for slot in self._slots_of_layer(layer_id):
        self._epochs[slot].complete_read(slot, "store_put", error)
```

挂接点（两处，均已在现有代码里存在，只需加回调）：
- 提交：`KVCacheStoreKeyLayerSendingThread` 里 range PUT 之前（当前有 `sync_save_events[layer].synchronize()`）；
- 完成：同一线程 PUT 之后 / `layer_save_finished_events[layer].set()` 处。

`wait_for_layer_reuse(layer_idx)` 扩展为**两路取与**：
```python
# 语义：该层的所有槽在 P/D 与 Store 两侧都 quiescent 才返回
for slot in layer_storage_slots[layer_idx]:
    pd_wait(slot)                       # 已有
    store_wait(slot)                    # 新增：epochs[slot].quiescent.wait()
    if epochs[slot].failed: raise RuntimeError(...)
```

### 8.3 与 `snapshot_committed` 的隔离（I4）

`store_wait(slot)` **只等"读者停止"**，不等待对象提交。
对象提交（`batch_put_session_end`）仍由 `try_finish_and_delete_stored_request` 独立推进；
下一 step 的恢复必须校验"该版本已提交"，未提交版本不得作为恢复源。

---

## 9. chunked prefill / prefix 命中的三条规则

这是 M1 正确性的另一半，且**上游已有实现，我们只需对齐**。

| 规则 | 内容 | 源码位置 |
|---|---|---|
| R1 | layerwise offload 下 **Store Get 会话起点取 block 0**（本地 prefix 命中也必须做完整 Get） | `b3222a7`（2 行）✅ |
| R2 | **共享层** load 从 block 0 开始；独立层可保留 `hbm_tail` 跳过 | `pool_worker.py` `load_start_block = ... else 0` ✅（rc1 已有） |
| R3 | 空 GET / 空 payload 也必须过屏障（不得因任务列表为空跳过） | `wait_for_layer_reuse` 的调用点在每层，不依赖是否有 load 任务 ✅ |

伪代码（对齐后）：

```python
def _prepare_layerwise_get_session(request):
    if request.load_spec and request.load_spec.can_load:
        start_block = 0 if self.layerwise_offload else \
                      request.load_spec.vllm_cached_tokens // self.block_size   # R1
        ...
```

**我们此前的错误**：自造 `save_block_keys` → range 的 per-step 重载，绕过了 R2，
并且用"有无 load 任务"决定是否等待，违反 R3。

---

## 10. 配置与启动校验

### 10.1 配置（三键，不引入 `reuse_buffer` 布尔）

```jsonc
{
  "use_layerwise": true,
  "layerwise_num_shared_buffers": 3,
  "layerwise_prefetch_layers": 1,
  "layerwise_independent_layers": [0, -1]
}
```

- `num_shared_buffers` 缺省 = `num_layers`（**不隐式开启复用**）✅ `layerwise_cache_layout.py:175`；
- `prefetch_layers` 缺省 = `min(num_shared_buffers, DEFAULT_MAX_PREFETCH)`✅；
- `independent_layers` 缺省 = `[0]`✅；
- `has_layer_reuse = len(reused_layers) > num_shared_buffers`✅。

### 10.2 启动校验（🟡 新增，失败即拒绝启动）

| 校验 | 失败动作 |
|---|---|
| `num_shared_buffers >= 1` 且层号合法 | 抛 ValueError ✅（已有） |
| cache spec / shape / dtype / 角色 / runner 兼容 | 抛 ValueError 🟡（新增） |
| **启用复用但 external waiter 未接线** | `assert_reuse_wired()` 抛错 🟡（对应上游"不能静默降级"） |
| 异构 P/D TP、混合 group、Mamba/KDA、量化、MTP | 明确拒绝 🟡 |

---

## 11. 移植清单（相对我方 `5f828ad`）

### 11.1 直接采用（0 改动，✅ 已验证存在）

`layerwise_cache_layout.py`（含 `build_layerwise_cache_layout` / `prefetch_layer_map` / `buffer_slots`）、
`mooncake_layerwise_connector.py` 的槽位屏障、`ascend_multi_connector.py` 的 provider 选择与 waiter 注入、
`ascend_store_connector.set_external_slot_release_waiter()`。

> 我们上一版把这些**全部绕过**，另写 24 槽布局 + 信号量，是最大的返工成本来源。

### 11.2 需要移植/新增

| # | 内容 | 来源 | 预估 | 风险 |
|---|---|---|---|---|
| **P0** | **P/D 交接连接器换成 `MooncakeLayerwiseConnector`**（当前是 `MooncakeConnectorV1`，不提供屏障） | `kv_p2p/mooncake_layerwise_connector.py`（两基线都有） | 中（配置 + 联调） | **高**（改变 P/D 数据路径） |
| P1 | `start_block = 0 if layerwise_offload` | `b3222a7` 2 行 | 极小 | 低 |
| P2 | Store 发送线程 `pd_transfer_waiter` → `external_slot_release_waiter` 命名对齐 | `1898107` 语义 | 小 | 低 |
| P3 | `assert_reuse_wired()` + MultiConnector 校验 | 本文 §5.2/§10.2 | 小 | 低 |
| P4 | **SlotEpoch / reader 登记（Store PUT 读者）** | 本文 §4.3/§8 | 中（~120 行） | **高** |
| P5 | `wait_for_layer_reuse` 两路取与（P/D + Store） | 本文 §8.2 | 小 | 中 |
| P6 | 启动期拒绝矩阵 | 本文 §10.2 | 小 | 低 |
| P7 | 删除自造实现（`probe/patch/reuse/*` 挂载、`[CREDIT-BAL]`、`reuse_mode.txt`） | —— | 删除 | 低 |

### 11.3 暂不做（M2）

`MooncakeHostPool` / `host_backend` / `dsa_pd_offload` / `TargetManifest` / `writer_plan` ——
前三个在 `dsa-base` 已有（可直接用），后两个需在 M2 新写。

---

## 12. 测试矩阵（M1 必过）

| # | 场景 | 判据 |
|---|---|---|
| T1 | 单 chunk、无复用（`num_shared_buffers=num_layers`） | 与现网 `1m-v030-lw` 输出一致 |
| T2 | 共享 3 槽 + 单 chunk | 输出正确 |
| T3 | **共享 3 槽 + 双 chunk（prompt >4096）** | **输出正确**（我们此前一直不过） |
| T4 | 共享 3 槽 + 本地 prefix 命中 | 命中层仍走完整 Get，输出正确（`b3222a7` 回归） |
| T5 | 共享 3 槽 + 4 并发 | 全部正确、无串数据 |
| T6 | 空 GET / 空 payload 层 | 仍过屏障（I3），无越界 |
| T7 | Store 慢 / P/D 慢 交替 | 无环形等待；`wait_for_layer_reuse` 可观测 |
| T8 | 取消 / 抢占 | 旧代回调不释放新代；不静默损坏 |
| T9 | PUT 失败 / sender 停止 | 有界退出，错误可见 |
| T10 | 未接线但启用复用 | 启动失败（不是静默降级） |

判据统一为：**输出与无复用基线逐 token 一致** + 日志无 ERROR + 无 `torch.npu.synchronize()`。

---

## 13. 风险与未决

| # | 风险 | 说明 | 缓解 |
|---|---|---|---|
| R1 | **"计算读取结束"如何证明** | 上游要求"对照实际 NPU event 记录位置证明"，不能只看变量名。我们此前实测：`record()` 在 stream 0、`synchronize()` 等 1.5ms，**流序看起来是对的**，但那时屏障根本没接 | M1 阶段加断言：用独立 event 在 attention 前后打点，验证 barrier 生效后仍正确 |
| R2 | Store PUT 读者登记的挂接点 | 现有代码里 PUT 完成点分散（`_process_range_layer_task` / `_handle_range_request` / commit） | 先只在**实际走的路径**上挂，加日志计数自检（acq==rel 那种） |
| R3 | `kv_cache_dtype=int8` 与 D Host offload 组合 | 上游支持矩阵未列 | M2 前确认，必要时先关量化 |
| R4 | `1898107` 的兜底方案依赖 ToDram 事件表 | 我们与 dsa-base 都没有 `get_shared_layer_transfer_events` | 只迁移命名与校验，不迁移兜底；必要时再评估 |
| R5 | 上游 fork 的环境依赖（CANN/Triton/memfabric） | 设计文档明确提示不能假定与 rc1 一致 | M2 前核对镜像依赖 |

---

## 14. 关键代码索引（✅ 已核对）

| 位置 | 内容 |
|---|---|
| `kv_p2p/mooncake_layerwise_connector.py:1254-1257` | 槽位状态初始化 |
| `…:1300-1315` | `_init_layerwise_reuse_state()`：地址去重 → slot 映射 + 事件表 |
| `…:1317-1325` | `_mark_layer_reuse_pending()`：开代、清事件 |
| `…:1327-1334` | `_complete_layer_reuse()`：闭代、记错误 |
| `…:1336-1350` | `wait_for_layer_reuse()`：逐槽等事件 + 失败传播 |
| `…:1524 / 1554` | 初始化与 `reuse_completion_callback` 绑定 |
| `…:2032-2047` | 逐层发送：pending 必须先于 Store 发布；入队失败即 complete(error) |
| `kv_transfer/ascend_multi_connector.py:71-99` | provider 选择 + waiter 注入 + 汇总等待 |
| `…:106-115` | `wait_for_layer_load()`：有 sink 时由 Store 负责层入口等待 |
| `kv_pool/ascend_store/ascend_store_connector.py:282/303` | `set_external_slot_release_waiter()` 委派 |
| `kv_pool/ascend_store/layerwise_cache_layout.py:163-215` | 布局构建：共享槽数 / 独立层 / `prefetch_layer_map` |
| `kv_pool/ascend_store/pool_worker.py`（rc1 `~1556`） | `load_start_block = 0 if layerwise_offload and 非独立层` |
| `b3222a7`（本地可 `git show`） | session `start_block = 0 if layerwise_offload` |
| `1898107`（本地可 `git show`） | waiter 命名对齐 + ToDram 兜底 |

---

## 15. 方案 A（基于 dsa-base 重建镜像）的前置核查（2026-11-02 实测）

### 15.1 不能靠"挂源码"走捷径

容器内 vllm-ascend 是 **editable 安装**（`vllm_ascend.__file__` → `/vllm-workspace/vllm-ascend/vllm_ascend`），
看上去替换源码树即可，但：

| 检查 | 结果 |
|---|---|
| `vllm_ascend/` 内编译产物 | **8 个 `.so`**：`vllm_ascend_C.cpython-312-aarch64-linux-gnu.so`、`libvllm_ascend_kernels.so`、`lib/libvllm_ascend_kernels.so`、`_cann_ops_custom/...` 下 5 个 CANN 算子库 |
| C++ 源文件（git 跟踪） | rc1 **1771** 个 vs dsa-base **1735** 个，文件名差异 **574 处**（大量重命名/移动） |

→ C++ 树差异过大，rc1 编出的 `.so` 不可能匹配 dsa-base 的 Python 调用约定，**必须重建镜像**。

### 15.2 还依赖版本对齐（比"编译"更大的风险）

| 包 | 镜像现有 | dsa-base `requirements.txt` | 结论 |
|---|---|---|---|
| torch | 2.10.0+cpu | `==2.10.0` | ✅ |
| torch_npu | **2.10.0.post4** | **`==2.10.0.post7`** | ❌ 需升级 |
| triton-ascend | 未见安装 | `==3.6.0` | ❓ 需补装 |
| mooncake | 0.3.13.post1 | — | ✅ |
| compressed-tensors | 0.17.0 | `>=0.11.0` | ✅ |
| vllm | 0.30.0+empty（editable） | 需与 dsa-base 匹配 | ❓ 需核对 |

`torch_npu` 从 post4 升到 post7 涉及 **CANN 版本匹配**，这正是上游文档提示的
"不得假定原基线的 CANN、Triton、memfabric 等版本已包含在新分支中"。
换言之 A 不是"加一层编译"，而是"做一个新基础镜像"。

### 15.3 已验证的中间结论（可用于任何方案）

- rc1 的 `ascend_multi_connector.py` 与 dsa-base 只差两处：
  `requires_mamba_state_copy_after_load` 聚合、`shutdown()` 顺序；**屏障接线本身在 rc1 上是完整的**；
- rc1 的逐层连接器缺 dsa-base 的 packed-SFA **c8 支持**（约 15 行，已移植并编译通过，见 `probe/patch/m1/`）；
- 代理协议缺 `remote_cached_tokens`（已补，见 `load_balance_proxy_server_example.py`）；
- 两者都补上后，rc1 上逐层连接器仍无法完成请求，并触发
  `gloo ... Connection closed by peer`（P0/P1 间），说明还差的是**连接器内部的月度修复合集**。

### 15.4 阻塞与待决

需要产品/工程侧决定（A/B/C，见 §1 与 §13 R5）：

- **A** 新建基于 dsa-base 的基础镜像（含 torch_npu/triton 对齐）——代码与设计文档一致，成本最高；
- **B** 继续在 rc1 上逐个移植——每轮 12 分钟，上限不明；
- **C** 只保留 blockwise + P1，无复用收益。

在决定之前，环境保持**干净档（blockwise、无复用、无挂载）**，已验证 4 引擎 200、请求正确。

---

## 16. 方案 A 实施记录（2026-11-02）

### 16.1 新镜像已构建并验证（M0 达成）

```
glm52-vllm-ascend:0.30.0-dsabase-4234a1f
  基础：glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13（沿用其 mooncake 0.3.13.post1）
  替换：vllm_ascend ← dsa_offload_rebase_30 @4234a1f 源码编译的 wheel
        vllm_ascend-0.30.0rc2.dev319+g4234a1f18.d20261102-cp312-cp312-linux_aarch64.whl
  自检：find_spec('vllm_ascend') → site-packages（不是旧 editable）
```

部署后实测（干净档、blockwise、无复用）：

```
4 引擎 200、proxy ok
pt=1629 → 2.2s，pt=4988 → 4.1s，输出正确
（首次请求约 60-90s：新镜像的图编译冷启动，之后恢复）
```

构建要点（踩过的坑，已固化在 `image_build/Dockerfile_glm52_dsabase` 与构建流程里）：

| 坑 | 处理 |
|---|---|
| git "dubious ownership" | `git config --global --add safe.directory "*"` |
| catlass 子模块拉不到（transport 'file' not allowed） | 直接 clone 到 `csrc/third_party/catlass` 指定 commit，保留 `include/` 即可（构建脚本见目录存在就跳过子模块） |
| CANN 算子包安装做"加载校验"，构建容器无 NPU | 给 `csrc/build_aclnn.sh` 的 installer 加 `--force`（真正校验留到实机） |
| CMake 阶段 torch 自动加载 torch_npu 失败（缺 libascend_hal） | 构建时 `TORCH_DEVICE_BACKEND_AUTOLOAD=0` + `LD_LIBRARY_PATH` 指向 `cann-9.1.0/aarch64-linux/devlib/linux/aarch64` |
| 镜像自检 import 会拉 torch_npu | 自检改用 `importlib.util.find_spec`，不执行模块 |

### 16.2 M1（逐层连接器 + 复用屏障）当前阻塞：代理的请求顺序

在 dsa-base 镜像上启用 `pd_connector: layerwise` + `num_shared_buffers: 3` + P1 补丁，
引擎全部就绪（4×200、proxy ok），但**请求挂住**：

- P/D 的 API server 日志里只有代理发来的 `/tokenize`，**没有 `/v1/chat/completions`**；
- 代理日志也只有 healthcheck，说明它卡在等待 prefill 响应；
- 即 **P 在等 D 发布目标块，而代理是"先 P 后 D"的顺序**。

这与上游设计的时序矛盾：设计明确要求"D scheduler 在 P 开始可复用的逐层执行之前，
分配并保留整个待传输 prompt 范围的目标 blocks；worker 绑定 TargetLease 并注册成功后发布目标描述"。
也就是说 **必须由 D 先注册目标**（或经 layerwise rendezvous / metaserver 建立会话），
而当前代理（`load_balance_proxy_server_example.py`）只在 P 返回之后才联系 D。

→ 这一步属于设计文档中开发者 B 的接口工作（D 目标提前发布 + 代理/Rendezvous 接线），
不是配置能解决的；M1 需要把"代理/D 侧先建会话"这条链路补上。

### 16.3 已具备的条件（下一步直接用）

- 新镜像 + 干净档已验证可用（当前部署状态）；
- `pd_connector` 开关、`num_shared_buffers`/`independent_layers` 配置、P1 补丁（`probe/patch/m1b/`）均已就绪；
- dsa-base 自带 c8 支持、Host pool、DSA offload runtime，无需再移植 rc1 的补丁。

### 16.4 M1 打通：layerwise 代理（D-first）是缺的那一环（2026-11-02 验证）

§16.2 里"请求挂住"的根因不是连接器，而是**代理的请求顺序**。换成镜像自带的
`examples/disaggregated_prefill_v1/load_balance_proxy_layerwise_server_example.py` 后一次通过。

该代理的机制（读码确认）：

```
client → proxy
  ① proxy 生成 request_id，先发给 **D**（kv_transfer_params 带 metaserver URL）
  ② D 分配 KV 目标块，把 block ids / host / port 等 POST 到 proxy 的 /v1/metaserver
  ③ proxy 等所有预期 rank 都 POST 完 → dispatch_prefill_batch() 把请求发给 **P**
  ④ P 拿到 D 的目标后逐层 push；D 解码
```

而 plain 代理是 P-first（P 返回后才联系 D），P 会永远等不到 D 的目标 → 挂住。

启用方式（已写入 `group_vars`）：

```yaml
proxy:
  layerwise: true      # 跑镜像示例目录里的 layerwise 代理
  script: load_balance_proxy_layerwise_server_example.py
```

注意 `start.yml` 已经把 layerwise 代理**绑定到 proxy 节点真实 IP**（不能 0.0.0.0），
因为 D 要回调 `host:port/v1/metaserver`。

### 16.5 M1 实测结果（dsa-base 镜像 + 逐层连接器 + 共享 3 槽）

配置：`pd_connector: layerwise`、`num_shared_buffers: 3`、`prefetch_layers: 1`、
`independent_layers: [0, -1]`、P1 补丁（`probe/patch/m1b/pool_worker.py`）、layerwise 代理。

| 用例 | 结果 |
|---|---|
| 单 chunk pt=509 | ✅ 2.0s |
| 单 chunk pt=2429 | ✅ 3.2s |
| **双 chunk pt=4237** | ✅ **3.8s**（此前复用下必乱码） |
| 双 chunk pt=4989 | ✅ 3.9s |
| 三 chunk pt=5628 | ✅ 4.6s |
| 8K pt=8029 | ✅ 5.2s |
| 4 并发 × pt≈4981 | ✅ 4/4（4.9–7.3s） |

生成脚本确认 `"layerwise_num_shared_buffers": 3` 已渲染，P 日志 5 个 rank 都读到该配置。

### 16.6 尚未做的验收（下一步）

- 对照组：`independent_layers: all`（等价不共享）跑同一批 prompt，逐 token 比对输出；
- prefix 命中（同 prompt 连发）在复用下的正确性（对应 `b3222a7` 的回归场景）；
- 取消/抢占、Store 慢 / P-D 慢 交替（设计 §8 的 T7–T9）；
- `[CREDIT-BAL]` 等旧实验开关在新镜像里已不存在，无需清理。

### 16.7 对照组与非确定性结论（2026-11-02）

用同一组固定 prompt（temperature=0，`FIXED-A/B/C`）分别跑「共享 3 槽」与「所有层独立（不共享）」两组，
各自重复一次比对文本：

| prompt | 共享 3 槽：两次是否一致 | 不共享（对照）：两次是否一致 | 首次分歧位置（共享/对照） |
|---|---|---|---|
| p509（单 chunk） | ✅ 一致 | ✅ 一致 | — |
| p4237（双 chunk） | ❌ 不一致 | ❌ 不一致 | 144 / 74 |
| p4989（双 chunk） | ❌ 不一致 | ❌ 不一致 | — / 81 |

结论：**多 chunk 的逐字不一致是模型自身的非确定性**（对照组同样发生，且分歧点更早），
不是 buffer reuse 引入的。两组输出的语义均正确（都是"总结重复文本"的合理答复）。

因此 M1 的正确性判据采用「语义正确 + 与对照组行为同构」，
而不是「与对照组逐 token 完全相等」（后者在不共享路径上也不成立）。

### 16.8 取消测试

对复用配置连发 3 次"发 1.5s 后断开"的长请求，随后再发正常请求：
`pt=499 → 17.5s 输出正确`（17.5s 是重启后首请求的图编译冷启动）。
引擎未崩溃、proxy 保持 ok，未观察到旧代回调串入新请求。

### 16.9 M1 交付状态

| 项 | 状态 |
|---|---|
| 镜像 | `glm52-vllm-ascend:0.30.0-dsabase-4234a1f`（四节点已构建） |
| 复用 | `num_shared_buffers: 3`、`prefetch_layers: 1`、`independent_layers: [0, -1]` |
| P/D 交接 | `MooncakeLayerwiseConnector`（提供每槽 send-done 屏障） |
| 代理 | `load_balance_proxy_layerwise_server_example.py`（D-first + /v1/metaserver） |
| 补丁 | `probe/patch/m1b/pool_worker.py`（b3222a7 的 start_block 修正） |
| 实测 | 单 chunk 2.1s；双 chunk 4237/4989 → 4.6s/4.1s；8K → 4.7s；4 并发 4/4 |
| 全程 | 未使用 `torch.npu.synchronize()`，未使用自造额度信号量 |
