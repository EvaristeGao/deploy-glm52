"""节点参数派生 Jinja2 单测（TDD，Ansible 原生化任务 N3）。

set_fact 派生表达式（设计规格 §4.1）：
在 play 里对每个节点 set_fact 以下变量，用 Jinja2 从 inventory_hostname /
groups / hostvars / pd_cluster 派生，不依赖外部解析脚本。

本测试只验证「派生表达式」的渲染结果：
- 用样例 A2 group_vars（内联小字典，与 a2/group_vars/all.yml 一致）
- 手动构造 groups / hostvars 模拟 Ansible 全局变量
- 用 jinja2 Environment 直接渲染表达式字符串

仅纯本地渲染，不跑 playbook、不 ssh 节点、不占卡、不起服务。

任务 N4 在本文件补充「实例清单 + proxy 端点」生成表达式（设计规格 §4.2/§4.3）：
- 实例清单：prefill/decode 每节点 dp_size_local 个实例 [role, ip, base_port+r]，
  proxy 单列。A2 期望 4×1 + 4×2 + 1 = 13 个探测目标。
- proxy 端点：prefiller_hosts/ports、decoder_hosts/ports、proxy_host/port，
  供 load_balance_proxy 启动时注入。
"""
import os
from itertools import product as _itertools_product

import yaml
from jinja2 import Environment, StrictUndefined
from jinja2.nativetypes import NativeEnvironment

START_YML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "playbooks", "start.yml",
)


# ---- 样例 A2 group_vars（内联，与 ansible/inventories/a2/group_vars/all.yml 一致）----
A2_GV = {
    "cluster_type": "a2",
    "model_path": "/mnt/share_space/models/GLM-5.2-w4a8c8",
    "model_name": "glm-52",
    "pd_cluster": {
        "max_model_len": 200000,
        "num_cards": 8,
        "prefill": {
            "dp_size": 4, "tp_size": 8, "dp_size_local": 1,
            "base_port": 9081, "kv_port": 30000, "rpc_port": 16591,
        },
        "decode": {
            "dp_size": 8, "tp_size": 4, "dp_size_local": 2,
            "base_port": 9900, "kv_port": 30100, "rpc_port": 16600,
        },
        "enable_prefix_caching": True,
    },
    # 来源：a2/group_vars/all.yml 的 proxy: { port: 1999 }
    "proxy": {"port": 1999},
}

# ---- 派生表达式（设计规格 §4.1，每节点 set_fact）----
DERIVED = {
    "node_role": "{{ 'prefill' if inventory_hostname[0] == 'p' else 'decode' }}",
    "node_idx": "{{ inventory_hostname[1:] | int }}",
    "dp_size": "{{ pd_cluster[node_role].dp_size }}",
    "dp_size_local": "{{ pd_cluster[node_role].dp_size_local }}",
    "tp_size": "{{ pd_cluster[node_role].tp_size }}",
    "dp_rank": "{{ node_idx * pd_cluster[node_role].dp_size_local }}",
    "dp_address": "{{ hostvars[groups[node_role][0]].ip }}",
    "rpc_port": "{{ pd_cluster[node_role].rpc_port }}",
    "start_port": "{{ pd_cluster[node_role].base_port }}",
    "kv_port": "{{ pd_cluster[node_role].kv_port }}",
    "max_model_len": "{{ pd_cluster.max_model_len }}",
    "cluster_type": "{{ cluster_type }}",
    "model_path": "{{ model_path }}",
}


# ---- 实例清单 + proxy 端点表达式（设计规格 §4.2/§4.3）----
# 纯 jinja2 3.1.x 没有 Python 式列表推导 / product / extract 过滤，Ansible 运行时由
# ansible.builtin 提供 product。这里用「嵌套 for + product + list.append」的等价写法，
# 即可在 playbook 中直接复用（set_fact 循环），也能在本地 NativeEnvironment 渲染。
#
# 实例清单：prefill/decode 每节点 dp_size_local 个实例 [role, ip, base_port+r]，proxy 单列。
INSTANCE_LIST_TPL = """{#
  prefill/decode：groups[role] × range(dp_size_local)，端口 base_port + r
  proxy：单列，取 groups['proxy'][0]（A2 为 p0）的 IP + proxy.port
#}{% set il = [] -%}
{% for role in ['prefill', 'decode'] -%}
{% for h, r in groups[role] | product(range(pd_cluster[role].dp_size_local)) -%}
{% set _ = il.append([role, hostvars[h].ip, pd_cluster[role].base_port + r]) -%}
{% endfor -%}
{% endfor -%}
{% set _ = il.append(['proxy', hostvars[groups['proxy'][0]].ip, proxy.port]) -%}
{{ il }}"""

# proxy 端点：load_balance_proxy 启动用（hosts = 各节点 IP 重复 dp_size_local 次，
# ports = base_port+r 递增；A2 prefill 4×"9081"、decode 8×("9900","9901" 交替)）。
PROXY_ENDPOINTS = {
    "prefiller_hosts": (
        "{% set out = [] -%}"
        "{% for h, r in groups['prefill'] | product(range(pd_cluster.prefill.dp_size_local)) -%}"
        "{% set _ = out.append(hostvars[h].ip) -%}"
        "{% endfor -%}"
        "{{ out }}"
    ),
    "prefiller_ports": (
        "{% set out = [] -%}"
        "{% for h, r in groups['prefill'] | product(range(pd_cluster.prefill.dp_size_local)) -%}"
        "{% set _ = out.append((pd_cluster.prefill.base_port + r) | string) -%}"
        "{% endfor -%}"
        "{{ out }}"
    ),
    "decoder_hosts": (
        "{% set out = [] -%}"
        "{% for h, r in groups['decode'] | product(range(pd_cluster.decode.dp_size_local)) -%}"
        "{% set _ = out.append(hostvars[h].ip) -%}"
        "{% endfor -%}"
        "{{ out }}"
    ),
    "decoder_ports": (
        "{% set out = [] -%}"
        "{% for h, r in groups['decode'] | product(range(pd_cluster.decode.dp_size_local)) -%}"
        "{% set _ = out.append((pd_cluster.decode.base_port + r) | string) -%}"
        "{% endfor -%}"
        "{{ out }}"
    ),
    # proxy 端点：proxy 节点（groups['proxy'][0]，A2 为 p0）IP + proxy.port
    "proxy_host": "{{ hostvars[groups['proxy'][0]].ip }}",
    "proxy_port": "{{ proxy.port }}",
}


# ---- 模拟 Ansible 全局变量：groups / hostvars（与 a2/inventory.yaml 一致）----
GROUPS = {
    "prefill": ["p0", "p1", "p2", "p3"],
    "decode": ["d0", "d1", "d2", "d3"],
    # 来源：a2/inventory.yaml 的 proxy: { hosts: { p0: {} } }（proxy/mooncake 落在 p0）
    "proxy": ["p0"],
}
HOSTVARS = {
    "p0": {"ip": "192.168.0.245"},
    "p1": {"ip": "192.168.0.15"},
    "p2": {"ip": "192.168.0.160"},
    "p3": {"ip": "192.168.0.91"},
    "d0": {"ip": "192.168.0.127"},
    "d1": {"ip": "192.168.0.161"},
    "d2": {"ip": "192.168.0.154"},
    "d3": {"ip": "192.168.0.140"},
}


def _derive(inventory_hostname):
    """按 Ansible set_fact 顺序渲染全部派生表达式，返回 {name: value}。

    派生有依赖顺序（node_idx 依赖 node_role，dp_rank/dp_address 依赖 node_idx 等），
    故按原表达式顺序逐项渲染并回填，与 set_fact 依次执行语义一致。

    用 NativeEnvironment：Ansible set_fact 会保留原生类型（node_idx/dp_rank 为
    int、端口为 int、地址为 str），而普通 Environment 会把一切渲染成字符串
    （'0' * 1 会变成重复字符串而非 0），不能还原真实语义。
    """
    env = NativeEnvironment(undefined=StrictUndefined)
    ctx = dict(A2_GV)
    ctx["inventory_hostname"] = inventory_hostname
    ctx["groups"] = GROUPS
    ctx["hostvars"] = HOSTVARS
    out = {}
    for name, tpl in DERIVED.items():
        out[name] = env.from_string(tpl).render(**{**ctx, **out})
        ctx[name] = out[name]
    return out


def _new_env(native=True):
    """返回与 Ansible 运行时一致的 Jinja2 环境。

    Ansible 的 jinja2 环境额外提供 product 过滤（ansible.builtin），纯 jinja2 需
    手动注册，才能在本地等价模拟 playbook 里的 product 表达式。
    native=False 用纯 Environment：NativeEnvironment 会把单节点输出字符串做
    ast.literal_eval（如 "9081"→int），端点值实际是 shell 空格分隔字符串，纯
    Environment 渲染更贴近实际。
    """
    cls = NativeEnvironment if native else Environment
    env = cls(undefined=StrictUndefined)
    env.filters["product"] = lambda *iterables: list(_itertools_product(*iterables))
    # Ansible 内置集合过滤（proxy 白名单 assert 校验用）：difference = 差集
    env.filters["difference"] = lambda a, b: [x for x in a if x not in b]
    return env


def _render_instance_list():
    env = _new_env()
    ctx = dict(A2_GV, groups=GROUPS, hostvars=HOSTVARS)
    return env.from_string(INSTANCE_LIST_TPL).render(**ctx)


def _render_proxy_endpoints():
    env = _new_env()
    ctx = dict(A2_GV, groups=GROUPS, hostvars=HOSTVARS)
    return {k: env.from_string(v).render(**ctx) for k, v in PROXY_ENDPOINTS.items()}


# ---- 任务 N4：实例清单 + proxy 端点 ----

def test_instance_list_count_a2():
    """实例清单 A2 = 13：prefill 4×1 + decode 4×2 + proxy 1。"""
    il = _render_instance_list()
    assert len(il) == 13
    # 顺序：prefill p0..p3 → decode d0..d3 → proxy
    assert il[0] == ["prefill", "192.168.0.245", 9081]
    assert il[3] == ["prefill", "192.168.0.91", 9081]
    assert il[4] == ["decode", "192.168.0.127", 9900]
    assert il[5] == ["decode", "192.168.0.127", 9901]
    assert il[11] == ["decode", "192.168.0.140", 9901]
    assert il[12] == ["proxy", "192.168.0.245", 1999]


def test_instance_list_roles_and_ports():
    """实例清单每行 [role, ip, port]；每节点 dp_size_local 个、端口 base_port+r 递增。"""
    il = _render_instance_list()
    prefill_rows = [r for r in il if r[0] == "prefill"]
    decode_rows = [r for r in il if r[0] == "decode"]
    assert prefill_rows == [
        ["prefill", ip, 9081] for ip in
        ["192.168.0.245", "192.168.0.15", "192.168.0.160", "192.168.0.91"]
    ]
    assert decode_rows == [
        ["decode", ip, prt] for ip, prt in
        [("192.168.0.127", 9900), ("192.168.0.127", 9901),
         ("192.168.0.161", 9900), ("192.168.0.161", 9901),
         ("192.168.0.154", 9900), ("192.168.0.154", 9901),
         ("192.168.0.140", 9900), ("192.168.0.140", 9901)]
    ]


def test_proxy_endpoints_prefill():
    """prefiller_hosts = 4 个 prefill IP，prefiller_ports = 4×"9081"。"""
    pe = _render_proxy_endpoints()
    assert pe["prefiller_hosts"] == [
        "192.168.0.245", "192.168.0.15", "192.168.0.160", "192.168.0.91"
    ]
    assert pe["prefiller_ports"] == ["9081", "9081", "9081", "9081"]


def test_proxy_endpoints_decode():
    """decoder_hosts = 8 个（每 decode 节点重复 2 次），decoder_ports = 8 个（9900 9901 交替）。"""
    pe = _render_proxy_endpoints()
    assert pe["decoder_hosts"] == [
        "192.168.0.127", "192.168.0.127",
        "192.168.0.161", "192.168.0.161",
        "192.168.0.154", "192.168.0.154",
        "192.168.0.140", "192.168.0.140",
    ]
    assert pe["decoder_ports"] == ["9900", "9901", "9900", "9901",
                                   "9900", "9901", "9900", "9901"]


def test_proxy_endpoint_host_port():
    """proxy_host = p0 IP(192.168.0.245)，proxy_port = 1999。"""
    pe = _render_proxy_endpoints()
    assert pe["proxy_host"] == "192.168.0.245"
    assert pe["proxy_port"] == 1999


def test_p0_prefill_full():
    """p0：prefill 首节点，dp_rank=0，首端口 9081，地址 192.168.0.245。"""
    v = _derive("p0")
    assert v["node_role"] == "prefill"
    assert v["node_idx"] == 0
    assert v["dp_size"] == 4
    assert v["dp_size_local"] == 1
    assert v["tp_size"] == 8
    assert v["dp_rank"] == 0
    assert v["dp_address"] == "192.168.0.245"
    assert v["start_port"] == 9081
    assert v["rpc_port"] == 16591
    assert v["kv_port"] == 30000


def test_p3_prefill_rank():
    """p3：prefill 第 4 节点，dp_rank = 3 * dp_size_local(1) = 3。"""
    v = _derive("p3")
    assert v["node_role"] == "prefill"
    assert v["node_idx"] == 3
    assert v["dp_rank"] == 3
    assert v["dp_address"] == "192.168.0.245"  # 仍是 prefill 首节点 p0


def test_d1_decode_full():
    """d1：decode 第 2 节点，dp_rank = 1 * 2 = 2，首端口 9900，地址 192.168.0.127。"""
    v = _derive("d1")
    assert v["node_role"] == "decode"
    assert v["node_idx"] == 1
    assert v["dp_size"] == 8
    assert v["dp_size_local"] == 2
    assert v["tp_size"] == 4
    assert v["dp_rank"] == 2
    assert v["dp_address"] == "192.168.0.127"  # decode 首节点 d0
    assert v["start_port"] == 9900
    assert v["rpc_port"] == 16600
    assert v["kv_port"] == 30100


def test_d3_decode_rank():
    """d3：decode 第 4 节点，dp_rank = 3 * dp_size_local(2) = 6。"""
    v = _derive("d3")
    assert v["node_role"] == "decode"
    assert v["node_idx"] == 3
    assert v["dp_rank"] == 6


def test_derived_rank_sequence_matches_a2_topology():
    """端端口/拓扑与 A2 一致：prefill rank 0..3，decode rank 0..7（每节点 2 引擎）。"""
    prefill_ranks = [_derive(f"p{i}")["dp_rank"] for i in range(4)]
    assert prefill_ranks == [0, 1, 2, 3]
    decode_ranks = [_derive(f"d{i}")["dp_rank"] for i in range(4)]
    assert decode_ranks == [0, 2, 4, 6]


def test_cluster_level_vars():
    """集群级变量（max_model_len / cluster_type / model_path）各节点一致。"""
    for node in ("p0", "d1"):
        v = _derive(node)
        assert v["max_model_len"] == 200000
        assert v["cluster_type"] == "a2"
        assert v["model_path"] == "/mnt/share_space/models/GLM-5.2-w4a8c8"


# ---- proxy 接入节点白名单（规格 docs/superpowers/specs/2026-09-08-proxy-nodes-whitelist-design.md）----
# 直接渲染 playbooks/start.yml 里「生成 proxy 端点」「校验 proxy 白名单」任务的真实表达式，
# 不在测试内复制镜像表达式（防两处漂移）。模拟 Ansible 语义：任务 vars:（pn/dn）先渲染注入
# ctx，再渲染 set_fact / assert 各表达式（同任务键间不能引用，vars: 渲染期已入作用域）。


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

    双环境模拟真实 Ansible 语义：
    - vars:（pn/dn 白名单）用 Native 渲染——Ansible 对纯 {{ }} 表达式保留原生类型，
      列表必须原样进入循环（纯 Environment 会把列表字符串化成 "['p0']"，逐字符迭代）；
    - set_fact 端点值用纯 Environment 渲染——模板含块标签，Ansible 结果是 shell 空格
      分隔字符串（Native 会把单元素 join 结果 "9081" literal_eval 回 int）。

    proxy 传 dict 覆盖 A2_GV['proxy'] 注入白名单配置（须含 port：StrictUndefined 下
    proxy.port 缺失会渲染失败）。
    """
    t = _playbook_task("生成 proxy 端点")
    native_env = _new_env(native=True)
    str_env = _new_env(native=False)
    ctx = dict(A2_GV, groups=GROUPS, hostvars=HOSTVARS)
    if proxy is not None:
        ctx["proxy"] = proxy
    for name, tpl in (t.get("vars") or {}).items():
        ctx[name] = native_env.from_string(tpl).render(**ctx)
    return {k: str_env.from_string(v).render(**ctx) for k, v in (t.get("set_fact") or {}).items()}


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
    results = [
        # that: 是裸 Jinja2 表达式（Ansible 内部按表达式求值），本地模拟需包 {{ }}
        env.from_string("{{ " + tpl.strip() + " }}").render(**ctx)
        for tpl in t["assert"]["that"]
    ]
    assert results == [True, False]  # prefill 未配置→合法；decode 含 dX→非法


# ==================== A3 配置选择器派生回归（deploy_config: 200k | 1m） ====================
# 用 A3 真实 inventory + 真实 group_vars + start.yml 真实表达式渲染（不镜像表达式，防两处漂移）。
# 1M 档对齐官方 A3 PD 分离 1M 参考：P 2 节点 dp2/tp16/每节点 1 引擎（rank 0/1），
# D 2 节点 dp8/tp4/每节点 4 引擎（rank 0/4），proxy 端点随之变成 2 + 8。

A3_INV_FILE = os.path.join(os.path.dirname(START_YML), "..", "inventories", "a3", "inventory.yaml")
A3_GV_FILE = os.path.join(os.path.dirname(START_YML), "..", "inventories", "a3", "group_vars", "all.yml")


def _resolve_gv(gv, rounds=3):
    """展开 group_vars 的 Jinja 间接层（deploy_config → _active → pd_cluster），保留原生类型。"""
    ev = NativeEnvironment(undefined=StrictUndefined)
    for _ in range(rounds):
        pending = False
        for k, v in gv.items():
            if isinstance(v, str) and "{{" in v:
                r = ev.from_string(v).render(**gv)
                if r != v:
                    gv[k] = r
                else:
                    pending = True
            elif isinstance(v, dict):
                for kk, vv in list(v.items()):
                    if isinstance(vv, str) and "{{" in vv:
                        r = ev.from_string(vv).render(**gv)
                        if r != vv:
                            v[kk] = r
                        else:
                            pending = True
        if not pending:
            break
    return gv


def _load_inventory(path):
    """按 Ansible 语义解析 inventory：groups（含 children 组）+ hostvars。"""
    inv = yaml.safe_load(open(path, encoding="utf-8"))
    groups, hostvars = {"all": []}, {}
    for child, cdef in inv["all"]["children"].items():
        groups[child] = list(cdef.get("hosts", {}).keys())
        groups["all"].extend(groups[child])
        for name, v in cdef.get("hosts", {}).items():
            if v:
                hostvars.setdefault(name, {}).update(v)
    groups["all"] = sorted(set(groups["all"]))
    return groups, hostvars


def _a3_ctx(deploy_config=None):
    gv = yaml.safe_load(open(A3_GV_FILE, encoding="utf-8"))
    if deploy_config is not None:
        gv["deploy_config"] = deploy_config
    groups, hostvars = _load_inventory(A3_INV_FILE)
    return _resolve_gv(gv), groups, hostvars


def _derive_node(host, deploy_config=None):
    """按 start.yml 真实 set_fact 顺序渲染 A3 节点的角色/索引 + 启动参数。

    同一 set_fact 任务内各键互不引用（Ansible 语义），故按任务分批渲染后再合并。"""
    gv, groups, hostvars = _a3_ctx(deploy_config)
    ctx = {"inventory_hostname": host}
    for task_name in ("派生本节点角色/索引", "派生本节点启动参数"):
        t = _playbook_task(task_name)
        env = _new_env(native=True)
        base = dict(gv, groups=groups, hostvars=hostvars, **ctx)
        ctx.update({
            k: env.from_string(str(v)).render(**base)
            for k, v in (t.get("set_fact") or {}).items()
        })
    return gv, groups, hostvars, ctx


def _render_a3_task_fact(task_name, deploy_config=None, native=True):
    """渲染 A3 下 start.yml 某任务的 vars: + set_fact: 表达式。

    native=True 保留原生类型（实例清单是 list）；native=False 得到 shell 空格分隔字符串
    （proxy 端点值）。"""
    gv, groups, hostvars = _a3_ctx(deploy_config)
    t = _playbook_task(task_name)
    native_env = _new_env(native=True)
    ctx = dict(gv, groups=groups, hostvars=hostvars)
    for name, tpl in (t.get("vars") or {}).items():
        ctx[name] = native_env.from_string(str(tpl)).render(**ctx)
    env = _new_env(native=native)
    return {k: env.from_string(str(v)).render(**ctx) for k, v in (t.get("set_fact") or {}).items()}


def test_a3_1m_prefill_engine_args():
    """A3 1M：p0/p1 各 1 引擎（dp2 tp16），rank 0/1，端口 9081，rpc 16600，dp_address = p0 IP。"""
    _, _, hostvars, p0 = _derive_node("p0", "1m")
    assert p0["node_role"] == "prefill"
    assert p0["node_idx"] == 0
    assert (p0["dp_size"], p0["tp_size"], p0["dp_size_local"]) == (2, 16, 1)
    assert p0["dp_rank"] == 0
    assert p0["start_port"] == 9081
    assert p0["rpc_port"] == 16600
    assert p0["kv_port"] == 30000
    assert p0["dp_address"] == hostvars["p0"]["ip"]

    _, _, _, p1 = _derive_node("p1", "1m")
    assert p1["dp_rank"] == 1
    assert p1["dp_address"] == hostvars["p0"]["ip"]   # dp master 恒为 p0


def test_a3_1m_decode_engine_args():
    """A3 1M：d0/d1 各 4 引擎（dp8 tp4），rank 0/4，端口 9900，kv_port 30200。"""
    _, _, hostvars, d0 = _derive_node("d0", "1m")
    assert d0["node_role"] == "decode"
    assert (d0["dp_size"], d0["tp_size"], d0["dp_size_local"]) == (8, 4, 4)
    assert d0["dp_rank"] == 0
    assert d0["start_port"] == 9900
    assert d0["rpc_port"] == 16600
    assert d0["kv_port"] == 30200
    assert d0["dp_address"] == hostvars["d0"]["ip"]

    _, _, _, d1 = _derive_node("d1", "1m")
    assert d1["dp_rank"] == 4
    assert d1["dp_address"] == hostvars["d0"]["ip"]


def test_a3_200k_engine_args():
    """A3 200K：官方 198K PD 的 dp/tp —— prefill 用 DP2 TP16（每节点 1 引擎，rank 0/1）
    代替官方的 PP2 TP16；decode 与官方一致 DP8 TP4（每节点 4 引擎，rank 0/4）。"""
    _, _, hostvars, p1 = _derive_node("p1", "200k")
    assert (p1["dp_size"], p1["tp_size"], p1["dp_size_local"]) == (2, 16, 1)
    assert p1["dp_rank"] == 1
    assert p1["start_port"] == 9081
    assert p1["rpc_port"] == 16591
    assert p1["kv_port"] == 30000
    assert p1["dp_address"] == hostvars["p0"]["ip"]

    _, _, hostvars, d1 = _derive_node("d1", "200k")
    assert (d1["dp_size"], d1["tp_size"], d1["dp_size_local"]) == (8, 4, 4)
    assert d1["dp_rank"] == 4
    assert d1["start_port"] == 9900
    assert d1["rpc_port"] == 16600
    assert d1["kv_port"] == 30200
    assert d1["dp_address"] == hostvars["d0"]["ip"]


def test_a3_1m_instance_list_and_proxy_endpoints():
    """A3 1M：探测实例 2×1 + 2×4 + 1 proxy = 11；proxy 端点 prefill 2 个、decode 8 个。"""
    il = _render_a3_task_fact("生成实例清单", "1m")["instance_list"]
    assert len(il) == 11
    assert [r[0] for r in il].count("prefill") == 2
    assert [r[0] for r in il].count("decode") == 8
    assert il[-1][0] == "proxy"

    pe = _render_a3_task_fact("生成 proxy 端点", "1m", native=False)
    assert len(pe["prefiller_hosts"].split()) == 2
    assert pe["prefiller_ports"] == "9081 9081"
    assert len(pe["decoder_hosts"].split()) == 8
    assert pe["decoder_ports"] == "9900 9901 9902 9903 9900 9901 9902 9903"
    # decode 端点每节点 4 个，端口按节点本地序号递增（与官方参考一致）
    dec_hosts = pe["decoder_hosts"].split()
    assert dec_hosts[:4] == [dec_hosts[0]] * 4
    assert dec_hosts[4:] == [dec_hosts[4]] * 4
    assert dec_hosts[0] != dec_hosts[4]


def test_a3_200k_instance_list_and_proxy_endpoints():
    """A3 200K：探测实例 2×1 + 2×4 + 1 proxy = 11；proxy prefill 2 个、decode 8 个。"""
    il = _render_a3_task_fact("生成实例清单", "200k")["instance_list"]
    assert len(il) == 11
    assert [r[0] for r in il].count("prefill") == 2
    assert [r[0] for r in il].count("decode") == 8
    pe = _render_a3_task_fact("生成 proxy 端点", "200k", native=False)
    assert pe["prefiller_ports"] == "9081 9081"
    assert pe["decoder_ports"] == "9900 9901 9902 9903 9900 9901 9902 9903"
    assert len(pe["decoder_hosts"].split()) == 8


# ==================== kvpool 单主（enable_ha=false）：不起 etcd/haproxy，免证书依赖 ====================
# 测试阶段用裸 mooncake_master（master 固定落 proxy 节点 p0），无需 etcd 选主与证书目录。


def _a3_ctx_with_ha(enable_ha):
    """A3 上下文 + 覆盖 mooncake.ha.enable_ha，并算好 mooncake_master_nodes（与 start.yml play vars 同式）。"""
    gv, groups, hostvars = _a3_ctx()
    gv = dict(gv)
    gv["mooncake"] = dict(gv["mooncake"], ha=dict(gv["mooncake"]["ha"], enable_ha=enable_ha))
    master_nodes = gv["mooncake"]["ha"]["nodes"] if enable_ha else [groups["proxy"][0]]
    ctx = dict(gv, groups=groups, hostvars=hostvars,
               inventory_hostname=groups["proxy"][0], mooncake_master_nodes=master_nodes)
    return ctx


def test_mooncake_container_certs_bind_only_in_ha_mode():
    """etcd 证书挂载只在 HA 模式出现（start.yml「起 mooncake 容器」）。

    单主模式没有 etcd，若仍 bind etcd.certs_dir，docker 会在宿主机创建一个空的
    证书目录；而切回 HA 前又必须确认该目录真实存在。故按 enable_ha 门控。"""
    env = _new_env(native=False)
    shell = str(_playbook_task("起 mooncake 容器")["shell"])

    out_ha = env.from_string(shell).render(**_a3_ctx_with_ha(True))
    assert "certs:/etc/pki/etcd/certs" in out_ha

    out_single = env.from_string(shell).render(**_a3_ctx_with_ha(False))
    assert "certs:/etc/pki/etcd/certs" not in out_single
    # 两种模式都要挂日志目录、用 mooncake 镜像、只在 proxy 节点起
    for out in (out_ha, out_single):
        assert "/mnt/sfs_turbo/g00832294/logs" in out
        assert "vllm-ascend-ha-gwh-test:v2.0" in out
        assert "docker run -itd --name mooncake-p0" in out


def test_gen_mooncake_json_single_master_points_to_p0():
    """gen.yml 单主分支：master_server_address 直连 p0 的 mooncake rpc 端口（无 etcd://）。"""
    with open(os.path.join(os.path.dirname(START_YML), "gen.yml"), encoding="utf-8") as f:
        play = yaml.safe_load(f)[0]
    task = next(t for t in play["tasks"]
                if (t.get("name") or "").startswith("渲染 mooncake.json（kvpool 单主"))
    ctx = _a3_ctx_with_ha(False)
    out = _new_env(native=False).from_string(str(task["copy"]["content"])).render(**ctx)
    p0_ip = ctx["hostvars"]["p0"]["ip"]
    assert f'"master_server_address": "{p0_ip}:{ctx["mooncake"]["ha"]["rpc_port"]}"' in out
    assert "etcd://" not in out
    assert '"metadata_server": "P2PHANDSHAKE"' in out