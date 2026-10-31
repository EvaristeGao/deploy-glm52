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

## 9. 第 33–47 轮进展与当前卡点

### 已解决

1. **P/D 挂载分离**（`debug_mounts` / `debug_mounts_decode`）：reuse 版 `pool_worker` 只挂 P，
   D 挂 C1 基线。此前 D 反复 000 的根因即「producer 语义 worker 挂到了 kv_consumer」。
2. **额度配对**：`wait_for_layer_load` 里每层固定 `acquire` 一次（不再只在「无 load task」时），
   与「put 返回 / 无 save 任务时 release」严格配对。
3. **per-step 前缀重载**：搬到 block-key 分支（每 step 执行），
   `computed_blk = (target_token_len - (save_end_token - save_start_token)) // block_size`，
   实测 4.5K/2 chunk 时为 32，日志 `[REUSE-PREFIX] restore=32` 已验证。
4. **移除调度器 `num_computed_tokens` 透传**：该路径在某些请求上 `request is None`
   → `AttributeError: 'NoneType' object has no attribute 'num_computed_tokens'`（引擎崩溃）。
   per-step 公式不需要它。

### 实测事实（复用生效时）

- 容量：`kv_cache_size_tokens = 2,258,944`（3.52×，24 槽）；
- 单 chunk（≤4096 token）：输出正确；
- 4.5K（2 chunk）：`[REUSE-PREFIX] restore=32` 正确触发，但**请求超时**（get 未完成）。

### 当前唯一缺口

历史块的 get 没有完成，attention 在等 `layer_load_finished_events`。三种可能，用已布好的日志区分：

| 观察 | 结论 | 修法 |
|---|---|---|
| 有 `[REUSE-PREFIX]`、无 `[RECV-TASK]` | get 未提交（key 未注册/被 `prepare_load_entries` 过滤） | 让 store 端注册历史块的 put key，或跳过 tracker 过滤 |
| 有 `[RECV-TASK]`（`key_major=True`）但无完成 | range 任务未 set 事件 | 在该分支补 `layer_load_finished_events[layer].set()` |
| 有 `[RECV-TASK]` 且 get 卡住 | key 不在池中（key 不一致） | 对齐上传/下载的 key（namespace/pp_rank/group） |

### 环境

- 已验证基线：`buffer_reuse: false` + C1 四文件（P/D 相同），4 引擎 200、proxy ok、输出正确；
- reuse 变体：P 9 挂载（C1 四文件 + `pool_worker_reuse` + `mooncake_layerwise` +
  `pool_scheduler` + `layerwise_cache_layout` + `kv_transfer` + `worker`），D 4 挂载，
  `buffer_reuse: true`、`max_num_seqs: 1`；容量应 ≈2.26M。

## 10. 第 48–53 轮：多 chunk 的最后一段（引擎崩溃）

### 事实链（全部有日志/命令证据）

1. reuse 生效：`kv_cache_size_tokens = 2,258,944`（3.52×，P 9 挂载 / D 4 挂载）。
2. 单 chunk（≤4096 token）：输出正确。
3. 2 chunk（4.5K）：per-step 前缀重载**块数正确**
   （`[REUSE-PREFIX] layer=9 computed_blk=64 restore=64`，8K 时 96），
   但 recv 侧只看到每层 `ranges=1` 的任务（`key_major=True, wait_save=None`），
   随后**请求失败并导致 P 引擎崩溃（health 000）**，客户端表现为超时 / ConnectionReset。
4. range 任务的完成路径本身会 `layer_load_finished_events[layer].set()`（代码已核对），
   所以问题不在事件，而在 **get 本身**（key 不存在或 H2D 长度/地址非法）——崩溃更像后者。

### 下一步（最小实验，两条命令）

```bash
python3 /tmp/to_reuse.py && cd ansible && (nohup ... start.yml &) ; ~/bin/asup
~/bin/asask 300 90 ; ~/bin/asask 600 90        # 先 1 chunk 再 2 chunk
~/bin/as 'tail -c 200000 logs/vllm_p0.log | grep -aE "aicore|TimeoutError|Error" | tail -5'
```

判据：

| 现象 | 结论 | 修法 |
|---|---|---|
| `aicore timeout` / `device error` | H2D 的 get 长度/地址非法 | 检查 restore 的 `block_ranges` 是否只应含 **历史块**（不应与新块重叠），并核对 `block_len/stride` 与合并后 slot 尺寸 |
| Python `KeyError`/`RuntimeError` | key 不存在或 session 未提交 | 对齐 put/get key（group/pp_rank/namespace），或在 put 完成后显式 commit |
| 无异常但挂 | get 阻塞 | 用 `m_store.get` 前先存在性检查，失败则跳过恢复并回退到重算 |

### 诊断工具（本轮新增）

`~/bin/as`（限长执行）、`asask`（单请求一行结果）、`aslog`（近 N 条日志去重）、`asup`（等 4 引擎）。
日志锚点：`[META-DUMP]`（ReqMeta 字段）、`[REUSE-PREFIX]`（块数与首 key）、`[RECV-TASK]`（recv 收到的任务）、`[PUT-KEY]`（上传注册的 key）。

### 环境

- 已验证基线：`buffer_reuse: false` + C1 四文件，4 引擎 200（当前状态）；
- reuse 变体：`python3 /tmp/to_reuse.py` 一键切换；回滚：`/tmp/to_baseline.py`。

## §11 2026-10-29 复盘：restore 假设被证伪，问题定位到 chunked step 的 save

### 设备日志可获取（此前结论错误）

ACL 设备日志在**容器内**，不是宿主机：`/root/ascend/log/{run,debug,security}/`，
`run/device-0..16/device-<pid>_<ts>.log`，`logLevel=3`。
已给引擎容器加持久化挂载：`{{ logs.dir }}/ascend/{{ inventory_hostname }}:/root/ascend/log`
→ 宿主机 `/mnt/sfs_turbo/g00832294/logs/ascend/{p0,p1,d0,d1}/`，容器重建不丢。

### 运行时实验开关（免重启做对照）

`pool_worker_reuse.py` 的 `_reuse_mode()` 读容器内可见的
`/mnt/sfs_turbo/g00832294/logs/reuse_mode.txt`（按 mtime 热加载）：
- bit0(1) 恢复已上传块（默认）
- bit1(2) 恢复前等本层上一次 put 返回
- bit2(4) 只恢复「槽位被后续层覆写过」的层
- bit3(8) **完全不恢复**（对照组）

### 关键实测（reuse 打开，冷请求，唯一 nonce）

| pt token | chunk2 token | 块数 | mode=1 | mode=8（不恢复） |
|---|---|---|---|---|
| 3869 | 无（单 chunk） | 31 | 正确 | 正确 |
| 4101 | 5 | 33 | 正确 | 正确 |
| 4197 | 101 | 33 | 正确 | 正确 |
| 4221 | 125 | 33 | 正确 | 正确 |
| 4229 | 133 | 34 | **乱码** | **乱码** |
| 4333 | 237 | 34 | 乱码 | 乱码 |
| 4989 | 893 | 39 | 乱码 | 乱码 |

结论：
1. **restore 与这个 bug 无关**——完全关掉恢复，边界位置一模一样（≤33 块正确，≥34 块乱码）。
2. 因此先前所有围绕 restore 的修改（key 记账、整块 clamp、fence）都没有触及根因。
3. `restore=37` 之类在 decode 每步触发，只是把时延从 2.9s 拉到 5-6s，不是乱码来源。
4. 全量 prompt 重放（完整前缀命中，无 put、纯 get）同样乱码 → **pool 里的对象内容本身是错的**，
   即 P 侧 upload 写进池子的数据有问题，而不是读的一方读错位置。
5. 乱码边界 =「chunk2 跨到第 2 个 block」。chunk2 只有 1 个（部分）块时，该块走
   `save_last_block_key`（每请求固定 key）路径，正常；跨 2 块时全块走 range-put 路径，出错。
6. chunk1（save_start=0，range [0,32) 全块 put）是正常的（4030 token 全命中重放输出正确）。

### 下一步（一次重启内可完成）

对 chunked step（save_start_token>0 且跨 ≥2 块）打印 save 侧的
`starts / ends / keys / block_ids / sizes / save_key_block_offset / mask_num`，
与 chunk1 的同名字段对照，确认：
- A. `token_database.process_tokens(..., mask_num)` 返回的 starts 是绝对 token 位置还是相对 mask 的偏移；
- B. `prepare_value_layer(starts, ends, request.block_ids, layer)` 在 chunk2 时 `block_ids` 是否已包含新块；
- C. `save_key_block_offset` 与 `save_block_keys` 配对是否跨 step 混用（`save_block_keys` 每 step 在
  `_prepare_layerwise_put_session` 被清空重建，offset 随之改变，任何跨 step 复用都会错位）。

### §11 补充：已排除项与剩余嫌疑

已核实排除（子代理审计 + 源码复核）：
- `process_tokens()` 的 `start_idx = chunk_id * block_size` 是**绝对** token 位置（mooncake/store/data.py:648），
  与 `save_start_block = save_start_token // block_size` 坐标系一致 → 不是相对偏移错位。
- save/load 两侧 key 与 block_id 的配对（`key_start = start_block - key_block_offset` 后 strict zip）正确。
- load 侧 `load_block_keys` 全局块号索引、offset=0，与 `block_ids` 同下标。
- 布局 `layer_id` 索引指向正确物理槽（`entries_per_layer=1`, `start_idx=layer_id`）。
- 请求总块数不是限制：4221 token（33 块）请求在 decode 阶段长到 35+ 块仍然正确。

剩余唯一嫌疑：**chunked step 里 ≥2 个块的 range-put**。
chunk1（`save_start_token=0`，一次 put 32 块）正常；chunk2（`save_start_token=4096`，put 2 块）坏。
两者差异只有 `save_start_token` 与块的个数，其中最可疑的是
`kv_transfer.py:1516-1519` 的 TP 轮转切片
`starts = starts[self.tp_rank % self.put_step :: self.put_step]`：
32 块时每个 rank 稳定分到 16 块；2 块时只有 put_step=2 的 rank 各得 1 块，块数少于 rank 数时
分配退化，可能与 `_start_layerwise_put_keys`（worker 侧对**全部** key 预先 allocate/register）不一致，
导致「对象已创建但没有任何 rank 写入」→ get 回来是未初始化数据，且全程无错误码。

取证方案（一次重启内）：在 layer 0 打印 save 侧每个块的
`(key, block_id, addr, size, offset, chunk_id, rank, put_step)`，chunk1 与 chunk2 对照；
同时打印 get 侧同名字段。若 chunk2 出现「key 无数写入者」或 offset 只覆盖部分 rank，即根因。

## §12 根因定位：共享槽位屏障失效（B 的大小决定对错）

### 决定性实验

把 `layerwise.num_shared_buffers` 从 9 改成 **61**（61 层各占一个槽，等价于关闭共享），
其它一切不变（reuse 仍开、restore 仍开、同一份 patch）：

| prompt | B=9（共享，复用生效） | B=61（不共享） |
|---|---|---|
| 4236 token（chunk2 跨 2 块） | 乱码 | **正确** |
| 4988 token | 乱码 | **正确** |
| 8029 token（8K） | 乱码 | **正确** |
| 2 并发 × 5630 token | — | **正确**（23.7s / 15.3s） |

结论：**key、地址、索引全是对的**（子代理已排除），错的是「上传完成」这件事被提前认为完成了——
层 ℓ 的数据还没真正拷进池子，槽位就被层 ℓ+B 覆写，于是池子里那一块是别人的 KV。
1 块时拷贝小、赶得上；≥2 块时拷贝变长，赶不上 → 正好解释「chunk2 跨 2 块必坏」的阈值。

### 现在的疑点（下一轮直接查这一处）

`_release_slot_credit()` 现在挂在 kv_transfer 发送线程的 `finally`（kv_transfer.py:1885），
即「`m_store.put(...)` 返回就归还额度」。对 mooncake 这个 block_key 后端，**put 返回是否等于数据已拷完，需要确认**：
若是「发起即返回」，那么额度归还点必须改到真正的完成/commit 信号上（代码里存在 commit 阶段，
见 `_emit_commit_debug_event`），或者改成对 D2H/put 的显式同步。

### 当前环境状态

`num_shared_buffers: 61`，reuse 打开、多 chunk / 8K / 2 并发全部正确——
功能可用，但没有容量收益（等于不复用）。要拿回 3.5× 容量，必须先修屏障，再把 B 调回小值。

### §12 补充：put 是同步的，所以问题在「额度计数/时序」而不是「归还太早」

查 `mooncake_backend.py:437 put()`：它调用 `store.batch_put_from_multi_buffers(keys, addrs, sizes, config)`
并**逐个检查返回码**后返回，属于同步 API（还有独立的 `batch_commit` → `batch_put_session_end`）。
所以「put 返回 ≠ 拷完」这个假设可以排除，发送线程 `finally` 里归还额度并不算早。

剩下唯一解释：**额度计数或时序不对称**，使某些层的写发生在同槽上一位上传完成之前。需要核对：
1. `wait_for_layer_load` 每层每 step 恰好 acquire 一次（当前看起来是）；
2. 归还点有三处：`_submit_attention_save`（无 save 任务时）、`save_kv_layer`（同理）、
   发送线程 `finally`（每个 batch 一次）——三处的触发条件是否可能对同一层重复归还；
3. `num_prefetch_layers > 0` 时，层 ℓ 的 acquire 与其 save 的入队顺序是否仍严格按层递增。

取证（一次重启）：给 `_acquire_slot_credit` / `_release_slot_credit` 各加一个每 step 计数器，
step 结束时打印「acquired / released / 当前信号量值」，不平衡立刻可见。

## §13 终局证据：store 的 device 拷贝异步，且没有可用的完成信号

在 B=9（共享）下继续做对照，全部通过 `reuse_mode.txt` 热切换：

| 模式 | 含义 | 4237 | 4989 | 说明 |
|---|---|---|---|---|
| 305（窗口=1） | 完全串行，上一层 put 返回后才允许下一层写 | 乱码 | 乱码 | **窗口大小不是原因** |
| 817（窗口=1 + sleep 5ms） | 归还前睡 5ms | 55.7s，前半句对、后面对 | — | 拷贝确实还在飞 |
| 1329（窗口=1 + `torch.npu.synchronize()`） | 归还前做全设备同步 | **正确** | **正确** | 拷贝在某个 NPU 流上，未被等待 |
| 4401（窗口=1 + `current_stream().synchronize()`） | 只同步当前流 | 乱码 | 乱码 | 拷贝不在当前流上 |
| B=24（另有 15 个独立槽） | 复用距离 24 | 乱码 | 乱码 | 加大距离不够 |
| B=61 | 不共享槽 | 正确 | 正确 | 唯一正确配置 |

结论：
1. `mooncake put` 返回**不代表** device 侧拷贝完成；`batch_put_from_multi_buffers` 的返回码只是提交状态。
2. 拷贝发生在内部流上，`current_stream().synchronize()` 覆盖不到；只有全设备同步能等到。
3. 全设备同步的代价是 4.2K 请求从 5.9s 涨到 50s（每层同步会把整条流水线串死），**不可用作修复**。
4. 因此「共享槽位」在当前后端下无法靠窗口/距离保证正确，必须有 store 提供的真正完成/刷出原语
   （或改走「自己 D2H 到 host 再 put」的数据路径），否则只能不共享。

当前可用状态：`num_shared_buffers: 61`（reuse 打开、无槽共享），多 chunk / 8K / 2 并发正确，但无容量收益。

### §13 补充：后端没有可用的完成原语（已核实）

- `kv_transfer.py:1837` 保存路径**每层都调用了** `m_store.batch_commit(active_keys)`（`batch_put_session_end`）并检查返回码，
  仍然挡不住后续覆写 → commit 也是异步提交语义，不等于 device 侧读已结束。
- mooncake python 包里没有 store 级完成/刷出接口：`wait_send_done` 只在 `mooncake_ep_buffer.py`（EP 通信），与 KV store 无关。
- `put_from`（host 侧指针）只有 `memcache_backend.py:526` 有，`mooncake_backend` 没有 → 无法改成「先 D2H 到 host 再 put」。

因此本后端下，共享槽位只能靠 `torch.npu.synchronize()`（每层约 0.8s，4.2K 请求涨到 50s）才正确，
工程上不可接受。要拿回容量必须由后端提供完成语义，或换支持 host 指针 put 的接口。
