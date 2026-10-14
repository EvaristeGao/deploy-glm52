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
GV_FILE_A3 = os.path.join(ANSIBLE_DIR, "inventories", "a3", "group_vars", "all.yml")
TPL_DIR = os.path.join(ANSIBLE_DIR, "templates")


def _env():
    # 与 Ansible 模板渲染一致（ansible/_internal/_templating/_jinja_bits.py: trim_blocks=True）：
    #   块标签独立成行时其尾部换行被吃掉，渲染输出无空行——否则本测试渲染出的空行是
    #   实际 gen 不会产生的假象，测不出模板换行回归。
    # StrictUndefined：模板里引用了未提供的变量会直接抛错，杜绝静默漏替换
    return Environment(loader=FileSystemLoader(TPL_DIR), undefined=StrictUndefined, trim_blocks=True)


def _ctx(role, cluster_type=None, mooncake_enabled=None, gv=None):
    """直接用 group_vars 平铺变量构造模板 ctx（小写变量，直接渲染）。
    role ∈ {prefill, decode}；运行时参数（$1..$7）不进 ctx——它们由
    launch_online_dp.py 按每引擎位置传入，模板只保留部署期确定的变量。
    cluster_type 可覆盖 group_vars 值（测 RoCE 开关）；
    mooncake_enabled 可覆盖 group_vars 的 kvpool 开关（测直连 MooncakeConnectorV1 分支）；
    gv 可传入别的 inventory / 别的 deploy_config 档位（默认 A2 当前档）。

    group_vars 含有 Jinja 表达式（deploy_config→{_200k,_1m}→_active→pd_cluster 等），
    用 _gv() 已展开为原生 Python 类型。"""
    gv = gv if gv is not None else _gv()
    pd = gv["pd_cluster"]
    return {
        # 节点级变量：nic 取自 inventory 节点（host 级）；ip 取自 inventory 静态字段（真实网卡 IP）
        "nic": "enp67s0f5",
        "ip": "192.168.0.245",
        # 集群级平铺变量（来源：group_vars 的 model / model_path / pd_cluster）
        "model_path": gv["model_path"],
        "served_model_name": gv["model"]["served_model_name"],
        "max_model_len": pd["max_model_len"],
        "kv_port": pd[role]["kv_port"],          # prefill=30000 / decode=30100(A2)
        "cluster_type": cluster_type or gv["cluster_type"],
        # 跨角色 dp/tp（kv-transfer 配置）+ mooncake 路径 + kvpool 开关
        "prefill_dp": pd["prefill"]["dp_size"],
        "prefill_tp": pd["prefill"]["tp_size"],
        "decode_dp": pd["decode"]["dp_size"],
        "decode_tp": pd["decode"]["tp_size"],
        "mooncake_config_path": "/root/pd/mooncake.json",
        "mooncake": {"enabled": gv["mooncake"]["enabled"] if mooncake_enabled is None else mooncake_enabled},
        # 1M 新功能嵌套变量（模板引用 pd_cluster.*/runtime/profiler）
        "pd_cluster": pd,
        "runtime": gv.get("runtime", {}),
        "profiler": gv.get("profiler", {}),
    }


def _render(role, cluster_type=None, mooncake_enabled=None, gv=None):
    tpl = _env().get_template(f"run_dp_{role}_template.sh.j2")
    return tpl.render(**{**_ctx(role, cluster_type, mooncake_enabled, gv)})


def _render_prefill(cluster_type=None, mooncake_enabled=None):
    return _render("prefill", cluster_type, mooncake_enabled)


def _render_decode(cluster_type=None, mooncake_enabled=None):
    return _render("decode", cluster_type, mooncake_enabled)


def _render_a3(role, deploy_config=None, mooncake_enabled=None):
    """按 A3 group_vars 的指定档位（200k / 1m）渲染 A3 模板。"""
    gv = _gv(GV_FILE_A3, deploy_config)
    return _render(role, cluster_type="a3", mooncake_enabled=mooncake_enabled, gv=gv)


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


def _gv(path=GV_FILE, deploy_config=None):
    """加载 group_vars 并把里面的 Jinja 表达式多轮展开成原生 Python 类型。
    deploy_config 非空时覆盖文件里的取值（用于测另一档：200k / 1m）。"""
    gv = yaml.safe_load(open(path))
    if deploy_config is not None:
        gv["deploy_config"] = deploy_config
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


def test_a2_prefill_cp_falls_back_to_decode_value():
    """A2 prefill 未配置 decode_context_parallel_size → decode CP 回落到 decode 自身口径
    （A2 两侧同值，行为与改造前一致）。"""
    if not _feature_config("cp"):
        pytest.skip("CP 未配置，跳过")
    pd = _gv_pd()
    assert "decode_context_parallel_size" not in pd["prefill"]
    assert f"--decode-context-parallel-size {pd['decode']['context_parallel_size']}" in _render_prefill()


# ==================== A3 部署配置选择器（deploy_config: 200k | 1m） ====================
# 官方参考：https://docs.vllm.ai/projects/ascend/en/v0.23.0/tutorials/models/GLM5.2.html
#   198K PD（§5.2.1）：prefill PP2 TP16 + decode DP8 TP4；1M PD（§5.2.2）：prefill DP2 TP16 + decode DP8 TP4
#   本项目 launch_online_dp.py 是纯 DP 启动器，表达不了 PP，故 198K 档 prefill 取 DP2 TP16 近似
#   （保留官方 TP16），decode 与官方一致；两档均关闭 enable_sparse_sfa_c8（c3b1d4d 精度策略）


def test_a3_deploy_config_selects_matching_block():
    """deploy_config 选中哪一档，pd_cluster 就解析成哪一块（不锁死具体默认值，防止手改后单测变红）。"""
    gv = _gv(GV_FILE_A3)
    cfg = gv["deploy_config"]
    assert cfg in ("200k", "1m")
    assert gv["pd_cluster"] == gv[f"_{cfg}"]["pd_cluster"]


def test_a3_1m_topology():
    """A3 1M 档：官方参考拓扑 + 1M 上下文 + 官方端口。"""
    pd = _gv(GV_FILE_A3, "1m")["pd_cluster"]
    assert pd["max_model_len"] == 1048576
    assert (pd["prefill"]["dp_size"], pd["prefill"]["tp_size"], pd["prefill"]["dp_size_local"]) == (2, 16, 1)
    assert (pd["decode"]["dp_size"], pd["decode"]["tp_size"], pd["decode"]["dp_size_local"]) == (8, 4, 4)
    assert pd["prefill"]["rpc_port"] == 16600
    assert pd["decode"]["rpc_port"] == 16600
    assert pd["prefill"]["kv_port"] == 30000
    assert pd["decode"]["kv_port"] == 30200
    assert pd["prefill"]["base_port"] == 9081
    assert pd["decode"]["base_port"] == 9900
    assert pd["num_cards"] == 16
    assert pd["cp_kv_cache_interleave_size"] == 128


def test_a3_200k_topology():
    """A3 200K 档：官方 198K PD 参考的 dp/tp（prefill 用 DP2 TP16 代替官方 PP2 TP16）。"""
    pd = _gv(GV_FILE_A3, "200k")["pd_cluster"]
    assert pd["max_model_len"] == 202752
    assert (pd["prefill"]["dp_size"], pd["prefill"]["tp_size"], pd["prefill"]["dp_size_local"]) == (2, 16, 1)
    assert (pd["decode"]["dp_size"], pd["decode"]["tp_size"], pd["decode"]["dp_size_local"]) == (8, 4, 4)
    assert pd["prefill"]["rpc_port"] == 16591
    assert pd["prefill"]["kv_port"] == 30000
    assert pd["decode"]["kv_port"] == 30200
    assert pd["num_cards"] == 16
    # 200K 档不带 CP（官方 198K 参考也没有）
    assert "context_parallel_size" not in pd["prefill"]
    assert "cp_kv_cache_interleave_size" not in pd
    out = _render_a3("prefill", "200k")
    assert "--prefill-context-parallel-size" not in out
    assert "--decode-context-parallel-size" not in out
    assert "--cp-kv-cache-interleave-size" not in out
    # 每节点卡数：prefill 1 引擎×16 卡、decode 4 引擎×4 卡 = 16
    assert pd["prefill"]["dp_size_local"] * pd["prefill"]["tp_size"] == pd["num_cards"]
    assert pd["decode"]["dp_size_local"] * pd["decode"]["tp_size"] == pd["num_cards"]
    # A3 inventory 固定 2 prefill + 2 decode 节点，dp_size = 节点数 × 每节点引擎数
    assert pd["prefill"]["dp_size"] == 2 * pd["prefill"]["dp_size_local"]
    assert pd["decode"]["dp_size"] == 2 * pd["decode"]["dp_size_local"]


def test_a3_1m_cp_flags_split_by_role():
    """A3 1M：prefill 模板声明 decode CP=16，decode 模板声明 decode CP=4（官方口径不同）。"""
    out_p = _render_a3("prefill", "1m")
    assert "--prefill-context-parallel-size 1" in out_p
    assert "--decode-context-parallel-size 16" in out_p
    assert "--decode-context-parallel-size 4" not in out_p
    out_d = _render_a3("decode", "1m")
    assert "--prefill-context-parallel-size 1" in out_d
    assert "--decode-context-parallel-size 4" in out_d
    assert "--decode-context-parallel-size 16" not in out_d
    for out in (out_p, out_d):
        assert "--cp-kv-cache-interleave-size 128" in out


def test_a3_1m_additional_config_per_role():
    """A3 1M：additional-config 逐字取 group_vars 配置值（prefill 开 DSA CP，decode 不开）。"""
    pd = _gv(GV_FILE_A3, "1m")["pd_cluster"]
    out_p = _render_a3("prefill", "1m")
    assert pd["prefill"]["additional_config"] in out_p
    for flag in ('"enable_dsa_cp":true', '"enable_sparse_sfa_c8": false',
                 '"enable_sparse_li_c8": true', '"c8_enable_reshape_optim":true',
                 '"recompute_scheduler_enable": false'):
        assert flag in out_p, f"prefill additional-config 缺少 {flag}"

    out_d = _render_a3("decode", "1m")
    assert pd["decode"]["additional_config"] in out_d
    for flag in ('"enable_sparse_sfa_c8": false', '"enable_sparse_li_c8": true',
                 '"recompute_scheduler_enable":true', '"multistream_overlap_shared_expert":true'):
        assert flag in out_d, f"decode additional-config 缺少 {flag}"
    assert '"enable_dsa_cp"' not in out_d
    assert '"c8_enable_reshape_optim"' not in out_d


def test_a3_200k_additional_config_per_role():
    """A3 200K：additional-config 逐字取 group_vars 配置值（prefill 开 DSA CP，decode 不开）。"""
    pd = _gv(GV_FILE_A3, "200k")["pd_cluster"]
    out_p = _render_a3("prefill", "200k")
    assert pd["prefill"]["additional_config"] in out_p
    for flag in ('"enable_dsa_cp":true', '"enable_sparse_sfa_c8": false',
                 '"enable_sparse_li_c8": true', '"c8_enable_reshape_optim":true',
                 '"recompute_scheduler_enable": false'):
        assert flag in out_p, f"prefill additional-config 缺少 {flag}"

    out_d = _render_a3("decode", "200k")
    assert pd["decode"]["additional_config"] in out_d
    assert '"recompute_scheduler_enable":true' in out_d
    assert '"enable_dsa_cp"' not in out_d


def test_a3_sfa_c8_disabled_for_both_configs():
    """本项目策略：两档都关闭 enable_sparse_sfa_c8（官方参考为 true；见 c3b1d4d 精度问题）。"""
    for cfg in ("200k", "1m"):
        pd = _gv(GV_FILE_A3, cfg)["pd_cluster"]
        for role in ("prefill", "decode"):
            assert '"enable_sparse_sfa_c8": false' in pd[role]["additional_config"]
            assert '"enable_sparse_sfa_c8": true' not in _render_a3(role, cfg)


def test_a3_hf_overrides_only_in_200k():
    """--hf-overrides 只在 200K 档渲染（官方 198K PD 参考有、1M 参考没有）。"""
    pd200 = _gv(GV_FILE_A3, "200k")["pd_cluster"]
    for role in ("prefill", "decode"):
        out = _render_a3(role, "200k")
        assert f"--hf-overrides '{pd200[role]['hf_overrides']}'" in out
        assert "use_index_cache" in out
    for role in ("prefill", "decode"):
        assert "--hf-overrides" not in _render_a3(role, "1m")
    # A2 也没有
    assert "--hf-overrides" not in _render_prefill()
    assert "--hf-overrides" not in _render_decode()


def test_a3_max_num_batched_tokens_per_config():
    """--max-num-batched-tokens 分档：官方 198K = prefill 16384 / decode 164；1M = 8192 / 192。"""
    pd200 = _gv(GV_FILE_A3, "200k")["pd_cluster"]
    pd1m = _gv(GV_FILE_A3, "1m")["pd_cluster"]
    assert f"--max-num-batched-tokens {pd200['prefill']['max_num_batched_tokens']}" in _render_a3("prefill", "200k")
    assert f"--max-num-batched-tokens {pd200['decode']['max_num_batched_tokens']}" in _render_a3("decode", "200k")
    assert (pd200["prefill"]["max_num_batched_tokens"], pd200["decode"]["max_num_batched_tokens"]) == (16384, 164)
    assert f"--max-num-batched-tokens {pd1m['prefill'].get('max_num_batched_tokens', 8192)}" in _render_a3("prefill", "1m")
    assert f"--max-num-batched-tokens {pd1m['decode']['max_num_batched_tokens']}" in _render_a3("decode", "1m")


def test_a3_decode_spec_tokens_and_hccl_buffsize_per_config():
    """decode 分档：MTP spec tokens 3(200K)/5(1M)；HCCL_BUFFSIZE 256(200K)/400(1M)。"""
    assert '"num_speculative_tokens": 3' in _render_a3("decode", "200k")
    assert '"num_speculative_tokens": 5' in _render_a3("decode", "1m")
    assert "export HCCL_BUFFSIZE=256" in _render_a3("decode", "200k").splitlines()
    assert "export HCCL_BUFFSIZE=400" in _render_a3("decode", "1m").splitlines()
    # prefill 两档都是 400（官方一致）
    for cfg in ("200k", "1m"):
        assert "export HCCL_BUFFSIZE=400" in _render_a3("prefill", cfg).splitlines()


def test_a3_env_alignment_with_official_reference():
    """A3 环境变量对齐官方参考：
    ACL_OP_INIT_MODE 两侧都有；HCCL_BUFFSIZE=400；两侧都无 VLLM_HOST_IP。
    （MLAPO 只在 decode 开，见 test_a3_a2_mlapo_branch）"""
    for role in ("prefill", "decode"):
        lines = _render_a3(role, "1m").splitlines()
        assert "export ASCEND_A3_ENABLE=1" in lines
        assert "export ACL_OP_INIT_MODE=1" in lines
        assert "export HCCL_BUFFSIZE=400" in lines
        assert "export HCCL_BUFFSIZE=256" not in lines
        assert "export VLLM_HOST_IP=$local_ip" not in lines
    # A2 仍然保留 VLLM_HOST_IP（回归）
    assert "export VLLM_HOST_IP=$local_ip" in _render_decode().splitlines()


def test_a3_a2_mlapo_branch():
    """MLAPO：A3/A2 的 decode 都开；A3 prefill 不开（官方 PD 场景只在 decode 开），A2 prefill 开。"""
    assert "export VLLM_ASCEND_ENABLE_MLAPO=1" in _render_a3("decode", "1m").splitlines()
    assert "export VLLM_ASCEND_ENABLE_MLAPO=1" not in _render_a3("prefill", "1m").splitlines()
    assert "export VLLM_ASCEND_ENABLE_MLAPO=1" in _render_prefill().splitlines()


def test_a3_serve_args_by_role():
    """A3 1M 推理参数：prefill enforce-eager/无 chunked-prefill；decode FULL_DECODE_ONLY/async。
    具体数值（batched-tokens / gpu-mem 等）从 group_vars 动态读，避免配置微调就红。"""
    pd = _gv(GV_FILE_A3, "1m")["pd_cluster"]
    out_p = _render_a3("prefill", "1m")
    assert "--max-num-seqs 64" in out_p
    assert "--enforce-eager" in out_p
    assert '"num_speculative_tokens": 1' in out_p
    assert "--enable-prefix-caching" in out_p
    assert "--enable-chunked-prefill" not in out_p
    assert "--async-scheduling" not in out_p
    assert "--safetensors-load-strategy" not in out_p
    for key in ("max_num_seqs", "gpu_memory_utilization", "max_num_batched_tokens"):
        if key in pd["prefill"]:
            assert f"--{key.replace('_', '-')} {pd['prefill'][key]}" in out_p

    out_d = _render_a3("decode", "1m")
    assert "--max-num-seqs 32" in out_d
    assert '"cudagraph_mode": "FULL_DECODE_ONLY"' in out_d
    assert "--async-scheduling" in out_d
    assert "--safetensors-load-strategy" not in out_d
    for key in ("max_num_seqs", "gpu_memory_utilization", "max_num_batched_tokens"):
        if key in pd["decode"]:
            assert f"--{key.replace('_', '-')} {pd['decode'][key]}" in out_d
    # A3 decode 用固定 cudagraph 形态，不渲染 A2 的长 capture_sizes 列表
    assert '"cudagraph_capture_sizes"' not in out_d


def test_a3_kvpool_keeps_multi_connector_with_1m_topology():
    """A3 保持 kvpool（mooncake.enabled=true）→ MultiConnector，且 P/D 并行度取 1M 档值。"""
    pd = _gv(GV_FILE_A3, "1m")["pd_cluster"]
    out = _render_a3("prefill", "1m", mooncake_enabled=True)
    assert '"kv_connector": "MultiConnector"' in out
    assert '"kv_connector": "AscendStoreConnector"' in out
    assert '"kv_role": "kv_producer"' in out
    assert f'"prefill": {{ "dp_size": {pd["prefill"]["dp_size"]}, "tp_size": {pd["prefill"]["tp_size"]} }}' in out
    assert f'"decode": {{ "dp_size": {pd["decode"]["dp_size"]}, "tp_size": {pd["decode"]["tp_size"]} }}' in out
    assert f'"kv_port": "{pd["prefill"]["kv_port"]}"' in out
    assert "MOONCAKE_CONFIG_PATH" in out

    out_d = _render_a3("decode", "1m", mooncake_enabled=True)
    assert '"kv_connector": "MultiConnector"' in out_d
    assert '"kv_role": "kv_consumer"' in out_d
    assert '"load_async": true' in out_d
    assert f'"kv_port": "{pd["decode"]["kv_port"]}"' in out_d


def test_a3_rendered_output_clean_newlines():
    """A3 两档渲染结果换行干净（块标签行不留空行/纯空白行）。"""
    for cfg in ("200k", "1m"):
        for role in ("prefill", "decode"):
            lines = _render_a3(role, cfg).splitlines()
            for i in range(len(lines) - 1):
                assert not (lines[i] == "" and lines[i + 1] == ""), f"{cfg}/{role} 连续空行 @{i}"
            for i, ln in enumerate(lines):
                assert not (ln and not ln.strip()), f"{cfg}/{role} 纯空白行 @{i}"
            assert "{{" not in "\n".join(lines)


def test_a3_model_path_shared_by_both_configs():
    """A3 两档共用同一份权重路径（当前为 w4a8c8，非官方参考的 w8a8c8），且已填真实路径。"""
    p200k = _gv(GV_FILE_A3, "200k")["model_path"]
    p1m = _gv(GV_FILE_A3, "1m")["model_path"]
    assert p200k == p1m
    assert p1m.startswith("/") and "TODO" not in p1m
    # 渲染时该路径必须原样出现在 vllm serve 后
    assert f"vllm serve {p1m}" in _render_a3("prefill", "1m")
    assert f"vllm serve {p1m}" in _render_a3("decode", "200k")


def test_a3_mounts_and_logs_dir_are_container_visible():
    """A3 关键约束：mounts 必须写 host:container（单路径会被 docker 当匿名卷）；
    logs.dir 必须落在 mounts 覆盖的路径下（引擎容器不单独挂 logs.dir），否则日志出不了容器。"""
    gv = _gv(GV_FILE_A3)
    mounts = gv["model"]["mounts"]
    log_dir = gv["logs"]["dir"]
    assert mounts, "model.mounts 不能为空"
    for m in mounts:
        host_src = m.split(":")[0]
        assert ":" in m, f"mount 必须写 host:container，当前: {m}"
        assert host_src.startswith("/")
        assert log_dir == host_src or log_dir.startswith(host_src.rstrip("/") + "/"), (
            f"logs.dir={log_dir} 不在 mounts 覆盖范围内（{mounts}）")
    # 模型权重也必须在 mounts 覆盖范围内，否则容器里看不到权重
    assert gv["model_path"].startswith(tuple(m.split(":")[0].rstrip("/") for m in mounts))