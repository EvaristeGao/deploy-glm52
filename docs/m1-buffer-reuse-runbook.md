# M1 Runbook：P 侧 KV Buffer Reuse（逐层 P/D + Mooncake）

> 状态：**已实现并验收**（2026-11-02）
> 适用配置：`deploy_config: 1m-v030-lw`（下称 M1 档）
> 相关设计：[mc-layerwise-detailed-design.md](mc-layerwise-detailed-design.md)

---

## 1. M1 是什么

P（prefill）侧的 Main KV 不再「每层一份 HBM」，而是用**少量共享槽位轮转**：
层 L 与层 L+N 复用同一块物理内存。写共享槽前必须等上一代读者（计算 / Store 上传 / P→D 传输）
全部结束——由上游 `MooncakeLayerwiseConnector` 的**每物理槽 send-done 事件**保证。

三条关键事实（也是它能work的原因）：

1. **屏障在上游已有**：`kv_p2p/mooncake_layerwise_connector.py` 的
   `wait_for_layer_reuse()` + `storage_send_done_events[]`，由 `AscendMultiConnector` 每层调用；
2. **P/D 交接必须用逐层连接器**：`MooncakeLayerwiseConnector`
   （`supports_layerwise_buffer_reuse = True`），块级 `MooncakeConnectorV1` 不提供该屏障；
3. **代理必须 D-first**：D 先把目标块 POST 到代理 `/v1/metaserver`，代理再把请求发给 P。
   P-first 的普通代理会让 P 永远等不到 D 的目标（表现为请求挂死）。

---

## 2. 交付物

| 项 | 值 |
|---|---|
| 镜像 | `glm52-vllm-ascend:0.30.0-dsabase-m1`（四节点已构建） |
| 基线 | `vllm_ascend` = `dsa_offload_rebase_30` @ **4234a1f**；其余沿用 rc1 基础镜像（mooncake 0.3.13.post1） |
| 镜像内补丁 | `b3222a7`：layerwise 下 Store Get 会话 `start_block = 0`（`image_build/m1/pool_worker.py`） |
| 运行时挂载 | **无**（补丁已在镜像里，`debug_mounts` 为空） |
| 引擎配置 | `pd_connector: layerwise`、`num_shared_buffers: 3`、`prefetch_layers: 1`、`independent_layers: [0, -1]` |
| 代理 | `load_balance_proxy_layerwise_server_example.py`（镜像自带 examples 目录） |

构建方式见 `image_build/Dockerfile_glm52_dsabase`（构建细节与踩坑见设计文档 §16.1）。

---

## 3. 部署

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml   # ~12 分钟
```

`group_vars/all.yml` 里 M1 档的要点：

```yaml
image: glm52-vllm-ascend:0.30.0-dsabase-m1
deploy_config: 1m-v030-lw

proxy:
  layerwise: true                                   # 必须：D-first 代理
  script: load_balance_proxy_layerwise_server_example.py

_1m_v030_lw:
  pd_cluster:
    pd_connector: layerwise                          # 必须：逐层连接器（提供槽位屏障）
    layerwise:
      enabled: true
      num_shared_buffers: 3                          # 共享槽位数（< 层数才真正复用）
      prefetch_layers: 1                             # 调度参数，不是 HBM 层数
      independent_layers: [0, -1]                    # 首尾层独立分配
```

---

## 4. 验收（每次改动后必跑）

```bash
# ① 四引擎 + proxy（在跳板机 ott9-a31 上执行）
ssh root@ott9-a31 'for h in 172.168.4.237 172.168.4.31; do printf "P:%s " "$(curl -s -o /dev/null -w %{http_code} http://$h:9081/health)"; done; \
 for h in 172.168.4.230 172.168.4.226; do printf "D:%s " "$(curl -s -o /dev/null -w %{http_code} http://$h:9900/health)"; done; echo; \
 curl -s http://172.168.4.237:8000/healthcheck'

# ② 复用确实生效（生成脚本里应有共享槽数）
grep -o '"layerwise_num_shared_buffers": [0-9]*' ansible/generated/run_dp_template_p0.sh

# ③ 请求（单 chunk / 双 chunk / 8K）——双 chunk 是关键回归
ssh root@ott9-a31 '/root/bin/rtest 1 60 16; /root/bin/rtest 1 526 24; /root/bin/rtest 1 1000 24'
```

判据：

| 用例 | 期望 | 实测 |
|---|---|---|
| 单 chunk（≈509 token） | 输出正确 | ✅ 2.1s |
| **双 chunk（≈4237 token）** | 输出正确 | ✅ 4.4s |
| 8K（≈8029 token） | 输出正确 | ✅ 5.1s |
| 4 并发 × ≈4981 | 4/4 正确 | ✅ 4.9–7.3s |

> ⚠️ **不要用"逐 token 与对照组完全一致"当判据**：多 chunk prefill 本身有数值非确定性，
> 不共享的对照组逐字也会变（实测分歧点更早）。正确判据是「语义正确 + 与对照组行为同构」。

---

## 5. 故障速查

| 现象 | 原因 | 处置 |
|---|---|---|
| 引擎 200 但请求**永远不返回**，P/D 日志只有 `/tokenize` | 用了 P-first 的普通代理 | 确认 `proxy.layerwise: true` + layerwise 脚本；D 必须能访问 `proxy_ip:8000/v1/metaserver` |
| 启动报 `Mooncake layerwise connector does not support packed SFA main KV caches yet.` | 用了 rc1 旧镜像（不支持 c8） | 换 `...-dsabase-m1` 镜像（dsa-base 已支持 c8） |
| 启动报 `KeyError: 'remote_cached_tokens'` | 代理没带该参数（plain 代理路径） | 用 layerwise 代理（它自带），或给 plain 代理补 `remote_cached_tokens: 0` |
| 输出乱码且 `num_shared_buffers` 很小 | 屏障未接上（连接器不是 layerwise，或不是 D-first 代理） | 查 §5 前两行；确认 `pd_connector: layerwise` |
| 引擎起来但容器很快就没了 | 见 §6 的 `hixl_preset` / `max_model_len` 两条硬约束（同干净档） | 同干净档处置 |
| 首次请求 60–90s | 新镜像的图编译冷启动 | 正常，后续请求恢复 |

---

## 6. 与干净档共享的硬约束（必须保留）

1. **`max_model_len: 262144`**：不挂任何 KV 预算放大补丁时，TP16/1M 需要 54.14 GiB > 可用 37.4 GiB；
2. **`hixl_preset: roce`** + 对应 `extra_env`（`HCCL_INTRA_ROCE_ENABLE=1` 等）：缺了会走 hccs P2P，
   同机不同 device 建链失败 → 首个请求 segfault。

---

## 7. 已知边界（不属于 M1）

| 项 | 说明 |
|---|---|
| D 侧复用 | 不支持；D 侧 KV 仍是独立分配 |
| D 侧 Host offload（Main→Host / Indexer→HBM） | 代码存在（`SparseKVOffloadConfig` + `MooncakeHostPool`），但只挂在 **blockwise `dsa_pd_offload`** 路径上，layerwise 连接器未接 → 属 M2 |
| 异构 P/D TP、混合 attention/recurrent、Mamba/KDA、量化传输、speculative | 设计明确不支持，需启动时拒绝（部分校验尚未加） |
| 慢路径矩阵（Store 慢 / P-D 慢交替） | 未做（M3） |

---

## 8. 关键代码位置

| 位置 | 内容 |
|---|---|
| `kv_p2p/mooncake_layerwise_connector.py:1254-1257` | 槽位状态（`layer_storage_slots` / 事件 / 错误表） |
| `…:1300-1315` | `_init_layerwise_reuse_state()`：按**实际地址**去重得到物理槽 |
| `…:1317-1334` | `_mark_layerwise_reuse_pending` / `_complete_layerwise_reuse` |
| `…:1336-1350` | `wait_for_layer_reuse()`：逐槽等事件 + 失败传播 |
| `…:2032-2047` | 逐层发送：pending 登记必须**先于** Store 发布 save-done |
| `kv_transfer/ascend_multi_connector.py:71-99` | provider 选择 + waiter 注入 |
| `kv_pool/ascend_store/layerwise_cache_layout.py:163-215` | 共享槽/独立层/`prefetch_layer_map` |
| `image_build/m1/pool_worker.py` | 镜像内的 `b3222a7` 补丁（start_block=0） |
| `examples/disaggregated_prefill_v1/load_balance_proxy_layerwise_server_example.py` | D-first 代理 + `/v1/metaserver` |

---

## 9. ⚠️ 容量现状：语义已通，但**物理复用目前未生效**（重要）

这一节纠正一个容易误判的结论：**"复用下多 chunk 正确"不能证明复用真的在起作用**。

### 9.1 实测数据

| 指标 | 当前（`num_shared_buffers: 3`） | 若复用真正生效（5 个槽：3 共享 + 2 独立） |
|---|---|---|
| KV 内存 | 36.41 GiB | 同 |
| `num_blocks` | **5509** | 应约为 **12×**（≈ 6.7 万） |
| 每 block 字节 | **6.77 MB** | 应约 **0.56 MB** |
| 61 层 × 116736 B | 6.79 MB ← 与实测一致 | — |

也就是说：**61 层仍然各自分配了 KV，没有发生任何跨层别名**。

### 9.2 根因（代码层）

```python
# backend/mooncake_layerwise.py:77
def extract_layout_config(extra_config):
    """Block-key transfer does not opt into GVA-backed physical reuse."""
    del extra_config
    return None            # ← mooncake 后端永远返回 None
```

而 KV 预算缩放依赖它：

```python
# worker/worker.py:639-657
extra_config = get_layerwise_reuse_config(self.vllm_config.kv_transfer_config)   # → None
if extra_config is not None:
    layout = build_layerwise_cache_layout(...)
    factor = num_layers / len(layout.storage_indices)   # 不会执行
    self.available_kv_cache_memory_bytes *= factor
```

日志里 `"Layerwise KV cache reuse maps ... scale logical KV budget by ..."` **命中数为 0**，与上面一致。

下游连带影响：

- `layerwise_cache_layout` 仍被 Store worker 使用 → `layerwise_offload=True`（load/restore 语义生效，这就是 `b3222a7` 补丁有意义的原因）；
- 但逐层连接器的槽位映射来自**实际注册地址**（`_init_layerwise_reuse_state`），地址没别名 ⇒
  每层仍是 1 个独立 slot ⇒ `wait_for_layer_reuse()` 无槽可等，屏障形同虚设。

### 9.3 结论与下一步（P 侧真正的工作量）

- **已经拿到的**：Store 侧 load/restore 语义正确（`b3222a7`）、D-first 协议、镜像与部署链路、
  逐层连接器的屏障代码可用；
- **还没拿到的**：容量收益（这正是 buffer reuse 的意义）；
- **要做的（P 侧）**：
  1. 为 block-key 实现 `extract_layout_config()` 的 opt-in 返回（按 `backend` + 显式开关键判断）；
  2. 让 runner 按**槽**而不是按层分配 KV tensor（真正的别名），否则只放大预算会导致内存爆炸；
  3. 再验证：`num_blocks` 应放大到 ≈ `num_layers / 槽数`；同时 `wait_for_layer_reuse` 出现真实等待
     （日志 `Waiting for Mooncake transfer to release physical KV slot %d` 应当能被观测到）。

设计文档 §2 的缺口表里本来就写着这一条：
"`extract_layout_config()` 当前返回 None；接入布局并修正复用时加载范围"——本次把它定位为**容量收益的关键路径**。

---

## 10. 复用接入（2026-11-02 第 2 轮）：容量已通，P→D 传输待修

### 10.1 已实现（分支 `m1-prefill-reuse`，commit `5423808b0` + `fix: Any`）

1. `backend/mooncake_layerwise.extract_layout_config()`：按显式配置 opt-in（此前恒 `None`，导致整条复用路径失效）；
2. `models/glm5next/cache_config.py`：`main_slot_count` 由复用计划（`storage_indices`）决定，
   tensor 的 `shared_by` 改为「该槽容纳的全部层」⇒ 真正的物理别名；
3. `worker/worker.py`：槽位感知模型（glm5_next）不再做通用预算缩放（避免 double count），
   并删掉按层数估的 fallback factor。

### 10.2 实测：容量收益确认

| 指标 | 接入前 | 接入后 |
|---|---|---|
| `num_blocks` | 5509 | **68882（12.5×）** |
| 缩放日志 | 0 次 | **16 次**（每 rank 一次，`79 layers onto 5 buffer assignments`） |
| 4 引擎健康 | 200 | 200 |

### 10.3 阻塞：物理别名下的 P→D 传输失败

```
[HixlClient] aclrtSynchronizeStreamWithTimeout failed, kernel=HixlBatchPut, list_num=1, slot=16
Stream synchronize timeout → BatchTransferSync failed
D 侧: Number of completed KV cache recv requests: 1  （随后停滞）
```

根因假设（待验证，按优先级）：

1. **重叠区间**：别名后同一块 HBM 被多个层的传输列表引用，HIXL 单批内出现重叠 range → 传输超时；
2. **D 侧按层记账**：D 期待 61 个层的对象/区间，而 P 只有 5 个物理槽 → 计数在第 1 个后不再推进；
3. **factor 偏差**：日志按 `79 layers`（含 draft/MTP）计算，而别名只覆盖 main 的 61 层 → 预算略偏大（68882 vs 理论 ~55k）。

已把环境回退到可用版本（`glm52-vllm-ascend:0.30.0-dsabase-m1`，无挂载），
4 引擎 200、单/双 chunk 请求正常，代码保留在分支上。

### 10.4 下一步

1. 传输计划按**物理槽**去重（同一槽的多层只传一次，或在 D 侧还原成物理槽视图）；
2. factor 改用 main 层数（61）而不是总层数（79）；
3. 传输修好后：验证屏障日志（`Waiting for ... release physical KV slot`）出现、多 chunk 正确；
4. 再固化进镜像（`docker save` → SFS → 其余节点 `docker load -i`）。

---

## 11. 复用接入第 3 轮（2026-11-02 晚）：别名已生效，屏障仍未接上

### 11.1 本轮修的三个 bug

| # | 现象 | 修复 |
|---|---|---|
| 1 | 启动即 `NameError: Any` | `cache_config.py` 补 `from typing import Any` |
| 2 | 缩放完全没发生（`num_blocks` 仍是 5509） | `kv_role` 是枚举，字符比较永远不等 → 改成按名字比较（`kv_role_name`）；`model_type` 常量写错（实际是 `glm_moe_dsa`）也一并修正 |
| 3 | 只应在 P 侧复用 | `_glm5_next_reuse_layout()` 增加 `kv_role == kv_producer` 门控 |

另外把 `bytes_per_block` 也改成按槽计算（`layout.main_slot_count`），否则块数不会随槽数放大。

### 11.2 实测结果

- **别名确实生效了**：双 chunk 输出 `NONThe NONNON` / `**YouUserNON``` ……` —— 典型的"物理共享但没有屏障"的乱码；
- **容量没涨**：`num_blocks` 仍 5508 → 因为
  ```python
  main_slot_count = max(len(main_slots), *(len(g.layer_names) for g in mamba_groups))
  ```
  mamba 分组仍按 61 层计，把槽数顶回 61；
- 单 chunk 正常（1.8–2.0s），多 chunk 必坏。

### 11.3 结论：剩下两件必须做的事

1. **槽数**：`main_slot_count` 不能被 mamba 分组顶回层数（本模型 mamba 组应一并按复用计划折叠，或明确不参与）；
2. **屏障**（核心）：混合链路里 `AscendMultiConnector._wait_for_external_slot_release()` 只在
   `supports_layerwise_buffer_reuse` 的连接器上调用 `wait_for_layer_reuse()`；
   当前 P 的逐层发送与 Store 上传没有被该槽位事件串起来 → 别名一开就乱码。
   这正是设计文档 §4/§6 里"每层三件写入（计算、Store、P/D）都必须计入屏障"的落点。

### 11.4 现场

已回退到已知可用镜像 `glm52-vllm-ascend:0.30.0-dsabase-m1`（无挂载、无别名），
4 引擎 200、单 chunk 2.0s、双 chunk 4.2s 输出正确。
复用接入的代码全部在 `src/vllm-ascend` 分支 `m1-prefill-reuse`（4 个 commit），
镜像 `...-m1c` 已含前三个修复，待第 1/2 项修好后重建分发。
