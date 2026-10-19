# A3 prefill KV offload（P/D remote-D2H）配置与待确认问题

对应官方文档（v0.30.0rc1）：

- [Layerwise and Sparse KV Cache Offloading Guide](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/layerwise_and_sparse_kv_cache_offloading.html)
  —— **§2 Layerwise KV Cache Offload on Prefill** + **§3 Sparse KV Cache Offload on Decode**（本档照这个来）
- [Layerwise and Sparse KV Cache Offloading Design](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/developer_guide/Design_Documents/layerwise_and_sparse_kv_cache_offloading.html)
- 相关但不同的另一条：[Mooncake Layerwise Hybrid Attention](https://docs.vllm.ai/projects/ascend/en/v0.30.0rc1/user_guide/feature_guide/mooncake_hybrid_attention.html)（= 本项目 `1m-v030-lw`，见 `a3-layerwise-kv-offload.md`）

本项目实现：`ansible/inventories/a3/group_vars/all.yml` 的 **`deploy_config: 1m-v030-poff`**（当前启用）。
本文记录：配置结构、与文档的**有意偏差**、**预期报错点**，以及**拿去问开发的清单**。

---

## 1. 这套东西在干什么

```
                ┌──────────── prefill (P) ────────────┐        ┌──────── decode (D) ────────┐
client → proxy → │ 每层 attention 算完，把该层 KV range │        │ SparseKVOffloadManager     │
                 │ ① PUT 到远端 store（AscendStore）   │        │ host DRAM KV 池（主 KV）   │
                 │ ② 同一批 buffer 注册给 MemFabric     │◀───────│ 按 top-k 选中的 block 回读 │
                 │    （SfaRemoteD2H，P 不 push）       │  pull  │ 到设备侧热 buffer          │
                 └─────────────────────────────────────┘        └────────────────────────────┘
```

- **P 侧两个 connector 同时挂**（文档 §2）：`AscendStoreConnector`（把层缓冲 offload 到远端 store，
  跨实例前缀复用）+ `SfaRemoteD2HConnector`（把同一批 buffer 暴露给 D，D 主动读）。
  **不再挂 `MooncakeConnectorV1`** —— 文档 §2 的 connector 列表里就没有它，P/D 交接由 D 直读完成。
- **D 侧只有 `SfaRemoteD2HConnector`**（文档 §3），KV 主存落 host DRAM 池
  （`sparse_kv_offload_config.enabled=true`），设备侧只留 top-k 热 buffer + indexer cache。
- 因此 `/v1/chat/completions` 是**先发给 D**：D 把 rendezvous 参数 POST 回 proxy 的
  `/v1/metaserver`，proxy 再拿这份参数去请求 P（这就是必须用 layerwise proxy 的原因）。

---

## 2. 本档渲染出来的东西

### P 侧（`ansible/templates/run_dp_prefill_template.sh.j2`）

```
--enforce-eager            # 文档 §2 硬要求：共享 buffer 的 layerwise prefill offload 必须 eager
--kv-transfer-config '{
  "kv_connector": "MultiConnector", "kv_role": "kv_producer", "kv_port": "20050",
  "kv_connector_extra_config": { "connectors": [
      { "kv_connector": "SfaRemoteD2HConnector", "kv_role": "kv_producer", "kv_port": "20050",
        "kv_connector_extra_config": { "transfer_backend": "memfabric",
                                       "memfabric_transfer_protocol": "sdma", "use_layerwise": true } },
      { "kv_connector": "AscendStoreConnector", "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "backend": "mooncake",              # ← 文档写的是 memcache，本档换成 mooncake（见 §4）
          "use_layerwise": true,
          "layerwise_prefetch_layers": 2, "layerwise_max_transfer_blocks": 64,
          "layerwise_max_transfer_bytes": 16777216,
          "layerwise_num_shared_buffers": 3,  # ← 文档 §2 的共享 buffer 旋钮
          "layerwise_independent_layers": [0] } }
  ] } }'
--additional-config '...（无 sparse_kv_offload_config）'   # 镜像 producer 侧 assert 禁止它
```

拓扑与数值：`max_model_len=1048576`、P/D 都 **DP2 TP16**（每节点 1 引擎，`dp_size_local=1`）、
`gpu_memory_utilization` 0.9 / 0.92、P 开 chunked-prefill（`max_num_batched_tokens=4096`）、
D 关 prefix caching、D `num_speculative_tokens=5`。P/D TP 相等 ⇒ 满足文档「P TP ≥ D TP 且能整除」。

### D 侧（`run_dp_decode_template.sh.j2`）

```
--kv-transfer-config '{
  "kv_connector": "SfaRemoteD2HConnector", "kv_role": "kv_consumer",
  "kv_port": "20050",                       # 文档：D 侧需预留 dp*tp 个连续端口（本档 2*16=32）
  "kv_connector_extra_config": { "transfer_backend": "memfabric",
                                 "memfabric_transfer_protocol": "sdma", "use_layerwise": true } }'
--attention_config.indexer_kv_dtype int8    # Sparse LI C8 允许
# 注意：没有 --kv-cache-dtype —— 稀疏 offload 要求主 KV 是 BF16（int8 会被 _validate_preconditions 拒）
--additional-config '{ ..., "sparse_kv_offload_config":
   {"enabled": true, "topk_buffer_size": 12288, "dram_size_per_dp_GB": 512} }'
```

- `topk_buffer_size`：设备侧热 buffer 的**每请求** token 预算。文档口径 = `(1+num_speculative_tokens)*2048`
  ⇒ MTP5 = 6×2048 = **12288**。rc1 代码默认是 **4096**（对 MTP5 偏小），所以显式给足。
- `dram_size_per_dp_GB`：host KV 池预算（每 DP rank 一份、TP ranks 共享）。文档默认 128 GiB 是按
  `max-model-len=131072` 的例子给的；本档 1M + 主 KV BF16，整份 KV 远不止 128 GiB，先给 **512 GiB**
  （节点内存约 2 TB、每节点 1 个 DP rank）。调参判据见 §5。

### proxy（`ansible/playbooks/start.yml`）

`proxy.layerwise`/`proxy.script`（group_vars）**自动跟随本档**：

- 脚本用镜像自带的 `load_balance_proxy_layerwise_server_example.py`
  （我们仓库里的 `load_balance_proxy_server_example.py` 是旧版，没有 `metaserver`/`do_remote_prefill` 逻辑）
- 绑定 `--host <p0_ip>`，**不能用 0.0.0.0**（脚本自身会 raise："the Wildcard Address ... is not allowed"）
- `stop.yml` 的 pkill 锚点也跟随脚本名

---

## 3. 镜像/环境前置（本档实际状态）

| 项 | 文档要求 | 本镜像/集群现状 |
|---|---|---|
| MemFabric Hybrid | A3 prefill/decode 都要装 `release/1.2` | ✅ pip 包 `memfabric_hybrid 1.2.0`（python3.12，`site-packages/memfabric_hybrid`，`__init__.py` 自己 ctypes 预加载 `lib/*.so`）——**不需要**文档里的 `/usr/local/.../set_env.sh`（本镜像没有该文件、也没有 python3.11.10） |
| Memcache Hybrid + MetaService | 文档 §1 要求（因为 §2 的 offload 后端是 memcache） | ✅ 包在（`memcache_hybrid 1.2.0`），**但本档换成 mooncake 后端，所以不起 MetaService、不写 `mmc-meta.conf`/`mmc-local.conf`** |
| mooncake client | 带 PR #2881 的 range/session 接口（layerwise 用） | ✅ 镜像 `glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13`（`mooncake-transfer-engine-npu 0.3.13.post1`） |
| mooncake_master | — | ✅ kvpool 单主（p0 裸 `mooncake_master`，`mooncake.enabled=true` + `ha.enable_ha=false`） |
| `ASCEND_HOME_PATH` | memfabric 的 aicpu kernel provision 需要 | ✅ 容器内 `/usr/local/Ascend/cann-9.1.0` |
| KV block size | 稀疏 offload 要 128 | ✅ Ascend 默认就是 128（`vllm_ascend/utils.py: refresh_block_size`） |
| 主机内存 | host KV 池 | ✅ 每节点约 2 TB；容器 `--shm-size 1024g`（先给 512 GiB 池） |

---

## 4. 与文档的偏差（都是**有意**的，也正是要问的点）

| # | 文档 | 本档 | 影响 |
|---|---|---|---|
| 1 | §2 offload 后端 = `memcache` | 换成 `mooncake` | **共享 buffer 复用会被静默忽略**（见下），HBM 不会因为 offload 下降 |
| 2 | 文档说 fused LIM + Copy-SFA 是「默认选中」 | 走 rc1 的默认 `fused_op_type="none"`（非 fused） | rc1 的 `SparseKVOffloadConfig.fused_op_type` 默认 `"none"`，全仓库没有任何地方把它设成 `"fused_copy_sfa"` ⇒ 文档那条默认在 rc1 里是「显式设置才生效」 |
| 3 | §1 的安装路径（`/usr/local/memfabric_hybrid/...`、python3.11.10、`set_env.sh`） | 不适用 | 本镜像 memfabric 是 pip 包 + python3.12，直接可用 |
| 4 | §2 prefill 的 `SfaRemoteD2HConnector` 子项没写 `kv_port` | 子项里显式写 `kv_port` | vLLM `MultiConnector` 构造子 config 是 `KVTransferConfig(**ktc)`（**不继承 root**），而 `SFAPDRD2H*Worker` 读的是自己那份 `kv_transfer_config.kv_port` ⇒ 不写就是 `None` |
| 5 | §3 示例 `max-model-len=131072`、MTP3 ⇒ host 池 128 GiB | 1M + MTP5 ⇒ `dram_size_per_dp_GB=512` + `topk_buffer_size=12288` | 见 §2 的说明；池子不够时容量会被 DRAM 卡住 |
| 6 | `use_fused_overlap` 默认 false「选 fused」 | 不设（rc1 语义不同） | rc1 里 `use_fused_overlap` 与 `fused_copy_sfa` 互斥，默认都不开 |

### 偏差 1 的代码依据（关键）

- `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py:76`
  `get_layerwise_reuse_config()`：遍历 `MultiConnector` 子项，找到 `AscendStoreConnector` 后按
  `extra_config["backend"]` 取 protocol，再调 `protocol.extract_layout_config(extra_config)`；
  **只有拿到非 None 才会建 reuse plan**（`apply_layerwise_kv_cache_plan` → `build_layerwise_reuse_layout`）。
- `.../backend/mooncake_layerwise.py:77`

  ```python
  def extract_layout_config(extra_config: dict[str, Any]) -> dict[str, Any] | None:
      """Block-key transfer does not opt into GVA-backed physical reuse."""
      del extra_config
      return None
  ```

  ⇒ `layerwise_num_shared_buffers` / `layerwise_independent_layers` 写了也没用（**不报错、也不省 HBM**），
  统计日志 `Layerwise KV cache reuse merged ...` 不会出现。
- 对照：`.../backend/memcache_backend.py:104` 的 `extract_layout_config()` 在 `use_layerwise` 时会返回
  extra_config ⇒ 文档 §2 的复用**只在 memcache 后端**成立。
- 文档 §5 最后一条只写了「Layerwise buffer reuse cannot currently be combined with
  `MooncakeLayerwiseConnector` ... 后续支持」——**rc1 代码里没有任何 fail-fast**。

---

## 5. 跑起来后看什么（预期报错点 / 观察点）

日志：`/mnt/sfs_turbo/g00832294/logs/{vllm_p0,vllm_p1,vllm_d0,vllm_d1,proxy}.log`（宿主机直接可读）。

| 阶段 | 关键日志 | 说明 |
|---|---|---|
| P/D 启动 | `MemFabric TransferEngine initialize: store_url=tcp://<本机ip>, ... role=Prefill|Decode, data_op_type=SDMA` | 出现即 MemFabric 初始化成功 |
| P | `Layerwise KV cache reuse merged %d descriptors into %d descriptors using %d buffer assignments.` | **预期不出现**（mooncake 后端不 opt-in 复用）——若真出现了，说明你们改了 `mooncake_layerwise.extract_layout_config` |
| P | `Mooncake client does not support batch_*` | 只在 mooncake client 太老时出现；本镜像 0.3.13.post1 已带 PR #2881 |
| D | `SparseKVOffloadManager starts CPU KV pool initialization: planned=%.2f GiB, configured_limit=512 GiB, num_blocks=%d` | `planned` 明显小于 512 GiB 说明 1M 容量被 DRAM 卡住 → 调大 `dram_size_per_dp_GB` |
| D | `Sparse KV offload: host DRAM budget allows only %d KV blocks while NPU memory could hold %d ...` | 同上，DRAM 成为瓶颈的 warning |
| D | `Sparse KV offload planned CPU pool exceeds DRAM limit after alignment: planned=... GiB, limit=...` | host 池装不下已计划的 KV：调大 `dram_size_per_dp_GB`（或降 `max_model_len`/`max_num_seqs`） |
| D | `ValueError: Sparse KV offload don't support compress now.` / `only support sparse attention model` / `don't support context parallel` / `don't support pipeline parallel` / `can only be used in D node` | `SparseKVOffloadConfig._validate_preconditions` 的前置检查（本档逐条已对齐，若报错就是别的地方动了） |
| D | `AssertionError: SfaRemoteD2HConnector consumer (D) must run with sparse_kv_offload_config.enabled=true.` | decode 的 additional-config 没生效 |
| P | `AssertionError: SfaRemoteD2HConnector producer (P) must run with sparse_kv_offload_config.enabled=false.` | prefill 误开了 sparse 配置 |
| proxy | `Initialized N prefill clients and M decode clients.` | proxy 起来了；随后 `curl http://<p0_ip>:8000/healthcheck` |
| proxy | `Decoder hosts will access Proxy host:port/metaserver, ensure that ... can access <host>:<port>/metaserver` | 正常提示（也说明为什么不能用 0.0.0.0） |

已知**结构性**观察点：P 侧的 `SfaRemoteD2HConnector` 是 pull 模式（P 把本地 KV tensor 注册给 MemFabric、
发 READ_READY，D 来读），因此 P 的 HBM 里那份 KV 仍然存在；`AscendStoreConnector` 的 offload 只是
**额外**往 mooncake 写一份（+ 命中时读回）。**“KV offloading 省 HBM” 在 mooncake 后端下不成立**，
这正是偏差 1 要确认的核心问题。

---

## 6. 拿去问开发的清单（建议照着贴日志）

1. **§2 的 prefill offload 能不能用 mooncake 后端？** rc1 里
   `mooncake_layerwise.extract_layout_config()` 无条件 `return None`
   （`kv_pool/ascend_store/backend/mooncake_layerwise.py:77`），
   于是 `layerwise_num_shared_buffers` / `layerwise_independent_layers` 被**静默忽略**；
   文档 §5 只说「暂不支持组合」。请问：(a) 这是预期行为还是要 fail-fast？
   (b) mooncake 后端下 prefill 的 HBM 本来就不会因 offload 下降，对吗？
   (c) 复用 + mooncake 有没有计划/分支？
2. **文档 §2 的 `SfaRemoteD2HConnector` 子项漏了 `kv_port`？** vLLM `MultiConnector` 子 config 是
   `KVTransferConfig(**ktc)`（不继承 root），而 `sfa_pd_rd2h/worker.py` 的
   `side_channel_port = kv_transfer_config.kv_port + dp_rank * tp_size` ⇒ 不显式给就是 `None`。
   还是说你们那边 MultiConnector 会继承 root（版本差异）？
3. **fused LIM + Copy-SFA 的默认值**：文档说默认选中，但 rc1 `SparseKVOffloadConfig.fused_op_type`
   默认 `"none"`、`topk_buffer_size` 默认 `4096`。文档对应的是哪个 tag/branch（PR #16640 之后？）？
   我们镜像的 `vllm_ascend_C.cpython-312-aarch64-linux-gnu.so` 里
   `npu_fused_scatter_copy_sparse_flash_attention` / `npu_fused_lightning_indexer_manage` /
   `npu_fused_quant_lightning_indexer_manage` 符号都在，是否只要写
   `"fused_op_type": "fused_copy_sfa"` 就是文档口径？MTP5 是否必须 `topk_buffer_size >= 12288`（256 对齐）？
4. **1M + BF16 主 KV 时 `dram_size_per_dp_GB` 的推荐值/公式**？文档只给了 `max-model-len=131072`、
   DP2×TP8 ⇒ 2×128 GiB 的例子。我们是 DP2×TP16 + `max_model_len=1048576` + 主 KV BF16（indexer int8），
   512 GiB 够不够、有没有 sizing 工具/日志可以对照？
5. **P 侧同时挂 store + D2H 的代价**：KV 既写 mooncake store（前缀复用）又注册给 MemFabric 让 D 直读，
   是否等于写两份、D 侧读的时候两条路径会互相抢带宽？§5 的「reuse 不能与 MooncakeLayerwiseConnector
   组合」是不是就是问题 1？如果 P 侧不用 store（只留 SfaRemoteD2H），是不是更接近文档的原意？
6. **稀疏 offload + MTP5 + graph mode（`FULL_DECODE_ONLY`）是否实机验证过**？文档只给了 MTP3、131072 的例子；
   我们 D 侧是 MTP5（`num_speculative_tokens=5`）+ graph mode + `--async-scheduling` 关闭。
   另外 rc1 的 `topk_buffer_size` 默认 4096 对 MTP5 是否偏小（非 fused 路径）？

---

## 7. 实机运行

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp        # 控制机 ~/.ansible/tmp 不可写

# 1) 重渲染节点脚本 + mooncake.json（改档后必须）
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml

# 2) 起 mooncake_master + 引擎 + layerwise proxy（全 4 节点）
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml

# 3) 看状态（引擎 /health + mooncake + proxy）
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/curl.yml

# 4) 端到端冒烟（走 proxy）
curl -s http://172.168.4.237:8000/healthcheck
curl -s http://172.168.4.237:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"glm-52","messages":[{"role":"user","content":"你好"}],"max_tokens":16}'
```

离线核对（不碰实机）：

```bash
cd ansible
.venv/bin/python simulate_modes.py --inv a3                    # 当前档位（1m-v030-poff）
.venv/bin/python simulate_modes.py --inv a3 --deploy-config 1m-v030   # 对照另一档
.venv/bin/python -m pytest tests/ -q                           # 83 passed
```

回退到已验证跑通的档：把 `deploy_config` 改回 `1m-v030`（或 `1m-v030-lw`）→ 重跑 `gen` → `start`。

---

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `ansible/inventories/a3/group_vars/all.yml` | `_1m_v030_poff` 档（P/D 拓扑、`prefill_kv_offload`/`decode_kv_offload`、decode 的 `sparse_kv_offload_config`、`proxy.layerwise`/`proxy.script`） |
| `ansible/templates/run_dp_prefill_template.sh.j2` | `prefill_kv_offload.sfa_remote_d2h` 分支（MultiConnector 两条子 connector + 共享 buffer 旋钮） |
| `ansible/templates/run_dp_decode_template.sh.j2` | `decode_kv_offload.sfa_remote_d2h` 分支（单 SfaRemoteD2H consumer，无 `--kv-cache-dtype`） |
| `ansible/playbooks/start.yml` / `stop.yml` | layerwise proxy 的取脚本/绑定地址/启动/停止 |
| `ansible/simulate_modes.py` | 新增 `bool`/`ternary`/`to_json`/`regex_replace` 近似 filter（playbook 的 when/vars 会用到） |
| `ansible/tests/test_template_render.py` | 新增 4 个 poff 档单测（connector 结构、BF16 主 KV、拓扑、不泄漏到其它档） |
