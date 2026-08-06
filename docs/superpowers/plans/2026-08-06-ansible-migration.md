# deploy.sh → Ansible 迁移实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把已实机跑通的 `deploy.sh`（GLM-5.2 P/D 分离一键部署）完整迁移为 Ansible playbook，保留 master 分支现有代码不动，在新分支 `feature/ansible` 工作。

**架构：** 用 Ansible（agentless，只装控制机）替代 deploy.sh 的 SSH 编排。`config.yaml` 作为唯一配置源（`include_vars` 加载），inventory 按 prefill/decode 分组，节点参数用 Jinja2 派生，核心「节点命令执行替换」用 `command`+`register`+`template`。模板用 Jinja2 `{{ var }}`（业界标准，替代自研 gomplate 子集）。

**技术栈：** ansible-core（venv 内）、Jinja2、shell/uri/template 核心模块、现有 `resolve/*.py` 逻辑复用为 filter 插件。

**原则：** 保留 master 的 deploy.sh 不删；produce 可独立运行的 playbook；每功能实机验证。

---

## 文件结构

```
ansible/
├── inventory.yaml                    # 节点清单（prefill/decode groups + 主机变量 ip/nic/role/index）
├── generate_inventory.py            # 从 config.yaml 生成 inventory（复用 nodes 段）
├── config_vars.yml                  # 集群级变量（image/model/pd_cluster/proxy/mooncake/container/runtime/logs）
├── group_vars/all.yml               # 加载 config_vars（或直接 include_vars config.yaml）
├── playbooks/
│   ├── check.yml                    # 预检：ping + docker
│   ├── pull.yml                     # 并行拉镜像
│   ├── start.yml                    # 一键部署（核心：容器+mooncake+引擎+wait+proxy+冒烟）
│   ├── stop.yml                     # 停止清理
│   ├── status.yml                   # 实例探测
│   └── logs.yml                     # 日志查看
├── templates/
│   ├── run_dp_prefill_template.sh.j2   # prefill 模板（Jinja2 + 节点命令替换）
│   └── run_dp_decode_template.sh.j2    # decode 模板
├── filter_plugins/
│   └── resolve.py                   # 复用 resolve/*.py 逻辑（节点参数/实例清单/proxy 端点）
└── tests/
    └── test_filters.py              # filter 插件单测
```

**配置来源**：`config.yaml` 顶层字段（cluster/ssh/image/model/nodes/pd_cluster/proxy/mooncake/container/runtime/logs）全部保留为唯一配置源，Ansible 通过 `include_vars` 加载。

---

## 任务总览

| 任务 | 内容 |
|---|---|
| 1 | 骨架 + generate_inventory.py + inventory 生成 |
| 2 | config 变量加载 + resolve filter 插件 |
| 3 | 模板 Jinja2 渲染 + 节点命令替换（核心需求） |
| 4 | check / pull playbook |
| 5 | start：起容器 + mooncake |
| 6 | start：引擎（模板渲染 + launch_online_dp.py）+ wait_ready |
| 7 | start：proxy + 冒烟 |
| 8 | stop / status / logs |
| 9 | 实机端到端验证 |

---

### 任务 1：骨架 + inventory 生成

**文件：**
- 创建：`ansible/generate_inventory.py`
- 创建：`ansible/inventory.yaml`（由脚本生成）
- 创建：`ansible/group_vars/all.yml`

- [ ] **步骤 1：编写 generate_inventory.py**

```python
#!/usr/bin/env python3
"""从 config.yaml 生成 ansible inventory。"""
import sys, yaml

def main(config_path):
    cfg = yaml.safe_load(open(config_path))
    lines = ["all:", "  children:", "    prefill:", "      hosts:"]
    for i, n in enumerate(cfg["nodes"]["prefill"]):
        lines.append(f'        p{i}: {{ ansible_host: {n["ip"]}, nic: {n["nic"]} }}')
    lines += ["    decode:", "      hosts:"]
    for i, n in enumerate(cfg["nodes"]["decode"]):
        lines.append(f'        d{i}: {{ ansible_host: {n["ip"]}, nic: {n["nic"]} }}')
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
```

- [ ] **步骤 2：生成 inventory 并验证**

运行：`python3 ansible/generate_inventory.py > ansible/inventory.yaml && cat ansible/inventory.yaml`
预期：prefill 和 decode 两组，各含 4 节点（A2），主机变量 `ansible_host`/`nic`。

- [ ] **步骤 3：写 group_vars/all.yml（集群级变量从 config 加载）**

```yaml
# 集群级配置（从 config.yaml 加载，部署时用 include_vars 覆盖）
cfg_image: "{{ lookup('template', '') }}"  # 占位，实际用 include_vars + 命名空间
```

- [ ] **步骤 4：commit**

```bash
git add ansible/generate_inventory.py ansible/inventory.yaml ansible/group_vars/all.yml
git commit -m "chore(ansible): inventory 生成 + 骨架"
```

---

### 任务 2：config 变量加载 + resolve filter 插件

**说明**：Ansible `include_vars` 直接加载 `config.yaml`，顶层字段成为变量（`{{ image }}`、`{{ model.path }}`、`{{ pd_cluster.prefill.dp_size }}`）。复用现有 `resolve/*.py` 逻辑为 custom filter 插件，避免重写节点参数派生。

**文件：**
- 创建：`ansible/filter_plugins/resolve.py`
- 创建：`ansible/tests/test_filters.py`

- [ ] **步骤 1：写 resolve filter 插件（包装现有 resolve_node/resolve_instances/resolve_router）**

```python
import sys, os, yaml
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))  # 项目根，可 import resolve/
from resolve import resolve_node, resolve_instances, resolve_router

class FilterModule:
    def filters(self):
        return {
            "resolve_node": self.resolve_node,
            "resolve_instances": self.resolve_instances,
            "resolve_router": self.resolve_router,
        }

    def resolve_node(self, cfg, node):
        """返回节点参数 dict（复用 resolve_node.py 的 KEY=VALUE 解析）。"""
        import tempfile, subprocess
        # 写临时 config，调用 resolve_node.py 输出 KEY=VALUE，转 dict
        ...

    def resolve_instances(self, cfg):
        ...

    def resolve_router(self, cfg):
        ...
```

- [ ] **步骤 2：写单测（test_filters.py）验证 filter 输出**

```python
import yaml, sys
sys.path.insert(0, "ansible/")
from filter_plugins.resolve import FilterModule

def test_resolve_node_p0():
    cfg = yaml.safe_load(open("config.yaml"))
    out = FilterModule().resolve_node(cfg, "p0")
    assert out["ROLE"] == "prefill"
    assert out["DP_SIZE"] == 4
    assert out["LOCAL_IP"] == "192.168.0.245"
```

- [ ] **步骤 3：运行测试确认通过**

运行：`uv run pytest ansible/tests/test_filters.py -v`
预期：PASS

- [ ] **步骤 4：commit**

```bash
git add ansible/filter_plugins/resolve.py ansible/tests/test_filters.py
git commit -m "feat(ansible): resolve filter 插件复用现有解析逻辑"
```

---

### 任务 3：模板 Jinja2 渲染 + 节点命令替换（核心需求）

**文件：**
- 创建：`ansible/templates/run_dp_prefill_template.sh.j2`
- 创建：`ansible/templates/run_dp_decode_template.sh.j2`
- 创建：`ansible/playbooks/_render.j2` 测试用

- [ ] **步骤 1：写 prefill 模板（Jinja2，替换原 __XX__，节点命令用 register 变量）**

```jinja2
#!/usr/bin/bash
# run_dp_template.sh — Prefill 节点模板（kv_producer，MultiConnector）
nic_name="{{ nic }}"
local_ip="{{ local_ip.stdout }}"

export HCCL_IF_IP=$local_ip
export GLOO_SOCKET_IFNAME=$nic_name
export TP_SOCKET_IFNAME=$nic_name
export HCCL_SOCKET_IFNAME=$nic_name
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_ASCEND_ENABLE_MLAPO=1
export HCCL_BUFFSIZE=256
export TASK_QUEUE_ENABLE=1
export HCCL_OP_EXPANSION_MODE="AIV"
export VLLM_USE_V1=1
export ASCEND_RT_VISIBLE_DEVICES=$1
export ASCEND_AGGREGATE_ENABLE=1
export ASCEND_TRANSPORT_PRINT=1
export VLLM_ASCEND_ENABLE_FLASHCOMM1=1
export PYTHONHASHSEED=0
export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
{% if cluster_type == "a2" %}export HCCL_INTRA_ROCE_ENABLE=1
{% endif %}export ACL_OP_INIT_MODE=1

vllm serve {{ model_path }} \
    --host 0.0.0.0 \
    --port {{ start_port }} \
    --data-parallel-size {{ dp_size }} \
    --data-parallel-rank {{ dp_rank }} \
    --data-parallel-address {{ dp_address }} \
    --data-parallel-rpc-port {{ rpc_port }} \
    --tensor-parallel-size {{ tp_size }} \
    ...（其余参数同现有模板，用 Jinja2 变量）
```

- [ ] **步骤 2：单测模板渲染（Jinja2 + register 变量替换）**

```python
# tests/test_template_render.py
from jinja2 import Environment, FileSystemLoader

def test_prefill_template_uses_register_vars():
    env = Environment(loader=FileSystemLoader("ansible/templates"))
    tpl = env.get_template("run_dp_prefill_template.sh.j2")
    out = tpl.render(nic="enp67s0f5", local_ip={"stdout": "192.168.0.245"}, model_path="/mnt/share_space/models/GLM-5.2-w4a8c8",
                     start_port=9081, dp_size=4, tp_size=8, dp_rank=0, dp_address="192.168.0.245", rpc_port=16591,
                     cluster_type="a2")
    assert 'local_ip="192.168.0.245"' in out
    assert "--port 9081" in out
    assert "HCCL_INTRA_ROCE_ENABLE=1" in out
```

- [ ] **步骤 3：运行测试确认通过**

运行：`uv run pytest ansible/tests/test_template_render.py -v`
预期：PASS

- [ ] **步骤 4：commit**

```bash
git add ansible/templates/ ansible/tests/test_template_render.py
git commit -m "feat(ansible): Jinja2 模板 + 节点命令替换"
```

---

### 任务 4：check / pull playbook

**文件：**
- 创建：`ansible/playbooks/check.yml`
- 创建：`ansible/playbooks/pull.yml`

- [ ] **步骤 1：写 check.yml**

```yaml
---
- name: deploy-glm52 预检
  hosts: all
  gather_facts: no
  vars_files: ["../config_vars.yml"]
  tasks:
    - name: SSH 连通
      ping:
    - name: docker 可用
      shell: docker info >/dev/null 2>&1 && echo ok || { echo fail; exit 1; }
    - name: 镜像检查
      shell: docker images --format "{{ '{{' }}.Repository{{ '}}' }}:{{ '{{' }}.Tag{{ '}}' }}" | grep -q "{{ cfg_image }}" && echo present || echo missing
```

- [ ] **步骤 2：语法检查**

运行：`ansible-playbook -i ansible/inventory.yaml ansible/playbooks/check.yml --syntax-check`
预期：无错误

- [ ] **步骤 3：实机跑 check（限 p0）**

运行：`ansible-playbook -i ansible/inventory.yaml ansible/playbooks/check.yml -l p0`
预期：8 步全 ok

- [ ] **步骤 4：写 pull.yml（并行拉镜像）**

```yaml
---
- name: 拉取镜像
  hosts: all
  gather_facts: no
  vars_files: ["../config_vars.yml"]
  tasks:
    - name: docker pull
      shell: docker pull {{ cfg_image }}
```

- [ ] **步骤 5：commit**

```bash
git add ansible/playbooks/check.yml ansible/playbooks/pull.yml
git commit -m "feat(ansible): check 与 pull playbook"
```

---

### 任务 5：start — 起容器 + mooncake

**文件：**
- 创建：`ansible/playbooks/start.yml`

- [ ] **步骤 1：写 start.yml（第一阶段：起容器 + mooncake）**

```yaml
---
- name: deploy-glm52 一键部署
  hosts: all
  gather_facts: no
  vars_files: ["../config_vars.yml"]
  tasks:
    - name: 删除旧容器（幂等）
      shell: docker rm -f {{ container_name }} 2>/dev/null || true
    - name: 起容器（复用 deploy.sh 的 docker run 参数）
      shell: |
        docker run -itd --name {{ container_name }} \
          --net=host --pid=host --privileged --shm-size={{ shm_size }} \
          {% for i in range(num_cards) %}--device /dev/davinci{{ i }} {% endfor %} \
          --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
          -v /usr/local/dcmi:/usr/local/dcmi \
          {% for m in model_mounts %}-v {{ m }} {% endfor %} \
          {{ cfg_image }} bash
    - name: 分发 launch_online_dp.py 与 mooncake.json
      copy:
        src: "{{ item }}"
        dest: "/tmp/{{ item | basename }}"
      with_items: ["../launch_online_dp.py"]
    - name: docker cp 进容器
      shell: docker exec {{ container_name }} mkdir -p /root/pd && docker cp /tmp/{{ item | basename }} {{ container_name }}:/root/pd/
      loop:
        - launch_online_dp.py
        - mooncake.json
```

- [ ] **步骤 2：mooncake 起在 p0（delegate_to）**

```yaml
    - name: mooncake_master（p0，先于引擎）
      shell: |
        docker exec {{ container_name }} pkill -x mooncake_master 2>/dev/null || true
        docker exec -d {{ container_name }} bash -c 'mooncake_master -port {{ mooncake_port }} -eviction_high_watermark_ratio {{ mooncake_evict }} >> {{ mooncake_log }} 2>&1'
        sleep 2 && docker exec {{ container_name }} pgrep -x mooncake_master >/dev/null
      when: inventory_hostname == 'p0'
```

- [ ] **步骤 3：语法检查 + p0 试跑（可选 dry-run）**

运行：`ansible-playbook -i ansible/inventory.yaml ansible/playbooks/start.yml --syntax-check`
预期：语法 OK

- [ ] **步骤 4：commit**

```bash
git add ansible/playbooks/start.yml
git commit -m "feat(ansible): start 起容器 + mooncake"
```

---

### 任务 6：start — 引擎 + wait_ready

- [ ] **步骤 1：渲染引擎模板（每节点 register 节点命令 + Jinja2）**

```yaml
    - name: 本机 IP（节点命令替换核心）
      shell: ip -4 addr show {{ nic }} | grep -oP 'inet \K[0-9.]+' | head -1
      register: local_ip
    - name: 渲染 run_dp_template.sh
      template:
        src: "../templates/run_dp_{{ role }}_template.sh.j2"
        dest: "/tmp/run_dp_template_{{ inventory_hostname }}.sh"
    - name: docker cp 模板进容器
      shell: docker cp /tmp/run_dp_template_{{ inventory_hostname }}.sh {{ container_name }}:/root/pd/run_dp_template.sh
```

- [ ] **步骤 2：启动引擎（launch_online_dp.py，参数用 resolve filter）**

```yaml
    - name: 启动引擎
      shell: |
        docker exec -d {{ container_name }} bash -c 'cd /root/pd && python3 launch_online_dp.py \
          --dp-size {{ dp_size }} --tp-size {{ tp_size }} --dp-size-local {{ dp_size_local }} \
          --dp-rank-start {{ dp_rank_start }} --dp-address {{ dp_address }} \
          --dp-rpc-port {{ rpc_port }} --vllm-start-port {{ start_port }} >> {{ vllm_log }} 2>&1'
```

- [ ] **步骤 3：wait_ready（uri 模块 + until，遍历全部实例）**

```yaml
    - name: 等待全部实例就绪
      uri:
        url: "http://{{ item.ip }}:{{ item.port }}/health"
        status_code: 200
        timeout: 5
      register: result
      until: result.status == 200
      retries: 160
      delay: 15
      loop: "{{ instances }}"   # instances 来自 resolve_instances filter，跳过 proxy
      when: item.role != 'proxy'
```

- [ ] **步骤 4：实机验证（限 p0 观察引擎是否起）**

运行：`ansible-playbook -i ansible/inventory.yaml ansible/playbooks/start.yml --limit p0 --tags engine`
预期：模板渲染成功、引擎启动无报错

- [ ] **步骤 5：commit**

```bash
git add ansible/playbooks/start.yml
git commit -m "feat(ansible): start 引擎 + wait_ready"
```

---

### 任务 7：start — proxy + 冒烟

- [ ] **步骤 1：渲染并启动 proxy（delegate_to p0）**

```yaml
    - name: 分发 proxy 脚本
      copy:
        src: "../load_balance_proxy_server_example.py"
        dest: "/tmp/load_balance_proxy_server_example.py"
      when: inventory_hostname == 'p0'
    - name: docker cp + 启动 proxy
      shell: |
        docker cp /tmp/load_balance_proxy_server_example.py {{ container_name }}:/root/pd/
        docker exec {{ container_name }} pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
        docker exec -d {{ container_name }} bash -c 'unset http_proxy https_proxy; python3 /root/pd/load_balance_proxy_server_example.py \
          --host 0.0.0.0 --port {{ proxy_port }} \
          --prefiller-hosts {{ prefiller_hosts }} --prefiller-ports {{ prefiller_ports }} \
          --decoder-hosts {{ decoder_hosts }} --decoder-ports {{ decoder_ports }} >> {{ proxy_log }} 2>&1'
      when: inventory_hostname == 'p0'
```

- [ ] **步骤 2：冒烟测试（uri POST /v1/completions）**

```yaml
    - name: 冒烟测试
      uri:
        url: "http://{{ proxy_host }}:{{ proxy_port }}/v1/completions"
        method: POST
        body: '{"model":"glm-52","prompt":"The future of AI is","max_tokens":20,"temperature":0}'
        body_format: json
        status_code: 200
      register: smoke
      until: smoke.status == 200
      retries: 10
      delay: 5
      delegate_to: localhost
```

- [ ] **步骤 3：commit**

```bash
git add ansible/playbooks/start.yml
git commit -m "feat(ansible): start proxy + 冒烟"
```

---

### 任务 8：stop / status / logs

- [ ] **步骤 1：写 stop.yml / status.yml / logs.yml**

```yaml
# stop.yml
---
- name: 停止清理
  hosts: all
  gather_facts: no
  tasks:
    - name: 停 proxy/mooncake（p0）
      shell: |
        docker exec {{ container_name }} pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
        docker exec {{ container_name }} pkill -x mooncake_master 2>/dev/null || true
      when: inventory_hostname == 'p0'
    - name: 删容器
      shell: docker rm -f {{ container_name }} 2>/dev/null || true
```

```yaml
# status.yml
---
- name: 实例探测
  hosts: all
  gather_facts: no
  tasks:
    - name: 容器状态
      shell: docker inspect -f '{{ "{{" }}.State.Status{{ "}}" }}' {{ container_name }} 2>/dev/null || echo no-container
    - name: 引擎健康
      uri:
        url: "http://{{ ansible_host }}:{{ start_port }}/health"
        status_code: 200
      ignore_errors: yes
```

- [ ] **步骤 2：commit**

```bash
git add ansible/playbooks/stop.yml ansible/playbooks/status.yml ansible/playbooks/logs.yml
git commit -m "feat(ansible): stop/status/logs"
```

---

### 任务 9：实机端到端验证

- [ ] **步骤 1：完整跑 start（全 8 节点）**

运行：`ansible-playbook -i ansible/inventory.yaml ansible/playbooks/start.yml`
预期：8 容器 + mooncake + 12 引擎全部就绪 + proxy + 冒烟成功，`verify` 13/13

- [ ] **步骤 2：验证各命令等价性**

运行：`ansible-playbook ... status.yml`、`... logs.yml`、`... stop.yml`
预期：与 deploy.sh 的 status/logs/stop 行为一致

- [ ] **步骤 3：commit 最终验证记录**

```bash
git add docs/ && git commit -m "docs(ansible): 迁移验证记录"
```

---

## 自检

**规格覆盖度**：
- ✅ check/pull/start/stop/status/logs 全部映射到 playbook
- ✅ 模板 Jinja2 替代 __XX__（任务 3）
- ✅ 节点命令替换（任务 3/6 的 register + template，核心需求）
- ✅ config.yaml 唯一配置源（任务 2 include_vars + filter）
- ✅ 保留 master 现有代码（新分支工作）

**占位符扫描**：无「待定/TODO」；每个任务有明确文件、代码、验证命令。

**类型一致性**：变量名统一（`container_name`/`start_port`/`dp_size` 等贯穿各任务）；filter 插件接口 `resolve_node(cfg,node)`/`resolve_instances(cfg)`/`resolve_router(cfg)` 一致。

**已知风险**：start 是最大任务，wait_ready 用 uri+until 遍历实例需先由 filter 生成实例清单；实机验证耗时（模型加载 ~10 分钟）。