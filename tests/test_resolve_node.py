import subprocess
import sys
import os

TEST_YAML_A2 = "tests/fixtures/config_a2.yaml"


def run(cfg, node):
    return subprocess.run(
        [sys.executable, "resolve/resolve_node.py", "--config", cfg, "--node", node],
        capture_output=True, text=True, check=True,
    ).stdout


def parse(out):
    return dict(line.split("=", 1) for line in out.strip().splitlines())


def test_prefill_p0_rank_start_0():
    out = parse(run(TEST_YAML_A2, "p0"))
    assert out["ROLE"] == "prefill"
    assert out["NODE_INDEX"] == "0"
    assert out["DP_RANK_START"] == "0"
    assert out["DP_SIZE"] == "4"
    assert out["TP_SIZE"] == "8"
    assert out["DP_SIZE_LOCAL"] == "1"
    assert out["KV_PORT"] == "30000"


def test_prefill_p3_rank_start_3():
    out = parse(run(TEST_YAML_A2, "p3"))
    assert out["DP_RANK_START"] == "3"
    assert out["DP_ADDRESS"] == "10.0.0.1"  # prefill 首节点 IP


def test_decode_d1_rank_start_2():
    out = parse(run(TEST_YAML_A2, "d1"))
    assert out["ROLE"] == "decode"
    assert out["DP_RANK_START"] == "2"     # 1 * dp_size_local(2)
    assert out["DP_SIZE"] == "8"
    assert out["TP_SIZE"] == "4"
    assert out["KV_PORT"] == "30100"
    assert out["DP_ADDRESS"] == "10.0.0.5"  # decode 首节点 IP（夹具中 decode[0]=10.0.0.5）


def test_unknown_node_fails():
    r = subprocess.run(
        [sys.executable, "resolve/resolve_node.py", "--config", TEST_YAML_A2, "--node", "xx"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "xx" in r.stderr
