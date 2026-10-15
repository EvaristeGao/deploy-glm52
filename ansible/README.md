# deploy-glm52 Ansible 部署套件

> 用 **Ansible** 完成 GLM-5.2 P/D 分离一键部署（4P/8D 引擎 + mooncake KV 池 + proxy 统一入口）。
> **agentless**：只控制机装 Ansible，节点无需任何 agent（SSH + 节点 Python 即可）。
> **完全自包含**：`ansible/` 一个目录即全部交付物（含 venv），无外部 `config.yaml` / `resolve/` / `filter_plugins` 依赖。

---

## 1. 目录结构

```
ansible/
├── ansible.cfg                  # Ansible 配置（inventory/group_vars 自动加载）
├── pyproject.toml               # uv 项目定义（依赖 ansible-core>=2.21；dev: pytest/pyyaml）
├── uv.lock                      # 依赖锁文件
├── .venv/                       # 已建好的虚拟环境（含 ansible-core，激活后即可用）
├── inventories/
│   ├── a2/
│   │   ├── inventory.yaml       # A2 集群节点拓扑（8xA2：4P+4D，ansible_host/nic/idx）
│   │   └── group_vars/all.yml   # A2 集群参数（SSH + 集群级变量，无 config.yaml）
│   └── a3/
│       ├── inventory.yaml       # A3 集群节点拓扑（4xA3：2P+2D，IP 待实机填写）
│       └── group_vars/all.yml   # A3 集群参数（1m-v030 档已实机跑通，见 docs/a3-v030rc1-vs-official.md）
├── playbooks/
│   ├── check.yml    # 预检：SSH + docker + 镜像
│   ├── pull.yml     # 拉镜像
│   ├── start.yml    # 一键部署（容器+mooncake+引擎+wait+proxy+冒烟）
│   ├── stop.yml     # 停止清理
│   ├── status.yml   # 实例探测
│   ├── logs.yml     # 日志查看
│   └── gen.yml      # 渲染模板/mooncake.json 到 ansible/generated/（只读本地，不占卡）
├── templates/
│   ├── run_dp_prefill_template.sh.j2   # prefill 模板（Jinja2 + 节点命令替换）
│   ├── run_dp_decode_template.sh.j2    # decode 模板
│   └── proto_template.sh.j2            # 原型评估模板（历史，可忽略）
├── launch_online_dp.py                  # 容器内按每引擎传入运行时位置参数（$1..$7）
├── load_balance_proxy_server_example.py # proxy 入口服务脚本
├── playbook-proto.yml                   # 原型评估 playbook（历史，可忽略）
├── tests/
│   ├── test_derive.py                  # 节点参数派生/实例清单/proxy 端点单测（11 用例）
│   └── test_template_render.py         # Jinja2 模板渲染单测（10 用例）
└── README.md
```

> 交付物即整个 `ansible/` 目录。旧版依赖的项目根 `config.yaml` / `resolve/` / `filter_plugins` 均已移除。

---

## 2. 前置条件

- 控制机无需手动装 Ansible——`.venv/` 已建好（ansible-core），直接 `source .venv/bin/activate`
- 控制机能免密 SSH 登录所有节点（SSH 参数在 `inventories/<集群>/group_vars/all.yml` 的 `ansible_*` 段，按你的环境编辑）
- 各节点已装 docker、NPU 驱动正常（`npu-smi`）、内存足够
- 模型权重就绪（group_vars `model.path`）

---

## 3. 配置

**没有 config.yaml**。所有参数都在 `ansible/inventories/` 内、按集群分目录：

- `inventories/<a2|a3>/group_vars/all.yml`：集群级参数（SSH 连接、`pd_cluster` P/D 拓扑、`mooncake`、`proxy`、`container`、`logs` 等）
- `inventories/<a2|a3>/inventory.yaml`：节点拓扑（每节点 `ansible_host` SSH 目标 / `ip` 网卡 IP / `nic` 网卡名 / `idx`）；`ip` 是组件间互通的规范地址（etcd/haproxy/mooncake/引擎都用它），生产环境填真实网卡 IP

A2/A3 切换：`-i` 指不同 inventory 即可，互不影响。

### 上下文长度档位选择器（`deploy_config`）

各集群 `group_vars/all.yml` 顶部有 `deploy_config` 开关，选择 `_200k` / `_1m` 两个配置块之一（`_active: "{{ _1m if deploy_config == '1m' else _200k }}"`，再经 `pd_cluster: "{{ _active.pd_cluster }}"` 落到具体的 P/D 拓扑、端口、CP 参数、`additional-config`）：

| inventory | `deploy_config` 当前值 | 200k 档 | 1m 档 |
|---|---|---|---|
| `a2` | `1m` | GLM-5.2 非对称 PD 4P+8D（P dp4tp8 / D dp8tp4），200K ctx | GLM-5.3 对称 PD 4P+4D，1M ctx + CP + timeouts + profiler |
| `a3` | `1m-v030` | GLM-5.2 **v0.30.0rc1 §5.2.3**（1M PD）：P DP2 TP16 / D **DP4 TP8**（每节点 2 引擎）+ DCP 8、`--kv-cache-dtype int8`、`--attention_config.indexer_kv_dtype int8`、`--safetensors-load-strategy prefetch`、`--api-server-count`/`--headless`、v0.30 env 集；另有 `1m`（v0.23 §5.2.2 口径）/ `200k`（v0.23 §5.2.1 口径，prefill 用 DP 近似官方 PP2） |

A3 两档**拓扑形状相同**（每节点都用满 16 卡），差别在上下文长度与运行时参数（batched-tokens / MTP spec tokens / `hf-overrides` / `HCCL_BUFFSIZE` / CP）。

切换后重跑 `gen`（重渲染节点脚本）→ `start` 生效。A3 两档与官方参考的逐项对照、有意偏离（prefill 用 DP 代替官方 PP2、保持 kvpool、关闭 sfa_c8、w4a8c8 而非 w8a8c8）及部署前检查清单见 `../docs/a3-glm52-configs.md`。

**关键字段**（group_vars/all.yml）：
- `pd_cluster.prefill` / `pd_cluster.decode`：dp/tp/dp_size_local/base_port/kv_port/rpc_port；可选分档覆盖项 `additional_config`、`hf_overrides`、`max_num_batched_tokens`、`num_speculative_tokens`（decode）、`hccl_buffsize`（decode）、`context_parallel_size`/`decode_context_parallel_size`（prefill）+ `cp_kv_cache_interleave_size`（未配置则不渲染，回落集群默认值）
- `model.path` / `model.served_model_name`：模型权重路径与 serve 名
- `model.mounts`：**必须写 `host:container`**（如 `/mnt/sfs_turbo:/mnt/sfs_turbo`）；单路径会被 docker 当匿名卷，容器里是空目录。`logs.dir` 与 `model_path` 都必须落在 mounts 覆盖范围内（引擎容器不单独挂 `logs.dir`，否则日志出不了容器）
- `proxy.port`、`mooncake.port`（后者当前无任何 playbook/模板引用）：统一入口与 KV 池端口
- `proxy.prefill_nodes` / `proxy.decode_nodes`：接入 proxy 的节点白名单（inventory 节点名，空/未配置 = 全量接入）；如 `decode_nodes: [d0]` 只让 d0 的实例接流量（模拟 decode 排队），改后重跑 `--tags proxy` 生效
- `container.name` / `container.shm_size`：容器名与共享内存
- `logs.dir` / `logs.vllm`：日志目录（必须在共享目录且被 mounts 覆盖，容器销毁后保留）+ 按节点 `vllm_{node}.log`
- `ansible_*`：SSH 连接（user/私钥/重试/心跳）

### 部署模式（kvpool / ha+etcd / haproxy 形态）

`mooncake.enabled`（kvpool）+ `mooncake.ha.enable_ha`（ha+etcd）+ `haproxy.mode`（haproxy 形态）组合出四种部署模式，各集群 `group_vars/all.yml` 独立设置：

| 模式 | enabled | enable_ha | haproxy.mode | 部署的 mooncake/etcd/haproxy 组件 | 引擎 KV 传输 |
|---|---|---|---|---|---|
| kvpool HA · 内嵌 haproxy（默认） | true | true | per_container | 3× master + 3× etcd + 每容器内嵌 haproxy | MultiConnector（Mooncake + Ascend Store 池）+ MOONCAKE_CONFIG_PATH |
| kvpool HA · 独立 haproxy | true | true | standalone | 3× master + 3× etcd + 3× 独立 haproxy 容器 | 同上，引擎/mooncake_master 经 3 个 haproxy 的 `IP:port` 访问 etcd |
| kvpool 单主 | true | false | — | 1× master（p0 直连），无 etcd/haproxy | 同上，master 直连 `<p0_ip>:52050` |
| 无 kvpool | false | — | — | 无 master/etcd/haproxy | 直连 MooncakeConnectorV1（`engine_id` prefill=0/decode=2、`use_ascend_direct: true`） |

- **校验**：`enable_ha=true` 要求 `enabled=true`（etcd/haproxy 为 mooncake_master 选主服务），非法组合 `start.yml` 开头 `assert` 拦截。
- **haproxy 形态**：`per_container`（默认）在每个 mooncake/引擎容器内嵌 haproxy（master→`cache_master_port`、引擎→`pd_port`）；`standalone` 在 `haproxy.nodes`（3 节点）各起一个独立 haproxy 容器（镜像 `haproxy.image`，构建自 `image_build/Dockerfile_ha`）监听统一 `haproxy.port`，组件经 3 节点 `IP:port` 访问。
- **生效点**：`start.yml`（etcd/mooncake/haproxy 容器、mooncake_master 任务门控与命令分支）、`gen.yml`（mooncake.json 三态渲染，无 kvpool 不生成，HA 时 `etcd://` 地址按 haproxy 形态二选一）、引擎模板（kv-transfer-config 两套 JSON + MOONCAKE_CONFIG_PATH 门控）、`check/pull/stop`（etcd/mooncake 镜像与容器按开关）、`status/logs`（无 mooncake 引用，不受影响）。
- **证书只在 HA 模式挂**：`etcd.certs_dir` 的 bind 在引擎容器、mooncake 容器、独立 haproxy 容器上都按 `mooncake.ha.enable_ha` 门控 —— 单主/无 kvpool 模式整条链路都不需要证书目录，也不会在宿主机上创建空的 `/…/certs`。
- ⚠️ **待实机确认**：`无 kvpool`（直连 PD 分离）在 w4a8c8 上官方标注有已知精度问题；`单主` 的 mooncake_master 调用与直连 `master_server_address`、`standalone` 下组件连 3 个 haproxy 的 `etcd://` 多地址形态，均需实机验证。

---

## 4. playbook 用法

在 `ansible/` 目录下运行（让 Ansible 加载 `ansible/ansible.cfg`）：

```bash
cd ansible
source .venv/bin/activate

# 预检（只读）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/check.yml

# 拉取/加载镜像（并行；group_vars 的 image_tar 非空则 docker load tar, 否则 docker pull）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/pull.yml

# 一键部署（起容器 + mooncake + 引擎 + 全就绪 + proxy + 冒烟）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml

# 只起容器（不启引擎，调试用；分段 tags：container/mooncake/engine/wait/proxy/smoke）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml --tags container

# 实例探测
ansible-playbook -i inventories/a2/inventory.yaml playbooks/status.yml

# 日志（node/mooncake/proxy，TAIL 改行数）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/logs.yml -e "node=p0"
ansible-playbook -i inventories/a2/inventory.yaml playbooks/logs.yml -e "target=mooncake"

# 停止清理
ansible-playbook -i inventories/a2/inventory.yaml playbooks/stop.yml

# 渲染模板/mooncake.json（部署前自动，也可手动；只读本地，不占卡）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/gen.yml

# 干跑（--check 近似 dry-run）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml --check

# 切换 A3：把 -i 换成 inventories/a3/inventory.yaml 即可
```

部署成功后统一入口：`http://<proxy_ip>:<proxy_port>/v1`（group_vars `proxy.port`，model 用 `model.served_model_name`）。

---

## 5. 关键设计

### 5.1 节点参数派生（Jinja2，替代原 resolve/*.py）
`start.yml` 用 Jinja2 直接派生每节点参数，无 Python filter 依赖：
- `node_idx = inventory_hostname[1:] | int`（p0→0、d3→3）
- `dp_rank = node_idx × dp_size_local`（每节点首引擎 rank = 节点序 × 本地 dp 数）
- `base_port/kv_port/rpc_port` 取自 `pd_cluster[role]`

### 5.2 实例清单 / proxy 端点生成
- 实例清单：prefill/decode 每节点 `dp_size_local` 个实例 `[role, ip, base_port+r]`，proxy 单列
- proxy 端点：`prefiller_hosts/ports`、`decoder_hosts/ports`、`proxy_host/port` 由模板 `groups × range(dp_size_local)` 生成

### 5.3 vllm 模板保留运行时位置参数
`launch_online_dp.py` 在**运行时**对每个本机引擎传位置参数（`$1..$7`：设备/端口/dp_size/dp_rank/地址/rpc/tp_size）。模板保留 `$2..$7`，**不硬编码**——保证 decode 多引擎（dp_size_local>1）每引擎拿到不同端口/rank。

### 5.4 --pid=host 安全
容器 `--pid=host`，容器内 `pkill` 必须精确匹配（`pkill -x mooncake_master` / `pkill -f '^python3 .*load_balance_proxy_server_example'`），避免误杀控制机 ssh 会话。

### 5.5 SSH 容错
`group_vars/all.yml` 加了连接重试 + 心跳（`ansible_ssh_retries: 3` + `ServerAliveInterval=30`），容忍节点容器起后 sshd 瞬时 reset。

---

## 6. 测试报告

### 6.1 单元测试（不占卡）
`cd ansible && .venv/bin/python -m pytest tests/ -q` → **30 passed**（0.4s）
- `tests/test_derive.py`（15 用例）：节点参数派生、实例清单、proxy 端点生成、proxy 接入节点白名单（直接渲染 start.yml 真实表达式）
- `tests/test_template_render.py`（15 用例）：Jinja2 模板渲染（含 decode 位置参数保留）

### 6.2 实机验证
- **原生化版本（本套件）**：完整端到端实机验证**通过**——全节点 failed=0，wait_ready **13/13 就绪**（4 prefill + 8 decode + proxy），proxy healthcheck `{"status":"ok","prefill_instances":4,"decode_instances":8}`，内建冒烟通过（首次 POST 因 KV 预热重试后成功），端到端请求 HTTP 200、`fingerprint vllm-0.23.0-tp4-dp8`（decode 参与，P/D 分离链路真实工作），预热后单请求约 2s。
- **历史（N 系列，config.yaml 旧版）**：8 节点容器生命周期（不占卡）通过；完整端到端（占卡）同 13/13 + 冒烟 + 端到端 200，与原生化结果一致。

---

## 7. 已知事项

- **A3 三档配置（`deploy_config`，当前 `1m-v030`）**：**`1m-v030` 档已实机跑通**（2026-10-15：6/6 引擎 `/health` 200 + proxy `{"status":"ok","prefill_instances":2,"decode_instances":4}` + 端到端冒烟 200，`system_fingerprint` = `vllm-0.30.0-tp8-dp4-ep`）—— 严格按 **v0.30.0rc1 文档 §5.2.3**：P DP2 TP16 / D **DP4 TP8**（每节点 2 引擎）+ DCP 8、`--kv-cache-dtype int8` + `--attention_config.indexer_kv_dtype int8`、`--safetensors-load-strategy prefetch`、v0.30 精简 env 集、additional-config 用 `enable_flashcomm1`/`enable_fused_mc2`/`enable_mlapo`/`mega_moe_max_tokens`。另两档是 v0.23 口径：`1m`（P DP2 TP16 / D DP8 TP4 + DCP 4）、`200k`（198K；prefill 用 DP2 TP16 近似官方 PP2，`launch_online_dp.py` 不支持 PP）。第四档 **`1m-v030-lw`**（未启用）= `1m-v030` + **Mooncake 分层 KV offload**（`use_layerwise`；P/D 都 TP8、无 CP/DCP、decode 纯 eager、prefill 开 chunked-prefill，前置是 mooncake client 需带 PR #2881 接口），见 `../docs/a3-layerwise-kv-offload.md`。**与官方文档的全部差异见 `../docs/a3-v030rc1-vs-official.md`**，其中两处是「照抄文档会启动失败」的坑：①`additional_config` 的嵌套层级（`fuse_muls_add`→`ascend_compilation_config`、`recompute_scheduler_enable`→`scheduler_config`）；②`--headless` 在 external DP LB 模式下非法（`--data-parallel-rank` 启动方式下 p1/d1 必须自己起 API server）。
- **A3 的 `enable_sparse_sfa_c8` 两档都关（有意不跟参考）**：官方 198K/1M 参考与集群三份 1M 参考都是 `true`，本项目沿用 c3b1d4d「关闭 sfa_c8 防精度问题」保持 `false`。关掉同时规避了官方警告的组合「w4a8c8 权重 + DCP + sfa_c8 同开」（官方：`on the w4a8c8 weights enabling both together has known issues and is not recommended`）——当前权重正是 w4a8c8、1M 档又开着 DCP。将来换 `/mnt/sfs_turbo/models/GLM-5.2-w8a8c8`（集群上已有）并确认精度后可再考虑开 `true`
- **A3 的 KV 走 mooncake 池化（`mooncake.enabled=true`）**：`1m-v030` 档亦如此（文档 §5.2.3 是直连，本项目有意保留池化）。当前为单主（`ha.enable_ha=false`）：测试阶段不引入 etcd/ha 选主，只起 p0 上的裸 `mooncake_master`，`mooncake.json` 的 `master_server_address` 直连 `<p0_ip>:52050`；etcd/haproxy 相关配置块保留，切回 HA 前需确认 `etcd.certs_dir` 在宿主机真实存在
- **`model.mounts` 必须写 `host:container`**：单路径写法会被 docker 当匿名卷（容器内是空目录，模型权重看不见）；且 `logs.dir` 与 `model_path` 都要落在 mounts 覆盖范围内，否则引擎日志/权重在容器里不可见（单测已锁）
- **运行需在 `ansible/` 目录内**（`ansible.cfg` 在 `ansible/`，让 Ansible 加载配置）
- 完整 `--check` 是干跑近似，不等价 `--dry-run` 的完整模拟
- `--tags engine` 等分段重跑依赖前置阶段（如 smoke 需 proxy 已起）
- 节点互通地址统一用 inventory 的静态 `ip`（网卡 IP）：控制面（etcd/haproxy/mooncake）、数据面（`dp_address`/健康检查/proxy 端点）都是 `hostvars[...].ip`；`ansible_host` 仅用于 SSH，playbook/模板里无引用
- 跑 `stop.yml` 会删 `container.name` 指定的容器（不影响其他运行容器）
