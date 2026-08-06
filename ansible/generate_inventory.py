#!/usr/bin/env python3
"""从 config.yaml 生成 ansible inventory。"""
import sys, yaml

def main(config_path):
    cfg = yaml.safe_load(open(config_path))
    lines = ["all:", "  children:", "    prefill:", "      hosts:"]
    for i, n in enumerate(cfg["nodes"]["prefill"]):
        lines.append(f'        p{i}: {{ ansible_host: {n["ip"]}, nic: {n["nic"]} }}')
    lines += ["    decode:", "      hosts:"]
    for i, n in enumerate(cfg["nodes"]["decode"]):
        lines.append(f'        d{i}: {{ ansible_host: {n["ip"]}, nic: {n["nic"]} }}')
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))