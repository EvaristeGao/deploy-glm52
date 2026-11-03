#!/usr/bin/env python3
"""Verify middle/end retrieval by explicitly replaying a completed P archive."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from reuse_container_experiment import ART, RUN, remote, verify_image
from reuse_long_context_probe import read_archive
from reuse_prefill_capacity_run import completed_probe, p_state


def require_held_controller(record):
    process = Path("/proc") / str(record["pid"])
    args = (process / "cmdline").read_bytes().decode().split("\0")[:-1]
    state = next(
        line
        for line in (process / "status").read_text().splitlines()
        if line.startswith("State:")
    )
    if args != record["args"] or not state.split()[1].startswith("T"):
        raise RuntimeError(
            "Original controller must remain held; refusing competing schedules"
        )


def metrics_snapshot(path):
    code = """import json,urllib.request
out={}
for node,host,port in (("p0","172.168.4.237",9081),("p1","172.168.4.31",9081),("d0","172.168.4.230",9900),("d1","172.168.4.226",9900)):
 try:
  with urllib.request.urlopen("http://%s:%s/metrics"%(host,port),timeout=5) as r:
   out[node]=[l for l in r.read().decode().splitlines() if l.startswith("vllm:") and any(k in l for k in ("prefix_cache","num_requests_running","num_requests_waiting","request_success_total","prompt_tokens_total","kv_cache_usage"))]
 except Exception as e:out[node]={"error":repr(e)}
print(json.dumps(out))
"""
    path.write_bytes(remote("p0", ["python3", "-c", code]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--held-controller", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if RUN != "r05":
        parser.error("Only the owned r05 experiment is supported")
    for path in (args.archive, args.held_controller, args.output_dir):
        if ART.resolve() not in path.resolve().parents:
            parser.error("All inputs and outputs must be inside repository artifacts")
    record = json.loads(args.held_controller.read_text())
    require_held_controller(record)
    args.output_dir.mkdir(exist_ok=False)
    while completed_probe(args.archive, 1048544) is None:
        require_held_controller(record)
        print(
            "Waiting for the existing full 1M prefill; no additional inference",
            flush=True,
        )
        time.sleep(30)
    archive = read_archive(args.archive, 1048544)
    if archive["endpoint"] != "http://172.168.4.237:9081":
        raise RuntimeError("Archive must be from the selected P0 endpoint")
    for attempt in range(12):
        state = p_state()
        if all(
            value["ready"]
            and len(value["gauges"]) == 2
            and all(g == 0 for g in value["gauges"])
            for value in state.values()
        ):
            break
        print(json.dumps({"waiting_for_idle": state}), flush=True)
        time.sleep(5)
    else:
        raise RuntimeError("P did not become healthy and idle; no replay submitted")
    for node in ("p0", "p1"):
        verify_image(node)
    metrics_snapshot(args.output_dir / "before.json")
    for label in ("BETA", "GAMMA"):
        require_held_controller(record)
        path = args.output_dir / f"{label.lower()}-1048544.log"
        with path.open("x") as output:
            subprocess.run(
                [
                    sys.executable,
                    "tools/reuse_long_context_probe.py",
                    "--target",
                    "p0",
                    "--prefill-only",
                    "--tokens",
                    "1048544",
                    "--query-label",
                    label,
                    "--reuse-prefix-from",
                    str(args.archive),
                    "--timeout",
                    "43200",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                check=True,
            )
        metrics_snapshot(args.output_dir / f"after-{label.lower()}.json")
        print(f"Passed prefix replay {label}; log={path}", flush=True)
    print(
        "All retrieval stages complete; final audit and held-controller cleanup remain",
        flush=True,
    )


if __name__ == "__main__":
    main()
