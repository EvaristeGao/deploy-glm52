#!/usr/bin/env python3
"""解析 config.yaml，输出所有 vLLM 实例与 proxy 的探测目标。

用法：python3 resolve_instances.py --config config.yaml
输出（每行）：role ip port
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    cfg = load(args.config)

    for role in ("prefill", "decode"):
        rc = cfg["pd_cluster"][role]
        for nd in cfg["nodes"][role]:
            ip = nd["ip"] if isinstance(nd, dict) else nd
            for r in range(rc["dp_size_local"]):
                print(f"{role} {ip} {rc['base_port'] + r}")

    pnode = cfg["proxy"]["node"].lower()
    pidx = int(pnode[1:])
    p_role_nodes = cfg["nodes"]["prefill"] if pnode.startswith("p") else cfg["nodes"]["decode"]
    p_ip = p_role_nodes[pidx]
    proxy_ip = p_ip["ip"] if isinstance(p_ip, dict) else p_ip
    print(f"proxy {proxy_ip} {cfg['proxy']['port']}")


if __name__ == "__main__":
    main()