"""节点参数派生 Jinja2 单测（TDD，Ansible 原生化任务 N3）。

替代旧 resolve_node filter 的 set_fact 派生表达式（设计规格 §4.1）：
在 play 里对每个节点 set_fact 以下变量，用 Jinja2 从 inventory_hostname /
groups / hostvars / pd_cluster 派生，不再依赖 resolve/*.py 脚本。

本测试只验证「派生表达式」的渲染结果：
- 用样例 A2 group_vars（内联小字典，与 a2/group_vars/all.yml 一致）
- 手动构造 groups / hostvars 模拟 Ansible 全局变量
- 用 jinja2 Environment 直接渲染表达式字符串

仅纯本地渲染，不跑 playbook、不 ssh 节点、不占卡、不起服务。
"""
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


# ---- 模拟 Ansible 全局变量：groups / hostvars（与 a2/inventory.yaml 一致）----
GROUPS = {
    "prefill": ["p0", "p1", "p2", "p3"],
    "decode": ["d0", "d1", "d2", "d3"],
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