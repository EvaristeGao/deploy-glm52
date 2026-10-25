# Layerwise 池 load 死锁分析（P 侧，v0.30.0rc1）

> 结论先行：**P（kv_producer）在 layerwise 模式下发起池 load 时会与计算线程形成环形等待**；
> 与我们的 buffer-reuse 补丁无关，也不是 `--enable-prefix-caching` 的开关问题，而是
> `attention_start_gate` 的等待语义与「producer 需求加载」组合出的上游缺陷。

## 1. 现象

- 4 引擎 `/health` 均为 200、proxy `healthcheck ok`、D 侧直连请求正常（0.6s），
  但 **chat 请求挂住**；P 直连也超时。
- 小并发、单波次请求正常；**多波次共享前缀请求**（GSP 50%/90%）在完成 2~3 条后整体停摆。

## 2. 复现矩阵（200K `1m-v030-hyb`）

| 负载 | 结果 |
|---|---|
| GSP 8 条 / 4 组×2 / 并发 1 | ✅ 108.6s，8/8 |
| GSP 8 条 / 并发 2 | ✅ 79.5s，8/8 |
| GSP 8 条 / 并发 8（单波次） | ✅ 53.6s，8/8 |
| **GSP 16 条 / 4 组×4 / 并发 8（两波次）** | ❌ 3/16 后停摆 |
| GSP 32 条 / 8 组×4 / 并发 8 / 输出 1024 | ❌ 3/32 后停摆 |
| 关闭 reuse（`buffer_reuse: false`）后同样负载 | ❌ 仍停摆 |
| 关闭 P 侧 `--enable-prefix-caching` 后同样负载 | ❌ 仍停摆 |

## 3. py-spy 证据（P worker）

主线程：

```
wait (threading.py:655)
  wait_for_layer_load (pool_worker.py:2836)
  wait_for_layer_load (ascend_store_connector.py:337)
  wait_for_kv_layer_from_connector (attention/utils.py:509)
  forward (sfa_v1.py:1964) → execute_model
```

recv 线程（`KVCacheStoreLayerRecvingThread`）：

```
event.synchronize (torch_npu/npu/streams.py:192)      # 或 attention_fence.py:72 wait
  _handle_range_layer_task (kv_transfer.py:2069)
  _handle_request (kv_transfer.py:2116)
  run (kv_transfer.py:779)
```

即：**计算线程等 load 完成事件，load 线程等「attention 计算起点」的 gate/事件** → 环形等待。

## 4. 代码链

1. `pool_worker.process_layer_data()` 依据 `request.load_spec` 构建 `layer_load_tasks`（producer 侧同样会建，
   因为 AscendStore 的**外部前缀缓存**命中会要求把池里的块 load 回 HBM）。
2. `_submit_ready_layer_loads()` 提交任务时：
   ```python
   if self.layer_load_tasks[layer_id] and layer_id != self.current_layer:
       attention_start_gate = get_attention_compute_start_gate()   # 提前提交的 prefetch 层绑 gate
   ```
3. recv 线程 `_handle_range_layer_task` → `data.attention_start_gate.wait()`（`kv_transfer.py:2058-2060`），
   再 `event.synchronize()`；gate 由 attention 路径 `record()` 打开。
4. 主线程推进到该层时 `wait_for_layer_load()` 等 `layer_load_finished_events[layer]`，
   而 recv 线程正卡在这一层的 gate 上 → **互相等待**。

## 5. 已排除

| 假设 | 结论 |
|---|---|
| 我们的 buffer-reuse 补丁（P 侧 `prefetch_layer_map`） | ❌ 关 reuse 仍复现 |
| `max_model_len`（200K / 640K / 1M） | ❌ 三档都复现 |
| P 侧 `--enable-prefix-caching` | ❌ 关闭后仍复现（外部 lookups 走连接器，不受该 flag 控制） |
| GSP 数据集本身 | ⚠️ 随机数据集长请求也曾停摆，触发面更宽（共享前缀只是更稳定复现） |

## 6. 尝试过的修复（均未生效）

1. **stale gate 放行**：`AttentionComputeStartGate.wait()` 检测到自身已被 `reset` 时直接返回 True。
   → 卡点变成 `event.synchronize()`：gate 已 record，但该 NPU event 永不完成。
2. **需求加载时 cancel 该层 gate**（`wait_for_layer_load` 里 `gate.cancel()`）。
   → 仍然停摆，说明阻塞不只是 host 侧 gate，**计算流与 recv 线程的 H2D 传输之间存在流级依赖**。

## 7. 建议的上游修法（待与 vllm-ascend 对齐）

1. producer 侧的「外部前缀命中」不要走 layerwise 的**门控 prefetch** 通道：需求加载应直接提交、
   不绑 `attention_start_gate`（`layer_id != current_layer` 的条件不足以区分 prefetch 与 demand）；
2. 或 recv 线程在 `synchronize` 前检查该层是否已成为「当前需求层」，是则跳过 gate；
3. 或在主线程 `wait_for_layer_load` 前显式 `record`/`cancel` 该层 gate（当前实现 `cancel` 后仍有流级等待，
   需要进一步查 `_handle_range_request` 提交的 H2D 与计算流的同步关系）。

## 8. 当前状态

- 已恢复 `1m-v030-hyb`（200K + reuse）4×200、proxy ok，2K/16K 冒烟通过；
- **55K/1K、50%/90% 命中的正式压测在该缺陷修复前无法按并发 8 跑完**，
  只能退化到并发 1（可用但吞吐无意义），或等上游修复。
