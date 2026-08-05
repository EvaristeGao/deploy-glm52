# deploy-glm52 使用说明

> GLM-5.2 P/D 分离一键部署套件：单一 `config.yaml` 配置源 + `deploy.sh` SSH 编排，完成 4P/8D vLLM 引擎 + mooncake KV 池 + proxy 统一入口的整集群部署。
> 适用：8 机 A2（8×910B2 64G/节点，共 64 卡）。

---

## 1. 目录结构

```
deploy-glm52/
├── config.yaml            # 唯一配置源（A2 示例）
├── config-a3.yaml         # A3 变体（未实机验证）
├── deploy.sh              # 主编排脚本（check/pull/start/stop/status/logs/gen）
├── verify.sh              # 全实例就绪探测
├── func_check.sh          # curl 功能验证（端到端请求）
├── launch_online_dp.py    # 多引擎启动器（DP 编排）
├── load_balance_proxy_server_example.py  # 项目自带 proxy 脚本（start 自动分发）
├── resolve/
│   ├── resolve_node.py          # 单节点启动参数（KEY=VALUE，供 deploy.sh eval）
│   ├── resolve_router.py        # proxy 端点列表
│   ├── resolve_instances.py     # 全部实例清单（含 proxy）
│   └── render_mooncake.py       # 渲染 mooncake.json
├── templates/
│   ├── run_dp_prefill_template.sh   # prefill 模板（参数化）
│   └── run_dp_decode_template.sh    # decode 模板（参数化）
├── generated/              # start/gen 渲染产物（可人工审查）
├── tests/                  # pytest 单测（23 用例）
└── docs/                   # 文档
```

切换集群：`CLUSTER_CONFIG=config-a3.yaml ./deploy.sh <command>`。

---

## 2. 配置文件 `config.yaml`

| 段 | 字段 | 说明 |
|---|---|---|
| `cluster` | `name` / `desc` | 集群名（`a2`/`a3`），`name` 作 `__CLUSTER_TYPE__` 渲染控制 RoCE 开关 |
| `ssh` | `user` / `opts` | SSH 用户与选项（`-i <私钥>`、BatchMode、ConnectTimeout） |
| `image` | — | 节点镜像 tag |
| `model` | `path` / `served_model_name` / `dir_host` / `mounts` | 模型路径、对外模型名、`/root/.cache` 挂载、额外 `-v` 挂载列表 |
| `nodes` | `prefill` / `decode` | 每节点 `{ip, nic}`，按 `p0..pN` / `d0..dN` 自动编号 |
| `pd_cluster` | `max_model_len` / `num_cards` | 最大长度、每节点卡数 |
| | `prefill` / `decode` | `dp_size`/`tp_size`（全局）、`dp_size_local`（每节点引擎数）、`base_port`、`kv_port`、`rpc_port` |
| | `enable_prefix_caching` | 前缀缓存 |
| `proxy` | `node` / `port` / `script_path` | proxy 节点（默认 p0）、端口（A2 1999）、容器内脚本路径（留空自动分发项目脚本） |
| `mooncake` | `port` / `evict` / `config` | KV 池端口（50088）、驱逐水位、`mooncake.json` 参数 |
| `container` | `name` / `shm_size` | 容器名（vllm-ascend）、共享内存（1024g） |
| `runtime` | `ready_timeout` / `proxy_ready_timeout` | 引擎 /health 就绪超时（2400s）、proxy 就绪超时（600s） |
| `logs` | `dir` / `vllm` / `mooncake` / `proxy` | 日志目录 + 文件名（见 §8） |

**拓扑**：A2 为 4P（dp4tp8，每节点 1 引擎 :9081）+ 4D（dp8tp4，每节点 2 引擎 :9900/:9901），共 12 引擎。

---

## 3. `deploy.sh` 命令

```bash
./deploy.sh check          # 预检：config 可解析 + 8 节点 SSH 连通 + 远端 docker
./deploy.sh pull           # 并行拉取镜像到所有节点
./deploy.sh start          # 一键部署：容器 + mooncake + 12 引擎 + 全就绪 + proxy + 内建冒烟
./deploy.sh status         # 各节点容器状态 + 每实例 API 健康 + mooncake 存活
./deploy.sh logs <节点|mooncake|proxy>   # 查看日志，TAIL=N 改行数
./deploy.sh gen            # 只渲染模板到 generated/（调试审查用）
./deploy.sh stop           # 停 proxy/mooncake + 删除全部节点容器
./deploy.sh --dry-run <cmd>  # 打印将执行的命令而不实际执行（全局可用）
```

**本地调用解析器需 pyyaml**：执行前先 `source .venv/bin/activate`（pyyaml 只在 venv 内）。

---

## 4. 配套脚本

- **`verify.sh`**：探测全部引擎 `/health` 与 proxy `/healthcheck`，输出 `READY`/`UNREADY(503)`/`DOWN`/`FAIL` 汇总。可选 `--wait`、`--no-router`。
- **`func_check.sh`**：向统一入口 POST `/v1/completions` 做功能验证，校验 200 且含 `choices`，打印每次延迟。参数：`--model`/`--prompt`/`--max-tokens`/`--count`/`--wait`。
- **`launch_online_dp.py`**：多引擎 DP 启动器（容器内执行，配合渲染后的模板）。
- **`load_balance_proxy_server_example.py`**：项目自带 proxy 脚本，`start` 自动分发进容器后拉起。
- **`resolve/`**：4 个解析器，从 config 派生节点参数 / proxy 端点 / 实例清单 / mooncake.json。

---

## 5. 前置条件

- 控制机能免密 SSH 登录所有节点（默认 root，`-i` 私钥）
- 所有节点已装 docker、NPU 驱动正常（`npu-smi`）、内存足够（`--shm-size=1024g`）
- 控制机 Python3 + pyyaml（`source .venv/bin/activate`）
- 模型权重就绪（A2 默认 `/mnt/share_space/models/GLM-5.2-w4a8c8`）
- 镜像内包含 `mooncake_master`；proxy 脚本由项目自带分发

---

## 6. 部署流程

```bash
source .venv/bin/activate          # 激活 venv（解析器需要 pyyaml）
./deploy.sh check                  # 预检
./deploy.sh start                  # 一键部署（约 10 分钟，含模型加载）
```

流程：起 8 容器 → mooncake_master（先于引擎）→ 12 引擎后台拉起 → `wait_ready` 逐实例等待 `/health` → 起 proxy → 等 proxy `/healthcheck` → **内建冒烟测试** → 打印 `部署完成`。

部署成功后统一入口：`http://<proxy_ip>:1999/v1`（model: `glm-52`）：

```bash
curl http://<proxy_ip>:1999/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-52","prompt":"The future of AI is","max_tokens":20,"temperature":0}'
```

---

## 7. 验证与停止

```bash
./deploy.sh status        # 实时查看容器/引擎健康
bash verify.sh            # 全实例探测汇总
bash func_check.sh --count 3   # 功能验证（3 次请求）
./deploy.sh stop          # 停止并清理（幂等，可反复执行）
```

`start` 幂等：先 `docker rm -f` 旧容器、pkill 旧 proxy/mooncake 再重建。`stop` 干净清理。

---

## 8. 日志

日志目录由 `config.logs.dir` 决定，**推荐指向挂载的共享目录**（如 `/mnt/share_space/<工号>/deploy-glm52/logs`），**容器销毁后日志仍保留**；留空则用容器内 `/root`。

配置：

```yaml
logs:
  dir: /mnt/share_space/g00832294/deploy-glm52/logs
  vllm: vllm_{node}.log     # {node} 按节点替换为 vllm_p0.log / vllm_d0.log 等，避免节点互相覆盖
  mooncake: mooncake.log
  proxy: proxy.log
```

查看：

```bash
./deploy.sh logs p0          # p0 引擎日志（vllm_p0.log）
./deploy.sh logs mooncake    # mooncake_master 日志
./deploy.sh logs proxy       # proxy 日志
TAIL=200 ./deploy.sh logs d2 # 自定义行数
```

三进程日志均 `>> xxx.log 2>&1` 追加（stdout+stderr）。

---

## 9. 测试结果（2026-08-05/06 实机验证）

**环境**：8 机 A2，8×910B2 64G/节点。

### 9.1 部署
- 单测：`uv run pytest` **23/23 通过**
- `deploy.sh check`：8 节点预检通过
- `deploy.sh start`：**13/13 实例 READY**（4 prefill + 8 decode + proxy），内建冒烟通过，`部署完成`
  - prefill ready ~572s，decode ready ~572s，proxy ready ~602s（就绪 15s）
- 端到端请求：HTTP 200，`fingerprint vllm-0.23.0-tp4-dp8`（decode 参与，P/D 分离链路）

### 9.2 命令验证
| 命令 | 结果 |
|---|---|
| `status` | ✅ 8 容器 running + 12 引擎 healthy + proxy + mooncake |
| `logs`（vllm/mooncake/proxy + TAIL） | ✅ |
| `gen` / `--dry-run` | ✅ |
| `verify.sh` | ✅ 13/13 READY |
| `func_check.sh` | ✅ 1/1（200） |
| `stop` | ✅ 干净清理 |

### 9.3 冒烟 ×3（`func_check.sh --count 3`）
| 请求 | 耗时 | 结果 |
|---|---|---|
| 1 | 21.2s（首请求冷启动） | 200 ✅ |
| 2 | 1.5s | 200 ✅ |
| 3 | 1.4s | 200 ✅ |

预热后 P/D 分离单请求 1-2s。

### 9.4 缓存
- mooncake KV 池 `role=leader, serving`，Clients:64
- KV 瞬时流式传输经 mooncake：prefill 存（`Delaying free of blocks`）→ decode 拉（`KV cache transfer` 63ms）→ 释放。**缓存工作正常**（vllm 日志 `KV cache transfer` 为证；mooncake 指标 `Keys` 常为 0 是瞬时消费 + 10s 采样所致）

### 9.5 日志
- 8 节点 vllm 日志按节点落共享目录 `vllm_{node}.log`，互不覆盖
- **容器销毁（stop）后 10 个日志文件完整保留**

### 9.6 可复现
两次完整 `start` 部署结果一致（13/13 就绪），一键部署 + 冒烟 + 停止全流程闭环。

---

**已知事项**：`config-a3.yaml`（A3 变体）尚未实机验证，首次使用前 `CLUSTER_CONFIG=config-a3.yaml ./deploy.sh gen` 审查渲染产物。