# M1（P 侧 KV Buffer Reuse）工作总览

> **2026-11-04 TargetProvider开发**：源码已提交`06e16d717`，D目标四方法核心、既有DSA注册布局适配和17项新UT已实现，聚焦测试共139项通过；共享契约按对方固定SHA原样导入。真实owner runtime／scheduler跨进程接线尚未完成，当前r05服务未动。详见源码仓库`PD_TRANSFER_HANDOFF.md` §11。

> **后续PD接入开发中**：源码提交`47f983157`新增源视图／槽位代次／ready协议、receiver写入前接线与显式restore-only入口；已构建独立r07并通过64K模型及受控读者硬件验证，收尾已恢复r05并通过短请求和健康检查。
> r05的1M证据仍对应此前已验收代码，不代表这些新增接口已有硬件验收。接口与限制见[PD接入准备](p-main-buffer-reuse-pd-integration-preparation.md)。

> **2026-11-03 新基线核对**：后续工作以 `mc.pdf` 与固定 SHA `4234a1f18` 为准，
> 新工作区 `src/vllm-ascend-reuse` / `p-main-buffer-reuse`，旧分支保留作历史记录。
> 当前新实现已完成开发与目标配置的真实验收，并部署在独立 r03 容器；
> 产物、Python 测试环境及原 `/tmp` 资料均已归拢到当前仓库。最新状态见核对文档 §5。
> 已发现 block-key waiter 注册被 GVA 门控拒绝，首批修补及完整对应表见
> [P Main shared buffer 核对](p-main-buffer-reuse-audit.md)。NPU 哨兵与 B=3 单/双/三chunk、并发同前缀及断连后恢复均已通过；
> B=1/32768 的非整block恢复及 r03 B=2 自动52042 blocks、并发与断连恢复均已通过。
> 当前入口：[运行与验收](p-main-buffer-reuse-usage.md)，配置 `ansible/profiles/p-main-buffer-reuse.yaml`。
> 源码功能提交 `9824ec91c`，格式提交 `f9b995c68`；111项聚焦UT通过，全仓CI遗留问题单独记录。
> 下文“缺 reader 登记即乱码病根”“计算读者已覆盖”“forward 不触发必为 ACLGraph”
> 等属于历史推断，未被本轮证据证明；容量数据也不是正确性验收。

> **P侧独立1M已实际通过**：r05 P0/P1、Main B=2、INT8、chunk32784，完整1048541-token冷prefill耗时3375.79秒，首token正确；同前缀BETA/GAMMA检索均正确。
> 最终健康、错误扫描、D无流量、镜像源码和模板复现审计均通过。D保持r03，master未动。
> 这是prefill＋首token验收，不包括持续decode或P/D端到端1M；r04 SFA历史不代表Mooncake失败。
> 当前配置与证据见[P侧独立1M](p-main-buffer-reuse-p-only-1m.md)。

> 代码交接入口：[当前代码与评审指南](p-main-buffer-reuse-handoff.md)，包含源码版本、文件索引、bundle／补丁和验收边界。

## 以下为旧 M1 实现历史记录

> 生成时间：2026-11-03
> 状态：**容量路径已完成并验证；正确性屏障未打通（按文档口径重新定位后待续）**
> 代码分支：`src/vllm-ascend` → `m1-prefill-reuse`，tag `m1-prefill-reuse-wip-20261103`
> 过程记录：[m1-buffer-reuse-runbook.md](m1-buffer-reuse-runbook.md)（§11.1–§11.16 逐轮证据）
> 设计依据：[mc-layerwise-detailed-design.md](mc-layerwise-detailed-design.md)

---

## 1. 目标与范围

只做 **P（prefill）侧 KV buffer reuse**：把多个层折叠到少量共享 HBM 槽位，从而在同样显存下
放大可用 block 数。D 侧复用、D 侧 Host offload、连接器协议演进不在本次范围（属其他人/后续）。

实现依据是设计文档三处：

- **§2 缺口表**：`layerwise_cache_layout` 接入布局、`extract_layout_config()` 接上、`pool_worker` 复用编排；
- **§2.2 不变量**：I1「写共享槽前必须等上一代所有读者结束」、I3「无远程 GET 也要过屏障」；
- **§6 逐层时序**：第 1 步就是 `MultiConnector._wait_for_external_slot_release(M)`（**Store 侧入口**）。

---

## 2. 已完成（有实测证据）

### 2.1 容量折叠：`num_blocks` 5508 → 32768（5.9×）

| 项 | 结果 |
|---|---|
| 落点 | 模型**不走** glm5next 专用构建器（诊断日志 `glm5next=False`），因此在 `patch_kv_cache_utils` 的默认路径上接管 |
| 折叠方式 | 按页大小分组取最大组为主层 → `build_layerwise_cache_layout` 折叠成槽 → 一槽一个 `KVCacheTensor`（`shared_by` = 该槽全部层）→ `bytes_per_block` 按槽数算 |
| 全 rank 一致 | 用「基线块数 × 逻辑页/物理页，再向下取 2 的幂」满足 `generate_scheduler_kv_cache_config` 的 `assert all(num_blocks == ...)` |
| 实测 | 4 引擎 200，`num_blocks: 32768`，启动无断言失败 |

### 2.2 其他已落地

- `backend/mooncake_layerwise.extract_layout_config()`：从「恒返回 None」改为**显式 opt-in**
  （`use_layerwise` + `layerwise_num_shared_buffers` 同时存在才生效）；
- `worker.py`：槽位感知模型不再做通用 KV 预算缩放（避免 double count），删除按层数估的 fallback；
- 复用门控限定 `kv_role == kv_producer`（只 P 侧折叠）；
- 镜像 `glm52-vllm-ascend:0.30.0-dsabase-m1c`（含上述代码）；
- 镜像分发流程跑通：p0 `docker build` → `docker save` 21G tar 到
  `/mnt/sfs_turbo/g00832294/build/images/` → 其余节点 `docker load -i`。

### 2.3 分支确认

`mazifan0120/vllm-ascend` 的 `dsa_offload_rebase_30` HEAD = **4234a1f18**，与本项目镜像基线一致；
该分支上没有现成的 reuse 实现（只有 `sfa_kv_offload` / `sparse_kv_offload_manager` 的 port 提交）。

---

## 3. 未完成：屏障（I1）没有接通

### 3.1 现象

折叠开启后：单 chunk 正常（1.9–2.1s），**双 chunk 输出乱码**
（`' ThisThis  NONThis'`、`'NON   NON NONThisThis…'`），且
`grep "release physical KV slot"` 计数始终为 **0**（屏障从未真正等待）。

### 3.2 我走错的路线（第 7–11 轮）

把屏障钩子挂在模型 forward 上，全部无效：

| 落点 | 结果 |
|---|---|
| `mla_v1.py` forward | hook=0 |
| `attention_c8_mxfp.py` forward | hook=0 |
| `dsa_v1.py` / `dsa_v41.py` forward | hook=0 |
| DSA forward 里放**无条件 warning**（确认挂载生效、无 PYTHONPATH 覆盖） | **仍然 hook=0** |

结论：服务期的注意力写入不经过 Python forward（ACLGraph / 融合算子），该路线不可行。

### 3.3 文档口径（重新对齐后）

设计文档 §6 第 1 步明确：屏障入口是 **Store 侧**
`MultiConnector._wait_for_external_slot_release(M)`，对应源码里已有的
`kv_transfer.py` 中 `self.external_slot_release_waiter(layer_id)`（约 2138 / 2218 行）。

文档 §4 还逐条列了读者覆盖情况：

| 读者 | 文档结论 |
|---|---|
| 计算读者 | ✅ 间接覆盖 |
| P/D 传输读者 | ✅ `storage_send_done_events` |
| **Store PUT 读者** | ❌ **无**（把 device 指针交给传输层后即返回） |
| Store GET（H2D） | 🟡 部分 |

⇒ 真正缺的是 **§8 的读者登记（SlotEpoch：`reserve_read` / `complete_read`）** 与
**§5.2 的 `assert_reuse_wired()`**，而不是模型侧钩子。

---

## 4. 代码改动清单（分支 `m1-prefill-reuse`，19 个 commit）

| 文件 | 改动 | 状态 |
|---|---|---|
| `models/glm5next/cache_config.py` | 折叠布局、`main_slots`、按槽页大小、2 的幂对齐、诊断日志 | ✅ 生效（容量） |
| `patch/platform/patch_kv_cache_utils.py` | 默认路径接管 + 复用分支 + `_layerwise_reuse_extra_config` | ✅ 生效 |
| `backend/mooncake_layerwise.py` | `extract_layout_config()` opt-in | ✅ 生效 |
| `worker/worker.py` | 不重复缩放、`GLM5_NEXT_SLOT_AWARE_MODEL_TYPES` | ✅ 生效 |
| `distributed/kv_transfer/ascend_multi_connector.py` | `on_kv_cache_write_begin` + provider 诊断日志 | ⚠️ 钩子不触发（路线已废） |
| `attention/utils.py`、`attention/mla_v1.py`、`attention_c8_mxfp.py`、`attention/dsa_v1.py`、`attention/dsa_v41.py` | 写前屏障钩子 | ⚠️ 路线已废，保留待清理 |

---

## 5. 下一步（按文档口径）

1. 先加日志确认 **Store 侧 waiter 在 P 上是否被调用**（`kv_transfer.py` 两处），
   区分「没调用」与「调用了但事件已 set」；
2. 按 §8 实现 store_put / store_get 的 reader 登记（`reserve_read` / `complete_read`），
   这是多 chunk 乱码的直接病根；
3. 补 `assert_reuse_wired()`：启用复用但 barrier 未接线 → 启动失败；
4. 清理第 7–11 轮的模型侧钩子（路线错误，保留只会误导）；
5. 全绿后：重建镜像（p0）→ `docker save` 到 SFS → 其余节点 `docker load -i`。

---

## 6. 环境状态与恢复

**稳定基线（当前）**

- 镜像：`glm52-vllm-ascend:0.30.0-dsabase-m1`
- 配置：`deploy_config: 1m-v030-lw`、`pd_connector: layerwise`、
  `num_shared_buffers: 3` 但**无折叠代码生效**（该镜像的 `extract_layout_config()` 返回 None）
- 无 `debug_mounts`；4 引擎 200；单/双 chunk 均正确

**含折叠的镜像**：`glm52-vllm-ascend:0.30.0-dsabase-m1c`（四节点已 load，可随时切回去做验证）

**恢复命令**

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
```

**注意**

- 验证必须在跳板机 `ott9-a31` 上 curl（控制机到 `172.168.4.x` 不通）；
- 判断 D 侧是否有流量看 `/metrics vllm:request_success_total`，不要看日志 mtime；
- 改动 `group_vars` 后必须先跑 `gen.yml` 再 `start.yml`。

## 当前活动目标：P独立1M（2026-11-03）

按用户最新边界停止SFA路线，开始r05独立P验证；配置、已有失败和验收标准见
[P独立1M记录](p-main-buffer-reuse-p-only-1m.md)。当前未宣称1M可用。
