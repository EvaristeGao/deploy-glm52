#!/usr/bin/env python3
"""解析 config.yaml，按节点名输出 launch_online_dp.py 启动参数。

用法：python3 resolve_node.py --config config.yaml --node p0|d1
输出（stdout，KEY=VALUE，供 deploy.sh eval）：
    ROLE prefill|decode
    NODE_INDEX DP_SIZE DP_SIZE_LOCAL TP_SIZE DP_RANK_START
    DP_ADDRESS DP_RPC_PORT VLLM_START_PORT KV_PORT
    NUM_CARDS MODEL_PATH SERVED_MODEL_NAME MAX_MODEL_LEN
    ENABLE_PREFIX_CACHING CLUSTER_TYPE
"""
import argparse
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("ERROR: pyyaml not installed. pip install pyyaml\n")
    sys.exit(1)


def load(cfg_path):
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def node_info(cfg, node):
    """返回 (role, node_index, node_dict) 或抛 ValueError。"""
    node = node.lower()
    for role in ("prefill", "decode"):
        for i, nd in enumerate(cfg["nodes"][role]):
            # nd 可能是 dict 或字符串；统一取 ip
            ip = nd["ip"] if isinstance(nd, dict) else nd
            if node == f"{role[0]}{i}":
                return role, i, nd
    raise ValueError(f"未知节点名: {node}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--node", required=True)
    args = ap.parse_args()

    cfg = load(args.config)
    try:
        role, idx, nd = node_info(cfg, args.node)
    except ValueError:
        sys.stderr.write(f"ERROR: 未知节点名: {args.node}\n")
        sys.exit(2)
    pdc = cfg["pd_cluster"]
    pconf = pdc["prefill"]
    dconf = pdc["decode"]

    if role == "prefill":
        dp_size, tp_size, local = pconf["dp_size"], pconf["tp_size"], pconf["dp_size_local"]
        base_port, kv_port, rpc_port = pconf["base_port"], pconf["kv_port"], pconf["rpc_port"]
        dp_address = cfg["nodes"]["prefill"][0]["ip"]
    else:
        dp_size, tp_size, local = dconf["dp_size"], dconf["tp_size"], dconf["dp_size_local"]
        base_port, kv_port, rpc_port = dconf["base_port"], dconf["kv_port"], dconf["rpc_port"]
        dp_address = cfg["nodes"]["decode"][0]["ip"]

    ip = nd["ip"] if isinstance(nd, dict) else nd
    nic = nd.get("nic", "") if isinstance(nd, dict) else ""
    dp_rank_start = idx * local

    out = {
        "ROLE": role,
        "NODE_INDEX": idx,
        "DP_SIZE": dp_size,
        "DP_SIZE_LOCAL": local,
        "TP_SIZE": tp_size,
        "DP_RANK_START": dp_rank_start,
        "DP_ADDRESS": dp_address,
        "DP_RPC_PORT": rpc_port,
        "VLLM_START_PORT": base_port,
        "KV_PORT": kv_port,
        "NUM_CARDS": pdc.get("num_cards", 8),
        "MODEL_PATH": cfg["model"]["path"],
        "SERVED_MODEL_NAME": cfg["model"].get("served_model_name", "glm-52"),
        "MAX_MODEL_LEN": pdc.get("max_model_len", 200000),
        "ENABLE_PREFIX_CACHING": str(cfg["pd_cluster"].get("enable_prefix_caching", True)).lower(),
        "CLUSTER_TYPE": cfg["cluster"]["name"],
        "LOCAL_IP": ip,
        "NIC": nic,
    }
    for k, v in out.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()