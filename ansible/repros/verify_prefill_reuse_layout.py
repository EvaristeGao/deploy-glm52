#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V0 离线验证：P 侧 prefill KV buffer 复用（mooncake/block_key 平面）对 GLM-5.2 是否成立。

在装了 vllm + vllm-ascend 的容器里用 CPU 跑（不需要 NPU）：

    python3 /mnt/sfs_turbo/g00832294/probe/verify_prefill_reuse_layout.py

回答三个问题（任一 FAIL 都不应继续改代码）：
  Q1 复用规划对我们模型的 cache spec 分类是否正确（main / indexer / 无主之外的杂项）
  Q2 合并 descriptor 后有没有层被丢掉（丢层 = 那层没分配显存 = 运行期坏掉）
  Q3 合并前后 Mooncake 的 key 空间（hybrid_layout_id）是否不变（变了 = 命中永远失效且不报错）

用法：
  --layers 78            # hidden layers（不含 MTP）
  --mtp 1                # MTP/spec-decode 层数
  --block-size 128
  --main-dim 576         # MLA latent: kv_lora_rank 512 + qk_rope 64
  --indexer-dim 128      # DSA indexer k
  --shared-indexer 4     # 每 N 层共享一个 indexer（0 = 每层独立）
  --shared-buffers 9     # B（复用槽位数）
"""
from __future__ import annotations

import argparse
import sys
import types

ap = argparse.ArgumentParser()
ap.add_argument("--layers", type=int, default=78)
ap.add_argument("--mtp", type=int, default=1)
ap.add_argument("--block-size", type=int, default=128)
ap.add_argument("--main-dim", type=int, default=576)
ap.add_argument("--indexer-dim", type=int, default=128)
ap.add_argument("--shared-indexer", type=int, default=0,
                help="每 N 层共享一个 indexer（复刻 indexer_types full/shared）；0=每层独立")
ap.add_argument("--shared-buffers", type=int, default=9)
args = ap.parse_args()

results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, note: str) -> None:
    tag = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
    results.append((name, tag, note))
    print(f"  [{tag}] {name}: {note}")


try:
    import torch
    from vllm.v1.kv_cache_interface import (
        KVCacheConfig,
        KVCacheGroupSpec,
        KVCacheTensor,
        UniformTypeKVCacheSpecs,
    )
    from vllm_ascend.core.kv_cache_interface import AscendMLAAttentionSpec
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store import (
        layerwise_cache_layout as LCL,
    )
    from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.backend import (
        mooncake_layerwise as MW,
    )
except Exception as exc:  # noqa: BLE001
    print("导入失败（需要在装了 vllm/vllm-ascend 的容器里跑）:", exc)
    sys.exit(2)

N, MTP = args.layers, args.mtp
BS, MAIN_DIM, IDX_DIM = args.block_size, args.main_dim, args.indexer_dim
B = args.shared_buffers

main_names = [f"model.layers.{i}.self_attn.attn" for i in range(N)]
mtp_names = [f"model.mtp.layers.{i}.self_attn.attn" for i in range(MTP)]

# indexer：full 层每层一个；shared 层按 --shared-indexer 共享（只给组首层建 spec）
indexer_names: list[str] = []
if args.shared_indexer and args.shared_indexer > 1:
    for i in range(N):
        if i % args.shared_indexer == 0:
            indexer_names.append(f"model.layers.{i}.self_attn.indexer.k_cache")
else:
    indexer_names = [f"model.layers.{i}.self_attn.indexer.k_cache" for i in range(N)]

main_spec = AscendMLAAttentionSpec(
    block_size=BS, num_kv_heads=1, head_size=MAIN_DIM, dtype=torch.int8, cache_dtype_str="int8"
)
idx_spec = AscendMLAAttentionSpec(
    block_size=BS, num_kv_heads=1, head_size=IDX_DIM, dtype=torch.int8, cache_dtype_str="int8"
)
mtp_spec = AscendMLAAttentionSpec(
    block_size=BS, num_kv_heads=1, head_size=MAIN_DIM, dtype=torch.int8, cache_dtype_str="int8"
)

specs: dict = {}
for n in main_names:
    specs[n] = main_spec
for n in indexer_names:
    specs[n] = idx_spec
for n in mtp_names:
    specs[n] = mtp_spec

all_names = main_names + indexer_names + mtp_names
uniform = UniformTypeKVCacheSpecs.from_specs(specs)
kv_cache_config = KVCacheConfig(
    num_blocks=1024,
    kv_cache_tensors=[],  # 由 _make_tensors 填充
    kv_cache_groups=[KVCacheGroupSpec(all_names, uniform)],
)


def _page_bytes(spec) -> int:
    try:
        return spec.page_size_bytes
    except Exception:  # noqa: BLE001
        return spec.block_size * spec.num_kv_heads * spec.head_size


def _make_tensor(name: str) -> KVCacheTensor:
    # 真实布局：[layer][block][page]，块间连续 ⇒ block_stride == page_size，
    # layer_stride = num_blocks * page_size（apply_layerwise_kv_cache_plan 的连续性校验）。
    page = _page_bytes(specs[name])
    layer_bytes = page * kv_cache_config.num_blocks
    return KVCacheTensor(layers=[name], layer_stride=layer_bytes, block_stride=page, offset=0, size=layer_bytes)


kv_cache_config.kv_cache_tensors = [_make_tensor(n) for n in all_names]

extra_config = {
    "backend": "mooncake",
    "use_layerwise": True,
    "use_layerwise_buffer_reuse": True,
    "layerwise_num_shared_buffers": B,
    "layerwise_prefetch_layers": min(B, 2),
    "layerwise_independent_layers": [0],
}

print(f"层数={N}+MTP{MTP} main={MAIN_DIM} indexer={IDX_DIM} shared_indexer={args.shared_indexer} B={B}")

# ---- Q1 分类 ----
layout = LCL.build_layerwise_reuse_layout(specs, N, extra_config)
extra = sum(len(s.extra_main_specs) for s in layout.layer_cache_specs.values())
idx = sum(1 for s in layout.layer_cache_specs.values() if s.indexer is not None)
print(f"  layer_cache_specs={len(layout.layer_cache_specs)} indexer_layers={idx} extra_main_specs={extra}")
print(f"  has_layer_reuse={layout.has_layer_reuse} buffer_slots={len(layout.buffer_slots)} "
      f"prefetch_map={len(layout.prefetch_layer_map)}")
record("Q1 分类完整（无杂项落入 extra_main_specs）", extra == 0,
       f"extra_main_specs={extra}")

# ---- 容量系数 ----
slots = len(layout.buffer_slots)
physical_layers = len(layout.layer_cache_specs)
factor = physical_layers / slots if slots else 1.0
print(f"  physical_layers={physical_layers} slots={slots} factor={factor:.2f}x")
record("容量放大符合预期（factor>1）", factor > 1.0, f"factor={factor:.2f}x")

# ---- Q3 key 空间（合并前后 hybrid_layout_id）----
before_id = MW.hybrid_layout_id(kv_cache_config, tp_size=16)
before = set()
for t in kv_cache_config.kv_cache_tensors:
    before.update(t.layers)

# 打开 opt-in（等价于 R1 改完后）
MW.extract_layout_config = lambda cfg: (cfg if cfg.get("use_layerwise_buffer_reuse") else None)
vllm_config = types.SimpleNamespace(
    kv_transfer_config=types.SimpleNamespace(
        kv_connector="AscendStoreConnector", kv_connector_extra_config=extra_config
    ),
    model_config=types.SimpleNamespace(get_num_layers=lambda pc: N),
    parallel_config=types.SimpleNamespace(),
)

q2_err = None
try:
    LCL.apply_layerwise_kv_cache_plan(kv_cache_config, vllm_config)
except Exception as exc:  # noqa: BLE001
    q2_err = exc

after = set()
for t in kv_cache_config.kv_cache_tensors:
    after.update(t.layers)
print(f"  descriptors: {len(all_names)} -> {len(kv_cache_config.kv_cache_tensors)}; layers lost={len(before - after)}")
if q2_err is not None:
    print(f"  !! apply_layerwise_kv_cache_plan 抛异常: {type(q2_err).__name__}: {q2_err}")
record("Q2 合并后无丢层且不抛异常", q2_err is None and not (before - after),
       f"err={type(q2_err).__name__ if q2_err else None} lost={len(before - after)}")

after_id = MW.hybrid_layout_id(kv_cache_config, tp_size=16) if q2_err is None else "<error>"
print(f"  hybrid_layout_id before={before_id!r} after={after_id!r}")
record("Q3 key 空间不变（hybrid_layout_id 前后一致）", q2_err is None and before_id == after_id,
       f"{before_id!r} -> {after_id!r}")

print()
fails = [r for r in results if r[1] == "FAIL"]
print(f"== 结论：{'全部通过' if not fails else '存在 FAIL，不要继续改代码'} ==")
for name, tag, note in results:
    print(f"  [{tag}] {name}")
sys.exit(0 if not fails else 1)
