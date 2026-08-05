import subprocess
import sys

TEST_YAML = "tests/fixtures/config_a2.yaml"
TEST_YAML_A3 = "tests/fixtures/config_a3.yaml"
TEST_YAML_STR = "tests/fixtures/config_nodes_str.yaml"


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


def test_a3_instances_count():
    rows = run(TEST_YAML_A3)
    # 2×2 prefill + 2×4 decode + 1 proxy = 13
    assert len(rows) == 13
    pre = [(r, ip, p) for r, ip, p in rows if r == "prefill"]
    dec = [(r, ip, p) for r, ip, p in rows if r == "decode"]
    assert len(pre) == 4
    assert len(dec) == 8


def test_a3_decode_4_engines_per_node():
    rows = run(TEST_YAML_A3)
    dec = [(r, ip, p) for r, ip, p in rows if r == "decode"]
    # d0 4 引擎端口 9900-9903，d1 4 引擎端口 9900-9903
    assert [ip for _, ip, _ in dec[:4]] == ["10.0.1.5"] * 4
    assert [p for _, _, p in dec[:4]] == ["9900", "9901", "9902", "9903"]
    assert [ip for _, ip, _ in dec[4:]] == ["10.0.1.6"] * 4
    assert [p for _, _, p in dec[4:]] == ["9900", "9901", "9902", "9903"]


def test_a3_proxy_present():
    rows = run(TEST_YAML_A3)
    proxy = [(r, ip, p) for r, ip, p in rows if r == "proxy"]
    assert proxy == [("proxy", "10.0.1.1", "8000")]


def test_str_nodes_instances():
    # 字符串形式节点（非 dict）解析正确
    rows = run(TEST_YAML_STR)
    assert len(rows) == 7  # 2 prefill + 4 decode + 1 proxy
    pre = [(r, ip, p) for r, ip, p in rows if r == "prefill"]
    assert [ip for _, ip, _ in pre] == ["10.0.2.1", "10.0.2.2"]
    proxy = [(r, ip, p) for r, ip, p in rows if r == "proxy"]
    assert proxy == [("proxy", "10.0.2.1", "1999")]
