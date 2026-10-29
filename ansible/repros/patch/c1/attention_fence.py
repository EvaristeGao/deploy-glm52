#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import torch

import re

_lock = threading.RLock()
# [PORT b348e1e] 每层一个 gate：prefetch/需求 load 绑定「自己那层」的 gate，
# attention 按 layer_name 打开对应 gate，避免旧实现「全局单 gate + 捕获时快照」
# 在跨 step reset 后永远等不到 record 的环形等待。
_gates: list[AttentionComputeStartGate] = []
_record_cursor = 0


class AttentionComputeStartGate:
    """Gate that opens when the compute stream reaches attention.

    The attention worker records an NPU event immediately before submitting the
    attention op. MemCache worker threads wait for that event to complete before
    submitting H2D/L2G work, so transfer starts when the compute stream is
    actually at the attention boundary rather than merely after the Python call
    site was reached.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self.on_start: Callable[[], None] | None = None
        self.on_finish: Callable[[], None] | None = None
        self._event: torch.npu.Event | None = None

    def record(
        self,
        stream: torch.npu.Stream | None = None,
    ) -> None:
        stream = stream or torch.npu.current_stream()
        event = torch.npu.Event()
        event.record(stream)
        with self._condition:
            if self._event is None:
                self._event = event
                self._condition.notify_all()
                if self.on_start is not None:
                    self.on_start()

    def cancel(self) -> None:
        """Release gated tasks after their caller has set its transfer abort flag."""
        self.on_start = None
        self.on_finish = None
        self.record()

    def wait(self, timeout: float = 10.0) -> bool:
        with self._condition:
            while self._event is None:
                if not self._condition.wait(timeout=timeout):
                    return False
            event = self._event

        # [FIX] 不能用 event.synchronize()：torch_npu 的实现会持 GIL 阻塞，
        # 而主线程此时要调 gate.record()/current_stream() 也需要 GIL → GIL 死锁。
        # 改成 query() + sleep（sleep 释放 GIL）轮询。
        while not event.query():
            time.sleep(0.0005)
        return True


def _extract_layer_index(layer_name: str) -> int | None:
    """从 cache 名（如 model.layers.5.self_attn.attn）解析物理层号。"""
    match = re.search(r"layers\.(\d+)", layer_name or "")
    return int(match.group(1)) if match else None


def reset_attention_compute_start_gates(num_layers: int) -> None:
    """每个 forward step 重建 num_layers 个 per-layer gate。"""
    global _gates, _record_cursor
    with _lock:
        _gates = [AttentionComputeStartGate() for _ in range(max(0, int(num_layers)))]
        _record_cursor = 0


def reset_attention_compute_start_gate() -> AttentionComputeStartGate:
    """兼容旧调用：等价于「只有一层」的 gate 列表，返回该 gate。"""
    reset_attention_compute_start_gates(1)
    return get_attention_compute_start_gate(0)  # type: ignore[return-value]


def get_attention_compute_start_gate(layer_idx: int | None = None) -> AttentionComputeStartGate | None:
    """返回 layer_idx 的 gate；layer_idx=None 时退回第 0 个（兼容旧调用）。"""
    with _lock:
        if not _gates:
            if layer_idx is None:
                _gates.append(AttentionComputeStartGate())
            else:
                return None
        if layer_idx is None:
            return _gates[0]
        if 0 <= layer_idx < len(_gates):
            return _gates[layer_idx]
    return None


def _next_gate_by_cursor() -> AttentionComputeStartGate | None:
    """没有 layer_name 的 attention 路径：按 record 顺序依次开 gate。"""
    global _record_cursor
    with _lock:
        if not _gates:
            return None
        idx = _record_cursor
        _record_cursor += 1
        if 0 <= idx < len(_gates):
            return _gates[idx]
    return None


def record_attention_compute_start(layer_name: str = "") -> None:
    """在提交 attention 前记录本层 gate（带 layer_name 时按层定位）。"""
    if layer_name:
        idx = _extract_layer_index(layer_name)
        if idx is None:
            return
        gate = get_attention_compute_start_gate(idx)
    else:
        gate = _next_gate_by_cursor()
    if gate is not None:
        gate.record()


def _resolve_window_gate(layer_name: str) -> AttentionComputeStartGate | None:
    if layer_name:
        idx = _extract_layer_index(layer_name)
        if idx is None:
            return None
        return get_attention_compute_start_gate(idx)
    return _next_gate_by_cursor()


@contextmanager
def attention_transfer_window(layer_name: str = ""):
    """Run the configured transfer completion policy after attention submission.

    The event is recorded after cache writes and any preceding collectives. The
    host then submits the attention kernel. The registered connector callback
    decides whether to drain, bound, or retain its asynchronous transfer work.
    """
    gate = _resolve_window_gate(layer_name)
    if gate is None:
        yield
        return
    try:
        gate.record()
        yield
    finally:
        if gate.on_finish is not None:
            gate.on_finish()
