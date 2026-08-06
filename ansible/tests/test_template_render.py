"""Jinja2 模板渲染单测（TDD，任务 3）。

验证 run_dp_*_template.sh.j2 的渲染正确性：
- 节点命令替换：local_ip 来自 register 的 local_ip.stdout，非静态值
- 部署期变量（端口/维度/地址等）来自 resolve filter 输出（大写 KEY）
- vllm serve 的运行时参数（--port/--data-parallel-*/--tensor-parallel-size）保留为
  位置参数 $2..$7，由 launch_online_dp.py 按每引擎传入（与 deploy.sh 原模板一致）；
  绝不固化成 Jinja2 常量（否则 decode 多引擎端口/rank 冲突必挂）
- decode 特有内容：--compilation-config FULL_DECODE_ONLY、load_async: true、
  kv_consumer、recompute_scheduler_enable: true
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


def _render(node, role, cluster_type=None):
    """渲染指定角色模板。role ∈ {prefill, decode}；运行时参数（$1..$7）不进 ctx——
    它们由 launch_online_dp.py 按每引擎位置传入，模板只保留部署期确定的变量。
    cluster_type 可覆盖 resolve 结果（测 RoCE 开关）。"""
    cfg = yaml.safe_load(open(CONFIG))
    node_cfg = FilterModule().resolve_node(cfg, node)
    pdc = cfg["pd_cluster"]
    ctx = {
        # resolve filter 输出的大写 KEY（部署期确定，走 Jinja2）
        "nic": node_cfg["NIC"],
        "model_path": node_cfg["MODEL_PATH"],
        "served_model_name": node_cfg["SERVED_MODEL_NAME"],
        "max_model_len": node_cfg["MAX_MODEL_LEN"],
        "kv_port": node_cfg["KV_PORT"],
        "cluster_type": cluster_type or node_cfg["CLUSTER_TYPE"],
        # 节点命令 register 变量（非静态值）
        "local_ip": {"stdout": "192.168.0.245"},
        # 集群级配置（kv-transfer 跨角色 dp/tp）+ mooncake 路径
        "prefill_dp": pdc["prefill"]["dp_size"],
        "prefill_tp": pdc["prefill"]["tp_size"],
        "decode_dp": pdc["decode"]["dp_size"],
        "decode_tp": pdc["decode"]["tp_size"],
        "mooncake_config_path": "/root/pd/mooncake.json",
    }
    tpl = _env().get_template(f"run_dp_{role}_template.sh.j2")
    return tpl.render(**ctx)


def _render_prefill(node="p0", cluster_type=None):
    return _render(node, "prefill", cluster_type)


def _render_decode(node="d1", cluster_type=None):
    return _render(node, "decode", cluster_type)


def test_prefill_uses_register_local_ip():
    """local_ip 必须来自节点命令 register 的 local_ip.stdout，而非静态值。"""
    out = _render_prefill()
    assert 'local_ip="192.168.0.245"' in out
    # 禁止残留未替换占位符（__XX__ / {{ }}）
    assert "__" not in out
    assert "{{" not in out


def test_prefill_runtime_args_are_positional():
    """vllm serve 的运行时参数必须是位置参数 $2..$7（launch_online_dp.py 传入），
    而非 Jinja2 常量——与 deploy.sh 原模板 templates/run_dp_prefill_template.sh 一致。"""
    out = _render_prefill()
    assert "--port $2" in out
    assert "--data-parallel-size $3" in out
    assert "--data-parallel-rank $4" in out
    assert "--data-parallel-address $5" in out
    assert "--data-parallel-rpc-port $6" in out
    assert "--tensor-parallel-size $7" in out
    # ASCEND_RT_VISIBLE_DEVICES 保持 $1
    assert "export ASCEND_RT_VISIBLE_DEVICES=$1" in out
    # 禁止把运行时值固化成常量（旧缺陷：decode 多引擎端口/rank 冲突）
    assert "--port {{ start_port }}" not in out
    assert "--port 9081" not in out
    assert "--data-parallel-rank 0" not in out
    # 位置参数未被 Jinja2 吞掉（无残留未替换占位符）
    assert "{{" not in out


def test_prefill_roce_enabled_for_a2():
    """cluster_type == a2 时启用 HCCL_INTRA_ROCE_ENABLE；非 a2 不启用。"""
    out = _render_prefill()
    assert "export HCCL_INTRA_ROCE_ENABLE=1" in out

    # a3：走灵衢 UB，不启用 RoCE
    out_a3 = _render_prefill(cluster_type="a3")
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


# ==================== decode（任务 8 修复回归：位置参数 + decode 特有内容） ====================


def test_decode_runtime_args_are_positional():
    """decode 节点的 vllm serve 运行时参数必须是位置参数 $2..$7——
    d1 dp_size_local=2 会拉起 2 个引擎（9900/9901、rank 2/3），若固化成
    Jinja2 常量则两引擎以同端口同 rank 启动，第二引擎必挂。"""
    out = _render_decode()
    assert "--port $2" in out
    assert "--data-parallel-size $3" in out
    assert "--data-parallel-rank $4" in out
    assert "--data-parallel-address $5" in out
    assert "--data-parallel-rpc-port $6" in out
    assert "--tensor-parallel-size $7" in out
    assert "export ASCEND_RT_VISIBLE_DEVICES=$1" in out
    # 旧缺陷固化值不得出现
    assert "--port 9900" not in out
    assert "--data-parallel-rank 2" not in out
    assert "--port {{ start_port }}" not in out
    assert "{{" not in out


def test_decode_is_kv_consumer_with_async_load():
    """decode 特有内容：kv_role=kv_consumer、AscendStore load_async: true。"""
    out = _render_decode()
    assert '"kv_role": "kv_consumer"' in out
    assert '"load_async": true' in out


def test_decode_compilation_config_full_decode_only():
    """decode 用 --compilation-config cudagraph_mode=FULL_DECODE_ONLY（预填充阶段跑 decode 图）。"""
    out = _render_decode()
    assert "--compilation-config" in out
    assert '"cudagraph_mode": "FULL_DECODE_ONLY"' in out
    assert '"cudagraph_capture_sizes"' in out


def test_decode_recompute_scheduler_enabled():
    """decode 侧 additional-config 开启 recompute_scheduler_enable（prefill 侧为 false）。"""
    out = _render_decode()
    assert "recompute_scheduler_enable\": true" in out
    # prefill 侧对照：应为 false
    out_prefill = _render_prefill()
    assert "recompute_scheduler_enable\": false" in out_prefill


def test_decode_speculative_and_kv():
    """decode 特有 speculative tokens=3、HCCL_BUFFSIZE=2560、VLLM_HOST_IP。"""
    out = _render_decode()
    assert '"num_speculative_tokens": 3' in out
    assert "export HCCL_BUFFSIZE=2560" in out
    assert "export VLLM_HOST_IP=$local_ip" in out
    # decode 侧 enable_flashcomm1=false（prefill 侧 true）
    assert "enable_flashcomm1\": false" in out