# deploy-glm52 Ansible 原生化设计（去掉手动 resolve）

> 目标：去掉 `ansible/filter_plugins/resolve.py`（subprocess 调 resolve/*.py）及项目根 `resolve/`、`config.yaml` 依赖，改用 **Ansible 原生配置**（inventory + group_vars + Jinja2 派生）承载全部参数。`ansible/` 目录完全自包含（含 venv、运行时脚本），支持 A2/A3 双集群。

---

## 1. 背景与动机

当前 Ansible 套件用 `filter_plugins/resolve.py` 通过 subprocess 调 `resolve/*.py`（写临时 config、剥 Ansible datatag、大写 KEY→小写转换层）派生节点参数/实例清单/proxy 端点。这是「把 deploy.sh 时代的 python 解析器塞进 Ansible」，非原生。

**所有派生值**（resolve_node 的 dp_size/tp_size/dp_rank_start/dp_address/端口/model 等；resolve_instances 的实例清单；resolve_router 的 proxy 端点）**都可用「inventory 主机变量 + group_vars + Jinja2 表达式」直接计算**，无需 python。

## 2. 目标

- 去掉 `resolve/filter_plugins` 及 `config.yaml` 依赖，全部参数 Ansible 原生承载
- `ansible/` 完全自包含：venv、运行时脚本、测试、文档都在其下
- 支持 A2/A3 双集群（不同 inventory + group_vars）
- 交付物 = `ansible/` 一个目录

## 3. 配置组织（无 config.yaml）

### 3.1 目录结构

```
ansible/
├── .venv/                        # venv（uv 管理）
├── pyproject.toml               # 依赖（ansible-core≥2.21, pytest, pyyaml）
├── ansible.cfg
├── inventories/
│   ├── a2/
│   │   ├── inventory.yaml        # A2 节点拓扑
│   │   └── group_vars/all.yml    # A2 集群参数
│   └── a3/
│       ├── inventory.yaml        # A3 节点拓扑
│       └── group_vars/all.yml    # A3 集群参数
├── playbooks/                    # check/pull/start/stop/status/logs/gen
├── templates/                    # run_dp_{prefill,decode}_template.sh.j2
├── launch_online_dp.py           # 运行时脚本（归位）
├── load_balance_proxy_server_example.py  # 运行时脚本（归位）
├── tests/
└── README.md
```

### 3.2 inventory（Ansible 原生组 + 主机变量）

每组含 `ansible_host`（IP）、`nic`（网卡）、`idx`（节点序号，供 rank 派生）。组 `prefill`/`decode` 承载引擎节点；`proxy`/`mooncake` 指向 p0。

`inventories/a2/inventory.yaml`：
```yaml
all:
  children:
    prefill:
      hosts:
        p0: { ansible_host: 192.168.0.245, nic: enp67s0f5, idx: 0 }
        p1: { ansible_host: 192.168.0.15,  nic: enp67s0f5, idx: 1 }
        p2: { ansible_host: 192.168.0.160, nic: enp67s0f5, idx: 2 }
        p3: { ansible_host: 192.168.0.91,  nic: enp67s0f5, idx: 3 }
    decode:
      hosts:
        d0: { ansible_host: 192.168.0.127, nic: enp67s0f5, idx: 0 }
        d1: { ansible_host: 192.168.0.161, nic: enp67s0f5, idx: 1 }
        d2: { ansible_host: 192.168.0.154, nic: enp67s0f5, idx: 2 }
        d3: { ansible_host: 192.168.0.140, nic: enp67s0f5, idx: 3 }
    proxy: { hosts: { p0: {} } }
    mooncake: { hosts: { p0: {} } }
```

`inventories/a3/inventory.yaml`：4 节点（p0-p1 + d0-d1），`num_cards 16`、`idx` 同规则。

### 3.3 group_vars/all.yml（集群参数，无 config.yaml）

`inventories/a2/group_vars/all.yml`：
```yaml
# SSH 连接（来自原 config ssh 段）
ansible_user: root
ansible_ssh_private_key_file: /home/g00832294/.ssh/KeyPair-2956.pem
ansible_ssh_common_args: "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ConnectionAttempts=3 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
ansible_ssh_retries: 3

# 集群参数（原 config 顶层/pd_cluster/proxy/mooncake/container/runtime/logs，去掉 nodes/cluster）
cluster_type: a2
image: quay.io/ascend/vllm-ascend:v0.23.0rc1
model_path: /mnt/share_space/models/GLM-5.2-w4a8c8
model_name: glm-52
pd_cluster:
  max_model_len: 200000
  num_cards: 8
  prefill: { dp_size: 4, tp_size: 8, dp_size_local: 1, base_port: 9081, kv_port: 30000, rpc_port: 16591 }
  decode:  { dp_size: 8, tp_size: 4, dp_size_local: 2, base_port: 9900, kv_port: 30100, rpc_port: 16600 }
  enable_prefix_caching: true
proxy: { port: 1999 }
mooncake: { port: 50088, evict: 0.9 }
container: { name: glm52-ansible-test, shm_size: 1024g }
runtime: { ready_timeout: 2400, proxy_ready_timeout: 600 }
logs: { dir: /mnt/share_space/g00832294/deploy-glm52/logs, vllm: vllm_{node}.log, mooncake: mooncake.log, proxy: proxy.log }
```

`inventories/a3/group_vars/all.yml`：`image ...-a3`、`model_path: /root/.cache/...`、`num_cards: 16`、`prefill.dp_size_local: 2`、`decode.dp_size_local: 4`、`proxy.port: 8000`、`container.name` 区分等。

### 3.4 运行方式（A2/A3 切换）

```bash
cd ansible && source .venv/bin/activate
ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml   # A2
ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml   # A3
```

## 4. 派生参数（Jinja2，替代 resolve filter）

### 4.1 节点参数（每引擎节点一次 set_fact）

```yaml
- name: 派生本节点参数
  set_fact:
    node_role: "{{ 'prefill' if inventory_hostname[0] == 'p' else 'decode' }}"
    node_idx: "{{ inventory_hostname[1:] | int }}"
    dp_size: "{{ pd_cluster[node_role].dp_size }}"
    dp_size_local: "{{ pd_cluster[node_role].dp_size_local }}"
    tp_size: "{{ pd_cluster[node_role].tp_size }}"
    dp_rank: "{{ node_idx * pd_cluster[node_role].dp_size_local }}"
    dp_address: "{{ hostvars[groups[node_role][0]].ansible_host }}"
    rpc_port: "{{ pd_cluster[node_role].rpc_port }}"
    start_port: "{{ pd_cluster[node_role].base_port }}"
    kv_port: "{{ pd_cluster[node_role].kv_port }}"
    max_model_len: "{{ pd_cluster.max_model_len }}"
```

`nic`/`local_ip` 来自 inventory 主机变量 / 节点命令 register（模板 `{{ local_ip.stdout }}` 不变）。

### 4.2 实例清单（wait_ready，run_once）

从 `groups['prefill']`/`groups['decode']` × `dp_size_local` 生成 `[[role, ip, base_port+r], ...]`，proxy 单列。实现时用 nested loop / product。

### 4.3 proxy 端点（run_once）

`prefiller_hosts` = 各 prefill 节点 IP 重复 `dp_size_local` 次；`prefiller_ports` = `base_port + r` 递增；decode 同理。union 进 proxy 启动命令。

## 5. playbook 改动点

| playbook | 改动 |
|---|---|
| 全部 | 去 `lookup('file','../../config.yaml')`（config 删除）；变量名从 `cfg.*` 改为 group_vars 平铺名（`image`/`model_path`/`pd_cluster.*`/`proxy.port`/`container.name`/`logs.*`） |
| `start.yml` | 去 resolve_node/instances/router filter → set_fact 派生 + 实例/端点生成；起容器/mooncake/模板/引擎/wait/proxy/冒烟逻辑不变 |
| `status.yml` | resolve_node 取端口 → 派生 `start_port` |
| `logs.yml`/`gen.yml` | 已不用 filter，仅变量名改平铺 |

`gen.yml` 的 `generated/` 输出目录：因 `ansible/` 自包含，改输出到 `ansible/generated/`（或临时目录）。

## 6. 删除与归位

**删除**：`ansible/filter_plugins/resolve.py`、项目根 `resolve/`、`config.yaml`、`config-a3.yaml`、`generate_inventory.py`、项目根 `pyproject.toml` / `.venv`。

**移入 ansible/**：`launch_online_dp.py`、`load_balance_proxy_server_example.py`、新写 `pyproject.toml`（ansible-core≥2.21、pytest、pyyaml）、`.venv`（`uv sync` 在 ansible/ 建）。

## 7. 测试改造

- 删 `tests/test_filters.py`（filter 删除）
- `tests/test_template_render.py`：模板变量已用小写，改为直接传小写变量渲染（不依赖 filter）
- 新增：派生表达式（节点参数/实例清单/proxy 端点）单测，用 sample group_vars/inventory 渲染验证
- 全量 `uv run pytest ansible/tests/` 通过；各 playbook `--syntax-check` 通过

## 8. 实机验证（等卡空闲）

- `start.yml`（A2）完整跑通：13/13 就绪 + 冒烟 + 端到端 200
- `status`/`stop`/`logs` 行为与现版一致
- A3 拓扑（16 卡、dp_size_local 2/4）至少 `--syntax-check` + 派生表达式的单测验证（A3 实机同样等卡）

## 9. 已知事项

- inventory/group_vars 手写，改节点/参数直接编辑（不再有 config 生成层）
- `simple` A3 切换：完整跑 start 需 A3 真机（未实机验证）
- 删除 config.yaml 后，旧 `deploy.sh` 方案不再可用（本设计为最终形态，交付物 = ansible/）