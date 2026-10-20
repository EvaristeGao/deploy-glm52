#!/usr/bin/env python3
"""Mooncake layerwise range-put 离线探针（不启 vLLM 引擎）。

目的：把 vllm-ascend 的 `AscendStoreConnector` + `backend=mooncake` + `use_layerwise`
那条 range/session 写入路径单独拎出来复现，从而把问题归因到 **mooncake/传输层**
而不是 serving 栈。wiring 与 `MooncakeBackend._setup_store()` 保持一致：

  · 默认：走进程级 global TE（`global_te.get_transfer_engine`），
          `local_hostname=<ip>:<te_rpc_port>`，`store.setup(..., engine=te.get_engine())`，
          缓冲区用 `global_te.register_buffer()` 注册；
  · `ASCEND_ENABLE_USE_FABRIC_MEM=1`：fabric mem / 统一地址直传，`local_hostname=<ip>`，
          不带 engine，缓冲区用 `store.register_buffer()`；
  · `ASCEND_GLOBAL_RESOURCE_CONFIG=<json>`（且未开 fabric mem）：
          store 独立 TE（dual-protocol/RoCE 路径），同上不带 engine。

用法（在引擎容器里跑，需要 MOONCAKE_CONFIG_PATH）：

  docker exec -e MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json <ctr> \
      python3 /root/pd/mooncake_put_probe.py --device 0 --blocks 4
  docker exec -e MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json <ctr> \
      python3 /root/pd/mooncake_put_probe.py --device 8 --blocks 4 --seg-gb 1

判定：最后一行 `[probe] RESULT PASS|FAIL`，PASS 要求 range_put 的 results 全 0
且 put_session_end 成功、batch_is_exist 全 1。
"""
import argparse
import json
import os
import socket
import sys


def get_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=0, help="NPU logic device id")
    ap.add_argument("--seg-gb", type=float, default=0.0, help="contribute this much global segment (GiB)")
    ap.add_argument("--blocks", type=int, default=4, help="how many keys (blocks) per put")
    ap.add_argument("--block-bytes", type=int, default=116736, help="per-block object size (production: 116736)")
    ap.add_argument("--key-prefix", default="probe")
    ap.add_argument("--prefer-local", action="store_true",
                    help="让对象优先分配在本进程的 segment（验证本地写 vs 跨进程/跨节点写）")
    ap.add_argument("--prefer-same-node", action="store_true",
                    help="只设 prefer_alloc_in_same_node（不给 preferred_segment）⇒ 同机跨进程目标")
    args = ap.parse_args()

    cfg_path = os.environ.get("MOONCAKE_CONFIG_PATH")
    if not cfg_path or not os.path.exists(cfg_path):
        print("[probe] MOONCAKE_CONFIG_PATH missing; set it (e.g. /root/pd/mooncake.json)")
        return 2
    with open(cfg_path) as f:
        cfg = json.load(f)
    metadata_server = cfg.get("metadata_server")
    master = cfg.get("master_server_address")
    protocol = cfg.get("protocol", "ascend")
    device_name = cfg.get("device_name", "")

    import torch  # noqa: F401
    import torch_npu  # noqa: F401
    from mooncake.store import MooncakeDistributedStore, ReplicateConfig

    torch.npu.set_device(args.device)

    use_fabric = os.environ.get("ASCEND_ENABLE_USE_FABRIC_MEM") == "1"
    use_indep = bool(os.environ.get("ASCEND_GLOBAL_RESOURCE_CONFIG")) and not use_fabric
    wiring = "fabric-indep" if (use_fabric or use_indep) else "global-te"

    ip = get_ip()
    seg_bytes = int(args.seg_gb * (1 << 30))
    store = MooncakeDistributedStore()
    if wiring == "fabric-indep":
        local_hostname = ip
        ret = store.setup(
            local_hostname=local_hostname,
            metadata_server=metadata_server,
            global_segment_size=seg_bytes,
            local_buffer_size=0,
            protocol=protocol,
            rdma_devices=device_name,
            master_server_addr=master,
        )
        te = None
    else:
        from vllm_ascend.distributed.kv_transfer.utils.mooncake_transfer_engine import global_te

        te = global_te.get_transfer_engine(ip, device_name=None)
        local_hostname = f"{ip}:{te.get_rpc_port()}"
        ret = store.setup(
            local_hostname=local_hostname,
            metadata_server=metadata_server,
            global_segment_size=seg_bytes,
            local_buffer_size=0,
            protocol=protocol,
            rdma_devices=device_name,
            master_server_addr=master,
            engine=te.get_engine(),
        )
    print(f"[probe] wiring={wiring} device={args.device} local={local_hostname} "
          f"seg_gb={args.seg_gb} setup_ret={ret}")
    if ret != 0:
        print("[probe] RESULT FAIL (store.setup != 0)")
        return 2

    buf = torch.zeros(args.block_bytes, dtype=torch.int8, device=f"npu:{args.device}")
    ptr = buf.data_ptr()
    if wiring == "fabric-indep":
        reg = store.register_buffer(ptr, args.block_bytes)
    else:
        global_te.register_buffer([ptr], [args.block_bytes])
        reg = 0
    print(f"[probe] register_buffer ret={reg} ptr=0x{ptr:x} bytes={args.block_bytes}")

    n = args.blocks
    keys = [f"{args.key_prefix}-{ip}-{os.getpid()}-{args.device}-{i}" for i in range(n)]
    sizes = [args.block_bytes] * n
    rep = ReplicateConfig()
    if args.prefer_local:
        try:
            rep.preferred_segment = local_hostname
            rep.prefer_alloc_in_same_node = True
            print(f"[probe] replicate: preferred_segment={local_hostname} prefer_same_node=True")
        except Exception as exc:
            print(f"[probe] replicate preference unsupported: {type(exc).__name__}: {exc}")
    elif args.prefer_same_node:
        try:
            rep.prefer_alloc_in_same_node = True
            print("[probe] replicate: prefer_same_node=True (no preferred_segment ⇒ 同机跨进程目标)")
        except Exception as exc:
            print(f"[probe] replicate preference unsupported: {type(exc).__name__}: {exc}")
    start = store.batch_put_session_start(keys, sizes, rep)
    print(f"[probe] put_session_start keys={n} -> {start}")
    try:
        results = store.batch_put_from_multi_buffer_ranges(
            keys, [[ptr]] * n, [[args.block_bytes]] * n, [[0]] * n
        )
    except Exception as exc:  # native path may crash the process instead
        print(f"[probe] range_put raised: {type(exc).__name__}: {exc}")
        print("[probe] RESULT FAIL (exception)")
        return 3
    print(f"[probe] range_put results={results}")

    # 约定：range put 的返回是「该 key 写入的字节数」，负数才是失败
    # （vllm-ascend 侧 `_handle_range_request` 用 `result < 0` 判定 failed_keys）。
    ok = len(results) == n and all(r >= 0 for r in results)
    if ok:
        end = store.batch_put_session_end(keys)
        print(f"[probe] put_session_end -> {end}")
        exist = store.batch_is_exist(keys)
        print(f"[probe] batch_is_exist -> {exist}")
        ok = len(end) == n and all(r == 0 for r in end) and all(e == 1 for e in exist)
        if ok:
            try:
                store.remove(keys)
            except Exception as exc:
                print(f"[probe] cleanup remove skipped: {type(exc).__name__}: {exc}")
    else:
        try:
            print(f"[probe] revoke -> {store.batch_put_session_revoke(keys)}")
        except Exception as exc:
            print(f"[probe] revoke raised: {type(exc).__name__}: {exc}")

    try:
        store.close()
    except Exception:
        pass
    print(f"[probe] RESULT {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
