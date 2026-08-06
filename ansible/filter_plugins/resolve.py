"""Ansible custom filter 插件：复用 deploy-glm52 的 resolve/*.py 解析逻辑。

每个 filter 通过 subprocess 调用现有 resolve 脚本（写临时 config 文件），
解析其 stdout，保证与直接跑脚本的输出完全一致。纯本地、不 ssh、不起服务。
"""
import os
import subprocess
import sys
import tempfile

import yaml

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
RESOLVE_DIR = os.path.join(PROJECT_ROOT, "resolve")

# resolve_node.py 输出中应为 int 的 KEY
_INT_KEYS = frozenset(
    {
        "NODE_INDEX",
        "DP_SIZE",
        "DP_SIZE_LOCAL",
        "TP_SIZE",
        "DP_RANK_START",
        "DP_RPC_PORT",
        "VLLM_START_PORT",
        "KV_PORT",
        "NUM_CARDS",
        "MAX_MODEL_LEN",
    }
)


def _to_plain(obj):
    """把 Ansible 包装类型（_AnsibleTaggedStr/Mapping 等）转成纯 python 类型，
    供 yaml.safe_dump 序列化到临时 config 文件。pyyaml 无法直接 dump 这些有 tag 的包装类。"""
    if isinstance(obj, dict):
        return {str(k): _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(i) for i in obj]
    if obj is None:
        return None
    # 顺序重要：bool 是 int 子类，先判 bool；str/int/float 用构造器剥掉 Ansible tag 包装
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    # 其它包装类型：尝试转原生
    try:
        from ansible.module_utils.common.text.converters import to_native

        return to_native(obj)
    except Exception:
        return str(obj)


def _run_script(script, cfg, *extra):
    """写临时 config 文件，运行 resolve 脚本，返回 stdout 行列表。"""
    cfg = _to_plain(cfg) if isinstance(cfg, dict) else {"__cfg": _to_plain(cfg)}
    fd, path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
        proc = subprocess.run(
            [sys.executable, os.path.join(RESOLVE_DIR, script), "--config", path, *extra],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{script} 失败 (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.splitlines()


def _kv_to_dict(lines, int_keys=()):
    d = {}
    for ln in lines:
        k, _, v = ln.partition("=")
        if k in int_keys:
            try:
                v = int(v)
            except ValueError:
                pass
        d[k] = v
    return d


class FilterModule:
    def filters(self):
        return {
            "resolve_node": self.resolve_node,
            "resolve_instances": self.resolve_instances,
            "resolve_router": self.resolve_router,
        }

    def resolve_node(self, cfg, node):
        """返回节点参数 dict（复用 resolve_node.py 的 KEY=VALUE 解析）。"""
        lines = _run_script("resolve_node.py", cfg, "--node", node)
        return _kv_to_dict(lines, _INT_KEYS)

    def resolve_instances(self, cfg):
        """返回 [(role, ip, port), ...]（复用 resolve_instances.py 的 'role ip port' 输出）。"""
        lines = _run_script("resolve_instances.py", cfg)
        out = []
        for ln in lines:
            role, ip, port = ln.split()
            out.append((role, ip, int(port)))
        return out

    def resolve_router(self, cfg):
        """返回代理端点 dict（复用 resolve_router.py 的 KEY=VALUE 输出）。"""
        lines = _run_script("resolve_router.py", cfg)
        d = _kv_to_dict(lines, {"PROXY_PORT"})
        for k in ("PREFILLER_HOSTS", "PREFILLER_PORTS", "DECODER_HOSTS", "DECODER_PORTS"):
            # 脚本输出带引号的多值字段，剥掉引号再按空白拆分
            raw = d[k].strip().strip('"').strip("'")
            d[k] = raw.split() if raw else []
        return d


if __name__ == "__main__":
    # 手动调试入口：uv run python ansible/filter_plugins/resolve.py <node>
    cfg = yaml.safe_load(open(os.path.join(PROJECT_ROOT, "config.yaml")))
    node = sys.argv[1] if len(sys.argv) > 1 else "p0"
    for k, v in FilterModule().resolve_node(cfg, node).items():
        print(f"{k}={v}")
