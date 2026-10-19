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

**结论（给开发）**：`Dest buffer device_id=N >= endpoint count 1` 说明目标 segment 只有 **1 个 endpoint**，
`device_id >= 1` 一律落到 fallback，随后 `Retry transfer` → segfault。也就是单机 **16 卡 TP16** 时
"源 NPU 设备 → 目标 segment 端点"的映射不成立；要么 segment 需要按设备注册多个 endpoint，要么
range put 应直接写本 client 的 host segment（而不是走 NPU D2D/HCCS）。
排查入口：`transfer_executor_base.cpp:64 / :689`、`client_service.cpp:4458`、`hixl_engine.cc:196/379`。

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
