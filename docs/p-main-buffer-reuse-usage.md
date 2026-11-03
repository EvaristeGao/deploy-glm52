# P 侧 Main buffer reuse：运行与验收

更新：2026-11-03。当前运行镜像为 `glm52-vllm-ascend:0.30.0-main-reuse-dev-20261103-r03`，
容器为四节点上的 `vllm-ascend-gwh-glm52-reuse-r03`，服务端口保持 P 9081、D 9900、proxy 8000。
原基线容器完整保留并停止，Mooncake master 保持运行。r01/r02 为停止的历史实验容器。

## 实现与边界

源码在 `src/vllm-ascend-reuse` 的 `p-main-buffer-reuse` 分支，基于固定 `4234a1f18`。
功能提交 `9824ec91c`，随后 `f9b995c68` 仅整理固定 Ruff 版本要求的空行、换行。
r03 镜像包含功能提交的生产代码；10 个文件的镜像清单校验通过，与格式提交后的源码 AST 等价。
旧 `src/vllm-ascend` 工作区保持原状。

- 只共享 Main，按 cache spec 类型识别；Indexer 独立。
- 覆写前等待前代 attention、Store PUT 源读取及 P/D 读者结束。
- partial snapshot 以 request-instance / generation 区分；非保存 rank 在 partial 变 Full 后更新恢复来源。
- 当前支持 eager、无 MTP/speculation、单 KV manager group、V1 runner、PP=CP=1；不支持的实际复用配置启动拒绝。

已验证配置在 [Ansible profile](../ansible/profiles/p-main-buffer-reuse.yaml)：P 为 TP16 × DP2、
2 个共享槽、首尾层独立、4112-token 预算、max_model_len=262144、max_num_seqs=4、严格 load failure。
P 使用自动 block 预算；D 不启用共享，验证档固定5508 blocks。两侧关闭 MTP，保留 RoCE 环境和 D-first proxy。
模型使用镜像内别名 `/root/reuse-main-20261103-model`，为实验提供独立 Store namespace。

## 验收结果

| 配置 | 实际 Main 槽 | 私有 Indexer | P blocks | 真实验证 |
|---|---:|---:|---:|---|
| B=3，4096预算 | 5 | 21 | 5508 | 单/双/三chunk、并发前缀、重复、断连后请求 |
| B=1，4112预算 | 3 | 21 | 32768 | 同上，含第三代partial恢复 |
| r03 B=2，4112预算 | 4 | 21 | **52042（自动）** | 同上；32个P rank一致 |

r03 单/双/三chunk输入535/5017/9656 tokens，输出正常中文KV摘要，耗时23.11/6.90/8.91s。
并发5000-token请求、重复前缀及关闭长请求连接后继续请求均正常返回。
最终四引擎 running/waiting/error counter 均0，完整日志未发现 ERROR/Traceback/传输失败。
断连探针仅证明客户端关闭后服务可继续，不覆盖内部所有 abort 分支；不以逐token一致判断复用正确性。

NPU sentinel 验证 B=1/2/3、6层×3轮：range PUT返回后立即污染源，后续GET数据正确。
111项聚焦单测通过；另205项metadata/backend测试通过。扩展scheduler/worker测试的13项失败已在固定基线复现。
固定 Ruff 0.14 对本次17个Python文件的lint/format检查通过。
全仓 `format.sh ci` 在独立副本运行，仍有未改动基线文件的lint/拼写问题、typos进程-9及shellcheck下载竞争；
gitleaks钩子跳过，未宣称全仓CI或secret scan通过。详见 `artifacts/p-main-buffer-reuse/format-ci.log`。

## 操作

从仓库根目录执行。所有资料、缓存、环境、构建上下文和日志副本在 `artifacts/p-main-buffer-reuse/`。

```bash
# 本地渲染；不连接或重启集群
ANSIBLE_LOCAL_TEMP=$PWD/artifacts/p-main-buffer-reuse/tmp/ansible \
ANSIBLE_HOST_KEY_CHECKING=False ansible/.venv/bin/ansible-playbook \
  -i ansible/inventories/a3/inventory.yaml ansible/playbooks/gen.yml \
  -e @ansible/profiles/p-main-buffer-reuse.yaml

# 只读：检查镜像源码、抓取最近4MiB日志
python3 tools/reuse_container_experiment.py verify-image
python3 tools/reuse_container_experiment.py collect

# 发真实请求；工具先核对开发容器标签与基线状态
python3 tools/reuse_request_smoke.py
python3 tools/reuse_request_stress.py

# 容器操作：统一重启开发P/D，保留master和基线，不覆盖源码
python3 tools/reuse_container_experiment.py restart-all

# 回退：停止r03，启动捕获ID对应的原基线容器和原启动文件
python3 tools/reuse_container_experiment.py restore
```

工具默认 `REUSE_RUN=r03`；旧实验显式指定变量。r01/r02已标记退役，工具拒绝启动。
检查到其他开发容器在运行、基线ID变化或owner不符时拒绝切换。
`--patch-source` 仅供明确的开发调试，正常重启不使用；最终镜像没有源码覆盖。
仅重启P曾出现原生P/D超时，实验切换统一重启P/D；没有执行主机、设备或Docker daemon reset。
回退命令已准备并核对保留文件，本轮最终验收后没有执行回退。

此 profile 带 `reuse_render_only: true`，通用 `start.yml` 会在删除容器前拒绝执行。
不要用通用部署流程替代上述保留基线的容器操作。
完整过程、失败证据与修正见 [runbook §11.17起](m1-buffer-reuse-runbook.md) 和
[设计核对](p-main-buffer-reuse-audit.md)。
