import json
import os
import subprocess
import sys

import pytest

TEST_YAML = "tests/fixtures/config_a2.yaml"
OUT = "tests/fixtures/out_mooncake.json"


@pytest.fixture(autouse=True)
def _cleanup_output():
    # 清理上次运行遗留的输出文件，避免污染 fixtures。
    yield
    if os.path.exists(OUT):
        os.remove(OUT)


def test_renders_master_address():
    r = subprocess.run(
        [sys.executable, "resolve/render_mooncake.py", "--config", TEST_YAML, "--output", OUT],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    with open(OUT) as f:
        cfg = json.load(f)
    assert cfg["master_server_address"] == "10.0.0.1:50088"
    assert cfg["metadata_server"] == "P2PHANDSHAKE"
    assert cfg["global_segment_size"] == "80GB"
