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
│       └── group_vars/all.yml   # A3 集群参数（依 A2 蓝本推演，未实机）
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
- `inventories/<a2|a3>/inventory.yaml`：节点拓扑（每节点 `ansible_host` / `nic` / `idx`）

A2/A3 切换：`-i` 指不同 inventory 即可，互不影响。

**关键字段**（group_vars/all.yml）：
- `pd_cluster.prefill` / `pd_cluster.decode`：dp/tp/dp_size_local/base_port/kv_port/rpc_port
- `model.path` / `model.served_model_name`：模型权重路径与 serve 名
- `proxy.port`、`mooncake.port`：统一入口与 KV 池端口
- `container.name` / `container.shm_size`：容器名与共享内存
- `logs.dir` / `logs.vllm`：日志目录（建议共享目录，容器销毁后保留）+ 按节点 `vllm_{node}.log`
- `ansible_*`：SSH 连接（user/私钥/重试/心跳）

---

## 4. playbook 用法

在 `ansible/` 目录下运行（让 Ansible 加载 `ansible/ansible.cfg`）：

```bash
cd ansible
source .venv/bin/activate

# 预检（只读）
ansible-playbook -i inventories/a2/inventory.yaml playbooks/check.yml

# 拉镜像（并行）
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
`cd ansible && .venv/bin/python -m pytest tests/ -q` → **21 passed**（0.37s）
- `tests/test_derive.py`（11 用例）：节点参数派生、实例清单、proxy 端点生成
- `tests/test_template_render.py`（10 用例）：Jinja2 模板渲染（含 decode 位置参数保留）

### 6.2 实机验证
- **原生化版本（本套件）**：完整端到端实机验证**通过**——全节点 failed=0，wait_ready **13/13 就绪**（4 prefill + 8 decode + proxy），proxy healthcheck `{"status":"ok","prefill_instances":4,"decode_instances":8}`，内建冒烟通过（首次 POST 因 KV 预热重试后成功），端到端请求 HTTP 200、`fingerprint vllm-0.23.0-tp4-dp8`（decode 参与，P/D 分离链路真实工作），预热后单请求约 2s。
- **历史（N 系列，config.yaml 旧版）**：8 节点容器生命周期（不占卡）通过；完整端到端（占卡）同 13/13 + 冒烟 + 端到端 200，与原生化结果一致。

---

## 7. 已知事项

- **A3 已按同事实机配置对齐**：inventory 已填 IP（10.246.64.45-48，nic `enp162s0f0`）、`model.path /mnt/weight/GLM-5.2-w4a8c8` + `/mnt/weight` 挂载、decode 拓扑 `dp32 tp1 dp_size_local 16`（同事 1.log 实机）、A3 特有环境变量（`ASCEND_A3_ENABLE`/`FUSED_MC2`/`HCCL_*_TIMEOUT`）与推理参数（模板 `cluster_type=='a3'` 条件）。**A3 仍未实机部署验证**（当前控制机连不上 10.246.64.x，需在能连 A3 的环境跑通）
- **运行需在 `ansible/` 目录内**（`ansible.cfg` 在 `ansible/`，让 Ansible 加载配置）
- 完整 `--check` 是干跑近似，不等价 `--dry-run` 的完整模拟
- `--tags engine` 等分段重跑依赖前置阶段（如 smoke 需 proxy 已起）
- `gen.yml` 用 inventory 静态 `ansible_host` 渲染 local_ip（调试语义；start.yml 用节点 register）
- 跑 `stop.yml` 会删 `container.name` 指定的容器（不影响其他运行容器）
