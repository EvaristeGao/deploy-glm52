#!/usr/bin/env python3
"""Manage the named development container and its captured predecessor."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True)


def inspect(name):
    return json.loads(docker("inspect", name))[0]


def stop_container(container_id):
    subprocess.run(["docker", "stop", "--time", "30", container_id], check=False)
    deadline = time.monotonic() + 120
    while inspect(container_id)["State"]["Running"]:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Container remains running after stop: {container_id}")
        time.sleep(2)


def check_npu(allowed_container=None):
    allowed = set()
    if allowed_container:
        allowed = set(docker("top", allowed_container, "-eo", "pid").split()[1:])
    info = subprocess.check_output(["npu-smi", "info"], text=True)
    active = set(re.findall(r"^\|\s*\d+\s+\d+\s*\|\s*(\d+)\s*\|", info, re.MULTILINE))
    if active - allowed:
        raise RuntimeError(
            f"NPU processes outside the captured deployment: {sorted(active - allowed)}"
        )
    print(f"NPU ownership checked, {len(active)} active processes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "cutover",
            "check-free",
            "stop",
            "restore-master",
            "restore-engine",
            "restore-proxy",
        ],
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    if args.action == "check-free":
        check_npu()
        return
    if args.action == "prepare":
        previous = inspect(args.previous)
        snapshot = directory / "previous-container.json"
        if snapshot.exists():
            saved = json.loads(snapshot.read_text())
            if saved["Id"] != previous["Id"]:
                raise RuntimeError("The captured predecessor ID changed")
        else:
            if previous["State"]["Running"]:
                (directory / "previous-processes.txt").write_text(
                    docker("top", args.previous, "-eo", "pid,ppid,args")
                )
                commands = {}
                for line in docker(
                    "top", args.previous, "-eo", "pid,args"
                ).splitlines()[1:]:
                    pid, command = line.strip().split(None, 1)
                    if command.startswith(
                        (
                            "bash -c cd /root/pd && python3 launch_online_dp.py",
                            "bash /root/pd/start_engines.sh",
                        )
                    ):
                        commands.setdefault(
                            "engine",
                            Path(f"/proc/{pid}/cmdline")
                            .read_bytes()
                            .rstrip(b"\0")
                            .decode()
                            .split("\0"),
                        )
                    if (
                        command.startswith("python3 ")
                        and "load_balance_proxy_layerwise_server_example.py" in command
                    ):
                        commands.setdefault(
                            "proxy",
                            Path(f"/proc/{pid}/cmdline")
                            .read_bytes()
                            .rstrip(b"\0")
                            .decode()
                            .split("\0"),
                        )
                    if command.split(None, 1)[0].endswith("/mooncake_master"):
                        commands.setdefault(
                            "master", ["bash", "/root/pd/start_master.sh"]
                        )
                if "engine" not in commands:
                    raise RuntimeError(
                        "Could not capture the predecessor's launch command"
                    )
                (directory / "previous-commands.json").write_text(
                    json.dumps(commands, indent=2) + "\n"
                )
            snapshot.write_text(json.dumps(previous, indent=2) + "\n")
        existing = docker("ps", "-aq", "--filter", f"name=^/{args.name}$").strip()
        if existing:
            current = inspect(args.name)
            if current["Config"]["Labels"].get("glm52.deployment") != "layerwise-guide":
                raise RuntimeError("Development container ownership mismatch")
            if (
                current["Image"]
                != json.loads(docker("image", "inspect", args.image))[0]["Id"]
            ):
                raise RuntimeError("Existing container uses a different image")
            if not current["State"]["Running"]:
                docker("start", args.name)
            return
        command = [
            "create",
            "-it",
            "--name",
            args.name,
            "--label",
            "glm52.deployment=layerwise-guide",
            "--network",
            "host",
            "--ipc",
            "private",
            "--shm-size",
            "500g",
            "--ulimit",
            "memlock=-1:-1",
            "--privileged",
            "--volume",
            f"{directory}:/root/pd",
        ]
        for mount in previous["Mounts"]:
            if mount["Type"] == "bind" and mount["Destination"] not in (
                "/root/ascend/log",
                "/root/pd",
            ):
                command.extend(
                    ["--volume", f"{mount['Source']}:{mount['Destination']}"]
                )
        for device in previous["HostConfig"]["Devices"] or []:
            command.extend(
                [
                    "--device",
                    f"{device['PathOnHost']}:{device['PathInContainer']}:{device['CgroupPermissions']}",
                ]
            )
        command.extend([args.image, "bash"])
        print(docker(*command).strip())
        docker("start", args.name)
    elif args.action == "cutover":
        saved = json.loads((directory / "previous-container.json").read_text())
        previous = inspect(args.previous)
        if previous["Id"] != saved["Id"]:
            raise RuntimeError("Predecessor ID changed before cutover")
        if previous["State"]["Running"]:
            check_npu(args.previous)
            stop_container(previous["Id"])
        check_npu()
    elif args.action.startswith("restore-"):
        saved = json.loads((directory / "previous-container.json").read_text())
        previous = inspect(args.previous)
        if previous["Id"] != saved["Id"]:
            raise RuntimeError("Predecessor ID changed before restoration")
        commands = json.loads((directory / "previous-commands.json").read_text())
        component = args.action.removeprefix("restore-")
        if component == "master" and component not in commands:
            print("Captured deployment uses an independently managed Master")
            return
        if previous["State"]["Running"]:
            # Docker requires a PID column even for command-only matching.
            processes = docker("top", previous["Id"], "-eo", "pid,args")
            if component == "engine":
                marker = (
                    "start_engines.sh"
                    if "start_engines.sh" in " ".join(commands[component])
                    else "launch_online_dp.py"
                )
            elif component == "proxy":
                marker = "load_balance_proxy_layerwise_server_example.py"
            else:
                marker = "mooncake_master"
            if marker in processes:
                print(f"Captured {component} is already running")
                return
        elif component == "engine":
            check_npu()
        if not previous["State"]["Running"]:
            docker("start", previous["Id"])
        docker("exec", "-d", previous["Id"], *commands[component])
    else:
        current = inspect(args.name)
        if current["Config"]["Labels"].get("glm52.deployment") != "layerwise-guide":
            raise RuntimeError("Development container ownership mismatch")
        if current["State"]["Running"]:
            stop_container(current["Id"])


if __name__ == "__main__":
    main()
