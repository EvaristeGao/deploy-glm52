import subprocess
import sys

TEST_YAML = "tests/fixtures/config_a2.yaml"


def run(cfg):
    out = subprocess.run(
        [sys.executable, "resolve/resolve_instances.py", "--config", cfg],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split() for line in out.strip().splitlines()]


def test_all_instances_count():
    rows = run(TEST_YAML)
    # 4 prefill + 8 decode + 1 proxy = 13
    assert len(rows) == 13


def test_prefill_single_per_node():
    rows = run(TEST_YAML)
    pre = [(r, ip, p) for r, ip, p in rows if r == "prefill"]
    assert len(pre) == 4
    assert (pre[0][1], pre[0][2]) == ("10.0.0.1", "9081")


def test_decode_two_per_node():
    rows = run(TEST_YAML)
    dec = [(r, ip, p) for r, ip, p in rows if r == "decode"]
    assert len(dec) == 8
    assert dec[0][1:] == ("10.0.0.5", "9900")
    assert dec[1][1:] == ("10.0.0.5", "9901")


def test_proxy_present():
    rows = run(TEST_YAML)
    proxy = [(r, ip, p) for r, ip, p in rows if r == "proxy"]
    assert proxy == [("proxy", "10.0.0.1", "1999")]
