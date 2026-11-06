> **2026-11-05：32K／64K完整层计算、GET／PUT和槽位时序实测完成。** B4/P3的prefill中位数9.585／20.419s，相对同计算配置pure约+0.15%／+0.51%；64K其余77层GET直接Free合计0.39ms，第0层约69ms。pure的SFA仍约12.46s／64K，进一步大收益应看计算路径。48/48请求有效，原P r05／D r03身份、源码、健康和新短请求恢复PASS，r18停止；717f6e6e0／原p-main-buffer-reuse未改，无生产源码提交。详见[完整报告](prefill-reuse-layer-overlap-32k-64k.md)与runbook §11.43。

# M1（P 侧 KV Buffer Reuse）工作总览

> **2026-11-04 固定B2/P1细分归因完成**：r17诊断overlay在剥离包装后AST与717f6e6e0基线一致，6项行为CPU测试和1000组区间oracle通过。两对128K关闭/开启探针及独立TP0 CANN、32K控制等12请求全部有效；P0 16TP共64份轻量记录，无丢记录。新profile的10.361秒Free中9.553秒落在native GET，地址/FIFO/reader等待直接暴露很小。21个带Indexer层贡献7.402秒GET暴露；确认独立Indexer仍随Main恢复，block-key分支绕过GVA的independent驻留过滤。下一优先项是证明previous-step块映射驻留后过滤独立组件，保留外部prefix完整加载；尚未实现，不能把7.402秒全部当可省。原四节点身份/源码/健康/新单token恢复与证据审计PASS，r17停止，原源码分支干净。详见[边界实测结果](prefill-reuse-boundary-trace-results.md)。


> **2026-11-04 4K–128K性能对照**：原始Ansible `1m` 经旧参数兼容迁移后（删除不支持的reshape键，Indexer C8改为显式INT8），DCP16主基线与当前Main B2复用配置各30个正式样本全部正确，零prefix hit／无抢占。单并发P-only首token，prefill中位数依次为原始0.60/0.82/1.62/3.30/6.76/14.19s、reuse1.18/2.30/4.67/9.58/20.80/56.42s，即延时+96.3%/+181.2%/+187.7%/+190.3%/+207.7%/+297.5%。这是包含DCP/chunk/MTP/精度等差异的整体配置对照，不是共享槽净开销；辅助1m-v030长输入错误，不用于主结论。计时、原四节点身份／源码／健康／新短请求恢复审计及离线证据审计均PASS，r15已停止退役，D和Master未动；本轮无生产代码修改。后续只读分析确认原DCP档还启用prefill DSA-CP token分片，复用存在逐层PUT／读者等待，128K跨chunk历史恢复有放大；尚无同配置消融／profiler，未给各项分配耗时占比。详见[完整报告](prefill-reuse-dcp-performance.md)。


> **2026-11-04 两分支代码整理**：按用户要求暂不继续64K定位。远端P `06e16d717`已被receipt `6c233175d`包含；新增独立只读评审worktree `src/vllm-ascend-receipt-review`，未合并或部署。两分支各139项聚焦CPU测试、receipt 27项账本测试通过，但源sender接线、D真实runtime／IPC、Host目标数据面、取消回收和目标链路验收仍未完成。完整代码地图、分工及先后顺序见[源码仓库实现清单](../src/vllm-ascend-reuse/PD_TRANSFER_IMPLEMENTATION_STATUS.md)。

> **2026-11-04 receipt交接代码核对**：远端feat/layerwise-receipt已fetch并固定6c233175d，TargetProvider核心与本地相同，真实TargetRuntime／跨进程facade／挂载仍缺；retention仅补字段签发。另以原27项UT及真实方法CPU复现，确认普通模式误入receipt、空backing契约冲突、逐层覆盖／失败账本、terminal失败清理顺序、visibility缺失时错误放行、admission前通知和request ID清理等问题。详见[receipt实现缺口核对](receipt-handoff-gap-audit-20261104.md)。没有merge、checkout或部署该分支；原服务恢复审计已通过。

> **2026-11-04 64K隔离验证**：原失败在同源码／同4112档多次重放通过；随后在Main全独立对照，以及彻底移除KV transfer的纯计算对照中，捕获同一65532-token样本的HTTP400 NaN。后者三次随机cache_salt冷重算的第3次失败，prefix hits均为0，因此这条NaN复现路径不需要共享槽或Mooncake。仍未确认最初`!`／`I`与它同因。原样本38400次prefill MoE实测未超8192预算；逐层探针下尚未复现。没有生产修复，尚未定位具体算子；已恢复原P r05／D r03，四节点身份与manifest、健康和新8K／64K冷请求均通过，诊断容器停止保留；详见[64K定位记录](p-main-buffer-reuse-64k-diagnosis.md)。

> **2026-11-04 既有流程真实回归**：当前源码`06e16d717`的r08候选已完成32K P/D检索、持续生成、并发、前缀及断连后请求，已测旧P/D用例通过，两台D均有成功流量。但64K P独立检索在P0/P1分别返回`!`／`I`（应为4），整轮不是全绿，根因尚未隔离。receipt未启用，不是新TargetProvider运行时验收。已恢复原P r05／D r03，四引擎健康，新8K及同一64K提示词正确；代码与配置同时不同，不能单独归因。详见[回归报告](target-provider-existing-flow-regression.md)。

> **精简Ansible入口**：[a3-reuse/usage.md](../ansible/inventories/a3-reuse/usage.md)，从原A3复制整理，移除历史档位，默认chunk32784／P模型长度262144，普通D，固定r05／r03镜像；本地生成和语法检查通过，未按新inventory实际部署。

> **2026-11-04 TargetProvider开发**：源码已提交`06e16d717`，D目标四方法核心、既有DSA注册布局适配和17项新UT已实现，聚焦测试共139项通过；共享契约按对方固定SHA原样导入。真实owner runtime／scheduler跨进程接线尚未完成；既有流程回归后已恢复r05，详见上方实测结论及源码仓库`PD_TRANSFER_HANDOFF.md` §11。

> **后续PD接入开发中**：源码提交`47f983157`新增源视图／槽位代次／ready协议、receiver写入前接线与显式restore-only入口；已构建独立r07并通过64K模型及受控读者硬件验证，收尾已恢复r05并通过短请求和健康检查。
> r05的1M证据仍对应此前已验收代码，不代表这些新增接口已有1M硬件验收。接口与限制见[PD接入准备](p-main-buffer-reuse-pd-integration-preparation.md)。

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

## 当前活动目标更新：A1/A2 target owner（2026-11-04）

64K 数值定位按用户要求暂停。已在独立集成工作区
`src/vllm-ascend-pd-integration` / `reuse-pd-integration` 实现实际资源持有、目标可见性和
同步 release，及既有分配/TE 注册 catalog、稳定 facade 和纯数据 IPC 适配入口。
40 项相关 CPU、139 项 P 聚焦、27 项 receipt 账本、16 项 TE/HostPool 与 4 子用例通过。
最终源码快照 TP1/TP2 的真实 HostRegister/TE/NPU stream 目标资源验证通过，实际
vLLM scheduler 的分配、ref_cnt、decode 追加与 retirement 验证通过。

完整模型 PD 数据面、生产 IPC/free/cancel/preempt 接线和远端 DMA 回收仍由 B/J 推进；
不据此宣称完整端到端可用。原 P r05 在原生目标测试期间未停；r14 已停止、原 D r03 已恢复健康。
恢复审计用错多 token P 请求触发原 r05 Store prefix 错误，已恢复同 ID 的 P 双节点，
按原 max_tokens=1 的 P-only 模式重测均返回 4；四引擎健康/空闲及源码/身份审计通过，
不据此宣称旧 P 多步 decode 已通过。
详情见 [A1/A2 实现与验证记录](target-owner-runtime-a1-a2.md)，源码仓接口说明为
`TARGET_OWNER_RUNTIME_HANDOFF.md`。

2026-11-04 分支归并：用户要求继续在原分支交付。A1/A2 和已测 receipt 基线已统一回 `src/vllm-ascend-reuse` 的 `p-main-buffer-reuse`，不改写原 P 历史。生产源码及原生测试脚本的 733 项摘要匹配 TP2 通过快照；40/139/27 项 CPU 与 16 项 TE/HostPool（4 子用例）回归复跑通过，未操作集群。当前交接与验证报告均在该源码仓，原隔离树仅供历史归档。

分支归并收尾：A1/A2、测试、交接/计划/验证文档已在原 p-main-buffer-reuse 提交 e817452b2（Signed-off-by），工作区干净；临时集成目录的16文件未提交副本已备份并stash归档。全量格式检查有继承的基线失败，本次变更manual hooks全部通过。未由本轮执行push；最终只读查询显示远端已包含同一e817452b2提交。


2026-11-05：Mooncake 池化五部署档冷／跨 P 命中比较采集结束（32K／64K，P-only 输出 1）。76 次请求尝试、75 条结果，73 条有效（54 条正式）；B2/P1 的 64K 混合序列阻塞，隔离复测冷预热完成后远端读取仍达 300 秒截止，未取得正式数据。B4/P3 全部 16 条通过，冷／命中中位数为 32K 9.586／5.159s、64K 20.444／10.352s；相对全独立 Main 的冷开销约 0.04%／0.53%，不能把原始 1M 档的整套差距都归为 buffer reuse。200K 原档 .92 MTP dummy OOM，兼容 .88 复测单列。完整配置、时延与失败证据见 [池化性能报告](mooncake-pool-deployment-performance.md) 和 runbook §11.44。原 P r05／D r03 四节点 ID／image／owner／脚本、各 10 源文件、健康空闲及两个 P 新单 token 数学请求恢复审计 PASS；r19 已停止，D 请求计数未变、D／Master 未重启，proxy 维持未运行。冻结分支／HEAD／732 源文件与实际 733 文件快照审计 PASS，生产 inventory 未改。证据审计 PASS 的范围是记录／源码／恢复完整性，B2/P1 的 64K 稳定性仍为 FAIL。


## 当前活动目标更新：完整 PD transfer（2026-11-05）

Mooncake 五档性能测试结束、原环境恢复后，已 fetch receipt 最新 `fa5aec40b`，在原
`src/vllm-ascend-reuse`／`p-main-buffer-reuse` 合入 12 个新增提交，继续完整 PD 补齐工作。
核对发现 fatal metadata 字段顺序、HTTP writer plan 序列化、错误 scheduler 绑定、D 容量
误判 restore-only、partial tail、source fence 覆盖及 retirement 失败仍放行等问题，已修复。
独立进程的值类型 RPC 与反向 retention 运行测试已加入；尚未接到生产 factory。
P 回归141项、receipt37项和组合61项（24子用例、另1项运行时限定skip）通过。
完整 Host目标传输、TP16 IPC、精确 receipt／worker ready、真实消费／写回、取消／抢占
与 P 无 forward 路径仍需接线，不能宣称完整模型已通过。最新清单见源码仓
[PD_TRANSFER_COMPLETION_REVIEW.md](../src/vllm-ascend-reuse/PD_TRANSFER_COMPLETION_REVIEW.md)。

本轮集成基础修复已在原 `p-main-buffer-reuse` 提交 `9cbdde548`（Signed-off-by，双亲717f6e6e0／fa5aec40b），源码工作区干净，未push。实际NPU运行栈的候选739个Python源码内存导入、receipt／普通标准hook、3项真实sender方法的CPU fence测试通过；没有替换服务文件／启动模型或DMA。复核原四容器ID／image／owner和各10源码摘要一致，健康200、running／waiting均0。变更manual hooks全通过；全仓format.sh ci在隔离副本因既有ascend_config／sfa_kv_offload问题失败，未污染交付源码。基础验证摘要foundation-validation.json明确标记完整模型功能／性能NOT_RUN，生产RPC factory未接，活动目标继续。


第二阶段生产接线（2026-11-05，完整PD目标继续）：新增ReceiptScheduler与串行KVCacheManager适配器，反向retention RPC和实际分配／free使用同一实例锁；标准GPU fence先于target release，覆盖正常结束/取消延迟free、抢占、强制prefix reset和接收失败。新role factory接入connector/MultiConnector/model_runner初始化；D注册选择已有HostMain+HBMIndexer，不再走进程全局rendezvous；新增全TP原始lease和TargetManifest、HTTP传播及本地lease选择。真实远端静止proof、传输计划、SourceReady直接发送、worker-ready、消费写回和restore-only仍待接线，缺proof时分配保持。P141、组合67项/33子用例通过，1项运行时依赖skip；实际vLLM scheduler/KVCacheManager四条CPU控制路径通过，不代表模型/DMA通过。原环境未切换，证据在pd-transfer-production-wiring-20261105，源码最新说明PD_TRANSFER_COMPLETION_REVIEW.md。

第二阶段收尾：原 p-main-buffer-reuse 已提交 `36e6aa4d3`（Signed-off-by），21文件，工作区干净，未push。743生产Python源码摘要与最终native-stack内存导入快照一致；真实依赖下6项direct/binding/finished hook、3项sender fence、1项CPU真实存储/录制TE的注册选择，以及4条真实vLLM scheduler控制路径通过。Native注册范围测试首次因harness缺少Mock名称失败，修正后通过并保留原日志；不是服务故障。变更manual hooks全部通过；全仓CI继承未改动基线失败，隔离格式化7文件与本轮交付无交集。原四容器身份/image/owner与各10源码摘要匹配，健康200、running/waiting均0，未切模型部署。validation.json明确标记真实新TE注册、完整模型功能与性能NOT_RUN，真实远端proof producer/数据面plan未接，完整活动目标继续。


第三阶段目标计划与接收确认（2026-11-05，完整PD目标继续）：已补manifest逐entry传输计划及请求级预编译索引，基于既有DSA mapper校验实际MR、页/stride/group/block/tail；worker本地解析真实地址仅走peer数据面，不进入scheduler IPC。同一真实Host backing支持多段不重叠注册，复用bounded DSA packer。新增精确entry覆盖/独立静止判据；scheduler通知P前等待所有TP owner安装ledger和request映射，重复metadata不重置结果，部分admission失败保留目标。真实receiver已校验带明确DMA静止bool的terminal和完整generation，acquire后校验失败保持lease可追踪。CPU组合119项/46子用例通过、1项运行时限定skip，P141通过；原P独立进程747生产Python内存加载、3条实际receiver/early-admission方法、既有hook/fence/注册选择与实际vLLM scheduler控制用例通过，没有模型计算或远端DMA。生产SourceReady直接writer、exact epoch、真实可见性/use/写回、all-rank readiness/free proof、取消抢占与restore-only仍待完成；完整模型功能/性能NOT_RUN。原服务未切换。源码最新说明PD_TRANSFER_COMPLETION_REVIEW.md，证据pd-transfer-data-plane-20261105。

第三阶段收尾：原 p-main-buffer-reuse 已提交 `aca80d8ef`（Signed-off-by），20文件，工作区干净，未push。747生产Python源码摘要与最终native-stack内存加载快照及commit逐文件一致；P141、组合119项/46子用例（另1运行时限定skip）、legacy receipt37项/4子用例通过。实际依赖下3条精确receiver/提前admission方法、6条hook、3条sender fence、1条CPU注册选择和4条真实vLLM scheduler控制路径通过，负向帧错误日志来自注入测试，不是服务请求失败。变更manual hooks全通过，全仓CI仍因未改动基线lint/拼写失败，隔离副本自动格式化7文件与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要匹配，健康200且running/waiting均0，未切部署。validation.json明确真实新注册/远端DMA/模型功能与性能NOT_RUN；SourceReady直接writer、真实receive/use/writeback及全rank静止proof仍待接线，完整活动目标继续。


第四阶段SourceReady writer与exact epoch（2026-11-05，完整PD目标继续）：manifest请求保留manager block和原始目标端口，SourceReady直接登记全部read job，在已有NPU sender线程调用batch_transfer_sync_write，Host/device分批，Main只由P0写、Indexer逐rank写；旧save hook排除manifest防止双发，混合batch保留普通路径，未接Store registry时显式拒绝。source consumer改为ready+sealed+所有exact epoch读者完成才可复用，按slot高水位支持旧Indexer重复等待；普通sender也回调确切ticket。原生完成先释放源读者，独立proof线程批量发送逐entry结果并复用REQ socket，不等待D ACK占槽；未知原生异常保持读者。worker完成后完整已登记计划排空再清理，部分计划按cancelled退出且不提交队列中未开始的DMA；这不等于已完成D→P取消协议。CPU组合181项/50子用例（1 runtime skip）及P141通过，真实ZMQ与CPU实际字节/页尾/fence/ACK阻塞/失败/取消测试通过；原P独立进程748生产源码内存导入、9条真实准备/receiver/提前admission方法及既有hook/fence/注册选择和实际scheduler控制路径通过。没有真实远端WRITE/新注册或模型运行，未切服务。D真实可见性/use/写回、全rank readiness/release proof、晚到proof清理、远端取消和restore-only仍待接通，完整模型功能/性能NOT_RUN。证据pd-transfer-source-writer-20261105，源码PD_TRANSFER_COMPLETION_REVIEW.md。

第四阶段收尾：原 p-main-buffer-reuse 提交 `4c455c943`（Signed-off-by），12文件，工作区干净，未push。组合最终182项/50子用例（1运行时限定skip）、P141通过；748生产Python源码与最终native-stack内存加载及commit逐文件摘要一致，9条准备/receiver/提前admission方法及既有hook/fence/CPU注册选择和四条实际scheduler控制路径通过。已补已封口target代际重放拒绝，以及peer操作耗尽重试后不再逐层重复超时。变更manual hooks全通过；全仓CI仍为未改动基线lint/拼写失败，隔离自动格式化7文件与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要匹配，健康200且空闲，未切模型部署。新增真实尺寸元数据probe：TP16×78层、32K prompt、block128、256请求blocks的合法manifest在现有owner codec触发traversal limit，已复现，尚未修复；需要压缩重复block map后再进D/模型接入，不上调限制绕过。真实新注册/远端DMA/完整模型功能与性能NOT_RUN，完整活动目标继续，证据pd-transfer-source-writer-20261105。


第五阶段D消费/回收接线（2026-11-05，完整PD目标继续）：新增exact原生完成到真实D stream依赖的本地桥、整个execute_model的owner-use及写回尾部fence、全部TP owner就绪门禁和基于静止proof的ReleaseAck。接收完成/失败仍保留内部请求身份、ledger和block map至owner retirement，避免迟到失败遗失与外部ID重用串线；失败通过真实vLLM failed_recving按请求报告，涵盖已经RUNNING的retained请求。关闭地址lookup gate，首次lookup前关闭在P按无native读取取消；已缓存地址的D→P远端取消仍未完成。owner/HTTP wire v2对重复block map引用，保持8MiB/500k node限额并兼容读取v1，TP16×78层32K/64K/128K元数据往返通过。组合CPU284项/76子用例（1 runtime skip）、P141通过；原P独立进程749生产源码内存导入、15条真实connector方法与7条实际scheduler控制路径通过，最终源码摘要匹配。变更manual hooks全过；隔离全仓CI因未改动基线lint/拼写失败，7项自动格式化与交付无交集。原四容器身份/image/owner/脚本/各10源码摘要和健康空闲匹配，没有切换模型服务。真实新注册、远端WRITE、完整模型功能/性能NOT_RUN；远端取消、restore-only调度及跨owner部分release失败的请求隔离仍待完善。证据pd-transfer-decode-wiring-20261105，源码PD_TRANSFER_COMPLETION_REVIEW.md。


第五阶段提交4204f545d（原p-main-buffer-reuse，未push）。独立r20容器实际运行生产worker注册与ManifestSourceWriter，目标注册5个MR，P两层源job都返回成功/静止，D执行prepare_decode并发布；原生日志roce_mode=true、use_fabric_mem=false。整轮FAIL：P在engine disconnect后的清理/退出阶段报malloc(): unsorted double linked list corrupted（-6），父进程断言后139退出。目标字节读取及后续use/writeback/retirement尚未执行，不能记PASS；根因未定位。控制监督已在finally恢复原D容器对，P未停，原D启动后健康审计仍在等待；终态看pd-transfer-native-write-20261105/supervisor-state.json及restoration-audit.json。完整模型功能/性能NOT_RUN。


第六阶段原生小探针（2026-11-05）：使用4204f545d的749生产Python源码，TP1/两层/23有效tokens同机NPU1源进程→NPU0目标进程，实际bounded注册5个MR，生产ManifestSourceWriter及真实ZMQ地址/回执路径完成device RoCE WRITE；D通过exact账本建立真实receive依赖并发布。全部8个Main/Indexer entry、partial尾部及未写区域字节正确，跨层复用Main源buffers；native消费/在途manager写回阻止提前释放，完成后release/retire/unregister/pool close通过，源/目标正常退出。显式保留tensor/context并先校验再析构TE避免了本轮退出崩溃，但尚未隔离具体free地址或修改生产shutdown，不能证明HBM double free。未跑真实GLM forward/decode、TP共享身份、跨节点WRITE或模型功能/性能。入仓测试receipt_transfer_npu.py与实测脚本非import AST一致（仅Ruff格式/排序/unused清理）。r21曾因准备检查早于复制完成而触发多余D重启，无NPU提交，随后原环境恢复；新监督同步prepare/hash/imports后才stop，准备失败仅清理候选，5条生命周期CPU回归通过。r22探针PASS后正在finally恢复原D对，P未停，终态看pd-transfer-native-order-20261105。功能审查六项缺口继续补齐后再做正式完整模型验收。

### 2026-11-05 Ansible 实机复现 r22

原始 r22 探针已在独立 Ansible 容器复现通过，750 个源码及探针哈希与原始记录一致。
真实 TP1 device RoCE WRITE、8 个目标 entry 的字节校验及写回／回收通过。随后在隔离 Ansible
候选上尝试完整 `4204f545d` 模型启动，证据及门禁结果见 runbook §11.52。

接续修复了首请求的 ReceiptScheduler 覆写签名，按实际 vLLM 镜像传递节流参数。
镜像内 scheduler 控制回归 8 项通过，完整模型重跑见 runbook §11.53。

实机短请求及 5,577／5,578-token 冷请求与重放已通过，D0 全 16 TP receipt 和目标释放
按请求核对，重放 Store 命中 5,504 token。已修复 P 请求收尾扫描 Store 列表型元数据的问题。
三 chunk、实际逐层 GET、并发和异常生命周期仍在继续验证，详见 runbook §11.53。

三 chunk 的 8,856／8,857-token 冷／重放请求、78 层实际 Store GET、D0 的 16 rank
receipt 与释放均通过；重放命中 8,832 token。并发短请求在 D0/D1 分别完整通过。
取消审计已切换到请求级日志，地址发布前后的取消边界继续验证，见 runbook §11.54。

后续实机重跑再次确认三 chunk 和并发。并发审计曾在采集到 D1 释放日志前超时，
原始日志已按两个请求 ID 确认 16 rank receipt 和释放，取消用例由下一候选补跑。
另已在 CPU 和镜像控制测试验证 D 关闭目标的 ACK 会让已有地址缓存的 P writer
跳过后续写入，完整模型实测待进行。

关闭 ACK 候选已通过三类实机取消，含 Decode 首 token 后断连、地址查询前取消并
迟到通知，以及首笔真实 RoCE WRITE 后保留目标直至终结证明。最后一例 P0 的
16 个 writer 均收到目标关闭反馈，D0 随后回收目标，下一请求完整通过。按
runbook §11.54 接续固定容量的长上下文和普通 HBM 对照。

固定 513 blocks 的 B2/Host 档已通过 4,872、31,754 和 62,474-token
三组冷请求／前缀重放／连续生成，九条均确认 D0 16 rank receipt 和目标释放。
62K 重放命中 488/488 Store blocks。旧逐层 P/D 的 HIXL 超时作为旁路故障
单独记录。普通 HBM 对照按用户口径使用纯一体推理，排在 B1 之后，
证据见 runbook §11.55。

B1 的三 chunk、双 D 并发、Main 和 Indexer 分别延迟的实机验证均通过。
P 记录了对应源代次的等待和退休，D 在门闩期间保持未发布，解除后两条
请求均收齐 16 rank 并回收。

固定 513 blocks 的纯 HBM 对照直接请求原始一体 vLLM，关闭 KV transfer、
Mooncake、Host offload 和共享槽。与 B2/Host 按实际 token 数配对的九条请求
均通过。62,474-token 样本的冷请求／重放 TTFT 为纯 HBM 20.80／0.55 s、
B2/Host 130.28／37.42 s。纯 HBM 重放命中本地 62,464 tokens，外部命中为 0。
分组数据和按节点的峰值 HBM 见 runbook §11.55。

B3 的三 chunk 冷请求和前缀重放、双 D 并发、Main／Indexer 单路延迟及
writer 关闭排空均通过，七条请求各收齐 16 rank 并释放。B1/B2/B3 已在
四节点真实模型上验证 receipt 路径。关闭用例覆盖 native WRITE 成功返回后
源回调仍在途的竞态，自动 restore-only 和卡死 DMA 的安全终止仍待实机验收。
详见 runbook §11.56。

62K receipt P/D 首 token 优化已完成实机对照。确认大头是 D 串行目标登记和
P 的 Mooncake 原生告警输出。按 rank 缩减登记载荷、复用已解析的目标、
减少 D 清单复制，并用 `MC_LOG_LEVEL=ERROR` 与 32,784 chunk，62K 冷
TTFT 从 130.28 s 的初始单样本降至 34.29 s 的三次中位数，回放中位数
15.15 s。D 并行获取的两轮未通过原生链路热身，已撤出工作区。实测阶段、
样本口径及剩余 HIXL 证据见 runbook §11.57。

独立 Master 上的 TP16 并行获取候选已通过九条 62K 完整 P/D 请求和全 rank
回收。首条 D 热身请求为 38.19 s，之后两条冷请求为 27.72／28.31 s，
回放中位数 9.23 s。稳态 D 目标准备约 2.54 s，前轮为 6.39 s。
后续候选进一步削减租约复制，并行登记及回收已进入验证。见 runbook §11.58。

第二候选并行发送各 D rank 回执，同时并行登记、回收并减少租约复制。
九条 62K 模型请求正确且全 rank 回收。冷／回放 TTFT 中位数为 24.55／6.01 s，
冷请求 D 目标准备中位数 1.68 s，D 16 rank 可见性首末相差 12–16 ms。
同 chunk 纯 HBM 已渲染待复测，长生成 TPOT 单独计。见 runbook §11.58。

32,784 chunk、513 blocks 的同输入纯 HBM 已实测。62K 冷 TTFT 中位数
20.03 s，完整 P/D 为 24.55 s。29-token 生成的首 token 后中位时长分别为
5.46 s 和 13.89 s。继续缩减 D 就绪轮询与 lease wire，并采集逐步 decode
和 fused SFA Host 调用计时。见 runbook §11.59。

D scheduler 单次校验取代逐 rank 的每步保留权 RPC 后，九条 62K 完整 P/D
请求全部正确并完成回收。冷／回放 TTFT 中位数为 24.00／5.36 s，29-token
生成的首 token 后中位时长由同探针的 15.40 s 降至 12.87 s。冷 TTFT 距
同 chunk 纯 HBM 3.97 s，D 目标准备仍占约 1.36 s。模型调用与步间开销
仍待拆分。D 异步调度已单独渲染候选。见 runbook §11.59。

D 异步调度候选也通过九条 62K 请求和全 rank 回收。冷／回放 TTFT 中位数
为 23.98／5.28 s。29-token 生成的首 token 后中位时长为 8.91 s，比带同类
探针的同步候选下降约 31%，纯 HBM 为 5.46 s。异步实现留在独立实验快照。

异步 scheduler 已接入 receipt 源码及 Ansible profile。随后 D 本地清单发布
直接使用协调器持有的快照。九条 62K 实机请求正确并回收，冷目标准备从
1.36 s 降至 0.75 s，冷／回放 TTFT 中位数为 23.31／4.67 s，同 chunk 一体
纯 HBM 冷 TTFT 仍领先 3.28 s。见 runbook §11.60。
