import subprocess
import sys

TEST_YAML = "tests/fixtures/config_a2.yaml"


def parse(cfg):
    out = subprocess.run(
        [sys.executable, "resolve/resolve_router.py", "--config", cfg],
        capture_output=True, text=True, check=True,
    ).stdout
    return {k: v.strip('"') for line in out.strip().splitlines() for k, _, v in [line.partition("=")]}


def test_proxy_host_port():
    r = parse(TEST_YAML)
    assert r["PROXY_HOST"] == "10.0.0.1"
    assert r["PROXY_PORT"] == "1999"


def test_prefill_hosts_all_4_nodes():
    r = parse(TEST_YAML)
    hosts = r["PREFILLER_HOSTS"].split()
    ports = r["PREFILLER_PORTS"].split()
    assert hosts == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
    assert ports == ["9081", "9081", "9081", "9081"]  # 每节点 1 引擎


def test_decode_hosts_2_per_node():
    r = parse(TEST_YAML)
    hosts = r["DECODER_HOSTS"].split()
    ports = r["DECODER_PORTS"].split()
    # 每节点 2 引擎: 9900 9901
    assert hosts == ["10.0.0.5", "10.0.0.5", "10.0.0.6", "10.0.0.6",
                     "10.0.0.7", "10.0.0.7", "10.0.0.8", "10.0.0.8"]
    assert ports == ["9900", "9901", "9900", "9901", "9900", "9901", "9900", "9901"]