#!/usr/bin/env python3
"""渲染 mooncake.json（KV pool 配置）。

用法：python3 render_mooncake.py --config config.yaml [--output mooncake.json]
master 地址 = mooncake.port + prefill 首节点 IP。
"""
import argparse
import json
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
    ap.add_argument("--output", default="mooncake.json")
    args = ap.parse_args()
    cfg = load(args.config)

    master_ip = cfg["nodes"]["prefill"][0]["ip"]
    mc = cfg["mooncake"]
    mc_cfg = mc.get("config", {})
    doc = {
        "metadata_server": "P2PHANDSHAKE",
        "protocol": "ascend",
        "device_name": "",
        "master_server_address": f"{master_ip}:{mc['port']}",
        "global_segment_size": mc_cfg.get("global_segment_size", "80GB"),
        "default_kv_lease_ttl": mc_cfg.get("default_kv_lease_ttl", 11000),
    }
    with open(args.output, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"rendered {args.output} (master={master_ip}:{mc['port']})")


if __name__ == "__main__":
    main()
