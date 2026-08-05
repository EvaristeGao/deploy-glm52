#!/usr/bin/env python3
"""解析 config.yaml，输出负载均衡代理的 prefiller/decoder 端点列表。

用法：python3 resolve_router.py --config config.yaml
输出：PROXY_HOST PROXY_PORT PREFILLER_HOSTS PREFILLER_PORTS DECODER_HOSTS DECODER_PORTS
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

    pconf = cfg["pd_cluster"]["prefill"]
    dconf = cfg["pd_cluster"]["decode"]

    def expand(role_cfg, role_nodes):
        hosts, ports = [], []
        for i, nd in enumerate(role_nodes):
            ip = nd["ip"] if isinstance(nd, dict) else nd
            for r in range(role_cfg["dp_size_local"]):
                hosts.append(ip)
                ports.append(str(role_cfg["base_port"] + r))
        return hosts, ports

    pre_hosts, pre_ports = expand(pconf, cfg["nodes"]["prefill"])
    dec_hosts, dec_ports = expand(dconf, cfg["nodes"]["decode"])

    # proxy 节点名 → IP
    pnode = cfg["proxy"]["node"].lower()
    pidx = int(pnode[1:])
    p_ip = cfg["nodes"]["prefill"][pidx] if pnode.startswith("p") else cfg["nodes"]["decode"][pidx]
    proxy_ip = p_ip["ip"] if isinstance(p_ip, dict) else p_ip

    print(f"PROXY_HOST={proxy_ip}")
    print(f"PROXY_PORT={cfg['proxy']['port']}")
    print(f"PREFILLER_HOSTS=\"{' '.join(pre_hosts)}\"")
    print(f"PREFILLER_PORTS=\"{' '.join(pre_ports)}\"")
    print(f"DECODER_HOSTS=\"{' '.join(dec_hosts)}\"")
    print(f"DECODER_PORTS=\"{' '.join(dec_ports)}\"")


if __name__ == "__main__":
    main()