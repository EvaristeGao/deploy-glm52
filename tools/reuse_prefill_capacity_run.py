#!/usr/bin/env python3
"""Advance a P-only capacity experiment after a verified completed request."""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from reuse_container_experiment import (
    ART,
    NAME,
    RUN,
    owned,
    remote,
    restart_development,
    verify_image,
)


def completed_probe(path, target):
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("target") != target:
            continue
        if event.get("event") == "error":
            raise RuntimeError(f"Previous request failed: {event}")
        if event.get("event") == "complete":
            usage = event.get("usage") or {}
            expected = event.get("expected_digit")
            if not (
                event.get("retrieval_pass") is True
                and isinstance(expected, str)
                and event["message"]["content"].strip() == expected
                and usage.get("completion_tokens") == 1
                and abs(usage.get("prompt_tokens", 0) - target) <= 16
            ):
                raise RuntimeError(f"Previous request did not pass: {event}")
            return event
    return None


def validate_templates(directory):
    result = {}
    for node in ("p0", "p1"):
        path = directory / f"run_dp_template_{node}.sh"
        data = path.read_text()
        args = shlex.split(data.replace("\\\n", ""), comments=True)
        transfer = json.loads(args[args.index("--kv-transfer-config") + 1])
        connectors = transfer["kv_connector_extra_config"]["connectors"]
        if [c["kv_connector"] for c in connectors] != [
            "MooncakeLayerwiseConnector",
            "AscendStoreConnector",
        ]:
            raise RuntimeError("Candidate must retain the Mooncake connectors")
        store = connectors[1]["kv_connector_extra_config"]
        if (
            store.get("backend") != "mooncake"
            or store.get("layerwise_num_shared_buffers") != 2
        ):
            raise RuntimeError("Candidate must retain Mooncake Main B=2")
        for flag, expected in (
            ("--max-model-len", "1048576"),
            ("--max-num-seqs", "1"),
            ("--kv-cache-dtype", "int8"),
            ("--max-num-batched-tokens", "32784"),
        ):
            if args[args.index(flag) + 1] != expected:
                raise RuntimeError(f"Unexpected candidate {flag}")
        additional = json.loads(args[args.index("--additional-config") + 1])
        if (
            additional.get("mega_moe_max_tokens") != 524544
            or "--enforce-eager" not in args
        ):
            raise RuntimeError(
                "Candidate must retain reviewed MoE budget and eager execution"
            )
        result[node] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "batch_tokens": int(args[args.index("--max-num-batched-tokens") + 1]),
        }
    return result


def p_state():
    code = """import urllib.request, json
out={}
for node,host in (("p0","172.168.4.237"),("p1","172.168.4.31")):
 try:
  with urllib.request.urlopen("http://"+host+":9081/metrics",timeout=5) as r:
   gauges=[float(l.rsplit(" ",1)[1]) for l in r.read().decode().splitlines()
    if l.startswith(("vllm:num_requests_running{","vllm:num_requests_waiting{"))]
  out[node]={"ready":True,"gauges":gauges}
 except Exception as e:out[node]={"ready":False,"error":repr(e)}
print(json.dumps(out))
"""
    return json.loads(remote("p0", ["python3", "-c", code]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-log", type=Path, required=True)
    parser.add_argument("--after-target", type=int, default=262144)
    parser.add_argument("--template-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--observe-running",
        action="store_true",
        help="Observe already restarted P containers with matching script SHA; do not restart",
    )
    args = parser.parse_args()
    if RUN != "r05":
        parser.error("This experiment is restricted to REUSE_RUN=r05")
    for path in (args.after_log, args.template_dir, args.output_dir):
        if ART.resolve() not in path.resolve().parents:
            parser.error("All experiment paths must be inside repository artifacts")
    templates = validate_templates(args.template_dir)
    args.output_dir.mkdir(exist_ok=False)
    (args.output_dir / "candidate.json").write_text(
        json.dumps(templates, indent=2) + "\n"
    )
    for node in ("p0", "p1"):
        owned(node)
        verify_image(node)
    while completed_probe(args.after_log, args.after_target) is None:
        print("Waiting for the existing request; no mutation", flush=True)
        time.sleep(30)
    if args.observe_running:
        for node in ("p0", "p1"):
            digest = (
                remote(
                    node,
                    [
                        "docker",
                        "exec",
                        NAME,
                        "sha256sum",
                        "/root/pd/run_dp_template.sh",
                    ],
                )
                .decode()
                .split()[0]
            )
            if digest != templates[node]["sha256"]:
                raise RuntimeError("Running P template differs from reviewed candidate")
        print("Observing SHA-matched running containers; no restart", flush=True)
    else:
        state = p_state()
        (args.output_dir / "before-switch.json").write_text(
            json.dumps(state, indent=2) + "\n"
        )
        if not all(
            value["ready"]
            and len(value["gauges"]) == 2
            and all(g == 0 for g in value["gauges"])
            for value in state.values()
        ):
            raise RuntimeError(f"P must be healthy and idle before switching: {state}")
        if validate_templates(args.template_dir) != templates:
            raise RuntimeError(
                "Templates changed while waiting; refusing an unreviewed switch"
            )
        for node in ("p0", "p1"):
            restart_development(node, template_dir=args.template_dir)
    for attempt in range(40):
        state = p_state()
        print(json.dumps({"startup_attempt": attempt, "state": state}), flush=True)
        if all(value["ready"] for value in state.values()):
            break
        time.sleep(30)
    else:
        raise RuntimeError(
            "Startup observation expired; inspect engines before retrying"
        )
    for node in ("p0", "p1"):
        verify_image(node)
    for label, targets in (
        ("ALPHA", [8192, 65536, 131072, 262144, 524288, 1048544]),
        ("BETA", [1048544]),
        ("GAMMA", [1048544]),
    ):
        for target in targets:
            path = args.output_dir / f"{label.lower()}-{target}.log"
            with path.open("x") as output:
                subprocess.run(
                    [
                        sys.executable,
                        "tools/reuse_long_context_probe.py",
                        "--target",
                        "p0",
                        "--prefill-only",
                        "--tokens",
                        str(target),
                        "--query-label",
                        label,
                        "--timeout",
                        "43200",
                    ],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    check=True,
                )
            print(f"Passed {label} target={target}; log={path}", flush=True)


if __name__ == "__main__":
    main()
