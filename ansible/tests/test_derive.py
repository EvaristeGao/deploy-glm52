"""节点参数派生 Jinja2 单测（TDD，Ansible 原生化任务 N3）。

替代旧 resolve_node filter 的 set_fact 派生表达式（设计规格 §4.1）：
在 play 里对每个节点 set_fact 以下变量，用 Jinja2 从 inventory_hostname /
groups / hostvars / pd_cluster 派生，不再依赖 resolve/*.py 脚本。

本测试只验证「派生表达式」的渲染结果：
- 用样例 A2 group_vars（内联小字典，与 a2/group_vars/all.yml 一致）
- 手动构造 groups / hostvars 模拟 Ansible 全局变量
- 用 jinja2 Environment 直接渲染表达式字符串

仅纯本地渲染，不跑 playbook、不 ssh 节点、不占卡、不起服务。

任务 N4 在本文件补充「实例清单 + proxy 端点」生成表达式（设计规格 §4.2/§4.3，
替代旧 resolve_instances.py / resolve_router.py 脚本）：
- 实例清单：prefill/decode 每节点 dp_size_local 个实例 [role, ip, base_port+r]，
  proxy 单列。A2 期望 4×1 + 4×2 + 1 = 13 个探测目标。
- proxy 端点：prefiller_hosts/ports、decoder_hosts/ports、proxy_host/port，
  供 load_balance_proxy 启动时注入。
"""
from itertools import product as _itertools_product

from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment


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
    "dp_address": "{{ hostvars[groups[node_role][0]].ansible_host }}",
    "rpc_port": "{{ pd_cluster[node_role].rpc_port }}",
    "start_port": "{{ pd_cluster[node_role].base_port }}",
    "kv_port": "{{ pd_cluster[node_role].kv_port }}",
    "max_model_len": "{{ pd_cluster.max_model_len }}",
    "cluster_type": "{{ cluster_type }}",
    "model_path": "{{ model_path }}",
}


# ---- 实例清单 + proxy 端点表达式（设计规格 §4.2/§4.3，替代旧 resolve_instances.py / resolve_router.py）----
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
{% set _ = il.append([role, hostvars[h].ansible_host, pd_cluster[role].base_port + r]) -%}
{% endfor -%}
{% endfor -%}
{% set _ = il.append(['proxy', hostvars[groups['proxy'][0]].ansible_host, proxy.port]) -%}
{{ il }}"""

# proxy 端点：load_balance_proxy 启动用（hosts = 各节点 IP 重复 dp_size_local 次，
# ports = base_port+r 递增；A2 prefill 4×"9081"、decode 8×("9900","9901" 交替)）。
PROXY_ENDPOINTS = {
    "prefiller_hosts": (
        "{% set out = [] -%}"
        "{% for h, r in groups['prefill'] | product(range(pd_cluster.prefill.dp_size_local)) -%}"
        "{% set _ = out.append(hostvars[h].ansible_host) -%}"
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
        "{% set _ = out.append(hostvars[h].ansible_host) -%}"
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
    "proxy_host": "{{ hostvars[groups['proxy'][0]].ansible_host }}",
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
    "p0": {"ansible_host": "192.168.0.245"},
    "p1": {"ansible_host": "192.168.0.15"},
    "p2": {"ansible_host": "192.168.0.160"},
    "p3": {"ansible_host": "192.168.0.91"},
    "d0": {"ansible_host": "192.168.0.127"},
    "d1": {"ansible_host": "192.168.0.161"},
    "d2": {"ansible_host": "192.168.0.154"},
    "d3": {"ansible_host": "192.168.0.140"},
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


def _new_env():
    """返回与 Ansible 运行时一致的 NativeEnvironment。

    Ansible 的 jinja2 环境额外提供 product 过滤（ansible.builtin），纯 jinja2 需
    手动注册，才能在本地等价模拟 playbook 里的 product 表达式。
    """
    env = NativeEnvironment(undefined=StrictUndefined)
    env.filters["product"] = lambda *iterables: list(_itertools_product(*iterables))
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