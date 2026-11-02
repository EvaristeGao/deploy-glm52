# Mooncake 逐层 P/D + 两端 KV Offload：可行性核查与实施设计

> 依据：《Mooncake 逐层 P/D 传输与两端 KV Offload 整体设计》（`mc.pdf`，作者 A/B 分工稿）
> 核查时间：2026-11-02
> 结论：**设计可达成**；但我们此前那版「单 connector + 自造槽位信号量」的实现**方向错误，整体废弃**。

---

## 0. 一页结论

1. **我们已有的**：v0.30.0rc1（`5f828ad`）里，layerwise 复用的**正确屏障机制已经存在**——在
   `kv_p2p/mooncake_layerwise_connector.py` 的 `MooncakeLayerwiseConnectorWorker.wait_for_layer_reuse()`
   里，用的是**每物理槽位的 send-done 事件 + 错误表**；由 `AscendMultiConnector` 在每层前调用。
2. **我们做错的**：把复用实现在**单个 `AscendStoreConnector` + 自造的 `pool_worker.py`** 上。那条路径的
   worker 只有 `wait_for_layer_load()`，**没有 `wait_for_layer_reuse()`**；`set_external_slot_release_waiter()`
   只有定义、全仓库无调用方。所以我们的复用**从来没有屏障**——这与实测完全吻合：B=9 必错、完全串行也错、
   只有 `torch.npu.synchronize()` 才"看起来对"。
3. **上游没有**：v0.30.0rc2 / v0.30.0（已发布）都不含 `dsa_pd_offload` / `host_backend` / `MooncakeHostPool`。
   D 侧 Host offload 能力只存在于 `mazifan0120/vllm-ascend` 的 `dsa_offload_rebase_30`（`4234a1f`）。
4. **要做的**：换到设计的多连接器拓扑 + fork 基线；补 Store 侧 reader/epoch 与 D 目标发布协议。
   我方可保留的资产是**配置/部署/验收/排障体系**，不是那版 reuse 代码。

---

## 1. 现状核查（可复现）

### 1.1 我们的源码基线

```
容器 vllm-ascend-gwh-glm52 内 /vllm-workspace/vllm-ascend
git log -1 → 5f828ad638ee4f9b12db87f9d5481f29076d37f9
             [Doc][Release] Prepare v0.30.0rc1 release (#17753)  2026-10-12
```

### 1.2 关键符号对比（`grep -rl <sym> --include=*.py vllm_ascend/ | wc -l`）

| 能力 | 符号 | 我们 `5f828ad` | 设计基线 `4234a1f` | 上游 `v0.30.0` |
|---|---|---|---|---|
| 每槽 send-done 事件 | `storage_send_done_events` | 3 | 3 | 3 |
| 层→物理槽映射 | `layer_storage_slots` | 3 | 3 | 3 |
| 槽位传输错误传播 | `_storage_send_errors` | 1 | 1 | 1 |
| 复用屏障接口 | `wait_for_layer_reuse` | 4 | 4 | 4 |
| 外部槽位释放 waiter | `set_external_slot_release_waiter` | 3 | 3 | 3 |
| DSA 稀疏 offload 配置 | `sparse_kv_offload_config` | 14 | 16 | 14 |
| fused overlap | `use_fused_overlap` | 3 | 4 | 3 |
| 失败策略 | `kv_load_failure_policy` | 2 | 3 | 2 |
| D Main Host pool | `MooncakeHostPool` | **0** | **2** | **0** |
| D Host offload 配置 | `host_backend` | **0** | **3** | **0** |
| 普通 connector DSA PD offload | `dsa_pd_offload` | **0** | **1** | **0** |
| Store 侧 reader 登记 | `reserve_read` / `seal_epoch` / `begin_slot_write` / `backing_generation` | 0 | **0** | 0 |
| D 目标发布协议 | `TargetManifest` / `writer_plan` | 0 | **0** | 0 |

读法：
- 前八行说明 **layerwise 传输 + 复用屏障 + DSA runtime 的脚手架，我们这条基线上就有**；
- `MooncakeHostPool` / `host_backend` / `dsa_pd_offload` 三行说明 **D 侧 Host offload 只在 fork 分支**；
- 最后两行说明 **设计里 A/B 要新建的接口，两处基线都还没有**——设计文档自己写的"拟实施接口，不表示目标基线已经提供"。

### 1.3 屏障到底在哪（关键）

我们基线与设计基线**完全一致**地实现于 P2P 连接器：

```python
# kv_p2p/mooncake_layerwise_connector.py  MooncakeLayerwiseConnectorWorker
def wait_for_layer_reuse(self, layer_idx: int) -> None:
    send_thread = self.kv_send_layer_thread
    if send_thread is None:
        return
    if layer_idx not in self.layer_storage_slots:
        raise RuntimeError(f"Mooncake layerwise reuse mapping is missing layer {layer_idx}")
    for slot_id in self.layer_storage_slots[layer_idx]:
        event = self.storage_send_done_events[slot_id]
        while not event.wait(timeout=LAYERWISE_REUSE_WAIT_LOG_INTERVAL_SECONDS):
            if not send_thread.is_alive():
                raise RuntimeError("Mooncake send thread stopped while waiting for layerwise buffer reuse")
        error = self._storage_send_errors.get(slot_id)
        if error is not None:
            raise RuntimeError(f"Mooncake failed to transfer physical KV slot {slot_id}: {error}")
```

- `layer_storage_slots[layer_idx]` → 该层覆盖到的**物理槽列表**（Main/Indexer 等多组件各一项）；
- `storage_send_done_events[slot_id]` → **该槽所有读者结束**的信号（不是"发送入队"）；
- `_storage_send_errors[slot_id]` → 失败必须抛出，不能被当成成功。

驱动方是 MultiConnector：

```python
# ascend_multi_connector.py
provider.wait_for_layer_reuse(layer_idx)      # 每层前调用
```

也就是说：**设计 §4 的第 3 条不变量（"P/D 已完成对源 buffer 的读取"）在上游 rc1 里已经有实现**，
只不过它属于 MultiConnector + `MooncakeLayerwiseConnector` 这条链路。

### 1.4 我们为什么没走上这条链路

| 项 | 我们（废弃方案） | 设计要求 |
|---|---|---|
| 连接器 | 单个 `AscendStoreConnector` | `AscendMultiConnector` = MooncakeLayerwise（传输/屏障） + AscendStore（保存/恢复） |
| 复用实现在 | 自造 `pool_worker.py`（reuse fork） | 由 connector 侧 `num_shared_buffers` 驱动，worker 不另起一套 |
| 屏障 | 自己写的 `Semaphore(B)` 额度 + per-step 前缀重载 | `wait_for_layer_reuse(layer_idx)` → 每槽 send-done 事件 |
| 结果 | 无屏障：源被覆写仍在读 → 池内容错 → 多 chunk 乱码 | —— |

补充证据：我们的 `pool_worker.py`（不管原版还是 fork）**只有 `wait_for_layer_load`，没有
`wait_for_layer_reuse`**；`set_external_slot_release_waiter` 在整个 vllm-ascend 里**没有任何调用方**。
设计文档也点了这一条：

> 基线 P/D worker 的 `wait_for_layer_load()` 是空实现；不能仅凭 MultiConnector 的
> provider-first fallback 就宣称有保护。显式启用复用却没有有效接线时应**启动失败**，不能静默降级。

---

## 2. mc.pdf 的核心约束（提炼）

### 2.1 覆盖前必须同时满足四条（§4）

设旧层 L 与新层 M 共槽 S：

1. L 对 S 的**计算读取**已完成（"不能把 KV scatter 完成等同于 attention 完成"，须按实际 NPU event 位置证明）；
2. Store 已完成对 L 源 buffer 的读取（**提交异步任务不算完成**）；
3. P/D 已完成对 L 源 buffer 的读取（**发送入队、RPC 提交成功均不算完成**）；
4. 之前任何关联失败都已传播（**不能把异常后的 `event.set()` 解释成成功**）。

两条附加规定（我们曾经违反）：
- **即使没有远程 GET**（首 chunk、零加载范围、本地 prefix 命中），共享槽位仍须过屏障；
  **不能因任务列表为空而跳过等待**；
- 必须区分 `source_read_done` 与 `snapshot_committed`：槽位屏障**不能**等整对象提交，
  否则形成"下一层等提交 → 提交等最后层 → 最后层等槽位"的环。

### 2.2 禁止项（§5.1）

- **不要把正常完成等待放在全局设备 synchronize 上**（我们实测过的 5.9s→50s 正是这条禁令）；
- 不要持 sender 完成回调需要的锁去等 event；
- 旧 callback 不得释放新一代槽位；单请求完成不得释放仍在服务其他请求的槽位。

### 2.3 两种独立完成条件（§6.2.3）

| 条件 | 含义 | 用途 |
|---|---|---|
| **P 源可释放** | 该槽当前代所有必要传输已停止读取且无关联失败 | 与计算/Store 读取屏障共同决定下一层能否覆盖 |
| **D 请求可执行** | 各 backing 的 Main writer 范围并集完整、各消费 rank 的 Indexer 完整、全部 terminal 成功、对消费 stream 可见 | 之后才允许 decode |

不能把"最后层发送过一次"或"收到一个 rank 的 DONE"当作 D 就绪。

### 2.4 范围（§1）

- 支持：同 P/D TP、PP=1、chunked prefill、prefix 命中、多请求、取消、失败退出；
- 不支持（须启动时显式拒绝）：D 侧复用、异构 TP、混合 attention/recurrent、Mamba/KDA、量化传输、speculative/MTP。

---

## 3. 可行性评估

### 3.1 结论

**可达成，但前提是接受三件事**：

1. 换拓扑：从单 `AscendStoreConnector` 换成 **MultiConnector（Layerwise + Store）**；
2. 换基线：D 侧 Host offload 依赖 fork 分支 `4234a1f`（上游任何 release 都没有）；
3. 新建两批接口（设计文档里 A/B 的工作），不是靠配置开关打开。

### 3.2 工作量与风险分解

| 部分 | 现状 | 需要做什么 | 风险 |
|---|---|---|---|
| P buffer reuse（槽位规划） | 有规划器（`layerwise_cache_layout.py`）与 `num_shared_buffers` | 让 `extract_layout_config()` 在 mooncake 后端按受支持条件启用 | 中：需对齐 cache spec 校验 |
| 复用屏障（P/D 侧） | **已有**（每槽 send-done） | 接通 MultiConnector 并加"未接线即启动失败"校验 | 低 |
| Store 保存/恢复 + 复用 | 有保存/恢复；缺 reader/epoch | 新增 `reserve_read`/`seal_epoch`/`begin_slot_write`/`backing_generation` | **高**：这是"Store 读取完成"的唯一依据 |
| 逐层 P/D 传输 | 有（MooncakeLayerwiseConnector） | 按 `writer_plan` 增加 Main 的 R2H/D2RH 与 Indexer D2D | 中高：需要 D 目标提前发布协议 |
| D 目标发布 | 有 rendezvous/metaserver 雏形 | 新增 `TargetManifest`/`TargetLease`/`writer_plan` + 代次校验 | 中高 |
| D Host offload | **无**（仅 fork 有） | 合并 `MooncakeHostPool` + `host_backend` + 稀疏 runtime 接线 | 中：属"已有实现可复用" |
| chunked prefill / prefix 命中 | 有 | 确保"命中层也要 restore→send"并走同一屏障 | 中：我们踩过"空任务跳过等待"的坑 |

### 3.3 与我方实测的对照（为什么这套设计能解决我们看到的现象）

| 我们的实测现象 | 本设计的对应条款 |
|---|---|
| B=9（共享槽）多 chunk 必错 | §4：覆盖前必须等 P/D 读取结束 |
| 额度窗口=1、复用距离 24 都无效 | 额度/距离都不是完成信号（§5.1：入队≠完成） |
| 每 9 层同步无效 | 屏障必须**逐层、逐槽**，不是周期性 |
| `torch.npu.synchronize()` 有效但 5.9s→50s | §5.1 明文禁止 |
| B=61（不共享）全对但无收益 | 无共享即无覆盖，问题被绕过而非解决 |

---

## 4. 实施路线（建议）

### 阶段 0：切换基线与拓扑（1–2 天）

- 拉 `mazifan0120/vllm-ascend` `dsa_offload_rebase_30` @ `4234a1f`；
- 用 MultiConnector 起 P/D（`MooncakeLayerwiseConnector` + `AscendStoreConnector`）；
- **先关复用**跑通，作为回归基线（设计要求："关闭 DSA offload 时保留既有普通 D HBM 路径"）。

验收：clean 档能力等价于我们现在的 `1m-v030-lw`（4 引擎 200、长短请求正确）。

### 阶段 1：P buffer reuse + P/D 屏障接线（A+B 共享）

- 打开 `layerwise_num_shared_buffers`（文档示例 3，`prefetch_layers: 1`，`independent_layers: [0, -1]`）；
- 校验 `wait_for_layer_reuse` 真的被调用（加断言：启用复用但 barrier 未接线 → 启动失败）；
- 逐层打印槽位覆盖与事件等待，证明"旧代读取结束后才写"。

验收：**双 chunk 长 prompt 输出正确**（这是我们之前唯一没做到的），且无全设备同步。

### 阶段 2：Store 侧 reader/epoch（A）

- `begin_slot_write` / `reserve_read` / `seal_epoch` / `complete_read`；
- `source_read_done` 与 `snapshot_committed` 分离；
- 空 GET / 本地 prefix 命中也必须过屏障。

验收：Store 慢、attention 未结束、P/D 慢三种交错下都不覆盖；`[CREDIT-BAL]` 这类临时手段全部删除。

### 阶段 3：D 目标提前发布 + 双路径传输（B）

- `TargetManifest`（身份/布局/范围/寿命/结果四组字段）+ `writer_plan`（按 `destination_ordinal % len(writer_ranks)` 选唯一 writer）；
- Main → D Host（D2RH），Indexer → D HBM（D2D）；
- D 就绪判定按"并集完整 + 各消费 rank Indexer 完整 + terminal 全成功 + 可见性"。

验收：D 侧 `kv_load_failure_policy="fail"`，缺数据不 decode、不静默重算。

### 阶段 4：边界与失败

取消/抢占、重试、shutdown、重复 DONE、迟到旧代 DONE、空 payload —— 按 `mc.pdf` §8 测试矩阵逐条过。

---

## 5. 配置形态（按设计文档，不引入 `reuse_buffer` 布尔开关）

```jsonc
// P：AscendStore（保存/恢复 + 复用）侧
"kv_connector_extra_config": {
  "backend": "mooncake",
  "use_layerwise": true,
  "layerwise_num_shared_buffers": 3,      // 决定共享层物理槽位数
  "layerwise_prefetch_layers": 1,          // 调度参数，不能当作 HBM 层数
  "layerwise_independent_layers": [0, -1]  // 首尾层独立分配
}
```

```jsonc
// D：稀疏 offload（Host Main + HBM Indexer）
"additional_config": {
  "sparse_kv_offload_config": {
    "enabled": true,
    "host_backend": "mooncake",
    "use_fused_overlap": true,
    "kv_load_failure_policy": "fail"
  }
}
```

要点：
- **不新增 `reuse_buffer` 之类的重复开关**（我们仓库现在的模板渲染 `use_layerwise_buffer_reuse`，需移除）；
- 未设置共享槽数时沿用基线默认，**不隐式开启复用**；
- 复用配置校验：合法层号、正整数、cache spec/shape/dtype 兼容、角色、runner、支持矩阵；不满足则**启动失败**。

---

## 6. 与我方现有交付的关系

| 资产 | 处置 |
|---|---|
| 干净档 `1m-v030-lw` 部署（4 引擎 200、长短请求正确） | **保留**，作为阶段 0 的回归基线 |
| `docs/a3-v030-layerwise-onboarding.md` + skill `a3-glm52-deploy` | **保留**（部署/验收/排障体系不受影响） |
| `/mnt/sfs_turbo/g00832294/probe/patch/{c1,reuse}/*`（自造 reuse 实现） | **废弃**，不再挂载 |
| `docs/reuse-buffer-debug-status.md` | 保留为"失败路径与证据"，供设计对照 |
| `[CREDIT-BAL]` / `reuse_mode.txt` 等实验开关 | 随废弃实现一起移除 |

---

## 7. 未决与需要确认

1. 文档提到的两个后续修复 **取不到**：`1898107…`、`b3222a7…`（在 fork 的两个可见分支 60/80 条历史里都不是祖先），
   需要作者提供所在分支或 patch；从代码特征看，`4234a1f` 已含 `_storage_send_errors` 与 `LAYERWISE_REUSE_WAIT`，
   推测"外部槽位释放接线"的核心语义已在内，但**未证实**；
2. fork 基线相对 upstream `v0.30.0` 的环境依赖（CANN/Triton/memfabric 版本）未核对，设计文档也提示不能假定；
3. D 侧 Host offload 与 `kv_cache_dtype=int8` 的组合是否在支持矩阵内，需在阶段 3 前确认。

---

## 8. 附录：核查命令

```bash
# 我们的基线
ssh root@ott9-a31 'docker exec vllm-ascend-gwh-glm52 bash -c "cd /vllm-workspace/vllm-ascend && git log -1 --format=%H"'

# 设计基线 / 上游
git ls-remote --heads https://github.com/mazifan0120/vllm-ascend.git | grep dsa_offload_rebase_30
git ls-remote --tags  https://github.com/vllm-project/vllm-ascend.git | tail

# 能力符号对比（在各自 checkout 里跑）
for s in storage_send_done_events layer_storage_slots wait_for_layer_reuse \
         set_external_slot_release_waiter MooncakeHostPool host_backend dsa_pd_offload \
         reserve_read seal_epoch begin_slot_write TargetManifest writer_plan; do
  printf '%-34s %s\n' "$s" "$(grep -rl "$s" --include=*.py vllm_ascend/ | wc -l)"
done
```
