> **2026-11-07 更新**：默认 inventory 仍为 `fe559c440` eager。现场D为
> `...-review-safe`，运行dfb52bd0a/mode3/MTP3/无计时，40条语义及含断连41条
> 生命周期通过；P/Master/proxy未重启。候选参数在
> `artifacts/review-fixes-20261107/safe.yml`，完整身份、限制及验收见runbook §11.78。旧 `graph.yml` 使用 mode0，
> 已复现多条语义错误，不再作为可用配置，生成器会在切换容器前拒绝它。
> receipt 图模式必须同时设置 compilation mode3 和 `FULL_DECODE_ONLY`。

# GLM-5.2 layerwise guide deployment

本档参考 sig-ai-inference 的 DSA Offload 部署指南
`b4965c0625c9f89ffd6c8f61dee6f8ed2f62cc8b`，使用开发分支
`p-main-buffer-reuse@fe559c440`。该分支基于 `dsa_offload_rebase_30@4234a1f18`，
保留长 prefill 的 receipt 超时，并加入 P MTP1 的跨请求 Mooncake 前缀恢复。

P 保留 MultiConnector、MooncakeLayerwiseConnector、逐层 Store 和 Main buffer
reuse。D 保留 Mooncake Host offload、AsyncReceiptScheduler 和完整 TP receipt。
代理使用 D-first。每节点按物理卡显式启动两个 TP8 进程，P/D 各 DP4。

| 参数 | 指南 | 本档 |
| --- | --- | --- |
| vLLM-Ascend | dsa_offload_rebase_0.26 | p-main-buffer-reuse，基于 0.30 |
| P / D 拓扑 | TP8×DP4 / TP2×DP16 | TP8×DP4 / TP8×DP4 |
| P / D DCP | 8 / 1 | 1 / 1，P 开 enable_dsa_cp |
| P/D 交付 | 块级，P-first | 逐层，D-first，Host receipt |
| P API | P0 聚合 API、P1 headless | 外部 DP 各自 API，四个 P 端点 |
| max_model_len | 1,048,576 | 1,048,576，运行结果见 runbook |
| P chunk / max seq | 16,384 / 8 | 16,384 / 8 |
| D token budget / max seq | 32 / 8 | 32 / 8 |
| P / D MTP | 1 / 3 | 1 / 3 |
| P prefix caching | 开启 | HBM 关闭；MTP1 通过 Mooncake 复用跨请求前缀 |
| D Host / TopK / threads | 100 GiB / 4096 / 16 | 100 GiB / 4096 / 16 |
| P / D utilization | 0.75 / 0.88 | 0.75 / 0.88 |
| D execution | FULL_DECODE_ONLY | eager（默认）；编译图候选须使用 mode3，见下文 |
| D recompute scheduler | 开启 | 关闭，保留 AsyncReceiptScheduler 的 target 生命周期接线 |
| Receipt 总交付超时 | 块级路径 | layerwise_receipt_timeout_s=7200，覆盖长 prefill |
| CANN / Mooncake | 9.2 / pr3952 | 9.2 / pr3952，固定提交和 wheel 摘要 |
| 传输协议 | HCCS | 本集群已验证的 RoCE |

Indexer 使用显式 INT8 配置，Main 使用 BF16。P 共享 Main 槽数为 2，首末层独立，
MTP 层由当前布局代码单独分配。D receipt owner 和 retention 端口按 DP rank
分配，Host pool 的名称也包含 DP rank。全局 DP/EP 仍覆盖每个角色的两节点。

所有命令在部署仓库根目录执行，健康检查由 P0 执行。首次切换按以下顺序。
SDK 已在 P0 构建完成；在缺少 SDK 镜像的环境上，先运行下方的 build-stack。

```bash
export PATH="$PWD/ansible/.venv/bin:$PATH"
export ANSIBLE_LOCAL_TEMP="$PWD/ansible/.ansible-tmp" ANSIBLE_HOST_KEY_CHECKING=False
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/gen.yml
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/build.yml
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/prepare.yml
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/start.yml
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/smoke.yml
```

先 gen，再 prepare，然后 start。prepare 保存原容器 ID 和启动命令，校验新镜像
源码摘要，并准备带独立标签的容器。start 检查原服务空闲，停止捕获的四个原
容器，确认 NPU 进程归属后启动新服务。健康检查覆盖八个引擎和代理。

容器使用 private IPC。D 的 `/dev/shm` 在容器内设置 `huge=always`，承载其
Host pool。模型权重和驱动挂载复用原部署。日志按节点写入
`/mnt/sfs_turbo/g00832294/layerwise-guide/tp8-dp4-1m-cann92-mtp-prefix/<node>/logs/`。
P API 为 P0 的 8100/8101、P1 的 8102/8103，D API 为 D0 的 8200/8201、
D1 的 8202/8203。代理在 P0 的 8333，独立 Master 在 P0 的 52551。

停止新部署和恢复原服务分别执行以下命令。restore 使用 prepare 捕获的原容器
与进程命令，先恢复八个引擎，再恢复原代理。当前捕获的前任为 `15d342c0b` CANN9.2 档。

```bash
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/stop.yml
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/restore.yml
```

重启已有候选服务或处理启动失败时，先用 stop.yml 停止当前候选容器；改配置后
运行 gen.yml，再运行 prepare.yml 启动空闲 shell，最后执行
`start.yml --tags launch,wait,proxy`。这条路径跳过首次切换使用的 cutover。
本档的实测状态和问题记录在 `docs/m1-buffer-reuse-runbook.md`。

CANN9.2＋pr3952 已成为本 inventory 的默认配置；`cann92.yml` 保留为空的
兼容参数文件。已通过以下功能验收：

| 阶段 | 请求与结果 |
| --- | --- |
| 初步回归 | 12 条短／四并发 33.7K 多 chunk／前缀回放请求正确 |
| 真实近 1M P/D | 1,047,254 输入 token，返回 MAPLE，3 输出 token，stop；TTFT 2,266.031 秒 |
| 长测后 Ansible 回归 | 再次通过 12 条请求，确认 copy＋command＋async 入口可运行 |
| 交付与生命周期 | 每条请求全 8-rank receipt 与 target 释放通过，四 D 均有流量 |
| 最终状态 | 25 条成功请求；八引擎与代理健康、空闲、KV 使用归零，日志无 ERROR |

证据位于 `artifacts/layerwise-guide/20261107/`，完整过程见 runbook §11.73。
当前服务镜像为 `glm52-vllm-ascend:0.30.0-layerwise-guide-fe559c440-cann92`，
四节点 ID 一致；容器为 `vllm-ascend-glm52-layerwise-guide-mtp-prefix`，服务保持运行。

`fe559c440` 另通过 13 条前缀专项请求和 12 条 Ansible 回归，每条的全
8-rank receipt 与 target 释放均通过，覆盖四 D。公共完整块采用后继块的
链式 hash 隔离 MTP 的下一 token 依赖，私有尾块继续重算；本地 HBM
prefix 开关保持关闭。专项结果和 55K/700、10/20 并发性能对照在
`artifacts/mtp-prefix-20261107/`，见 runbook §11.75。上表的近 1M 长测
是前任 `15d342c0b` 的结果，本次没有重复 1M 长测。

完整依赖栈构建入口为 `build-stack.yml`，固定 vLLM `ced6857`、
Mooncake `7bfe1cc`、pybind11 `58c382a`、yalantinglibs `7801bc9` 和
MessagePack C++ `9199087`（cpp-7.0.0）。MessagePack 是 openEuler 构建所需
头文件依赖；构建脚本还修正 FLA 下载目录，并核对官方 Release 的 SHA256。
节点无法直接拉取 GitHub 时，通过控制机分发依赖源码，并从已验证镜像中导出
同一 vLLM 提交。全量编译仅在 P0 的 Docker 构建容器中执行。

```bash
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/build-stack.yml
```

依赖镜像为 `0.30.0-layerwise-sdk-993713305-cann92`，固定编译源码由
`native_source_revision` 指定。随后 `build.yml` 将最新开发源码固化到其
editable 包路径，生成不同标签的服务镜像。该步骤会核对两次提交的 C++、
编译配置和依赖；若存在差异，需更新 `native_source_revision` 并重新构建 SDK。
gen、build、prepare、start 和 smoke 使用同一 inventory 默认配置。

八个引擎和短请求回归通过后，以下命令验证实际约 1,047K token 的完整 P/D
请求。探针先调用 `/tokenize` 检查长度，再检查流式答案、usage 和正常结束；
结果中的 request ID 用于在 D 日志核对全 TP receipt 和 target 释放。
这是功能探针，所报耗时不能当作独立性能基准。

```bash
ansible-playbook -i ansible/inventories/a3-layerwise-guide/inventory.yaml ansible/dsa-layerwise/long.yml
# 或直接执行探针，在终端实时查看流式结果。
ssh root@ott9-a31 'python3 -u -' < ansible/dsa-layerwise/long_probe.py
```

D decode graph 候选的实现、同输入性能结果及后续语义问题见
[runbook §11.76–11.78](../../docs/m1-buffer-reuse-runbook.md)。
模板支持按角色设置`enforce_eager`和`compilation_config`，以及可选的
`profiler_config`与D receipt host计时；未配置时仍采用原eager行为，不启用诊断。
图候选将D设为`enforce_eager: false`、`compilation_config.mode: 3`、
`cudagraph_mode: FULL_DECODE_ONLY`，P保持eager。当前GLM draft仍受框架family
guard约束使用eager，需看启动的`Receipt MTP execution`日志确认真实决策。
PP/CP/DCP仍限制为1，其他graph模式继续拒绝。旧mode0虽然capture成功，
无计时语义回归仅17/20，同提示对照18/20，不能作为已验收的性能基线。
源码和Ansible均拒绝该组合；mode3隔离验证和最终候选profile记录在§11.78，
不要使用旧graph.yml或默认参数重部署实验服务。
