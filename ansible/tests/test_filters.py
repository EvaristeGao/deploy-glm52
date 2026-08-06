"""resolve filter 插件单测（TDD）。

验证 filter 输出与直接跑 resolve/*.py 脚本完全一致。
仅解析配置、做断言，不拉起任何服务、不 ssh 节点。
"""
import os
import subprocess
import sys

import pytest
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ansible"))
from filter_plugins.resolve import FilterModule  # noqa: E402

CONFIG = os.path.join(PROJECT_ROOT, "config.yaml")


def _run_script(script, *extra):
    """直接跑 resolve 脚本，返回 stdout 行列表。"""
    return subprocess.check_output(
        [sys.executable, script, "--config", CONFIG, *extra],
        text=True,
    ).splitlines()


def test_resolve_node_p0():
    cfg = yaml.safe_load(open(CONFIG))
    out = FilterModule().resolve_node(cfg, "p0")

    assert out["ROLE"] == "prefill"
    assert out["NODE_INDEX"] == 0
    assert out["DP_SIZE"] == 4
    assert out["DP_SIZE_LOCAL"] == 1
    assert out["TP_SIZE"] == 8
    assert out["DP_RANK_START"] == 0
    assert out["DP_ADDRESS"] == "192.168.0.245"
    assert out["DP_RPC_PORT"] == 16591
    assert out["VLLM_START_PORT"] == 9081
    assert out["KV_PORT"] == 30000
    assert out["MODEL_PATH"] == "/mnt/share_space/models/GLM-5.2-w4a8c8"
    assert out["LOCAL_IP"] == "192.168.0.245"
    assert out["NIC"] == "enp67s0f5"
    assert out["CLUSTER_TYPE"] == "a2"


def test_resolve_node_matches_script():
    """filter 结果与直接跑 resolve_node.py 一致（逐 KEY 比对，数字类型归一）。"""
    cfg = yaml.safe_load(open(CONFIG))
    lines = _run_script("resolve/resolve_node.py", "--node", "d1")
    expected = {}
    for ln in lines:
        k, _, v = ln.partition("=")
        expected[k] = v
    out = FilterModule().resolve_node(cfg, "d1")
    assert out["ROLE"] == "decode"
    assert out["NODE_INDEX"] == 1
    assert out["DP_SIZE"] == 8
    assert out["DP_SIZE_LOCAL"] == 2
    assert out["DP_RANK_START"] == 2
    assert out["DP_ADDRESS"] == "192.168.0.127"
    assert str(out["DP_SIZE"]) == expected["DP_SIZE"]
    assert str(out["LOCAL_IP"]) == expected["LOCAL_IP"]
    assert str(out["MAX_MODEL_LEN"]) == expected["MAX_MODEL_LEN"]


def test_resolve_instances_p0_proxy():
    cfg = yaml.safe_load(open(CONFIG))
    inst = FilterModule().resolve_instances(cfg)
    # proxy 节点 p0 → 其 IP 是首个 prefill 节点
    assert ("proxy", "192.168.0.245", 1999) in inst
    # prefill 每节点 dp_size_local=1, base_port=9081
    assert ("prefill", "192.168.0.245", 9081) in inst
    # decode 每节点 dp_size_local=2, base_port=9900,9901
    assert ("decode", "192.168.0.127", 9900) in inst
    assert ("decode", "192.168.0.127", 9901) in inst
    assert len(inst) == 4 + 8 + 1


def test_resolve_instances_matches_script():
    """filter 结果行集合 == 直接跑 resolve_instances.py 的行。"""
    cfg = yaml.safe_load(open(CONFIG))
    lines = _run_script("resolve/resolve_instances.py")
    expected = set(lines)
    inst = FilterModule().resolve_instances(cfg)
    got = {f"{r} {ip} {port}" for r, ip, port in inst}
    assert got == expected


def test_resolve_router_matches_script():
    """filter 结果与直接跑 resolve_router.py 一致。"""
    cfg = yaml.safe_load(open(CONFIG))
    lines = _run_script("resolve/resolve_router.py")
    expected = {}
    for ln in lines:
        k, _, v = ln.partition("=")
        expected[k] = v.strip('"')
    out = FilterModule().resolve_router(cfg)
    assert out["PROXY_HOST"] == expected["PROXY_HOST"]
    assert str(out["PROXY_PORT"]) == expected["PROXY_PORT"]
    assert " ".join(out["PREFILLER_HOSTS"]) == expected["PREFILLER_HOSTS"]
    assert " ".join(out["PREFILLER_PORTS"]) == expected["PREFILLER_PORTS"]
    assert " ".join(out["DECODER_HOSTS"]) == expected["DECODER_HOSTS"]
    assert " ".join(out["DECODER_PORTS"]) == expected["DECODER_PORTS"]


def test_resolve_node_with_ansible_tagged_config():
    """真实 Ansible 传入的是带 tag 的包装类型（_AnsibleTaggedStr 等），
    pyyaml 无法直接 dump 它们，必须经 _to_plain 剥标签。回归测试。"""
    # ansible-core 仅随部署环境安装（marker 限 >=3.12）；在无 ansible 的 Python 3.10/3.11
    # 上此用例直接跳过，避免导入 _datatag 时 ImportError。
    pytest.importorskip("ansible")
    from ansible.module_utils._internal._datatag import (
        _AnsibleTaggedInt,
        _AnsibleTaggedStr,
    )
    from filter_plugins.resolve import _to_plain

    tagged = {
        "cluster": {"name": _AnsibleTaggedStr("a2")},
        "pd_cluster": {
            "prefill": {"dp_size": _AnsibleTaggedInt(4), "base_port": _AnsibleTaggedInt(9081)},
            "decode": {"dp_size": _AnsibleTaggedInt(8), "base_port": _AnsibleTaggedInt(9900)},
        },
        "nodes": {
            "prefill": [{"ip": _AnsibleTaggedStr("192.168.0.245")}],
            "decode": [{"ip": _AnsibleTaggedStr("192.168.0.127")}],
        },
        "proxy": {"node": _AnsibleTaggedStr("p0"), "port": _AnsibleTaggedInt(1999)},
        "model": {"path": _AnsibleTaggedStr("/mnt/x"), "served_model_name": _AnsibleTaggedStr("glm-52")},
    }
    plain = _to_plain(tagged)
    assert plain["cluster"]["name"] == "a2"
    assert plain["pd_cluster"]["prefill"]["dp_size"] == 4
    assert isinstance(plain["pd_cluster"]["prefill"]["dp_size"], int)