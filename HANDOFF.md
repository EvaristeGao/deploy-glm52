# deploy-glm52 交接文档

> 本文档用于把当前项目状态移交给下一个 Agent。包含：项目背景、已完成工作、目录结构、关键技术决策、与实机配置（solution-2）的对比结论、已知事项与后续建议。

---

## 1. 项目是什么

在独立目录 `deploy-glm52/` 构建的一套 **GLM-5.2 P/D 分离一键部署套件**：通过「单一 `config.yaml` 配置源 + 控制机 SSH 一键编排」完成整集群的部署。

运行时参数以**根目录实际跑通的 a2.md 蓝本**为主（`MultiConnector` + `AscendStoreConnector` + `mooncake_master` KV pool、`max_model_len 200000`）。

**关键定位**：这是「config 驱动 + SSH 编排」的改进版，替代根目录原有的 `deploy.sh` + `cluster-*.env` + 分散模板方案。设计上借鉴了合作方 `vllm-ascend/.ci/example-glm5.2-1m` 的「config.yaml 单一配置源 + Python 解析器派生参数 + 全实例验证」架构。

---

## 2. 当前状态

- **git 仓库**：`deploy-glm52/` 是独立 git 仓库（根目录 `/home/gao/code/script` 不是 git 仓库）。
- **当前分支**：`main`（已合并实现分支，工作区干净）。
- **最新提交**：`509de9f fix: config.yaml 权重改 w4a8c8、挂载加 /data2（对齐实机 solution-2）`。
- **测试**：`uv run pytest tests/` → **23 个用例全部通过**；5 个脚本 `bash -n` 语法检查通过。
- **实机验证**：**尚未在真实集群上跑通**（A2 与 A3 均未实机验证）。代码基于 a2.md 蓝本还原，逻辑已通过单测和 dry-run 预演，但**没在 8 机 A2 上真正部署成功过**。

---

## 3. 目录结构

```
deploy-glm52/
├── config.yaml            # 唯一配置源（A2 示例）
├── config-a3.yaml         # A3 变体（未实机验证）
├── deploy.sh              # 主编排脚本（SSH 一键编排）
├── verify.sh              # 全实例探测（借鉴合作方）
├── func_check.sh          # curl 功能验证（替代 sglang bench）
├── launch_online_dp.py    # 多引擎启动器（从根目录拷贝）
├── pyproject.toml         # uv 项目（pyyaml 运行时 + pytest 开发）
├── resolve/               # 4 个 Python 解析器
│   ├── resolve_node.py          # 单节点启动参数（KEY=VALUE）
│   ├── resolve_router.py        # proxy 端点列表
│   ├── resolve_instances.py     # 全实例清单
│   └── render_mooncake.py       # 渲染 mooncake.json
├── templates/
│   ├── run_dp_prefill_template.sh   # prefill 模板（参数化）
│   └── run_dp_decode_template.sh    # decode 模板（参数化）
├── tests/                 # pytest 单测（23 用例）
│   ├── fixtures/          # config_a2 / config_a3 / 覆写 / 字符串节点
│   └── test_*.py
└── docs/superpowers/      # 设计规格 + 实现计划（历史记录）
```

---

## 4. 已完成的组件

| 组件 | 文件 | 作用 |
|---|---|---|
| 配置 | `config.yaml` / `config-a3.yaml` | 单一配置源，A2/A3 由拓扑参数驱动 |
| 主编排 | `deploy.sh` | check/pull/start/stop/status/logs/gen + `--dry-run` |
| 解析器 | `resolve/` 4 个 | node/router/instances/mooncake 参数派生 |
| 模板 | `templates/` 2 个 | 单套参数化模板，A2/A3 通用（RoCE 开关按 `__CLUSTER_TYPE__`） |
| 验证 | `verify.sh` / `func_check.sh` | 全实例探测 + curl 功能验证 |
| 测试 | `tests/` | 23 用例（A2/A3/覆写/字符串节点） |

**deploy.sh 命令集**：
```bash
./deploy.sh check    # 预检: config + SSH 连通 + 远端 docker
./deploy.sh pull     # 并行拉取镜像到所有节点
./deploy.sh start    # 起容器 + mooncake_master + 全部引擎 + 全端口就绪 + proxy + 冒烟
./deploy.sh status   # 各节点容器状态 + API 健康
./deploy.sh logs <node|mooncake>   # 节点 vllm / mooncake 日志
./deploy.sh stop     # 停 proxy/mooncake + 删容器
./deploy.sh gen      # 只渲染各节点模板与 mooncake.json（调试）
./deploy.sh --dry-run <cmd>   # 打印将执行的命令
```

**测试环境**：用 uv 管理 venv。`uv sync` 后 `uv run pytest tests/`。

---

## 5. 关键技术决策（接手前必须理解）

1. **拓扑彻底从模板解耦**到 config.yaml。`pd_cluster.prefill/decode` 的 `dp_size / tp_size / dp_size_local / base_port / kv_port / rpc_port` 驱动一切展开。A2：P dp4tp8（每节点 1 引擎 9081）、D dp8tp4（每节点 2 引擎 9900/9901）。

2. **解析器输出协议**：`resolve_node.py` 输出 `KEY=VALUE` 供 deploy.sh `eval`；`resolve_router.py` 的多值列表（HOSTS/PORTS）**必须带引号**（`PREFILLER_HOSTS="ip1 ip2 ..."`），因为 deploy.sh `proxy_args` 用 `eval` 消费后未加引号展开。

3. **全引擎就绪等待**：`start` 用 `resolve_instances.py` 输出逐实例等待（不只首引擎），proxy 行跳过。

4. **模板占位符**：`__NIC__ __LOCAL_IP__ __MODEL_PATH__ __MODEL_NAME__ __MOONCAKE_CONFIG_PATH__ __CLUSTER_TYPE__ __MAX_MODEL_LEN__ __KV_PORT__ __PREFILL_DP__ __PREFILL_TP__ __DECODE_DP__ __DECODE_TP__` 由 deploy.sh sed 替换。

5. **A2/A3 共一套模板**：A3 差异仅 `HCCL_INTRA_ROCE_ENABLE` 按 `__CLUSTER_TYPE__` 条件导出（A2 走 RoCE，A3 走灵衢 UB）。

6. **verify.sh 四态区分**：READY(200) / UNREADY(503) / DOWN(连接失败) / FAIL，内嵌 python urllib（不依赖 curl），捕获 `urllib.error.HTTPError`。

---

## 6. 上层目录依赖与随附参考（只移交本目录时必读）

`deploy-glm52/` 的代码大量源于上层目录（`/home/gao/code/script/`）的蓝本文件。**如果只拷贝 `deploy-glm52/` 目录，下面这些信息必须知晓**，否则代码里的引用会悬空、实机排障会无从下手。

### 已经随附进本目录的
- **`reference/a2.md`**（2516 行）—— 权威蓝本。deploy.sh / templates / config.yaml 都声明「以跑通的 a2.md 为蓝本」，本目录内所有参数均还原自它。**这是最重要的随附文件**，务必保留。

### 真机已有、无需随附的
- **`/mnt/share_space/solution-2/`** —— 同事实机跑通的配置（8 机 A2，含各节点 `model-run.sh`、`run_dp_template.sh`、`docker-run.sh`、`start-proxy.sh`、`mooncake.json`、`load_balance_proxy_server_example.py`，以及 `run.log`/`run.log.bak` 真实运行日志）。**目标机器上已存在此目录**（正是从 A2 服务器拷贝过去的），所以未随附。需要实机对照时，直接参考真机上的 `/mnt/share_space/solution-2/`。

### 上层目录其他文件（未随附，说明去向）
| 上层文件 | 说明 |
| --- | --- |
| `scripts/launch_online_dp.py` | 已拷贝进本目录根 `launch_online_dp.py`（diff 确认一致） |
| `templates/a2/`、`templates/a3/` | 已改造为参数化模板 `templates/run_dp_*_template.sh` |
| `deploy.sh`（根目录的旧版） | 本套件是它的改进版，无需随附 |
| `cluster-a2.env` / `cluster-a3.env` | 旧 env 配置，已被 config.yaml 取代 |
| `README.md`（根目录） | 旧方案说明，本目录有新版 README |
| `vllm-ascend/.ci/example-glm5.2-1m/` | 合作方 1M 版脚本，仅架构借鉴，无运行时依赖 |

### 实机部署时两种模式的取舍
- solution-2 用的是**单 `MooncakeConnectorV1`**（无 mooncake_master KV pool）——这是同事测「不开 MoonCake 性能」的版本。
- 本套件用的是 **`MultiConnector`（MooncakeConnectorV1 + AscendStoreConnector）+ mooncake_master** KV pool——这是 a2.md 蓝本、也是同事确认「对的」配置。
- 部署时若想对照/切换两种模式，可编辑 `templates/run_dp_*_template.sh` 的 `kv-transfer-config` 段（当前是 MultiConnector；改成单 Mooncake 需参考真机 `/mnt/share_space/solution-2/*/run_dp_template.sh`）。

---

## 7. 与实机配置 solution-2 的对比结论

### 已确认一致的（无需改）
- **拓扑**：p0-p3 dp-size-local 1（rank 0/1/2/3，dp4tp8）、d0-d3 dp-size-local 2（rank 0/2/4/6，dp8tp4）——与我们的 config.yaml 及运行日志完全一致。
- **推理参数**：max_model_len 200000、max-num-batched-tokens 8192/256、max-num-seqs 256/128、gpu-memory-utilization 0.95/0.92、enforce-eager（prefill）、compilation-config FULL_DECODE_ONLY（decode）、speculative tokens 1/3、additional-config 的 sparse 开关——全部逐字一致。
- **环境变量**：VLLM_ASCEND_ENABLE_MLAPO、ASCEND_AGGREGATE_ENABLE、ASCEND_TRANSPORT_PRINT、VLLM_ASCEND_ENABLE_FLASHCOMM1、HCCL_INTRA_ROCE_ENABLE 等——prefill 有、decode 无，与实机一致。
- **dp-rpc-port 单值**：每节点多引擎共用同一 rpc_port（16600）是 master-bind/slave-connect 语义，**正常不冲突**，无需改（详见下文）。

### 已根据 solution-2 修正的
- **权重路径**：`/mnt/share_space/models/GLM-5.2-w4a8` → `GLM-5.2-w4a8c8`（config.yaml，commit `509de9f`）。
- **挂载**：`model.mounts` 增加 `/data2:/data2`（solution-2 docker-run.sh 确认，commit `509de9f`）。

### 有意不一致的（solution-2 是另一种测试场景）
- **kv_connector**：我们用 `MultiConnector`（MooncakeConnectorV1 + AscendStoreConnector + mooncake_master KV pool），solution-2 当前模板用的是**单 `MooncakeConnectorV1`**（无 KV pool / 无 mooncake_master）。同事明确说：**solution-2 是测试「不开 MoonCake 性能」的版本，MultiConnector 才是对的**，忽略这个差异。我们保留 MultiConnector + mooncake_master。

### 一处疑似实机坑（已记录，未改）
- d0 运行日志里 `ZMQError: Address already in use (16600)` 发生在**第一次启动尝试**（16:14），随后 16:19 重启成功段**再无冲突**。结论：这不是「共用端口」问题，而是**上次残留监听未释放**导致的临时失败。因为 dp-rpc-port 是 master-bind/slave-connect 语义，多引擎共用是预期行为。**无需改**。注意：因为 master 会 bind 该端口，重启前必须清干净（我们 deploy.sh 已 `docker rm -f` + `pkill` 自动清理，比手动更稳）。

---

## 7. 已知事项与风险

1. **A2 与 A3 均未实机验证**——A3 更是纯推演（config-a3.yaml 顶部已标注）。首次部署前建议 `./deploy.sh gen` 审查渲染产物，并用 `--dry-run` 预演。
2. **A3 镜像 tag** `v0.23.0rc1-a3` 未确认存在；A3 的 `VLLM_ASCEND_ENABLE_MLAPO` 等环境变量是否适用未验证。
3. **proxy 脚本手动提供**：镜像内含 `mooncake_master` 和 proxy 脚本未经验证。若镜像内找不到 proxy，需在 config `proxy.script_path` 指定（deploy.sh 会 die 提示）。
4. **mooncake_master 单点**：跑在 p0，p0 故障则 KV pool 不可用。
5. **docs/ 下历史设计文档**里仍有旧权重路径 `w4a8`（非 w4a8c8），是设计快照，未改，不影响运行。

---

## 8. 后续建议（移交给下个 Agent 的候选任务）

1. **实机验证 A2**：在真实 8 机 A2 上跑 `check → pull → start`，重点确认 mooncake_master 与 proxy 脚本在镜像内是否存在、全引擎就绪等待是否正常。
2. **A3 实机验证**：确认 `-a3` 镜像 tag、RoCE 关闭后的拓扑与参数是否适用。
3. **可选改进**：把「单节点多引擎共用 rpc_port」在 README 里补一句说明（master-bind 语义），避免其他人误判为 bug。
4. **可选**：把本次 solution-2 对比结论沉淀进 README，并清掉 docs/ 里过时的 w4a8 历史引用。

---

## 9. 接手常见命令

```bash
cd /home/gao/code/script/deploy-glm52
uv sync                       # 首次：创建 venv
uv run pytest tests/ -v       # 跑单测（23 用例）
./deploy.sh --dry-run gen     # 预演渲染（用 config.yaml）
CLUSTER_CONFIG=config-a3.yaml ./deploy.sh --dry-run start   # A3 预演
```