# deploy-glm52 Ansible 部署套件

> 用 **Ansible** 完成 GLM-5.2 P/D 分离一键部署（4P/8D 引擎 + mooncake KV 池 + proxy 统一入口）。
> **agentless**：只控制机装 Ansible，节点无需任何 agent（SSH + 节点 Python 即可）。
> 配置源：`config.yaml`（本目录套件以它为唯一配置源）。

---

## 1. 目录结构

```
ansible/
├── ansible.cfg              # Ansible 配置（filter 插件发现）
├── inventory.yaml           # 节点清单（由 generate_inventory.py 生成）
├── generate_inventory.py    # 从 config.yaml 生成 inventory
├── group_vars/all.yml       # SSH 连接参数（含重试/心跳容错）
├── filter_plugins/resolve.py   # 复用 resolve/*.py 的 filter 插件
├── templates/
│   ├── run_dp_prefill_template.sh.j2   # prefill 模板（Jinja2 + 节点命令替换）
│   └── run_dp_decode_template.sh.j2    # decode 模板
├── playbooks/
│   ├── check.yml    # 预检：SSH + docker + 镜像
│   ├── pull.yml     # 拉镜像
│   ├── start.yml    # 一键部署（容器+mooncake+引擎+wait+proxy+冒烟）
│   ├── stop.yml     # 停止清理
│   ├── status.yml   # 实例探测
│   ├── logs.yml     # 日志查看
│   └── gen.yml      # 渲染模板/mooncake.json 到 ../generated/
└── tests/           # filter + 模板单测（16 用例）
```

> 依赖项目根 `resolve/`（filter 插件复用）与 `config.yaml`。交付时需包含这两个：

```
交付清单：
├── ansible/          # 本套件（含本 README）
├── config.yaml       # 唯一配置源
└── resolve/          # filter 插件依赖的解析器
    ├── resolve_node.py
    ├── resolve_instances.py
    ├── resolve_router.py
    └── render_mooncake.py
```

---

## 2. 前置条件

- 控制机装 `ansible-core`（venv 内，`uv pip install ansible-core`）
- 控制机能免密 SSH 登录所有节点（连接参数在 `group_vars/all.yml`，编辑成你的 ssh 配置）
- 各节点已装 docker、NPU 驱动正常（`npu-smi`）、内存足够
- 模型权重就绪（config `model.path`）

---

## 3. 配置

`config.yaml` 是唯一配置源，playbook 用 `include_vars` 加载。

**关键字段**：
- `nodes.prefill` / `nodes.decode`：节点 `{ip, nic}`（改后重跑 `generate_inventory.py`）
- `container.name`：容器名
- `logs`：日志目录（建议共享目录，容器销毁后保留）+ vllm 按节点 `vllm_{node}.log`
- `ssh`：`user` / `opts`（控制机到节点的 -i 私钥、-o 选项）

---

## 4. playbook 用法

在 `ansible/` 目录下运行（让 Ansible 加载 `ansible/ansible.cfg`）：

```bash
source .venv/bin/activate
cd ansible

# 生成 inventory（改 config 节点后重跑）
python3 generate_inventory.py > inventory.yaml

# 预检（只读）
ansible-playbook -i inventory.yaml playbooks/check.yml

# 拉镜像（并行）
ansible-playbook -i inventory.yaml playbooks/pull.yml

# 一键部署（起容器 + mooncake + 12 引擎 + 全就绪 + proxy + 冒烟）
ansible-playbook -i inventory.yaml playbooks/start.yml

# 只起容器（不启引擎，调试用；分段 tags：container/mooncake/engine/wait/proxy/smoke）
ansible-playbook -i inventory.yaml playbooks/start.yml --tags container

# 实例探测
ansible-playbook -i inventory.yaml playbooks/status.yml

# 日志（node/mooncake/proxy，TAIL 改行数）
ansible-playbook -i inventory.yaml playbooks/logs.yml -e "node=p0"
ansible-playbook -i inventory.yaml playbooks/logs.yml -e "target=mooncake"

# 停止清理
ansible-playbook -i inventory.yaml playbooks/stop.yml

# 渲染模板/mooncake.json（部署前自动，也可手动）
ansible-playbook -i inventory.yaml playbooks/gen.yml

# 干跑（--check 近似 dry-run）
ansible-playbook -i inventory.yaml playbooks/start.yml --check
```

部署成功后统一入口：`http://<proxy_ip>:<proxy_port>/v1`（config `proxy.port`，model 用 `model.served_model_name`）。

---

## 5. 关键设计

### 5.1 节点命令替换（核心）
模板里 `local_ip="{{ local_ip.stdout }}"`——`local_ip` 是各节点执行
`ip -4 addr show <nic>` 后的 `register` 结果，**每节点替换成自己的值**。

### 5.2 vllm 模板保留运行时位置参数
`launch_online_dp.py` 在**运行时**对每个本机引擎传位置参数（`$1..$7`：
设备/端口/dp_size/dp_rank/地址/rpc/tp_size）。模板保留 `$2..$7`，**不硬编码**——
保证 decode 多引擎（dp_size_local=2）每引擎拿到不同端口/rank。

### 5.3 --pid=host 安全
容器 `--pid=host`，容器内 `pkill` 必须精确匹配（`pkill -x mooncake_master` /
`pkill -f '^python3 .*load_balance_proxy_server_example'`），避免误杀控制机 ssh 会话。

### 5.4 SSH 容错
`group_vars/all.yml` 加了连接重试 + 心跳（`ansible_ssh_retries: 3` +
`ServerAliveInterval`），容忍节点容器起后 sshd 瞬时 reset。

---

## 6. 测试报告（2026-08-06 实机验证）

### 6.1 单元测试
`uv run pytest ansible/tests/` → **16 passed**（filter 插件 + Jinja2 模板渲染，含 decode 位置参数单测）。

### 6.2 容器生命周期（不占卡）
| 步骤 | 命令 | 结果 |
|---|---|---|
| 渲染 | `gen.yml` | ✅ 8 节点模板 + mooncake.json |
| 起容器 | `start.yml --tags container` | ✅ 8 节点起容器成功 |
| 容器内文件 | docker exec | ✅ launch_online_dp.py + mooncake.json 分发正确 |
| 探测 | `status.yml` | ✅ failed=0 |
| 清理 | `stop.yml` | ✅ 8 节点无残留 |

### 6.3 完整端到端（占卡）
`ansible-playbook playbooks/start.yml`：
- **8 节点 failed=0**，容器 + mooncake + 12 引擎启动
- **wait_ready 13/13 就绪**（4 prefill + 8 decode + proxy，elapsed ~1053s）
- 内建冒烟通过
- proxy healthcheck：`{"status":"ok","prefill_instances":4,"decode_instances":8}`
- **端到端请求 HTTP 200**，`fingerprint vllm-0.23.0-tp4-dp8`（decode 参与，P/D 分离链路真实工作），1.6s

---

## 7. 已知事项

- 完整 `--check` 是干跑近似，不等价 `--dry-run` 的完整模拟
- `--tags engine` 等分段重跑依赖前置阶段（如 smoke 需 proxy 已起）
- `gen.yml` 用 config 静态 LOCAL_IP 渲染（调试语义）
- 跑 `stop.yml` 会删 config `container.name` 指定的容器（不影响其他运行容器）