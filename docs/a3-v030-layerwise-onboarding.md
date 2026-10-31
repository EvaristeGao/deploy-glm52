# A3 / GLM-5.2 / vLLM-Ascend v0.30 —— 部署方案上手文档

> 面向：接手这个部署方案的新 Agent 或新同学。
> 状态：**已实机部署并验收通过**（4 引擎 200、proxy ok、并发请求正确）。
> 最后更新：2026-10-31，对应 commit `9ddec01`。

---

## 1. 这套东西是什么

GLM-5.2 在 A3 集群上的 **P/D 分离**部署：

```
                       ┌──────────── proxy (:8000, 节点 p0) ────────────┐
                       │ 最小负载堆选 prefill → 转发 → 选 decode → 流式回 │
                       └───────┬───────────────────────────┬────────────┘
                               │                           │
                    ┌──────────▼──────────┐     ┌──────────▼──────────┐
                    │ P0 / P1（prefill）  │     │ D0 / D1（decode）   │
                    │ TP16 · DP2 · :9081  │     │ TP16 · DCP16 · :9900│
                    └──────────┬──────────┘     └──────────┬──────────┘
                               │      KV 池（mooncake）      │
                               └──────────► 共享 KV 池 ◄─────┘
                                     block_key 数据面
                                     layerwise 逐层读写
```

- **P 侧**：`kv_producer`，逐层把 KV 写进池（layerwise upload）；
- **D 侧**：`kv_consumer`，逐层从池里读回 KV 再解码；
- **池**：mooncake，key = block hash，一层一个对象页（`page_size × num_layers` 的对象，按 `layer_id × page` 写）；
- **入口**：`load_balance_proxy_server_example.py`，对客户端暴露 OpenAI 兼容 API。

**当前部署的是「干净档」**：只用 v0.30.0rc1 镜像自带能力，**不挂任何 patch、不开 buffer reuse**。

---

## 2. 集群与目录（硬编码事实）

| 角色 | 节点 | IP | 端口 | 容器 |
|---|---|---|---|---|
| prefill p0（也是跳板机 / proxy 所在） | `ott9-a31` | 172.168.4.237 | 9081 | `vllm-ascend-gwh-glm52` |
| prefill p1 | `ott9-a32` | 172.168.4.31 | 9081 | 同上 |
| decode d0 | `ott9-a33` | 172.168.4.230 | 9900 | 同上 |
| decode d1 | `ott9-a34` | 172.168.4.226 | 9900 | 同上 |
| proxy | = p0 | 172.168.4.237 | 8000 | 同容器内进程 |

- 镜像：`glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13`
- 共享盘：`/mnt/sfs_turbo/g00832294/`
- 引擎日志：`/mnt/sfs_turbo/g00832294/logs/vllm_{p0,p1,d0,d1}.log`、`proxy.log`、`mooncake_{node}.log`
- **ACL 设备日志**：`/mnt/sfs_turbo/g00832294/logs/ascend/{p0,p1,d0,d1}/`（容器内 `/root/ascend/log` 的持久化挂载，`run/device-N/device-*.log`、`debug/`）
- 控制机：本仓库所在机器（有 `ansible/.venv`），通过 ssh 到 `ott9-a3x`；`ott9-a31` 同时是跳板机

---

## 3. 配置文件在哪、怎么切换

`ansible/inventories/a3/group_vars/all.yml`：

```yaml
deploy_config: 1m-v030-lw      # ← 当前档（干净档）
```

可选档（`_active` 里注册的）：

| `deploy_config` | 用途 | 状态 |
|---|---|---|
| `1m-v030-lw` | **干净档**：v0.30 原生 layerwise + mooncake，无 patch / 无 reuse | ✅ 已验收 |
| `1m-v030-hyb` | 实验档：带 buffer reuse、诊断挂载、`max_num_seqs=1` | ⚠️ reuse 不可用，勿当基线 |
| `1m-v030` / `1m-v030-hyb-1m` / `1m-v030-hyb-tp8` / `1m-v030-poff` | 其它 TP / offload 变体 | 未在本次验收范围 |
| `_200k` / `_1m` | v0.23 时代的老档 | 历史参考 |

**干净档的关键参数**（`_1m_v030_lw`）：

```yaml
layerwise:
  enabled: true
  prefetch_layers: 2
  max_transfer_blocks: 64
  max_transfer_bytes: 16777216
prefill: { tp_size: 16, dp_size: 2, max_num_batched_tokens: 4096, max_num_seqs: 64 }
decode:  { tp_size: 16, decode_context_parallel_size: 16, enforce_eager: true }
max_model_len: 262144
hixl_preset: roce          # ★ 必须，见 §6
extra_env: { HCCL_INTRA_ROCE_ENABLE: 1, HCCL_INTRA_PCIE_ENABLE: 0, ... }
```

它**不渲染**这三个键（渲染出来就说明档配错了）：
`use_layerwise_buffer_reuse` / `layerwise_num_shared_buffers` / `layerwise_independent_layers`。

---

## 4. 部署流程

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False

# 1) 生成（改了 group_vars 或模板后必须重跑，否则 start 用的是旧脚本）
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml

# 2) 起容器 + 起引擎 + 等就绪 + 冒烟（约 12 分钟）
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
```

`start.yml` 会：重建 4 个容器（挂载 ACL 日志目录）→ 起 mooncake master（HA）→ 起引擎 → 等 `/health` → 等 proxy `/healthcheck` → 冒烟。

**启动期的正常现象**：P 先起、D 后起；proxy 的 `until` 重试若干次；`PLAY RECAP` 里应全部 `failed=0`。

---

## 5. 验证清单（每次部署后必做）

> ⚠️ **这些 curl 必须在跳板机 `ott9-a31` 上执行**。控制机（本仓库所在机器）到
> `172.168.4.x` 是不通的，只在控制机上跑会一直挂住。
> 在控制机上请统一用 `ssh root@ott9-a31 '<命令>'` 包裹，例如：
> `ssh root@ott9-a31 'curl -s http://172.168.4.237:8000/healthcheck'`

```bash
# ① 4 个引擎
for h in 172.168.4.237 172.168.4.31; do curl -s -o /dev/null -w "$h:9081 %{http_code}\n" http://$h:9081/health; done
for h in 172.168.4.230 172.168.4.226; do curl -s -o /dev/null -w "$h:9900 %{http_code}\n" http://$h:9900/health; done
# 期望：四个都是 200

# ② proxy
curl -s http://172.168.4.237:8000/healthcheck
# 期望：{"status":"ok","prefill_instances":2,"decode_instances":2}

# ③ 真实请求（短 + 长）
#   短请求验证链路通；长请求（>4096 token）验证 chunked prefill + KV 池跨 chunk
curl -s http://172.168.4.237:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"glm-52","messages":[{"role":"user","content":"用一句话总结：KV 池按 block 存 KV。"}],"max_tokens":16,"temperature":0}'

# ④ 两个 decode 实例都在干活（不要用日志 mtime 判断，D 不一定按请求写日志）
for h in 172.168.4.230 172.168.4.226; do
  printf "%s success=%s\n" $h "$(curl -s http://$h:9900/metrics | grep -E '^vllm:request_success_total' | awk '{s+=$2} END{print s+0}')"
done

# ⑤ 日志无错
for f in p0 p1 d0 d1; do
  L=/mnt/sfs_turbo/g00832294/logs/vllm_$f.log
  echo "$f ERROR=$(tail -2000 $L | grep -ac ERROR) TBE=$(tail -2000 $L | grep -ac 'TBE Subprocess')"
done
# 期望：全 0
```

**判据**：①②③ 全过 + ④ 两个实例计数都在增长 + ⑤ 全 0 才算部署成功。

---

## 6. 两条硬约束（踩过的坑，别重犯）

### 6.1 `max_model_len` 不能给 1M

不挂 reuse 补丁 → KV 预算不做放大。TP16/1M 需要 **54.14 GiB**，可用只有 **37.4 GiB**：

```
ValueError: To serve at least one request with the model's max seq len (1048576),
(54.14 GiB KV cache is needed, which is larger than the available KV cache memory (37.4 GiB).
→ RuntimeError: Engine core initialization failed.
```

**当前取 262144（约 13.5 GiB）**。若要更长上下文，先算 KV 预算，别再拍脑袋写 1M。

### 6.2 `hixl_preset: roce` 不能缺

缺了会退到 hccs P2P，同机不同 device 建链失败，**第一个请求就 segfault 打死 P**：

```
Failed to auto connect ... remote_engine:172.168.4.237:20276
aclrtIpcMemImportByKey failed. return[107000]
halDeviceDisableP2P failed, drvRetCode=17, phyIdSrc=2
Create failed, local=[protocol=hccs ... devPhyId=0], remote=[... devPhyId=2]
!!!!!!! Segfault encountered !!!!!!!
[ERROR] TBE Subprocess[task_distribute] raise error[], main process disappeared!
```

配套 `extra_env`：`HCCL_INTRA_ROCE_ENABLE=1`、`HCCL_INTRA_PCIE_ENABLE=0`、
`ASCEND_LOCAL_COMM_RES={"version":"1.3"}`、`HCCL_BUFFSIZE=1024`、`ACL_OP_INIT_MODE=1`、
`PYTHONHASHSEED=0`、`OMP_PROC_BIND=false`、`OMP_NUM_THREADS=10`。

---

## 7. 常见故障速查

| 现象 | 判据 | 处置 |
|---|---|---|
| 引擎全 000、proxy 全 503 `All connection attempts failed` | P 进程已死 | 看 `vllm_p0.log` 尾部 + `logs/ascend/p0/run/device-*/` |
| 启动即 `Engine core initialization failed` + `KV cache is needed` | 上下文太长 | 见 §6.1 |
| 首个请求 segfault + `TBE Subprocess ... main process disappeared` | 传输建链失败 | 见 §6.2 |
| 输出乱码但引擎不崩 | KV 内容错 | 当前档不应出现；若出现在复用档，见 `reuse-buffer-debug-status.md` §11–§13 |
| D 起不来（P 正常） | D 挂到了 producer 语义的 `pool_worker` | 检查 `debug_mounts_decode`，干净档应完全没有 mount |
| 部署完配置没生效 | 忘了跑 `gen.yml` | 重跑 gen 后看 `ansible/generated/run_dp_template_*.sh` |
| 磁盘涨得快 | `vllm_*.log` 巨大 | 重启前 `: > /mnt/sfs_turbo/g00832294/logs/vllm_*.log` |

**配置自检**（确认干净档没混进 patch/reuse）：

```bash
F=ansible/generated/run_dp_template_p0.sh
grep -cE 'use_layerwise_buffer_reuse|layerwise_num_shared_buffers|probe/patch' $F   # 必须是 0
```

---

## 8. proxy 的选路逻辑（排查负载倾斜时看）

- 两段式、**每个请求独立选**：先选 prefill，转发拿到 `kv_transfer_params`，再选 decode；
- 都是**最小负载堆**：prefill 优先级 = `active_tokens + active_kv_cache*0.3`，decode 优先级 = `active_tokens`；
- prefill 分数 = `request_length/4*0.0345 + 120.0745`，decode 分数 = `request_length`；
- 堆元素为 `(priority, index, server)`，**平局按索引** → 两个 decode 都空闲时总选 D0；
- 实测 D0:D1 ≈ 19:8（顺序请求几乎全落 D0），属设计行为，不影响正确性；
- 支持 `/instances/add`、`/instances/remove` 动态增删与 `tainted` 隔离。

---

## 9. buffer reuse 的现状（重要，避免重复踩坑）

**结论：在 mooncake 这个 block_key 数据面上，共享槽位的 buffer reuse 不能用。**

- 曾实现过「24 槽共享 + 按步前缀重载」，单 chunk 正确、多 chunk 全错；
- 根因是**传输完成语义**：ranged 写发出后，源 HBM 槽位何时可以覆写没有保证；
  只有每层 `torch.npu.synchronize()` 才正确，代价是 4.2K 请求 5.9s → **50s**；
- 已逐条排除：restore 逻辑、key/地址/索引、`put` 返回语义、额度窗口/距离、跨层同步；
- 唯一"正确但无收益"的配置是 `num_shared_buffers=61`（等于不共享）；
- 完整证据链：[reuse-buffer-debug-status.md](reuse-buffer-debug-status.md) §11–§13。

**因此干净档不带 reuse**。要重启这件事，先读上面那份文档，别从零再猜一遍。

---

## 10. 相关文档索引

| 文档 | 内容 |
|---|---|
| [layerwise-mooncake-clean-profile.md](layerwise-mooncake-clean-profile.md) | 干净档参数表与自检命令 |
| [reuse-buffer-debug-status.md](reuse-buffer-debug-status.md) | buffer reuse 的完整排查与结论 |
| [architecture.md](architecture.md) | 方案总体架构（mooncake HA、proxy、ansible 结构） |
| [a3-glm52-configs.md](a3-glm52-configs.md) | 各配置档与官方参考的逐项对照（部分内容早于 v0.30） |
| [a3-layerwise-kv-offload.md](a3-layerwise-kv-offload.md) | layerwise KV offload 设计说明 |
| [subagent-context-budget.md](subagent-context-budget.md) | 排查类任务的上下文节省方法 |

---

## 11. 变更记录（本方案相关的最近提交）

```
9ddec01 chore(config): 删除 all.yml 尾部重复定义（清理重复的 _1m_v030_hyb_tp8 等）
be1b3dd fix(config): 1m-v030-lw 补齐可部署所需的上下文长度与传输层预设
25e1bea docs: 记录 buffer reuse 根因定位（§11-§13）与 subtask 上下文预算方法
5a7251f feat(infra): 持久化 ACL 设备日志——容器 /root/ascend/log 挂到共享盘
c791182 feat(config): 干净档 1m-v030-lw——v0.30 原生 layerwise+mooncake，不含 reuse
```
