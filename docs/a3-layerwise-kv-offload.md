# A3 Mooncake 分层 KV offload（layerwise / hybrid attention）

对应官方文档（v0.30.0rc1，developer preview）：
- [Mooncake Layerwise Hybrid Attention](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/mooncake_hybrid_attention.html)
- [Mooncake Layerwise Adaptation and Optimization](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/mooncake_layerwise_adaptation_and_optimization.html)
- [Layerwise and Sparse KV Cache Offloading](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/layerwise_and_sparse_kv_cache_offloading.html)

本项目实现两档（同一份 `use_layerwise`，连接器形状不同）：

| 档位 | 形状 | 说明 |
|---|---|---|
| **`1m-v030-hyb`**（★ 当前启用） | **单个 `AscendStoreConnector`（P `kv_producer` / D `kv_consumer`）** | hybrid 文档 §Configuration 的 connector（见 §3.1）；`kv_role` 按 P/D 分离拆，不照抄文档的 `kv_both` |
| `1m-v030-lw` | `MultiConnector[MooncakeConnectorV1, AscendStoreConnector]` | 保留 P/D 直连 connector 的变体（早期按 MultiConnector 拼的） |

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
| 1 | **mooncake client 必须带 PR #2881 的 7 个接口**：`batch_put_session_start`、`batch_put_from_multi_buffer_ranges`、`batch_put_session_end`、`batch_put_session_revoke`、`batch_get_session_start`、`batch_get_into_multi_buffer_ranges`、`batch_get_session_end` | ❌ 基础镜像里是 `mooncake-transfer-engine-npu 0.3.11.post1`（2026-06-10，早于该 PR），**没有**这些接口（python wrapper 未暴露、`.so` 符号计数为 0）；vllm-ascend 启动时 fail fast 报 `Mooncake client does not support batch_*`。✅ **解法见 §2.1**（`image_build/Dockerfile_mooncake_layerwise`） |
| 2 | p0 上有 mooncake_master（本项目 kvpool 单主）+ `mooncake.json` | ✅ 已有（`mooncake-p0` 容器） |
| 3 | 官方标注 **real-NPU E2E 尚未完成**（只有 CPU mock 285 例 + 静态检查），"No latency or throughput improvement is claimed without hardware measurements" | ⚠️ 属 developer preview，别当生产特性 |

---

## 2.1 换 mooncake：`image_build/Dockerfile_mooncake_layerwise`

**版本对照**（PyPI 包名 `mooncake-transfer-engine-npu`，Ascend NPU 变体，来源
<https://github.com/kvcache-ai/Mooncake>）：

| 版本 | 发布 | 是否含 PR #2881 |
|---|---|---|
| 0.3.11.post1 | 2026-06-10 | ❌ 基础镜像里的版本 |
| 0.3.12 / 0.3.12.post1 | 2026-07-23 / 07-24 | ❌（早于 PR 合并） |
| **0.3.13 / 0.3.13.post1** | 2026-08-26 / 08-31 | ✅（PR #2881 于 **2026-08-10** 合入 main，merge commit `78726c5`） |

已实测 `0.3.13.post1` 的 **cp312-manylinux_2_35-aarch64** wheel：7 个 range/session 符号全部存在，
且仍自带 `mooncake_master`（kvpool 单主需要它）—— 所以只需一次 pip 升级，不必从源码构建。

Dockerfile 做三件事：
1. `pip install --upgrade --no-deps mooncake-transfer-engine-npu==0.3.13.post1`
   （`--no-deps`：该 wheel 只依赖 aiohttp / requests，基础镜像已有，避免顺带动 vLLM 相关包）
2. **构建期自检**：`strings mooncake/*.so | grep` 逐个确认 7 个接口存在，缺一个就让构建失败
   （用符号检查而非 `import` —— import 需要宿主机挂进来的 Ascend 驱动，构建容器里没有）
3. 确认 `mooncake_master` 仍在

```bash
# 必须在 aarch64 上构建（wheel 是 aarch64 + cp312；控制机若是 x86_64 需 buildx --platform linux/arm64）
# 最省事：直接在 A3 节点上构建（节点已有基础镜像 + 能连 PyPI + 有 docker）
scp image_build/Dockerfile_mooncake_layerwise root@<node>:/tmp/lwbuild/Dockerfile
ssh root@<node> 'cd /tmp/lwbuild && docker build -f Dockerfile \
  -t <你的镜像名>:v0.30.0rc1-a3-mc0.3.13 .'
# 内网镜像源：--build-arg PIP_INDEX_URL=<你的源>
```

换镜像后同步改 `group_vars`：

```yaml
image:          <新镜像>   # 引擎容器（layerwise 的 data plane 在这里）
mooncake.image: <新镜像>   # kvpool 的 mooncake_master 容器（旧镜像也能跑单主，可不动）
```

> 若将来 PyPI 上没有含目标 PR 的版本，按官方文档 §5.1 从源码构建：
> `https://github.com/kvcache-ai/Mooncake`，参考 merge commit `78726c5ca6417f3926c384320728bb7c426e8f22`。

---

## 3. 三条硬约束（镜像源码，`use_layerwise` 时启动即校验）

| 约束 | 出处 | 违反后果 | 本档处理 |
|---|---|---|---|
| **CP/DCP 必须 = 1** | `mooncake_layerwise.validate_topology()`：`prefill_context_parallel_size`/`decode_context_parallel_size` > 1 直接 `ValueError("...supports TP/PP, but not context parallelism")` | 启动失败 | 本档**不渲染任何 CP 参数**（无 `--prefill-context-parallel-size`/`--decode-context-parallel-size`/`--cp-kv-cache-interleave-size`），也不给 `context_parallel_size` / `cp_kv_cache_interleave_size` 键 |
| **P/D 的 TP 必须一致** | `validate_runtime()`：`Mooncake layerwise does not yet support prefill/decode TP mismatch`；PD 分离下 producer/consumer 的 dcp/pcp 也必须一致（否则 pool 里 layerwise GVA shard 布局错位） | 启动失败 | P/D **都 TP16**（`1m-v030-lw` / `1m-v030-hyb` 都是 2 节点 × 每节点 1 引擎；TP16 也是 1M KV 容量所必需，见 commit 8d2e320） |
| **先 eager 验证** | 文档："Start validation in eager mode. Graph-mode execution and additional attention backends require separate integration validation." | 未验证路径 | decode `enforce_eager: true` + `async_scheduling: false` → 渲染 `--enforce-eager`，**不渲染** `--compilation-config` |

> 顺带：`validate_pp_groups()` 还要求 PP 的每个 stage 在每个 KV group 里都有层（本档无 PP，不受影响）；
> layerwise 与 **TP mismatch** 也不兼容（本档 TP 一致）。

---

## 3.1 `1m-v030-hyb`：照抄文档 §Configuration 的那一档（★ 当前启用）

hybrid 文档 §Configuration 给的是**单实例**命令：

```bash
export MOONCAKE_CONFIG_PATH=/path/to/mooncake.json
vllm serve /path/to/hybrid-model \
  --tensor-parallel-size 8 --enforce-eager --enable-prefix-caching \
  --enable-chunked-prefill --max-num-batched-tokens 4096 \
  --kv-transfer-config '{
    "kv_connector": "AscendStoreConnector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {
      "backend": "mooncake", "use_layerwise": true,
      "layerwise_prefetch_layers": 2,
      "layerwise_max_transfer_blocks": 64,
      "layerwise_max_transfer_bytes": 16777216
    }
  }'
```

本档把它落到 2P+2D（4 节点），底子是**已实机跑通的 `1m-v030`**：

| 项 | 文档 | 本档 | 说明 |
|---|---|---|---|
| connector | 单 `AscendStoreConnector` | **同** | 不再是 `MultiConnector`，也不再挂 `MooncakeConnectorV1`；P/D 交接改由 KV 池承担（P 写池、D 查池读） |
| `kv_role` | `kv_both` | **P `kv_producer` / D `kv_consumer`** | **不照抄文档**：`kv_both` 是单实例口径（同一份既写又读）；本项目 P/D 分离，按角色拆。交接靠池：P 写 block、D 用同一套 layerwise block key 查池读（两侧 TP/KV-group 一致才查得到 —— 这也是文档要求 P/D TP 一致的实际原因）。另 decode 不带 `load_async`：镜像 `pool_scheduler.py:768/820` 是 `load_async and not use_layerwise`，layerwise 下自动失效 |
| layerwise 三旋钮 | 2 / 64 / 16777216 | **原样** | — |
| 额外键 | — | `kv_load_failure_policy: "recompute"` + `lookup_rpc_port: "0"` | 项目既有约定键，不改文档语义 |
| `--enforce-eager` | 有 | P（模板恒有）+ **D 也开** | 文档："Start validation in eager mode" |
| `--enable-prefix-caching` | 有 | P 开 / **D 关** | **唯一有意差异**：P/D 部署下官方 A3 参考一律关 decode 侧；要完全照文档就把 `decode.enable_prefix_caching: true` |
| `--enable-chunked-prefill` + `--max-num-batched-tokens 4096` | 有 | **同** | — |
| `max_model_len` / TP / CP | 示例 TP8、无 context 说明 | **64K**（跟随已跑通档；1M 值以注释保留）、P/D 都 **DP2 TP16**、**无 CP/DCP** | 文档 §Scope 硬约束："DCP, PCP and prefill/decode TP mismatch remain unsupported" ⇒ 必须去掉 `1m-v030` 的 DCP8，并把 P/D TP 取一致 |

⚠️ 文档 §Scope 还有一句和前面那个"能不能 reuse"的争论直接相关的原文：

> Only complete, coordinator-aligned block snapshots are published. **Partial block offloading and
> cross-layer cache-buffer reuse are not part of this hybrid implementation.**

即 **hybrid attention 这条路径本身就不含"跨层 buffer 复用"**，所以本档**不配** `layerwise_num_shared_buffers` /
`layerwise_independent_layers`（且这两个键在 `backend=mooncake` 下会被
`mooncake_layerwise.extract_layout_config()` 静默忽略，见 `a3-prefill-kv-offload.md` §4.1 的代码链）。

---

## 4. 两档 layerwise 与底档 `1m-v030` 对照

| 项 | `1m-v030`（已跑通） | `1m-v030-lw` | `1m-v030-hyb`（当前） |
|---|---|---|---|
| kv-transfer-config | `MultiConnector[MooncakeConnectorV1, AscendStoreConnector]` | `MultiConnector[MooncakeConnectorV1, AscendStoreConnector(+use_layerwise)]` | **单 `AscendStoreConnector`（P `kv_producer` / D `kv_consumer`）** |
| prefill dp/tp/local | 2 / 16 / 1 | 2 / 16 / 1 | 2 / 16 / 1 |
| decode dp/tp/local | 4 / 8 / 2 | **2 / 16 / 1** | **2 / 16 / 1** |
| P/D TP 一致 | 16 vs 8 ❌（layerwise 不允许） | 16 vs 16 ✅ | 16 vs 16 ✅ |
| CP / DCP / interleave | 1 / 16 / 8 / 128 | 不渲染（=1） | 不渲染（=1） |
| `max_model_len` | 65536（原 1M 以注释保留） | 1048576 | 65536（原 1M 以注释保留） |
| prefill `max_num_batched_tokens` | 8192 | 4096 | **4096**（文档） |
| prefill chunked-prefill | 不渲染 | 渲染 | **渲染**（文档） |
| decode 图模式 | `FULL_DECODE_ONLY` + `--async-scheduling` | `--enforce-eager` | **`--enforce-eager`**（文档） |
| decode prefix caching | 关 | 关 | 关（见 §3.1） |
| prefill / decode gpu-mem | 0.85 / 0.9 | 0.9 / 0.92 | **0.85 / 0.9**（跟随已跑通档） |
| P/D 交接 | MooncakeConnectorV1 | MooncakeConnectorV1 | **KV 池（P 写、D 读）** |
| layerwise 旋钮 | 无 | 有（MultiConnector 形态） | 有（文档单 connector 形态） |

> 注：`1m-v030` 的 `max_model_len` 与 gpu-mem 是实机调参后的当前值（64K / 0.85 / 0.9）；
> 本表按 group_vars 现值填写，改档后请以文件为准。

---

## 5. 怎么跑（4 节点 2P+2D）

```bash
cd ansible
# 0) 先把带 PR #2881 的 mooncake 镜像打到节点上，并把 group_vars 的 image / mooncake.image 指过去
# 1) 切档
#    inventories/a3/group_vars/all.yml:  deploy_config: 1m-v030-hyb
.venv/bin/python simulate_modes.py --inv a3                      # 先看渲染
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp
ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/check.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/status.yml
```

预期形状（hyb / lw 档都是每节点 1 引擎、TP16）：P 2 个端点（p0/p1:9081）+ D 2 个端点（d0/d1:9900），
`wait_ready` 探测目标 5（P2 + D2 + proxy）。

### 5.1 排查：kvpool 分层 range 审计日志（`range_debug`）

镜像里 `envs.VLLM_ASCEND_KVPOOL_RANGE_DEBUG` 控制 `[KVPOOL_RANGE_DEBUG]` 事件，**本项目已做成档位开关**：

```yaml
# inventories/a3/group_vars/all.yml → 对应档的 pd_cluster
range_debug: true        # true ⇒ 模板渲染 export VLLM_ASCEND_KVPOOL_RANGE_DEBUG=1（P/D 两侧都加）
```

打开后每个 `(direction, layer)` 一行 JSON（`kv_transfer.py:66/83`）：

```json
[KVPOOL_RANGE_DEBUG] {"event":"range","direction":"save","layer_id":0,"key_count":10,
  "requested_bytes":[98304,...],"sizes":[[...]],"object_offsets":[[...]],"results":[0,...]}
[KVPOOL_RANGE_DEBUG] {"event":"commit","layer_id":78,"key_count":10,"results":[0,...]}
```

怎么读：

| 现象 | 含义 |
|---|---|
| 完全没有 `event:"range"` 行 | save 根本没触发：prompt 没到 `cache_transfer_granularity`（= 128 token）或没跨过新的满块边界（见 `metadata.py:1099-1145`，`discard_partial_chunks` 默认 True、尾部块在 `has_layer_reuse=False` 时不保存） |
| 有 `range` 行但 `results` 里出现负数 | 该 key 的 range **put 失败**（save 侧随后 `batch_put_session_revoke`，远端对象永远不可见）——把这一行连 `sizes`/`object_offsets` 一起贴给 PR #16004 作者 |
| 有 `range(save)` 全 0，但**没有** `event:"commit"` | 会话没走到提交点（commit 只在 `layer_id == final_layer_id` 触发，最后一层是 MTP 层） |
| 有 `event:"range", direction:"load"` | D 真在从池里取块（配合 `consumer_is_to_load: true`） |
| master 侧 `/metrics` 的 `master_batch_put_end_requests_total` 涨了 | 提交成功；`master_key_count` / `master_allocated_bytes` 会跟着涨 |

抓取方式：

```bash
grep -h "KVPOOL_RANGE_DEBUG" /mnt/sfs_turbo/g00832294/logs/vllm_{p0,p1,d0,d1}.log | tail -50
curl -s http://172.168.4.237:52052/metrics | grep -E "batch_put_(start|end|revoke)|batch_exist|master_key_count|master_allocated_bytes"
```

⚠️ 跑性能/基准前把 `range_debug` 关回 `false`（每层一行 JSON，开销不小）。当前 `1m-v030-hyb` 档**开着**，就是为了查"写不进池"这件事。

### 5.2 实测记录：range 写入失败（2026-10-19，两轮 env 规避均失败）

**现象**：pool 永远是空的。master `/metrics`：`batch_put_start` 有、`batch_put_end` 恒 0、`key_count=0`、
`allocated_bytes=0`；P 侧日志 `Block-key layerwise hit check ... hit_blocks=0/10`。
`range_debug` 打开后每层一行，`results` 全负数：

```json
[KVPOOL_RANGE_DEBUG] {"event":"range","direction":"save","layer_id":0,"key_count":16,
 "requested_bytes":[116736,...],"sizes":[[83968,32768],...],"object_offsets":[[0,83968],...],
 "results":[-800,-800,...]}
```

**默认 env（不加任何规避）** —— 优雅失败：range put 走 NPU→NPU 的 HCCS P2P，通道建不起来：

```
[HixlEngine] Failed to auto connect, local_engine:...:20012, remote_engine:...:21121, timeout:10000 ms
  → aclrtIpcMemImportByKey failed. return[107000]
  → halDeviceDisableP2P failed, drvRetCode=17 → rtDisableP2P: driver error:internal error
  → [Channel] Create failed local=[protocol=hccs, devPhyId=0] remote=[... devPhyId=11]
  → client_service.cpp:4458  Range write failed, entry=0..15, error=-800
```

结果：16 个 key 全部 revoke，`put_end` 从不出现 ⇒ 池永远空。**请求本身仍能返回**（`kv_load_failure_policy=recompute`）。

**第一轮 `MC_STORE_MEMCPY=1`** —— 更糟，直接崩：

```
W transfer_executor_base.cpp:64  Dest buffer device_id=1 >= endpoint count 1,
                                 falling back to 172.168.4.237:20176
I transfer_executor_base.cpp:689 Retry transfer to:172.168.4.237:20176
!!!!!!! Segfault encountered !!!!!!!
→ Worker proc VllmWorker-0 died unexpectedly → p1 侧 gloo 断（EngineDead）
```

**第二轮 `ASCEND_A3_ENABLE=1`** —— 有进展但同样崩：`put_start` 成功且对象真分配了
（`master_key_count=16`、`allocated_bytes≈108 MiB`、无 revoke），但第一个 range put 仍然

```
W Dest buffer device_id=8 >= endpoint count 1, falling back to 172.168.4.237:20802
I Retry transfer to:172.168.4.237:20802
!!!!!!! Segfault encountered !!!!!!!
```

⇒ 两轮 env 规避都回退（group_vars 里只留排查记录，不再加 `extra_env`）。

**第三轮：TP 降到 8（`1m-v030-hyb-tp8` 档，P/D 都 TP8、每节点用卡 0~7、64K）** —— 结果与 TP16 的默认 env 完全一样，
说明**与 TP/设备数量无关**：

```
probe 2098 tokens → 请求正常返回 200
[KVPOOL_RANGE_DEBUG] layer 0, key_count=16, results=[-800 ×16]
Range write failed ×16 → BatchPutRevoke 1 req/16 items → BatchPutEnd 0 → master_key_count 0
eventlog: device_id=4 >= endpoint count 1, falling back to 172.168.4.237:20429 (×34)
          aclrtIpcMemImportByKey failed[107000] / rtsIpcMemImportByKey: driver error:invalid value
          [HixlEngine] Failed to auto connect local(...:20041) → remote(...:20429)
```

四轮汇总：

| 轮 | TP | env | put_start | 写入 | commit | 引擎 |
|---|---|---|---|---|---|---|
| 1 | 16 | 默认 | ✅ 16 items | ❌ `-800` ×16（HCCS P2P 建链失败） | 0 | 存活（优雅失败） |
| 2 | 16 | `MC_STORE_MEMCPY=1` | ✅ | ❌ endpoint fallback(dev=1) → Retry | 0 | **Segfault** |
| 3 | 16 | `ASCEND_A3_ENABLE=1` | ✅ 且对象已分配（key_count=16 / 108MiB） | ❌ endpoint fallback(dev=8) → Retry | 0 | **Segfault** |
| 4 | **8** | 默认 | ✅ 16 items | ❌ `-800` ×16（同样 IPC import 失败） | 0 | 存活（优雅失败） |

**结论（给开发）**：`Dest buffer device_id=N >= endpoint count 1` 说明目标 segment 只有 **1 个 endpoint**，
`device_id >= 1` 一律落到 fallback；底层是 `aclrtIpcMemImportByKey` 107000 / `rtsIpcMemImportByKey:
driver error:invalid value` 导致 HCCS P2P 建链失败。TP8 与 TP16 表现一致 ⇒ **不是规模问题，是
"源 NPU 设备 → 目标 segment 端点"的注册/映射在该版本不成立**；要么 segment 需要按设备注册多个 endpoint，
要么 range put 应直接写本 client 的 host segment（而不是走 NPU D2D/HCCS）。
排查入口：`transfer_executor_base.cpp:64 / :689`、`client_service.cpp:4458`、`hixl_engine.cc:196/379`。

---

### 5.3 离线探针：把 range put 从 vLLM 里剥出来（已定位到「跨节点」）

脚本：`ansible/repros/mooncake_put_probe.py`（不启 vLLM，wiring 与
`MooncakeBackend._setup_store()` 完全一致，30 秒一次）。用法：

```bash
docker cp ansible/repros/mooncake_put_probe.py <ctr>:/root/pd/
docker exec -e MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json <ctr> \
  python3 -u /root/pd/mooncake_put_probe.py --device 0 --blocks 2 [--seg-gb 2] [--prefer-local|--prefer-same-node]
```

判定：`range_put` 的返回值是**写入字节数**，负数才是失败（vllm-ascend 用 `result < 0` 判失败）。

实测矩阵（2026-10-20，d0 空闲卡 dev0）：

| 场景 | 结果 |
|---|---|
| 对象在**本进程**的 segment（`--seg-gb 2 --prefer-local`） | ✅ `range_put=[116736,116736]` → `put_session_end=[0,0]` → `is_exist=[1,1]` |
| 对象在**同机其它进程**的 segment（`--seg-gb 2 --prefer-same-node`，不带 preferred_segment） | ✅ 同上 |
| 对象在**其它节点**的 segment（默认 `ReplicateConfig()`，落到 p0 的 segment） | ❌ `Transfer failed to: 172.168.4.237:<port>, errmsg: Invalid_Argument(EL0003)` → `rtsIpcMemImportByKey: driver error:invalid value` → `aclrtIpcMemImportByKey return[107000]` → `range_put=[-800,-800]`，且默认 env 下还会在 fallback/Retry 后 **Segfault** |
| `ASCEND_ENABLE_USE_FABRIC_MEM=1` 或 `ASCEND_GLOBAL_RESOURCE_CONFIG={"fabric_memory": true}` | ⚠️ 不再崩，但 `store.register_buffer` 返回 **-600**，range_put 仍 -800 ⇒ 该路径在当前 client 上不可用 |
| `ASCEND_AUTO_CONNECT=0` | ✅ 把 segfault 变成干净的 `-800`（排查/兜底用） |

**结论**：layerwise range/session 协议本身在本机是通的（能 commit、能 `is_exist`）；
失败点全部出现在 **跨节点 segment 的访问方式**上 —— 它用 `aclrtIpcMemImportByKey`
（同机跨进程的 IPC 映射）去访问**另一台机器**的 device VA，必然 `Invalid_Argument`；
叠加 `Dest buffer device_id=N >= endpoint count 1`（对端 segment 只暴露 1 个 endpoint）
导致先 fallback、再重试、最后段错误。**与 HCCS 物理链路无关**（用户侧 Ascend C demo 已证明链路正常）。

给厂商的三个具体问题：
1. 跨节点 segment 的传输为什么走 `aclrtIpcMemImportByKey`？是否应改走 RDMA/HCCS 网络通道，
   或先把数据落到本机再走 IPC？
2. `endpoint count` 为什么是 1？是否应按 device 扇出（每个 NPU 一个 endpoint），
   好让 `device_id>0` 的写入不必 fallback？
3. `ASCEND_ENABLE_USE_FABRIC_MEM=1`（A3 统一地址直传）下 `store.register_buffer` 为什么返回 -600？
   这条路径是否就是为跨节点直传准备的、需要 master/segment 也以 fabric 模式创建？

### 5.4 突破：`preferred_segment: true` 让 C1 通过（2026-10-20）

**先纠正 5.3 的一个判断**：单机单实例（TP16，全部进程在同一节点）**同样失败** ——
日志与 4 节点时一模一样（`Dest buffer device_id=1 >= endpoint count 1` → fallback →
`aclrtIpcMemImportByKey[107000]` → `halDeviceDisableP2P drvRetCode=17`）。所以问题不是"跨节点"，
而是 **跨进程/跨 device 访问对端 NPU 内存**。离线探针里 `--prefer-local` 能过、`--prefer-same-node`
能过，是因为对象恰好落在本进程自己的 segment 上；一旦落到别的 rank 进程的 segment 就必败。

**解法（本版本可用）**：在 `mooncake.json` 里打开
`"preferred_segment": true` —— vllm-ascend 的 `_build_replicate_config()` 会把
`preferred_segment` 填成 `self.local_seg`，于是**每个 rank 把对象钉在自己的 segment 上**，
写入变成"自写自有"，不再需要跨进程 IPC。

实测（单节点 TP16 co-located，`layerwise_smoke_single_node.sh`，202752，range_debug=1）：

| 指标 | 默认（preferred_segment=false） | 打开 preferred_segment |
|---|---|---|
| `[KVPOOL_RANGE_DEBUG]` save 事件 | 78 层，`results=[-800 ×16]` | 78 层，`results=[180224 ×16]`（正数=写入字节数） |
| `Range write failed` | 32 次 | **0** |
| `event:"commit"` | 0 | **1** |
| master `BatchPutEnd` / `key_count` / `allocated` | 0 / 0 / 0 | **1 / 16 / 195,035,136 B** |
| 第二次同 prompt 的 `hit check` | `0/16` | **`16/16`** |
| Segfault | 0（本轮） | 0 |

**当前结论**：
1. **C1 通过**（写入 + commit + master 有 key），机制本身可用；
2. 只验证到"**自己写、自己读**"（单实例 `kv_both`，同进程）；P/D 分离（跨进程）仍被堵：
   D 必须从 P 的 segment 读，而那正是跨进程访问对端 device 内存；
3. 所以 `preferred_segment: true` 是**验证/兜底**配置（同实例 kv offload 可用），
   不是 P/D 分离的解法；P/D 分离仍需厂商修 IPC/endpoint 或让 fabric 路径可用。

**待验证**：关掉本地前缀缓存（`ENABLE_PREFIX_CACHING=0`）后再发同一 prompt，
确认是**从池里 load**（日志出现 `"direction":"load"` 且 results 为正数），而不是本地 prefix cache 兜住。

### 5.5 协议层验证（roce / ubmem / fabric 三档，2026-10-20）

背景：默认（`compat`）日志是 `roce_mode=false, use_fabric_mem=false, GlobalResourceConfig unset`
—— 既不是官方"方案一 RoCE"，也不是"方案三 FabricMem"，走的是默认 HCCS 直连 + IPC import。
按官方 [HIXL 接口](https://gitcode.com/cann/hixl/blob/master/docs/zh/api/cpp/HIXL-interface.md)
的 `GlobalResourceConfig` 字段表，给 hyb 档加了 `hixl_preset`（compat/roce/ubmem/fabric 四选一），
一轮只改一个变量。

| preset | 注入的 env | 结果 |
|---|---|---|
| `compat` | 无 | 引擎正常；2K 冷请求 range put 全 `-800`（`endpoint count 1` → fallback → IPC import 107000 → 有时 segfault） |
| `roce` | `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config":{"protocol_desc":"roce:device"}}` | `roce_mode=true` 生效，引擎能起；但请求 500：`Endpoint CreateChannel failed` → `HixlClient Connect failed(172.168.4.237:20419)` → `IpcMemImportByKey`×26 → **Segfault**；master `key_count=15/allocated=106MB` 但无 commit |
| `ubmem` | `...{"protocol_desc":"ubmem"}` | 同上：`conn_fail=45` / `ipc=18` / `endpoint_fb=43` → **Segfault**；master `key_count=16/allocated=113MB`、无 commit |
| `fabric` | `ASCEND_ENABLE_USE_FABRIC_MEM=1` + `{"protocol_desc":"ubmem","fabric_memory":{"max_capacity":1,"task_stream_num":4}}` | 传输层**真的进了 fabric 模式**（`use_fabric_mem=true`、`Fabric mem mode is enabled`），但 `store.setup` 返回 **-600** → `Initialize mooncake failed`，引擎起不来 |

两个配置坑（都能从报错直接读出）：
- `fabric_memory.max_capacity` 单位是 **TB**，范围 **(0,1024]**（写 `"16GB"` 或 `17179869184` 都会
  `ParseGlobalResourceConfig` 失败，报错原文 `must be in (0, 1024] TB, got 17179869184`）；
- 设了 `ASCEND_GLOBAL_RESOURCE_CONFIG` 会同时把 vllm-ascend 的 store 切到"store 独立 TE"路径
  （缓冲区走 `store.register_buffer`）。

**结论**：
1. `protocol_desc` 只改"设备侧协议"，**没有改变目标 segment 的内存映射方式**（日志里 `endpoint count 1`
   与 `IpcMemImportByKey` 依旧），所以 RoCE/ubmem 两档都还是在跨进程建链处失败；
2. FabricMem 是最有希望的一条（传输层 adxl 已经支持），卡点在 **mooncake store client 的 `setup()` 返回 -600**；
3. RoCE/ubmem 两轮都在传输失败后踩到 `transfer_executor_base.cpp:699` 的 **segfault**（引擎被打死）——
   这条必须单独报给厂商，否则任何一次网络抖动都会崩引擎。

### 5.6 最小复现：ACL 跨进程 IPC 显存导入（60 行 C++）

源码/产物放在 `/mnt/sfs_turbo/g00832294/probe/`（`acl_ipc_demo.cc` + 编译好的 `acl_ipc_demo` + `README.md`），
仓库里也有同一份：`ansible/repros/acl_ipc_demo.cc`。

编译（容器内，CANN 9.1.0 自带 adxl/hixl 头与 `libllm_datadist.so`，只依赖 ACL，不碰 HIXL/mooncake/vLLM）：

```bash
g++ -std=c++17 -O2 acl_ipc_demo.cc \
    -I/usr/local/Ascend/cann-9.1.0/aarch64-linux/include \
    -L/usr/local/Ascend/cann-9.1.0/aarch64-linux/lib64 -lascendcl -o acl_ipc_demo
```

实测（2026-10-20，p0 容器内，同一设备、两个进程）：

| 变体 | export flags | import | 结果 |
|---|---|---|---|
| V1 | DEFAULT（PID 校验） | DEFAULT + `aclrtIpcMemSetImportPid(self)` | ❌ `507899`（连 SetImportPid 都失败） |
| V4 | DEFAULT | ENABLE_PEER_ACCESS | ❌ `507899` |
| V5 | DEFAULT | DEFAULT | ❌ `507899` |
| **V2** | **DISABLE_PID_VALIDATION** | ENABLE_PEER_ACCESS | ✅ `rc=0`，读回 `0xab` 校验通过 |
| **V3** | **DISABLE_PID_VALIDATION**（export dev0 → import dev1，**跨 device**） | ENABLE_PEER_ACCESS | ✅ `rc=0`，PASS |

错误码含义：`507899 = ACL_ERROR_RT_DRV_INTERNAL_ERROR`、`107000 = ACL_ERROR_RT_PARAM_INVALID`。

**结论（可直接给厂商）**：
1. 跨进程 / 跨 device 的 NPU 显存 IPC 导入在本机**完全可用**，前提是导出端用
   `ACL_RT_IPC_MEM_EXPORT_FLAG_DISABLE_PID_VALIDATION`；
2. **默认的 PID 校验路径本身是坏的**（`aclrtIpcMemSetImportPid` 直接 507899 driver internal error），
   任何走默认 flags 的跨进程导入都必然失败；
3. 引擎里 mooncake 报的是 **107000（参数非法）**而不是 507899，说明它传给 driver 的 key/参数
   与 export 方式不匹配 ⇒ 问题收窄为"**mooncake 用错了 flags / 没用 ACL 的 export-key 机制**"这一条。

### 5.7 根因定位到 hcomm 源码（开源）：`aclrtIpcMemGetExportKey` 的 flags 写死 0

`hcomm` 已 clone 到 `/mnt/sfs_turbo/g00832294/probe/hcomm-src`（gitcode `cann/hcomm`）。
跨进程导入的实现就在：

```cpp
// hcomm-src/src/legacy/ascend910/platform/common/adapter/adapter_rts.cc
// 导出（hrtIpcSetMemoryName，约 1257 行）：
//   flag 预留字段填 0
aclrtIpcMemGetExportKey(ptr, ptrMaxLen, name, nameMaxLen, 0UL);   // = ACL_RT_IPC_MEM_EXPORT_FLAG_DEFAULT
// 导入（hrtIpcOpenMemory，约 1331 行）：
aclrtIpcMemImportByKey(ptr, name, 0UL);                          // = ACL_RT_IPC_MEM_IMPORT_FLAG_DEFAULT
```

而 `ansible/repros/acl_ipc_demo.cc` 的实测（同一设备、两个进程）说明：**只要导出端用 DEFAULT flags，
跨进程导入必失败**：

| 变体 | export flags | 导出端做白名单 | import flags | 结果 |
|---|---|---|---|---|
| V1 | DEFAULT | 否（导入端 SetImportPid 自己） | DEFAULT | ❌ 507899 |
| V6 | DEFAULT | **是**（导出端 `aclrtIpcMemSetImportPid(peer)`，rc=**0**） | DEFAULT | ❌ 507899 |
| V7 | DEFAULT | 是（同上） | ENABLE_PEER_ACCESS | ❌ 507899 |
| **V8** | **DISABLE_PID_VALIDATION** | 否 | **DEFAULT** | ✅ `rc=0`，读回校验 PASS |

结论：
1. 分水岭**只在导出端的 flag**；导入端的 `ENABLE_PEER_ACCESS` 可有可无；
2. 白名单路径（`aclrtIpcMemSetImportPid`）在 HDK 26.0.rc1 上是坏的：导出端调用返回 0，
   导入端仍然 507899（DRV_INTERNAL_ERROR）；
3. hcomm 里那句注释 **"flag 预留字段填 0"** 说明这段代码写在 flag 还是“预留位”的年代；
   HDK 26.0 下 0 已经变成“开启 PID 校验”，而校验路径本身不可用 ⇒ **前向兼容 bug**，
   一行改成 `ACL_RT_IPC_MEM_EXPORT_FLAG_DISABLE_PID_VALIDATION` 即可（hixl 的 ubmem 路径
   `hixl/src/hixl/cs/ubmem/ubmem_allocator.cc:258` 正是这么写的）。

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
