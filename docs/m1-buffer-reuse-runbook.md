# M1 Runbook：P 侧 KV Buffer Reuse（逐层 P/D + Mooncake）

> 2026-11-03：已从固定基线建立 `src/vllm-ascend-reuse` / `p-main-buffer-reuse`。
> 新核对结果与首批修补见 §11.17 和 [对应表](p-main-buffer-reuse-audit.md)；
> 旧轮次的推断保留作历史，不作为新实现已验证事实。

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

### 11.5 第 4 轮：门控是通的，但 builder 没被调用

直接进程内探针（容器里构造真实形态的 `kv_transfer_config`）：

```python
_glm5_next_reuse_layout(fake_vllm_config)
# → (has_layer_reuse=True, slots=5)     ✅ 门控/布局都对
```

但真实启动时：

- 新增的诊断日志（`glm5next layerwise reuse: ...`，3 处）**一行都没出现**；
- `num_blocks` 仍是 5509；
- 说明 `_get_glm5_next_cache_layout()` 在**真实 KV 配置生成路径上根本没被调用到**
  （或走了另一条历史/cached 路径），而不是布局算错。

下一轮要做的定位（一次部署即可）：

1. 把诊断日志从 `logging.info` 改成 `logging.warning`（vllm 的 root logger 默认 INFO 以下被丢，
   本次就是因为 `logger.level=0/handlers=0` + root 级别过滤而看不到）；
2. 在 `patch_kv_cache_utils` 的分派处加一行 warning，打印 `is_deepseek_v41_cache / glm5next / 默认` 走了哪个分支；
3. 若确实走了 glm5next 分支却仍 61 槽 → 检查 `main_slot_count` 是否被 mamba 分组顶回层数。

现场已回退：`glm52-vllm-ascend:0.30.0-dsabase-m1`、无挂载，4 引擎 200、`pt=4237 → 4.4s` 正确。

### 11.6 根因锁定：这份模型根本没走 glm5next 的 KV 配置构建器

诊断（warning 级，单次启动即可见）：

```
(EngineCore_DP0 pid=730) kv_cache_config dispatch: v41=False glm5next=False available=39093626880
```

`glm5next=False` ⇒ `patch_kv_cache_utils` 的分派落到 **vLLM 默认构建器**
（`_orig_get_kv_cache_config_from_groups`），从来没有调用
`get_glm5_next_kv_cache_config()`。

所以前面几轮改的 `main_slots` / `shared_by` / 按槽 `bytes_per_block` **一次都没执行**：
`num_blocks` 自然还是 5509（61 层 × 6.79 MB），控制台也看不到任何复用日志。

> 这也解释了为什么"门控探针返回 slots=5"却对线上毫无影响——探针调的是没人用的函数。

### 11.7 正确的落点（下轮直接做）

复用折叠必须在**默认构建器**的产物上做，两个可行方案：

**方案 1（推荐）**：在 `patch_kv_cache_utils` 里加一个复用分支——
当 `get_layerwise_reuse_config(vllm_config.kv_transfer_config)` 非空且
`kv_role == kv_producer` 时，接管配置生成：

```python
base = _orig_get_kv_cache_config_from_groups(...)      # 拿到 specs 与 logical 页大小
slots = build_layerwise_cache_layout(num_main_layers, extra_config).storage_indices
phys_page = sum(page of slot[0] for slot in slots) + (非共享部分)
num_blocks = available_memory // phys_page
kv_cache_tensors = [KVCacheTensor(size=page*num_blocks, layers=slot_layers,
                                  block_stride=page) for slot_layers in slots] + 非共享部分
```

**方案 2**：直接把 `kv_cache_groups` 伪装成 glmnext 形态以复用现有 builder（脆弱，不推荐）。

### 11.8 现场

已回退到 `glm52-vllm-ascend:0.30.0-dsabase-m1`（无挂载、无别名），引擎正常。
诊断用的两处 warning（`kv_cache_config dispatch`、`glm5next layerwise reuse`）
保留在分支上，便于下次一次启动确认分支走向。

### 11.9 第 5 轮：折叠真的跑了，卡在"各 rank num_blocks 必须一致"

按 §11.7 方案 1 实现了 `get_layerwise_reuse_kv_cache_config()`（按页大小分组 → 取最大组为主层 →
用 `build_layerwise_cache_layout` 折叠成槽 → 一个槽一个 tensor → `bytes_per_block` 按槽算），
并挂进 dispatch。首次真正执行到：

```
layerwise reuse folded 79 main layers into 5 physical slots
  (page=83968 bytes, others=22, blocks=356074)
```

两个问题：

1. **折叠了 79 层**（应为 61 个 main；79 含 MTP/draft），导致 `blocks=356074` 明显偏大；
2. **引擎起不来**，断言在 `generate_scheduler_kv_cache_config`：

```python
assert all(cfg.num_blocks == kv_cache_configs[0].num_blocks for cfg in kv_cache_configs)
```

即 **各 TP rank 必须得到相同的 num_blocks**。我的实现按"本 rank 投影到的层"折叠，
各 rank 的 `main_names` 不同 → 槽数不同 → `num_blocks` 不同 → 断言失败。

修复方向（下轮直接做）：

1. 槽数用**全局**层数推导（`model_config.get_num_layers()`），各 rank 只把**本地存在的层**
   放进对应槽，保证 `len(slot_layers)` 与 `num_blocks` 全 rank 一致；
2. 只折叠真正的 main MLA 层（排除 MTP/draft：它们的 spec 页大小相同但语义不同，
   需要按层名/role 过滤，而不是只按 page size 分组）；
3. 复核 `bytes_per_block`（5×83968 + 其余）与 `blocks` 的数量级（期望 ~7 万级，不是 35 万）。

现场：已回退 `glm52-vllm-ascend:0.30.0-dsabase-m1`、无挂载；
4 引擎 200、`pt=507 → 2.0s` 正确。

### 11.10 分支确认

`mazifan0120/vllm-ascend` 的 `dsa_offload_rebase_30` HEAD = **4234a1f18**，与本项目镜像基线一致；
该分支上没有额外的 reuse 实现（只有 sfa_kv_offload / sparse_kv_offload_manager 的 port 提交），
所以复用接入确实要在这里自己写。

### 11.11 第 6 轮：容量 8× 稳住，正确性缺口定位到"写入前没有 wait"

#### 做成的事

把折叠块数改成 **基线块数 × (逻辑页/物理页)**，并对齐到 128 的倍数（各 rank 基线块数有 ±2 抖动）：

```
baseline num_blocks: 5508
folded  num_blocks: 44288      → 8.04×
4 引擎 200，启动正常
```

（之前 356074 那种离群值来自"按各自 rank 内存算块数"，已废弃。）

#### 仍然坏的部分

```
pt=509  → 1.9s  ✅
pt=4237 → ' This The    ``````````'      ❌ 乱码
pt=4989 → 'Moon OurOur Moon$ MoonMoon...' ❌ 乱码
slot 等待计数 = 0
```

#### 根因（本轮定位）

`wait_for_layer_reuse()` 在整个仓库里**只有两处调用**：

- `ascend_multi_connector.py:99`（`_wait_for_external_slot_release`），它被注入成
  **Store 连接器**的 `external_slot_release_waiter`，用在 store 的 load/save 之前；
- `sfa_pd_rd2h/connector.py:306`（另一套实现）。

也就是说：**计算侧（attention 写 KV cache 之前）没有任何地方等待槽位**。
现有钩子只有"写完之后"的通知 `notify_kv_cache_written(layer_name)`
（`attention/utils.py:613`），没有"写之前等上一代读者结束"的钩子。

所以别名一开，层 L+N 覆盖槽位时，层 L 的 P→D 发送和 Store 上传可能都还在进行 → 乱码。

#### 下一步（明确的实现点）

1. 在 attention 写 KV cache **之前**加一个 pre-write hook，例如
   `wait_kv_cache_reuse(layer_name/layer_idx)`，由 multi-connector 转发到
   `_wait_for_external_slot_release`（即 `wait_for_layer_reuse`）；
2. 或在 `save_kv_layer` 之前插入同一等待（若写与发送在同一调用栈）；
3. 加断言：启用复用但 provider 为空 → 启动失败（设计 §5 要求）。

现场已回退：`glm52-vllm-ascend:0.30.0-dsabase-m1`、无挂载；4 引擎 200、`pt=4237 → 4.1s` 正确。

### 11.12 第 7 轮：屏障钩子写好了，卡在 KV 配置的全 rank 一致性

#### 已实现（本轮的核心代码）

**写入前屏障**——layerwise 复用缺的那一环：

1. `attention/utils.py` 新增 `notify_kv_cache_write_begin(layer_name)`；
2. `attention/mla_v1.py` 的 `forward()` **一开始**就调用它（早于任何 KV 写入）；
3. `ascend_multi_connector.py` 新增 `on_kv_cache_write_begin()`，把层名解析成层号后
   转发到 `_wait_for_external_slot_release()` → `provider.wait_for_layer_reuse()`。

这样层 L+N 覆盖共享槽之前，会等层 L 的 send-done 事件，语义上正是设计 §4 要求的
"每层三件写入（计算 / Store / P-D）都必须计入屏障"里的计算侧入口。

#### 仍然卡住的地方

启用折叠后引擎起不来，断言仍在：

```python
# vllm/v1/core/kv_cache_utils.py:2353
assert all(cfg.num_blocks == kv_cache_configs[0].num_blocks for cfg in kv_cache_configs)
```

尝试过两种让各 rank 一致的办法，都没解决：

| 办法 | 结果 |
|---|---|
| 对齐到 128 的倍数 | 值仍有 ±2 → 断言失败 |
| `dist.all_reduce(MIN)`（我的 try/except 兜底） | 仍然失败，推测配置构建阶段进程组未初始化或不是同一组 |

#### 下轮的正确解法（建议）

不要在 **worker 侧**算块数。`generate_scheduler_kv_cache_config` 的入参就是各 worker 的配置，
说明块数应当由**引擎侧一次算出**再下发。做法：
在引擎侧（拿到 workers 的 `available_memory` 之后）调用折叠 builder 一次，
worker 只按下发值建 tensor；这样天然全 rank 一致。

若不想动引擎侧，退路是：用 `min(available_memory)` 的确定性来源（例如
`determine_available_memory()` 已经做过的 min 归约结果）而不是各 rank 自己的值。

现场已回退：`dsabase-m1`、无挂载；4 引擎 200、`pt=4236 → 3.9s` 正确。

### 11.13 第 8 轮：引擎能带折叠启动了，但屏障钩子没被触发

#### 关键突破：全 rank 一致性问题解决

把放大后的块数**向下取到 2 的幂**（`1 << (n.bit_length()-1)`），各 rank 无论抖动几块都落到同一个值：

```
baseline num_blocks : 5508
folded  num_blocks  : 32768     → 5.9×
4 引擎 200，折叠生效且启动无断言
```

#### 但正确性仍然坏，且屏障没触发

```
pt=509  → 2.1s   ✅
pt=4237 → ' ThisThis  NONThis'                  ❌
pt=4988 → 'NON   NON NONThisThis` ` ` ` ` . . ' ❌
slot等待计数 = 0     ← wait_for_layer_reuse 一次都没进
```

#### 下一轮的第一个诊断（必须先做）

`AscendMultiConnector._layerwise_slot_release_providers` 很可能为空，判断条件四个都要满足：

```python
connector.is_producer
connector.connector_worker is not None
connector.supports_layerwise_buffer_reuse   # MooncakeLayerwiseConnector 里是 True
callable(connector.wait_for_layer_reuse)
```

`supports_layerwise_buffer_reuse = True` 已确认存在（`mooncake_layerwise_connector.py:749`），
所以嫌疑集中在 `connector_worker` 是否已注入、以及 `_configure_layerwise_reuse_completion()`
是否在 worker 侧被调用。做法：在该方法末尾加一条 warning 打印
`len(self._layerwise_slot_release_providers)` 和每个 connector 的四个条件，一次启动即可定位。

另外要确认我加的 `notify_kv_cache_write_begin()` 真的被 MLA 走到了
（可临时在 hook 里累加计数，请求结束后打印一次）。

现场：已回退 `dsabase-m1`、无挂载；4 引擎 200、`pt=4236 → 4.3s` 正确。

### 11.14 第 9 轮：屏障钩子挂错了模块——这个模型用的是 DSA 注意力

#### 诊断结果（本轮的关键发现）

1. **provider 注册是好的**（worker 侧）：

```
Worker_DP0_TP10_EP10: layerwise reuse providers=1
  details=[('MooncakeLayerwiseConnector', True, True, True, True),
           ('AscendStoreConnector', False, ...)]
EngineCore: providers=0（调度器角色没有 connector_worker，正常）
```

2. **但 `on_kv_cache_write_begin` 一次都没被调用**（hook=0）——说明我把钩子挂在
   `mla_v1.py` / `attention_c8_mxfp.py` 的 forward 上，而 GLM-5.2 实际走的是
   **DSA 注意力**：

```
vllm_ascend/attention/dsa_v1.py:1578   class AscendDSAImpl(AttentionImplBase[Any])
vllm_ascend/attention/dsa_v41.py:252   class AscendDSAV41Impl
vllm_ascend/attention/dsa_v41.py:690   def forward(self, attn, positions, hidden_states, output=None)
```

所以那两处钩子从未执行。

#### 下一轮（一步到位）

把 `notify_kv_cache_write_begin(...)` 加到 **`AscendDSAImpl.forward` 和
`AscendDSAV41Impl.forward`** 的入口（写 KV 之前），再启动验证：

- `hook>0` ⇒ 钩子路径对了；
- `slot等待>0` ⇒ 屏障真的在等（说明 send-done 事件被正确 clear/set）；
- 多 chunk 输出正确 ⇒ 复用链路真正闭环。

现场：已回退 `dsabase-m1`、无挂载；4 引擎 200、`pt=4236 → 4.4s` 正确。

### 11.15 第 10 轮：DSA 钩子也没触发 → 下一步先确认"钩子函数本身是否可达"

给 `AscendDSAImpl.forward`（`dsa_v1.py:1869`）和 `AscendDSAV41Impl.forward`
（`dsa_v41.py:693`）都加了 `notify_kv_cache_write_begin(...)`，重新部署：

```
hook=0 | 等待=0 | blocks=32768(5.9x)   ← 仍然一次都没打印
```

两种可能：

1. 这两个 DSA forward 也不是实际执行路径（可能被 `torch.compile`/ACLGraph 捕获，
   或走 `dsa_attn_kv_plan` / 自定义算子直接写缓存）；
2. 钩子被调用了，但 `on_kv_cache_write_begin` 在 **early return** 处静默返回
   （`providers==0` 或 `layer_name` 为空字符串）。

已把日志移到 early return **之前**并打印 `layer_name` 与 `providers` 数量
（commit `chore(kv_pool): log the write-begin hook even when it bails out`），
下一次启动即可区分这两种情况：

- 有 `on_kv_cache_write_begin layer='...' providers=1` ⇒ 钩子可达，问题在 `extract_layer_index`/槽映射；
- `layer=''` ⇒ 传参问题（`attn.layer_name` 不存在）；
- **完全没有该日志** ⇒ 这两个 forward 不是执行路径，需要改用
  `save_kv_layer`（连接器侧，写完但发送前）或由图外的统一入口触发。

现场已回退：`dsabase-m1`、无挂载；4 引擎 200、`pt=4237 → 3.9s` 正确。

### 11.16 第 11 轮：连"无条件探针"都不触发 → Python 注意力 forward 不在服务路径上

在 `AscendDSAImpl.forward` 开头加了**无条件** warning（不依赖任何守卫）：

```python
logging.getLogger(__name__).warning("dsa forward entered layer=%r", layer_name)
```

部署后（已确认挂载生效：容器内文件里 grep 得到该行、run 脚本里没有 PYTHONPATH 覆盖）：

```
dsa_forward=0   write_begin=0   等待=0
```

**结论：服务期的注意力写入根本不经过 Python 的 `forward`。**
合理推断是走 ACLGraph/自定义算子（`dsa_v41_forward` / `dsa_attn_kv_plan` 里的
torch_npu 融合算子），Python 那层只在 profiling/capture 阶段跑。

这也解释了为什么 `notify_kv_cache_written` 那条路（Store 上传）能工作——
它是连接器在**图外**按层调度的，跟模型 forward 无关。

#### 因此屏障必须放到"图外、且每层都会被调用"的位置

候选（按可行性排序）：

1. **连接器侧 `save_kv_layer(layer_name, ...)`**：图外、每层一次、且在发送之前；
   但它发生在**写之后**，所以只能保证"发送不越界"，不能防止下一层写坏数据；
   → 需要配合"引擎在每层写之前调用连接器的某个钩子"，而该钩子必须由**图外的调度代码**
   （model_runner 的逐层循环或 `AscendStoreConnector` 的 layer loop）触发；
2. **在 `AscendStoreConnector` 的逐层流水里插 wait**（它已有 layer save/load 的逐层循环，
   且已经注入了 `external_slot_release_waiter`）——最现实；
3. 若 ACLGraph 完全吞掉逐层语义，则必须退化为"按请求粒度"的屏障：请求结束（或一批
   layer 组结束）才允许覆盖槽位，容量收益会打折但没有正确性风险。

#### 现场

已回退 `dsabase-m1`、无挂载；4 引擎 200、`pt=4237 → 4.2s` 正确。

### 11.17 固定基线重新核对与 block-key waiter 修补（2026-11-03）

- 工作：从 `4234a1f18` 建 `p-main-buffer-reuse` worktree，旧实现保留。
  按 `mc.pdf` 核对 Main-only 布局、Store 队列、hook 与设备事件，记录于
  [p-main-buffer-reuse-audit.md](p-main-buffer-reuse-audit.md)。
- 证据：基线及 p0 安装源码的 `set_external_slot_release_waiter` 均只接受 GVA，
  Mooncake block-key 注册会返回 False；receiver 构造却同时支持两种数据面。
  新回归测试在修补前复现该问题。p0 镜像仍为 `dsabase-m1`、无源码调试挂载。
- 修改：注册门控与 receiver 选择条件对齐；测试正常/晚注册、非适用模式拒绝、
  block-key GET 等待顺序及 waiter 失败禁止 GET。未启用物理复用，未部署。
- 验证：聚焦 UT 及既有 transfer/session 共 60 个通过，Ruff lint/format 通过。
  完整三个模块的 CPU mock 扩展运行存在 tensor 能力等环境/测试问题，未报告全量通过；
  `format.sh ci` 因缺少 pre-commit 未执行，NPU ST 尚未运行。
- 认识修正：hybrid 路径提前保存的 `sync_save_events` 是 KV-ready，不能证明 attention-done；
  Python range API 返回的源静止保证尚未核实，不能先断言 Store 上传一定异步。
- 下一步：Main-only 唯一布局规划、独立 attention-done、恢复范围及快照版本。
  硬件验证前继续保持稳定服务配置。

### 11.18 Main-only、计算完成与私有快照实现（2026-11-03，待 NPU 验收）

- 产物全部归拢到当前仓库；40 项迁移清单位于 `artifacts/p-main-buffer-reuse/tmp-migration.json`。
- 新分支实现 Main-only 统一规划、独立 attention-done、跨 step 不可重置完成对象、启动 waiter 自检、
  严格 range 错误传播及 request-instance/generation 私有 partial snapshot。目标当前单 manager group；
  MTP、graph、V2、PP/CP 与未验证多 group 的实际复用显式拒绝。
- 原生完成语义核查：固定参考 PR #2881 源码和安装库反汇编均证明正常 range write 等待 scatter 完成。
  原安装库与迁入 wheel 哈希相同；不再沿用“Store API 只提交即返回”的历史假设。
- 聚焦 UT 107 个通过，metadata/backend 两模块 205 个通过；完整 pool_worker 的 6/1 已用固定基线复现，未宣称全量通过。
- 模板新选项默认不改变任何四节点启动参数；新实验会关闭 MTP、先固定 block 容量，使用独立容器。
  四节点基线容器配置和启动文件已备份，尚未停止或修改稳定服务。
- 下一步：NPU sentinel、独立实验服务、B=1/2/3、多 chunk/prefix/多请求/取消与失败验证、容量扩容和性能记录。

### 11.19 独立开发容器与 NPU sentinel（2026-11-03 12:40 起）

- 四节点构建 Python overlay 镜像 `0.30.0-main-reuse-dev-20261103-r01`，容器内 Python3.12 编译通过。
  实验模板关闭 MTP、固定5508 blocks、Main shared=3；默认线上配置与基线生成文件未修改。
- 确认四引擎 running/waiting=0 后，只停止并保留原引擎容器；独立开发容器带 owner 标签。
  保留 Mooncake master、其他容器；未手工修改 host 日志/文件。回退入口 `tools/reuse_container_experiment.py restore`。
- NPU sentinel 以 B=1/2/3、6层×3chunk 验证 range PUT 返回后立即污染源，后续 GET 数据仍正确。
  首轮因 remove 尚有 lease 返回 -706 而停；固定参考源码确认 OBJECT_HAS_LEASE。
  修正测试按安全延迟清理处理后全通过，未强制删除。日志均在 `artifacts/p-main-buffer-reuse/`。
- 四实验引擎已提交启动，完整服务、真实多chunk以及容量仍待验收。

### 11.20 Scheduler 合并规格与 worker 物理规格分离（2026-11-03 12:48）

- 首轮 P worker 全部成功规划：78层、Main_slots=5（shared3+独立2）、private_Indexers=21、5508 blocks、
  849920 physical bytes/block；profiling logical multiplier8.212。D0/D1 均健康200。
- P engine 初始化 scheduler 时失败：`Main-only reuse has multiple Indexer caches at layer 0`。
  Scheduler 接收跨rank合并KV规格，不应再次执行仅适用于rank局部的物理内存规划。
- 修复 scheduler 仅从公共逻辑槽布局判断复用；worker/runner 的严格物理规划、接线检查保持有效。
  新增禁止 scheduler 调用 worker planner 的回归，聚焦108项通过。
- 仅重启两个开发P容器，向其site-packages写入修正后的pool_scheduler.py；D与master保持运行。
  因而当前开发容器带镜像外修补，最终交付镜像仍须重建。
- 扩展pytest运行scheduler/worker（启用其autouse fixtures）：194 passed、170 subtests passed，13 failed。
  相同环境在内存载入固定基线scheduler/worker后：193 passed、169 subtests passed，15 failed；
  相同13项全部复现，额外两项为本次修复的block-key waiter回归。
  日志：`ut-20261103-125022-777771.log`、`baseline-pytest-comparison.log`。
  之前直接unittest运行扩展模块忽略pytest fixtures，产生MOONCAKE_CONFIG_PATH等错误，不作为验收结论。

### 11.21 首请求输出正确但 P 收尾死锁（2026-11-03 12:57–13:02）

- Scheduler修正后四引擎健康200。首短请求536输入tokens、15输出tokens，22.5s，输出：
  “以上资料主要描述 Mooncake 将 KV blocks 作为对象进行保存。”
- 后续长请求未返回；P0的shared-memory broadcast提示超过60秒等待。
  不能把首请求输出正确视为step收尾成功，更不能报告多chunk通过。
- 在开发容器内使用py-spy只读抓取worker线程栈：
  主线程停在 `_wait_for_final_layer_save` 逐个等待前序层event；Store发送/接收线程均在空队列get。
  证据 `p0-worker0-stack-r01.txt`，工具下载和校验信息在本仓库packages目录，未安装host工具。
- 根因：receiver在后层覆写前正确等待并clear前代save event，step末尾却重复等这些已消费的event，永不再set。
  修复：队列drain确认全部PUT终结后，跳过已被逐层复用消费的source events；独立层检查与逐层屏障保留。
- 新增回归：已消费event的wait若被调用即失败；聚焦109项通过。正在只重启开发P容器验证修复，D/master不动。

### 11.22 仅重启 P 后 P/D 传输超时（2026-11-03 13:10）

- drain修正后四引擎重新健康200，但新短请求在P0 TP7首层P/D发送超时（native ret=-1，30s）。
  随后TP7等外部slot释放，其他rank在后续层等待；并非上一轮空队列收尾event问题。
- `p0-worker0-stack-drain-fix.txt`：P/D发送与Store发送等待NPU event，receiver等source save，主线程等layer load。
  失败记录包含目标D0原生TE端点15396。尚不能断言为复用代码新问题或旧连接缓存。
- 为排除保留D、只重启P产生的原生连接状态影响，正在同时重启四个带owner标签的开发引擎容器。
  不重启主机，不修改master，不操作其他容器，基线容器与文件仍保留。
- 当前尚未完成drain修正的真实请求验收。后续按干净P/D进程状态重新跑smoke，若重现则继续定位原生链路。
- 13:13 p1的docker stop一度报告“tried to kill container, but did not receive an exit event”。
  随后只读inspect确认已exited（137），npu-smi全卡健康且无残留NPU进程；未reset设备或操作host daemon。
  仅继续启动该带owner标签的开发容器并启动proxy。工具补充“停止报错后检查实际终态”，不升级为host kill。

### 11.23 干净 P/D 进程下 B=3 的真实回归通过（2026-11-03 13:20–13:24）

- 统一重启四开发引擎后均健康200；单/双/三chunk输入537/5017/9656 tokens，分别4.34/6.80/8.38s，
  输出均为正确的中文Mooncake KV对象存储摘要。证据 `request-smoke-all-reset.log`。
- P success(length)=3、D success(stop)=3，四引擎running/waiting均0：确认P收尾完成，drain死锁修正生效。
- 两个同前缀5005-token请求并发、原请求重复前缀、关闭独立长请求连接后的短请求均给出正确内容。
  并发5.23/23.95s，重复4.50s，断连后短请求5.35s，见 `request-stress-b3-5508.log`。
  两个P引擎均有实际成功请求；P1日志出现真实Store hit_blocks=32/39。
- 后续四引擎running/waiting均0。断连探针只证明客户端关闭后服务可继续，不据此声称内部每个abort分支已覆盖。
  完整counter见 `metrics-after-stress-b3.txt`。
- 仅重启P时的原生P/D超时，在本次统一重启后未重现；连接状态问题仍是推断，不把它算成已定位的代码根因。
- 日志量较大，collect改为每容器最近4MiB的有界读取，避免完整cat持续搬运增长日志；容器内完整日志保留。
- 下一轮：共享槽1、32768 blocks、4100-token预算，验证扩容和非整block恢复。配置已本地Ansible生成，尚未切换。

### 11.24 非整block预算需满足TP padding（2026-11-03 13:31）

- B1/32768实验未进入KV分配，在runner `_dummy_run` 的 `num_tokens_padded <= max_num_tokens` 断言失败。
  4100不是TP16倍数，padding后4112超过配置；这不是Main buffer容量或恢复失败。
- 调整实验预算为4112：满足TP16对齐，仍不按block128对齐（余16），继续覆盖partial snapshot。
  新配置 `profile-b1-expanded-aligned.yaml` 与 `profile-r02-aligned.yaml`，原失败配置保留。
- r02四节点镜像已构建、10个生产文件SHA一致、109 UT通过，容器已创建但未启动引擎；
  为不干扰B1重试，先停止这些空闲开发容器。后续使用已校正启动脚本验证r02，无源码补丁。

### 11.25 B1/32768：第三partial chunk暴露非保存rank的旧短快照（2026-11-03 13:43）

- 校正4112后四引擎健康200，真实分配78层、Main_slots=3、private_Indexers=21、32768 blocks、681984 bytes/block。
  短537 tokens与双chunk5014 tokens输出正确，耗时4.94/6.88s。
- 三chunk请求在第三步被 `Shared Main snapshot does not cover the required prefix` 拦截。
  已有日志确认三代partial端点4112→8224→9656，是真实partial恢复路径，未误判为对齐的普通分块。
- 根因：保存rank会在PUT commit后把旧partial索引更新成Full；非保存rank只经历GET，仍保留上一轮旧partial。
  下一步prefer_committed把例如block32/end4112优先于新的Full候选（需覆盖到4224），故拒绝恢复。
- 修复只在旧private snapshot覆盖不足时选择当前Full候选；原生GET失败仍保留旧记录，不提前发布新映射。
  Full公开键不可读时仍尝试该request-instance的immutable private_full fallback。
- 新增非保存rank三步回归，公开Full与contention/private Full两个分支均在修复前复现相同错误；
  再增加GET失败不得提前替换旧partial的用例。修正后111项通过，日志 `ut-20261103-134948-992227.log`。
- 正在统一重启四开发引擎重跑B1。r02镜像早于该修正，已标记废弃、容器保持停止，不能交付；最终需新镜像。

### 11.26 B1/32768/4112修复后真实验证通过（2026-11-03 13:58–14:01）

- 重跑短/双/三chunk，输入535/5016/9659 tokens，4.56/6.80/9.24s，均输出正确KV对象存储摘要。
  三chunk已通过曾失败的第三代partial恢复检查，见 `request-smoke-b1-promotion-fix.log`。
- 并发同前缀两个5000-token请求5.65/6.83s，重复4.33s；关闭长请求连接后短请求5.45s，均正常返回。
  断连后短回答把Mooncake译作“月饼店”，仍为通顺的KV块对象存储描述；不以逐token匹配判正确性。
- 四引擎running/waiting最终均0；真实partial代际日志及NPU内存快照已保存。
  文件：`request-stress-b1-promotion-fix.log`、`metrics-b1-promotion-fix.txt`、
  `partial-snapshots-b1-promotion-fix.txt`、`npu-memory-b1-32768.txt`。
- r03镜像包含所有当前源码修正，四节点已构建并创建独立容器；r02保持停止并标记退役，工具拒绝启动。
  准备停止r01、启动无源码覆盖的r03，B2/4112去掉P的num_gpu_blocks_override，验证原生自动容量预算。

### 11.27 r03最终镜像与自动容量验收通过（2026-11-03 14:05起）

- r03四节点镜像构建与10生产文件SHA校验通过；无源码覆盖。原基线与r01/r02均停止保留，master不动。
- 新镜像内NPU sentinel B1/2/3、6层×3轮全部通过，lease保护删除返回-706时保留延期处理，不强删。
- B2/4112移除P的num_gpu_blocks_override。实际32个P rank全部自动52042 blocks；
  78逻辑层、Main槽4、Indexer21私有、765952 bytes/block。相对旧档5508，当前配置容量约9.45倍；
  当前关闭MTP、max_num_seqs=4，容量值不作为同配置性能对比或单请求上下文上限。
- r03单/双/三chunk535/5017/9656 tokens，23.11/6.90/8.91s，输出正常KV摘要。
  并发同前缀5000-token请求5.47/23.89s，重复3.95s，断连后短请求10.94s，均正常返回。
- 最终四引擎running/waiting/error counter均0；完整日志ERROR/Traceback/传输失败计数0；
  两P节点各16条布局记录一致。见 `final-runtime-check.json`、`metrics-r03-final.txt`。
- 功能提交9824ec91c，随后f9b995c68只整理两处固定Ruff格式，10生产模块与镜像AST等价。
  r03请求后SHA复核也通过。source工作区干净，旧source分支保持原状。
- 入口：`docs/p-main-buffer-reuse-usage.md`、`ansible/profiles/p-main-buffer-reuse.yaml`。
  工具默认r03，正常restart不写源码，旧镜像拒绝启动，回退核对baseline ID与其他开发容器状态。

### 11.28 仓库级检查的证据边界

- 本特性111聚焦UT及205 metadata/backend测试通过；固定Ruff0.14对本次17个Python文件lint/format通过。
- 独立worktree运行format.sh ci，真实工作区不受自动修复影响。CI修改的9文件与本次17文件无交集。
  失败包括基线ascend_config长行、sfa_kv_offload未使用变量、mooncake_connector的TE拼写、
  typos基线文本并被-9终止、shellcheck并发下载触发Text file busy。其余多项hook通过。
- gitleaks hook显式跳过，未执行其未固定且关闭证书校验的OBS二进制下载；未报告secret scan通过。
  完整日志 `format-ci.log`，范围对照 `ci-baseline-scope.json`。未把全仓CI报告为全绿。

### 11.29 1M 验证接入已有 D Host offload（进行中）

- 用户指出 D offload 已存在；源码确认 SparseKVOffloadManager 与 SfaRemoteD2HConnector 可用。
- r03 D 未启用 Host offload，5508 blocks 的容量不足仅描述该配置，不代表需要新开发 D offload。
- r04 使用 BF16 Main、int8 Indexer、MemFabric device_rdma，D Host 预算256 GiB，P继续B2共享。
- SDK原生枚举存在但Python包缺少导出，隔离镜像补公开导出，四节点校验通过。
- r03停止保留，r04已发起启动；尚无1M请求成功证据。
- 详细记录：`docs/p-main-buffer-reuse-1m-validation.md`；原始证据：`artifacts/p-main-buffer-reuse/one-million/`。

- 后续实测：补基线CPP后，MemFabric Host因缺libmf_hybm_accoffload.so初始化失败；转Mooncake Host后，
  D约88GiB Host pool与warmup成功。短请求收到MF_META后DEVICE_RDMA RaInit=328002并Segfault，
  P屏障等待；停止r04。1M尚未通过，当前卡点为所选传输路径的原生运行时，不是D容量。

- 回退未完成：r04三节点已exited，d1 stop报未收到exit事件且仍running，top仅Zl worker。
  为防设备重叠未启动r03，整套服务当前不可用；无host reset/驱动/系统服务变更。

- d1随后自行退出；复核r04全部停止后启动r03回退，未执行主机处理。健康恢复待检查。

- 交接时复核r03四引擎health均200，回退完成，未重新跑请求回归。
  代码与版本入口见 `docs/p-main-buffer-reuse-handoff.md`；附Git bundle、完整patch及SHA256清单。

### 11.30 P独立1M目标启动

- r03直连P的8185-token/32输出请求暴露完整Store前缀恢复失败，保留首个Python异常证据。
- 改为独立prefill+1token数字检索验收，目标仍为真实1M输入及前中后位置检查。
- r05基于r03仅加模型别名，两个P镜像构建、容器创建、10生产文件SHA校验通过。
- 配置保持INT8、B2、4112；无SFA/D offload；D不变，待旧P容器停止后启动r05。

- r05 P健康，8190-token独立prefill+1token数字检索通过，12.13秒。
  64K起逐级请求已发出，D计数基线为0，当前未报告1M完成。

- r05 65530/131069输入tokens独立prefill通过，耗时87.16/353.77s，1token数字检索正确；
  262140-token请求进行中。已生成但未应用chunk32784候选，后续需比较显存与吞吐。

- 新增P独立容量流程工具，当前等待256K明确成功且两个P空闲后才切换大chunk。
  Mooncake connector、B2、INT8、1M上限、MoE预算和模板SHA均有检查；负面拒绝检查通过。
  运行handle46620，候选未应用；当前请求handle33780仍live。

- 256K真实262140 tokens验证通过，1424.30s；切换前P0/P1 running/waiting均0。
  首次stop异常后复核状态恢复流程安全终止，两个P确认退出后已启动32784配置。
  新observe-running模式校验容器脚本SHA，不重复重启；当前等待新档启动与分级验证。

- 32784档8188/65533/131065/262139输入tokens检索通过，3.80/21.40/57.16/209.29s。
  P0 TP16均30013 blocks、P1观察30010，物理KV预算约21.26GiB，512K进行中。

- 新档512K实际524285 tokens独立prefill通过，813.41s；开始约1M真实输入验证。

- 首个1M实际1048541 tokens继续执行，prefix hit=0，D计数0。
  后续改为从该完成archive显式复用前缀检查BETA/GAMMA；重建必须匹配原prompt SHA/token数。
  本地旧调度父进程已SIGSTOP，原1M子进程未停；新后续runner为46414，等待同一1M完成。
  最终需清理旧父进程并做健康、错误、缓存命中与D无流量审计。

- 首个完整1M请求通过：1048541输入、1输出，3375.79s（56.3分钟），ALPHA数字7正确；
  继续同archive的BETA/GAMMA检索及最终健康、源码、错误、D无流量审计。


### §11.30 收尾：P独立1M验收通过

- 冷prefill：实际1048541输入tokens，3375.79秒，ALPHA=7正确；输入新nonce，历史prefix hits为0。
- 同archive前缀复用：BETA／GAMMA实际1048542 tokens，189.36／189.54秒，答案1／3正确；累计本地prefix hits2096896，external hits0。
- 四引擎健康200，P空闲且错误计数0，D成功计数0；P0/P1后续新短请求通过。P1首次SSH255未采集答案，客户端重试通过，无模型重启。
- 本轮日志从启动偏移扫至EOF无指定错误；镜像SHA、源码AST、纳入版本管理profile生成脚本与实际运行脚本SHA均一致。
- 本地暂挂调度器已核实子进程退出后清理，无集群信号、reset或主机服务变更。
- 配置、交付包和限制见[p-only验收文档](p-main-buffer-reuse-p-only-1m.md)。不代表持续decode或P/D 1M通过。


### §11.31 P侧源视图与代次协议首版 本地开发

- 新增reuse_source适配层；从真实布局导出Main/Indexer视图，receiver三方等待后、GET前打开新代次，KV-ready发布完整batch并封口。
- 保留旧waiter，MultiConnector对version=1 provider显式接线；源范围和请求实例校验失败不发布。
- 增加restore-only显式入口，校验无新token，并记录起始gate放行预取；尚无scheduler自动选择和真实connector联调。
- 新增确定性CPU测试；初次自动接线测试受上游基类mock缺失影响，补stub后通过。生产源码未部署，集群仍为之前r05/r03。
- 保守step drain，失败后停止写入并保留分配；不声称原生卡住传输的安全退出已验收。
- 详细实现入口和约束见[pd接入准备](p-main-buffer-reuse-pd-integration-preparation.md)末节。

本轮最终结果：121项聚焦CPU测试通过（原111项＋新增10项），日志`artifacts/p-main-buffer-reuse/ut-20261103-201322-533491.log`。
变更模块Ruff lint/format及两个仓库diff检查通过。源码未提交增量已导出`artifacts/p-main-buffer-reuse/handoff/pd-source-contract-wip.patch`和SHA清单，包含新增文件；不覆盖此前已验收bundle。


### §11.32 P源接口真实NPU／Store与64K模型验证

- r06首个8192-token请求失败，定位空GET走batch分支未打开新epoch；补齐该分支及真正分发回归，122项CPU测试通过。
- 独立NPU测试65521-token人工KV、6层、B1/2/3，Main/Indexer恢复、持有reader阻止覆盖、GET故障禁止继续复用均通过。
- r07模型8190／65532-token冷请求、65533-token同前缀BETA、P1新32762-token请求答案均正确。
- 32 rank共17472 open/read配对，51520次样本比较无reader错误，两节点均覆盖78层；P错误0、D请求0、四引擎健康200。
- r06真实失败、harness整数返回误用、replay多完成记录拒绝以及正常malloc告警误匹配均保留证据并区分，不计为通过请求。
- 完整说明见[p源接口硬件验证](p-main-buffer-reuse-pd-source-hardware-validation.md)。测试的是受控源读者，不代表新P/D网络connector已验收；收尾恢复r05。

§11.32收尾：r05原ID／镜像恢复，10模块SHA通过，新8189-token请求正确，四引擎健康200，P空闲／错误0，D请求0。r06/r07停止保留并退役。

§11.32提交收尾：源码`47f983157`（sign-off）已提交，工作区干净；122项聚焦UT和lint通过。提交前尝试完整pytest，在收集e2e时因缺少huggingface_hub中止，原始日志`artifacts/p-main-buffer-reuse/commit-full-suite-attempt.log`，不报告全量通过。最新bundle／patch另存handoff的latest产物，旧验收包保持冻结。


### §11.33 TargetProvider首版 2026-11-04

- 原样导入YYangY receipt契约，固定46f045aa0acbe7d26903ff364ea13f652ac29d08；未合并其sender。
- 实现RegisteredTargetProvider四方法、完整容量／身份／区域校验、真实runtime依赖协议与既有DSA注册布局适配；不分配第二套KV。
- 添加17项测试，聚焦139项通过；日志ut-20261104-110837-974015.log。自身provider／UT lint通过；共享契约的两处UP042保留原样，未unsafe改写同事类型。
- 明确阻塞：B当前未传retention token；同backing多region契约限制；D Host Main只支持BF16，与现有P独立INT8档不同。
- 真实owner引用／可见性／写回静止和scheduler-worker跨进程接线未完成，无NPU验证；未部署，原r05服务未修改。

§11.33提交：源码`06e16d717`（sign-off）；提交前139项聚焦UT通过，日志ut-20261104-111126-012223.log。真实D runtime和receipt模式仍未接通／验收，无部署操作。

### §11.34 TargetProvider提交后的既有流程回归 2026-11-04

- r08四节点复制当前提交15个生产模块、逐文件SHA核验；无reader注入，receipt缺省，旧Mooncake layerwise P/D，INT8、eager、无MTP/DCP，P chunk4112。
- P独立8188／32765-token数字检索及32766-token前缀BETA正确；P/D 536／5017／9656-token持续生成正确，32762-token三词检索全对。
- P/D两并发、重复前缀、自身连接关闭后的新请求均通过。D0／D1分别7／1个成功stop；结束时四引擎健康200、空闲、error计数0。
- **失败保留**：65535-token P0预期4却返回`!`；相同提示词P1返回`I`。HTTP正常不代表正确性；整轮candidate-audit的passed为false，根因尚未隔离，不直接归因于未被旧流程引用的TargetProvider。
- 完整native日志扫描无指定异常；初次SSH255发生在只读预检、无模型请求，工具禁用跳板连接复用后重试。三个工具lint／format通过。
- 候选空闲后按原ID／镜像／脚本恢复P r05／D r03中；无host reset／主机服务修改。证据`artifacts/p-main-buffer-reuse/target-regression/`，详见[完整回归报告](target-provider-existing-flow-regression.md)。

§11.34收尾：原P r05／D r03恢复完成，ID／image／脚本及源码manifest核验通过，四引擎健康200，新8191-token正确。
原r05对同一65535-token提示词返回正确4，21.23s；chunk32784且代码/调度参数均不同，不据此单独归因。
附加请求后四引擎健康、空闲、error0；proxy恢复原未运行状态，r08停止并退役。

### §11.35 精简A3 reuse Ansible交付 2026-11-04

- 从原A3复制节点及公共字段至`ansible/inventories/a3-reuse/`，只保留Main B2、旧Mooncake layerwise PD和单Master；无旧档位选择器、HA、SFA/MemFabric或D Host offload配置。
- 默认P chunk32784／max_num_seqs1／MoE预算524544，共同模型上限262144，P/D固定r05／r03镜像，单独容器名和日志；与原P独立1M档区别已写明。
- `usage.md`提供预检、镜像安装、gen/start/stop、32K正确性检查及日志/指标排查；明确现有服务占卡、Master复用和完整部署影响。
- 已运行本地gen、playbook语法检查及逐节点只读镜像查询；原inventory未修改，未按新inventory启动或停止服务。


### §11.36 64K间歇性错误答案／NaN定位 2026-11-04

- r09与r08的15个生产模块相同，4112／4／262144／INT8／MoE8192不变；新命名空间和原命名空间的原8K／32K／64K重放均通过，新增4个64K冷样本也通过，原失败未稳定复现。
- r10独立MoE计数探针覆盖32 rank×16 chunk×75层、38400次真实prefill调用；峰值5230，超过8192数为0，没有用增大预算冒充修复；65792档只准备未部署。
- r09将Main设为all独立、4096 blocks的对照仍出现HTTP400 JSON nan，Store当时仍开启。r11彻底移除KV transfer后，7个初始样本通过，但同65532-token失败样本3次随机cache_salt冷重算的第3次再次NaN，prefix hits均为0。
- r11数值检查启用后3次冷重算通过；r12改为设备标志、forward边界读回，8次P0/P1交替冷重算通过。32 rank、4096个批次、2387968次张量检查没有first_nonfinite；不能据此声称竞态已确定或异常消失。
- r13追加只在采样点读回raw logits／computed logprobs／sampled ids的诊断，无模型逐层检查kernel。具体算子根因尚未确定，最初`!`／`I`与NaN是否同因也未证明；生产源码保持06e16d717干净，无修复提交。
- 只操作自有开发容器和诊断镜像，D r03及Master不动。诊断正在继续，结束前必须恢复原P r05并核对ID／image／脚本／manifest及新请求。
- 证据`artifacts/p-main-buffer-reuse/k64-diagnosis/`；详见[64K定位报告](p-main-buffer-reuse-64k-diagnosis.md)。

§11.36收尾：r13采样点12次冷重算（前5次逐chunk、后7次延后采集）通过，1408对记录无非有限值／argmax差异；关闭采集后2普通＋4流式冷请求通过，包含原失败提示词。没有确定具体算子，也不撤销r11纯计算NaN失败证据。原P r05／D r03的ID、image、owner、脚本与10模块manifest恢复审计通过；四引擎健康空闲、error0，D成功请求仍0，proxy原本未运行。恢复后新8K和已知NaN用64K样本均正确。r09至r13停止并退役，源工作区06e16d717干净，未改生产实现。


### §11.37 Receipt交接实现缺口核对 2026-11-04

- 仅fetch远端feat/layerwise-receipt至对象库并固定6c233175d；本地p-main-buffer-reuse仍06e16d717且干净，没有合并、切换或部署远端代码。
- 远端已包含我方TargetProvider四方法和17项UT，代码相同；真实TargetRuntime、owner facade／IPC、provider构造挂载、Host可见性和取消／写回／DMA安全回收仍未完成。retention token字段签发不等于完整retention runtime验收。
- 原有27项receipt CPU UT复跑通过；固定源码ledger及AST提取的6个真实connector方法另行复现普通模式无条件带instance、空backing冲突、缺层或failed仍ready、失败terminal错误、无provider仍publish、admission前提交P通知、内部／外部ID清理不一致。
- 两个harness尝试日志保留，枚举值断言和账本测试的合法scope已校正；不把这些脚本错误计为NPU故障。复现范围为CPU控制流，不是已部署端到端故障声明。
- 证据artifacts/p-main-buffer-reuse/receipt-handoff-audit，完整说明见[实现缺口核对](receipt-handoff-gap-audit-20261104.md)。不建议以“只差runtime三回调”作为可上线判断；需先修connector关键路径，再共同BF16短请求联调。


### §11.38 两分支实现清单整理 2026-11-04

- 用户要求暂不继续64K定位，改为整理P／receipt两分支；本轮没有集群命令。
- 重新查询远端HEAD仍06e16d717／6c233175d，前者为后者祖先，差异8个文件；独立detached worktree放在src/vllm-ascend-receipt-review，当前P分支未切换／合并。
- 两边各139项P相关聚焦CPU测试通过，receipt纯账本27项通过；既有协议问题在完整receipt worktree的真实方法CPU复现仍成立。
- 新增组件映射复现：单Main tensor＋独立Indexer data/scale同group时漏发Indexer回执；默认pytest类前缀Test，现有connector类以Tests结尾，依赖占位收集0项，未运行测试体或NPU测试。
- 新增源码仓库PD_TRANSFER_IMPLEMENTATION_STATUS.md，列代码入口、已实现／待接线／未闭合、A/B分工及验收顺序；更新旧handoff中retention字段已被远端补齐的事实。
- 生产源码行为无改动、未提交。CPU日志、分支关系及复现放在artifacts/p-main-buffer-reuse/two-branch-inventory。

### §11.39 A1/A2 target owner runtime 与注册 catalog（2026-11-04）

- 在独立 `src/vllm-ascend-pd-integration` / `reuse-pd-integration`（基于 `6c233175d`）实现；P 分支源码及冻结评审树未改，S1 草稿另存 deferred-s1。
- 实际 Host 池与 TE 注册 ownership pin、Indexer storage 引用、scheduler 权威 guard；Host-visible fence、原生 Indexer event 与 consumption/compute/writeback 静止后同步 release。异常保留、quarantine 恢复和资源布局变更拒绝均有测试。
- manager 按真实 spec 导出 Main/Indexer 的层/group、形状/stride、region/backing/代次和容量；TP owner 协商 shared backing 身份。稳定 facade 先注入、真实注册后绑定；scheduler-worker 只传纯数据，生产 IPC 仍待 B/J。
- 40 项 runtime/provider/lifetime、139 项 P 聚焦、27 项 receipt 账本、16 项 TE/HostPool 和 4 子用例通过。基线旧测试的 7 个失败经冻结分支复现后更新为实际 legacy HostRegister/fallback 语义。
- r14 仅作小规模合成目标资源测试：最终快照 TP1、TP2 两 rank 通过，真实 native 注册、内容、尚未完成 writeback 的 release 拒绝、同 Host backing/独立注册身份以及真实 connector A2 方法调用顺序均通过。实际 KVCacheManager 分配/追加/retire 验证通过，未触碰 ref_cnt。
- 实验中 wildcard HostRegister 注册失败、通用 Host alias `.cpu()` 崩溃、TP 配置上下文/Request 签名问题均留失败日志；改正测试条件后复验，不混同于 64K 根因或完整模型 PD 故障。
- 真实 PD 远端 Host-visible 回执、全模型稀疏 decode/writeback、sender、取消/抢占/远端 DMA 静止仍未联调；没有伪造 receive event、DMA ack 或新分配生产 KV 池。
- 自有 P r05 在原生目标验证阶段保持运行；D r03 原 ID/镜像/脚本保留。r14 已停止、原 D 已重新启动，健康与源码 manifest/短请求恢复审计进行中；没有 host/device reset、主机配置修改或 Master 清理。
- 证据 `artifacts/p-main-buffer-reuse/target-owner-runtime/`；详见 [A1/A2 实现与验证](target-owner-runtime-a1-a2.md) 及集成仓 `TARGET_OWNER_RUNTIME_HANDOFF.md`。

§11.39 恢复审计补记：D r03 恢复健康后，审计脚本误用 `max_tokens=16` 普通聊天请求，触发未改动 r05 的 `Main reuse could not open the complete committed Store prefix`；P0 退出，P1 随 DP 连接断开退出。没有把失败算作通过，未启动旧问题定位或修复；正恢复原 ID/镜像/脚本的 P r05 双节点，并将审计修正为已验证的 `max_tokens=1` P-only 请求。失败日志见 target-owner-runtime 下 restoration-audit-multitoken-failed-controller.log 与 restored-smoke-p*-error.log。

§11.39 收尾：原 P r05 双节点恢复，四引擎健康/空闲，原 ID/image/owner/启动脚本及每节点 10 文件源码 manifest 审计通过。正确 max_tokens=1 P-only 检查 P0/P1 均返回 4；D 成功请求仍 0、proxy 维持原未运行状态。r14 停止并标记测试退役。最终源码及手动 NPU 测试脚本摘要与 TP1/TP2 通过快照一致；ruff、format、diff 检查通过。A1/A2 owner 交付完成，B/J 数据面和模型联调仍待接线；没有恢复 64K 定位。

§11.39 分支归并：按用户要求把已验证 A1/A2 和所依赖 receipt 基线归入原 `p-main-buffer-reuse`；当前唯一开发目录为 `src/vllm-ascend-reuse`。原 P 历史保持祖先关系，不 force/reset。迁回后源码/原生脚本摘要一致，40/139/27 项 CPU 和 16 项注册/HostPool（4 子用例）复跑通过。源码仓新增可随 push 交付的 TARGET_OWNER_RUNTIME_VALIDATION.md，并更新计划/交接文件的目录和分支；无集群操作。归并证据位于 artifacts/p-main-buffer-reuse/branch-consolidation。

分支归并收尾：A1/A2、测试、交接/计划/验证文档已在原 p-main-buffer-reuse 提交 e817452b2（Signed-off-by），工作区干净；临时集成目录的16文件未提交副本已备份并stash归档。全量格式检查有继承的基线失败，本次变更manual hooks全部通过。未由本轮执行push；最终只读查询显示远端已包含同一e817452b2提交。


### §11.40 4K–128K：Main buffer reuse与原始DCP配置性能对照 2026-11-04

- 当前源码固定`717f6e6e0`，自有r15容器沿用原P镜像/native扩展，复制并核对732个Python文件；原P r05保留，D r03和Master不变。所有配置、脚本、日志和原始请求均在`artifacts/p-main-buffer-reuse/perf-dcp-comparison/`，未修改原inventory或生产源码。
- 单并发P0直连，max_tokens=1，4096/8192/16384/32768/65536/131072，每长度1次预热+5次正式计时。三组固定相同请求体／nonce／salt，核对真实tokens、零local/external prefix hit、无抢占、每次1请求及数字检索答案；测server prefill与客户端首内容TTFT，不代表PD端到端性能。
- 辅助1m-v030（DCP16、Main/Indexer INT8）36次HTTP200，但30个计时样本正确数为5/5、5/5、4/5、0/5、0/5、1/5；保留错误答案，不用它宣称长输入的等价正确性能，也没有恢复64K数值根因定位。
- 未迁移原始1m在VllmConfig初始化因`c8_enable_reshape_optim`未知键失败。保存失败证据并恢复原服务，首次四节点身份／源码／健康／短请求审计PASS后才补测兼容档。兼容档仅删除该过期键，并将原Indexer C8开关等价迁移到CLI int8（当前dtype派生会覆盖旧开关），Main保持auto，DCP16/chunk8192/MTP1等性能设置不变；未覆盖原始配置。
- 主基线兼容1m与Main B2复用配置各36个请求（含预热）均正确，30对正式样本全部通过采样校验。prefill中位数：原始0.60/0.82/1.62/3.30/6.76/14.19s，reuse1.18/2.30/4.67/9.58/20.80/56.42s；延时+96.3%/+181.2%/+187.7%/+190.3%/+207.7%/+297.5%。双方其他设置差异包括DCP、chunk、MTP、Main精度、Store协议；不能将差值全部归为共享槽净开销。
- 全矩阵计时已完成，正在恢复原P r05并等待最终身份／manifest／健康／新单token请求审计。收尾审计完成前不将此轮标记退役。完整中位数/min-max/TTFT、迁移说明和原始证据索引见[性能报告](prefill-reuse-dcp-performance.md)。

§11.40收尾：兼容主基线全部计时完成后，原P r05／D r03四节点的ID、image、owner、启动脚本与每节点10文件源码manifest核验通过；四引擎健康200、空闲，P0/P1新单token数学题均返回4。proxy维持原未运行状态，r15停止退役；D三组前后指标均一致。离线证据审计PASS，三组各36条结果／30条正式计时均完整，摘要与真实tokens、计算tokens及指标增量核对一致；辅助组错误答案未被隐藏。原始未迁移1m启动失败证据保留；兼容迁移说明单列。最终源码仍717f6e6e0、原p-main-buffer-reuse分支干净，本轮未改生产实现、未push。

§11.40时延原因分析补充：仅检查相同源码及原始计时数据，无集群操作。原始1m同时开启enable_dsa_cp，prefill的SFA／Indexer使用TP本地token shard；reuse关闭该路径。4K–32K均为单chunk、没有前序chunk历史GET却已变慢，主要候选为计算路径改变和逐层PUT／槽位交接暴露开销。64K两chunk每共享Main层理论恢复约32K历史，128K四chunk累计约192K（约6倍），叠加更长历史Indexer成本；不能将线性外推残差直接当成GET时间。现有数据尚不能量化各项贡献，后续应做同条件全独立Main/B2与纯计算DSA-CP消融、关键路径timeline。细节已写入性能报告，机器可读推算见perf-dcp-comparison/latency-cause-analysis.json；未修改生产代码或重新部署。

§11.40参数优化候选补充：按用户希望接近原始DCP性能的目标，核对B／prefetch约束并执行纯CPU布局解析，B2/P2、B3/P2、B4/P2、B4/P3、B8/P4均被当前解析器接受，未做新NPU实测。代码没有P<B硬限制，实际lookahead受槽位与读者生命周期制约；总Main槽分别为4、5、6、6、10（含首尾独立）。候选及状态保存在tuning-candidates.json，性能报告补了HBM容量公式、先测DCP1参考下限／全独立Main对照、batch64/128/256、chunk与MoE预算协同、计算分片适配及后续减少恢复量的路线。未改生效配置、源码或集群状态；尚无新收益承诺。


### §11.41 32K／64K／128K调优与关键路径分析（扫描中止、阶段归因完成） 2026-11-04

- 用户明确本轮不做1M请求回归。新r16 P候选使用原P镜像/native扩展与717f6e6e0源码；原四节点ID／image／脚本及每节点10文件manifest预检通过，候选732文件摘要通过。只操作自有P容器，D r03与Master不动。
- 统一262144模型上限、DCP1／INT8／chunk32784／seq1／无MTP／MoE524544，pure→全独立Main逐层Store→B2/P1→B4/P2→B8/P4→B4/P2/batch256；用户提供官方latest指南后，在原监督流程末尾追加B3/P3／independent[0]／批次限制0的Mooncake参考组合，不中断当前监督和最终恢复。
- 每长度1预热+3计时，同一固定case0–3，max_tokens=1；质量和计时同时保留。官方配置有device_sdma／GVA直接batch_copy、默认prefetch3和不拆批差别；RFC报告2–4buffer可掩盖多数加载。并非当前memcache实机对照，没有启动其MetaService或改PD connector。
- 证据位于artifacts/p-main-buffer-reuse/perf-tuning-32k-128k；详见prefill-reuse-memcache-comparison.md。计时进行中，结束必须恢复原P r05并审计四节点身份、源码、健康和新单token短请求；不把空白行或HTTP200当成通过。

§11.41方向修正：用户指出参数扫描先于时间线归因。六组各12条请求（9次正式）完成后，已主动中止b3p3-guide请求循环、取消BF16组；部分参考请求原样保留，不当完整对照。取消通过校验PID/父进程/命令的本地测量SSH执行，最后请求排空后才停自有r16模型；pipeline-failure为主动取消传播记录，不是NPU崩溃。现先备份/离线解析B2 TP0原始profile，之后恢复原P并审计。没有新增参数实验或重启D/Master。

§11.41收尾：B2独立128K profile的TP0原始数据已备份、离线导出成功。3211935事件流式分析定位：CANN Free 11.466秒，Prolog提交前占11.229秒（97.9%）；真实chunk1–4该阶段Free为0.002／0.466／3.172／7.589秒。Event Synchronize累计54.680秒主要在后台线程且与计算重叠，不能算成净损失。量化SFA／Indexer设备算子累计24.999／9.507秒，不是墙钟因果占比。进一步区分slot gate、metadata和GET完成仍需固定B2的细粒度边界探针，没有继续盲扫或删除I1屏障。原P r05／D r03四节点身份、10文件源码、健康空闲和P0/P1新单token请求恢复审计PASS；六组72条记录／54次正式均有效，主动中止组部分记录与未启动组明确区分，离线证据审计PASS。r16停止退役，源码717f6e6e0／原p-main-buffer-reuse干净，无生产实现改动或push。完整数值、边界与复算脚本见prefill-reuse-critical-path-analysis.md。

### §11.42 固定B2/P1边界探针（诊断完成，原服务恢复） 2026-11-04

用户授权后补队列/旧save/旧compute/attention gate/reader/source代次/地址构造/range拆分/native GET-PUT/ready发布及主线程等待时间戳。仅r17诊断overlay，原732文件基线和分支不变；AST剥离还原及6项CPU验证通过。原四节点ID/image/脚本/源码/健康预检通过，正准备自有P r17。固定原B2/P1参数，128K关闭/开启探针对照与独立TP0 CANN关联；请求均max_tokens1，结束后下一条关闭探针短请求统一导出，避免forward内逐层日志。D r03和Master不动。

时序核对发现GVA在attention gate前构造元数据并在本层聚合groups，range路径在gate/读者屏障后构造、按group提交；FIFO是共性。该差异是探针重点，尚无收益断言。证据prefill-boundary-trace，收尾必须恢复原P并审计。

§11.42收尾：12次请求全部有效，128K off/on两对为56.714/56.551s、56.867/56.195s；独立profile56.736s，P0 16TP四label共64文件无丢失，312个main wait均与同次CPU MLA span对齐。10.361s CANN Free中9.553s在native GET、main load wait内总计9.631s；build_addrs直接暴露0.000464s，FIFO零，reader约0.000003s。后续chunk GET逐层中位47/96/146ms，对应前层SFA约80ms，后两chunk难掩盖。

§11.42时序/组件发现：GVA在attention gate前准备地址、整层聚合并按independent/vllm_cached_tokens过滤；block-key在gate后准备、按group调用，且_process_load_for_layer_batch的block-key分支不经过该驻留起点过滤。实测21独立Indexer仍参与GET，57单段层与16 Main+Indexer data+scale三段层在第4chunk平均129/353ms，后者payload仅多约20%；21层贡献7.402s GET暴露，包含其Main本身，不能全算可省。下一项应验证previous-step独立组件块映射/驻留并过滤range，外部prefix和不确定驻留保留完整GET，Store格式/PUT/offset与I1不变。未做这项生产优化。

§11.42恢复与证据：P r05/D r03四节点原ID/image/owner/脚本与各10文件源码manifest、健康空闲、新P0/P1单token数学题均PASS；D请求指标前后一致、Master不动，proxy维持未运行。r17停止退役，原p-main-buffer-reuse/717f6e6e0和732文件均未改。离线审计初次误将profile_stopped后的最后事件要求为arm_complete，已修正成唯一完整结果/完成事件并复核，日志保留，不是NPU故障。完整结果和复算脚本见prefill-reuse-boundary-trace-results.md及artifacts/p-main-buffer-reuse/prefill-boundary-trace。


### §11.43 32K/64K 完整层计算与槽位最早可用时间实测（2026-11-05，完成／原服务恢复）

目的：量每层真实设备计算、GET／PUT、槽位释放与提交时间，验证扩大共享环／lookahead是否能补回等待。计划／源码快照／原始数据在`artifacts/p-main-buffer-reuse/perf-layer-overlap-32k-64k/`。冻结717f6e6e0／原p-main-buffer-reuse；r18诊断overlay共733文件，原732文件不动，4文件剥离探针后AST等价。新增完整decoder层scope及已有KV-ready／compute-done／attention-start的record标记，不新增device同步／poll、不移动gate；正式请求关闭采样，profile另计。

共同配置TP16×DP2、DCP1、eager、Main／Indexer dtype参数INT8、chunk32784、seq1、无MTP、batch64／16MiB、首尾Main独立，max_model_len统一131072。每组每长度1预热＋3正式，再加独立32K／64K profile及flush，共48请求全部答案／实际tokens／computed tokens／零prefix hit／无抢占／单请求校验有效。只测P-only输出1token，不做1M或新PD数据面验收。

| 配置 | 32K prefill中位数 | 64K prefill中位数 |
| --- | ---: | ---: |
| pure（相同计算参数，移除KV transfer） | 9.571s | 20.316s |
| B2/P1 | 9.585s | 20.926s |
| B4/P1 | 9.583s | 20.952s |
| B4/P3 | 9.585s | 20.419s |

只加buffer而不加prefetch未见收益；B4/P3追回64K约507.5ms（2.43%），相对pure额外开销约0.51%。代价是Main物理槽4→6，固定KV预算下日志num_blocks30010→24614（约−18%）。没有改生效inventory，未证明B4是最少buffer数，B2/P2尚未测试。

B2 64K真实core区间并集18.901s，其中SFA12.466s、LightningIndexer主核2.445s、MlaProlog主核1.616s；pure对应core18.906s、SFA12.459s，计算大头关闭offload后仍存在。每层core中位数约104.7ms；第二chunk历史恢复每层GET中位50.7ms、最高131.9ms。GET native累计5.272s与模型core重叠约4.575s，不能全部当损失。直接GET等待Free由B2的516.0ms降至B4/P3的69.3ms；其中第0层68.9ms，其余77层合计约0.39ms，层间稳态基本掩盖。最后一轮PUT native累计反而增加但端到端更快，说明累计CPU调用时长不足以定位净开销。

按旧slot compute-done设备事件、保守Store source完成及receiver前序GET完成约束，至实际GET开始的间隔中位数B2 27.0ms、B4/P1 65.7ms、B4/P3 25.9ms；新增空槽只有配合提前调度才被利用。这不是允许无视collective约束移动DMA gate。当前剩余优先候选是证明独立首层／Indexer驻留后过滤GET（64K约69ms首层预算）；更大收益应先同DCP1对比Main BF16与INT8计算路径，或设计计算分片与共享Main解耦。不能将原始DCP／BF16配置的整体差距全归给offload，也不承诺切dtype即追平原始。

采集：P0 16TP两长度每组32份host trace，三复用组共96份，无drop；每个历史层16rank齐全；设备仅TP0。每组78／156个真实层，复用组各702个已有fence记录映射齐全，设备计算关联覆盖率超过99%，pure为100%。CANN按Node@launch／ACL connection关联，排除AI_CPU collective包络和DP dummy；初版reader遗漏Node入口及一次cache版本交接错误已修复重算，记录保留，不是运行请求失败。本地初始监督进程退出时，按实际存活容器与模型进程从pure已启动阶段续接，没有重启该模型或重放前三组；后续监督和解析使用独立会话。

恢复／审计：原P r05、D r03四节点ID/image/owner/脚本、各10文件源码manifest、四引擎健康200且空闲、P0/P1新单token数学题返回4全部PASS；D请求指标前后一致，Master未动，proxy保持未运行，r18停止退役。当前源码分支／HEAD／732摘要不变，733文件实际部署快照与manifest一致，completion-audit.json全部门槛PASS。完整逐层CSV、时序图、预算／memcache核对及限制见[prefill-reuse-layer-overlap-32k-64k.md](prefill-reuse-layer-overlap-32k-64k.md)。


### §11.44 Mooncake 池化五部署档比较（2026-11-05，完成／B2-64K 未通过／原服务恢复）

用户确认范围：原始 200K、原始 1M、普通逐层无复用、B2/P1、B4/P3；全部启用 Mooncake，32K／64K，每长度一对预热＋三对正式 P0 冷写入→P1 远端读取，输出固定 1 token。每对相同 prompt/salt、不同对隔离；核验零本地命中、实际 external hit／补算 token 和答案，失败与未命中单列。

保留部署档 DCP／dtype／chunk／MTP 等参数，统一 max_model_len=131072 仅为本轮请求对照；原始档移除旧 c8 开关、显式 Indexer INT8 并使用已验证 RoCE 配置，兼容调整单独记录。此实验不代表原始 1M 容量验收。冻结源码 717f6e6e0／p-main-buffer-reuse，r19 私有诊断 overlay 仅记录保存 handler 最终 target watermark／writer 拓扑，不改搬运或计算同步。候选共 733 Python 文件，剥离探针后 AST 等价及 6 项 CPU 检查通过。

只切原 P r05 和自有 r19，D r03／Master 不动。独立本地监督进程负责逐档验证和 finally 恢复；结束要求原四节点 ID／image／script／各 10 源文件 manifest、健康空闲和两个 P 单 token 数学请求通过。计划／配置／证据：`artifacts/p-main-buffer-reuse/mooncake-pool-deployment-comparison/`。结果未出齐前不沿用上一轮无池 pure 数字，也不把保存完成标记当成功命中。


§11.44 采集收尾：实际76次请求尝试／75条结果，73有效（54正式），36对冷／远端双方通过；另有B2隔离64K冷预热单独通过。原始200K .92在首个32K预热的空闲P1 MTP dummy logits all-gather处OOM（申请2.37GiB、仅1.38GiB可用），仅将KV预算降至.88兼容复测，保留原MTP／DSA-CP／chunk等参数；原始失败另存initial092-evidence。一次复测监控误扫残留traceback、尚未发请求即退出，已在自有私有日志清理后重跑，不作为模型失败。原始1M／全独立Main／B4P3各16请求通过，B2P1的32K八请求通过；B2混合序列64K冷请求阻塞>256s，随后同参数隔离重启的64K冷预热22.261s通过、P1远端读取仍达300秒截止，未继续正式64K样本。日志仅能定位逐层load完成等待，未区分软件屏障与native GET，不展开旧64K根因修复。

正式prefill中位数（32K冷／远端，64K冷／远端）：200K兼容.88为2.270／7.689、4.578／15.296s；原始1M为3.286／1.070、6.775／1.492s；全独立Main为9.582／5.378、20.335／10.692s；B2P1为9.587／5.299、64K无正式有效样本；B4P3为9.586／5.159、20.444／10.352s。B4相对全独立Main的冷增量0.04%／0.53%、命中约快4.1%／3.2%；固定KV预算num_blocks由B2的30010降至B4的24614（约−18%）。B4未复现B2的阻塞不是根因已修复的证据。所有有效外部命中达到可匹配块的完整对齐边界、零本地hit、computed=actual−external，答案／实际tokens等检查通过。DCP16的命中单位2048，DCP1为128，补算量区别单列；200K保留legacy DSA-CP与Index cache，因此不能把整套差距称为DCP单变量收益。详见[池化性能报告](mooncake-pool-deployment-performance.md)，逐请求、配对审计、CSV、快照及故障记录在mooncake-pool-deployment-comparison。finally 已恢复原 P，最终恢复审计如下。


§11.44 恢复／离线审计：原 P r05、D r03 四节点 ID／image／owner／脚本、各10文件源码、健康200且空闲，以及 P0/P1 新 max_tokens=1 数学题返回4全部 PASS。D 请求计数与预检一致，D／Master 未重启，proxy 保持未运行，r19 停止退役。源 p-main-buffer-reuse／717f6e6e0 与732文件未改，实际733文件部署快照与manifest相符；配对body相同、endpoint不同、salt隔离及有效seed保存证据审计PASS。completion-audit.json 的 PASS 仅代表证据／源码／恢复完整，benchmark_status=COMPLETED_WITH_FAILURES、functional_stability=FAIL（B2 64K混合序列和隔离远端读取阻塞）；B4本轮16请求通过。没有将超时、初始OOM、仅预热样本或上一轮cold-only结果填成B2的64K正常中位数。完整报告、三次样本范围、client/server TTFT与逐请求CSV见mooncake-pool-deployment-performance.md。


### 11.45 最新 receipt 分支核对及完整 PD 补齐（2026-11-05，进行中）

前置 Mooncake 五档测试见§11.44，原 P r05／D r03 已恢复。本轮未切换模型部署。
实际 fetch `maz/feat/layerwise-receipt` 到 `fa5aec40b`，原 P 分支上合入12提交并审查
25文件；远端 handoff 中接口实现不等于完整调用链已通。已修 metadata 构造失败、HTTP
lease 编码、真实 scheduler hook、D 错标 restore-only、source有效尾部、source fence
覆盖和 retirement 失败放行。新增显式 loopback 值类型 IPC，独立子进程双向 RPC 的
retain／validate／release、超时未决与拒绝重发通过，但尚未接生产 factory。
P141、receipt37和组合61项／24子用例通过，另1项真实运行时限定用例明确skip。
原容器内新增独立进程做候选源码内存导入检查，不替换已安装源码、不启动模型／DMA；
检查结果和完整日志见本轮 artifacts，不作为功能端到端证明。完整模型验收仍待后续
多 rank owner／Host注册／精确传输与receipt／消费写回／取消抢占接线后执行。
详见源码仓 PD_TRANSFER_COMPLETION_REVIEW.md；本轮证据目录为
`artifacts/p-main-buffer-reuse/pd-transfer-completion-20261105/`。

本轮集成基础修复已在原 `p-main-buffer-reuse` 提交 `9cbdde548`（Signed-off-by，双亲717f6e6e0／fa5aec40b），源码工作区干净，未push。实际NPU运行栈的候选739个Python源码内存导入、receipt／普通标准hook、3项真实sender方法的CPU fence测试通过；没有替换服务文件／启动模型或DMA。复核原四容器ID／image／owner和各10源码摘要一致，健康200、running／waiting均0。变更manual hooks全通过；全仓format.sh ci在隔离副本因既有ascend_config／sfa_kv_offload问题失败，未污染交付源码。基础验证摘要foundation-validation.json明确标记完整模型功能／性能NOT_RUN，生产RPC factory未接，活动目标继续。


### 11.46 生产 owner／scheduler 接线与控制验证（2026-11-05）

第二阶段生产接线（2026-11-05，完整PD目标继续）：新增ReceiptScheduler与串行KVCacheManager适配器，反向retention RPC和实际分配／free使用同一实例锁；标准GPU fence先于target release，覆盖正常结束/取消延迟free、抢占、强制prefix reset和接收失败。新role factory接入connector/MultiConnector/model_runner初始化；D注册选择已有HostMain+HBMIndexer，不再走进程全局rendezvous；新增全TP原始lease和TargetManifest、HTTP传播及本地lease选择。真实远端静止proof、传输计划、SourceReady直接发送、worker-ready、消费写回和restore-only仍待接线，缺proof时分配保持。P141、组合67项/33子用例通过，1项运行时依赖skip；实际vLLM scheduler/KVCacheManager四条CPU控制路径通过，不代表模型/DMA通过。原环境未切换，证据在pd-transfer-production-wiring-20261105，源码最新说明PD_TRANSFER_COMPLETION_REVIEW.md。

第二阶段收尾：原 p-main-buffer-reuse 已提交 `36e6aa4d3`（Signed-off-by），21文件，工作区干净，未push。743生产Python源码摘要与最终native-stack内存导入快照一致；真实依赖下6项direct/binding/finished hook、3项sender fence、1项CPU真实存储/录制TE的注册选择，以及4条真实vLLM scheduler控制路径通过。Native注册范围测试首次因harness缺少Mock名称失败，修正后通过并保留原日志；不是服务故障。变更manual hooks全部通过；全仓CI继承未改动基线失败，隔离格式化7文件与本轮交付无交集。原四容器身份/image/owner与各10源码摘要匹配，健康200、running/waiting均0，未切模型部署。validation.json明确标记真实新TE注册、完整模型功能与性能NOT_RUN，真实远端proof producer/数据面plan未接，完整活动目标继续。


### 11.47 Manifest逐entry计划、碎片MR与receive admission（2026-11-05）


第三阶段目标计划与接收确认（2026-11-05，完整PD目标继续）：已补manifest逐entry传输计划及请求级预编译索引，基于既有DSA mapper校验实际MR、页/stride/group/block/tail；worker本地解析真实地址仅走peer数据面，不进入scheduler IPC。同一真实Host backing支持多段不重叠注册，复用bounded DSA packer。新增精确entry覆盖/独立静止判据；scheduler通知P前等待所有TP owner安装ledger和request映射，重复metadata不重置结果，部分admission失败保留目标。真实receiver已校验带明确DMA静止bool的terminal和完整generation，acquire后校验失败保持lease可追踪。CPU组合119项/46子用例通过、1项运行时限定skip，P141通过；原P独立进程747生产Python内存加载、3条实际receiver/early-admission方法、既有hook/fence/注册选择与实际vLLM scheduler控制用例通过，没有模型计算或远端DMA。生产SourceReady直接writer、exact epoch、真实可见性/use/写回、all-rank readiness/free proof、取消抢占与restore-only仍待完成；完整模型功能/性能NOT_RUN。原服务未切换。源码最新说明PD_TRANSFER_COMPLETION_REVIEW.md，证据pd-transfer-data-plane-20261105。

第三阶段收尾：原 p-main-buffer-reuse 已提交 `aca80d8ef`（Signed-off-by），20文件，工作区干净，未push。747生产Python源码摘要与最终native-stack内存加载快照及commit逐文件一致；P141、组合119项/46子用例（另1运行时限定skip）、legacy receipt37项/4子用例通过。实际依赖下3条精确receiver/提前admission方法、6条hook、3条sender fence、1条CPU注册选择和4条真实vLLM scheduler控制路径通过，负向帧错误日志来自注入测试，不是服务请求失败。变更manual hooks全通过，全仓CI仍因未改动基线lint/拼写失败，隔离副本自动格式化7文件与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要匹配，健康200且running/waiting均0，未切部署。validation.json明确真实新注册/远端DMA/模型功能与性能NOT_RUN；SourceReady直接writer、真实receive/use/writeback及全rank静止proof仍待接线，完整活动目标继续。


### 11.48 SourceReady writer与exact epoch（2026-11-05）


第四阶段SourceReady writer与exact epoch（2026-11-05，完整PD目标继续）：manifest请求保留manager block和原始目标端口，SourceReady直接登记全部read job，在已有NPU sender线程调用batch_transfer_sync_write，Host/device分批，Main只由P0写、Indexer逐rank写；旧save hook排除manifest防止双发，混合batch保留普通路径，未接Store registry时显式拒绝。source consumer改为ready+sealed+所有exact epoch读者完成才可复用，按slot高水位支持旧Indexer重复等待；普通sender也回调确切ticket。原生完成先释放源读者，独立proof线程批量发送逐entry结果并复用REQ socket，不等待D ACK占槽；未知原生异常保持读者。worker完成后完整已登记计划排空再清理，部分计划按cancelled退出且不提交队列中未开始的DMA；这不等于已完成D→P取消协议。CPU组合181项/50子用例（1 runtime skip）及P141通过，真实ZMQ与CPU实际字节/页尾/fence/ACK阻塞/失败/取消测试通过；原P独立进程748生产源码内存导入、9条真实准备/receiver/提前admission方法及既有hook/fence/注册选择和实际scheduler控制路径通过。没有真实远端WRITE/新注册或模型运行，未切服务。D真实可见性/use/写回、全rank readiness/release proof、晚到proof清理、远端取消和restore-only仍待接通，完整模型功能/性能NOT_RUN。证据pd-transfer-source-writer-20261105，源码PD_TRANSFER_COMPLETION_REVIEW.md。

第四阶段收尾：原 p-main-buffer-reuse 提交 `4c455c943`（Signed-off-by），12文件，工作区干净，未push。组合最终182项/50子用例（1运行时限定skip）、P141通过；748生产Python源码与最终native-stack内存加载及commit逐文件摘要一致，9条准备/receiver/提前admission方法及既有hook/fence/CPU注册选择和四条实际scheduler控制路径通过。已补已封口target代际重放拒绝，以及peer操作耗尽重试后不再逐层重复超时。变更manual hooks全通过；全仓CI仍为未改动基线lint/拼写失败，隔离自动格式化7文件与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要匹配，健康200且空闲，未切模型部署。新增真实尺寸元数据probe：TP16×78层、32K prompt、block128、256请求blocks的合法manifest在现有owner codec触发traversal limit，已复现，尚未修复；需要压缩重复block map后再进D/模型接入，不上调限制绕过。真实新注册/远端DMA/完整模型功能与性能NOT_RUN，完整活动目标继续，证据pd-transfer-source-writer-20261105。


### 11.49 D消费与回收接线、元数据容量（2026-11-05）

第五阶段D消费/回收接线（2026-11-05，完整PD目标继续）：新增exact原生完成到真实D stream依赖的本地桥、整个execute_model的owner-use及写回尾部fence、全部TP owner就绪门禁和基于静止proof的ReleaseAck。接收完成/失败仍保留内部请求身份、ledger和block map至owner retirement，避免迟到失败遗失与外部ID重用串线；失败通过真实vLLM failed_recving按请求报告，涵盖已经RUNNING的retained请求。关闭地址lookup gate，首次lookup前关闭在P按无native读取取消；已缓存地址的D→P远端取消仍未完成。owner/HTTP wire v2对重复block map引用，保持8MiB/500k node限额并兼容读取v1，TP16×78层32K/64K/128K元数据往返通过。组合CPU284项/76子用例（1 runtime skip）、P141通过；原P独立进程749生产源码内存导入、15条真实connector方法与7条实际scheduler控制路径通过，最终源码摘要匹配。变更manual hooks全过；隔离全仓CI因未改动基线lint/拼写失败，7项自动格式化与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要和健康空闲匹配，没有切换模型服务。真实新注册、远端WRITE、完整模型功能/性能NOT_RUN；远端取消、restore-only调度及跨owner部分release失败的请求隔离仍待完善。证据pd-transfer-decode-wiring-20261105，源码PD_TRANSFER_COMPLETION_REVIEW.md。


### 11.50 新 bounded 注册与原生 WRITE 探针（2026-11-05）

第五阶段提交4204f545d（原p-main-buffer-reuse，未push）。独立r20容器实际运行生产worker注册与ManifestSourceWriter，目标注册5个MR，P两层源job都返回成功/静止，D执行prepare_decode并发布；原生日志roce_mode=true、use_fabric_mem=false。整轮FAIL：P在engine disconnect后的清理/退出阶段报malloc(): unsorted double linked list corrupted（-6），父进程断言后139退出。目标字节读取及后续use/writeback/retirement尚未执行，不能记PASS；根因未定位。控制监督已在finally恢复原D容器对，P未停，原D启动后健康审计仍在等待；终态看pd-transfer-native-write-20261105/supervisor-state.json及restoration-audit.json。完整模型功能/性能NOT_RUN。


### 11.51 原生 WRITE／读取／回收小探针（2026-11-05）

第六阶段原生小探针（2026-11-05）：使用4204f545d的749生产Python源码，TP1/两层/23有效tokens同机NPU1源进程→NPU0目标进程，实际bounded注册5个MR，生产ManifestSourceWriter及真实ZMQ地址/回执路径完成device RoCE WRITE；D通过exact账本建立真实receive依赖并发布。全部8个Main/Indexer entry、partial尾部及未写区域字节正确，跨层复用Main源buffers；native消费/在途manager写回阻止提前释放，完成后release/retire/unregister/pool close通过，源/目标正常退出。显式保留tensor/context并先校验再析构TE避免了本轮退出崩溃，但尚未隔离具体free地址或修改生产shutdown，不能证明HBM double free。未跑真实GLM forward/decode、TP共享身份、跨节点WRITE或模型功能/性能。入仓测试receipt_transfer_npu.py与实测脚本非import AST一致（仅Ruff格式/排序/unused清理）。r21曾因准备检查早于复制完成而触发多余D重启，无NPU提交，随后原环境恢复；新监督同步prepare/hash/imports后才stop，准备失败仅清理候选，5条生命周期CPU回归通过。r22探针PASS后正在finally恢复原D对，P未停，终态看pd-transfer-native-order-20261105。功能审查六项缺口继续补齐后再做正式完整模型验收。

### 11.52 Ansible 复现 r22 并推进模型启动（2026-11-05）

以 r05／r03 Ansible 基线为回退点，使用新容器重跑 r22。探针使用 D0 的 r03 镜像 ID
`sha256:4721490241c6…`，从 `4204f545d` 提取 749 个生产 Python 文件，连同原始探针共
750 个 SHA256 与 §11.51 一致。准备、校验及导入完成后才停基线 Decode 对，P 和原 Master 保留。
D0 同机 NPU1→NPU0 的 TP1 原生 WRITE 复现通过，5 个实际目标 MR、8 个 Main／Indexer entry
及有效尾部校验正确，异步写回阻止提前 release，源和目标清理正常。候选自动清理，基线四引擎
及代理恢复健康。证据见 `artifacts/p-main-buffer-reuse/r22-ansible-reproduction/`。

随后使用同目录的隔离 Ansible 候选执行真实 GLM 启动及短请求验证，四容器各自的 749 个源码哈希
均匹配。候选使用独立服务、RPC 和 Master 端口，BF16 Main、INT8 Indexer、D Mooncake Host offload
及 ReceiptScheduler。首个模型门禁要求四引擎／代理健康、答案正确以及 D receipt 完成证据。

首轮模型候选的两个 P 健康 200，但两个 D 在 `initialize_kv_cache → MultiConnector →
AscendStoreConnector → KVPoolWorker._as_cache_tuple` 处报 `NoneType.numel`，未发起模型请求。
该轮保留了基线 D 的 Store 子连接器；Host offload 布局合法省略 Main 的 device tensor，
Store 注册未处理该空位。保存失败栈后终止候选健康等待，并执行基线恢复。
第二轮保留 P 的 Store＋逐层连接器，D 只配 receipt 逐层连接器，固定源码与前轮相同。
第二轮证据单独存放在 `artifacts/p-main-buffer-reuse/r22-ansible-receipt-only-decode/`。

第二轮 D-only receipt 配置四引擎和代理全部健康 200，越过了首轮 KV 注册失败点。
短生成请求在 D0 触发 `EngineCore.step_with_batch_queue → scheduler.schedule(throttle_prefills)`，
固定源码的 `ReceiptScheduler.schedule(self)` 拒绝该实参并使 D0 退出；代理重试记录 D HTTP 500，
无有效生成正文、无模型 receipt，D1 随后报告 peer reset。问题发生在首个调度步，尚未进入
模型级 KV 传输验收。没有修改固定源码；后续需适配镜像的 scheduler 调用签名再重跑首请求。

两轮模型候选及独立 Master 均已清理。原 `mooncake-p0` 的容器 ID 和启动时间一致，
r05／r03 基线四引擎及代理恢复健康 200。使用的候选 inventory、模板、生成脚本和日志
保存在上述两份证据目录，正式 inventory 和生产源码未修改。

### 11.53 ReceiptScheduler 调用签名兼容（2026-11-05）

修复 `ReceiptScheduler.schedule()` 的覆写签名，使其接受并向 vLLM 基类传递
`throttle_prefills: bool = False`，保留每轮 receipt 轮询及延迟释放。候选以
`4204f545d` 为底，仅替换一个生产 Python 文件，四节点 749 个文件的 SHA256 匹配。
候选 r03 镜像内 8 项 scheduler 控制回归通过，包含 EngineCore 的有参调用和无参调用。
用 §11.52 的 D-only receipt 配置重跑模型短请求，候选与回退证据存于
`artifacts/p-main-buffer-reuse/receipt-pd-scheduler-compat/`。

本轮首次启动的覆盖包误过滤非 Python 文件，两个 D 因缺少运行时编译所需
`sparse_kv_offload.cpp` 退出。为两台候选 D 补齐原快照的 55 个非 Python 文件，804 个
覆盖文件哈希全部匹配后，用 Ansible 仅重启候选 D。后续候选直接使用完整覆盖包。

补齐后四引擎和代理健康，首个模型请求已越过 scheduler 并由 D0 发布
`expected_senders=16` 的目标，P0 随后提交到 D0 的跨节点 RoCE WRITE。多个非零 TP
rank 在 P 的 Store SourceReady 路径报 `Source read belongs to an unknown or closed producer request`，
请求未完成。清理时 Docker 对 P1 一次报告未收到退出事件，随后只读复核确认该候选已退出且
设备空闲，按其捕获 ID 删除并恢复基线。调用栈和恢复日志均保留。

下一候选按 manifest 的 rank/layer 要求登记源读者，避免 Indexer-only rank 完成 terminal 后
对后续无本 rank 写入要求的层继续登记。实际有任务的关闭状态仍拒绝，且保留首次 writer 错误
作为异常原因。writer／plan／source 共 32 项 CPU 用例通过，覆盖 terminal 后的无任务层。
新 Ansible 监督日志位于 `artifacts/p-main-buffer-reuse/receipt-pd-source-readers/`。

读者修复候选在四引擎与代理健康后完成首个真实跨节点模型请求：17 个 prompt token、
5 个生成 token，答案「一周有七天。」，同一请求在 D0 的 TP0–TP15 均记录
`published after prepare_decode`。随后 P0 在请求收尾的无模型 step 调用
`AscendMultiConnector._receipt_restore_only_requests`，把 AscendStore 的
`list[ReqMeta]` 当作请求字典而退出，P1 随后报 peer disconnect。多 chunk 探针在
tokenize 前连接失败，未进入长请求。证据见 `receipt-pd-source-readers/`。

已修复扫描器只读取 request-keyed mapping，新增真实 MultiConnector 方法回归。
相关 36 项用例及变更 lint 通过。新候选与上轮相比仅一个生产 Python 文件摘要变化，
证据见 `receipt-pd-store-metadata/`。下一模型门禁包含短请求后的健康、两条跨 chunk
请求各自的全 16 rank receipt，以及按重放请求 ID 关联的 Store 命中。

元数据修复候选实机完成三条请求并通过最后一次四引擎／代理健康检查。短请求返回
「一周有七天。」；5,577-token 冷请求返回 MAPLE，5,578-token 同前缀重放返回
QUARTZ。冷请求的 Store 查找 0/43，重放 43/43、5,504 token 命中；两条长请求分别
在 D0 TP0–TP15 发布 receipt 后生成，scheduler 记录了对应的目标释放。
原监督脚本首次抓取的 receipt 日志只含短请求，审计因此退出。完整 SFS 日志补采后，
按两条长请求 ID 复核通过，见 `receipt-pd-store-metadata/model-request-audit.jsonl`。
日志同时显示部分私有快照待清理，尚未证明其长期有界回收。原基线随后健康恢复。

下一轮在相同源码上用仓库 Decode 模板的可选 receipt 分支、16,384 上下文、4,112
chunk 和逐层 range debug，验证三 chunk、实际成功的 Store GET、两条并发请求及回收。
首次分发遇 SSH multiplex broken pipe，在切换前清理了候选。使用 Ansible piped
传输重试，结果见 `receipt-pd-three-chunk/`。该档未纳入默认 inventory。

### 11.54 三 chunk、实际 Store GET、并发与取消（2026-11-05）

使用独立 SSH 配置关闭复用、Ansible piped 传输、每次调用前恢复阻塞 I/O，并改为逐节点
上传校验，候选四端 804 个覆盖文件验证通过。仓库 Decode 模板的 receipt 渲染回归通过，
完整模板测试 63 通过／2 失败，同一测试 harness 在旧模板上复现这两个配置断言。

16,384 上限／4,112 chunk 的 B2 候选完成 8,856-token 三 chunk 冷请求及 8,857-token
同前缀重放，答案 MAPLE／QUARTZ，重放命中 8,832 token。两个请求各观察到 78 层
成功原生 GET、D0 16 rank receipt 和目标释放。两条并发请求正确返回 CEDAR／AMBER，
分别在 D0／D1 收齐 16 rank 并释放。首 token 后断开流的请求亦记录目标释放。
原脚本误将 API 侧断开与 Prometheus abort 计数绑定而退出；镜像源码显示 API 先
pop 输出处理状态再向 EngineCore 发 abort，不能由此计数为零推断未取消。
证据在 `receipt-pd-three-chunk/model-validation.jsonl` 与 `decode-disconnect-trace.txt`。

新增 writer 控制回归：D 在地址查询时拒绝已取消目标后，P 后续有要求的层也不再
登记该请求的 DMA 读者，且不把已取消读者升级为引擎错误。26 项 writer/consumer/delivery
控制用例通过。下一独立候选改用请求级 abort 日志加目标释放证明，并增加先取消 D、
再交付延迟 P 通知的受控实机案例。私有快照待清理告警包含原生 remove 状态码，以便区分
租约占用与对象已不存在。证据在 `receipt-pd-cancellation/`。

地址查询取消候选再次通过 8,855／8,856-token 三 chunk 冷请求与重放，两条请求
均取得 78 层 Store GET、D0 16 rank receipt 和目标释放，重放命中 8,832 token。
并发 CEDAR／AMBER 两条请求均返回正确答案。D1 的释放日志未在脚本的 30 秒
审计窗内被采集，脚本退出后在保留的日志中按请求 ID 复核，两个请求分别在 D1／D0
完成 16 rank receipt 及目标释放。三个取消场景没有在这一轮执行，基线已恢复。

下一候选把并发释放审计延长到 120 秒，并要求两个 Decode 节点各完成一个请求。
该候选用一份仅用于实验的 writer 覆盖文件记录迟到地址查询拒绝，并在 rank 0 第一次
真实 RoCE WRITE 成功后、发出完成证明前加门闩。门闩用于验证 D 在远端写者未证明
静止时保留目标，观察窗口在同步 native WRITE 返回之后。候选证据在
`receipt-pd-inflight/`。

代码审查还发现已有地址缓存的 P writer 无法获知 D 取消。在独立源码改动中，D 在原
proof batch ACK 中反馈当前请求代次的目标已关闭，P 随后跳过排队的 native 写入并在
本地作业静止后回报 terminal，D 仍检查全 rank 静止。保持本地槽位的完成信号独立于
远端 ACK。聚焦 CPU 47 项、镜像内真实 receiver 方法 7 项通过。这个回执改动晚于
`receipt-pd-inflight` 源码快照，需另做模型实机验证。

启动日志确认 P 的 78 个逻辑 Main 层使用 4 个物理槽（2 个共享及首尾独立），
每 rank 23,526 blocks、1,019,904 物理字节／block。D 每个 DP 使用一个 TP
共享的 5.95 GiB Mooncake Host 池，513 blocks；容量与精度配置记录于
`receipt-pd-inflight/startup-kv-layout.txt`。内存验收还需固定容量的普通 HBM
对照，并分别采集 Indexer、selection、staging 和峰值 HBM。

门闩候选短请求、三 chunk、双 D 并发以及 Decode 首 token 后的断连与恢复通过。
断连请求 D0 记录 `Aborted request(s)` 后释放目标，下一条 ASPEN 成功并释放。
直接 D0 取消试验已收到真实目标通知，断开后同一实例被回收，随后 P 处理迟到通知并
完成一个生成 token。脚本在等待拒绝标记时退出，镜像默认只配置 `vllm` logger，
原探针使用的模块 logger 不会输出 INFO。当前证据已覆盖 D 先取消并释放，
P 拒绝地址的日志和门闩用例在下一轮补验。基线已恢复健康。

下一候选 `receipt-pd-cancel-feedback/` 已包含关闭目标的 ACK 以及快照清理改动。
实验标记直接刷新到输出，每项取消独立执行。正式部署模板提供可选的 Decode 请求
日志参数，实机档的可复查渲染配置见
[`p-main-buffer-reuse-receipt.yaml`](../ansible/profiles/p-main-buffer-reuse-receipt.yaml)。

关闭回执候选通过三个实机取消用例。提前取消在 D0 释放目标后交付 5,561-token
请求的旧通知，P0 记录目标查询被拒绝，D0 无 decode receipt，随后 LARCH 正常。
另一请求在 rank 0 的真实 RoCE WRITE 返回后持有门闩，D0 收到 abort 时保持目标，
解除门闩后 P0 的 16 个 TP writer 收到关闭反馈，探针记录 rank 0 跳过后续写入，
D0 随后释放且无 decode receipt，BIRCH 正常。首 token 后断连再次按请求 ID
确认 abort 与目标释放，后续请求继续正常。对应证据为 `model-validation.jsonl`
与 `cancellation-protocol-evidence.txt`。

同一候选的正常路径回归也通过。8,857／8,858-token 三 chunk 冷请求和重放
均有 78 层成功 GET、16 rank receipt 和目标释放，重放命中 8,832 token。
并发各经 D0／D1。持续生成用例用 8,907-token 提示词生成 65 tokens，
正确复述整行，D0 全 16 rank receipt、78 层 GET 和目标释放均有记录。

候选 P0 日志中的私有快照删除拒绝均为 `-706`（对象仍有租约）。
客户端继续保留重试记录，增加一秒重试间隔，避免每个 scheduler step 重发删除；
`-704`（对象已不存在）按完成处理。28 项快照和 Mooncake backend 回归通过。
完整 pool worker suite 的 6 个失败断言和 1 个错误在当前分支 HEAD 的干净
源码快照上同样复现，详细基线保存在 `receipt-pd-benchmark/pool-worker-head-baseline.log`。

在取消候选恢复基线后，监督脚本继续启动固定 513 blocks 的 65,536 上下文 B2/Host
候选。相同容量的无共享/普通 HBM 对照已完成本地渲染与脚本语法检查。长输入脚本
采集实际 token 数、TTFT、生成时长、答案及按节点采样的 HBM 使用量，证据放在
`receipt-pd-benchmark/`。

### 11.55 固定容量长上下文与纯 HBM 对照（2026-11-05）

B2/Host 档在 P0/P1/D0/D1 实际均为 513 blocks，manager block 为 128 tokens。
P 的 78 层 Main 映射到 4 个物理槽（含首尾独立层），每 rank 物理 KV 为
523,210,752 字节。D 容量规划报告每个 DP 的共享 Host Main 约 5.50 GiB，
设备 KV 约 0.21 GiB。GPU/NPU 峰值采样覆盖四节点各 16 张卡。

4,872、31,754 和 62,474-token 三组各跑冷请求、前缀重放、连续生成。九条
答案正确，按请求 ID 核对 D0 各 16 rank receipt 和目标释放。三组重放的
Store 命中分别为 38/38、248/248、488/488 blocks。62,474-token 冷请求
和重放的 TTFT 分别为 130.28 s、37.42 s，各为一次采样。62,477-token
持续生成返回预期序列，生成 29 tokens。最后一次目标释放晚于监督脚本首轮日志
采集，从保留的 SFS 日志复核后全量审计通过。证据在
`receipt-pd-benchmark/host-b2/audit.json`。

本轮监督脚本成功退出并验证原基线健康。随后尝试了一轮关闭共享槽与 Host Main、
仍经旧逐层 P/D 的旁路实验。
B1/B3 保持首尾独立层，Ansible 渲染与 shell 语法已通过。其测试档用门闩
分别扣住一次 Main 或 Indexer 提交，按源代次记录等待和退休，并在 D 核对
回执门禁。门闩和分组件内存日志只存在于实验快照。

本地复现了 writer 停机竞态。最后一个 native 作业结束、源读者完成回调仍在执行时，
旧实现会先停止 proof 线程，导致 terminal 留在队列。现在关闭新任务入口后，有界
等待已接收的作业及其回调入队，再停 proof 线程；未知的 native 状态继续保留。
复现记录为 `receipt-pd-benchmark/proof-shutdown-race-before.txt`，聚焦测试
27 项通过。B1/B3 快照已更新，实机停机测试仍需执行。

旧逐层 P/D 实验的四节点启动日志确认 513 blocks，每 rank Main HBM
5,900,304,384 字节、Indexer HBM 220,631,040 字节。首短请求中 D0 的
TP1–TP15 报告收到 KV，P0 TP0 对 D0 TP0 的第一层和后续层写入连续出现
HIXL `NOTIFY_WAIT` 超时。代理请求 210 秒截止，长输入样本尚未发出。
记录为 `receipt-pd-benchmark/hbm-unshared/legacy-first-transfer-error.txt`。
这轮只用于旧传输路径的故障记录。
设计 §8 的「普通 D HBM 模式回归」继续保留为关闭 DSA offload 的兼容性用例，
与下面的一体推理性能对照分别记录。

B1 在旧逐层候选恢复基线后接续启动。用户明确普通 HBM 对照应使用原始一体推理，
旧逐层的排队复测在分配容器前取消。正确对照位于 `receipt-pd-benchmark/pure-hbm/`。
它保留同一 checkpoint、TP16×DP2、513 blocks、4,112-token chunk、BF16 Main 和
INT8 Indexer，直接请求一体引擎 API，命令不含 KV transfer、Mooncake 或 offload。
监督脚本只切换两台 P 节点的 vLLM 容器，等待 B1 恢复基线后再启动。

B1 启动日志确认 513 blocks、3 个 Main 物理槽，每 P rank 的 Main HBM
为 226,934,784 字节，Indexer HBM 为 220,631,040 字节。D 的 Main HBM
为 0，每个 DP 的共享 Host Main 为 5,900,304,384 字节，每 rank 的设备
selection 为 736,100,352 字节。分组件统计和 staging 探针记录在
`receipt-pd-benchmark/host-b1/startup-kv-layout.txt`。

B1 的 8,853／8,854-token 冷请求及重放通过，分别在 D0 收齐 16 rank 并释放，
重放命中 8,832 tokens。两条并发请求分别经 D0、D1。Main 和 Indexer
各自停在 P0 TP0 的第 1 层 native 提交前，记录同一源代次的槽位等待，D0
保持未发布。解除门闩后记录实际 WRITE 完成和槽位退休，两条请求均正确生成
SAPPHIRE、取得 16 rank receipt 并回收。证据为
`receipt-pd-benchmark/host-b1/component-delay-evidence.txt` 与
`model-validation.jsonl`。两台 D 的已统计设备 staging 均为 336,924–336,956
字节／rank；该数字覆盖探针列出的描述符和计划 staging。

B3 增加一个测试专用的 writer 关闭探针，在 rank 0 最后一批 native WRITE 完成、
源回调入队之前进入关闭。CPU fixture 已验证先等待、后发送 terminal 和停 proof
线程的顺序，真实模型用例留在 B3 的最后执行。纯 HBM 对照先于 B3 运行。

首个纯 HBM 候选的两台一体引擎均健康，启动探针确认每 rank 513 blocks、
Main HBM 5,900,304,384 字节、Indexer HBM 220,631,040 字节。监督脚本在
`docker top -eo args` 的进程核验处遇到 Docker 要求 PID 列的错误，
在发送模型请求前回退。复跑改为读取容器中的实际启动脚本，并用
`docker top -eo pid,args` 补充进程证据，保留独立的 `pure-hbm-retry/`。

纯 HBM 复跑的两台已安装启动脚本均直接运行 vLLM，一体引擎无 KV transfer、
Mooncake、Host offload 或共享槽。32 个 rank 的实际布局均为 513 blocks，
每 rank Main HBM 5,900,304,384 字节、Indexer HBM 220,631,040 字节。
同样的九条请求答案正确，三组回放分别命中本地 4,864、31,744 和 62,464
tokens，全部请求外部命中为 0。按相同提示词和实际 token 数配对结果如下，
每个格子为一次采样。

| 提示词 tokens | 纯 HBM 冷／重放 TTFT (s) | B2/Host 冷／重放 TTFT (s) |
|---:|---:|---:|
| 4,872 | 1.48 / 0.54 | 21.44 / 16.94 |
| 31,754 | 10.20 / 0.68 | 43.17 / 27.14 |
| 62,474 | 20.80 / 0.55 | 130.28 / 37.42 |

62,477-token 连续生成的首个到最后一个输出 token 区间速率分别为纯 HBM
5.35 tokens/s 和 B2/Host 2.05 tokens/s。两台一体引擎节点逐卡采样峰值
分别为 43,355 和 43,006 MiB，B2/Host 的 P 节点为 38,358 和 37,998 MiB，
D 节点为 23,778 和 22,822 MiB。TTFT 差值包含完整架构路径，纯 HBM 重放走
本地 cache，B2/Host 重放走 Store，单组采样无法拆分各项开销。
证据在 `receipt-pd-benchmark/pure-hbm-retry/model-validation.jsonl` 与
`pure-paired-audit.json`。

复跑结束后原基线四引擎和代理健康。D0、D1 与原 Mooncake Master 的容器
ID 和启动时间在切换前后逐项相同，证据在 `pure-hbm-retry/other-services-before.txt`
与 `other-services-after.txt`。监督脚本中的进程参数检查有一处多余的参数，
已修正并对留存的两份容器启动脚本及实际命令独立复核。

### 11.56 B3 与 writer 关闭顺序（2026-11-05）

B3 四节点用 Ansible 独立档启动，P 使用 r05、D 使用 r03，每节点核对 804
个候选源码文件。P0/P1 各 16 rank 均为 513 blocks，78 层 Main 映射到 5 个
物理槽，含 3 个共享槽与首尾独立层。P 的 Main 和 Indexer 分别为
378,224,640 与 220,631,040 字节／rank。两台 D 的 32 个 rank 均无完整 Main
HBM，Host Main、Indexer、selection 和 staging 与 B1 同量。原始分组件数据
在 `receipt-pd-benchmark/host-b3/kv-layout-audit.json`。

8,856／8,857-token 三 chunk 冷请求／重放答案正确，均记录 78 层实际
Store GET，重放命中 8,832 tokens。两个并发请求分别通过 D0 和 D1。
在 P0 TP0 第 1 层分别延迟 Main 和 Indexer 提交，记录对应槽位等待，D0
保持未发布。解除门闩后，先记录真实 WRITE 完成，再记录源代次退休。
最后一条请求在 P0 TP0 的最后一批 native WRITE 成功返回、源回调尚未入队
时调用 writer 关闭，观测到关闭先等待、后排空，D0 正常生成。以上七条
用例各收齐 16 rank 回执并释放目标。证据为 `host-b3/model-validation.jsonl`
与 `host-b3/component-shutdown-evidence.txt`。

此次关闭用例覆盖 native 成功返回后仍有源回调的竞态。同步 DMA 卡死时的进程
退出与传输资源销毁、完整模型无 forward 的自动 restore-only、以及旧逐层
普通 D HBM 路径首传超时仍需单独验收。CPU 聚焦回归 220 项通过，镜像内真实
scheduler 回归 8 项通过。15 个改动 Python 文件均通过 Ruff check 与 format
检查。全仓 `bash format.sh ci` 在隔离副本运行，已有 Ruff 问题仍出现，
并在 typos 阶段达到 240 秒时限。日志在 `final-format-ci.log`。

B3 监督脚本退出码为 0，候选已清理。复核原基线四引擎及代理各返回
HTTP 200，原 Mooncake Master 容器 ID 和启动时间一致。证据为
`host-b3/restored-health-and-master.txt`。

### 11.57 Receipt P/D 首 token 时延定位（2026-11-05 至 2026-11-06）

旧 64K 的 20.505 s 为直连 P 的 B4/P3 客户端 TTFT，Main INT8、chunk
32,784 tokens。§11.55 的 130.28 s 为 BF16 Main、chunk 4,112 的完整
receipt P/D 冷请求。同输入、同 dtype 和 chunk 的纯 HBM 为 20.80 s。

按请求 ID 对齐留存日志，62,474-token 冷请求的 D 目标准备约 35 s，
随后 P Store 查询到 D 全 rank `prepare_decode` 约 92 s。回放分别约
18 s 和 15 s。后段包含计算、Store、跨节点写入与 D 接收准备。
九条请求的阶段审计为 `receipt-pd-benchmark/host-b2/request-phase-audit.json`。
P0 原日志约 3.04 GB，多段抽样重复记录 Mooncake endpoint fallback 告警。

同 B2 配置的诊断档在短 P/D 请求中量得 D 侧 acquire 2.02 s、manifest
读取 0.75 s、两个发布序列化阶段合计约 0.20 s、逐 rank admit 9.16 s。
随后的直连 P 对照因 Store 打开已提交历史失败退出，长样本未完成。
证据在 `receipt-pd-benchmark/host-b2-ttft-trace/candidate-log-tails.txt`。

本地实现让 scheduler 从已验证的完整 manifest 一次性计算各 rank 回执项，
向每个 D worker 仅发送本地 lease 与回执项。worker 按真实注册视图核对完整覆盖，
全 rank 确认后才通知 P。移除 D 的重复 manifest 广播，P 对同一个发送请求复用
已校验的目标描述符。本机 TP16、78 层、62,474-token 合成控制面样本中，16 次
admit 的载荷从 50.79 MB 降到 1.94 MB，串行编解码从 11.04 s 降到 0.28 s。
47 项聚焦测试通过。优化候选在原基线恢复后运行实机 64K 对照。

精简版镜像内的真实 connector 回归通过，确认 P 的两个连续 chunk 只解析一次
远端目标。两个候选的同一 17-token 短请求中，D 的逐 rank admit 从 9.16 s
降至 0.77 s，D 目标准备从约 12.13 s 降至约 3.66 s。精简版首个
62,474-token 冷样本的 D 目标准备约 8.81 s，其中 admit 0.82 s，
acquire 5.17 s、manifest 读取 2.55 s。

镜像里的 glog 库在加载时读取 `GLOG_minloglevel=2`，导入
`mooncake.engine` 后将它重置为 0。仅在候选 P worker 内使用实验探针切换
原生最低日志级别，保留错误与致命日志，按告警开／关／再开记录 TTFT 与日志字节数。

4,112 chunk 精简候选的告警开／关／再开三组冷 TTFT 为
103.24／40.46／98.79 s，回放为 28.49／17.11／27.84 s。P0 每组三请求
日志增量为 2.248 GB／0.925 MB／2.253 GB，三组冷请求均为 62,474 tokens。
P0 TP0 的冷请求 Store GET 层调用均为 1,170 次，累计时间从约 71.2 s
降至 6.79 s，重新开启告警后回到 71.5 s。同步 P→D WRITE 提交区间在该
rank 累计不足 1 s，和 Store 及计算的时间可重叠。九条请求均答对，D0
收齐 16 rank 回执并回收。证据为 `host-b2-ttft-compact/phase-audit.json`。

同一安静候选上的单输出 token 对照（两组 62,473 tokens，独立前缀）
测得 P-only + Store 冷／回放 22.76／1.20 s，完整 P/D 为 39.41／16.66 s。
另在镜像内验证 Mooncake 原生 `MC_LOG_LEVEL=ERROR` 导入后实际生效，
加入独立 receipt profile。32,784 chunk 候选使用此配置，无动态日志探针。

安静的 62,474-token 冷请求在 14:03:05 发布 D 目标，14:03:06 进入
P Store 查询，14:03:32 记录 P TP0 最后一个 WRITE 作业完成，14:03:34
D 全 rank 就绪，客户端 TTFT 40.46 s。TP0 在 16 个 chunk 间交错完成
1,248 个 WRITE 作业，提交区间累计 0.77 s。时间戳精度为秒，末端约两秒
还包含其他 rank 完成、proof 交付与 D 可见性准备。该区间与 D 前置准备
分别计时，避免把控制面或 Store 告警耗时归给 P→D 链路。

正式 `MC_LOG_LEVEL=ERROR` 配合 32,784 chunk 的候选完成三组 62K 冷／重放／
连续生成。冷 TTFT 为 39.72、36.80、35.72 s，重放中位数 17.80 s。
九条均答对并收齐 16 rank 回执与释放，三条冷请求 Store 命中 0/488，
三条重放均为 488/488。冷请求的 P TP0 Store GET 层调用从 4,112 chunk 的
1,170 次降到 78 次，累计约 0.42 s。九条请求 P0 日志合计增长约 0.59 MB，
未出现 endpoint fallback 告警。证据为 `host-b2-ttft-chunk32k/phase-audit.json`。

相同 32,784 chunk 的第二个候选减少 D 的清单复制，并按 group 和容量复用
已完成的 block ID 校验。62K 冷 TTFT 为 37.36、34.29、33.51 s，回放中位数
15.15 s。D 目标准备的冷请求中位数从 8.94 s 降至 6.39 s，清单读取从
约 2.58 s 降至约 0.45 s。九条请求均正确且收齐 16 rank，最后一条长生成
的释放日志未在候选退出前采集。证据在 `host-b2-ttft-d-copy/phase-audit.json`。

该候选第一条 62K 请求在 D 已入队后等约 6 s，等到上一条预热请求的目标释放
才开始准备新目标。P TP0 最后一笔 WRITE 到 D scheduler 确认就绪约 3 s，
最终 `delivery_status` 轮询本身 0.22 s。下一候选保留所有 TP 静止证明，
在不同 D owner 进程并行获取目标，并额外跑逐条等待上一目标回收的对照。

首次并行获取候选的 17-token 请求成功发布两次 D 目标（代理在 HTTP 500 后
重试），第二次目标 acquire 为 0.52 s。P0 TP0 到 D0 的同步 WRITE 与
本地 Store PUT 分别发生 HIXL `NOTIFY_WAIT` 超时，Store 返回 `-800`，
候选退出后原基线恢复健康。长样本未开始，短请求的控制面秒数不计入
端到端性能。原生日志在 `host-b2-ttft-d-parallel/native-error-context.txt`。
复跑用相同源码、全新候选容器，先执行单输出 token 的 P-only Store 热身。

复跑中 4,871-token P-only 冷请求正确返回。随后 P-only 回放在第 2 层
等待 Store GET，P0 TP11 报 HIXL `NOTIFY_WAIT`。此时 D 尚未收到任何模型
请求，故障可独立于 D 并行获取出现。通过结束本地测试客户端触发监督脚本
的既有清理和恢复流程。两轮均未得到有效的并行获取端到端样本，源码回到
已完成三组性能测量的 D 清单复制优化。重建的候选 tar 和逐文件 manifest
与该次实机快照完全一致。复跑证据在
`host-b2-ttft-d-parallel-retry/warmup-live-evidence.txt`。

最终复核原基线四引擎和代理均返回 HTTP 200，四个原 vLLM 容器 ID
与切换前一致，原 Mooncake Master 的 ID 和启动时间一致。证据在
`host-b2-ttft-d-parallel-retry/restored-health-and-master.txt` 与
`restored-container-ids.txt`。保留源码的 79 项聚焦回归和改动文件 Ruff
检查通过，实验日志探针留在候选快照。

### 11.58 TP16 目标并行获取与回收剖析（2026-11-06）

复查前两轮候选的 Ansible 副本，发现它们仍使用 B2 旧 Master 的容器名，
监督脚本却按本轮候选名清理。新一轮为引擎和 Master 使用独立容器名，
Master 监听 52160，保留既有 Master。候选沿用 513 blocks、32,784 chunk、
BF16 Main、INT8 Indexer 和 `MC_LOG_LEVEL=ERROR`，四节点校验 804 个文件。
真实 scheduler 的 8 个控制路径用例通过。候选快照仅新增并行获取和实验计时。

P-only 冷热身正确。完整 P/D 的首个 62K 冷样本为 38.19 s，其中 D 确认
就绪到首 token 约 9.84 s。后两个冷样本为 27.72 和 28.31 s，D 目标准备
2.53 和 2.55 s，获取分别为 0.99 和 1.00 s。旧串行档的冷样本中位数为
34.29 s，目标准备中位数为 6.39 s。两条已热身冷请求从 P Store 查询到
TP0 最后一层 WRITE 均约 21 s，随后约 3 s 达到 D 全 rank 就绪，至首 token
再花约 0.70–1.16 s。阶段边界来自秒级日志。纯 HBM 的 20.80 s 是 4,112
chunk 的单样本，需补同 chunk 对照。三条回放的中位数为 9.23 s，Store 均命中
488/488。九条请求均正确、收齐 16 个 rank 回执并释放。证据为
`receipt-pd-benchmark/host-b2-ttft-d-parallel-isolated/phase-audit.json`。

D 串行 owner 回收在首条样本耗时 7.17 s，后续为 1.73–1.74 s。隔离测试
逐条等待回收，实际连续请求还可能遇到这段排队。下一候选复用 lease 中的
不可变 block ID 元组，并在独立 worker 进程间并行 acquire、admit 和 release。
scheduler 等齐全部结果才发布或回收，失败时保留已成功 rank 的记录，
回收重试只调用尚无成功确认的 rank。

TP16、78 层、489 blocks 的本地剖析中，去除重复 block ID 遍历后，
进程内串行 acquire 的带剖析时间从 2.80 s 降至 1.17 s。独立 16 个 owner
进程、真实 ZMQ 和反向 retention 的三样本中位数中，旧并行候选 acquire／
release 为 0.65／0.76 s，新候选为 0.53／0.32 s。脚本和结果见
`host-b2-ttft-d-parallel-isolated/control-benchmark*.{py,jsonl}`。

监督脚本正常清理候选。恢复 playbook 的本地副本漏链接 `templates/`，
四个原容器已按 ID 拉起，首次 engine 恢复任务在渲染前失败。补齐路径后
对同一组原 ID 重跑 engine、wait、proxy。后续恢复 playbook 一并修正。
复核四引擎与代理恢复，两个既有 Master 的 ID 与启动时间一致。

九条样本中 D 的 TP0 至 TP15 均按 rank 次序完成发布，首末约差 2 s。
P proof sender 之前逐个等待接收 rank ACK。第二候选保留同一 proof 线程
和每个 D rank 的独立 REQ socket，先发送所有接收方，再回收各自 ACK，
同一个 socket 保持按序，且只重试超时或无效 ACK 的接收方。本地真实 ZMQ
对照中 16 个模拟 rank 各有 125 ms ACK 等待，三次中位数从 2.02 s 降至
0.128 s。86 个聚焦测试和改动文件 Ruff 检查通过。第二候选加采集 P 的终结
证明排队／确认和 D 的可见性时间戳。证据在 `host-b2-ttft-d-owner-control/`。

29-token 连续生成的首 token 后耗时约 15–17 s，纯 HBM 单样本为 5.23 s。
本轮 TTFT 优化与这段生成耗时分开记账。后续需拆分 D 的每步保留权 RPC、
owner 本地校验／事件及 Host offload 模型执行。

第二候选在独立 52170 Master 上先热身 P 和完整 P/D，再跑同样三组 62K。
三条冷 TTFT 为 24.84／24.49／24.55 s，中位数 24.55 s，较前轮串行档
34.29 s 少 9.74 s。回放中位数 6.01 s，九条正确、全 16 rank 就绪并释放。
三条冷请求的目标准备中位数 1.68 s，其中 `acquire` 约 0.61 s、`admit`
约 0.35 s。稳态 owner 回收从约 1.74 s 降到 0.18 s。

冷请求从 P Store 查询到末个 P rank 终结回执入队约 21.3–21.6 s，包含 P
计算、逐层 Store 与 P/D 写入。P 最后终结证明入队到全部 D 可见约 47–56 ms，
D 内 16 rank 可见性首末相差 12–16 ms，随后约 0.99–1.15 s 收到首 token。
这轮相对旧 20.80 s HBM 对照仍有 3.75 s 差距。回放中位数 6.01 s 仍含
约 2 s 的前置准备和 2–3 s 的 Store 恢复／逐层推送。三条 29-token 样本的
首 token 后时长为 13.46–15.51 s。计时、回执和阶段分析在
`receipt-pd-benchmark/host-b2-ttft-d-owner-control/{phase-audit,terminal-audit}.json`。
第二轮监督脚本退出码为 0，四个候选和本轮 Master 均已清理，原四引擎
按原 ID 恢复且各返回 HTTP 200，代理健康，两个既有 Master 的 ID 与启动时间
均一致。复核在同目录的 `restored-container-ids.txt` 和 `restored-health.txt`。

同 chunk 纯 HBM 对照已通过 Ansible 渲染检查，两份 P 脚本均为 32,784 chunk、
513 blocks，无 KV transfer、Store 或 sparse KV offload 配置。部署和采样脚本
在 `receipt-pd-benchmark/pure-hbm-chunk32k/`。

### 11.59 同 chunk HBM 对照与 D 每步计时（2026-11-06）

纯 HBM 使用两台原始一体 vLLM P，TP16、513 blocks、32,784 chunk、eager、
BF16 Main／INT8 Indexer。已预热 62K 图和模型，再按同一组 prompt／输出
配对。三条冷请求为 19.77／20.03／20.03 s，中位数 20.03 s。与 §11.58
完整 P/D 的 24.55 s 比，相差 4.52 s。三组均正确，纯 HBM 冷请求前缀命中
为零，回放本地前缀命中 62,464 tokens，外部命中为零。回放 TTFT 中位数
为纯 HBM 0.55 s、完整 P/D 6.01 s。29-token 连续生成在首 token 后的
中位时长分别为 5.46 s 和 13.89 s。配对审计在
`receipt-pd-benchmark/pure-hbm-chunk32k/matched-audit.json`。

下一候选并行查询 16 个 D owner 的就绪状态，并让 acquire 直接使用协调器
持有的注册 catalog 快照。外部读取仍返回副本。本机真实 ZMQ 的 TP16 就绪
查询中位数由 343 ms 降至 165 ms。lease JSON 在同一份描述符内按 block
元组与容量复用范围校验，owner codec 预计算固定白名单的字段和名字查找表，
保持已有 schema、限额、旧版 decode 和完整 lease 校验。本机 TP16、78 层、
62K 的 `lease_to_wire`／`lease_from_wire` 由 276／135 ms 降至 106／58 ms；
manifest 编解码的差值约 16／20 ms。两组 47 项聚焦回归通过。证据在
`receipt-pd-benchmark/host-b2-ttft-d-decode-step/`。

候选另记录全 TP 的 decode 保留权 RPC、注册校验、可见性、模型调用及事件
记录，并在 TP0 记录 fused SFA 的 Host 调用区间。计时使用 Host wall clock，
不额外同步 NPU。用 62K 和约 4.9K 两种生成样本区分固定开销和上下文规模效应。

同 chunk 纯 HBM 监督脚本退出码为 0。原 P 与代理恢复健康，D 两容器和两个
既有 Master 的 ID、启动时间在前后快照一致。证据在 `pure-hbm-chunk32k/`。

D 计时候选的三条 62K 冷 TTFT 为 24.37／23.90／24.03 s，中位数 24.03 s，
相对同 chunk 纯 HBM 差 4.01 s。回放中位数为 5.54 s。九条正确且全 rank
回收。冷请求 D 准备约 1.37 s，最后一次就绪轮询约 92 ms，前轮分别为
1.68 s 和约 210 ms。逐步探针样本的生成时间应与无探针 P/D 单列。

62K 的三个 29-token 样本中，最后完成的 D rank 每步保留权校验中位数分别为
91／159／123 ms，模型调用约 313／287／304 ms，注册校验、事件注入和记录
合计约 1 ms。相邻两个 use scope 间隔约 152–162 ms，仍包含 worker 执行／
采样／调度等未分段时间。TP0 的 78 层 fused SFA Host 调用合计约 48–51 ms，
它包含在模型调用内，不能作为设备核执行时间。4,875-token 样本也完成 29 个
decode step，模型调用约 315 ms、保留权约 104 ms；这轮未观察到长上下文
主导这段延迟的证据。详情在 `host-b2-ttft-d-decode-step/decode-step-audit.json`。

接续候选由 D scheduler 在每次形成 step metadata 时，依据实际 block manager
校验一次所有已调度的保留分配。worker 从同一 step metadata 取得分配，与
owner 的已安装 lease 核对，并在现有 users／stream fence 保护下使用。缺失、
改代或无对应 worker delivery 的证明拒绝执行；无 step metadata 的本地 owner
用法仍走反向校验。release/retire 仍检查静止证明和 guard。61 项本地测试通过；
实机候选记录 scheduler 校验耗时和每个 rank 是否消费了 step proof。

实机九条 62K 请求均正确，16 rank 就绪并回收。冷／回放 TTFT 中位数为
24.00／5.36 s，冷请求仍比同 chunk 纯 HBM 慢 3.97 s。全 TP 审计了三条
62K 和一条 4.9K 的 29-step 生成，共 1,856 次 worker 使用记录，均取得
当前 step 证明。D scheduler 的 138 次校验中位数为 43 µs，最后完成的 rank
每步本地核对约 1 µs。相同 Host 探针下，62K 的 29-token 首 token 后中位
时长由 15.40 s 降为 12.87 s，纯 HBM 为 5.46 s。模型调用仍约 300–315 ms，
相邻 use scope 间隔为 145–162 ms，4.9K 也落在同一量级。详情见
`receipt-pd-benchmark/host-b2-ttft-d-step-proof/` 的三份审计 JSON。

冷请求的 D 目标准备中位数为 1.36 s，其中约 0.45 s 计在 `manifest_rpc`。
该调用经 D scheduler 进程内的 `DispatcherOwnerTransport`，包含清单快照复制
及双边值检查。冷请求 P 最早与最晚 rank 发终结回执相差约 0.32 s，D 全 rank
可见之后到首个内容约 0.86–0.92 s。各区间按原有 Host 时间戳估计，涉及
重叠的区间不能直接加总。

D 的实机日志确认同步 `ReceiptScheduler` 触发了 vLLM 的异步调度关闭提示。
已单独渲染 D 侧 `AsyncReceiptScheduler` A/B，继承上游 `AsyncScheduler` 的
token 占位，并复用现有 receipt 的保留与回收门禁。实验源包和生成脚本在
`receipt-pd-benchmark/host-b2-ttft-d-async-scheduler/`。

同步候选监督脚本退出码为 0，四个原引擎和代理恢复健康，两个既有 Master
的 ID 和启动时间一致。异步候选用同组九条 62K 请求全部通过正确性及全 rank
回收审计，冷／回放 TTFT 中位数为 23.98／5.28 s，首 token 后的 29-token
样本为 8.44／9.05／8.91 s，中位数较同步的 12.87 s 下降约 31%。4.9K
生成也通过。异步探针记录 30 个提交 step，客户端仍收到 29 个生成 token。
探针用 wall clock 记录 scope 入口，异步下相邻 scope 的边界差可能为负，
不能用同步候选的步间算法估算异步的等待时长。异步实现仍留在实验快照。
异步候选监督脚本退出码为 0，原四引擎和代理恢复健康，两个既有 Master
的 ID 与启动时间一致。同步优化与源码交接已签署提交 `8c2d81c40`。

### 11.60 D 本地目标发布和异步配置（2026-11-06）

将已实测的 `AsyncReceiptScheduler` 接入源码和 receipt 的 Ansible profile，
保留显式同步选项。真实 scheduler 测试覆盖上游异步 token 占位、已发 GPU step
未处理时的延迟释放，以及处理完成后的 receipt 回收。

D scheduler 通过进程内协调器直接用保有的 rank 清单构建 HTTP 值并登记，
省去返回清单的防御性复制及本地 dispatcher/client 对同一对象的重复遍历。
生成的 HTTP 值与原方案一致。仍先校验 lease、sender 数、写覆盖，并等全部
owner 登记成功才交给 metaserver。注入 transport 的适配入口继续走原方法。
TP16、78 层、62,474 tokens 的本机描述符测试比较了发布阶段，不含真实
传输。21 项聚焦回归和 receipt 模板用例通过。模板全量另有两项旧档失败，
已在旧模板复现，分别是 `1m-v030` 长度断言与 `1m-v030-lw` 共享参数断言。
证据保存在 `receipt-pd-benchmark/host-b2-ttft-local-publication/`。

本机五组交错对照的发布加登记中位数为 1.014／0.446 s。A3 四节点 804 个
源码哈希一致，容器内真实 scheduler 的九个生命周期用例通过。与 §11.59 的
同异步调度候选对照，三条 62K 冷请求为 23.57／23.18／23.31 s。中位数从
23.98 s 降至 23.31 s，较同 chunk 一体纯 HBM 20.03 s 仍差 3.28 s。
D 冷目标准备中位数从 1.364 s 降到 0.749 s，acquire 仍约 0.38 s。候选
trace 中 `validate_wire` 对应 scheduler 本地 P/D 检查，`admit` 包含清单 HTTP
编码和全 rank 登记，故以准备总时间作对照。三个冷请求均为 0/488 Store 命中。

三条 62K 回放命中 488/488 Store blocks，TTFT 中位数从 5.28 s 降至 4.67 s。
首 token 后的 29-token 输出中位数为 8.32 s，前轮异步候选为 8.91 s；发布
阶段在首 token 之前，生成时长差异只作为样本观测。九条长样本全 rank 就绪
并回收，4.9K 生成也通过。生成审计覆盖四条样本、每条 30 个异步提交 step，
全部 worker step 均使用保留分配证明。

监督脚本退出码为 0，候选容器与独立 Master 已清理。四个原引擎和代理
恢复健康，两个既有 Master 的 ID 及启动时间与切换前一致。
本轮生产源码和回归测试见 `vllm-ascend-reuse` 的签署提交 `c76f087d2`。

### 11.61 新镜像部署到 a3-reuse（2026-11-06）

P0 使用 r05 ARM64 底座构建 `glm52-vllm-ascend:0.30.0-receipt-c76f087d2`。
镜像覆盖 `c76f087d2` 的 804 个 `vllm_ascend` 包文件及 layerwise 代理，
镜像内清单逐文件校验通过。独立无 NPU 容器通过 9 项 scheduler 生命周期用例。
四节点加载同一镜像 ID `sha256:1f7fb2c68732a1af9c10ec6ed52aed7f244024ce8b755193f96288224b80ba28`。

`a3-reuse` 采用 65,536 上下文、513 blocks、P chunk 32,784、BF16 Main、
INT8 Indexer、D Host offload 和 `AsyncReceiptScheduler`。P/D 和代理端口保留
9081／9900／8000。新的独立 Master 监听 52210。Ansible 停止并保留旧四个引擎
容器，新四个引擎和代理均就绪，两个既有 Master 的容器 ID 与启动时间一致。

约 4.9K 和 62K 各跑冷请求、前缀回放及 29-token 连续生成，六条结果正确，
D0 每条均有 16 rank 就绪及目标回收记录，D1 通过健康检查。服务保持运行。
镜像 tar 位于共享盘 `build/images/0.30.0-receipt-c76f087d2.tar`，构建、
部署和请求证据在 `artifacts/p-main-buffer-reuse/image-receipt-c76f087d2/`。

### 11.62 55K 输入／700 输出的并发配置（2026-11-06）

§11.61 的单请求验收档将每个引擎限制为一条序列和 513 blocks。首轮压测在
并发 20 时，每台 D 实际运行 1 条、等待 9 条。这轮已停止，数据不计入并发结果。

`a3-reuse` 改用每 P 64 序列、每 D 32 序列，并移除固定 block 数。上下文仍为
65,536，P/D 的每步 token 预算分别为 32,784 和 192。D 使用异步 receipt、Host KV
和每引擎 192 GiB 预算。四个引擎沿用 `c76f087d2` 镜像。Ansible 用独立容器切换，
保留上一套容器及原 Mooncake Master。实测 KV 容量为 P0 2,884,864、P1 2,884,736，
两台 D 各 2,097,280 tokens。每台 D 的 16,385 blocks 受调度上限约束，Host 计划占用
175.51 GiB，另有 0.46 GiB 对齐预留。

192 GiB 档的两台 D 均在启动时失败。共享 Host 池完成分配，HIXL 随后对
63.11 GiB 的单个 NPU 映射区调用 `HcommMemReg` 失败，原生错误为 503900，
两个引擎均未就绪。上一套单请求档的同路径成功注册过 5.79 GiB 的区间。
这两条数据无法确定单区上限或每卡累计上限。完整输出见
`artifacts/p-main-buffer-reuse/receipt-concurrency-c76f087d2/`。

40 GiB 档沿用原镜像和 P64/D32。两台 D 均规划 3,691 blocks、472,448 KV
tokens 和 39.54 GiB Host KV，按容量可放约 8 条 55K 请求。实际注册区大小为
42,529,898,496 字节。D0 的 16 个 rank 中 8 个成功，D1 有 7 个成功。两台机器
的 8 张物理卡均各有两个 chip，成功的卡都只有一个 chip 的 rank 注册通过。
现象提示同一物理卡的两个 chip 可能争用注册资源，具体配额仍需单独验证。
两台 D 均未就绪，端到端探针和压测未发出。证据见
`artifacts/p-main-buffer-reuse/receipt-host40-c76f087d2/`。
Ansible 已按原容器 ID 恢复单请求配置，四引擎和代理健康。通过代理的
4,871-token 检查返回预期答案，D0 的 16 个 rank 均完成准备并回收目标。
三个 Mooncake Master 的容器 ID 与启动时间一致。

### 11.63 D Host KV 26 GiB 与共享内存大页（2026-11-06）

26 GiB 档保留 P64/D32，D0/D1 各自的 16 个 rank 均成功注册
27,643,871,232 字节的 Host KV 映射区。每台 D 有 2,384 blocks、305,152 KV
tokens。代理的 55,273-token 冷请求正确返回，首 token 约 41.54 s，随后完成目标
回收。抽查两台 D 的共享内存映射时 `ShmemPmdMapped` 为 0，Docker 的
`/dev/shm` 为私有 tmpfs。

在临时容器内对 `/dev/shm` 设 `huge=always` 后，两台 D 在各执行一次
`drop_caches=1` 前后均完整映射了 40 GiB 透明大页，`thp_file_fallback` 未增加。
该对照只运行 Linux mmap 和触页。原始记录见
`artifacts/p-main-buffer-reuse/receipt-host40-drop1-20261106/`。

### 11.64 D Host KV 256 GiB 与容器透明大页（2026-11-06）

使用 `c76f087d2`，仅替换 D0/D1 容器。D 的 `max_num_seqs` 设为 48，
使调度容量覆盖 256 GiB Host 预算。在 worker 启动前执行
`mount -o remount,huge=always /dev/shm`。宿主机 HugeTLB 预留为 0。
两台 D 的 Host 池创建进程均有 268,433,408 KiB 的共享映射，全部计入
`ShmemPmdMapped`。

每台 D 的实际规划是 23,856 blocks、3,053,568 KV tokens。每台 D 的
16 个 rank 均通过 HIXL，单 rank 的 5 个 Host 区间合计 274,640,666,624
字节。四个引擎和代理均健康，55,273-token 冷请求正确返回并完成目标回收，
首 token 31.39 s。候选 D 保持运行，P/代理和三个 Mooncake Master 的容器 ID
与启动时间通过对比。证据在
`artifacts/p-main-buffer-reuse/receipt-host256-thp-20261106/`。

HIXL 定位手册的 MR 失败场景列出相同的 `-13 / 128102` 错误，建议为大容量
Host 区间使用 mmap 和预留大页。本轮实测的是容器 tmpfs 透明大页。

### 11.65 55K 输入／700 输出压测及 P 调度观测（2026-11-06）

在 §11.64 的服务上运行 benchmark-tool v1.7。每档计划 40 条请求。
并发 20 的 40 条均返回完整 SSE 和实际 700 个输出 token。服务端输入范围为
53,988–55,006 tokens，耗时 762.13 s，输出吞吐 36.74 token/s，
TTFT P50/P99 为 50.98/210.54 s。首批 20 条 TTFT 为 29.44–211.12 s；
其余 20 条随完成陆续提交，TTFT 为 21.35–56.84 s。

应用户要求停止客户端。并发 30 留有 21 条完整结果，未形成整档统计，并发 40
未开始。停测后两台 D 的运行与等待请求清零，代理健康。

15 s 的 Prometheus 采样和 vLLM 自己的 10 s 定时日志均观测到每台 P
running 峰值为 2，KV 占用峰值约 3%。P 每步 32,784 token 的预算先用于
running 请求，等待请求只使用剩余预算。每台 P 的 `max_num_seqs=64` 为
调度上限。采样在 API 进程读取已发布的计数，未做开关采样的时延对照。
原始日志在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/host256-thp/`。

### 11.66 原始块级 P/D 性能对照（2026-11-06）

从 `67397b9` 渲染 `1m-v030`，使用原版 `quay.io/ascend/vllm-ascend:v0.30.0rc1-a3`
和同一份 W4A8C8 权重。P 为 TP16×DP2，D 为 TP8×DP4；两侧保留原档 INT8 KV、
DCP 和 MTP，使用块级 `MooncakeConnectorV1` 直传。禁用 Store 池化，并用独立容器和
日志目录。采用 v1.7、同一随机种子、每档 40 条、55K 输入和 700 输出。首轮额外的
语义探针触发了提前回退；正式轮在服务健康后直接发压测，只按 SSE 与 usage 统计。

| 客户端并发 | 完整 700-token 响应 | 整档耗时（s） | 输出（token/s） | TTFT P50/P99（s） | TPOT P50（ms） |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 40/40 | 306.09 | 91.48 | 8.49 / 49.33 | 44.11 |
| 20 | 40/40 | 225.07 | 124.40 | 33.26 / 70.29 | 37.62 |
| 30 | 40/40 | 226.90 | 123.40 | 39.72 / 86.64 | 158.72 |
| 40 | 40/40 | 214.66 | 130.44 | 60.09 / 110.89 | 108.65 |

并发 20 有一条含 `[DONE]`、`finish=length` 和 700 个输出 token 的响应未出现可见正文，
因此 TTFT／TPOT 使用其余 39 条；usage 计数的总输出吞吐纳入全部 40 条。
同并发的 §11.65 receipt 档为 36.74 token/s、TTFT P50/P99 50.98/210.54 s。
原始档每台 P 采样 running 峰值也为 2。两档的 D 拓扑、MTP、图模式、DCP、KV 精度
和 chunk 同时不同。证据在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/original-v030-performance/`。

并发 10 在同一原始配置重启后单独运行，沿用随机种子和请求参数，服务端实际输入
53,988–55,006 tokens，端到端 P50 为 51.35 s。两台 P 的等待队列采样峰值各为 3，
并发 20 时各为 8。并发 10 的原始记录在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/original-v030-c10/`。

### 11.67 并发 10 的原始档与复用档对照（2026-11-06）

复用档使用 `c76f087d2`，P 开启 Main reuse，两台 D 各配置 256 GiB Host KV。
两档服务端逐条输入 token 数一致，范围为 53,988–55,006。40 条均完整返回
700 个输出 token。原始档 306.09 s、91.48 token/s，复用档 1,175.05 s、
23.83 token/s。TPOT P50 分别为 44.11 和 330.75 ms。

| TTFT 口径 | 原始档（s） | 复用档（s） |
| --- | ---: | ---: |
| 40 条 P50 | 8.49 | 37.43 |
| 40 条 P99 | 49.33 | 110.41 |
| 首发 10 条 P50 | 34.48 | 78.16 |
| 后续 30 条 P50 | 7.82 | 32.54 |

复用档第一条完成请求的 TTFT 为 27.21 s。D 在 13:46:55 开始通知代理，
13:46:56 发布目标，P 在 13:46:57 完成 Store 查询（0/429），D 的 16 个
rank 在 13:47:20 完成 `prepare_decode`，13:47:21 报告 KV 接收完成。
首批 10 条从 D 发布到 P 查询完成为中位 27 s，P 查询完成到 D 全 rank
接收完成为中位 39 s。后续 30 条对应 4 s 和 25.5 s。秒级日志只区分
这些边界，后者包含预填充、逐层传输和接收确认。D 目标发布前阶段的中位数
约为 1 s。复用档 P 的本地及 Store 前缀命中均为零，原始档 P 的定时日志
也报告零命中。原始档 D 的块级直传日志中，640 次单 rank 传输中位 13.50 ms。
两档的 P chunk、KV 格式及 DCP，以及 D 拓扑、图模式和 MTP 同时不同。

首次恢复时 P1 的 HIXL 端口 21000 绑定失败。重启 P 后，首轮压测出现 P0
TP9 KV 写入失败，伴随 HIXL `NOTIFY_WAIT` 超时，停止客户端时 0 条完成。
完整重启 P/D 后同配置复跑成功，压测后四引擎与代理健康，Mooncake Master
身份及启动时间一致。日志分别在 `host256-thp-c10/` 和
`host256-thp-c10-retry/`，均位于上述 benchmark-tool 实验目录下。

### 11.68 并发 10 的同执行配置 HBM 对照（2026-11-06）

独立 Ansible 档使用同一 `c76f087d2` 镜像、W4A8C8 权重、P/D TP16×DP2、
BF16 Main、INT8 Indexer、CP/DCP=1、eager、无 MTP、异步 D 调度和 65,536 上下文。
P/D token budget 保持 32,784/192，序列上限保持 64/48。切到块级直传和普通
HBM KV 后，取消 P Store/共享槽、D Host/receipt 及对应 scheduler，代理切为 P-first。
两台 D 的实际 HBM 容量各 437,888 tokens，P 为 246,528。整轮零抢占，D 的
external hit 共 2,197,493 tokens，与 40 条实际输入合计一致。三档逐条输入
token 数一致，均完成 40 条、每条输出 700 tokens。

| 指标 | 原始 INT8 档 | 同配置 HBM 直传 | 复用 + Host 256 GiB |
| --- | ---: | ---: | ---: |
| TTFT P50（s） | 8.49 | 38.83 | 37.43 |
| 首批 10 条 TTFT P50（s） | 34.48 | 85.29 | 78.16 |
| 后续 30 条 TTFT P50（s） | 7.82 | 35.14 | 32.54 |
| 每条首 token 后平均时间（s） | 51.10 | 156.49 | 232.23 |
| TPOT P50（ms） | 44.11 | 220.55 | 330.75 |
| 整档耗时（s） | 306.09 | 880.60 | 1,175.05 |
| 输出吞吐（token/s） | 91.48 | 31.80 | 23.83 |

原始档到复用档每条平均多 209.43 s，其中首 token 后多 181.14 s，占 86.5%。
同配置 HBM 换到 Host receipt 后，TPOT P50 多 110.20 ms。用 P 空闲、两台 D
各运行 5 条、窗口两端无 D 等待且 P 完成计数相同的 15 s 采样估算，HBM 和
Host receipt 的吞吐中位数为 46.67 和 31.00 token/s，对应约 214 和 323 ms/步，
分别有 6 和 26 个采样窗口。上次同源码单请求审计的 78 层 model submit 中位约
304 ms，owner 本地校验、可见性和 fence 合计约 1 ms。D Host 路径在每个 decode
step 为各层准备 TopK 计划、用 fused SFA 取 Host Main KV，并把本步新 KV 写回 Host。
当前并发采样定位到这条解码路径的净增量，设备搬运、算子和调度的份额未单独计时。

原始档 D 的 MTP 日志合计 67,340 个 draft token、接受 14,584 个，按每步 5 个
draft 折合每步约产出 2.08 个 token。当前 D receipt 注册要求 eager、CP/DCP=1
和无 MTP。原始档到同配置 HBM 的 574.52 s 差值同时包含镜像、D 拓扑、图模式、
MTP、KV 格式和 chunk。原始 P 启用了 DSA-CP、DCP16 和 8,192 chunk，对照 P
使用普通 SFA、DCP1 和 32,784 chunk。HBM 和 Host receipt 的 TTFT 接近，且 HBM 直传
的 640 次单 rank 搬运中位数为 337.81 ms。P 端调度到产出首 token 的均值为
HBM 32.11 s、复用 27.99 s。并发 10 下，换回 HBM 后首 token 差距依然存在，
对照定位到配置组合，DSA-CP、DCP、chunk 等各项的影响需要单独对照。

原始数据和复算在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/matched-hbm-eager-c10/`。
压测后按原容器 ID 恢复复用档，四个引擎和代理通过健康检查，Mooncake Master
的容器 ID 和启动时间与切换前核验一致。

### 11.69 仅 P 开启 DSA-CP 的并发 10 对照（2026-11-06）

沿用 `c76f087d2`、P Main 复用、BF16 Main、INT8 Indexer、DCP1 和 32,784
chunk。Ansible 的 P 渲染产物仅增加 `enable_dsa_cp: true`，两个 D 沿用
原容器。DSA-CP 在 TP 组内切分 query，写回前收齐 KV，继承 SFA 的逐层
等待和保存入口。启动后每台 P 的逻辑 KV 容量约 246 万 tokens，原档约 288 万。

同 seed 的 40 条请求逐条实际输入长度一致，均完成 700 个输出 token，P 的
本地、Store 前缀命中及抢占均为零。仅在客户端记录首次非空 SSE 内容的时间。

| TTFT（s） | 原始 INT8 档 | 原复用档 | 复用 + P DSA-CP |
| --- | ---: | ---: | ---: |
| 全部 P50 | 8.49 | 37.43 | 10.54 |
| 全部 P99 | 49.33 | 110.41 | 49.44 |
| 首发 10 条 P50 | 34.48 | 78.16 | 40.93 |
| 后续 30 条 P50 | 7.82 | 32.54 | 10.24 |

P 端调度后到首 token 的均值由 27.99 s 降到 7.80 s。首批 P 查完 Store 到
D 全 rank 收齐 KV 的中位数由 39 s 降到 12.5 s，后续由 25.5 s 降到 5 s。
开启后首批 D 发布目标到 P 查完 Store 中位 24 s，后续约 2 s。后续请求的
D 目标准备中位约 1 s，接收完成到首内容约 1.68 s。阶段日志精度为秒，
P 到 D 的区间包含可重叠的计算和逐层传输。首批 P 阶段也受冷链路影响。

数据在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/receipt-dsa-cp-c10/`。

### 11.70 DSA-CP 下扩大 P 每步 token 预算（2026-11-06 至 2026-11-07）

两个 P 的渲染脚本仅将 `max_num_batched_tokens` 从 32,784 提到 57,344，
保留 DSA-CP、Main 复用与 chunked prefill。两个 D 沿用同一组运行中的实例。
两档与原始 INT8 档逐条实际输入 token 数一致，每条完整输出 700 tokens。

| TTFT（s） | 原始 INT8 档 | DSA-CP 32,784 | DSA-CP 57,344 |
| --- | ---: | ---: | ---: |
| 全部 P50 | 8.49 | 10.54 | 10.71 |
| 全部 P99 | 49.33 | 49.44 | 47.41 |
| 首发 10 条 P50 | 34.48 | 40.93 | 36.07 |
| 后续 30 条 P50 | 7.82 | 10.24 | 9.28 |

P 每台逻辑容量由约 246 万降到 235 万 tokens。按每个请求独立的 Store
snapshot 代次，32,784 档的 40 条分别有 34 条两代、6 条三代，57,344 档
有 30 条单代、10 条两代。首批 D 发布目标到 P 查完 Store 的中位数由
24 s 降到 6 s，P 查完到 D 收齐由 12.5 s 增到 20 s。后续 30 条的这两段
均为 2 s 和 5 s 左右。两档各测一轮，D 在第二轮沿用已热实例。

证据在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/receipt-dsa-cp-chunk56k-c10/`。
测试结束按原 ID 恢复两个 P，四引擎和代理健康，D 与 Mooncake Master 的
容器 ID 和启动时间均通过前后比对。

### 11.71 GLM 单层 MTP 接入 Host receipt（2026-11-07）

P 保留 57,344 token budget 和 DSA-CP，为第 79 个逻辑层单独分配 Main 槽，
使模型层 `-1` 仍指向第 78 层。D 把 draft KV 纳入现有 receipt 和 Host
KV 生命周期。draft forward 在 `sample_tokens` 单独登记实际的设备流尾部，
覆盖主模型 forward 结束后的 KV 访问。测试容器用 `c76f087d2` 镜像叠加
Python 补丁，最终验证的补丁已提交到 `src/vllm-ascend-reuse` 的
`993713305`。首轮 P/D 均启用 MTP1。D 仍用 eager、TP16×DP2、BF16 Main、
INT8 Indexer 和 256 GiB Host 预算。

首轮四引擎启动成功，两个 P 各有 79 个逻辑层、5 个 Main 槽，两个 D
的 KV 容量各约 181 万 token。10 条 55K 输入、32 token 输出的随机请求
中，D0 完整返回第一条并累计接受 15/18 个 draft token。其余九条未完成。
P1 TP3 和 TP11 的 HIXL `HixlBatchPut` 报 `NOTIFY_WAIT` 超时，D 有
多条 receipt 等待 300 s 后超时。无 MTP 的复用档也曾遇到这类传输超时，
见 §11.67。首轮只有一个完整样本，需用完整并发结果对比 TPOT。

首轮结束按原 ID 恢复四个引擎和代理，健康检查通过，Mooncake Master
身份和启动时间核验一致。证据位于
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/receipt-mtp1-probe/`。

MTP1 同档重启后四引擎和代理均健康，D 仍能分配约 181 万 token。新客户端
漏带模型和驱动目录的只读挂载，在加载 tokenizer 时退出，服务指标确认未
收到测试请求。后续客户端按首轮挂载重建，离线 tokenizer 与 benchmark
入口均已通过预检。

块尾的 MTP KV 依赖下一 token，而原公共块键只包含当前块及其前缀。MTP
候选禁用 P 的跨请求前缀缓存，完整块和部分块都使用请求内快照，同一请求
跨 chunk 恢复按原版本和租约处理。无 MTP 的档继续使用原公共块键。
D MTP5 第一次启动配置为 48 条序列、每步 96 token，两台 D 都在分配
TopK buffer 时 OOM。MTP1 和 MTP5 的每行容量分别为 4,096 和 12,288，
行数上限同为 96 时，单层申请由约 0.42 GiB 增至 1.27 GiB。只将序列
上限降到 16 仍允许 96 行。随后只重启候选 D，设为 16 条序列、每步
32 token，将预分配量恢复到 MTP1 的约 33.33 GiB。该档先跑并发 10 的
32 token 探针，再跑 40 条 55K 输入、700 token 输出的正式压测。

两台 D 在 16/32 档启动成功，实测各有 1,048,704 token KV 容量。D0
完整返回两条 55K 请求，记录 27 次投机、135 个 draft token、接受 41 个。
随后 P1 的跨 chunk 路径在 `save_kv_layer` 访问第 79 层的
`_compute_done_events` 时越界。连接器注册后更新了 `total_layers`，
事件列表仍按构造时的 78 层分配。改为每个生产者 step 按注册后的层数
初始化事件和本步无计算标记。离线复现旧实现的索引错误，并验证修复后的
第 79 层可以写入。已中止该轮短探针，未启动正式压测。证据位于
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/receipt-mtp5-private-c10/`。

修复后保持 P DSA-CP、57,344 token budget、P MTP1 与 D MTP5 16/32。
并发 10 的短探针 10/10，D0、D1 各生成 305 个 draft token，分别接受
109、104 个。正式压测 40/40，每条输出 700 token，三档按提交顺序的实际
输入长度一致，范围为 53,988–55,006。正式压测 D0 完成 19 条，D1 完成
21 条，短探针和正式压测的 50 条请求均通过各自 D 的全 16 TP receipt
及目标释放检查。

| 并发 10 指标 | 原始 INT8 档 | P 优化、D 无 MTP | P 优化、D MTP5 |
| --- | ---: | ---: | ---: |
| TTFT P50/P99（s） | 8.49 / 49.33 | 10.71 / 47.41 | 8.97 / 31.72 |
| 后续 30 条 TTFT P50（s） | 7.82 | 9.28 | 8.86 |
| TPOT P50/P99（ms） | 44.11 / 149.66 | 349.91 / 364.37 | 112.80 / 188.11 |
| 输出吞吐（token/s） | 91.48 | 26.28 | 66.15 |
| 耗时（s） | 306.09 | 1,065.28 | 423.29 |

MTP5 正式压测接在短探针后，首批 TTFT 处于热链路条件。正式阶段两台 D
合计投机 7,864 次，生成 39,320 个 draft token，接受 20,222 个，按计数
折合每次投机推进约 3.57 token。P 使用请求内快照，D 每台 KV 容量为
1,048,704 token。D 16 条序列、每步 32 token 是本次并发 10 的验证配置。
数据在
`artifacts/benchmark-tool-v1.7/run-55k-700-c20-30-40-20261106/receipt-mtp5-fence-c10/`。
压测后停止候选，原四个容器 ID 和启动脚本逐一核验一致，四个引擎与代理
健康。Mooncake Master 的容器 ID 和启动时间与切换前一致。

### 11.72 三档并发 10/20 复测（2026-11-07）

同一份 GLM-5.2 W4A8C8 权重和 v1.7 `performance-test` 客户端，随机种子
20261106。每档用新引擎先跑并发 10，再复用引擎跑并发 20。每组 40 条，
实际输入逐条长度在六组之间一致，为 53,988–55,006 token。六组均完整返回
40×700 token，按 SSE 和 usage 统计。

| 档位 | P / D 拓扑 | P / D DCP | Main / Indexer | KV 路径 |
| --- | --- | --- | --- | --- |
| 原始 v0.30 | TP16×DP2 / TP8×DP4 | 16 / 8 | INT8 / INT8 | 原版 `1m-v030`，块级直传 |
| `1m(dcp)` | TP16×DP2 / TP4×DP8 | 16 / 4 | BF16 / INT8 | `1m` 兼容档，块级直传 |
| Main reuse + Host MTP5 | TP16×DP2 / TP16×DP2 | 1 / 1 | BF16 / INT8 | P 复用、D Host receipt |

前两档共用原版 v0.30.0rc1 镜像，关闭 Store 池化，最大上下文均为
1,048,576。原始档的实际启动脚本与 §11.66 对照一致。`1m(dcp)` 按旧
`1m` 拓扑渲染，将 Indexer C8 参数迁移为显式 INT8。八个 D 实例各有
1,476,608 token KV 容量。我们的档使用 `c76f087d2` 镜像叠加
`993713305` 的已验证源码，最大上下文 65,536。P 开 DSA-CP、MTP1 和
57,344 token budget，D 开 MTP5、Host 256 GiB，每实例 16 条序列、
每步 32 token。D 实测每实例 1,048,704 token KV 容量。

| 档位 | 并发 | 完成 | TTFT P50 / P99（s） | TPOT P50 / P99（ms） | 输出吞吐（token/s） | 耗时（s） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 原始 v0.30 | 10 | 40/40 | 8.03 / 52.23 | 42.32 / 153.48 | 98.90 | 283.12 |
| 原始 v0.30 | 20 | 40/40 | 14.14 / 63.23 | 132.94 / 181.27 | 107.80 | 259.74 |
| `1m(dcp)` | 10 | 40/40 | 11.85 / 58.14 | 45.03 / 56.29 | 130.27 | 214.94 |
| `1m(dcp)` | 20 | 40/40 | 28.16 / 66.98 | 48.78 / 68.75 | 175.88 | 159.20 |
| Main reuse + Host MTP5 | 10 | 40/40 | 9.00 / 54.49 | 123.01 / 177.33 | 61.27 | 457.01 |
| Main reuse + Host MTP5 | 20 | 40/40 | 86.31 / 146.68 | 116.44 / 180.76 | 65.37 | 428.30 |

我们的档在并发 10 的首批和后续 TTFT P50 分别为 49.48 s 和 8.39 s，
原始档为 34.71 s 和 7.34 s。并发 20 时，两台 D 合计 running 峰值仍为
10。P 空闲的多个采样点中，D0/D1 各有 5 条 running、5 条 waiting，
后续 20 条 TTFT P50 为 91.01 s。每台 D 的 32 token 预算只能容纳五条
请求同时执行完整的六位置验证。该档的 80 条请求均逐条核对了各自 D 的
16 个 TP rank receipt 及目标释放。

原始档上轮并发 20 从冷引擎开始，本轮接在并发 10 后。两轮采样的 D
running 峰值分别为 13 和 20，按投机日志估算，每步平均推进约 3.1 和
1.6 token。这两个差异均影响 TPOT，记录只用于比较这两次实测。

可复算数据、实际启动脚本及各档采样位于
`artifacts/benchmark-tool-v1.7/retest-55k-700-c10-c20-20261107/`。
测试结束后，原四个容器及启动脚本逐一核验一致，四引擎与代理均健康。
三轮前后的 Mooncake Master 容器 ID 和启动时间核验一致。

### 11.73 参考 DSA Offload 指南的新 layerwise 拓扑（2026-11-07，已完成）

以 sig-ai-inference 指南固定提交
`b4965c0625c9f89ffd6c8f61dee6f8ed2f62cc8b` 的脚本与参数为参考，
创建独立 inventory `ansible/inventories/a3-layerwise-guide/` 和
`ansible/dsa-layerwise/` playbooks。开发源码仍为
`p-main-buffer-reuse@993713305`，其与 `maz/dsa_offload_rebase_30`
的 merge-base 为 `4234a1f18`。

保留 P MultiConnector、Main reuse、逐层 Store 与 PD SourceReady，D
Mooncake Host、AsyncReceiptScheduler、全 TP receipt 及 D-first 代理。
当前 registered target owner 要求 eager，SourceReady writer 要求等 TP 和
CP/DCP1，因此采用 P/D 各 TP8×DP4，每节点两个显式隔离设备的外部 DP
实例。每个 D 实例的 owner RPC/retention 端口独立。P 开 enable_dsa_cp，
其余主要参数为 1M 上下文、16K chunk、P/D MTP1/3、D 8 seq/32 token、
Host 100 GiB/实例、TopK4096、LRU16，P/D utilization 0.75/0.88。
完整差异见 `ansible/dsa-layerwise/README.md`。

首轮镜像基于已验证的 `c76f087d2` 镜像，保持 CANN9.1、
Mooncake0.3.13.post1 和已有编译算子，复制开发提交完整源码与代理，
校验 807 个源文件。镜像
`glm52-vllm-ascend:0.30.0-layerwise-guide-993713305` 的 ID 为
`sha256:1f324cd039ed7a7388dd41484aa57b99b565048289c40485363a2803dc3e52d3`。
构建日志与镜像导出在
`/mnt/sfs_turbo/g00832294/layerwise-guide/build/993713305/`。
本轮 CANN9.2/pr3952 的兼容性尚待验证。

gen 的四节点 shell 语法检查和 playbook 语法检查通过，镜像构建完成，
正在 prepare。prepare 捕获原容器 ID 和实际启动命令，新容器使用独立标签
和 private IPC，D 在容器内为 shm 设置 huge=always。start 仅切换捕获的
四个 vLLM 容器，先检查原引擎空闲及 NPU 进程归属，再启动独立 Master、
八个引擎与代理。新服务的实测容量、请求和性能结果待启动后补充。

首次切换时旧 D 容器的 Docker stop 返回 exit-event 超时，稍后两台均确认
exited。容器管理脚本增加按捕获 ID 等待实际退出状态的逻辑。
首轮 P 启动遇到 vLLM 0.30 的加载参数校验，
`enable_multithread_load does not support safetensors_load_strategy='prefetch'`。
已按指南保持 P 的多线程 lazy 加载，D 使用 prefetch。首轮 P 日志保存在
`artifacts/layerwise-guide/20261107/first-prefill-failure.tar.gz`，
当前仅重启 P 两节点，D 继续完成本轮权重加载。

第二轮 P 在 KV 布局阶段命中
`Mooncake Main-only reuse with MTP requires prefix caching disabled`。
已关闭 P 跨请求 prefix caching，同一请求跨 chunk 的 Store 快照仍保留。
第三轮八个引擎与代理全部就绪，代理报告 P/D 各四个实例。P 各有
79 个逻辑层、5 个 Main 槽和 22 个私有 Indexer，block 数为
14,887／14,888，约 190 万 token。D 各有 9,174 个 block，
1,174,272 token，Host 实际约 99.53 GiB。DCP1、eager 与 MTP1/3 均在
实际进程参数和日志中确认。

初步功能验证共 12 条，含短请求、四并发 33,661 token 多 chunk 请求、
重复前缀的新请求，全部返回 MAPLE／QUARTZ。四个 D 实例均有流量，每条
请求的 8 TP rank 发布与目标释放检查通过。首次脚本即时扫描共享日志时漏读
部分释放记录，已改为有界等待日志可见，使用原请求记录重新核验通过。
请求与审计证据为 `artifacts/layerwise-guide/20261107/smoke*.json*`。

1,047,256 token 首测在约 305 秒结束、零输出，D 明确记录固定
300 秒 receipt watchdog 超时。P 收到 `Peer closed target` 后仍在完成剩余
prefill。为覆盖长 prefill，开发分支新增已签名提交 `15d342c0b`，通过
`layerwise_receipt_timeout_s` 显式配置总交付期限，默认维持 300 秒，
新 1M 档设为 7,200 秒。连接器实际依赖环境的 44 项测试全部通过，新增
默认期限、自定义期限及非法值回归。旧 no-compute 测试夹具补齐有效元数据，
源实现行为保持原逻辑。修改文件的 pre-commit 检查全部通过。扩大 Store CPU
检查为 268 项，候选与干净 `993713305` 的 6 个 failure、1 个 error 完全一致，
属于既有 KVPP／测试夹具问题。

最新 inventory 指向 `15d342c0b`，新容器名带 `-wd`、日志目录为
`tp8-dp4-1m-wd`。镜像构建、四节点准备和切换均完成，八个引擎及代理健康。
实际 P 布局仍为 79 逻辑层／5 Main 槽／22 私有 Indexer；独立 Master 总容量
640 GiB。修复版再次通过 12 条请求，含四并发 33,660 token 多 chunk 和重复
前缀新请求，四个 D 的每条请求均有全 8 rank receipt 和目标释放。
首轮短请求的 `MAPLE.` 被脚本的严格字面比较误判，调整为只容许目标词后单个
句末标点，重跑完整请求集与生命周期审计通过。证据为
`artifacts/layerwise-guide/20261107/wd-smoke-r2.jsonl`，stderr 为空。
共享日志可见性等待仍保持有界。1M 端到端尚未复测，不能据此宣称修复已通过
1M 验收。首测日志和旧容器保留。
CANN9.2＋pr3952 的独立构建在进行，节点访问 GitHub 失败后，改用已验证镜像内
干净的 vLLM `ced6857` 提交归档，以及控制机分发的 Mooncake、pybind11、
yalantinglibs 固定版本源码。P 初始化期间曾仅暂停私有 PID namespace 内的
构建进程，短回归结束后已恢复，并限定该构建容器 16 CPU；未修改宿主内核或
其它服务。构建的实际存活进程与日志需继续跟踪。

原生依赖栈后续遇到两项可复现的构建问题。FLA 下载 URL 的目录应为
`flash-linear-attention-npu-a3`，不是下划线形式；镜像脚本中注释掉的旧摘要
也与现有 Release 不一致。控制机实际下载的 SHA256 为
`0e0febfe0b524df54c02dead985b787cf51f276ef904e4c2f88c5d43707a370b`，
与官方 GitHub `v26.9.1-release` 的 aarch64 asset digest 一致，已固定并启用
校验。首次修正构建上下文导致 `COPY .` 缓存失效，已在重复编译前停止该构建，
从已完成编译、仅 FLA 下载失败的构建容器快照继续安装，没有修改服务容器。
恢复记录在共享构建目录的 `Dockerfile.ascend-recover` 和 `build-recover.log`。

Mooncake 的 `dependencies_openeuler.sh` 未安装 `msgpack.hpp`，openEuler RPM
仓库也没有对应包。按其上游 `dependencies.sh` 已有做法，补充固定
`msgpack-c@919908742b4fdbc575e77fe1a8657e70c9573c44`（cpp-7.0.0）头文件，
已通过先前失败位置，完整编译仍在进行。构建入口已同步修正上述依赖，拆开
依赖安装、编译和 wheel 打包层；源码差异校验确保 Python 镜像不会引用不同
C++／构建依赖的 SDK。依赖 SDK 与最终服务镜像使用独立标签。

CANN9.2 的 SDK 构建、wheel 打包和导出已完成，SDK ID 为
`sha256:04b8019a8292542a3f6e82109c875ca829d36855a7155c81e4461da5744e89c6`。
最新服务镜像 `glm52-vllm-ascend:0.30.0-layerwise-guide-15d342c0b-cann92`
的 ID 为 `sha256:d805ff2d015abfbfd780dcffb1b46790f62fc150538677064153895832af39da`，
已通过完整源码摘要检查并导出，正在分发。运行包为 vLLM 0.30.0+empty、
vLLM-Ascend 0.30.0、torch-npu 2.10.0.post7、Triton-Ascend 3.6.0.post2、
MemFabric 1.3.0、FLA 26.9.1。新 Mooncake 的包版本仍为 0.3.13，因此用
固定提交 `7bfe1cc` 和 wheel SHA256
`78811cbb73d08fe47a3e52e26cc76c096af015f0d706352f582c876fc02c74e3`
区分实际实现。元数据证据为 `native-sdk-metadata.json`，运行验证尚未开始。

随后四节点镜像 ID、807 文件摘要、私有 shm 配置均通过，旧引擎空闲检查、
停止和 NPU 归属检查通过。Ansible 启动完成，八个引擎及代理健康。新栈的 P
仍为 79 逻辑层／5 Main 槽／22 私有 Indexer、14,883／14,884 block；D 各有
9,174 block（1,174,272 token）和 99.99 GiB Host pool，Master 总容量 640 GiB。
新栈再次通过 12 条请求，含四并发 33,664 token 多 chunk 和重复前缀回放；
四 D 全 8 rank receipt 和 target 释放审计通过，证据为
`cann92-smoke.jsonl`，stderr 为空。实际 Store 分配量也已增长，数据面不是空跑。

将 CANN9.2 配置合并为新 inventory 默认，`cann92.yml` 保留为空的兼容参数
文件。重新 gen 四节点均 changed=0，生成脚本与已部署版本一致。1M 长测新增
`long.yml`／`long_probe.py`，初次启动发现 Ansible `script` 不支持 async，
该次未发送推理请求；已将 long 和 smoke playbooks 改为 copy＋command。
1M 复测仍进行中，需跟踪异步作业和独立 JSONL 日志，不能据短回归宣称 1M 通过。

实际 1M 输入为 1,047,254 token；P0 DP0 计算、D0 DP0 等待 KV。异步作业为
`j510195889183.1654235`，控制机日志 `/tmp/layerwise-cann92-long-r2.log`，
探针 PID 1654269，结果文件为 P0 日志目录的
`probe_1m_34c895e2cc3d4c9481d50553552448cd.jsonl`。14:27 CST 已超过原先
300 秒超时点，探针仍活跃、D 目标保留，P 的 KV 分配约对应 475K token，
尚未产生首 token。低频观测在 `long-progress.jsonl`；应继续跟踪同一作业，
不能因控制机观察会话丢失或等待超时而重启它。

新栈出现过私有快照 remove=-706，已核对其含义为 `OBJECT_HAS_LEASE`。
当前 Store 删除使用 force=False，并保留重试，符合租约保护；没有绕过租约
或强制清理。`published after prepare_decode` 在 `_maybe_publish()` 中发生于
完整 receipt 数据 ready 之后，长 prefill 期间没有这条发布日志属于预期。
待长测完成后仍需核对全 TP 发布、target 释放，并实际运行修正后的 Ansible
`smoke.yml`，验证测试入口和长请求之后的新请求，再做最终健康／空闲审计。

**最终验收结果：**同一 1M 作业于 15:00 前完成，Ansible 返回 rc=0。
请求 `chatcmpl-3c3aab8c-bc56-4800-a1f1-9e2ed1a6e523` 的真实输入为
1,047,254 token，输出 MAPLE（3 token）、finish_reason=stop，
TTFT=2,266.031416764 秒，总耗时 2,266.043837352 秒。全 8-rank
`published after prepare_decode` 与 target 释放审计通过。
证据为 `cann92-long.jsonl` 和 `cann92-long-receipt-audit.json`。

随后实际执行修正后的 Ansible `smoke.yml`，copy＋command＋async 流程通过，
12 条短／四并发多 chunk／前缀回放请求及四 D 全 TP receipt、target 释放通过。
证据为 `cann92-post-long-smoke.jsonl`，控制机原始输出为
`/tmp/layerwise-cann92-post-long-smoke.log`。新栈合计 25 条请求，P/D 成功计数
均为 25，无 error／abort。最终八个 API 为 200，running／waiting／KV 使用
均归零，代理报告 P/D 各四实例；八份引擎日志无 ERROR／Traceback。
见 `final-health.json`。前后四节点容器 ID、镜像 ID、启动时间及 24 份生成配置
全部一致，见 `identity-{node}.json`／`identity-final-{node}.json`。
九个 playbook 语法、三个 Python 文件的 Ruff／format 和 git diff 检查通过。

当前服务使用新 inventory 默认，容器名为
`vllm-ascend-glm52-layerwise-guide-cann92`，代理在 P0 的 8333。
旧 WD／初版 guide 容器与日志保留，新的服务保持运行。只保留最终服务镜像的
分发 tar，已通过 Ansible 清理本轮重复的 SDK tar；SDK 镜像本体仍在 P0。
没有修改宿主全局 THP、驱动或其它服务。本轮是功能验收，未进行吞吐基准对照。

### 11.74 新 layerwise 拓扑 55K／700、10／20 并发性能测试（2026-11-07）

在 §11.73 的服务上直接测试，未重启或修改服务配置。P/D 各 TP8×DP4、
CP/DCP1、eager，P chunk16384／seq8／MTP1，D token budget32／seq8／MTP3，
Host100 GiB。镜像和开发提交仍为 `15d342c0b-cann92`。

复用前次 v1.7 镜像 `99bd08d5f3b7f`、相同 vendor bench SHA256 `b109ab8e` 和
checked-bench 包装器，接口为新代理 8333。每档 40 条，seed20261106，
random-input-len55000、output-len700、range-ratio1、request-rate inf、
warmup0，ignore_eos 保持开启，关闭 thinking，启用 SSE usage。
每档独立 cache_salt，P 的 HBM／外部前缀命中增量均为零。只检查响应完整和
实际 token 计数，不检查随机乱码内容的语义或精度；未开启 profiler，只在测试
前后读取服务指标。客户端数据准备时间不计入 benchmark duration。

| 并发 | 成功 | 压测耗时 s | TTFT 均值 s | TTFT P50 s | TTFT P99 s | TPOT P50 ms | TPOT P99 ms | 输出 token/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 10 | 40/40 | 445.969 | 16.987 | 16.140 | 33.432 | 123.312 | 147.614 | 62.785 |
| 20 | 40/40 | 282.760 | 27.986 | 23.025 | 53.378 | 125.205 | 184.185 | 99.024 |

两档实际输入总量均为 2,197,493 token，单条范围 53,988–55,006；输出均为
28,000 token，80 条请求均完整输出 700 token、finish=length，无失败。
TPOT 使用 `(e2e-ttft)/(usage.output_tokens-1)`；MTP 流式分块的 ITL 不是单 token
TPOT。D 实际每轮草拟 3 token，平均接受长度（含基准 token）为 2.899／2.966，
draft token 接受率 63.31%／65.52%，并非只配置了 MTP 而未执行。

与 §11.72 的旧 receipt MTP5 整体配置相比：10 并发 TTFT P50 从 9.000 增至
16.140 秒，吞吐从 61.268 至 62.785（+2.48%），TPOT 基本不变；20 并发 TTFT
P50 从 86.309 降至 23.025 秒（-73.32%），吞吐从 65.374 至 99.024（+51.47%），
TPOT 从 116.439 增至 125.205 毫秒（+7.53%）。拓扑、SDK、chunk、Host、MTP
均有变化，不能将差异当作某一个代码优化的净收益。解码 TPOT 仍约 125ms。

前后四节点容器 ID、镜像、启动时间和生成脚本核对一致。P/D 成功计数各增
80，error／abort 增量为零，最终八个 API 健康、running／waiting／KV 使用归零。
只停止了本次独立的 CPU 压测客户端，推理服务保持运行。
完整命令、原始 JSONL、SSE 计数日志、前后指标、可复算 summary 和 comparison
位于 `artifacts/benchmark-tool-v1.7/layerwise-guide-55k-700-c10-c20-20261107/`。


### 11.75 Main reuse + P MTP1 的跨请求前缀（2026-11-07）

用户要求保留当前 P/D MTP1/3，先实现跨请求前缀，再重测约 50% 命中的性能。
原 `15d342c0b` 把 MTP 完整块与部分块全部设为请求私有，scheduler 明确返回
零外部命中；仅预热不能产生 50% 命中。HBM prefix caching 仍须关闭。

开发提交 `fe559c440` 增加 MTP1 的 Mooncake 公共前缀。公共完整块身份为
`mtp_prefix_v1:<block_index>:<successor_chain_hash>`；MTP 块尾消耗下一输入
token，因此使用完整后继块的链式 hash 保守覆盖该依赖，继承 cache_salt
和 vLLM hash extras。prompt 末尾、部分块与生成区间仍用私有快照。查询、
保存及恢复使用同一函数；公开前缀比纯 Main 匹配最多少一块。其他 P draft
方法或多步 MTP 暂时维持原先禁用跨请求查询的行为。

公共 PUT 争用可回退到该请求的私有完整块，非保存 TP rank 可推导相同回退
身份；同一请求跨 chunk 优先使用已提交版本。查询后淘汰会在 attention 前
失败，不借用别的请求的私有对象。清理只处理私有 key，公共对象交由 Store
管理。未改变 Main 共享槽、registered target、全 TP receipt 与生命周期屏障。

控制机聚焦测试扩大至 83 项及 44 个子用例，全部通过。完整 scheduler 套件
有 6 个失败在未修改 HEAD 同样复现，另 74 项通过；全仓库 format 检查遇到
既有 lint 问题，其对无关文件的自动改动已撤回。未声称全套测试或 lint 全绿。

Ansible 使用独立容器 `vllm-ascend-glm52-layerwise-guide-mtp-prefix`，旧
`...-cann92` 容器停机保留；四节点同一镜像
`glm52-vllm-ascend:0.30.0-layerwise-guide-fe559c440-cann92`，ID
`sha256:8dad7347dd0d2728206f058b20e0f2f0fa7cfd6d6f9ea984fd28dfcfd1e0f74b`。
保持 CANN9.2/pr3952、TP8×DP4、1M、eager、P chunk16384/seq8/MTP1、
D budget32/seq8/MTP3/Host100GiB。新部署 P 约 14,880 blocks，D 9,174。
所有 gen/build/prepare/start 检查通过；八引擎与代理健康。默认 inventory
已更新为此部署，重现实验覆盖参数在 `artifacts/mtp-prefix-20261107/override.yml`。

真实功能探针 13 条全部通过：部分前缀实测 19,072/33,655 token（56.67%，
这是功能探针，不是正式 50% 性能组）、完整回放、salt 隔离、四并发缓存读取
与四并发冷写争用；输入、预期命中量和输出分别核验。Ansible 另 12 条短/
多 chunk/前缀回放全部通过。两组共 25 条请求逐条检查全 8-rank receipt 与
target 释放，覆盖四 D 引擎，无请求错误。

正式性能使用 v1.7 客户端、同一 seed 的 40 条约 55K 输入、每条 700 输出、
10/20 并发。每请求独立 salt，分别跑新代码下 0% 与约 50% 命中；50% 组在
原生计时前预热，每条按实际 chat token 边界校准，匹配量向下按 128 对齐。
以 P 侧实际 external hits 增量对照逐请求预期总量；预热不计入正式吞吐和
延迟。功能正确性探针与随机性能请求分开；正式阶段只检查 SSE、usage、
700 输出及失败计数。

四组均 40/40 成功，每组实际输入 2,197,493、输出 28,000 token。两组缓存
实际命中均为 1,094,912 token，即 49.8255%，逐请求预期和 P 计数完全一致；
0% 两组命中均为零。40 条输入的 SHA256 向量在四组完全相同，所有正式响应
均完整返回 700 token，预热和功能探针不计入性能统计。

| 并发 | 实际命中 | TTFT均值/P50/P99(s) | TPOT P50/P99(ms) | 输出吞吐(token/s) | 正式耗时(s) |
| --- | --- | --- | --- | --- | --- |
| 10 | 0.00% | 12.92/10.81/27.85 | 118.51/157.59 | 67.76 | 413.23 |
| 10 | 49.83% | 8.84/7.13/19.05 | 115.60/138.49 | 74.46 | 376.07 |
| 20 | 0.00% | 24.06/17.23/52.48 | 120.62/150.66 | 111.10 | 252.03 |
| 20 | 49.83% | 17.06/10.37/38.77 | 117.72/164.52 | 116.44 | 240.46 |

相对同源码同配置 0% 组，10/20 并发 TTFT P50 降低 34.00%/39.85%，均值
降低 31.60%/29.11%，输出吞吐提升 9.88%/4.81%。TPOT P50 降低约 2.4%；
20 并发 TPOT P99 从 150.66 增至 164.52 ms，因此不声称所有尾延迟都改善。
TPOT 使用 `(e2e-ttft)/699`，不把 MTP 的流式分块间隔当作单 token 延时。
D 侧平均接受长度依表格顺序为 2.9648/3.0054/3.0123/3.0127，每步 draft
计数均为 3，证明 MTP3 在执行，并未为前缀测试关闭投机。

265 条请求（25 功能、80 预热、160 正式）的全 8-rank receipt 和 target 释放
全部通过，覆盖四 D 引擎；P/D 各 success=265，error/abort=0。八引擎和代理
健康、空闲，KV 使用归零，八份引擎日志 ERROR=0。测试前后四容器 ID、镜像
ID、启动时间和启动脚本 hash 完全相同。只停止本次 CPU 性能客户端，保留
已验证 vLLM 服务。默认 inventory 已与运行配置一致，用户无需额外覆盖参数。
本轮未重复近 1M 长请求，不能将前任的长测结果标成新代码已重测。


证据与可复现脚本：`artifacts/mtp-prefix-20261107/`；日志：
`/mnt/sfs_turbo/g00832294/layerwise-guide/tp8-dp4-1m-cann92-mtp-prefix/`。


### 11.76 D decode 性能定位与首轮优化（2026-11-07，性能验证完成）

用户要求定位 D 性能劣化的主要原因并开始优化。PR #1 的三条意见分别指向
完整 lease 深比较、共享注册池逐请求校验、receipt eager 限制与图外事件开销。
当前尚未量出这些项的现场占比，不把静态复杂度或不同配置的历史性能差距
直接当成因果结论。

诊断源码 `f0abb2e8a` 增加默认关闭的 `receipt_timing_interval`，在已绑定的
worker 实例上记录 target/draft 两种 batch 上下文的进入、body wall、退出，
以及 lease、资源、tensor 元数据、注册快照、visibility 和 record_use 嵌套
计时。计时不新增 NPU 同步或事件；嵌套项不可相加，body wall 也不是纯
kernel 时长。min_prompt_tokens=50000 排除功能探针；每64个上下文汇总日志。
90项和48子用例通过，包含异常传播和原有完成路径的透明性验证。

独立 Ansible 诊断覆盖配置为 `artifacts/d-decode-performance-20261107/diagnostic.yml`，
新容器 `vllm-ascend-glm52-layerwise-guide-dtiming`，前任 `...-mtp-prefix` 保留。
不改默认 inventory，仍保留 TP8×DP4、P/D MTP1/3、1M、P B2、D Host100GiB、
D seq8/token32、eager。先运行未开启 NPU trace 的20条55K/700/C10样本，
再单独跑10条请求，并在收到8条首token后选择活跃D实例采集8步trace；后者
不用于正式性能结论。设备trace、计时日志和分项统计尚待采集。

诊断已完成：20/20随机性能请求成功，TPOT P50=117.632ms、TTFT P50=15.452s，
输入1,099,510/output14,000；该20条样本不能把吞吐直接与前轮40条结果比较。
32个D worker按target/draft分别统计：target enter/exit合计约4.71ms，body
326.75ms；draft enter/exit约4.89ms，body19.88ms；target step interval
369.81ms。receipt进入/退出约占2.6%，嵌套检查不重复累计，不对TP耗时求和。

独立8步trace已采集，在D1容器中离线解析TP0/1（profiler子进程的在线导出器
不能创建子进程，离线analyse成功）。TP0的stage3.510s、compute0.518s、
free2.967s，36,680次kernel launch；host self MLA0.764s、MoE0.466s。
TP1表现为更多通信等待：stage3.864s、compute1.255s、通信2.257s、free0.352s。
各rank不能互相当作相同空闲率，也不能把trace绝对时长当作正常性能。
优先验证eager host下发/图外执行，而非把完整劣化归于receipt metadata。

原诊断smoke在short-2(540input/7output)返回MAPLEPAMAPLE，正常stop但未满足
单词语义断言；已保留smoke-attempt1.log，尚未证明原因。独立随机性能阶段
仅验证完整SSE/usage/700输出，未以随机文本语义阻断定位。

候选源码e9bd1ce19允许registered D target使用FULL_DECODE_ONLY，仍拒绝
其他graph模式和PP/CP/DCP>1。P侧eager、Main复用、MTP1及D MTP3/Host/
AsyncReceiptScheduler均保留；receipt admission仍在graph外，实际target和
draft replay由原use上下文包围，消费stream前的visibility和replay后的完成
事件均未删。93项/60子用例通过。候选Ansible graph.yml只对D切换target和
draft graph并固定1..32的10个capture尺寸；无计时/trace配置。新容器...-dgraph，
日志run_name=tp8-dp4-1m-cann92-dgraph。部署和NPU验证进行中，不视为已可用。

图候选已完成现场性能验证，源码e9bd1ce19，新镜像
`glm52-vllm-ascend:0.30.0-layerwise-guide-e9bd1ce19-cann92`，四节点ID一致：
`sha256:4c7a55de5cdc31eab28a8b97c8f7af099d30825957824d3de67e2e5f6180099d`。
四D引擎decode捕获成功，约38–39s、每rank7.75GiB；保留9174blocks/
1,174,272tokens容量。P仍为B2 Main复用/eager/MTP1，D为Host100GiB/DP/
MTP3/AsyncReceiptScheduler/FULL_DECODE_ONLY。没有变更attention算法、TopK、
KV容量或receipt/retention检查。所有性能组关闭host计时和NPU trace。

同一组20条55K/700/C10、逐请求hash及实际token总数完全一致的现场对照：
eager诊断组TPOT P50/P99=117.632/160.511ms，吞吐56.442token/s、
248.043s；图组32.087/44.819ms，150.347token/s、93.118s。MTP平均
接受长度2.9390/2.9691。该对照的eager侧启用了低频host计时，未启用NPU
trace；计时开销不能解释72.72%的TPOT降幅，下面再与无计时的40条基线核对。

正式40条组与§11.75同日eager基线比较，55K输入/700输出、0%前缀命中，
seed和客户端相同；每组实际输入2,197,493/output28,000，40条prompt SHA256
向量完全一致。不是背靠背切换运行，仍可能受现场负载和调度差异影响。

| 并发 | D执行 | TTFT均值/P50/P99(s) | TPOT P50/P99(ms) | 吞吐(token/s) | 耗时(s) | MTP平均接受长度 |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | eager | 12.92/10.81/27.85 | 118.51/157.59 | 67.76 | 413.23 | 2.9648 |
| 10 | graph | 13.29/11.54/27.47 | 31.15/39.98 | 183.40 | 152.67 | 2.9584 |
| 20 | eager | 24.06/17.23/52.48 | 120.62/150.66 | 111.10 | 252.03 | 3.0123 |
| 20 | graph | 24.38/19.99/51.13 | 47.51/70.65 | 210.37 | 133.10 | 2.9394 |

图组10/20并发TPOT P50降低73.72%/60.61%，输出吞吐提升到2.71/1.89倍；
不是靠更高MTP接受率。结合host计时和设备trace，D主要劣化来自eager下
大量host执行/下发及其引发的rank等待；receipt逐请求检查在旧eager C10中约
2.6%，不是原主要瓶颈。图执行之后这些检查的占比尚未重新量化，仍可继续
研究每batch共用注册校验、紧凑lease身份等优化，但本轮未删任何安全检查。
TTFT没有改善：P50由10.81/17.23升到11.54/19.99s，不能声称P侧或端到端
首token瓶颈已解决；20并发图组仍有47.51ms TPOT，亦未宣称逼近所有原始基线。

候选初次严格smoke的short-0返回MAPLEPMAPLE(540input/6output，normal stop)，
与eager诊断组的重复词同类，尚不能归因于graph，也未证明修复。完整性能
结束后，独立新nonce的12条短请求/多chunk/前缀回放全部语义正确且normal
stop，覆盖四D及全8-rank receipt/target释放。此轮observer先记录全部样本，
再对12个answer/finish作严格后置断言；没有把放宽断言后的进程exit0当语义
正确证据。初次异常完整保留，因此默认inventory仍保持fe559c440/eager，
不自动提升候选为默认服务版本。

100条随机性能请求全都完整输出700token，逐请求全8-rank receipt及释放审计
通过；加初次short-0和后续12条功能探针，共113条，D receipt904、释放113，
P/D success各113、error/abort=0。最终八引擎/代理健康且空闲，KV使用归零，
八份引擎日志ERROR=0。现场保留...-dgraph候选容器（旧...-dtiming及...-mtp-prefix
均停止保留）；默认inventory与当前临时候选有意不同，部署候选须用本轮graph.yml。

用户授权D Host KV可扩大到1TB；现场每D节点约2TiB物理内存、约1.7TiB可用。
若按节点1TiB预算，两DP可各512GiB，仍需核对HBM Indexer与TopK工作区预算。
本次55K/10和20并发无需扩大Host容量，为保持对照仍为每DP100GiB，未修改
主机大页、内核参数或vLLM以外的服务。未重跑1M长度，也未重跑50%命中性能矩阵。

复现与证据：`artifacts/d-decode-performance-20261107/`内graph.yml、
deploy-graph.sh、comparison.json、trace-summary.json、timing-summary.json、
performance-audit.log、graph-observer.log、observer-verification.json及最终
health/manifest。设备原始trace留在...-dtiming/d1/logs/npu-profile，图候选服务
日志在`/mnt/sfs_turbo/g00832294/layerwise-guide/tp8-dp4-1m-cann92-dgraph/`。

### 11.77 Graph 后 receipt host 开销重测（2026-11-07）

在e9bd1ce19同镜像下只通过Ansible替换两台D容器，P、Master、代理保持原
进程。临时档`...-dgraph-timing`，FULL_DECODE_ONLY、async scheduling、MTP3、
Host100GiB/DP、seq8/token32、1M均不变，64个上下文记录一次；不启用NPU
trace，不新增同步/事件。55K/700、C10/C20各40条，输入hash及token总数与前轮
相同，均40/40成功、零外部前缀命中，80条全8-rank receipt/释放审计通过。
每worker/phase排除首个窗口；C20去掉C10已有记录并再排除转换首窗。原D日志
先归档再恢复，P0容器/启动时间、Master和proxy PID前后完全相同，结束恢复
原...-dgraph无计时D、八引擎健康空闲，压测客户端已停止。

| 每步target+实际draft，跨32worker取中位数 | C10(ms) | C20(ms) |
| --- | --- | --- |
| lease查找与完整比较 | 2.893 | 4.460 |
| 注册池完整校验（含以下嵌套项） | 3.212 | 4.722 |
| visibility wait入队 | 0.113 | 0.196 |
| record_use完成事件管理 | 0.767 | 1.053 |
| receipt进入/退出合计 | 7.402 | 11.203 |
| target step interval | 99.273 | 104.071 |
| 上述receipt合计/interval | 7.54% | 10.62% |

典型窗口估计占比为8.03%/10.61%；interval可能包含调度/空闲间隔，比例只是
近似。每个target/draft上下文平均batch分别约1.745/2.410，请勿将TP耗时相加。
资源检查内含tensor signature与registration snapshot，不能再累加；进入/退出
残差还包括scheduler allocation比较、上下文及计时开销，不当作纯lease比较。
计时包含观察者成本，这些数字也不等于删除检查后可以直接获得的TPOT收益。

带计时TPOT P50/P99为31.025/47.262ms(C10)、36.895/43.661ms(C20)，
吞吐175.97/226.73token/s，TTFT P50=13.008/25.620s。此前未计时graph结果
分别为31.149/39.981ms、47.512/70.649ms。C20本次batch密度与调度也变化，
不能把更低TPOT归为计时收益，亦不据此声称精确量出了观察者开销。

最初--limit decode使原start.yml委派P0的SSH跳板参数错误，发生在切换前，
原服务未动；本轮拆为显式P0空闲检查和D-only cutover，再复用原launch任务。
证据及可复现Ansible/采样/恢复脚本见
`artifacts/d-graph-host-timing-20261107/`，其中summary.json、audit.log、
final-health.json、p0-control-before/after.json及前后D日志归档均保留。

### 11.78 PR 检视修改、batch execution 与重构首审（2026-11-07）

用户要求先处理必要检视意见，再审视几万行实现的重构/优化空间。
源码57c1b306e将完整lease比较移到admission，worker-local不可序列化句柄
仅绑定当前owner/state，热路径仍逐请求核对scheduler实际BlockAllocation，
每batch校验一次共享注册池并共用一组完成事件；visibility仍逐请求安装，
stale/foreign/duplicate、改变block map、storage、mapping与TE registration
继续拒绝。没有删除cold/wire descriptor校验或提前授权block释放。

真实CPU torch2.10环境回归：202 passed、1 skipped、107 subtests（包括后续
startup与graph stream测试）。skip为缺少完整vLLM/msgspec的connector导入用例，
原生服务接线另验。scoped Ruff和git diff --check通过。

通过Ansible仅替换D，P仍e9bd1ce19/eager/Main B2/MTP1，Master/proxy保持原进程。
57c1b306e D容器...-dbatch，MTP3、FULL_DECODE_ONLY、compilation mode0、
async scheduling，seq8/token32、Host100GiB/DP，1M不变。mode0的target graph
确实capture，但runner._use_aclgraph()不启用draft graph；不得将speculative
enforce_eager=false当成draft graph已开启。

首次启动DP3/TP7 receive线程bind57031冲突后退出，主线程永久等ready_event，
其它TP停于backing identity all_gather、其它DP停于graph metadata all_reduce。
启动前尚无业务请求；nonblocking py-spy仅用于故障启动，未用于性能采样。
原占用很快消失，未识别到占用者；未杀其它服务、未改host ephemeral range。
候选改用检查空闲且位于32768–60999临时端口范围之外的KV24700、owner24100。
8ecddeb82另修send/recv线程启动等待：检测死亡并限制60秒，增加真实ZMQ冲突、
dead-after-ready和timeout回归，避免同类故障表现为永远无API。bb679f01d把真实
graph update stream加入native完成fence，图模式缺此接线时fail closed。

控制机自动采样脚本曾重叠，导致一次功能/性能请求混跑；全部归档在
attempt2-contaminated，不用于正式数字。停掉自有controller和CPU压测客户端、
确认八引擎空闲后，单一controlled-run依次执行20条独立功能与两组性能。
20/20短请求、多chunk、前缀回放语义及全8-rank receipt/释放通过，覆盖四D。
55K/700、N40、C10/C20均40/40成功，hash向量与此前相同，实际输入2,197,493、
输出28,000/组，零外部命中；80条全8-rank receipt/释放审计通过。

| 每step target+实际draft、32worker中位数 | 修改前C10/C20(ms) | 修改后C10/C20(ms) |
| --- | --- | --- |
| lease完整比较 | 2.893/4.460 | 0/0 |
| 注册池校验 | 3.212/4.722 | 1.640/1.652 |
| visibility | 0.113/0.196 | 0.083/0.108 |
| record_use | 0.767/1.053 | 0.490/0.492 |
| receipt host合计 | 7.402/11.203 | 2.514/2.593 |
| 占step interval | 7.54%/10.62% | 2.42%/2.36% |

排除每worker/phase首窗；C20去掉已有C10记录及转换首窗。shared资源计数应按
batch而非request解释，不能将嵌套tensor/registration耗时再次相加。host总耗时
约减少66%/77%，但带计时TPOT P50/P99=32.256/48.453、38.276/49.945ms，
吞吐166.30/207.31token/s，TTFT P50=13.257/30.026s；相比上一轮带计时
31.025/36.895ms，TPOT没有提升。只确认CPU重复工作下降，不宣称端到端加速。

正式后四份vLLM D日志ERROR/Traceback/Exception in thread均为0，八引擎空闲。
ACL debug plog在启动graph阶段有GE GetPointerByIndex attr越界日志，服务未
因之失败；未宣称所有底层ACL日志无ERROR。D日志已归档，避免后续启动覆盖。
完整证据：artifacts/review-fixes-20261107的controlled-*、summary.json、
timing-*、functional20-pass.json、post-controlled-idle.json及测试日志。

首轮重构审查见[代码审查](receipt-code-refactor-audit-20261107.md)：相对基线新增
运行代码约10.35K、测试9.94K、文档2.20K。优先清生产无调用者的co-located
原型、拆retention协议与Pipe耦合，再整理connector多份请求状态和锁；另发现
event历史、退休请求记录无上界，以及COPY覆盖镜像无法删除旧Python模块。
本轮未实施大范围拆分；MTP5原生组合及最终无计时服务状态另补以下实测记录。

bb679f01d后续使用独立...-dbatch-mtp5容器，MTP5、seq8/token48、mode3、
FULL_DECODE_ONLY、async scheduling；P/Master/proxy继续保留。四D均完成target
graph capture（38秒、7.77GiB），32worker启动日志明确draft_graph=False：当前
GLM draft仍由框架的family gate强制eager，mode3未改变这一点，不能称双图验收。
20条独立语义/多chunk/前缀回放及55K/700 N8/C8全部通过；提议1695次、draft
token8475，比例精确5。224个host计时窗口覆盖32worker的target/draft，实际
batch最大2，每batch一次资源校验/record_use、逐请求visibility、无完整lease
比较、无timing failures。四vLLM D日志ERROR/Traceback/Exception in thread均0。
N8探针TPOT P50/P99=28.364/35.128ms、TTFT P50=18.940s、吞吐116.68token/s，
仅为该组合执行验证，不与N40/C10或C20作性能提升比较。

两次真实客户端在收到8个content chunk后断连，target均很快释放、KV/队列归零。
首次探针错误地要求frontend request_success{abort}增长，进程exit1，原始失败
日志保留为mtp5-cancel-metric-assumption.log；未据此修改服务代码或伪造exit0。
补充探针测得8次draft/40个draft token（生成上界48，远小于ignore_eos/4096
的正常长度完成），success没有增长，且精确request ID的全8-rank receipt与
target释放通过，证明提前终止和实际回收。共30条（20功能+8性能+2断连）生命周期
审计通过，四D覆盖。不是native delayed-DMA/registration-reset故障注入验收。
新增证据：mtp5-native-verification.json、mtp5-native-proof.json、mtp5-audit30.json、
mtp5-dlogs.tar.gz、mtp5-post-health.json；首次错误指标假设与补充实测均保留。

**后续MTP3/mode0验收失败，撤回该配置可用性结论。** 在bb679f01d的新无计时
...-review-final容器上，20条生命周期全部通过，但逐条语义只有17/20：
short-0=MAPLEPMAPLEPMAPLE、short-1=MAPLEPSEUDO-ATTENTION、
multichunk-4=MAPLEAFETY_INSTRUCTION!。首次严格verifier只报首个失败，后续
完整统计纠正为3条，未将shell尾部健康检查的exit0当成功。原始结果保留在
final-functional20.log、final-functional-verdict.json，不重跑覆盖失败。

为隔离配置，用同一source、MTP3、无计时、同一20条完整提示与run_nonce，
分别使用新的arm cache_salt防止旧公共cache命中；两arm内部仍做prefix回放。
mode0再次18/20（两条回放QUARTZTRAUQ），mode3为20/20，均全8-rank生命周期
审计通过，覆盖四D。prefix回放失败说明问题不限于冷启动首请求。
该比较仍含重建D容器、P计算非确定性和不同cache key等边界，支持限制未经
验收mode0，不足以单凭一组通过宣布全部异常根因已修复。此前eager也有异常，
尚不能认定全都同因。旧mode0性能数字保留，但不作为语义已验收的可用基线。

代码检查发现mode0虽然能通过继承路径capture target graph，runner._use_aclgraph
却为false，且不走其编译图参数初始化；这是一处配置不一致，不是完整因果证明。
dfb52bd0a将receipt图模式限制为解析后的CompilationMode.VLLM_COMPILE及
FULL_DECODE_ONLY，eager和原PP/CP/DCP1限制保留；Ansible gen也在切换前拒绝
mode0。新增回归后203 passed、1 skipped、112 subtests，scoped Ruff通过。
Ansible mode3 check全通过、mode0 check预期exit2且changed0；README已移除
旧graph.yml作为可用复现入口。新增证据：compiled-graph-gate-unit.log、
gate-compile3-check.log、gate-compile0-rejection.log、isolate-compile*-*。

带限制的新镜像由同一Ansible构建、分发到D，并准备最终mode3/MTP3/无计时
候选；最终身份、回归结论与保留状态见下面收尾记录。P、Master、proxy仍不重启。

**最终保留现场：dfb52bd0a / mode3 / MTP3 / 无计时。** 镜像
`glm52-vllm-ascend:0.30.0-layerwise-guide-dfb52bd0a-cann92`，两D一致ID
`sha256:4aac85d0b0c9128a0ae72e6eee15f6b997dd5189a3b403252d7182714db9cc58`。
容器`vllm-ascend-glm52-layerwise-guide-review-safe`，run_name
`tp8-dp4-1m-cann92-review-safe`，实际profile为
`artifacts/review-fixes-20261107/safe.yml`：D TP8×DP4、Host100GiB/DP、
seq8/token32、MTP3、async、FULL_DECODE_ONLY、compilation mode3，未启用
receipt timer或NPU profiler。KV24700、owner24100；P继续e9bd1ce19的...-dgraph，
eager/Main B2/MTP1。默认inventory仍fe559c440/eager，未自动提升候选为默认。
此profile已在源码和Ansible限制下成功原生启动，证明真实parsed compile mode
及graph update stream接线满足启动约束；32worker日志均确认target graph，draft eager。

新nonce冷启动20/20语义与生命周期通过；随后同一原失败提示、独立新salt的
20/20也通过，均覆盖四D。两组P external prefix命中267264/540752和
267264/540692，约49.42%/49.43%，证明回放实际走了公共Store复用。最后一条
ignore_eos/4096输出请求收到8个content chunk后断连，仅8次提议/24个draft
token（生成上界32），正常success未增长、KV和队列归零；精确request ID的
8-rank receipt与target释放通过。共41/41生命周期通过，含40条完整语义回归。

最终八引擎及proxy健康空闲，KV使用归零；四份vLLM D日志ERROR/Traceback/
Exception in thread和RECEIPT_HOST_TIMING均0，四D完成graph capture；底层ACL
日志不在此零ERROR声明范围。P0/P1容器ID、image ID和StartedAt均与原先一致，
P0 Master=2435912/proxy=2470638未重启，CPU压测客户端已停止。拒绝的mode0
容器、MTP5和中间mode3容器均停止保留，只有最终D候选运行。

正式C10/C20性能矩阵尚未在该最终mode3配置重跑；不从mode0数字推算新配置
TPOT收益。限制mode0和这批mode3成功样本不构成全部历史异常根因已修复的证明，
亦未补做native delayed-DMA/registration-reset故障注入或新1M长请求。
证据：safe-functional-verdict.json、safe-compare-verdict.json、safe-audit41.json、
safe-prefix-proof.json、safe-cancel.log、safe-health.json、safe-manifest.json、
safe-log-audit.json、safe-dlogs.tar.gz、p0-control-safe.json。最终不再发新请求。

### 11.79 Receipt重构第一批（2026-11-08，进行中）

源码080220d72移除生产无调用者的target_owner_channel和target_owner_wire，
Pipe双向真实线程/进程故障夹具移至tests/owner_pipe.py，清除无调用者的
PipeOwnerChannel组装器；RPC继承无transport字段的RemoteRetentionAuthority，
不再跳过Pipe构造函数。RetentionOp/Result字段与codec v1/v2名称保持不变。
原全局slot测试改为两个同时存活的显式DispatcherOwnerTransport独立生命周期
和未绑定/关闭拒绝测试，真实connector接线用例仍保留。首批200 passed、
1 skipped、112 subtests；额外legacy wire frame兼容回归待下一次聚焦运行。

镜像源替换前读取实际SDK文件清单，发现除.so/metadata还有整套CANN生成算子
目录及include/lib64，必须整体保留。install_source.py校验并保留这些SDK产物，
删除其余管理的源码树后复制新快照；构建改为mktemp独立context。文件系统
5项回归覆盖删除模块不可import、旧字节码消失、重复快照删除、native/symlink
内容保留、metadata碰撞和未知嵌套native库拒绝。真实新镜像与后续结构改造
仍待验收，不将首批CPU通过当全目标完成。详见执行清单及本轮artifacts。

11.79补记：真实SDK构建检测到源码内_cann_ops_custom/.gitkeep；初次过严规则
在镜像构建期拒绝，不涉及服务切换。补充只允许.gitkeep、不允许覆盖实际
operator文件的回归后6项文件系统测试通过，新080220d72镜像构建成功；两台D
的空闲refactor-r1候选均1395项SDK哈希通过，已删除的channel/wire模块及旧
pyc不可import。完整日志image-check.log，初次失败保存在phase1-build-attempt1.log。

cf4ad2e31继续拆分ReceiptReceiver和ReceiveRequestIndex，去除receive端平行
ledger/id/deadline字典及worker_admitted_target_blocks；旧普通P/D映射保留，
exact目标通过接收记录查询。delivery->index->ledger锁序明确，外部provider
回调在index锁之外；回调后检查记录仍存活，取消赢得visibility锁时不发布。
同时覆盖共享external-id多代次、未知/延迟身份、正常legacy发布与状态集轮换；
16项接收用例使用真实msgpack0.22在CPU运行，全套217通过/1跳过/112子用例，
旧ledger37通过。真实SDK connector导入回归正在运行，尚未做新版本原生PD验收。
