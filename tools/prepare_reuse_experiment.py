#!/usr/bin/env python3
"""Prepare a reviewable overlay image and Ansible profile; no remote mutations."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/p-main-buffer-reuse"
SOURCE = ROOT / "src/vllm-ascend-reuse"
TAG_PREFIX = "glm52-vllm-ascend:0.30.0-main-reuse-dev-20261103"
MODEL = "/root/reuse-main-20261103-model"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-only", action="store_true")
    parser.add_argument("--name", default="r01")
    parser.add_argument("--slots", type=int, default=3)
    parser.add_argument("--blocks", type=int, default=5508)
    parser.add_argument("--automatic-blocks", action="store_true")
    parser.add_argument("--batch-tokens", type=int, default=4096)
    args = parser.parse_args()
    if (
        not args.name.replace("-", "").isalnum()
        or min(args.slots, args.blocks, args.batch_tokens) < 1
    ):
        parser.error("Use an alphanumeric run name and positive numeric settings")
    variables = yaml.safe_load(
        (ROOT / "ansible/inventories/a3/group_vars/all.yml").read_text()
    )
    cluster = variables["_1m_v030_lw"]["pd_cluster"]
    if args.batch_tokens % cluster["prefill"]["tp_size"]:
        parser.error(
            "The current FlashComm1 profile requires a token budget divisible by Prefill TP size"
        )
    for role in ("prefill", "decode"):
        cluster[role].update(
            enable_mtp=False, num_gpu_blocks_override=5508, max_num_seqs=4
        )
    cluster["prefill"].update(
        kv_load_failure_policy="fail",
        num_gpu_blocks_override=args.blocks,
        max_num_batched_tokens=args.batch_tokens,
    )
    if args.automatic_blocks:
        cluster["prefill"].pop("num_gpu_blocks_override")
    cluster["layerwise"]["num_shared_buffers"] = args.slots
    profile = {
        "reuse_render_only": True,
        "pd_cluster": cluster,
        "model_path": MODEL,
        "gen_dir": str(ARTIFACTS / f"generated-{args.name}"),
    }
    profile_path = ARTIFACTS / f"profile-{args.name}.yaml"
    if profile_path.exists():
        parser.error(
            f"Refusing to overwrite existing experiment evidence: {profile_path}"
        )
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    if args.profile_only:
        print(profile_path)
        return
    context = ARTIFACTS / f"image-{args.name}"
    context.mkdir(exist_ok=True)
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "4234a1f18", "--", "vllm_ascend"],
        cwd=SOURCE,
        text=True,
    ).splitlines()
    manifest = {}
    for name in changed:
        if not name.endswith(".py"):
            raise RuntimeError(f"Unexpected non-Python production change: {name}")
        target = context / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, target)
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    shutil.copy2(
        ROOT / "tools/reuse_store_sentinel.py", context / "reuse_store_sentinel.py"
    )
    site = "/usr/local/python3.12.13/lib/python3.12/site-packages/vllm_ascend"
    (context / "Dockerfile").write_text(
        "FROM glm52-vllm-ascend:0.30.0-dsabase-m1\n"
        f"COPY vllm_ascend/ {site}/\n"
        "COPY reuse_store_sentinel.py /root/reuse_store_sentinel.py\n"
        "COPY manifest.json /root/reuse-source-manifest.json\n"
        f"RUN python3 -m compileall -q {site} /root/reuse_store_sentinel.py "
        f"&& ln -s /mnt/sfs_turbo/models/GLM-5.2-w4a8c8 {MODEL}\n"
    )
    (context / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "tag": f"{TAG_PREFIX}-{args.name}",
                "files": len(manifest),
                "context": str(context),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
