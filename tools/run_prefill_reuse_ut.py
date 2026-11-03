#!/usr/bin/env python3
"""Run the isolated reuse worktree's CPU tests with repository-local artifacts."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import importlib
import os
import sys
import types
import unittest
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "artifacts" / "p-main-buffer-reuse"
    venv = artifacts / "imported-tmp" / "glm52-reuse-ut"
    os.environ["TMPDIR"] = str(artifacts / "tmp")
    os.environ["UV_CACHE_DIR"] = str(artifacts / "uv-cache")
    os.environ["XDG_CACHE_HOME"] = str(artifacts / "cache")
    if Path(sys.prefix) != venv:
        if not (venv / "bin" / "python").exists():
            raise SystemExit(f"Test environment is missing: {venv}")
        task_env = os.environ.copy()
        os.execve(
            str(venv / "bin" / "python"),
            [str(venv / "bin" / "python"), __file__, *sys.argv[1:]],
            task_env,
        )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true", help="Include the full pool worker module"
    )
    parser.add_argument(
        "--pytest",
        action="store_true",
        help="Run explicit test paths with pytest fixtures",
    )
    parser.add_argument(
        "tests", nargs="*", help="Optional unittest module/class/method names"
    )
    args = parser.parse_args()
    worktree = root / "src" / "vllm-ascend-reuse"
    sys.path.insert(0, str(worktree))
    # Source checkouts lack the generated build info. Use the same CPU-test
    # hardware declaration as upstream tests/ut/conftest.py.
    build = types.ModuleType("vllm_ascend._build_info")
    build.__device_type__ = "A2"
    sys.modules.setdefault(build.__name__, build)
    importlib.import_module("tests.ut.distributed.ascend_store._mock_deps")
    prefix = "tests.ut.distributed.ascend_store."
    tests = args.tests or [
        prefix
        + "test_pool_worker.TestKVPoolWorkerProcessLayerData.test_block_key_layerwise_accepts_external_slot_release_waiter",
        prefix
        + "test_pool_worker.TestKVPoolWorkerProcessLayerData.test_set_external_slot_release_waiter_updates_running_recv_thread",
        prefix
        + "test_pool_worker.TestKVPoolWorkerProcessLayerData.test_set_external_slot_release_waiter_gated_on_receiver_data_plane",
        prefix + "test_kv_transfer",
        prefix + "test_mooncake_layerwise",
        prefix + "test_main_only_cache_plan",
        prefix + "test_main_compute_completion",
        prefix + "test_main_snapshots",
    ]
    if args.full:
        tests.append(prefix + "test_pool_worker")
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    logfile = artifacts / f"ut-{timestamp}.log"
    with (
        logfile.open("x") as output,
        contextlib.redirect_stdout(output),
        contextlib.redirect_stderr(output),
    ):
        if args.pytest:
            import pytest

            if not args.tests:
                parser.error("--pytest requires explicit test paths")
            status = pytest.main(
                [
                    "-q",
                    "--confcutdir="
                    + str(worktree / "tests/ut/distributed/ascend_store"),
                    *[str(worktree / name) for name in args.tests],
                ]
            )
        else:
            suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            status = 0 if result.wasSuccessful() else 1
    if args.pytest:
        print(f"pytest status={status}")
        print(f"Log: {logfile.relative_to(root)}")
        return int(status)
    print(
        f"Ran {result.testsRun} tests: failures={len(result.failures)}, errors={len(result.errors)}"
    )
    print(f"Log: {logfile.relative_to(root)}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
