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
