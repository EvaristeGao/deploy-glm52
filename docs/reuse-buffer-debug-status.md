# v0.30 Prefill reuse_buffer 调试状态（截至第 27 轮）

## 1. 结论摘要

| 项 | 状态 |
|---|---|
| 槽位并发安全（多请求同时预填） | ✅ 已解决：单一 credit（写前 acquire，`put()` 返回后 release）+ `max_num_seqs=1` |
| 单 chunk（≤4096 token）正确性 | ✅ 通过 |
| 多 chunk（>4096 token）正确性 | ❌ 未通过；需「前缀重载」闭环 |
| D 侧 reuse | 已关闭（`buffer_reuse_consumer` 默认 false），D 必须保留全序列 KV |

## 2. 架构要点

- **数据面**：mooncake → `LAYERWISE_DATA_PLANE="block_key"`（对象 = 一个 block 的本地 stage 全部层 KV）；memcache → `"gva"`（全局一致虚拟地址）。
  - GVA 下「地址一致」可用来做复用屏障；mooncake 下**只能**用「`put()` 返回」证明拷贝完成。
- **两个方向的事件**：
  - 输入就绪（put 之前）：`sync_save_events[ℓ]`（`torch.npu.Event`，compute stream）；
  - 输出完成（槽位可复用）：**只认 `put()` 返回**（现由 `release_slot_credit()` 表达）。
- **槽位约束**：层 ℓ 与 ℓ−B 同址，故 `write(ℓ)` 必须在 `upload(ℓ−B)` 之后；用 `Semaphore(B)` 即可（B = `len(layout.buffer_slots)`）。

## 3. chunked prefill + reuse 的必要条件（未完成的部分）

同一请求的 chunk2 需要 chunk1 的 layer ℓ KV，而 reuse 已把它挤掉，因此必须**从池里重新 load 回槽位**：

- load 起点：复用层 = 0（`get_layer_load_start_block(...)` 语义），非复用层 = `num_computed_tokens / block_size`；
- 上传仍只传本 chunk 新块；
- 恢复的块偏移与新块偏移不重叠，不是抢占。

### 代码位置

- 实际生效路径：`KVPoolWorker._prepare_layerwise_get_session()`（block-key 分支；
  `layerwise_protocol.prepare_layerwise_sessions` 那条在本部署中未被调用，勿改错位置）。
- 现有跨 chunk 恢复分支在 `_build_*` 的 `if self.use_block_key_layerwise:` 段，依赖 `request.load_block_keys`；producer 侧该字段为空 → 必须由 `_prepare_layerwise_get_session` 填充。

### ReqMeta 字段语义（实测）

| 字段 | 含义 | 实测值（4.5K prompt, chunk1） |
|---|---|---|
| `target_token_len` | `tracker.token_len` = 本 step 结束后的总 token 数 | 4096 |
| `save_end_token` | 本 step 保存到的 token 位置 | 4096 |
| `save_start_token` | 「本 step 之前尚未保存」的起点（**不是**累计值） | 0 |
| `num_computed_tokens` | 调度器传入（当前未送到 worker，实测 0） | 0 |
| `load_block_keys` | 待 load 块的 key（producer 为空） | `[]` |

由此，「已计算前缀 token 数」= `target_token_len − save_end_token`（chunk1: 0；chunk2: 应为 4096）。

## 4. 当前未解问题

- 加入前缀重载骨架后，4.5K 请求在 **chunk1 阶段** 即返回 500（`[PREFIX-CHK] target=4096 ... computed=0` 之后失败），需按时间戳抓当次完整栈定位；怀疑是最近改动（credit/前缀分支）引入的回归。

## 5. 诊断开关

| 开关/日志 | 位置 | 用途 |
|---|---|---|
| `LAYERWISE_LAYOUT_DEBUG=1` → `[LAYOUT-DEBUG]` | `layerwise_cache_layout.py` | 槽位布局（slots / sizes） |
| `REUSE_DIAG=1` → `[ADDR-DIAG]` / `[REUSE-DIAG]` | `kv_transfer.py` / `pool_worker.py` | 每层 base 地址表、entries 数 |
| `[PREFIX-CHK]` | `pool_worker._prepare_layerwise_get_session` | target/save_end/computed/reuse |
| `[LOAD-KEYS]` | block-key load 分支 | `load_block_keys` 是否为空 |
| `[GS-DIAG]` | 同上 | reuse_limit / has_spec / can_load / computed |

注意：`[EVT]`、`SLOT-WAIT`、`REUSE-PREFIX` 等日志已随改动增删，复跑前先 `grep -c` 确认是否还在。

## 6. 环境与回滚

- 已验证可用基线：`buffer_reuse: false` + C1 四文件挂载（chunked prefill 4096、`max_num_seqs: 64`），4 引擎 200、proxy ok。
- reuse 变体：`buffer_reuse: true` + C1 四文件 + reuse 四文件（`mooncake_layerwise.py` / `pool_scheduler.py` / `layerwise_cache_layout.py` / `kv_transfer.py`），`max_num_seqs: 1`。
- 每次切换需 `gen.yml` + `start.yml`，重启约 10–12 分钟；proxy 需手动确认 `/healthcheck`。

## 7. 下一步（顺序建议）

1. 抓 chunk1 的 500 堆栈（按时间戳过滤 `Traceback`），定位最近回归；
2. 修好后先验 3K（单 chunk）不回归，再验 4.5K（2 chunk）；
3. 若 4.5K 仍乱码，检查 `[PREFIX-CHK] computed` 是否等于 4096、`[REUSE-PREFIX]` 是否打印 `restore_blocks=32`；
4. 最后核对 `kv_caches` 数与地址表 entries 数是否一致（曾见 101 vs 117）。

## 8. 更正与补充（第 29–30 轮）

- **[更正] group 假设不成立**：实测 `num_kv_cache_groups = 1`（`use_hybrid=False, use_mamba=False`），
  因此 `_make_mooncake_layerwise_key` 里硬编码 group 0 是正确的，不是 500 的原因。
- **[确定 bug，已修] 额度不配对**：`wait_for_layer_load` 里原先只在「该层没有 load task」时
  `_acquire_slot_credit()`，而 release 仍然每层发生 → 信号量计数单调上升 → 复用屏障逐步失效。
  已改为：**只要 reuse 生效就每层 acquire 一次**，与「put 返回释放一次」严格配对。
- 待验：3K（单 chunk）不回归、4.5K（2 chunk）是否恢复。
