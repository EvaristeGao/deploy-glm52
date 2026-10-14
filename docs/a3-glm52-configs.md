# A3 GLM-5.2 部署配置（`deploy_config: 200k | 1m`）

本文记录 A3 集群两档部署配置与官方参考的逐项对照、模板新增能力、验证方式与部署前检查清单。

- 官方依据：<https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/GLM5.2.html>
  - §5.2.1 `PD Disaggregation with PP2`（**198K**）：prefill **PP2 TP16**（2 节点合成 1 个 PP2 引擎）+ decode **DP8 TP4**（每节点 4 引擎）
  - §5.2.2 `PD Disaggregation 1M Deployment`（**1M**）：prefill **DP2 TP16**（每节点 1 引擎）+ decode **DP8 TP4**（每节点 4 引擎）
- 涉及文件：
  - `ansible/inventories/a3/group_vars/all.yml`（`deploy_config` 选择器 + `_200k` / `_1m` 两块）
  - `ansible/inventories/a3/inventory.yaml`（4 节点 2P+2D）
  - `ansible/templates/run_dp_prefill_template.sh.j2` / `run_dp_decode_template.sh.j2`
  - `ansible/tests/test_template_render.py` / `test_derive.py`
  - `ansible/simulate_modes.py`（`--inv a3 [--deploy-config 200k|1m]`）

---

## 1. 两档总览

两档**拓扑形状相同**（每节点都用满 16 卡：prefill 1 引擎 × TP16，decode 4 引擎 × TP4），差别在上下文长度与运行时参数。

| 项 | `200k`（当前启用） | `1m` |
|---|---|---|
| 官方出处 | §5.2.1（198K PD） | §5.2.2（1M PD） |
| KV 传输 | kvpool 单主：p0 裸 `mooncake_master`，`master_server_address=<p0_ip>:52050`，无 etcd/haproxy | 同 |
| `max_model_len` | **202752** | **1048576** |
| prefill dp / tp / local | 2 / 16 / 1 | 2 / 16 / 1 |
| decode dp / tp / local | 8 / 4 / 4 | 8 / 4 / 4 |
| prefill `max_num_batched_tokens` | **16384** | 8192（模板默认） |
| decode `max_num_batched_tokens` | **164** | 192 |
| decode MTP spec tokens | **3** | 5 |
| decode `HCCL_BUFFSIZE` | **256** | 400 |
| `--hf-overrides` | **两侧都有** `{"use_index_cache": true, "index_topk_freq": 4}` | 无 |
| CP / DCP | 无 | prefill CP=1、prefill 侧声明 decode CP=**16**、decode 侧 CP=**4**、interleave 128 |
| prefill `rpc_port` / `kv_port` | 16591 / 30000 | 16600 / 30000 |
| decode `rpc_port` / `kv_port` | 16600 / 30200 | 16600 / 30200 |
| prefill `gpu_memory_utilization` | 0.92 | 0.85（官方 0.9） |
| decode `gpu_memory_utilization` | 0.92 | 0.92 |
| `enable_sparse_sfa_c8` | **false**（官方 true） | **false**（官方 true） |

切换方式：改 `deploy_config` → 重跑 `gen`（重渲染节点脚本）→ `start` 生效。

---

## 2. 与官方参考的差异（逐条）

### 2.1 prefill 用 DP 代替 PP（**结构性差异，无法避免**）

官方 198K 档的 prefill 是 **PP2**：2 个节点合成 *一个* 引擎，靠
`--pipeline-parallel-size 2 --distributed-executor-backend mp --nnodes 2 --node-rank 0/1 --master-addr <p0> --master-port 7060`
（node1 加 `--headless`）+ `VLLM_PP_LAYER_PARTITION="41,37"` 组装，kv-transfer 里写
`prefill: {"dp_size": 1, "pp_size": 2, "tp_size": 16, "pp_layer_partition": "41,37"}`，proxy 只挂 1 个 prefiller 端点。

本项目的 `ansible/launch_online_dp.py` 是**纯 DP 启动器**（只有
`--dp-size/--tp-size/--dp-size-local/--dp-rank-start/--dp-address/--dp-rpc-port/--vllm-start-port`），
模板也按 `$1..$7` 位置参数一次拉起一个 DP rank，**表达不了 PP**。因此 200k 档 prefill 取
**DP2 TP16**（保留官方 TP16，横向铺成 2 个 DP 引擎、每节点 1 个），kv-transfer 写
`prefill: {"dp_size": 2, "tp_size": 16}`，proxy 挂 2 个 prefiller 端点。

若将来要真正对齐 PP2，需要新增一条 PP 启动路径（模板 PP 分支 + `start.yml` 每节点 1 进程 + proxy/实例清单改造）。

### 2.2 KV 传输：kvpool 单主（官方参考是直连）

官方 198K/1M 参考都用直连 `MooncakeConnectorV1` + `use_ascend_direct: true`（无 `AscendStoreConnector`）。
本项目 A3 走 **kvpool 单主**：`mooncake.enabled: true` + `mooncake.ha.enable_ha: false` ——
只在 proxy 节点（p0）起一个裸 `mooncake_master`（`--rpc-address <p0_ip> -rpc_port=52050`），
**不起 etcd、不起 haproxy、不需要证书目录**；`mooncake.json` 的 `master_server_address`
直连 `<p0_ip>:52050`。模板走 `MultiConnector`（内层 `MooncakeConnectorV1` + `AscendStoreConnector`），
因此渲染结果里 **没有 `engine_id`**（kvpool 形态不需要）。

> 选单主是为了测试阶段规避 etcd/ha 选主与 `etcd.certs_dir` 证书依赖；`etcd` / `haproxy` 配置块保留，
> 切回 HA（`enable_ha: true`）前必须先确认 `etcd.certs_dir` 在宿主机上真实存在
> （`ca.crt`/`server.crt`/`server.key`）。`etcd.certs_dir` 的 bind 在引擎容器、mooncake 容器、
> 独立 haproxy 容器上都按 `enable_ha` 门控，单主模式下整条链路都不会挂它。
>
> 官方 198K 档 decode 用**每 rank 独立 engine_id**（`$((100 + $4))`）来避免 KV 路由混乱；
> 本项目在 kvpool 模式下不渲染 engine_id，故未实现。若切到直连模式
> （`mooncake.enabled: false`），需要补上 per-rank engine_id 能力。

### 2.3 权重：w4a8c8（官方参考是 w8a8c8）

当前 `model_path: /mnt/sfs_turbo/models/GLM-5.2-w4a8c8`，官方 A3 的 198K/1M 参考权重都是 **w8a8c8**。
官方对 w4a8c8 的说明：

- `GLM-5.2-w4a8c8 (experimental) ... has known accuracy issues in Prefill-Decode (PD) disaggregation scenarios`
- w4a8c8 权重上 **DCP + `enable_sparse_sfa_c8` 同开有已知问题，不推荐**（这也是 1M 档必须关 sfa_c8 的原因）

### 2.4 `enable_sparse_sfa_c8` 两档都关

官方两档参考都是 `true`。本项目按 c3b1d4d「关闭 sfa_c8 开关，防止精度问题」的策略，
两档 prefill/decode 都取 **false**（`enable_sparse_li_c8` / `c8_enable_reshape_optim` 保持官方值）。
单测 `test_a3_sfa_c8_disabled_for_both_configs` 锁住该策略。

### 2.5 官方文档内部不一致处（按本项目口径处理）

- 198K 档的 decode 启动命令写 `--dp-rpc-port 12321 --vllm-start-port 8000`、proxy `--port 9000`，
  与 1M 档的 `16600 / 9900 / 8000` 互相矛盾，疑似文档残留；本项目统一用 `16600 / 9900`，proxy 用 `8000`。
- 198K 档 prefill 两个节点端口写 9081/9082，本项目按 `base_port + 本节点本地引擎序号` 生成
  （每节点 1 引擎 ⇒ 两侧都是 9081），与 A2 官方参考实现口径一致，proxy 端点同源生成不会错配。

---

## 3. 模板新增的可选能力

全部遵循「未配置则不渲染 / 回落集群默认值」的向后兼容约定，A2 渲染结果不变（有回归单测）。

| 配置键 | 作用 | 未配置时 |
|---|---|---|
| `pd_cluster.<role>.additional_config` | `--additional-config` 整串（分档差异最大的一项） | 回落按 `cluster_type` 的集群默认值 |
| `pd_cluster.<role>.hf_overrides` | `--hf-overrides` | 不渲染（1M / A2 都没有） |
| `pd_cluster.prefill.max_num_batched_tokens` | `--max-num-batched-tokens` | `8192` |
| `pd_cluster.decode.max_num_batched_tokens` | 同上（decode） | a3=`192` / a2=`256` |
| `pd_cluster.decode.num_speculative_tokens` | MTP 投机 token 数 | a3=`5` / a2=`3` |
| `pd_cluster.decode.hccl_buffsize` | `export HCCL_BUFFSIZE` | a3=`400` / a2=`2560` |
| `pd_cluster.prefill.decode_context_parallel_size` | prefill 节点声明的 decode CP | 回落 `pd_cluster.decode.context_parallel_size` |
| `pd_cluster.prefill.context_parallel_size` + `cp_kv_cache_interleave_size` | CP 三件套 | 不渲染 |

---

## 4. 验证

```bash
cd ansible && .venv/bin/python -m pytest tests/ -q        # 59 passed
.venv/bin/python simulate_modes.py                        # a2（4 模式，0 语法失败）
.venv/bin/python simulate_modes.py --inv a3               # a3 当前档
.venv/bin/python simulate_modes.py --inv a3 --deploy-config 1m
```

覆盖点：两档拓扑/端口/CP/additional-config/hf-overrides/batched-tokens/spec tokens/HCCL_BUFFSIZE、
`deploy_config` 选块行为、kvpool MultiConnector、A3 环境变量、渲染换行干净、
挂载与日志目录在容器内可见（`test_a3_mounts_and_logs_dir_are_container_visible`）、
以及 A2 全量回归。

模拟出的启动命令（200K 与 1M 同形）：

```
# prefill（p0 rank0 / p1 rank1）
python3 launch_online_dp.py --dp-size 2 --tp-size 16 --dp-size-local 1 \
  --dp-rank-start 0|1 --dp-address <p0_ip> --dp-rpc-port 16591|16600 --vllm-start-port 9081
# decode（d0 ranks 0-3 / d1 ranks 4-7）
python3 launch_online_dp.py --dp-size 8 --tp-size 4 --dp-size-local 4 \
  --dp-rank-start 0|4 --dp-address <d0_ip> --dp-rpc-port 16600 --vllm-start-port 9900
# proxy
python3 load_balance_proxy_server_example.py --host 0.0.0.0 --port 8000 \
  --prefiller-hosts <p0> <p1> --prefiller-ports 9081 9081 \
  --decoder-hosts <d0>×4 <d1>×4 --decoder-ports 9900 9901 9902 9903 9900 9901 9902 9903
```

未做：`gen`/`start` 真跑需 `/dev/shm`（Ansible 多进程队列），当前沙箱拒绝写入，
故用 `simulate_modes.py`（纯 Jinja2 + `bash -n`）等价验证渲染与命令语法。**A3 未实机部署验证。**

---

## 5. 部署前检查清单

| 级别 | 项 | 说明 |
|---|---|---|
| 🔴 | **`ansible_host` 可解析** | inventory 用的是主机名 `ott9-a31..34`，控制机若无 DNS 需改回 IP |
| 🟡 | **权重目录** | `model_path` 必须在 `model.mounts`（`/mnt/sfs_turbo:/mnt/sfs_turbo`）覆盖范围内，单测已加校验 |
| 🟡 | **镜像存在** | `image` = `quay.io/ascend/vllm-ascend:v0.30.0rc1-a3`；`mooncake.image` = `vllm-ascend-ha-gwh-test:v2.0`（需含 `mooncake_master`）。`check.yml` 会在 p0 上校验 mooncake 镜像 |
| 🟡 | **镜像版本 vs 文档** | 官方参考基于 v0.23.0；当前用 v0.30.0rc1，`additional_config` 的 flag 名/语义如有变化需以该版本为准 |
| 🟡 | **w4a8c8 + PD 精度** | 官方标注 w4a8c8 的 PD 分离为 experimental、有已知精度问题；建议尽快切 w8a8c8 |
| ⚪ | **单主模式的 KV 池是单点** | p0 的 `mooncake_master` 挂了则整个 KV 池不可用；测试够用，上生产需切回 HA（并先备好证书） |
| ⚪ | **切回 HA 前** | 确认 `etcd.certs_dir`（当前 `/ce-efs-hd2-01/g00832294/certs`）在宿主机真实存在且有 `ca.crt`/`server.crt`/`server.key`；单主模式下不挂它，不会报错 |
| ⚪ | **首次启动耗时** | 202752/1048576 上下文模型加载 + 图捕获较慢；`runtime.ready_timeout`（2400s）不够时上调 |
| ⚪ | **死配置** | `mooncake.port`、`pd_cluster.enable_prefix_caching` 目前没有任何 playbook/模板引用 |
