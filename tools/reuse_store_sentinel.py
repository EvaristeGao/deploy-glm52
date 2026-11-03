#!/usr/bin/env python3
"""NPU/Mooncake sentinel test; run only on explicitly freed development devices."""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/root/pd/mooncake.json")
    parser.add_argument("--host-ip", required=True)
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--layers", type=int, default=6)
    parser.add_argument("--chunks", type=int, default=3)
    parser.add_argument("--slots", type=int, nargs="+", default=[1, 2, 3])
    args = parser.parse_args()
    if args.layers < 1 or args.chunks < 1 or any(slot < 1 for slot in args.slots):
        parser.error("layer, chunk and shared-slot counts must be positive")

    import torch
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore, ReplicateConfig

    config = json.loads(Path(args.config).read_text())
    torch.npu.set_device(args.device)
    store = MooncakeDistributedStore()
    code = store.setup(
        local_hostname=args.host_ip,
        metadata_server=config["metadata_server"],
        global_segment_size=128 * 1024 * 1024,
        local_buffer_size=0,
        protocol="ascend",
        rdma_devices=config.get("device_name", ""),
        master_server_addr=config["master_server_address"],
    )
    if code != 0:
        raise RuntimeError(f"Mooncake sentinel setup failed: {code}")
    replicate = ReplicateConfig()
    replicate.preferred_segment = args.host_ip
    replicate.prefer_alloc_in_same_node = True
    alignment = 2 * 1024 * 1024
    main_bytes, indexer_bytes = 16384, 4096
    object_bytes = args.layers * (main_bytes + indexer_bytes)
    allocations = []
    owned_keys, pending_keys, active_gets = set(), set(), set()
    namespace = f"reuse-sentinel-{uuid.uuid4().hex}"

    def expect(codes, *, transfer=False):
        if len(codes) != 1 or (codes[0] < 0 if transfer else codes[0] != 0):
            raise RuntimeError(f"Mooncake sentinel operation failed: {codes}")

    def allocate(size):
        raw = torch.zeros(
            size + alignment, dtype=torch.int8, device=f"npu:{args.device}"
        )
        offset = (-raw.data_ptr()) % alignment
        view = raw[offset : offset + size]
        allocations.append(raw)
        result = store.register_buffer(view.data_ptr(), size)
        if result != 0:
            raise RuntimeError(f"Sentinel buffer registration failed: {result}")
        return view

    def ranges(layer, scratch, indexer):
        return (
            [[scratch.data_ptr(), indexer.data_ptr()]],
            [[main_bytes, indexer_bytes]],
            [
                [
                    layer * (main_bytes + indexer_bytes),
                    layer * (main_bytes + indexer_bytes) + main_bytes,
                ]
            ],
        )

    def value(chunk, layer):
        return (chunk * 13 + layer) % 100

    def verify(scratch, indexer, chunk, layer):
        expected = value(chunk, layer)
        if not bool(torch.all(scratch.cpu() == expected)) or not bool(
            torch.all(indexer.cpu() == -expected - 1)
        ):
            raise AssertionError(f"KV overwritten: chunk={chunk}, layer={layer}")

    try:
        for count in args.slots:
            buffers = [allocate(main_bytes) for _ in range(count)]
            indexers = [allocate(indexer_bytes) for _ in range(args.layers)]
            previous = None
            for chunk in range(args.chunks):
                key = f"{namespace}-B{count}-generation-{chunk}"
                owned_keys.add(key)
                expect(store.batch_put_session_start([key], [object_bytes], replicate))
                pending_keys.add(key)
                if previous is not None:
                    expect(store.batch_get_session_start([previous]))
                    active_gets.add(previous)
                for layer in range(args.layers):
                    scratch, indexer = buffers[layer % count], indexers[layer]
                    addresses, sizes, offsets = ranges(layer, scratch, indexer)
                    if previous is not None:
                        expect(
                            store.batch_get_into_multi_buffer_ranges(
                                [previous], addresses, sizes, offsets
                            ),
                            transfer=True,
                        )
                        verify(scratch, indexer, chunk - 1, layer)
                    scratch.fill_(value(chunk, layer))
                    indexer.fill_(-value(chunk, layer) - 1)
                    torch.npu.synchronize()
                    expect(
                        store.batch_put_from_multi_buffer_ranges(
                            [key], addresses, sizes, offsets
                        ),
                        transfer=True,
                    )
                    # Immediately destroy source contents after the range API
                    # returns. An asynchronous source read would corrupt Store.
                    scratch.fill_(-127)
                    indexer.fill_(127)
                    torch.npu.synchronize()
                # Commit is deliberately after the last layer, so B=1 proves
                # that slot rotation never waits on this whole-object commit.
                expect(store.batch_put_session_end([key]))
                pending_keys.remove(key)
                if previous is not None:
                    if store.batch_get_session_end([previous]) != 0:
                        raise RuntimeError("Sentinel old GET lease did not close")
                    active_gets.remove(previous)
                    removal_code = store.remove(previous, False)
                    if removal_code == 0:
                        owned_keys.remove(previous)
                    else:
                        print(
                            json.dumps(
                                {"cleanup_deferred": previous, "code": removal_code}
                            ),
                            flush=True,
                        )
                previous = key
            expect(store.batch_get_session_start([previous]))
            active_gets.add(previous)
            for layer in range(args.layers):
                scratch, indexer = buffers[layer % count], indexers[layer]
                addresses, sizes, offsets = ranges(layer, scratch, indexer)
                expect(
                    store.batch_get_into_multi_buffer_ranges(
                        [previous], addresses, sizes, offsets
                    ),
                    transfer=True,
                )
                verify(scratch, indexer, args.chunks - 1, layer)
            if store.batch_get_session_end([previous]) != 0:
                raise RuntimeError("Sentinel final GET lease did not close")
            active_gets.remove(previous)
            removal_code = store.remove(previous, False)
            if removal_code == 0:
                owned_keys.remove(previous)
            else:
                print(
                    json.dumps({"cleanup_deferred": previous, "code": removal_code}),
                    flush=True,
                )
            print(
                json.dumps(
                    {
                        "slots": count,
                        "layers": args.layers,
                        "chunks": args.chunks,
                        "result": "PASS",
                    }
                ),
                flush=True,
            )
    finally:
        if active_gets:
            store.batch_get_session_end(list(active_gets))
        if pending_keys:
            store.batch_put_session_revoke(list(pending_keys))
        for key in owned_keys - pending_keys:
            store.remove(key, False)
        store.close()
    print("reuse_store_sentinel: PASS", flush=True)


if __name__ == "__main__":
    main()
