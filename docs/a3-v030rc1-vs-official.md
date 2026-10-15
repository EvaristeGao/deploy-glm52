# A3 1M PD 配置 vs 官方 v0.30.0rc1 文档：差异汇总

对照的官方文档：
<https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/tutorials/models/GLM5.2.html#52-1m-context-configuration>
（§5.2.3 PD Disaggregation 1M Deployment）

本项目实现：`ansible/inventories/a3/group_vars/all.yml` 的 `deploy_config: 1m-v030`
（engine 镜像 `quay.io/ascend/vllm-ascend:v0.30.0rc1-a3`，与文档版本一致）。

---

## 0. 实机验证结果（2026-10-15，当前 4×A3 节点）

| 项 | 结果 |
|---|---|
| 容器 | p0：`mooncake-p0`（单主 KV 池）+ `vllm-ascend-gwh-glm52`；p1/d0/d1：`vllm-ascend-gwh-glm52` |
| 引擎 `/health` | **6/6 全 200**（p0:9081、p1:9081、d0:9900/9901、d1:9900/9901） |
| proxy healthcheck | `{"status":"ok","prefill_instances":2,"decode_instances":4}` |
| 端到端冒烟（proxy:8000 → P/D 分离链路） | HTTP 200，`system_fingerprint: vllm-0.30.0-tp8-dp4-ep-7e984aeb`（DP4 TP8 EP，与配置一致） |

---

## 1. 差异汇总

**A = 文档与镜像实际行为不符**（照抄文档会启动失败，已按镜像修正）；
**B = 本项目有意偏离**（架构/需求决定）；**C = 部署套件层面**（文档是裸脚本，本项目是 Ansible 套件）。

### A. 文档与镜像不符（照抄必挂）

| # | 文档 §5.2.3 写法 | 镜像实际行为 | 本项目做法 |
|---|---|---|---|
| A1 | `additional_config` **顶层** 写 `"fuse_muls_add": true` | `AscendConfig` 是 `extra="forbid"`，而 `fuse_muls_add` 只属于嵌套的 `AscendCompilationConfig` → 启动即 `pydantic ValidationError: Unexpected keyword argument: fuse_muls_add` | `"ascend_compilation_config": {"fuse_muls_add": true}` |
| A2 | `additional_config` **顶层** 写 `"recompute_scheduler_enable": true` | 该字段属于嵌套的 `SchedulerConfig`；顶层只是 deprecated 兼容路径（会打 warning） | decode 用 `"scheduler_config": {"recompute_scheduler_enable": true}`；prefill 直接省略（默认 false） |
| A3 | p1 / d1 加 `--headless`（文档："remain headless"） | `launch_online_dp.py` 传了 `--data-parallel-rank`，vLLM 判定为 **external LB 模式**，该模式要求 remote engine 自己起 API server：`RuntimeError: Remote engine N must not use --headless in external or hybrid dp lb mode` | **不加 `--headless`**，所有节点渲染 `--api-server-count 1` |
| A4 | prefill node 1 用 `--vllm-start-port 9082`；proxy 只列 p0 | 同上（p1 不是 headless，必须被 proxy 挂上） | p0/p1 都是 9081（不同主机，proxy 按 `host:port` 配对）；proxy 列 `p0 9081` + `p1 9081`；decode 列 `d0 d0 d1 d1` 的 `9900 9901` |

> 备注 1：`--api-server-count 1` 在 external LB 模式下**默认就是 1**，文档写它是显式 no-op；本项目保留以便与文档对照。
> 备注 2：`--headless` 与 `--api-server-count` **互斥**（vLLM 拒绝二者同时出现），模板里已改成互斥渲染。
> 备注 3：文档 §5.2.3 若原样照抄，会在 A1 处最先失败；修完 A1 会在 A3 处失败。两处都在本仓库修好了。

**证据位置**（v0.30.0rc1 镜像内）：
`vllm_ascend/ascend_config.py`（`AscendConfig.model_config = ConfigDict(extra="forbid")`、
`AscendCompilationConfig.fuse_muls_add`、`SchedulerConfig.recompute_scheduler_enable`、
`_NON_USER_INPUT_KEYS` 的 strip 名单）；
`vllm/entrypoints/cli/serve.py`（LB 模式判定 `is_external_lb = data_parallel_external_lb or data_parallel_rank is not None`
与 `api_server_count` 默认值）；
`vllm/v1/engine/utils.py::wait_for_engine_startup`（headless 校验）。

### B. 本项目有意偏离

| # | 官方文档 §5.2.3 | 本项目 | 原因 |
|---|---|---|---|
| B1 | KV 传输是**直连** `MooncakeConnectorV1`（无 `AscendStoreConnector`、不需要 mooncake master、带 `engine_id` 0/2） | **mooncake 池化**：`MultiConnector`（内层 `MooncakeConnectorV1` + `AscendStoreConnector`），p0 上单主 `mooncake_master`，`mooncake.json` 的 `master_server_address = <p0>:52050` | 需求明确「KV 要走 mooncake 池化」 |
| B2 | 权重 `GLM-5.2-w8a8c8` | `GLM-5.2-w4a8c8` | 沿用现网权重；集群上 w8a8c8 也有（`/mnt/sfs_turbo/models/GLM-5.2-w8a8c8`），换过去只改 `model_path` 一行 |
| B3 | `--served-model-name glm-5` | `glm-52` | 沿用现网对外模型名（只影响 API 返回里的 model 字段） |
| B4 | prefill p1 端口 **9082** | p0/p1 都是 **9081** | 本项目端口恒为 `base_port + 本节点本地引擎序号`（每节点 1 引擎 ⇒ 9081）；不同主机同端口不冲突，proxy 按 `host:port` 配对 |
| B5 | 1M 段 env 未列 `ASCEND_RT_VISIBLE_DEVICES` / `TP_SOCKET_IFNAME` | **保留** | 同节点多引擎多卡隔离必需（decode 每节点 2 个引擎）；集群现网参考也都有 |
| B6 | 未列 `PYTHONHASHSEED` | 保留 `PYTHONHASHSEED=0` | 无害，保持既有行为 |
| B7 | decode 不写 prefix caching 参数（默认关） | 显式写 `--no-enable-prefix-caching` | 语义等价；写出来是为了自解释（官方口径是按 prefix cache 命中率二选一） |
| B8 | 不涉及 single-master 端口 | 单主 master 端口 **52050**（`mooncake.ha.rpc_port`） | 集群现网参考用 50088（正好是本项目那个未被引用的 `mooncake.port`）；两者自洽即可，改端口要同时改配置与生成的 `mooncake.json` |

### C. 部署套件层面（文档是逐节点裸脚本）

| 项 | 官方文档 | 本项目 |
|---|---|---|
| 部署方式 | 手工准备 `run_dp_template.sh` / `launch_online_dp.py`，逐节点 `bash` 起服务 | Ansible：`gen`（渲染）→ `start`（容器/引擎/proxy/就绪探测）→ `status`/`curl`/`logs`/`stop` |
| 容器与挂载 | 各自维护 `docker-run*.sh` | `start.yml` 统一：`--net=host --privileged --shm-size=1024g`、`-v /mnt/sfs_turbo:/mnt/sfs_turbo`、日志落 `/mnt/sfs_turbo/g00832294/logs` |
| KV 池/master | 文档不涉及（直连） | kvpool 单主：p0 起 `mooncake-p0` 容器跑 `mooncake_master`（不起 etcd/haproxy） |
| 拓扑选择 | 一份文档一个场景 | `deploy_config` 三档：`1m-v030`（本档）/ `1m`（v0.23 口径）/ `200k`（v0.23 口径） |
| 启动器 | 两节点各起进程 / `launch_online_dp.py` | 统一 `launch_online_dp.py`（DP-only；所以 200k 档官方的 PP2 prefill 只能近似成 DP2 TP16） |

### D. 与文档一致的部分（对齐程度）

拓扑 **P DP2 TP16（每节点 1 引擎）/ D DP4 TP8（每节点 2 引擎）**；
prefill CP 1 / prefill 侧声明 decode CP **16** / decode CP **8** / interleave 128；
`max-model-len 1048576`；
prefill batched 8192 / seqs 64 / spec 1 / gpu-mem **0.85** / `--enforce-eager` / `--enable-prefix-caching`；
decode batched 192 / seqs 32 / spec 5 / gpu-mem **0.90** / `FULL_DECODE_ONLY` / `--async-scheduling`；
`--safetensors-load-strategy 'prefetch'`、`--kv-cache-dtype int8`、
`--attention_config.indexer_kv_dtype int8`、`mega_moe_max_tokens: 8192`；
additional-config 其余键（`enable_flashcomm1` / `enable_fused_mc2` / `enable_mlapo` /
`enable_dsa_cp` / `multistream_overlap_shared_expert`）；
v0.30 精简 env 集（`VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000` / `MF_GROUP_JOIN_MAX_TIMEOUT=1200` /
`VLLM_USE_FASTOKENS=1` / `HCCL_BUFFSIZE=400` / `HCCL_OP_EXPANSION_MODE=AIV` / `HCCL_IF_IP` /
`GLOO`+`TP`+`HCCL_SOCKET_IFNAME` / `PYTORCH_NPU_ALLOC_CONF` / `LD_LIBRARY_PATH`）；
kv 并行度 `prefill {dp2,tp16}` / `decode {dp4,tp8}`、kv_port 30000 / 30200、rpc 16591 / 16600。

> ⚠️ `--kv-cache-dtype int8` + `--attention_config.indexer_kv_dtype int8` 按文档原文
> **即「SFA/LI 稀疏注意力 + C8 reshape 优化」的开关**（v0.30 已无单独关 SFA 的参数）。
> 精度若出问题，最小回退是删掉 `_1m_v030` 里那两处 `kv_cache_dtype` / `indexer_kv_dtype`。

---

## 2. 模板里承载这些差异的可选键

| 配置键 | 作用 | 未配置时 |
|---|---|---|
| `pd_cluster.<role>.additional_config` | `--additional-config` 整串（分档差异最大） | 回落按 `cluster_type` 的集群默认值 |
| `pd_cluster.<role>.safetensors_prefetch` | `--safetensors-load-strategy prefetch`（A3 也能开） | A2=开 / A3=关 |
| `pd_cluster.<role>.kv_cache_dtype` | `--kv-cache-dtype` | 不渲染 |
| `pd_cluster.<role>.indexer_kv_dtype` | `--attention_config.indexer_kv_dtype` | 不渲染 |
| `pd_cluster.<role>.api_server_count` | `--api-server-count N` | 不渲染 |
| `pd_cluster.<role>.non_master_headless` | 非首节点 `--headless`（与 `api_server_count` 互斥） | `false`（**当前必须 false**，见 A3） |
| `pd_cluster.prefill.max_num_batched_tokens` | prefill `--max-num-batched-tokens` | `8192` |
| `pd_cluster.decode.max_num_batched_tokens` | decode 同上 | a3=`192` / a2=`256` |
| `pd_cluster.decode.num_speculative_tokens` | MTP 投机 token 数 | a3=`5` / a2=`3` |
| `pd_cluster.prefill.hccl_buffsize` / `pd_cluster.decode.hccl_buffsize` | `HCCL_BUFFSIZE` | a3=`400`（decode a2=`2560`） |
| `pd_cluster.<role>.enable_prefix_caching` | `--enable-` / `--no-enable-prefix-caching` | `true`（A2 两侧都开） |
| `pd_cluster.prefill.decode_context_parallel_size` | prefill 节点声明的 decode CP | 回落 `pd_cluster.decode.context_parallel_size` |
| `pd_cluster.v030_env` | 切到 v0.30 精简 env 集 | `false`（渲染 v0.23 env 集） |

---

## 3. 复现 / 回归

```bash
cd ansible
.venv/bin/python -m pytest tests/ -q                       # 75 passed
.venv/bin/python simulate_modes.py --inv a3                # 1m-v030 × 4 部署模式
.venv/bin/python simulate_modes.py --inv a3 --deploy-config 1m
.venv/bin/python simulate_modes.py --inv a3 --deploy-config 200k
ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/status.yml
ansible-playbook -i inventories/a3/inventory.yaml playbooks/curl.yml
```

实机已按上面顺序跑通（见 §0）。

---

## 4. 附录：v0.23.0 → v0.30.0rc1 的文档级差异

用于理解为什么本项目要维护两套口径（`1m`/`200k` = v0.23 口径，`1m-v030` = v0.30 口径）。
关键字出现次数（v0.23.0 → v0.30.0rc1）：

| 关键字 | v0.23.0 | v0.30.0rc1 |
|---|---|---|
| `enable_sparse_sfa_c8` / `enable_sparse_li_c8` / `c8_enable_reshape_optim` | 19 / 18 / 4 | **0 / 0 / 0**（改由 `--kv-cache-dtype int8` + `--attention_config.indexer_kv_dtype int8` 隐含） |
| `--hf-overrides` | 4 | **0** |
| `ACL_OP_INIT_MODE` / `ASCEND_A3_ENABLE` / `TASK_QUEUE_ENABLE` | 15 / 9 / 8 | **0 / 0 / 0** |
| `--kv-cache-dtype` / `--attention_config.indexer_kv_dtype` | 0 / 0 | **15 / 18** |
| `mega_moe_max_tokens` / `VLLM_USE_FASTOKENS` / `MF_GROUP_JOIN_MAX_TIMEOUT` | 0 / 0 / 0 | **3 / 5 / 5** |
| `enable_flashcomm1` / `enable_fused_mc2` / `enable_mlapo`（additional-config 键） | 1 / 0 / 0 | **14 / 15 / 9** |
| `--headless` / `--api-server-count` | 5 / 4 | 15 / 11 |

章节结构也变了：v0.23 的 §5.2.1 / §5.2.2 → v0.30 的 **§5.1.3.1（198K PD on 4×A3）** / **§5.2.3（1M PD）**。
v0.30 的 198K 档是 **PP2 TP16 prefill + DP16 TP2 decode**；1M 档是 **DP2 TP16 + DP4 TP8**。

> 相关文档：
> - `a3-glm52-configs.md` —— A3 三档配置总览与部署前检查清单
> - `a3-cluster-reference-comparison.md` —— 集群**现网**参考部署的对照（v0.23 时代口径）
