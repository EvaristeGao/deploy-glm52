# P Main shared buffer：固定基线核对与实现入口

日期：2026-11-03。设计依据：仓库根目录 `mc.pdf` §3–6、§7.4、§8。

> 最终状态见 §5.8 和 [运行与验收](p-main-buffer-reuse-usage.md)；其余保留各轮核对与失败记录。

## 1. 工作区与证据边界

- 固定基线：`4234a1f1815252e8b52c75d6956d0bdf9ae80cc0`，本地 `dsa-base`。
- 新工作区：`src/vllm-ascend-reuse`，分支 `p-main-buffer-reuse`，从该 SHA 建立。
- 旧 `src/vllm-ascend` / `m1-prefill-reuse` 保留，未将其实现合入新分支。
- 只读实查 p0：运行镜像 `glm52-vllm-ascend:0.30.0-dsabase-m1`，没有源码调试挂载。
  安装版本为 `0.30.0rc2.dev319+g4234a1f18.d20261102`，
  Mooncake NPU 包为 `0.3.13.post1`。
- p0 安装的 `pool_worker.py` 确认带有 GET 起点修补：`layerwise_offload` 时从 block 0 开始。
  固定基线源码没有该修补；该条件是否在目标路径真正启用还须验证。
- 本轮没有改动集群文件、切镜像、重启服务或启用物理复用。以下静态结论不等于 NPU 验收。

## 2. 布局、hook、事件和完成语义对应表

下列源码路径相对于新工作区，除 runner/attention 外均位于
`vllm_ascend/distributed/kv_transfer/`。

| 环节 | 实际入口或状态 | 已核对事实 | 对 Main-only reuse 的含义 |
|---|---|---|---|
| 配置发现 | `kv_pool/ascend_store/backend/mooncake_layerwise.py::extract_layout_config` | 固定基线恒返回 `None` | 不能仅配置共享槽数就宣称物理复用开启 |
| 物理规划 | `kv_pool/ascend_store/layerwise_cache_layout.py::apply_layerwise_kv_cache_plan` | 会合并 Main，并对同槽的 Indexer 调用 `_merge_specs(..., indexer=True)` | 必须适配为 Main-only；Indexer 保持独立；不得按 page 大小猜组件身份 |
| runner 分配入口 | `vllm_ascend/worker/model_runner_v1.py` 的规划调用及 `_allocate_kv_cache` | 已消费 `KVCacheTensor`、stride 与别名描述 | 复用现有入口，先固定 block 容量验证字节数和地址，不先扩容 |
| worker 数据面 | `kv_pool/ascend_store/pool_worker.py::__init__` | `use_layerwise_transfer` 仅指 GVA；Mooncake 使用 `use_block_key_layerwise` | 不能把 GVA 模式开关当成所有 layerwise 功能的能力标志 |
| worker 复用编排 | `pool_worker.py::_init_layerwise_config` | 构建复用布局及设置 `prefetch_layer_map/layerwise_offload` 的分支仍受 `use_layerwise_transfer` 限制 | 仅开放配置解析和物理分配不足；Store 必须消费同一 Main-only 规划 |
| waiter 注册 | `pool_worker.py::set_external_slot_release_waiter` | 固定基线拒绝 block-key；但 `_start_kv_transfer_threads` 对 GVA/block-key 均创建同类 receiver 并传 waiter | 第一批修补已对齐注册门控；尚未启用复用 |
| MultiConnector | `ascend_multi_connector.py::_configure_layerwise_reuse_completion`、`wait_for_layer_load` | 记录 sink 注册结果；无 sink 时调用 provider 的普通 load hook | 不能假定 fallback 等价于 reuse barrier；复用激活后必须校验真实 sink |
| 层入口 | `vllm_ascend/attention/utils.py::wait_for_kv_layer_from_connector` → Store `wait_for_layer_load` | Store 按 `current_layer` 推进并等待 receiver；无任务时可直接调用 waiter | 必须实测目标 backend、图模式下每层执行；历史 forward 探针没有证明该入口是否可达 |
| 空 GET | `pool_worker.py::_submit_ready_layer_loads` | 有 `reuse_source` 时，即便 `transfer_tasks=[]` 仍提交 `LayerLoadTask` | 保留此语义，不能因没有网络负载就跳过旧占用者屏障 |
| receiver 覆盖前等待 | `kv_pool/ascend_store/kv_transfer.py::_handle_range_layer_task` | 旧层 save event → save failure checker → `sync_save_events` → attention gate → 外部 waiter → GET | 结构可复用，但其中 save event 的设备语义须单独证明 |
| KV-ready | `pool_worker.py::_submit_attention_save` | block-key hybrid 的 gate.on_start 在 attention 入口记录 `sync_save_events` 并提交保存 | 该事件不能证明 attention 后续读取结束 |
| 层后 save | `pool_worker.py::save_kv_layer` | 对不在 `_attention_saved_layers` 的层记录 save event；已提前保存的层不重新记录 | 不能因为 save hook 在层尾调用，就把此前记录的事件当成 attention-done |
| Store 源读取 | `kv_transfer.py::_handle_range_request`（sender）→ backend `batch_copy_put` | 同线程调用 `batch_put_from_multi_buffer_ranges`；Python 包装未给出源静止保证 | 要核对所装 Mooncake native 实现或做受控硬件测试；不能声称“提交即返回”或“返回必完成” |
| Store 发布 | sender 的 `batch_commit` → `batch_put_session_end` | 最后相关层才提交对象；session tracker 跟踪提交 | 槽位轮换不能等待当前对象整组层提交，否则 B=1 可产生循环等待 |
| Store 完成通知 | `_handle_range_layer_tasks` | `finally` 中置 save event；异常存入 `_fatal_error` | event.set 只代表结束通知，调用者必须检查失败，不能当作成功 |
| 外部读取 | `kv_p2p/mooncake_layerwise_connector.py::wait_for_layer_reuse` | 等实际地址映射的槽位事件，之后检查错误；超时才输出 waiting 日志 | 日志为 0 不证明 waiter 未调用；跨 batch/step 的 epoch、封口与旧回调语义仍待核对 |
| GET 范围 | `pool_worker.py::_prepare_layerwise_get_session` | 固定基线从本地 cached tokens 开始；p0 安装版有 block 0 修补 | 共享 Main 必须恢复所需完整前缀，独立层可保留 tail 优化 |
| 私有 partial | GET 使用 `{req_id}_lastblock`；已有 session tracker | 固定 key 本身不能区分请求实例和多个快照版本 | 需 request_instance、snapshot_generation、已提交版本与引用回收闭环 |

## 3. 首批修改及测试

第一批仅修外部 waiter 的数据面门控：GVA 与 Mooncake block-key 共用
`KVCacheStoreLayerRecvingThread`，注册应与 receiver 选择条件一致。
不改变 legacy key-only receiver 的拒绝行为，不开启布局复用。

测试覆盖：

1. 真实 Mooncake worker 模式标志下注册成功；基线复现为返回 False。
2. GVA/block-key 两种路径晚注册都更新已运行 receiver。
3. legacy 非适用路径拒绝注册。
4. block-key GET 严格在旧层 save 同步、外部 waiter 之后，完成事件在 GET 后发布。
5. 外部 waiter 失败时不 GET，receiver 保留并传播异常。
6. 使用可控线程事件证明：外部 reader 未释放时，空 GET 的 load-finished 也不能提前发布。

以上聚焦测试及既有 transfer/session 测试共 **60 个通过**。修改的四个 Python 文件
通过 Ruff lint、format 检查及 `git diff --check`。
`bash format.sh ci` 已尝试，因本机缺少 `pre-commit` 未执行完整仓库检查。

本地 CPU 测试依赖已迁入 `artifacts/p-main-buffer-reuse/imported-tmp/glm52-reuse-ut`，未安装到集群。
Store 测试的轻量 mock 缺少 speculative package 隔离，已补测试支持：加载真实
`dspark_utils` 而不触发所有 proposer；上游 eagle helper 仍用既有方式 mock。
源码 checkout 无生成的 `_build_info`，用与 UT conftest 相同的 A2 元信息启动 unittest：

```bash
python3 tools/run_prefill_reuse_ut.py
```

首次扩展运行三个完整模块共 175 个测试，有 2 个失败及 16 个错误（包括 subtest）：
当前 CPU mock 缺少真实 tensor/`bfloat16` 等能力，不能报告全量通过。
相关范围按上面的独立命令验证；完整 pytest/NPU ST 尚未通过验收。

## 4. 后续实现边界与顺序

1. 固定实际 checkpoint/cache spec，确认 Main、Indexer、scale/tail 与原生别名。
   规划器统一输出 Main 槽位、旧占用者及独立组件；runner/Store 使用同一份规划。
2. 先保持 block 数不变，验证 Main 物理共享、Indexer 独立、slot 字节数和越界校验。
   PP=1、P-only；未核实的混合/压缩/MTP/speculative 组合显式拒绝。
3. 补独立的 attention-done 依赖，覆盖空 GET、无计算恢复任务和非 Store 保存 rank。
   现有有序队列若能证明 Store 源静止，不重复增加同义计数；异步读者须在发布前登记。
4. 对外遵循 PDF epoch/open/seal/failure 契约，先用受控 waiter 测试；激活复用时校验真实 sink。
5. 补完整前缀恢复、Store 不足显式失败、私有 partial snapshot 版本；不改变公共完整 block key。
6. 完成 sentinel、B=1/2/3、慢读者、失败、prefix、多 chunk 测试后，再验证扩容与跨 rank 容量协商。

上述核对不支持把历史双 chunk 乱码归因为单一原因。新的工作区尚未实现完整 shared buffer 功能。

## 5. 继续开发记录（2026-11-03，本地实现待硬件验证）

### 5.1 文件和环境归拢

- 已将 40 项确认属于本项目的旧 `/tmp` 产物迁入当前仓库，未操作其他项目的临时文件。
  清单：`artifacts/p-main-buffer-reuse/tmp-migration.json`；迁移前后路径、方法及状态均有记录。
- Python 环境及入口脚本已迁移并验证，后续测试、下载缓存和临时文件均在 `artifacts/p-main-buffer-reuse/`。
  四个旧 Git worktree 通过 `git worktree move` 迁入 `src/legacy-*`，保留 Git 关联。
- 新增 `tools/run_prefill_reuse_ut.py`，默认从仓库内的测试环境启动并保存独立时间戳日志。

### 5.2 当前实现

- Main-only 规划按 `AscendMLAAttentionSpec` / `AscendSFAIndexerCacheSpec` 识别组件；
  Main 共享槽位，Indexer/scale/复制容量逐层私有。保持固定 block 数先验证，预算使用相同规划计算实际物理页。
- Runner 与 Store 使用后端同一个 `build_cache_plan`；显式共享槽数才 opt-in。
  B 足够大时保持普通行为；实际复用目前明确限定 P、V1、eager、PP=1、CP=1、单 manager group、无 MTP/speculative。
  目标当前日志显示 `glm_moe_dsa`、78 基础层、单 manager group；原配置另有一层 MTP，实验将禁用。
- 每层执行创建不可重置的 `AttentionComputeCompletion`，Store GET 等待旧代 host record 和设备完成，
  再等待外部 P/D；空 GET、跨 step 槽尾、失败和停机均有保护。Store 早期 KV-ready 与计算完成分开。
- `assert_reuse_wired()` 在启动传输线程前检查真实 waiter，无法接线时拒绝启动。
- range PUT/GET/commit 的严格失败模式会传播错误；宽松模式维持原行为。
- scheduler 传递 request instance、partial snapshot generation 和必要恢复长度；
  私有 partial key 不再反复覆盖 `{req_id}_lastblock`。GET 只绑定旧版本，PUT 生成新版本。
- 非保存 rank 使用 scheduler 明确传递的旧版本；step 入口 TP barrier 等待保存 rank 提交后再恢复。
  本地 prefix 而 Store miss/短命中、无 LoadSpec、查询后淘汰均显式失败，不进入未恢复的 attention。
- 公共完整 block key 不变；另一 P 正在写同一公共 key 时，使用该请求的私有不可变 full-block 回退，
  避免持有未填充会话时互等。私有对象清理只在 GET/PUT 队列静止后进行，`remove(key, False)` 尊重现有 lease。
  清理失败保留重试记录。

### 5.3 Store 返回语义的新证据

已阅读 [Mooncake PR #2881](https://github.com/kvcache-ai/Mooncake/pull/2881)，固定参考为合并提交 `f0f84fb`。
其 `Client::BatchTransferWriteRanges` / `BatchTransferReadRanges` 在返回前执行 `operation->wait()`。
源码保存于 `artifacts/p-main-buffer-reuse/mooncake-pr2881/`。

p0 安装的 `store.so` / `libmooncake_store.so` 与迁入 wheel 的 SHA256 一致，记录于
`installed-mooncake-sha256.txt`。对该确切 `libmooncake_store.so` 反汇编，
`BatchTransferWriteRanges` 在 `0x687200` 调用 `ScatterTransferOperation::wait()`，
证据在 `mooncake-pr2881/installed-write-ranges.disasm.txt`。
因此正常 Store PUT 源读取可由已有有序队列的完成事件保护，不凭空增加同义 reader 计数；
失败路径则保留 fatal error 并禁止后续覆盖。硬件 sentinel 已通过，完整请求仍待验证。

### 5.4 验证现状与部署边界

- 最近聚焦 UT 107 个通过；另行运行 metadata/backend 两个完整 unittest 模块共 205 个通过。
- 扩展运行完整 pool_worker 时仍有 KVPP fixture 的 6 个 subtest 失败和多 group 失败策略用例的 1 个错误；
  已在同一测试环境执行固定基线原始 `pool_worker.py`，复现相同 6/1，不能报告全量通过。
  比较日志：`baseline-worker-comparison.log`。
- 模板新增 `enable_mtp`、固定 block 数和 P 失败策略选项；四节点默认生成命令逐 token 与修改前相同，Shell 语法检查通过。
- 已抓取四节点 baseline 容器 ID、镜像 ID、绑定配置和启动文件，备份在 `baseline-runtime/`。
  12:40 左右确认四引擎无运行/排队请求后，停止并保留基线引擎容器；Mooncake master 未停止。
  开发容器使用独立名称、owner label、容器内日志及模型别名形成的新 Store namespace；正在启动服务。
- p0 NPU sentinel：B=1/2/3、6 层、3 轮 chunk，全量写后立即污染源缓冲区，再 GET 验证均通过。
  证据 `sentinel-r01-retry.log`；初次测试因严格要求立即删除而失败（`sentinel-r01.log`）。
  原生 `remove(key, False)` 返回 -706=OBJECT_HAS_LEASE，固定上游 `types.h:324` 已核对。
  测试修正为保留延期清理对象；没有使用 force 或修改 master 默认租约。
- 目标仍是完成开发、部署调试和真实验收；以上本地进展不代表该目标已经完成。

### 5.5 首轮服务验证与两处运行时修正

- Worker 实际布局为78层、Main槽5、私有Indexer21、5508 blocks、849920 bytes/block。
  两个P节点一致；profile预算比例8.212为计算结果，扩容量尚未验收。
- Scheduler接收合并的跨rank规格，不应运行rank局部物理planner；首轮因此报同层多个Indexer。
  修正其仅依据公共逻辑槽规划判定offload，worker/runner仍严格验证实际物理布局。
- 四引擎随后健康200；首个536-token请求给出正确中文摘要，但P收尾卡住，后续长请求超时。
  py-spy线程栈证实主线程重复等待已被receiver复用并clear的前代save event，收发队列均为空。
  修正step收尾在PUT队列drain后跳过这些已消费事件，保留独立层检查和覆写前逐层屏障。
- 新增两项回归后聚焦109项通过。开发P容器已写入scheduler/worker修正并重新启动加载；
  镜像r01尚不包含这两项修正，最终交付镜像必须重建。D与master保持运行，基线容器保留。
- 开启pytest fixtures的扩展运行有13项失败；在内存执行固定基线源码同样复现全部13项，另有两项
  本次已修复的waiter失败（基线共15）。详见runbook §11.20–21，未报告全量UT通过。

### 5.6 服务回归已通过的部分（13:24）

- 统一重启开发P/D后，B=3、5508 blocks：537/5017/9656输入tokens对应单/双/三chunk，输出均正确，
  耗时4.34/6.80/8.38s；P成功3、D成功3且四引擎空闲，确认收尾死锁修复。
- 两个5005-token同前缀请求并发、重复前缀以及客户端关闭长请求连接后再发短请求，输出均正确。
  两个P均有成功请求，P1真实Store命中32/39 blocks；四引擎最终running/waiting均0。
  断连探针不等于内部所有abort分支已验收。证据及时间见runbook §11.23。
- B1/32768初次使用4100预算，触发基线runner的TP padding profiling断言（未分配共享KV）；
  已改为4112并重新加载，仍覆盖partial block。r02源码镜像已在四节点构建并校验10文件SHA，
  引擎尚未运行，后续将验证B2/32768/4112。以上两轮仍待完成。

### 5.7 第三代partial快照的非保存rank更新（13:50）

B1/32768已实际分配成功；537-token短请求与5014-token双chunk正确。三chunk的第三轮在非保存rank
被覆盖检查拦截：它仍优先选择第一代短partial，而对应block已经在保存rank的下一代变为Full。
修复为旧版本覆盖不足时选择当前Full候选或private_full fallback，并保持GET成功后才更新记录。
三步非保存rank回归在修复前重现，公开/私有Full和失败不得提前替换的用例补齐后111项通过。
正在重启开发P/D重跑；r02镜像已过期、禁止启动，最终镜像必须包含此修正。详见runbook §11.25。

### 5.8 最终验收状态

- B1/32768/4112修复后单/双/三chunk与并发/重复/断连后请求通过。
- r03镜像包含全部功能修正，没有源码覆盖。B2自动预算52042 blocks，两个P节点32 rank一致；
  Main槽4、私有Indexer21、每block物理765952 bytes。没有手动block覆盖或二次幂取整。
- r03 NPU sentinel B1/2/3通过；完整模型单/双/三chunk、并发同前缀、重复前缀、断连后请求通过。
  最终四引擎running/waiting/error counter均0，完整运行日志ERROR/Traceback/传输失败计数0。
- 功能提交9824ec91c，f9b995c68只做固定Ruff格式；镜像10文件SHA复核及与格式后源码的AST等价验证通过。
- 111聚焦UT、205 metadata/backend测试通过；扩展测试与全仓CI仍有已记录的基线问题。
  全仓CI在独立worktree执行，自动修改的9个基线文件与本特性17个文件无交集，未改实际工作区。
- 原基线及r01/r02容器保留并停止，master未操作。默认工具指向r03，正常重启不写源码。
