"""Jinja2 模板渲染单测（TDD，任务 3）。

验证 run_dp_*_template.sh.j2 的渲染正确性：
- 节点命令替换：local_ip 来自 register 的 local_ip.stdout，非静态值
- 端口/维度/地址等参数来自 resolve filter 输出（大写 KEY）
- RoCE 开关按 cluster_type == 'a2' 条件切换
- kv-transfer-config 完整保留 MultiConnector（公 Mooncake + AscendStore）

仅纯本地 Jinja2 渲染，不拉起任何服务、不 ssh 节点、不占卡。
"""
import os
import sys

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "ansible"))
from filter_plugins.resolve import FilterModule  # noqa: E402

CONFIG = os.path.join(PROJECT_ROOT, "config.yaml")
TPL_DIR = os.path.join(PROJECT_ROOT, "ansible", "templates")


def _env():
    # StrictUndefined：模板里引用了未提供的变量会直接抛错，杜绝静默漏替换
    return Environment(loader=FileSystemLoader(TPL_DIR), undefined=StrictUndefined)


def _render_prefill(node="p0"):
    cfg = yaml.safe_load(open(CONFIG))
    node_cfg = FilterModule().resolve_node(cfg, node)
    pdc = cfg["pd_cluster"]
    ctx = {
        # resolve filter 输出的大写 KEY
        "nic": node_cfg["NIC"],
        "model_path": node_cfg["MODEL_PATH"],
        "served_model_name": node_cfg["SERVED_MODEL_NAME"],
        "max_model_len": node_cfg["MAX_MODEL_LEN"],
        "kv_port": node_cfg["KV_PORT"],
        "start_port": node_cfg["VLLM_START_PORT"],
        "dp_size": node_cfg["DP_SIZE"],
        "dp_rank": node_cfg["DP_RANK_START"],
        "dp_address": node_cfg["DP_ADDRESS"],
        "rpc_port": node_cfg["DP_RPC_PORT"],
        "tp_size": node_cfg["TP_SIZE"],
        "cluster_type": node_cfg["CLUSTER_TYPE"],
        # 节点命令 register 变量（非静态值）
        "local_ip": {"stdout": "192.168.0.245"},
        # 集群级配置（kv-transfer 跨角色 dp/tp）+ mooncake 路径
        "prefill_dp": pdc["prefill"]["dp_size"],
        "prefill_tp": pdc["prefill"]["tp_size"],
        "decode_dp": pdc["decode"]["dp_size"],
        "decode_tp": pdc["decode"]["tp_size"],
        "mooncake_config_path": "/root/pd/mooncake.json",
    }
    tpl = _env().get_template("run_dp_prefill_template.sh.j2")
    return tpl.render(**ctx)


def test_prefill_uses_register_local_ip():
    """local_ip 必须来自节点命令 register 的 local_ip.stdout，而非静态值。"""
    out = _render_prefill()
    assert 'local_ip="192.168.0.245"' in out
    # 禁止残留未替换占位符（__XX__ / {{ }}）
    assert "__" not in out
    assert "{{" not in out


def test_prefill_port_and_dimensions_from_resolve_filter():
    """端口/维度/地址等参数来自 resolve filter 输出（p0: port 9081, dp4 tp8, rank0）。"""
    out = _render_prefill()
    assert "--port 9081" in out
    assert "--data-parallel-size 4" in out
    assert "--data-parallel-rank 0" in out
    assert "--data-parallel-address 192.168.0.245" in out
    assert "--data-parallel-rpc-port 16591" in out
    assert "--tensor-parallel-size 8" in out


def test_prefill_roce_enabled_for_a2():
    """cluster_type == a2 时启用 HCCL_INTRA_ROCE_ENABLE；非 a2 不启用。"""
    out = _render_prefill()
    assert "export HCCL_INTRA_ROCE_ENABLE=1" in out

    # a3：走灵衢 UB，不启用 RoCE
    cfg = yaml.safe_load(open(CONFIG))
    node_cfg = FilterModule().resolve_node(cfg, "p0")
    node_cfg["CLUSTER_TYPE"] = "a3"
    pdc = cfg["pd_cluster"]
    ctx = {
        "nic": node_cfg["NIC"],
        "model_path": node_cfg["MODEL_PATH"],
        "served_model_name": node_cfg["SERVED_MODEL_NAME"],
        "max_model_len": node_cfg["MAX_MODEL_LEN"],
        "kv_port": node_cfg["KV_PORT"],
        "start_port": node_cfg["VLLM_START_PORT"],
        "dp_size": node_cfg["DP_SIZE"],
        "dp_rank": node_cfg["DP_RANK_START"],
        "dp_address": node_cfg["DP_ADDRESS"],
        "rpc_port": node_cfg["DP_RPC_PORT"],
        "tp_size": node_cfg["TP_SIZE"],
        "cluster_type": node_cfg["CLUSTER_TYPE"],
        "local_ip": {"stdout": "192.168.0.245"},
        "prefill_dp": pdc["prefill"]["dp_size"],
        "prefill_tp": pdc["prefill"]["tp_size"],
        "decode_dp": pdc["decode"]["dp_size"],
        "decode_tp": pdc["decode"]["tp_size"],
        "mooncake_config_path": "/root/pd/mooncake.json",
    }
    out_a3 = _env().get_template("run_dp_prefill_template.sh.j2").render(**ctx)
    assert "HCCL_INTRA_ROCE_ENABLE" not in out_a3


def test_prefill_kv_transfer_config_multi_connector():
    """kv-transfer-config 完整保留：MultiConnector + MooncakeConnectorV1 + AscendStoreConnector。"""
    out = _render_prefill()
    assert '"kv_connector": "MultiConnector"' in out
    assert '"kv_role": "kv_producer"' in out
    assert '"kv_connector": "MooncakeConnectorV1"' in out
    assert '"kv_connector": "AscendStoreConnector"' in out
    assert '"kv_port": "30000"' in out
    # 跨角色 dp/tp 正确填入
    assert '"prefill": { "dp_size": 4, "tp_size": 8 }' in out
    assert '"decode": { "dp_size": 8, "tp_size": 4 }' in out


def test_prefill_model_and_served_name():
    """模型路径与 served-model-name、max-model-len 正确替换，且核心参数无遗漏。"""
    out = _render_prefill()
    assert "vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8" in out
    assert "--served-model-name glm-52" in out
    assert "--max-model-len 200000" in out
    # 关键推理参数须保留（对照 generated/run_dp_template_p0.sh）
    for flag in (
        "--enable-expert-parallel",
        "--enable-prefix-caching",
        "--enable-chunked-prefill",
        "--gpu-memory-utilization 0.95",
        "--quantization ascend",
        "--tool-call-parser glm47",
        "--reasoning-parser glm45",
        "--speculative-config",
    ):
        assert flag in out, f"缺少参数: {flag}"