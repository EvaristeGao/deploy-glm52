# deploy-glm52 设计规格

日期：2026-08-05
状态：已批准（目录名 `deploy-glm52`；压测改为 curl 功能验证）

## 1. 背景与目标

现有 GLM-5.2 P/D 分离一键部署（根目录 `deploy.sh` + `templates/` + `cluster-*.env`）以跑通的 `a2.md` 为蓝本，从控制机 SSH 一键编排整集群。它的短板：

- 配置分散：`cluster-*.env` + 模板硬编码 + `deploy.sh` 默认值三处各管一段
- 拓扑参数（dp4tp8/dp8tp4/kv_port）写死在模板里，改 env 拓扑会静默错配
- 就绪等待只探每节点首个引擎端口
- A2/A3 靠两套模板目录 + 两个入口脚本

合作方 `vllm-ascend/.ci/example-glm5.2-1m` 的优势：单一 `config.yaml` 配置源 + Python 解析器集中派生参数 + 全实例 `verify.sh`。

**目标**：新建独立目录 `deploy-glm52/`，把两者结合为「单一集群 `config.yaml` + SSH 一键编排」，**运行时参数以我们现有 a2 蓝本为主**（不照搬合作方 1M 那套），与根目录现有脚本彻底分开。

## 2. 设计决策（已确认）

| 决策 | 选择 | 理由 |
|---|---|---|
| 技术栈 | Shell 编排 + Python 解析 | 保留现有 deploy.sh 骨架，借鉴合作方 resolve_* 写法 |
| 集群范围 | 一套代码 A2/A3 通用 | config 驱动拓扑，一套模板跑两种集群 |
| 运行时参数 | 以现有 a2 蓝本为主 | MultiConnector + mooncake_master + 200K，已验证 |
| 压测 | 不用 sglang bench_serving（镜像没有） | 改为 curl 触发简单 vLLM 功能验证 |
| 目录 | `deploy-glm52/`（独立 git 仓库） | 与根目录现有脚本隔离 |

## 3. 目录结构

```
deploy-glm52/
├── deploy.sh                  # 主编排（Shell，控制机执行）
├── config.yaml                # 唯一配置源（单一集群，A2 或 A3）
├── resolve/
│   ├── resolve_node.py        # config → 单节点 launch_online_dp.py 参数
│   ├── resolve_router.py      # config → proxy 端点列表
│   ├── resolve_instances.py   # config → 全部实例 role ip port（verify 用）
│   └── render_mooncake.py     # config → mooncake.json
├── templates/
│   ├── run_dp_prefill_template.sh   # 以现有 a2 prefill 模板为蓝本
│   └── run_dp_decode_template.sh    # 以现有 a2 decode 模板为蓝本
├── launch_online_dp.py        # 复用现有多引擎启动器（根目录 scripts/ 拷贝）
├── verify.sh                  # 全实例探测 + --wait
├── func_check.sh              # curl 触发的简单功能验证（替代 bench_pd.sh）
├── README.md
└── docs/superpowers/specs/2026-08-05-deploy-glm52-design.md
```

## 4. config.yaml（唯一配置源）

运行时参数均以现有 `cluster-a2.env` + `templates/a2/*` 蓝本为准。A2/A3 差异由 `pd_cluster` 中的 `dp_size_local`/`num_cards`/`nodes` 数量驱动，一套模板通用。

```yaml
cluster:
  name: a2            # a2 | a3（影响 RoCE 开关等网络差异）
  desc: "8xA2 4P+4D"

ssh:
  user: root
  opts: "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

image: quay.io/ascend/vllm-ascend:v0.23.0rc1

model:
  path: /mnt/share_space/models/GLM-5.2-w4a8
  served_model_name: glm-52
  dir_host: ""        # 非空则挂载 /root/.cache（A3 用）
  mounts:             # 原 EXTRA_MOUNTS 的列表形式
    - "/mnt/share_space/:/mnt/share_space/"

nodes:                # 顺序即节点表，role 由所属组决定
  prefill:
    - { ip: "P0_IP",  nic: "NIC_P0" }
    - { ip: "P1_IP",  nic: "NIC_P1" }
  decode:
    - { ip: "D0_IP",  nic: "NIC_D0" }
    - { ip: "D1_IP",  nic: "NIC_D1" }

pd_cluster:           # 蓝本全局拓扑参数化（不再硬编码进模板）
  max_model_len: 200000
  num_cards: 8        # 每节点 NPU 数（A3 为 16）
  prefill: { dp_size: 4, tp_size: 8, dp_size_local: 1, base_port: 9081, kv_port: 30000, rpc_port: 16591 }
  decode:  { dp_size: 8, tp_size: 4, dp_size_local: 2, base_port: 9900, kv_port: 30100, rpc_port: 16600 }
  enable_prefix_caching: true

proxy:
  node: p0            # 节点名（须在 nodes 中）
  port: 1999
  script_path: ""     # 容器内 proxy 脚本路径；空则自动查找

mooncake:
  port: 50088
  evict: 0.9
  config:
    global_segment_size: "80GB"
    default_kv_lease_ttl: 11000

container:
  name: vllm-ascend
  shm_size: 1024g

runtime:
  ready_timeout: 2400
```

**推导规则**（由 resolve_node.py 实现）：
- 节点命名：第 i 个 prefill → `p<i>`，第 i 个 decode → `d<i>`
- `dp_rank_start = node_index × dp_size_local`
- `dp_address`：prefill 组用第一个 prefill 节点 IP，decode 组用第一个 decode 节点 IP（与现有 deploy.sh 一致）
- A2 展开结果须与 a2.md 蓝本一致（P 每节点 1 引擎 dp4tp8 / D 每节点 2 引擎 dp8tp4）

## 5. Python 解析器

统一约定：stdout 输出 `KEY=VALUE`（shell `eval` 直接消费），错误时 stderr 中文报错 + 非零退出。均可用 `--dry-run` 打印不执行（shell 侧处理）。

- **resolve_node.py**：输入 `--config config.yaml --node p0|d1`（节点名由 deploy.sh 传入，控制机已知身份，不需要合作方"本机 ifconfig 自查"）。输出：`ROLE / NODE_INDEX / DP_SIZE / DP_SIZE_LOCAL / TP_SIZE / DP_RANK_START / DP_ADDRESS / DP_RPC_PORT / VLLM_START_PORT / KV_PORT / NUM_CARDS / MODEL_PATH / SERVED_MODEL_NAME / MAX_MODEL_LEN / ENABLE_PREFIX_CACHING / CLUSTER_TYPE`。
- **resolve_router.py**：输出 `PROXY_HOST / PROXY_PORT / PREFILLER_HOSTS / PREFILLER_PORTS / DECODER_HOSTS / DECODER_PORTS`（多引擎展开规则与现有 `proxy_args` 一致）。
- **resolve_instances.py**：逐行输出 `role ip port`（全部引擎 + proxy），供 verify.sh 与 cmd_start 就绪等待。
- **render_mooncake.py**：按 `mooncake.config` 渲染 mooncake.json，master 地址 = prefill 首节点 IP:port。

## 6. 主编排 deploy.sh

保留现有命令与工具函数骨架：`check / pull / start / stop / status / logs / gen` + `--dry-run`、`sshn`/`scpn`/`wait_ready`/`log/warn/die`、幂等清理、并行 pull。改动：

1. **配置加载**：不再 `source cluster-*.env`，改由解析器输出 + `eval`；`require_vars` 预检保留（校验 config.yaml 可解析、字段齐全、节点 IP 非空）。
2. **修复就绪等待**：`cmd_start` 用 `resolve_instances.py` 返回的**全部引擎端口**逐一 `wait_ready`（不再只等首引擎），全部就绪后再起 proxy。
3. **模板渲染**：保留 sed 占位符替换；占位符集从 5 个扩为含 `__PREFILL_DP__`/`__PREFILL_TP__`/`__DECODE_DP__`/`__DECODE_TP__`/`__KV_PORT__`/`__CLUSTER_TYPE__` 等（从 config 派生），模板不再硬编码拓扑。
4. **mooncake_master**：起在 p0 容器内（同现有），`MOONCAKE_LOG=/root/mooncake.log`。
5. **proxy**：定位保留 `PROXY_SCRIPT_PATH` + 容器内搜索兜底（手动提供，同 a2.md）。
6. **gen**：渲染所有节点模板 + mooncake.json 到 `generated/` 供人工审查（调试用）。

命令集：`check` 预检 / `pull` 并行拉镜像 / `start` 起容器+mooncake+全部引擎+全端口就绪+proxy+冒烟 / `stop` / `status` / `logs <node|mooncake>` / `gen`。入口：`./deploy.sh <command>`，用 `CLUSTER_CONFIG=config-a3.yaml` 环境变量切换集群（默认 `config.yaml`）。

## 7. 模板（以现有 a2 模板为蓝本，仅参数化）

- **内容、环境变量、vLLM 参数全部照搬现有 `templates/a2/`**（MultiConnector + AscendStoreConnector、200K、`--enable-prefix-caching`、`--async-scheduling`、`VLLM_ASCEND_ENABLE_MLAPO=1`、flashcomm1、HCCL_BUFFSIZE 256/2560 等）。
- **唯一改动**：把硬编码的拓扑/端口替换为解析器注入的变量——kv-transfer-config 的 `dp_size/tp_size`、`kv_port` 30000/30100、additional-config 的 sparse 开关、`--max-model-len 200000`。
- A3 差异（去掉 `HCCL_INTRA_ROCE_ENABLE`）由模板顶部按 `__CLUSTER_TYPE__` 条件导出，一套模板跑两种集群，不再维护 `templates/a2`、`templates/a3` 两份。

## 8. 功能验证（func_check.sh，替代 bench_pd.sh）

镜像内没有 sglang bench_serving，改为 curl 触发的简单 vLLM 功能验证：

```bash
./func_check.sh [--model glm-52] [--prompt "The future of AI is"] [--max-tokens 50] [--count 3]
```

- 向统一入口 `http://<PROXY_HOST>:<PROXY_PORT>/v1/completions` 发 `count` 个请求，校验 HTTP 200 且响应含 `choices`，逐条打印延迟与结果摘要。
- 可加 `--wait` 循环直到可用（复用 verify.sh 就绪语义）。
- 该脚本只在代理节点可访问网络的机器上跑，不依赖 sglang。

## 9. 错误处理与健壮性

- 解析器对"节点名不存在 / config 缺字段 / 节点 IP 为空"显式报错并退出（借鉴合作方 resolve_node.py 的多命中/零命中校验）。
- deploy.sh 全程 `set -euo pipefail`；远端命令失败即 die，不静默继续。
- `start` 幂等：先 `docker rm -f` 旧容器、pkill 旧 proxy/mooncake_master 再重建。
- mooncake_master 单点（p0），故障需重部署——README 说明（同现有）。

## 10. 测试策略

- 解析器单元测试：构造最小 config.yaml（A2/A3 各一份），断言输出的 `KEY=VALUE` 与 a2.md 蓝本展开结果一致。
- `deploy.sh --dry-run check/gen/start/stop` 全命令预演，人工审查 `generated/` 渲染产物。
- 实机部署后：`verify.sh --wait` 全实例就绪 + `func_check.sh` 功能验证。

## 11. 关键差异对照

| 项 | 现有根目录脚本 | 新 deploy-glm52/ |
|---|---|---|
| 配置源 | 分散（env + 模板硬编码 + 默认值） | 单一 config.yaml + Python 派生 |
| 拓扑参数 | 模板写死 dp4tp8/dp8tp4/kv_port | config 驱动，模板只读变量 |
| 集群支持 | 两套模板目录 + 两个入口 | 一套模板 + config 开关 |
| 就绪等待 | 只等每节点首引擎 | 全部引擎逐一等待 |
| 验证 | 只有冒烟 | + verify.sh 全实例 + func_check.sh |
| 目录 | 根目录混杂 | 独立新目录（独立 git） |

## 12. 范围外（YAGNI）

- 不做多组 PD 集群 / KV 隔离（合作方 pd_cluster 组列表能力暂不引入）
- 不引入 sglang bench（镜像无此依赖）
- 不做 A2/A3 之外的硬件适配
- 不做自动回滚 / 健康自愈
