# v0.30.0rc1 官方文档校对（A3 GLM-5.2）

对照文档：<https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/tutorials/models/GLM5.2.html#52-1m-context-configuration>
（本地副本 `/tmp/glm52_v030.html` / `.txt`）

**触发原因**：本项目的 engine 镜像是 `quay.io/ascend/vllm-ascend:v0.30.0rc1-a3`，
而此前 A3 配置是按 **v0.23.0** 文档（`/tmp/glm52_en.txt`）+ 集群现网参考推的。
两代文档的配置形态差别很大，这次以 v0.30.0rc1 为准做一次校对。

章节结构也变了：

| v0.23.0 | v0.30.0rc1 |
|---|---|
| §5.2.1 PD Disaggregation with PP2（198K） | **§5.1.3.1 Deployment on 4 Atlas 800 A3** |
| §5.2.2 PD Disaggregation 1M Deployment | **§5.2.3 PD Disaggregation 1M Deployment** |

## 0. 结论（先说要点）

1. **v0.30 把 C8 稀疏优化整套删了**：`enable_sparse_sfa_c8` / `enable_sparse_li_c8` /
   `c8_enable_reshape_optim` 在 v0.30 文档里**一次都没出现**（v0.23 里分别出现 19/18/4 次）。
   → 我们自己关掉 sfa_c8 的方向是对的；但 **`enable_sparse_li_c8` / `c8_enable_reshape_optim`
   是 v0.23 遗留，v0.30 的 additional-config 里没有**。
2. **v0.30 删了 `--hf-overrides`**（v0.23 出现 4 次 → v0.30 0 次）。
   → 我们 `_200k` 档加的 `--hf-overrides '{"use_index_cache": true, "index_topk_freq": 4}'`
   是 v0.23 的东西，v0.30 应去掉。
3. **v0.30 新增了一批参数**：`--kv-cache-dtype int8`、`--attention_config.indexer_kv_dtype int8`、
   `--safetensors-load-strategy 'prefetch'`、`mega_moe_max_tokens`、`VLLM_USE_FASTOKENS=1`、
   `MF_GROUP_JOIN_MAX_TIMEOUT=1200`、`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000`。我们**全都没有**。
4. **开关从 env 搬进 additional-config**：v0.30 用 `enable_flashcomm1` / `enable_fused_mc2` /
   `enable_mlapo` 三个 additional-config 键；而 v0.23（和我们）用的是 env
   `VLLM_ASCEND_ENABLE_FLASHCOMM1` / `VLLM_ASCEND_ENABLE_FUSED_MC2` / `VLLM_ASCEND_ENABLE_MLAPO`。
   同时 v0.30 删掉了 env `ACL_OP_INIT_MODE` / `ASCEND_A3_ENABLE` / `TASK_QUEUE_ENABLE`
   （v0.23 分别 15/9/8 次 → v0.30 全为 0）。
5. **拓扑变了**：
   - 1M decode：v0.23 = DP8 TP4（每节点 4 引擎、DCP 4）→ **v0.30 = DP4 TP8（每节点 2 引擎、DCP 8）**
   - 198K decode：v0.23 = DP8 TP4 → **v0.30 = DP16 TP2（每节点 8 引擎）**
6. **gpu-mem 变了**：1M prefill v0.23 = 0.9 → **v0.30 = 0.85**（我上一轮刚从 0.85 改成 0.9，方向反了）；
   1M decode v0.23 = 0.92 → **v0.30 = 0.90**。
7. **API 形态变了**：v0.30 普遍用 `server_role_args` —— 主节点 `--api-server-count N`、
   其余节点 `--headless`；proxy 只列主节点（1M：prefiller 只 p0:9081，decoder 只 d0 的 9900/9901）。

## 1. v0.23 vs v0.30 文档级差异（关键字出现次数）

| 关键字 | v0.23.0 | v0.30.0rc1 |
|---|---|---|
| `enable_sparse_sfa_c8` | 19 | **0** |
| `enable_sparse_li_c8` | 18 | **0** |
| `c8_enable_reshape_optim` | 4 | **0** |
| `--hf-overrides` | 4 | **0** |
| `ACL_OP_INIT_MODE` | 15 | **0** |
| `ASCEND_A3_ENABLE` | 9 | **0** |
| `TASK_QUEUE_ENABLE` | 8 | **0** |
| `--kv-cache-dtype` | 0 | **15** |
| `--attention_config.indexer_kv_dtype` | 0 | **18** |
| `mega_moe_max_tokens` | 0 | **3** |
| `VLLM_USE_FASTOKENS` | 0 | **5** |
| `enable_flashcomm1`（additional-config 键） | 1 | **14** |
| `enable_fused_mc2`（additional-config 键） | 0 | **15** |
| `enable_mlapo`（additional-config 键） | 0 | **9** |
| `MF_GROUP_JOIN_MAX_TIMEOUT` | 0 | **5** |
| `--headless` / `--api-server-count` | 5 / 4 | 15 / 11 |

## 2. `_1m` vs v0.30 §5.2.3

v0.30 原文要点：4×A3（64GB × 16）、w8a8c8；prefill **DP2 TP16**（1 DP rank/节点）、
decode **DP4 TP8**（2 DP rank/节点）、decode CP 8。

| 项 | v0.30 §5.2.3 | 我们 `_1m` | 差异 |
|---|---|---|---|
| prefill dp/tp/local | 2/16/1 | 2/16/1 | ✅ |
| prefill CP / decode CP / interleave | 1 / **16** / 128 | 1 / 16 / 128 | ✅ |
| prefill dp-rpc-port / port | 16591 / 9081（p1 9082） | 16600 / 9081 | ⚠️ rpc 不同（v0.23 写的 16600） |
| **decode dp/tp/local** | **4/8/2** | **8/4/4** | ❌ |
| **decode DCP** | **8** | **4** | ❌ |
| decode dp-rpc-port / port | 16600 / 9900（9900 9901） | 16600 / 9900 | ✅ |
| kv_port | 30000 / 30200 | 同 | ✅ |
| max-model-len | 1048576 | 同 | ✅ |
| prefill batched / seqs | 8192 / 64 | 同 | ✅ |
| decode batched / seqs | 192 / 32 | 同 | ✅ |
| prefill spec / decode spec | 1 / 5 | 同 | ✅ |
| decode compilation / async | FULL_DECODE_ONLY / 有 | 同 | ✅ |
| prefill prefix-caching | 有 | 有 | ✅ |
| decode prefix-caching | 无 | `--no-enable-` | ✅（等价） |
| **prefill gpu-mem** | **0.85** | 0.9 | ❌（我上轮按 v0.23 改成 0.9，v0.30 是 0.85） |
| **decode gpu-mem** | **0.90** | 0.92 | ❌ |
| **`--kv-cache-dtype int8`** | 两侧都有 | 无 | ❌ |
| **`--attention_config.indexer_kv_dtype int8`** | 两侧都有 | 无 | ❌ |
| **`--safetensors-load-strategy 'prefetch'`** | 两侧都有 | 无（A3 分支不渲染） | ❌ |
| **additional-config prefill** | `{"fuse_muls_add":true, "multistream_overlap_shared_expert": true, "enable_dsa_cp":true, "mega_moe_max_tokens": 8192, "enable_flashcomm1": true, "enable_fused_mc2": 1}` | v0.23 形态（含 sfa_c8/li_c8/c8_reshape/recompute） | ❌ |
| **additional-config decode** | `{"fuse_muls_add":true, "recompute_scheduler_enable":true, "multistream_overlap_shared_expert":true, "enable_fused_mc2": 1, "enable_mlapo": true}` | v0.23 形态（含 sfa_c8/li_c8） | ❌ |
| **`--api-server-count` / `--headless`** | p0/d0 `--api-server-count 1`，p1/d1 `--headless` | 无 | ❌ |
| **`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000`** | 两侧 | 无（A3 未设 runtime.timeouts） | ❌ |
| **`MF_GROUP_JOIN_MAX_TIMEOUT=1200`** | 两侧 | 无 | ❌ |
| `HCCL_BUFFSIZE` | 400 / 400 | 400 / 400 | ✅ |
| env `FUSED_MC2` / `FLASHCOMM1` / `MLAPO` | **改由 additional-config 提供** | 仍用 env | ⚠️ 形态 |
| env `ACL_OP_INIT_MODE` / `ASCEND_A3_ENABLE` / `TASK_QUEUE_ENABLE` | v0.30 无 | 我们有 | ⚠️ v0.23 遗留 |
| proxy 端点 | prefiller 只 p0:9081；decoder 只 d0 的 9900/9901 | 2 prefill + 8 decode | ❌（跟随 headless 架构） |

## 3. `_200k` vs v0.30 §5.1.3.1

v0.30 原文要点：4×A3（64GB × 16）、w8a8c8；prefill **PP2 TP16**（`--pipeline-parallel-size 2`
+ `VLLM_PP_LAYER_PARTITION="41,37"`，2 节点合成 1 引擎）、decode **DP16 TP2**（8 DP rank/节点）；
`max-model-len 202752`。

| 项 | v0.30 §5.1.3.1 | 我们 `_200k` | 差异 |
|---|---|---|---|
| prefill | **PP2 TP16**（`--pipeline-parallel-size 2`、`VLLM_PP_LAYER_PARTITION="41,37"`） | DP2 TP16（纯 DP 近似） | ❌ 结构性（`launch_online_dp.py` 无 PP） |
| **decode dp/tp/local** | **16/2/8** | **8/4/4** | ❌ |
| max-model-len | 202752 | 202752 | ✅ |
| prefill batched / seqs | 16384 / 64 | 同 | ✅ |
| decode batched / seqs | 192 / 32 | 164 / 32 | ❌ batched（v0.23 是 164） |
| prefill spec / decode spec | 1 / 5 | 1 / 3 | ❌ decode spec（v0.23 是 3） |
| prefill prefix-caching | 有 | 有 | ✅ |
| decode prefix-caching | 无 | `--no-enable-` | ✅ |
| prefill gpu-mem / decode | 0.85 / 0.90 | 0.92 / 0.92 | ❌ |
| `--safetensors-load-strategy 'prefetch'` | 两侧 | 无 | ❌ |
| `--attention_config.indexer_kv_dtype int8` | 两侧 | 无 | ❌ |
| `--kv-cache-dtype int8` | 只有 decode | 无 | ❌ |
| **additional-config prefill** | `{"fuse_muls_add":true, "enable_dsa_cp":true, "enable_flashcomm1": true, "enable_fused_mc2": 1}` | v0.23 形态 + `enable_sparse_sfa_c8/li_c8/c8_enable_reshape_optim` | ❌ |
| **additional-config decode** | `{"fuse_muls_add":true, "recompute_scheduler_enable":true, "enable_fused_mc2": 1, "enable_mlapo": true}` | v0.23 形态 + sfa_c8/li_c8 | ❌ |
| **`--hf-overrides`** | **无** | 两侧都有 | ❌ v0.23 遗留 |
| `--api-server-count` / `--headless` | decode `--api-server-count 16` / d1 `--headless` | 无 | ❌ |
| env `VLLM_USE_FASTOKENS=1` | 两侧 | 无 | ❌ |
| `HCCL_BUFFSIZE` | 400（prefill）/ **256**（decode） | 400 / 256 | ✅ |
| env `ACL_OP_INIT_MODE` / `ASCEND_A3_ENABLE` | v0.30 无（decode 保留 `ASCEND_RT_VISIBLE_DEVICES=$1`） | 我们有 | ⚠️ v0.23 遗留 |
| kv-transfer 并行度 | prefill `{dp1, pp2, tp16, pp_layer_partition}` / decode `{dp16, tp2}` | `{dp2, tp16}` / `{dp8, tp4}` | ❌ |

## 4. 已实现：A3 `deploy_config: 1m-v030` 档（严格按 §5.2.3）

`ansible/inventories/a3/group_vars/all.yml` 新增 `_1m_v030` 块并设为当前启用档
（`_active` 改为 `{'200k':…, '1m':…, '1m-v030':…}[deploy_config]` 查表）。

### 4.1 与 §5.2.3 的对应

| §5.2.3 要求 | 本项目实现 |
|---|---|
| prefill DP2 TP16（1 rank/节点） | `dp_size 2 / tp_size 16 / dp_size_local 1` ✅ |
| decode DP4 TP8（2 rank/节点） | `dp_size 4 / tp_size 8 / dp_size_local 2` ✅ |
| prefill CP 1 / 声明 decode CP 16 / decode CP 8 / interleave 128 | `context_parallel_size` / `decode_context_parallel_size` / `cp_kv_cache_interleave_size` ✅ |
| max-model-len 1048576 | ✅ |
| prefill batched 8192、seqs 64、spec 1、gpu-mem 0.85、enforce-eager、prefix-caching | ✅ |
| decode batched 192、seqs 32、spec 5、gpu-mem 0.90、FULL_DECODE_ONLY、async-scheduling、无 prefix-caching | ✅ |
| `--safetensors-load-strategy 'prefetch'` 两侧 | 新键 `safetensors_prefetch: true` ✅ |
| `--kv-cache-dtype int8` / `--attention_config.indexer_kv_dtype int8` 两侧 | 新键 `kv_cache_dtype` / `indexer_kv_dtype` ✅ |
| additional-config（prefill 含 `mega_moe_max_tokens: 8192` / `enable_flashcomm1` / `enable_fused_mc2`；decode 含 `enable_fused_mc2` / `enable_mlapo`） | `additional_config` 逐字 ✅ |
| 各 role 首节点 `--api-server-count`，其余 `--headless` | 新键 `non_master_headless` + `api_server_count`，按 inventory 的 `idx` 渲染 ✅ |
| env：`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000` / `MF_GROUP_JOIN_MAX_TIMEOUT=1200` / `VLLM_USE_FASTOKENS=1` / `HCCL_BUFFSIZE=400` / `HCCL_OP_EXPANSION_MODE=AIV` / `HCCL_IF_IP` / `GLOO`+`TP`+`HCCL_SOCKET_IFNAME` / `PYTORCH_NPU_ALLOC_CONF` / `LD_LIBRARY_PATH`；**不再有** `ACL_OP_INIT_MODE`/`ASCEND_A3_ENABLE`/`TASK_QUEUE_ENABLE`/`VLLM_ASCEND_ENABLE_*`/`HCCL_*_TIMEOUT`/`OMP_*` | 新键 `v030_env: true` 切换 env 集（模板里门控）✅ |
| proxy：prefiller 只 p0:9081、decoder 只 d0 的 9900/9901 | start.yml 实例清单/proxy 端点按 `non_master_headless` 只取首节点 ✅（模拟出的命令与文档一致） |
| 无 `--hf-overrides` | v030 档不渲染 ✅ |

### 4.2 有意保留的偏离（需知悉）

| # | 偏离 | 说明 / 怎么改回 |
|---|---|---|
| 1 | **`mooncake.enabled: true`（kvpool 单主）** | §5.2.3 用的是**直连** `MooncakeConnectorV1`（无 `AscendStoreConnector`、不需要 mooncake master）。此处沿用你之前的单主 kvpool，渲染成 `MultiConnector`。要完全对齐文档：把 `mooncake.enabled` 改 `false`（一行），此时也不再需要 mooncake 容器与 master |
| 2 | **`--kv-cache-dtype int8` 会带上 SFA/LI** | 文档原文：这两项即「SFA/LI sparse attention optimizations and the reshape optimization of the C8 quantized model」——v0.30 **没有**单独关 SFA 的开关。若精度上要避开，删掉 `_1m_v030` 里两处 `kv_cache_dtype` / `indexer_kv_dtype`（配置会偏离文档） |
| 3 | **权重仍是 w4a8c8** | 文档 §5.2.3 用 w8a8c8；集群上 `/mnt/sfs_turbo/models/GLM-5.2-w8a8c8` 已存在，改 `model_path` 即可 |
| 4 | `--served-model-name glm-52` | 文档为 `glm-5`；只影响对外模型名，改 `model_name` 即可 |
| 5 | prefill p1 用 `--vllm-start-port 9081` | 文档写 9082；但 p1 是 `--headless`，不进 proxy 端点也不进 `wait_ready` 探测清单，端口无实际影响（本项目端口恒为 `base_port + 本节点本地引擎序号`） |
| 6 | 保留 `ASCEND_RT_VISIBLE_DEVICES=$1` 与 `TP_SOCKET_IFNAME` | 文档 1M 段没写这两个，但多引擎/多卡隔离需要（集群现网参考也都有）。不保留会导致同节点多个引擎抢同一批卡 |
| 7 | 保留 `PYTHONHASHSEED=0` | 文档未列，无害，保持既有行为 |

### 4.3 验证

- `pytest`：**72 passed**（新增 v030 档 8 个用例：拓扑/CP、新参数、headless、additional-config、
  env profile、gpu-mem/spec、无 hf-overrides、实例清单与 proxy 端点；含 v0.23 档与 A2 回归）
- `simulate_modes`：a3（v030）/a3 1m/a3 200k/a2 各 4 模式，**bash 语法失败 0 条**
- 模拟出的命令与 §5.2.3 逐项一致：
  - prefill `--dp-size 2 --tp-size 16 --dp-size-local 1 --dp-rank-start 0/1 --dp-rpc-port 16591 --vllm-start-port 9081`
  - decode `--dp-size 4 --tp-size 8 --dp-size-local 2 --dp-rank-start 0/2 --dp-rpc-port 16600 --vllm-start-port 9900`
  - proxy `--prefiller-hosts <p0> --prefiller-ports 9081 --decoder-hosts <d0> <d0> --decoder-ports 9900 9901`


按"与 v0.30 对齐"整理，分三档：

**A. 低风险、纯新增（建议直接做）**
1. `--safetensors-load-strategy 'prefetch'`（A3 两侧）
2. `--kv-cache-dtype int8` + `--attention_config.indexer_kv_dtype int8`（按 v0.30 各段的位置）
3. `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000`、`MF_GROUP_JOIN_MAX_TIMEOUT=1200`、`VLLM_USE_FASTOKENS=1`
4. `mega_moe_max_tokens: 8192` 进 prefill（1M）/ 不进 198K

**B. 形态切换（改动较大，需你拍板）**
5. additional-config 换成 v0.30 形态：**去掉** `enable_sparse_li_c8` / `c8_enable_reshape_optim` /
   `enable_sparse_sfa_c8`（v0.30 已无 C8 稀疏优化），**加上** `enable_flashcomm1` / `enable_fused_mc2` /
   `enable_mlapo`；同时 **prefill 的 `recompute_scheduler_enable` 也可以去掉**（v0.30 1M prefill 没有它）
6. **去掉 env** `VLLM_ASCEND_ENABLE_FLASHCOMM1` / `VLLM_ASCEND_ENABLE_FUSED_MC2` /
   `VLLM_ASCEND_ENABLE_MLAPO` / `ACL_OP_INIT_MODE` / `ASCEND_A3_ENABLE` / `TASK_QUEUE_ENABLE`
7. **去掉 `_200k` 的 `--hf-overrides`**

**C. 拓扑/数值变化（影响部署形状，必须你确认）**
8. **1M decode：DP8TP4/local4/DCP4 → DP4TP8/local2/DCP8**（v0.30 口径；注意集群现网 1M 参考是 DP8TP4，
   那是 v0.23 时代的镜像）
9. **200K decode：DP8TP4/local4 → DP16TP2/local8**
10. gpu-mem：1M = 0.85 / 0.90；200K = 0.85 / 0.90
11. decode spec：1M = 5；200K = **5**（v0.23 是 3）
12. decode batched：200K = **192**（v0.23 是 164）
13. `--headless` / `--api-server-count` 架构（含 proxy 端点与 `wait_ready` 实例清单要跟着改）
14. `max_model_len` / `decode_context_parallel_size` 的按档取值

> ⚠️ C 类会改变部署形状（引擎数、proxy 端点、KV 传输并行度），且与集群**现网正在跑的
> v0.23 时代配置**不一致。若只是想让"配置贴近 v0.30 文档"，先做 A + B；若要按 v0.30 真正
> 重排拓扑，需连同 proxy/实例清单一起改并实机验证。

## 5. 其余档位仍然保留（未改动）

- `1m` —— v0.23 口径的 1M（DP8TP4/DCP4 + kvpool + v0.23 env 集）
- `200k` —— v0.23 口径的 198K（prefill 用 DP2TP16 近似官方 PP2）

第 1~3 节列出的差异（v0.30 的 additional-config 形态、kv-cache-dtype、
`enable_flashcomm1/fused_mc2/mlapo`、headless 架构、decode 拓扑等）
只应用到了新的 `1m-v030` 档，`1m`/`200k` 维持原样以便对比与回退。
若确认 v0.30 口径可用，可以再决定是否把 `1m`/`200k` 也切换过去。
