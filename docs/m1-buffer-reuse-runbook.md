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
