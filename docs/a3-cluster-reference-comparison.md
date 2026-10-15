# A3 1M 档与集群现网参考配置对照

在 `ott9-a31`（p0）上 ssh 采集的集群现网 A3 1M 参考部署，与本项目 A3 `_1m` 档的逐项对照。

- 采集时间：2026-10-14 12:00（CST），采集方式：`ssh root@ott9-a31`
- 忽略项：`yy/GLM5.2-W8A8C8-dspark` 那套开了 **dspark**，本项目不做 dspark，本对照不含它（它也是 128K + 旧组节点，见 §4）

## 0. 当前 4 台节点真实 IP（已逐台核对，与本项目 inventory 一致）

| 节点 | 主机名 | inventory `ip` | 实际 `enp23s0f3` |
|---|---|---|---|
| p0 | ott9-a31 | 172.168.4.237 | 172.168.4.237 ✅ |
| p1 | ott9-a32 | 172.168.4.31 | 172.168.4.31 ✅ |
| d0 | ott9-a33 | 172.168.4.230 | 172.168.4.230 ✅ |
| d1 | ott9-a34 | 172.168.4.226 | 172.168.4.226 ✅ |

## 1. 三份 1M 参考

| 代号 | 路径 | 日期 | 节点 | 权重 | max-model-len | KV 传输 |
|---|---|---|---|---|---|---|
| **R1** | `yy/GLM5.2-1M` | 9/24 | 旧组 `.19/.156/.2/.212` | **w8a8c8** | 1048576 | kvpool |
| **R2** | `lh/deployScript/a3-w8a8c8-1MB-nokvpool` | 10/8 | **当前组** | 脚本里是 w4a8c8（目录名写 w8a8c8） | 1048576 | **直连** |
| **R3** | `lh/deployScript/a3-w4a8c8-1MB-nokvpool-didi-1` | 10/13 | **当前组** | w4a8c8 | 200000 | **直连** |

R1 最接近"官方 1M + kvpool"形态，R2/R3 是当前 4 台节点上的直连形态。三份的 prefill/decode 拓扑完全一致。

## 2. `_1m` 档逐项对照（本次重点）

「一致性」列 = 三份参考之间是否一致；✅ = 与本项目一致，❌ = 不一致。

| 项 | R1 (9/24) | R2 (10/8) | R3 (10/13) | 一致性 | 本项目 `_1m` |
|---|---|---|---|---|---|
| max-model-len | 1048576 | 1048576 | 200000 | R3 不同 | 1048576 ✅ |
| prefill dp/tp/local | 2/16/1 | 2/16/1 | 2/16/1 | 一致 | 2/16/1 ✅ |
| prefill `--prefill-context-parallel-size` | 1 | 1 | 1 | 一致 | 1 ✅ |
| prefill `--decode-context-parallel-size` | **16** | **16** | **16** | 一致 | 16 ✅ |
| `--cp-kv-cache-interleave-size` | 128 | 128 | 128 | 一致 | 128 ✅ |
| prefill port / dp-rpc-port | 9081 / 16600 | 9081 / 16600 | 9081 / 16600 | 一致 | 9081 / 16600 ✅ |
| prefill kv_port | 30000 | 30000 | 30000 | 一致 | 30000 ✅ |
| prefill MTP spec | 1 | 1 | 1 | 一致 | 1 ✅ |
| prefill `--max-num-batched-tokens` | 8192 | 8192 | 8192 | 一致 | 8192 ✅ |
| prefill `--max-num-seqs` | 64 | 64 | 64 | 一致 | 64 ✅ |
| prefill `--enforce-eager` | 有 | 有 | 有 | 一致 | 有 ✅ |
| **prefill gpu-mem** | 0.9 | 0.9 | 0.89 | ≈一致 | 0.9 ✅（已按参考改，原 0.85） |
| **prefill prefix-caching** | **`--no-enable-prefix-caching`** | `--enable-prefix-caching` | `--enable-prefix-caching` | R1 不同 | `--enable` ✅（对 R2/R3 与官方默认） |
| prefill `--async-scheduling` | 无 | 无 | **有** | R3 不同 | 无 ✅（多数一致） |
| decode dp/tp/local | 8/4/4 | 8/4/4 | 8/4/4 | 一致 | 8/4/4 ✅ |
| decode `--decode-context-parallel-size` | 4 | 4 | 4 | 一致 | 4 ✅ |
| decode port / dp-rpc-port | 9900 / 16600 | 9900 / 16600 | 9900 / 16600 | 一致 | 9900 / 16600 ✅ |
| decode kv_port | 30200 | 30200 | 30200 | 一致 | 30200 ✅ |
| decode MTP spec | **5** | **3** | **5** | R2 不同 | 5 ✅ |
| decode `--max-num-batched-tokens` | 192 | 192 | 192 | 一致 | 192 ✅ |
| decode `--max-num-seqs` | 32 | 32 | 32 | 一致 | 32 ✅ |
| **decode gpu-mem** | **0.92** | 0.9 | 0.89 | 不一致 | 0.92 ✅（对 R1） |
| decode `--compilation-config` | FULL_DECODE_ONLY | 同 | 同 | 一致 | 同 ✅ |
| decode `--async-scheduling` | 有 | 有 | 有 | 一致 | 有 ✅ |
| **decode prefix-caching** | 无（vLLM 默认关） | 无（默认关） | **`--no-enable-prefix-caching`** | 一致=**关** | `--no-enable` ✅（已按参考改） |
| **`enable_sparse_sfa_c8`** | **true** | **true** | **true** | 一致=**true** | **false**（有意不跟参考：精度优先，见 §3） |
| prefill `enable_dsa_cp` | true | true | true | 一致 | true ✅ |
| `enable_sparse_li_c8` | true | true | true | 一致 | true ✅ |
| prefill `c8_enable_reshape_optim` | true | true | true | 一致 | true ✅ |
| prefill `recompute_scheduler_enable` | false | false | false | 一致 | false ✅ |
| decode `recompute_scheduler_enable` | true | true | true | 一致 | true ✅ |
| prefill `HCCL_BUFFSIZE` | 400 | 400 | 400 | 一致 | 400 ✅ |
| decode `HCCL_BUFFSIZE` | 400 | 400 | 400 | 一致 | 400 ✅ |
| kv 传输 | MultiConnector（kvpool） | MooncakeConnectorV1（直连） | MooncakeConnectorV1（直连） | **不一致** | MultiConnector（kvpool） |
| kv-transfer `prefill/decode` 并行度 | dp2tp16 / dp8tp4 | 同 | 同 | 一致 | 同 ✅ |
| 直连 engine_id（R2/R3） | — | 0 / 2 | 0 / 1 | R3 不同 | 0 / 2（默认）✅ 对 R2 |
| `--hf-overrides` | 无 | 无 | 无 | 一致=**无** | 无 ✅（只有 `_200k` 有） |
| 权重 | w8a8c8 | 脚本 w4a8c8 | w4a8c8 | 不一致 | w4a8c8 |

### 环境变量

| 变量 | R1 | R2 | R3 | 本项目 A3 |
|---|---|---|---|---|
| `FUSED_MC2=1` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `HCCL_OP_EXPANSION_MODE=AIV` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `HCCL_IF_IP` / `GLOO/TP/HCCL_SOCKET_IFNAME` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `HCCL_TRANSFER_TIMEOUT=600` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `HCCL_EXEC_TIMEOUT` / `HCCL_CONNECT_TIMEOUT` | 3600 / 3600 | 3600 / 3600 | 3600 / 3600 | 3600 / 3600 ✅ |
| `OMP_PROC_BIND=false` / `OMP_NUM_THREADS=1` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `PYTORCH_NPU_ALLOC_CONF` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `HCCL_BUFFSIZE=400` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `ACL_OP_INIT_MODE=1` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `ASCEND_A3_ENABLE=1` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `LD_LIBRARY_PATH=…:/usr/local/lib` | 两侧 | 两侧 | 两侧 | 两侧 ✅ |
| `FLASHCOMM1=1` | prefill | prefill | prefill | prefill ✅ |
| `MLAPO=1` | decode | decode | decode | decode ✅ |
| `TASK_QUEUE_ENABLE=1` | decode | decode | decode | 两侧 ⚠️（本项目两侧都有） |
| `ASCEND_RT_VISIBLE_DEVICES=$1` | decode | decode | decode | 两侧 |
| **`ASCEND_AUTO_CONNECT=1`** | **两侧** | 无 | **两侧** | 无 ❌（2/3 参考有） |
| **`HCCL_INTRA_ROCE_ENABLE=1`** | **两侧** | 无 | 无 | 无（A3 分支不导出） |
| **`PYTHONHASHSEED=1024`** | **两侧** | 无 | 无 | `0` |
| `VLLM_LOG_LEVEL=DEBUG` | 两侧 | 无 | 无 | 无 |
| `VLLM_VERSION=0.23.0` | 无 | 无 | 两侧 | 无 |
| `ASCEND_CONNECT_TIMEOUT=60000` | 无 | 无 | 两侧 | 无 |
| `ASCEND_AGGREGATE_ENABLE=1` / `ASCEND_TRANSPORT_PRINT=1` | 无 | 无 | 两侧 | 无 |
| `VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT=480`（prefill）/ `VLLM_ASCEND_KV_LOAD_QUEUE_TIMEOUT=420`（decode）/ `VLLM_ASCEND_ENABLE_WAITING_ABORT_KV_CLEANUP=0` | 无 | 无 | 两侧 | 无 |
| `MOONCAKE_CONFIG_PATH` | 两侧 | 无（直连） | 无（直连） | 两侧（本项目 kvpool） |

### 额外启动参数

| 参数 | R1 | R2 | R3 | 本项目 |
|---|---|---|---|---|
| `--enable-prompt-tokens-details` | 无 | 无 | 两侧 | 无 |
| `--safetensors-load-strategy prefetch` | 无 | 无 | 无 | 无 ✅（A3 不渲染） |
| `--api-server-count` / `--headless` | 无 | 无 | 无 | 无 ✅ |

## 3. 结论与已应用的改动

`_1m` 原本已高度对齐，差异集中在 3 处，**已按参考改完**（本次提交）：

| # | 项 | 三份 1M 参考 | 改前 | 改后 |
|---|---|---|---|---|
| 1 | **decode 侧 prefix-caching** | 一致为**关**（R1/R2 不带 flag = 默认关；R3 显式 `--no-`） | 模板两侧都硬编码 `--enable-prefix-caching` | 模板改为按 role 取 `pd_cluster.<role>.enable_prefix_caching`（缺省 true → A2 渲染不变）；A3 两档 prefill=true / decode=false |
| 2 | prefill `gpu_memory_utilization` | 0.9 / 0.9 / 0.89 | `0.85`（来自 dspark/128K 参考） | **`0.9`**（对齐官方文档与 R1/R2） |
| 3 | ~~`enable_sparse_sfa_c8`（1M）~~ | 一致为 **true** | `false` | **保持 `false`（不改）** —— 精度优先，有意不跟参考；而且当前权重 w4a8c8 + 1M 开着 DCP，正是官方警告的组合 |

对应的单测：`test_a3_sfa_c8_disabled_for_both_configs`、`test_a3_prefix_caching_per_role`、
`test_prefix_caching_default_keeps_a2_unchanged`、`test_a3_1m_prefill_gpu_mem_matches_reference`。

> **第 3 项是有意不跟参考**：官方与三份集群 1M 参考的 `enable_sparse_sfa_c8` 都是 `true`，
> 本项目按 c3b1d4d「关闭 sfa_c8 防精度问题」保持 `false`（两档都关）。
> 关掉也正好规避官方警告的「**w4a8c8 权重 + DCP + sfa_c8 同开**」组合
> （官方原文：has known issues, not recommended）——当前权重是 w4a8c8，1M 档还开着 DCP。
> 若将来换 `/mnt/sfs_turbo/models/GLM-5.2-w8a8c8`（集群上已有，R1 用的就是它）
> 且确认精度 OK，再考虑开 `true`。

此外可考虑（参考之间不一致，属"增强"而非"纠错"，**本次未改**）：

- `ASCEND_AUTO_CONNECT=1`（R1/R3 有，R2 无）
- `HCCL_INTRA_ROCE_ENABLE=1`（只有 R1 有；本项目模板注释假定 A3 走灵衢 UB 不启用）
- `PYTHONHASHSEED=1024`（只有 R1 有；本项目 `0`）
- R3 的那组稳定/调试变量（`VLLM_VERSION`、`ASCEND_CONNECT_TIMEOUT`、`ASCEND_AGGREGATE_ENABLE`、`VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT`、`VLLM_ASCEND_KV_LOAD_QUEUE_TIMEOUT`、`--enable-prompt-tokens-details`）
- **权重换 w8a8c8**：`/mnt/sfs_turbo/models/GLM-5.2-w8a8c8` 在集群上确实存在，R1 用的就是它
- **KV 传输**：R1 是 kvpool，R2/R3 是直连——本项目现在是 kvpool（单主）

## 4. 记录：非 1M 的两份参考（本次不比对）

| 路径 | 日期 | 节点 | max-model-len | prefill | decode | 说明 |
|---|---|---|---|---|---|---|
| `yy/GLM5.2-W8A8C8-dspark` | 10/14 | 旧组 | 131072 | dp2tp16 | **dp4tp8** | 开了 dspark；`gpu-mem 0.85/0.90`、`batched 8192/384`、`additional_config` 多一个 `mega_moe_max_tokens: 8192`、有 `--api-server-count 1` |
| `lh/deployScript/proxy/start_proxy-1.sh` | 10/10 | 当前组 | — | 2×9081 | **4 引擎/节点** | 佐证当前组的 decode 是 **dp8tp4 local4**（不是 dspark 那份的 dp4tp8） |

> `GLM5.2-W8A8C8-dspark` 的 IP 是 172.168.4.19/156/2/212，**不是**当前 4 台；它的 `dp4tp8` 只适用于旧组。
> 当前 4 台上的参考（R2/R3 + `proxy/start_proxy*.sh` + `a3-w8a8c8-1MB-nokvpool/d0/start_d.sh`）统一是 `dp8tp4 local4`，
> 与本项目 `_200k`/`_1m` 一致。
