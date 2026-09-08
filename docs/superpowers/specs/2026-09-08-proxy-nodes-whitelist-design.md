# proxy 接入节点白名单 — 设计规格

- 日期：2026-09-08
- 状态：已批准（用户批准设计后成文）
- 范围：`ansible/`（start.yml、group_vars、单测、README）；**不改 proxy 脚本本身**

## 1. 背景与目标

proxy（`load_balance_proxy_server_example.py`）目前把**全部** prefill/decode 节点的全部引擎实例
接入负载均衡（start.yml 端点生成时遍历 `groups['prefill']`/`groups['decode']` × `dp_size_local`）。

需要支持**只把部分节点接入 proxy**，例如 decode 侧只让 d0 接入，用少量 decode 实例承接全部流量，
模拟 decode 排队/拥堵场景（配合 verify_req 打补丁线做验证）。

## 2. 已定决策

| 决策点 | 结论 | 未选方案及原因 |
|---|---|---|
| 控制方式 | group_vars 静态配置 | proxy 已有运行时 `/instances/add\|remove` API，可动态调但配置不留痕、重跑 start.yml 回全量；静态配置确定性强、可追溯 |
| 适用范围 | P/D 两侧对称 | 只做 decode 不符“配置接入节点”的通用诉求，对称成本≈0 |
| 配置形态 | 白名单列表 | 黑名单与“接入”语义相反；前 N 个截断依赖 inventory 顺序，语义隐晦 |

## 3. 配置设计

各集群 `inventories/<a2|a3>/group_vars/all.yml` 的 `proxy` 段新增两个键（A2/A3 都加，保持结构一致）：

```yaml
proxy:
  port: 1999
  # 接入 proxy 的节点白名单（inventory 节点名）；空/未配置 = 全部接入
  prefill_nodes: []          # 例: [p0, p1]
  decode_nodes: []           # 例: [d0] —— 模拟 decode 排队
```

语义：

- **粒度是节点**（inventory_hostname），节点上全部 `dp_size_local` 个引擎实例随节点一起接入/排除。
- **空列表或键未配置 = 全量接入**（默认行为完全不变）。
- 白名单是**子集选择器**，不改路由算法（proxy 内部仍按 active_tokens 优先级队列选实例）。

## 4. start.yml 改动（单点）

改动集中在 proxy 端点生成任务（HEAD 位置 `playbooks/start.yml:324`，`tags: proxy`）：

```yaml
- name: 校验 proxy 白名单节点名（非法名 fail-fast）
  run_once: true
  assert:
    that:
      - proxy.prefill_nodes | default([]) | difference(groups['prefill']) | length == 0
      - proxy.decode_nodes | default([]) | difference(groups['decode']) | length == 0
    fail_msg: >-
      proxy.prefill_nodes/decode_nodes 含 inventory 中不存在的节点名：
      {{ (proxy.prefill_nodes | default([])) | difference(groups['prefill'])
         + (proxy.decode_nodes | default([])) | difference(groups['decode']) }}
  tags: proxy

- name: 生成 proxy 端点（Jinja2 生成；run_once 于 p0 并回收 facts）
  run_once: true
  delegate_to: p0
  delegate_facts: yes
  vars:
    # 白名单解析：undefined 或空列表都回退全量（default(x, true) 对 falsy 也生效）
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

实现要点（已核实）：

- **白名单解析放 `vars:` 段而非同任务 set_fact 键**：Ansible 对同一任务的模板先整体渲染再执行，
  同一 `set_fact` 内后键无法引用前键；`vars:` 在渲染期已入作用域。
- `default(groups['decode'], true)` 的 `true` 使空列表也走默认值，单表达式同时覆盖
  “键未配置”与“显式空列表”两种情况。
- 循环体本身不变，仅把 `groups['prefill']`/`groups['decode']` 换成 `pn`/`dn`（`product` 对列表变量照常工作）。

## 5. 边界（明确不动的部分）

| 项 | 行为 | 理由 |
|---|---|---|
| 引擎启动（engine 阶段） | 全部节点照常启动 | 被排除的 decode 引擎保持存活，只是不接 proxy 流量 |
| wait_ready `instance_list` | 仍遍历全部实例 | 就绪探测与 proxy 接入是两条独立链路 |
| proxy 脚本 | 零改动 | 端点列表变短即可，`nargs='+'` 天然支持；路由/健康检查/动态 API 均不变 |
| mooncake / 引擎模板 / gen.yml | 零改动 | KV 池成员与 proxy 流量接入无关；被排除节点无请求故无 KV 传输 |
| status/logs/stop/check/pull | 零改动 | 与 proxy 端点无关 |

## 6. 生效与回退

- 生效：改 group_vars 白名单后，重跑 `ansible-playbook ... playbooks/start.yml --tags proxy`
  （proxy 会被 pkill 后按新端点重启）。
- 回退：把白名单清空（或删键）再重跑 `--tags proxy`，恢复全量接入。

## 7. 测试

单元测试（`ansible/tests/test_derive.py`，已有 proxy 端点派生用例，复现 start.yml 表达式逻辑）新增：

1. 白名单命中子集：`decode_nodes: [d0]` → decoder_hosts/ports 只含 d0 的 `dp_size_local` 个实例
2. 空列表 / 键缺省 → 与全量结果逐项相等
3. prefill 侧对称用例
4. 非法节点名 → 校验表达式判定为失败

实机验证路径（A2）：

1. `decode_nodes: [d0]` → `--tags proxy` 重跑 → `/healthcheck` 应显示
   `decode_instances: 2`（d0 的 `dp_size_local=2`），`prefill_instances: 4` 不变
2. 冒烟（curl /v1/chat/completions 经 proxy）成功，且 decode 流量只落 d0
3. 清空白名单 → `--tags proxy` → `decode_instances: 8` 恢复

## 8. 文档

- `ansible/README.md`「关键字段」段补 `proxy.prefill_nodes/decode_nodes` 两键说明与示例。
- `docs/architecture.md` 不涉及（proxy 端点派生细节属于 README/playbook 层）。
