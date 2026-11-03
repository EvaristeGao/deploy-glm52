#!/usr/bin/env python3
"""Operate only captured baseline IDs and separately labelled development containers."""

import argparse
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts/p-main-buffer-reuse"
NODES = {"p0": "ott9-a31", "p1": "ott9-a32", "d0": "ott9-a33", "d1": "ott9-a34"}
RUN = os.environ.get("REUSE_RUN", "r03")
if re.fullmatch(r"r[0-9]+", RUN) is None:
    raise ValueError("REUSE_RUN must have the form r01, r02, ...")
NAME = f"vllm-ascend-gwh-glm52-reuse-{RUN}"
OWNER = f"main-reuse-20261103-{RUN}"
IMAGE = f"glm52-vllm-ascend:0.30.0-main-reuse-dev-20261103-{RUN}"


def remote(node, args, data=None):
    result = subprocess.run(
        ["ssh", f"root@{NODES[node]}", shlex.join(args)],
        check=False,
        input=data,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"{node}: {args}: {(result.stdout + result.stderr).decode()}"
        )
    return result.stdout


def inspect(node, name):
    return json.loads(remote(node, ["docker", "inspect", name]))[0]


def baseline(node):
    saved = json.loads((ART / "baseline-runtime" / node / "container.json").read_text())
    actual = inspect(node, saved["Name"])
    if actual["Id"] != saved["Id"]:
        raise RuntimeError(f"{node}: baseline ID changed; refusing mutation")
    return saved


def owned(node):
    actual = inspect(node, NAME)
    if actual["Config"]["Labels"].get("reuse.owner") != OWNER:
        raise RuntimeError(f"{node}: development ownership mismatch")
    return actual


def stop_owned(node):
    owned(node)
    try:
        remote(node, ["docker", "stop", "--time", "30", NAME])
    except RuntimeError:
        # Docker may report no exit event just before the container exits.
        # Accept only an inspected terminal state; never escalate to host kill.
        state = owned(node)["State"]
        if state["Running"] or state["Status"] != "exited":
            raise
        print(f"{node}: verified delayed development-container exit", flush=True)


def create(node):
    saved = baseline(node)
    cfg = saved["HostConfig"]
    args = [
        "docker",
        "create",
        "-it",
        "--name",
        NAME,
        "--label",
        f"reuse.owner={OWNER}",
        "--network",
        cfg["NetworkMode"],
        "--ipc",
        cfg["IpcMode"],
        "--shm-size",
        str(cfg["ShmSize"]),
    ]
    if cfg["Privileged"]:
        args.append("--privileged")
    if cfg["PidMode"]:
        args.extend(["--pid", cfg["PidMode"]])
    for bind in cfg["Binds"]:
        if bind.split(":")[1] != "/root/ascend/log":
            args.extend(["--volume", bind])
    for device in cfg["Devices"]:
        args.extend(
            [
                "--device",
                f"{device['PathOnHost']}:{device['PathInContainer']}:{device['CgroupPermissions']}",
            ]
        )
    for value in cfg["SecurityOpt"] or []:
        args.extend(["--security-opt", value])
    args.extend([IMAGE, "bash"])
    remote(node, args)
    owned(node)
    remote(node, ["docker", "start", NAME])
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        files = {
            "run_dp_template.sh": ART
            / f"generated-{RUN}"
            / f"run_dp_template_{node}.sh",
            "mooncake.json": ART / f"generated-{RUN}/mooncake.json",
            "launch_online_dp.py": ART
            / "baseline-runtime"
            / node
            / "launch_online_dp.py",
        }
        for name, path in files.items():
            archive.add(path, arcname=name)
    remote(
        node,
        [
            "docker",
            "exec",
            NAME,
            "mkdir",
            "-p",
            "/root/pd",
            "/root/reuse-main-20261103/logs",
        ],
    )
    remote(
        node,
        ["docker", "exec", "-i", NAME, "tar", "-xf", "-", "-C", "/root/pd"],
        payload.getvalue(),
    )
    print(f"{node}: development container prepared", flush=True)


def stop_baseline(node):
    saved = baseline(node)
    owned(node)
    remote(node, ["docker", "stop", "--time", "30", saved["Id"]])
    print(f"{node}: captured baseline stopped and preserved", flush=True)


def launch(node, restore=False):
    other_development = set(
        remote(
            node,
            ["docker", "ps", "--filter", "label=reuse.owner", "--format", "{{.Names}}"],
        )
        .decode()
        .split()
    ) - {NAME}
    if other_development:
        raise RuntimeError(
            f"{node}: other development containers are running: {other_development}"
        )
    if restore:
        saved = baseline(node)
        owned(node)
        stop_owned(node)
        remote(node, ["docker", "start", saved["Id"]])
        container = saved["Name"]
        logfile = f"/mnt/sfs_turbo/g00832294/logs/vllm_{node}.log"
    else:
        owned(node)
        status_file = ART / f"image-{RUN}/STATUS.txt"
        if status_file.exists():
            raise RuntimeError(
                f"Refusing to launch a retired experiment image: {status_file}"
            )
        if inspect(node, baseline(node)["Name"])["State"]["Running"]:
            raise RuntimeError(
                f"{node}: baseline is running; refusing overlapping engines"
            )
        container = NAME
        logfile = f"/root/reuse-main-20261103/logs/vllm_{node}.log"
    prefill = node.startswith("p")
    args = [
        "python3",
        "launch_online_dp.py",
        "--dp-size",
        "2",
        "--tp-size",
        "16",
        "--dp-size-local",
        "1",
        "--dp-rank-start",
        node[1],
        "--dp-address",
        "172.168.4.237" if prefill else "172.168.4.230",
        "--dp-rpc-port",
        "16591" if prefill else "16600",
        "--vllm-start-port",
        "9081" if prefill else "9900",
    ]
    command = f"cd /root/pd && exec {shlex.join(args)} >> {shlex.quote(logfile)} 2>&1"
    remote(node, ["docker", "exec", "-d", container, "bash", "-lc", command])
    print(
        f"{node}: {'baseline' if restore else 'development'} engine launched",
        flush=True,
    )


def proxy(restore=False):
    baseline("p0")
    owned("p0")
    container = "vllm-ascend-gwh-glm52" if restore else NAME
    script = "/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_layerwise_server_example.py"
    logfile = (
        "/mnt/sfs_turbo/g00832294/logs/proxy.log"
        if restore
        else "/root/reuse-main-20261103/logs/proxy.log"
    )
    args = [
        "python3",
        script,
        "--host",
        "172.168.4.237",
        "--port",
        "8000",
        "--prefiller-hosts",
        "172.168.4.237",
        "172.168.4.31",
        "--prefiller-ports",
        "9081",
        "9081",
        "--decoder-hosts",
        "172.168.4.230",
        "172.168.4.226",
        "--decoder-ports",
        "9900",
        "9900",
    ]
    remote(
        "p0",
        [
            "docker",
            "exec",
            "-d",
            container,
            "bash",
            "-lc",
            f"exec {shlex.join(args)} >> {shlex.quote(logfile)} 2>&1",
        ],
    )


def collect(node):
    owned(node)
    data = remote(
        node,
        [
            "docker",
            "exec",
            NAME,
            "tail",
            "-c",
            "4194304",
            f"/root/reuse-main-20261103/logs/vllm_{node}.log",
        ],
    )
    (ART / f"runtime-{node}-{RUN}.log").write_bytes(data)
    lines = data.decode(errors="replace").splitlines()
    print(f"{node}: {len(lines)} lines; tail:", flush=True)
    print("\n".join(lines[-5:]), flush=True)


def verify_image(node):
    owned(node)
    code = (
        "import json,hashlib; from pathlib import Path; "
        "manifest=json.loads(Path('/root/reuse-source-manifest.json').read_text()); "
        "site=Path('/usr/local/python3.12.13/lib/python3.12/site-packages'); "
        "print(json.dumps({name:hashlib.sha256((site/name).read_bytes()).hexdigest() for name in manifest}))"
    )
    actual = json.loads(remote(node, ["docker", "exec", NAME, "python3", "-c", code]))
    expected = json.loads((ART / f"image-{RUN}/manifest.json").read_text())
    if actual != expected:
        raise RuntimeError(f"{node}: production-source manifest mismatch")
    print(
        f"{node}: {len(actual)} production files match the image manifest", flush=True
    )


def restart_development(node, template_dir=None, patch_source=False):
    if node not in NODES:
        raise ValueError("Unknown development node")
    owned(node)
    if inspect(node, baseline(node)["Name"])["State"]["Running"]:
        raise RuntimeError("Baseline is running; refusing development restart")
    stop_owned(node)
    remote(node, ["docker", "start", NAME])
    for filename in ("pool_scheduler.py", "pool_worker.py") if patch_source else ():
        relative = (
            "vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/" + filename
        )
        target = "/usr/local/python3.12.13/lib/python3.12/site-packages/" + relative
        remote(
            node,
            ["docker", "exec", "-i", NAME, "tee", target],
            (ROOT / "src/vllm-ascend-reuse" / relative).read_bytes(),
        )
        remote(node, ["docker", "exec", NAME, "python3", "-m", "py_compile", target])
    if template_dir is not None and node.startswith("p"):
        script = template_dir / f"run_dp_template_{node}.sh"
        remote(
            node,
            ["docker", "exec", "-i", NAME, "tee", "/root/pd/run_dp_template.sh"],
            script.read_bytes(),
        )
    launch(node)


def assert_development_cluster():
    for node in NODES:
        if inspect(node, baseline(node)["Name"])["State"]["Running"]:
            raise RuntimeError(f"{node}: refusing probes against a running baseline")
        if not owned(node)["State"]["Running"]:
            raise RuntimeError(f"{node}: development container is stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "create",
            "stop-baseline",
            "launch",
            "restore",
            "proxy",
            "collect",
            "restart-prefill",
            "restart-all",
            "stop-development",
            "verify-image",
        ],
    )
    parser.add_argument("--prefill-template-dir", type=Path)
    parser.add_argument(
        "--patch-source",
        action="store_true",
        help="Explicitly overlay local Python files for debugging",
    )
    args = parser.parse_args()
    if args.patch_source and args.action not in ("restart-prefill", "restart-all"):
        parser.error("Source overlays are limited to explicit development restarts")
    if args.prefill_template_dir is not None:
        if args.action not in ("restart-prefill", "restart-all"):
            parser.error("Template replacement is limited to development restarts")
        template_dir = args.prefill_template_dir.resolve()
        if not template_dir.is_relative_to(ART.resolve()):
            parser.error("Experiment templates must be inside repository artifacts")
        for node in ("p0", "p1"):
            subprocess.run(
                ["bash", "-n", str(template_dir / f"run_dp_template_{node}.sh")],
                check=True,
            )
        args.prefill_template_dir = template_dir
    if args.action == "proxy":
        proxy()
        return
    actions = {
        "create": create,
        "stop-baseline": stop_baseline,
        "stop-development": stop_owned,
        "launch": launch,
        "restore": lambda node: launch(node, restore=True),
        "collect": collect,
        "verify-image": verify_image,
        "restart-prefill": lambda node: restart_development(
            node, args.prefill_template_dir, args.patch_source
        ),
        "restart-all": lambda node: restart_development(
            node, args.prefill_template_dir, args.patch_source
        ),
    }
    # Check all identities before any cluster transition.
    for node in NODES:
        baseline(node)
        if args.action != "create":
            owned(node)
    nodes = ("p0", "p1") if args.action == "restart-prefill" else NODES
    with ThreadPoolExecutor(max_workers=4) as executor:
        for result in executor.map(actions[args.action], nodes):
            pass
    if args.action == "restore":
        proxy(restore=True)
    elif args.action in ("restart-prefill", "restart-all"):
        proxy()


if __name__ == "__main__":
    main()
