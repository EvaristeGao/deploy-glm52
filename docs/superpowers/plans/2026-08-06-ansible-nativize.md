# Ansible 原生化（去 resolve）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现。步骤用复选框（`- [ ]`）跟踪进度。

**目标：** 去掉 `ansible/filter_plugins/resolve.py` 及 `resolve/`、`config.yaml` 依赖，参数改用 Ansible 原生（inventory + group_vars + Jinja2）承载；`ansible/` 完全自包含（含 venv、运行时脚本），支持 A2/A3 双集群。

**架构：** inventory 按 prefill/decode/proxy/mooncake 分组 + 主机变量（`ansible_host/nic/idx`）；group_vars 平铺集群参数；playbook 用 set_fact + Jinja2 派生节点参数/实例清单/proxy 端点。

**技术栈：** ansible-core（venv）、Jinja2、pytest。**不占卡**（实机验证延期）。

**规格：** `docs/superpowers/specs/2026-08-06-ansible-nativize-design.md`

---

## 文件结构（目标态）

```
ansible/
├── .venv/                         # uv 建
├── pyproject.toml                # 依赖 ansible-core≥2.21/pytest/pyyaml
├── ansible.cfg                   # filter 插件（删后仅 defaults）
├── inventories/
│   ├── a2/{inventory.yaml, group_vars/all.yml}
│   └── a3/{inventory.yaml, group_vars/all.yml}
├── playbooks/{check,pull,start,stop,status,logs,gen}.yml
├── templates/run_dp_{prefill,decode}_template.sh.j2
├── launch_online_dp.py           # 从项目根移入
├── load_balance_proxy_server_example.py  # 从项目根移入
├── generated/                    # gen.yml 输出（运行时，gitignore）
├── tests/
└── README.md
```

**删除**（任务 7）：`config.yaml`、`config-a3.yaml`、`resolve/`、`ansible/filter_plugins/`、`generate_inventory.py`、项目根 `pyproject.toml`/`.venv`/`ansible.cfg`（已移入）。

---

## 任务 1：ansible/ 自包含骨架 + venv

**文件：**
- 创建：`ansible/pyproject.toml`
- 创建：`ansible/.venv`（uv sync）
- 修改：`ansible/ansible.cfg`（删 filter_plugins 行）
- 移动：`launch_online_dp.py`、`load_balance_proxy_server_example.py` → `ansible/`

- [ ] **步骤 1：写 ansible/pyproject.toml**

```yaml
[project]
name = "deploy-glm52-ansible"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["ansible-core>=2.21"]

[dependency-groups]
dev = ["pytest>=8.0", "pyyaml>=6.0"]
```

- [ ] **步骤 2：在 ansible/ 建 venv**

运行：`cd ansible && uv sync`
预期：`.venv/` 生成，ansible-core/pytest/pyyaml 装好。

- [ ] **步骤 3：移动运行时脚本**

运行：`git mv launch_online_dp.py ansible/launch_online_dp.py && git mv load_balance_proxy_server_example.py ansible/load_balance_proxy_server_example.py`

- [ ] **步骤 4：ansible.cfg 删 filter_plugins 行**

编辑 `ansible/ansible.cfg`，删除 `filter_plugins = filter_plugins` 行（filter 插件即将删除）。

- [ ] **步骤 5：commit**

```bash
git add ansible/ && git commit -m "chore(ansible): 自包含骨架(venv+pyproject) + 运行时脚本归位"
```

---

## 任务 2：inventory + group_vars（A2/A3）

**文件：**
- 创建：`ansible/inventories/a2/inventory.yaml`、`ansible/inventories/a2/group_vars/all.yml`
- 创建：`ansible/inventories/a3/inventory.yaml`、`ansible/inventories/a3/group_vars/all.yml`

- [ ] **步骤 1：写 A2 inventory（8 节点）**

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

- [ ] **步骤 2：写 A2 group_vars/all.yml**

```yaml
ansible_user: root
ansible_ssh_private_key_file: /home/g00832294/.ssh/KeyPair-2956.pem
ansible_ssh_common_args: "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ConnectionAttempts=3 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
ansible_ssh_retries: 3

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

- [ ] **步骤 3：写 A3 inventory + group_vars**

A3：4 节点（p0-p1 + d0-d1，IP 待填空或占位），`group_vars`：`image quay.io/ascend/vllm-ascend:v0.23.0rc1-a3`、`model_path: /root/.cache/modelscope/hub/models/vllm-ascend/GLM-5.2-w4a8c8`、`num_cards: 16`、`prefill.dp_size_local: 2`、`decode.dp_size_local: 4`、`proxy.port: 8000`、`cluster_type: a3`、`container.name` 区分。

- [ ] **步骤 4：验证 inventory 解析**

运行：`cd ansible && .venv/bin/ansible-inventory -i inventories/a2/inventory.yaml --list | head`
预期：prefill/decode 组 + 主机变量正确。

- [ ] **步骤 5：commit**

```bash
git add ansible/inventories/ && git commit -m "feat(ansible): inventory + group_vars (A2/A3) 原生配置"
```

---

## 任务 3：节点参数派生 set_fact + 单测

**文件：**
- 创建：`ansible/tests/test_derive.py`

- [ ] **步骤 1：写派生逻辑（先在测试里定义期望，再实现）**

派生表达式（设计规格 §4.1）：`node_role = 'prefill' if hostname[0]=='p' else 'decode'`、`node_idx = hostname[1:]|int`、`dp_rank = node_idx * dp_size_local`、`dp_address = hostvars[groups[role][0]].ansible_host` 等。

- [ ] **步骤 2：写 test_derive.py（用 sample group_vars + 模拟 hostvars/groups 渲染）**

```python
import pytest
from jinja2 import Environment

A2_GV = {...}  # 与 a2/group_vars/all.yml 一致（内联小字典）

def test_derive_p0_rank():
    # p0: node_idx=0, prefill, dp_rank = 0*1 = 0
    ...
def test_derive_d1_rank():
    # d1: node_idx=1, decode, dp_rank = 1*2 = 2
    ...
def test_dp_address_prefill():
    # = groups['prefill'][0].ansible_host = 192.168.0.245
    ...
```

- [ ] **步骤 3：跑测试确认**

运行：`cd ansible && uv run pytest tests/test_derive.py -v`
预期：PASS

- [ ] **步骤 4：commit**

```bash
git add ansible/tests/test_derive.py && git commit -m "feat(ansible): 节点参数派生 Jinja2 + 单测"
```

---

## 任务 4：实例清单 / proxy 端点生成 + 单测

**文件：**
- 修改：`ansible/tests/test_derive.py`

- [ ] **步骤 1：写实例清单生成表达式（Jinja2）**

从 `groups['prefill']`/`groups['decode']` × `dp_size_local` 生成 `[[role, ip, base_port+r],...]`，proxy 单列。用 product/nested loop。

- [ ] **步骤 2：写 proxy 端点生成表达式**

`prefiller_hosts` = 各 prefill 节点 IP 重复 `dp_size_local` 次；`prefiller_ports` = `base_port+r` 递增。decode 同理。

- [ ] **步骤 3：test_derive.py 补实例清单 + proxy 端点用例**

```python
def test_instances_count_a2():
    # prefill 4×1 + decode 4×2 + proxy 1 = 13
    ...
def test_proxy_endpoints_decode():
    # decoder_hosts 长度 = 4×2=8, decoder_ports 9900 9901 ...
    ...
```

- [ ] **步骤 4：跑测试确认**

运行：`cd ansible && uv run pytest tests/test_derive.py -v`
预期：PASS

- [ ] **步骤 5：commit**

```bash
git add ansible/tests/test_derive.py && git commit -m "feat(ansible): 实例清单/proxy 端点 Jinja2 + 单测"
```

---

## 任务 5：start.yml 原生化改造

**文件：**
- 修改：`ansible/playbooks/start.yml`

- [ ] **步骤 1：去 filter，改派生**

把 `cfg | resolve_node(inventory_hostname)` 等 filter 调用替换为 set_fact 派生（任务 3 表达式）。删除 `lookup('file','../../config.yaml')`，变量改用 group_vars 平铺名（`image`/`model_path`/`pd_cluster.*`/`model_name`/`container.name`/`logs.dir` 等）。

- [ ] **步骤 2：wait_ready 用实例清单**

`resolve_instances` filter → 任务 4 的 Jinja2 实例清单（run_once set_fact）。

- [ ] **步骤 3：proxy 端点用生成**

`resolve_router` filter → 任务 4 的端点生成（run_once）。

- [ ] **步骤 4：模板渲染变量**

模板变量名已用小写（`start_port`/`dp_size`/`dp_rank`/`dp_address`/`rpc_port`/`tp_size`/`kv_port`/`model_path`/`served_model_name`/`max_model_len`/`cluster_type`/`local_ip`/`nic`/`prefill_dp`/`decode_dp` 等），set_fact 派生后直接可用。`served_model_name` 从 `model_name` 映射。

- [ ] **步骤 5：语法检查（不占卡）**

运行：`cd ansible && uv run ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml --syntax-check`
预期：通过

- [ ] **步骤 6：commit**

```bash
git add ansible/playbooks/start.yml && git commit -m "feat(ansible): start.yml 原生化(去 resolve filter)"
```

---

## 任务 6：status/logs/gen 原生化

**文件：**
- 修改：`ansible/playbooks/status.yml`、`logs.yml`、`gen.yml`

- [ ] **步骤 1：status.yml**

`resolve_node` 取端口 → 派生 `start_port`；变量名改平铺。

- [ ] **步骤 2：logs.yml**

去 `cfg` lookup，用 group_vars 平铺（`logs.dir`/`logs.vllm`/`logs.mooncake`/`logs.proxy`/`container.name`）。

- [ ] **步骤 3：gen.yml**

输出目录改 `ansible/generated/`；渲染用 group_vars 平铺变量。

- [ ] **步骤 4：语法检查**

运行：`cd ansible && for p in status logs gen; do uv run ansible-playbook -i inventories/a2/inventory.yaml playbooks/$p.yml --syntax-check; done`
预期：全过

- [ ] **步骤 5：commit**

```bash
git add ansible/playbooks/ && git commit -m "feat(ansible): status/logs/gen 原生化"
```

---

## 任务 7：删除 resolve/config + 测试改造

**文件：**
- 删除：`resolve/`、`config.yaml`、`config-a3.yaml`、`ansible/filter_plugins/`、`generate_inventory.py`、项目根 `pyproject.toml`/`.venv`/`ansible.cfg`（已移入）
- 删除：`ansible/tests/test_filters.py`
- 修改：`ansible/tests/test_template_render.py`（直接传小写变量渲染，不依赖 filter）

- [ ] **步骤 1：删除文件**

运行：`git rm -r resolve config.yaml config-a3.yaml ansible/filter_plugins generate_inventory.py ansible/tests/test_filters.py`（项目根 pyproject/.venv 若被忽略则删工作区）

- [ ] **步骤 2：改 test_template_render.py**

去 filter 依赖，直接用小写变量 dict 渲染模板。

- [ ] **步骤 3：全量单测**

运行：`cd ansible && uv run pytest tests/ -v`
预期：全过（test_derive + test_template_render）

- [ ] **步骤 4：全 playbook 语法检查（A2/A3）**

运行：`cd ansible && for i in a2 a3; do for p in playbooks/*.yml; do uv run ansible-playbook -i inventories/$i/inventory.yaml $p --syntax-check; done; done`
预期：全过

- [ ] **步骤 5：commit**

```bash
git add -A && git commit -m "refactor(ansible): 删除 resolve/config，测试改原生"
```

---

## 任务 8：README 更新 + 收尾

**文件：**
- 修改：`ansible/README.md`

- [ ] **步骤 1：更新 README**

目录结构（inventories/）、运行方式（`-i inventories/a2/inventory.yaml`）、删除 config.yaml 说明、交付清单（仅 ansible/）。

- [ ] **步骤 2：commit**

```bash
git add ansible/README.md && git commit -m "docs(ansible): README 更新为原生化(自包含+双集群)"
```

---

## 任务 9：实机端到端验证（延期，不占卡）

- [ ] **延期**：`start.yml`（A2）完整跑通、status/stop/logs 行为一致。等卡空闲执行。

---

## 自检

**规格覆盖度**：✅ 目录结构（任务 1/2）、派生参数（任务 3）、实例/proxy 端点（任务 4）、start 改造（任务 5）、status/logs/gen（任务 6）、删除（任务 7）、README（任务 8）、实机（任务 9，延期）。

**占位符扫描**：无「待定/TODO」；每个任务有文件/代码/验证/commit。

**类型一致性**：变量名统一（`model_path`/`pd_cluster.*`/`container.name`/`logs.*` 平铺贯穿）；`node_role`/`node_idx`/`dp_rank` 等派生名一致。

**已知风险**：A3 inventory IP 为空（待填）；实机验证延期；`ansible-inventory` 需在 venv 内跑。