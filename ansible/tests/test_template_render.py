"""Jinja2 模板渲染单测（TDD，Ansible 原生化任务 N7 改造）。

验证 run_dp_*_template.sh.j2 的渲染正确性：
- 节点命令替换：local_ip 来自 register 的 local_ip.stdout，非静态值
- 部署期变量（端口/维度/地址等）直接取自 A2 group_vars 平铺变量 / 派生变量
  （Ansible 原生化，直接传小写变量渲染模板）
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

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined
from jinja2.nativetypes import NativeEnvironment

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ANSIBLE_DIR = os.path.join(PROJECT_ROOT, "ansible")
GV_FILE = os.path.join(ANSIBLE_DIR, "inventories", "a2", "group_vars", "all.yml")
TPL_DIR = os.path.join(ANSIBLE_DIR, "templates")


def _env():
    # 与 Ansible 模板渲染一致（ansible/_internal/_templating/_jinja_bits.py: trim_blocks=True）：
    #   块标签独立成行时其尾部换行被吃掉，渲染输出无空行——否则本测试渲染出的空行是
    #   实际 gen 不会产生的假象，测不出模板换行回归。
    # StrictUndefined：模板里引用了未提供的变量会直接抛错，杜绝静默漏替换
    return Environment(loader=FileSystemLoader(TPL_DIR), undefined=StrictUndefined, trim_blocks=True)


def _ctx(role, cluster_type=None, mooncake_enabled=None):
    """直接用 A2 group_vars 平铺变量构造模板 ctx（小写变量，直接渲染）。
    role ∈ {prefill, decode}；运行时参数（$1..$7）不进 ctx——它们由
    launch_online_dp.py 按每引擎位置传入，模板只保留部署期确定的变量。
    cluster_type 可覆盖 group_vars 值（测 RoCE 开关）；
    mooncake_enabled 可覆盖 group_vars 的 kvpool 开关（测直连 MooncakeConnectorV1 分支）。
    
    group_vars 含有 Jinja 表达式（deploy_config→{_52,_53}→_active→pd_cluster 等），
    用 _gv() 已展开为原生 Python 类型。"""
    gv = _gv()
    pd = gv["pd_cluster"]
    return {
        # 节点级变量：nic 取自 A2 inventory 节点（host 级）；ip 取自 inventory 静态字段（真实网卡 IP）
        "nic": "enp67s0f5",
        "ip": "192.168.0.245",
        # 集群级平铺变量（来源：a2/group_vars/all.yml 的 model / model_path / pd_cluster）
        "model_path": gv["model_path"],
        "served_model_name": gv["model"]["served_model_name"],
        "max_model_len": pd["max_model_len"],
        "kv_port": pd[role]["kv_port"],          # prefill=30000 / decode=30100
        "cluster_type": cluster_type or gv["cluster_type"],
        # 跨角色 dp/tp（kv-transfer 配置）+ mooncake 路径 + kvpool 开关
        "prefill_dp": pd["prefill"]["dp_size"],
        "prefill_tp": pd["prefill"]["tp_size"],
        "decode_dp": pd["decode"]["dp_size"],
        "decode_tp": pd["decode"]["tp_size"],
        "mooncake_config_path": "/root/pd/mooncake.json",
        "mooncake": {"enabled": gv["mooncake"]["enabled"] if mooncake_enabled is None else mooncake_enabled},
        # GLM-5.3 1MB 新功能嵌套变量（模板引用 pd_cluster.*/runtime/profiler）
        "pd_cluster": pd,
        "runtime": gv.get("runtime", {}),
        "profiler": gv.get("profiler", {}),
    }


def _render(role, cluster_type=None, mooncake_enabled=None):
    tpl = _env().get_template(f"run_dp_{role}_template.sh.j2")
    return tpl.render(**{**_ctx(role, cluster_type, mooncake_enabled)})


def _render_prefill(cluster_type=None, mooncake_enabled=None):
    return _render("prefill", cluster_type, mooncake_enabled)


def _render_decode(cluster_type=None, mooncake_enabled=None):
    return _render("decode", cluster_type, mooncake_enabled)


def test_prefill_uses_inventory_ip():
    """local_ip 必须来自 inventory 静态 ip 字段（真实网卡 IP），而非运行时 register。"""
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
    gv = _gv(); pd = gv["pd_cluster"]
    out = _render_prefill(mooncake_enabled=True)
    assert '"kv_connector": "MultiConnector"' in out
    assert '"kv_role": "kv_producer"' in out
    assert '"kv_connector": "MooncakeConnectorV1"' in out
    assert '"kv_connector": "AscendStoreConnector"' in out
    assert '"kv_port": "30000"' in out
    # 跨角色 dp/tp 正确填入（从 group_vars 动态读取，不硬编码）
    assert f'"prefill": {{ "dp_size": {pd["prefill"]["dp_size"]}, "tp_size": {pd["prefill"]["tp_size"]} }}' in out
    assert f'"decode": {{ "dp_size": {pd["decode"]["dp_size"]}, "tp_size": {pd["decode"]["tp_size"]} }}' in out


def test_prefill_model_and_served_name():
    """模型路径与 served-model-name、max-model-len 正确替换，且核心参数无遗漏。"""
    gv = _gv()   # 用 group_vars 当前值，避免硬编码失效
    out = _render_prefill()
    assert f"vllm serve {gv['model_path']}" in out
    assert f"--served-model-name {gv['model']['served_model_name']}" in out
    assert f"--max-model-len {gv['pd_cluster']['max_model_len']}" in out
    pd = gv["pd_cluster"]
    # 关键推理参数须保留（对照 generated/run_dp_template_p0.sh；gpu-mem 未配置时用模板默认 a2=0.95）
    gpu_mem = pd["prefill"].get("gpu_memory_utilization", "0.95")
    for flag in (
        "--enable-expert-parallel",
        "--enable-prefix-caching",
        "--enable-chunked-prefill",
        f"--gpu-memory-utilization {gpu_mem}",
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
    out = _render_decode(mooncake_enabled=True)
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


def test_a2_env_branch_not_leaked():
    """a2/a3 环境变量分支互不泄漏：只保留本集群分支的导出。
    用按行精确匹配（'export OMP_NUM_THREADS=1' 是 '...=10' 的子串，不能做 substring 断言）。"""
    lines = _render_prefill().splitlines()
    assert "export OMP_NUM_THREADS=10" in lines
    assert "export OMP_NUM_THREADS=1" not in lines
    assert "export HCCL_BUFFSIZE=256" in lines
    assert "export HCCL_BUFFSIZE=400" not in lines
    assert "export ASCEND_A3_ENABLE" not in lines
    lines_a3 = _render_prefill(cluster_type="a3").splitlines()
    assert "export OMP_NUM_THREADS=1" in lines_a3
    assert "export OMP_NUM_THREADS=10" not in lines_a3
    assert "export HCCL_BUFFSIZE=400" in lines_a3
    assert "export ASCEND_A3_ENABLE=1" in lines_a3


def test_rendered_output_clean_newlines():
    """渲染结果换行干净（Ansible trim_blocks 吃掉块标签行换行）：
    无连续空行、无纯空白行——保证独立成行的块标签不会污染输出。"""
    for out in (_render_prefill(), _render_decode()):
        lines = out.splitlines()
        for i in range(len(lines) - 1):
            assert not (lines[i] == "" and lines[i + 1] == ""), f"发现连续空行 @{i}"
        for i, ln in enumerate(lines):
            assert not (ln and not ln.strip()), f"发现纯空白行 @{i}"


# ==================== 部署模式：kvpool 开关（mooncake.enabled） ====================


def test_kvpool_on_keeps_mooncake_config_path():
    """kvpool 开启（默认）：导出 MOONCAKE_CONFIG_PATH 且走 MultiConnector。"""
    out = _render_prefill(mooncake_enabled=True)
    assert 'export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"' in out
    assert '"kv_connector": "MultiConnector"' in out


def test_kvpool_off_prefill_direct_mooncake():
    """kvpool 关闭：prefill 用直连 MooncakeConnectorV1（engine_id 可配 + use_ascend_direct），
    无 MultiConnector / AscendStoreConnector / MOONCAKE_CONFIG_PATH。"""
    gv = _gv(); pd = gv["pd_cluster"]
    out = _render_prefill(mooncake_enabled=False)
    assert '"kv_connector": "MooncakeConnectorV1"' in out
    assert '"kv_role": "kv_producer"' in out
    # engine_id 未配置时模板默认 prefill=0
    assert f'"engine_id": "{pd["prefill"].get("engine_id", "0")}"' in out
    assert '"use_ascend_direct": true' in out
    assert '"kv_connector": "MultiConnector"' not in out
    assert "AscendStoreConnector" not in out
    assert "MOONCAKE_CONFIG_PATH" not in out
    # 直连仍保留 kv_port + P/D 并行度（从 group_vars 动态读取）
    assert '"kv_port": "30000"' in out
    assert f'"prefill": {{ "dp_size": {pd["prefill"]["dp_size"]}, "tp_size": {pd["prefill"]["tp_size"]} }}' in out
    assert f'"decode": {{ "dp_size": {pd["decode"]["dp_size"]}, "tp_size": {pd["decode"]["tp_size"]} }}' in out


def test_kvpool_off_decode_direct_mooncake():
    """kvpool 关闭：decode 用直连 MooncakeConnectorV1（engine_id 可配），仍 kv_consumer。"""
    gv = _gv(); pd = gv["pd_cluster"]
    out = _render_decode(mooncake_enabled=False)
    assert '"kv_connector": "MooncakeConnectorV1"' in out
    assert '"kv_role": "kv_consumer"' in out
    # engine_id 未配置时模板默认 decode=2
    assert f'"engine_id": "{pd["decode"].get("engine_id", "2")}"' in out
    assert '"use_ascend_direct": true' in out
    assert '"kv_connector": "MultiConnector"' not in out
    assert "AscendStoreConnector" not in out
    assert "MOONCAKE_CONFIG_PATH" not in out
    assert '"kv_port": "30100"' in out
    # decode 专属 flag 不受 kvpool 开关影响
    assert "--compilation-config" in out


# ==================== GLM-5.3 1MB CP 新功能（context_parallel_size / timeouts / profiler / capture_sizes） ====================
# 这些功能是「可选」的：默认 GLM-5.2 配置不定义这些字段（模板不渲染）。
# 测试用 skipif 判断字段是否配置——配置了才验证渲染正确性，没配置则跳过（向后兼容）。


def _gv():
    gv = yaml.safe_load(open(GV_FILE))
    _ev = NativeEnvironment(undefined=Undefined)
    for _ in range(3):
        any_skipped = False
        for k, v in gv.items():
            if isinstance(v, str) and "{{" in v:
                rendered = _ev.from_string(v).render(**gv)
                if rendered != v:
                    gv[k] = rendered
                else:
                    any_skipped = True
            elif isinstance(v, dict):
                for kk in list(v.keys()):
                    vv = v[kk]
                    if isinstance(vv, str) and "{{" in vv:
                        rendered = _ev.from_string(vv).render(**gv)
                        if rendered != vv:
                            v[kk] = rendered
                        else:
                            any_skipped = True
        if not any_skipped:
            break
    return gv


def _gv_pd():
    return _gv()["pd_cluster"]


def _feature_config(feature):
    """GLM-5.3 可选字段是否已配置。feature: cp / timeouts / capture / engine_id"""
    pd = _gv_pd()
    if feature == "cp":
        return "context_parallel_size" in pd["prefill"] and "cp_kv_cache_interleave_size" in pd
    if feature == "timeouts":
        return "timeouts" in _gv().get("runtime", {})
    if feature == "capture":
        return "capture_sizes" in pd["decode"]
    if feature == "engine_id":
        return "engine_id" in pd["prefill"] and "engine_id" in pd["decode"]
    return False


def test_cp_flags_render_when_configured():
    """配置 context_parallel_size 后，P/D 模板都渲染 CP 三件套。"""
    if not _feature_config("cp"):
        pytest.skip("GLM-5.3 CP 未配置（默认 GLM-5.2），跳过")
    pd = _gv_pd()
    for out in (_render_prefill(), _render_decode()):
        assert f"--prefill-context-parallel-size {pd['prefill']['context_parallel_size']}" in out
        assert f"--decode-context-parallel-size {pd['decode']['context_parallel_size']}" in out
        assert f"--cp-kv-cache-interleave-size {pd['cp_kv_cache_interleave_size']}" in out


def test_cp_flags_absent_when_unconfigured():
    """未配置 context_parallel_size 时 CP 三件套不渲染（向后兼容）。"""
    ctx = _ctx("prefill")
    ctx["pd_cluster"] = {k: v for k, v in ctx["pd_cluster"].items() if k != "cp_kv_cache_interleave_size"}
    ctx["pd_cluster"]["prefill"] = {k: v for k, v in ctx["pd_cluster"]["prefill"].items() if k != "context_parallel_size"}
    ctx["pd_cluster"]["decode"] = {k: v for k, v in ctx["pd_cluster"]["decode"].items() if k != "context_parallel_size"}
    out = _env().get_template("run_dp_prefill_template.sh.j2").render(**ctx)
    assert "--prefill-context-parallel-size" not in out
    assert "--decode-context-parallel-size" not in out
    assert "--cp-kv-cache-interleave-size" not in out


def test_runtime_timeouts_export():
    """配置 runtime.timeouts 后模板 export 超时环境变量。"""
    if not _feature_config("timeouts"):
        pytest.skip("runtime.timeouts 未配置（默认 GLM-5.2），跳过")
    rt = _gv()["runtime"]["timeouts"]
    for out in (_render_prefill(), _render_decode()):
        assert f"export VLLM_RPC_TIMEOUT={rt['vllm_rpc_timeout']}" in out
        assert f"export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS={rt['execute_model_timeout_seconds']}" in out
        assert f"export HCCL_EXEC_TIMEOUT={rt['hccl_exec_timeout']}" in out
        assert f"export HCCL_CONNECT_TIMEOUT={rt['hccl_connect_timeout']}" in out


def test_profiler_disabled_not_rendered():
    """profiler 未启用（默认）时不渲染 --profiler-config。"""
    for out in (_render_prefill(), _render_decode()):
        assert "--profiler-config" not in out


def test_capture_sizes_configurable():
    """decode cudagraph capture sizes 从 pd_cluster.decode.capture_sizes 读。"""
    if not _feature_config("capture"):
        pytest.skip("decode.capture_sizes 未配置（默认长列表），跳过")
    pd = _gv_pd()
    out = _render_decode()
    assert f'"cudagraph_capture_sizes": {pd["decode"]["capture_sizes"]}' in out


def test_engine_id_from_config():
    """无 kvpool 直连时 engine_id 从 pd_cluster 读（prefill=0，decode 可配）。"""
    if not _feature_config("engine_id"):
        pytest.skip("engine_id 未配置（默认 prefill=0/decode=2），跳过")
    pd = _gv_pd()
    assert f'"engine_id": "{pd["prefill"]["engine_id"]}"' in _render_prefill(mooncake_enabled=False)
    assert f'"engine_id": "{pd["decode"]["engine_id"]}"' in _render_decode(mooncake_enabled=False)