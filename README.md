# deploy-glm52 — GLM-5.2 P/D 分离一键部署套件

通过 SSH 从控制机自动完成所有节点的镜像拉取、`mooncake_master` 拉起、vLLM 引擎
拉起与请求转发代理，提供统一 OpenAI API 入口。配置蓝本为实际跑通的 `a2.md`：
`MultiConnector`（`MooncakeConnectorV1` + `AscendStoreConnector`）双连接器，
KV pool 由镜像自带的 `mooncake_master` 管理；
`--enable-prefix-caching`、`--async-scheduling`、`max-model-len 200000` 等参数
均与 a2.md 保持一致。

**唯一配置源是 `config.yaml`**——所有节点表、拓扑、推理参数、镜像、proxy 信息
都从这一个文件读取，`deploy.sh` / `verify.sh` / `func_check.sh` 与 `resolve/`
解析器均以它为准。切换集群只需改配置文件。

## 集群切换

默认使用 `config.yaml`（A2 8 机版）。切换到 A3 4 机版用环境变量指向另一个配置：

```bash
CLUSTER_CONFIG=config-a3.yaml ./deploy.sh check
CLUSTER_CONFIG=config-a3.yaml ./deploy.sh start
```

`verify.sh` / `func_check.sh` 同样读取 `$CLUSTER_CONFIG`（缺省 `config.yaml`）。

## 两个集群

| | `config.yaml`（A2） | `config-a3.yaml`（A3） |
|---|---|---|
| 集群 | 8 x A2（8 x 910B 64G/节点，共 64 卡） | 4 x A3（16 卡/节点，共 64 卡） |
| 角色 | 4P + 4D | 2P + 2D |
| Prefill | DP4 x TP8，每节点 1 引擎（`:9081`） | DP4 x TP8，每节点 2 引擎（`:9081` `:9082`） |
| Decode | DP8 x TP4，每节点 2 引擎（`:9900` `:9901`） | DP8 x TP4，每节点 4 引擎（`:9900`-`:9903`） |
| 默认镜像 | `quay.io/ascend/vllm-ascend:v0.23.0rc1` | `quay.io/ascend/vllm-ascend:v0.23.0rc1-a3` |
| proxy 端口 | `1999` | `8000` |
| 模型路径 | `/mnt/share_space/models/GLM-5.2-w4a8` | `/root/.cache/.../GLM-5.2-w4a8c8` |

两个集群共用同一套 `deploy.sh`、`resolve/` 解析器与参数化模板
（`templates/run_dp_{prefill,decode}_template.sh`），区别只在配置文件。

## 组件

- **mooncake_master**：KV pool 管理服务，在 p0 容器内拉起（`:50088`），
  `mooncake.json`（master 地址 = prefill 首节点 IP）由 `resolve/render_mooncake.py`
  渲染并分发到所有节点容器
- **vllm 引擎**：每节点容器内运行 `launch_online_dp.py` + 渲染后的
  `run_dp_template.sh`，多引擎按 `dp_size_local` 逐卡拉起
- **proxy**：项目自带的 `load_balance_proxy_server_example.py`（内容以 a2.md 蓝本为准），
  `deploy.sh start` 自动分发进 `proxy.node`（默认 p0）容器 `/root/pd/` 后拉起，
  端点列表按拓扑自动生成（`resolve/resolve_router.py`）

## config.yaml 字段说明

以下为 `config.yaml` 各段字段及含义（`config-a3.yaml` 结构相同，仅取值不同）：

| 段 | 字段 | 说明 |
|---|---|---|
| `cluster` | `name` / `desc` | 集群名（`a2`/`a3`）与描述；`name` 同时作为 `__CLUSTER_TYPE__` 渲染进模板，控制是否启用 RoCE |
| `ssh` | `user` / `opts` | SSH 登录用户（默认 root）与额外选项（BatchMode、HostKey accept-new、ConnectTimeout） |
| `image` | — | 节点拉取的镜像 tag |
| `model` | `path` | 模型权重路径（容器内可见） |
| | `served_model_name` | 对外模型名（默认 `glm-52`） |
| | `dir_host` | 非空则挂载 `-v <dir_host>:/root/.cache`（A3 用）；A2 留空 |
| | `mounts` | 额外 `-v` 挂载列表，每项一条（如 A2 的 `/mnt/share_space/`） |
| `nodes` | `prefill` / `decode` | 每个节点一行 `{ ip, nic }`；节点名按 `p0..pN` / `d0..dN` 自动编号 |
| `pd_cluster` | `max_model_len` | 最大模型长度（模板 `--max-model-len`） |
| | `num_cards` | 每节点卡数（决定 `--device /dev/davinciN` 数量） |
| | `prefill` / `decode` | `dp_size` / `tp_size`（全局拓扑）、`dp_size_local`（每节点引擎数）、`base_port`（首引擎端口）、`kv_port`（KV 传输端口）、`rpc_port`（DP RPC 端口） |
| | `enable_prefix_caching` | 是否启用前缀缓存 |
| `proxy` | `node` | proxy 所在节点名（默认 `p0`） |
| | `port` | proxy 对外端口（A2 `1999`，A3 `8000`） |
| | `script_path` | proxy 脚本在**容器内**路径；留空时 `deploy.sh` 把项目自带脚本分发进 `/root/pd/` 并使用 |
| `mooncake` | `port` | mooncake_master 端口（默认 `50088`） |
| | `evict` | 驱逐高水位比例 |
| | `config` | `global_segment_size` / `default_kv_lease_ttl`，写入 `mooncake.json` |
| `container` | `name` / `shm_size` | 容器名（`vllm-ascend`）与共享内存大小（`1024g`） |
| `runtime` | `ready_timeout` | `/health` 就绪等待超时（秒，默认 2400 = 40 分钟，可用 `READY_TIMEOUT` 覆盖） |
| | `proxy_ready_timeout` | proxy `/healthcheck` 就绪超时（秒，默认 600；引擎全部就绪后才启动 proxy，此项兜住 vllm/torch 冷启动） |
| `logs` | `dir` | 日志目录（容器内路径；可指向挂载的共享目录如 `/mnt/share_space/<工号>/deploy-glm52/logs`，**容器销毁后日志仍保留**；留空则用 `/root`） |
| | `vllm` / `mooncake` / `proxy` | 日志文件名（`vllm` 支持 `{node}` 占位符按节点替换为 `vllm_p0.log`/`vllm_d0.log` 等，**避免节点互相覆盖**；默认 `vllm_{node}.log`/`mooncake.log`/`proxy.log`） |

## 前置条件

- 控制机能 SSH 免密登录所有节点（默认 root）
- 所有节点已安装 docker，NPU 驱动正常（`npu-smi` 可用），内存足够（`--shm-size=1024g`）
- 控制机装有 Python3 与 `pyyaml`（`pip install pyyaml`）；`deploy.sh` 启动时会校验，
  缺失报错退出。**注意**：解析器（`resolve/`）只在控制机本地跑，**8 台远程节点不需要 pyyaml**。
  本机用 uv 管理环境时，pyyaml 只装在 `.venv` 里，而脚本默认调系统 `python3`（可能无 pyyaml）；
  因此**执行 deploy.sh / verify.sh / func_check.sh 前先 `source .venv/bin/activate`**，
  让 `python3` 指向 venv（见 `deploy.sh` 顶部 TODO，后续计划改为自动检测 venv）。
- 模型权重就绪：A2 默认共享目录 `/mnt/share_space/models`；A3 默认 `/root/.cache`
- 镜像内含 `mooncake_master`；proxy 脚本由项目自带（`load_balance_proxy_server_example.py`，
  `deploy.sh start` 自动分发进容器，也可用 `config.proxy.script_path` 指定容器内其他路径）
- 本地开发/测试环境：`uv sync` 安装依赖后可用 `uv run pytest` 跑解析器用例

## 使用

以 A2 为例（A3 加 `CLUSTER_CONFIG=config-a3.yaml` 前缀）：

1. 先激活 venv（解析器需要 pyyaml，见「前置条件」）：
   ```bash
   source .venv/bin/activate
   ```
2. 编辑 `config.yaml`，填入节点 IP、网卡名（`ifconfig` 可查）、镜像、模型路径。
3. 依次执行：

```bash
./deploy.sh check    # 预检: config 可解析 + SSH 连通 + 远端 docker
./deploy.sh pull     # 并行拉取镜像到所有节点
./deploy.sh start    # 起容器 + mooncake_master + 全部引擎 + 全端口就绪 + proxy + 冒烟测试
```

其他命令：

```bash
./deploy.sh status            # 各节点容器状态 + 每节点引擎 API 健康 + mooncake_master 状态
./deploy.sh logs p0           # 节点 vllm 日志 (p0..pN / d0..dN)，TAIL=100 可改行数
./deploy.sh logs mooncake     # mooncake_master 日志 (p0 容器内)
./deploy.sh logs proxy        # proxy 日志（proxy 节点容器内）
./deploy.sh stop              # 停 proxy / mooncake_master + 删除所有节点容器
./deploy.sh gen               # 只渲染各节点模板与 mooncake.json 到 generated/ (调试用)
./deploy.sh --dry-run start   # 打印将执行的命令而不实际执行
```

部署成功后，统一入口为 `http://<proxy_node_ip>:<proxy.port>/v1`
（A2 端口 `1999`，A3 端口 `8000`），模型名 `glm-52`：

```bash
curl http://<P0_IP>:1999/v1/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "glm-52", "prompt": "The future of AI is", "max_completion_tokens": 50, "temperature": 0}'
```

## 验证

- **`verify.sh`**：探测全部 vLLM 引擎 `/health` 与 proxy `/healthcheck`，输出
  `READY` / `UNREADY(503)` / `DOWN` 汇总。
  `bash verify.sh` 单次探测；`bash verify.sh --wait [--timeout 600] [--interval 5]`
  进入等待模式，全部就绪即退出 0；`--no-router` 跳过 proxy。
- **`func_check.sh`**：curl 触发的简单 vLLM 功能验证（替代 sglang bench，镜像无该
  依赖）。向统一入口 `POST /v1/completions`，校验 200 且响应含 `choices`，打印每次
  延迟。参数：`--model` / `--prompt` / `--max-tokens` / `--count` / `--wait`。
- **`deploy.sh start` 内建冒烟测试**：部署完成后自动 POST 一次 `/v1/completions`。

两脚本均读同一 `config.yaml`（或 `$CLUSTER_CONFIG`）与 `resolve/resolve_instances.py`。

## 文件说明

- `config.yaml` / `config-a3.yaml` — 唯一配置源（A2 / A3 变体）
- `deploy.sh` — 主编排（check/pull/start/stop/status/logs/gen + `--dry-run`）
- `verify.sh` / `func_check.sh` — 就绪探测与功能验证
- `launch_online_dp.py` — 多引擎启动器（与 a2.md / 官方文档一致）
- `load_balance_proxy_server_example.py` — 项目自带 proxy 脚本（a2.md 蓝本，`start` 自动分发进容器）
- `resolve/resolve_node.py` — 按节点名输出启动参数（供 deploy.sh `eval`）
- `resolve/resolve_instances.py` — 输出全部实例与 proxy 的 `role ip port` 探测目标
- `resolve/resolve_router.py` — 输出 proxy 的 prefiller/decoder 端点列表
- `resolve/render_mooncake.py` — 渲染 `mooncake.json`（master = prefill 首节点 IP）
- `templates/run_dp_{prefill,decode}_template.sh` — A2/A3 通用参数化模板，占位符由
  deploy.sh 按节点替换（`__NIC__` / `__LOCAL_IP__` / `__MODEL_PATH__` /
  `__MODEL_NAME__` / `__MOONCAKE_CONFIG_PATH__` / `__CLUSTER_TYPE__` /
  `__MAX_MODEL_LEN__` / `__KV_PORT__` / `__PREFILL_DP__` / `__PREFILL_TP__` /
  `__DECODE_DP__` / `__DECODE_TP__`）
- `generated/` — 每次 `start`/`gen` 重新渲染的产物，可人工审查
- `tests/` — 解析器单元测试（4 个文件，12 个用例），`uv run pytest` 运行

## 备注

- `start` 幂等：会先 `docker rm -f` 旧容器、pkill 旧 proxy/mooncake_master 再重建
- mooncake_master 是单点（跑在 p0），p0 故障则 KV pool 不可用，需重新部署
- proxy 脚本由项目自带，`deploy.sh start` 自动分发进容器 `/root/pd/` 后拉起；如需改用
  容器内其他路径，在 `config.proxy.script_path` 手动填入容器内路径即可
- `--dry-run` 全局可用：打印将执行的 SSH/SCP/docker 命令而不实际执行，适合先核对
  部署动作
- A2 配置以跑通的 a2.md 为蓝本；**A3 版本（`config-a3.yaml`）尚未实机验证**，字段
  依 A2 推演（仅每节点卡数/引擎数、镜像 tag、模型路径、proxy 端口不同），首次部署
  请先用 `CLUSTER_CONFIG=config-a3.yaml ./deploy.sh gen` 审查 `generated/` 模板，
  必要时再调整
- 模型加载较慢，`/health` 就绪等待默认超时 40 分钟（`runtime.ready_timeout` 可调）