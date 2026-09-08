# proxy 接入节点白名单 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** start.yml 的 proxy 端点生成支持按 group_vars 白名单（`proxy.prefill_nodes`/`proxy.decode_nodes`）过滤接入节点，P/D 对称，空/未配置 = 全量接入，用于模拟 decode 排队等场景。

**架构：** 只改 start.yml 的 proxy 端点生成任务（前置 assert 校验 + `vars:` 段解析白名单 `pn`/`dn`，四个端点表达式循环源从 `groups[...]` 换成 `pn`/`dn`）。proxy 脚本、引擎启动、wait_ready 全部零改动。单测直接渲染 start.yml 任务的真实表达式（不在测试里复制镜像表达式，防漂移）。

**技术栈：** Ansible（Jinja2 + set_fact）、pytest + pyyaml + jinja2（本地 NativeEnvironment 模拟 Ansible 渲染语义）。

**规格：** `docs/superpowers/specs/2026-09-08-proxy-nodes-whitelist-design.md`

**关键实现事实（已核实）：**
- `default(x, true)` 对 undefined **和 falsy（空列表）** 都回退默认值 → 单表达式覆盖「键未配置」与「显式空列表」。
- Ansible 同一任务模板先整体渲染再执行 → set_fact 键间不能引用，白名单解析必须放 `vars:` 段。
- `difference` 是 Ansible 内置过滤，纯 jinja2 没有 → 测试的 `_new_env()` 需注册。
- start.yml 的端点表达式以 `| join(' ')` 结尾（空格分隔字符串），测试渲染结果直接按字符串断言。
- HEAD 中 proxy 端点任务在 `playbooks/start.yml:324`（`生成 proxy 端点`），A2 group_vars `proxy: { port: 1999 }`，A3 `proxy: { port: 8000 }`。
- pyyaml 已在 dev 依赖组（pyproject.toml），`tests/test_template_render.py` 已有 `import yaml` 先例。

---

### 任务 1：测试先行——start.yml 真实表达式渲染用例（红）

**文件：**
- 修改：`ansible/tests/test_derive.py`（顶部 import；`_new_env()`；文件末尾追加 helper + 4 个测试）

- [ ] **步骤 1：加 import 与 start.yml 路径**

`test_derive.py` 顶部 import 区（现只有 itertools/jinja2）改为：

```python
import os
from itertools import product as _itertools_product

import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

START_YML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "playbooks", "start.yml",
)
```

- [ ] **步骤 2：`_new_env()` 注册 `difference` 过滤**

```python
def _new_env():
    """返回与 Ansible 运行时一致的 NativeEnvironment。

    Ansible 的 jinja2 环境额外提供 product 过滤（ansible.builtin），纯 jinja2 需
    手动注册，才能在本地等价模拟 playbook 里的 product 表达式。
    """
    env = NativeEnvironment(undefined=StrictUndefined)
    env.filters["product"] = lambda *iterables: list(_itertools_product(*iterables))
    # Ansible 内置集合过滤（proxy 白名单 assert 校验用）：difference = 差集
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    return env
```

- [ ] **步骤 3：追加 helper + 4 个测试（文件末尾）**

```python
# ---- proxy 接入节点白名单（规格 docs/superpowers/specs/2026-09-08-proxy-nodes-whitelist-design.md）----
# 直接渲染 playbooks/start.yml 里「生成 proxy 端点」「校验 proxy 白名单」任务的真实表达式，
# 不在测试内复制镜像表达式（防两处漂移）。模拟 Ansible 语义：任务 vars:（pn/dn）先渲染注入
# ctx，再渲染 set_fact / assert 各表达式（同任务键间不能引用，vars: 渲染期已入作用域）。

START_YML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "playbooks", "start.yml",
)


def _playbook_task(name_prefix):
    """从 start.yml 提取 name 以 name_prefix 开头的任务；找不到则断言失败。"""
    with open(START_YML, encoding="utf-8") as f:
        play = yaml.safe_load(f)
    for t in play[0]["tasks"]:
        if (t.get("name") or "").startswith(name_prefix):
            return t
    raise AssertionError(f"start.yml 未找到以 {name_prefix!r} 开头的任务")


def _render_start_yml_endpoints(proxy=None):
    """渲染 start.yml「生成 proxy 端点」任务的 vars: + set_fact 表达式。

    proxy 传 dict 覆盖 A2_GV['proxy'] 注入白名单配置（须含 port：StrictUndefined 下
    proxy.port 缺失会渲染失败）。
    """
    t = _playbook_task("生成 proxy 端点")
    env = _new_env()
    ctx = dict(A2_GV, groups=GROUPS, hostvars=HOSTVARS)
    if proxy is not None:
        ctx["proxy"] = proxy
    for name, tpl in (t.get("vars") or {}).items():
        ctx[name] = env.from_string(tpl).render(**ctx)
    return {k: env.from_string(v).render(**ctx) for k, v in (t.get("set_fact") or {}).items()}


def test_start_yml_endpoints_decode_whitelist_d0():
    """decode 白名单 [d0]：decoder 端点只含 d0 的 2 个实例，prefill 不受影响。"""
    pe = _render_start_yml_endpoints(proxy={"port": 1999, "decode_nodes": ["d0"]})
    assert pe["decoder_hosts"] == "192.168.0.127 192.168.0.127"
    assert pe["decoder_ports"] == "9900 9901"
    assert pe["prefiller_hosts"] == "192.168.0.245 192.168.0.15 192.168.0.160 192.168.0.91"
    assert pe["prefiller_ports"] == "9081 9081 9081 9081"


def test_start_yml_endpoints_prefill_whitelist_p0():
    """prefill 白名单 [p0]：prefiller 端点只含 p0，decode 全量不受影响。"""
    pe = _render_start_yml_endpoints(proxy={"port": 1999, "prefill_nodes": ["p0"]})
    assert pe["prefiller_hosts"] == "192.168.0.245"
    assert pe["prefiller_ports"] == "9081"
    assert pe["decoder_hosts"] == (
        "192.168.0.127 192.168.0.127 192.168.0.161 192.168.0.161 "
        "192.168.0.154 192.168.0.154 192.168.0.140 192.168.0.140"
    )
    assert pe["decoder_ports"] == "9900 9901 9900 9901 9900 9901 9900 9901"


def test_start_yml_endpoints_empty_whitelist_equals_full():
    """白名单空列表 = 全量接入（与键缺省渲染结果逐项一致）。"""
    pe_full = _render_start_yml_endpoints()
    pe_empty = _render_start_yml_endpoints(
        proxy={"port": 1999, "prefill_nodes": [], "decode_nodes": []}
    )
    assert pe_empty == pe_full


def test_start_yml_whitelist_assert_rejects_unknown_node():
    """start.yml assert：白名单含未知节点名 → 对应表达式判定 False（fail-fast）。"""
    env = _new_env()
    t = _playbook_task("校验 proxy 白名单")
    ctx = dict(A2_GV, groups=GROUPS, hostvars=HOSTVARS)
    ctx["proxy"] = {"port": 1999, "decode_nodes": ["d0", "dX"]}
    results = [env.from_string(tpl).render(**ctx) for tpl in t["assert"]["that"]]
    assert results == [True, False]  # prefill 未配置→合法；decode 含 dX→非法
```

注意：`START_YML` 在步骤 1 已加过，步骤 3 的代码块中如重复定义以一处为准（实现时只保留一份，放 import 区之后）。

- [ ] **步骤 4：运行测试验证失败（红）**

运行：`cd ansible && .venv/bin/python -m pytest tests/test_derive.py -q`
预期：11 个旧用例 PASS；4 个新用例 FAIL——3 个端点用例报 `pn is undefined`（当前 start.yml 无 `vars:` 段），assert 用例报 `start.yml 未找到以 '校验 proxy 白名单' 开头的任务`。

- [ ] **步骤 5：不 commit（红态随任务 2 一起提交）**

---

### 任务 2：start.yml 实现（绿）

**文件：**
- 修改：`ansible/playbooks/start.yml:324-335`（「生成 proxy 端点」任务整体替换 + 前插 assert 任务）

- [ ] **步骤 1：替换任务块（Edit，old_string 为现有整块，new_string 如下）**

```yaml
    - name: 校验 proxy 白名单节点名（proxy.prefill_nodes/decode_nodes 只能填对应组节点名，非法 fail-fast）
      run_once: true
      assert:
        that:
          - proxy.prefill_nodes | default([]) | difference(groups['prefill']) | length == 0
          - proxy.decode_nodes | default([]) | difference(groups['decode']) | length == 0
        fail_msg: >-
          proxy.prefill_nodes/decode_nodes 含 inventory 中不存在的节点名：
          {{ (proxy.prefill_nodes | default([])) | difference(groups['prefill']) | join(' ') }}
          {{ (proxy.decode_nodes | default([])) | difference(groups['decode']) | join(' ') }}
      tags: proxy

    - name: 生成 proxy 端点（Jinja2 生成；run_once 于 p0 并回收 facts；白名单解析见 vars pn/dn）
      run_once: true
      delegate_to: p0
      delegate_facts: yes
      vars:
        # 接入白名单（group_vars proxy.prefill_nodes/decode_nodes）：undefined 或空列表都回退全量
        # （default(x, true) 对 falsy 也生效）。放 vars: 而非 set_fact 键——同任务模板先整体
        # 渲染再执行，键间不能引用；vars: 渲染期已入作用域。
        pn: "{{ proxy.prefill_nodes | default(groups['prefill'], true) }}"
        dn: "{{ proxy.decode_nodes | default(groups['decode'], true) }}"
      set_fact:
        proxy_host: "{{ hostvars[groups['proxy'][0]].ip }}"
        proxy_port: "{{ proxy.port | string }}"
        prefiller_hosts: "{% set out = [] -%}{% for h, r in pn | product(range(pd_cluster.prefill.dp_size_local)) -%}{% set _ = out.append(hostvars[h].ip) -%}{% endfor -%}{{ out | join(' ') }}"
        prefiller_ports: "{% set out = [] -%}{% for h, r in pn | product(range(pd_cluster.prefill.dp_size_local)) -%}{% set _ = out.append((pd_cluster.prefill.base_port + r) | string) -%}{% endfor -%}{{ out | join(' ') }}"
        decoder_hosts: "{% set out = [] -%}{% for h, r in dn | product(range(pd_cluster.decode.dp_size_local)) -%}{% set _ = out.append(hostvars[h].ip) -%}{% endfor -%}{{ out | join(' ') }}"
        decoder_ports: "{% set out = [] -%}{% for h, r in dn | product(range(pd_cluster.decode.dp_size_local)) -%}{% set _ = out.append((pd_cluster.decode.base_port + r) | string) -%}{% endfor -%}{{ out | join(' ') }}"
      tags: proxy
```

（四个端点表达式相对现版的**唯一**差异：循环源 `groups['prefill']`→`pn`、`groups['decode']`→`dn`；`proxy_host`/`proxy_port` 不变。）

- [ ] **步骤 2：运行测试验证通过（绿）**

运行：`cd ansible && .venv/bin/python -m pytest tests/test_derive.py -q`
预期：**15 passed**（11 旧 + 4 新）。

- [ ] **步骤 3：全量单测**

运行：`cd ansible && .venv/bin/python -m pytest tests/ -q`
预期：**25 passed**（21 旧 + 4 新）。

- [ ] **步骤 4：Commit**

```bash
git add ansible/tests/test_derive.py ansible/playbooks/start.yml
git commit -m "feat(ansible): proxy 接入节点白名单——端点生成按 proxy.prefill_nodes/decode_nodes 过滤（P/D 对称，空=全量，非法节点名 fail-fast）"
```

---

### 任务 3：group_vars 配置键（A2/A3）+ README 文档

**文件：**
- 修改：`ansible/inventories/a2/group_vars/all.yml`（`proxy: { port: 1999 }` 一行）
- 修改：`ansible/inventories/a3/group_vars/all.yml:34`（`proxy: { port: 8000 }` 一行）
- 修改：`ansible/README.md`（「关键字段」段）

- [ ] **步骤 1：A2 group_vars**

```yaml
# 旧
proxy: { port: 1999 }
# 新
proxy:
  port: 1999
  # 接入 proxy 的节点白名单（inventory 节点名）；空/未配置 = 全部接入
  prefill_nodes: []          # 例: [p0, p1]
  decode_nodes: []           # 例: [d0] —— 模拟 decode 排队
```

- [ ] **步骤 2：A3 group_vars**

```yaml
# 旧
proxy: { port: 8000 }
# 新
proxy:
  port: 8000
  # 接入 proxy 的节点白名单（inventory 节点名）；空/未配置 = 全部接入
  prefill_nodes: []          # 例: [p0]
  decode_nodes: []           # 例: [d0] —— 模拟 decode 排队
```

- [ ] **步骤 3：README「关键字段」段补一条**

在 `- \`proxy.port\`、\`mooncake.port\`：统一入口与 KV 池端口` 之后插入：

```markdown
- `proxy.prefill_nodes` / `proxy.decode_nodes`：接入 proxy 的节点白名单（inventory 节点名，空/未配置 = 全量接入）；如 `decode_nodes: [d0]` 只让 d0 的实例接流量（模拟 decode 排队），改后重跑 `--tags proxy` 生效
```

- [ ] **步骤 4：验证**

```bash
cd ansible
.venv/bin/python -c "import yaml; [yaml.safe_load(open(f)) for f in ['inventories/a2/group_vars/all.yml','inventories/a3/group_vars/all.yml']]; print('yaml ok')"
.venv/bin/python -m pytest tests/ -q                      # 25 passed
.venv/bin/ansible-playbook -i inventories/a2/inventory.yaml playbooks/start.yml --syntax-check   # 无 ERROR
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml --syntax-check   # 无 ERROR
```

- [ ] **步骤 5：Commit**

```bash
git add ansible/inventories/a2/group_vars/all.yml ansible/inventories/a3/group_vars/all.yml ansible/README.md
git commit -m "feat(ansible): A2/A3 group_vars 加 proxy 白名单配置键（默认空=全量接入）+ README 说明"
```

---

### 任务 4：收尾——全量验证证据 + 恢复用户 stash 现场

- [ ] **步骤 1：最终全量验证**

```bash
cd ansible && .venv/bin/python -m pytest tests/ -q         # 25 passed
```

- [ ] **步骤 2：恢复用户改动现场（stash 是实现前按用户要求存的）**

```bash
git stash pop    # 预期与白名单改动无冲突（不同 hunk/文件）；冲突则停止并报告，保留 stash
```

恢复后重跑 `cd ansible && .venv/bin/python -m pytest tests/ -q` 确认 stash 改动（start.yml 新增 patch 任务、curl.yml 等）与白名单共存仍 25 passed。

- [ ] **步骤 3：汇报**

汇总：测试证据、commit 列表、实机验证指引（A2 设 `decode_nodes: [d0]` → `--tags proxy` 重跑 → `/healthcheck` 应显示 `decode_instances: 2`；清空后重跑恢复 8）。

---

## 自检结论（写计划后已执行）

1. **规格覆盖度**：§3 配置→任务 3；§4 start.yml（assert+vars+表达式）→任务 2；§5 边界→零改动（无任务，符合规格）；§6 生效/回退→README（任务 3）+ 实机指引（任务 4）；§7 测试 4 用例→任务 1，实机路径→任务 4；§8 文档→任务 3。无遗漏。
2. **占位符扫描**：所有代码步骤含完整代码与精确命令/预期输出，无 TODO/待定。
3. **类型一致性**：`pn`/`dn`、`_playbook_task`/`_render_start_yml_endpoints`、任务名前缀「生成 proxy 端点」「校验 proxy 白名单」在各任务间一致；测试断言的节点 IP/端口与 `test_derive.py` 现有 GROUPS/HOSTVARS/A2_GV 一致。
