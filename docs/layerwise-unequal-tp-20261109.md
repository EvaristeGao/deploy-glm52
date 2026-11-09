# GLM layerwise P/D unequal TP — 2026-11-09

## Scope and mapping

The implementation is committed as `a1a45e9be` in `src/vllm-ascend-reuse`,
based on `85f7f6e50`.
Support GLM DSA replicated Main BF16 / Indexer K+scale, CP1, P TP an integer multiple of D TP.
The initial deployment target is P TP8 × DP4 → D TP4 × DP8. Equal TP retains its mapping.
Reverse P TP < D TP, other model families, CP layouts and Main quantization remain rejected.

GLM's Indexer K projection is `MergedColumnParallelLinear(..., disable_tp=True)` in upstream
`deepseek_v2.Indexer`; cache spec declares one KV head. P DSA-CP gathers K and scale across
TP before writing cache. Main MLA KV is replicated. Thus the first D-TP P ranks can supply
complete Indexer views without head resharding. Existing source/target dtype, stride, page,
registration generation and block bounds checks still run for every actual writer.

- Main: canonical P0 writes the shared D Host backing once; every D rank receives its proof.
- Indexer: P rank r → D rank r for `0 <= r < D TP`, including separate K and scale entries.
- Higher P ranks: local compute / Store PUT and source fencing continue; no remote DMA,
  address lookup, terminal or completion proof is submitted. Request tombstones still forbid replay.
- Exact D receipt writers remain `0..D TP-1`; missing an active writer never permits decode.
  No manifest or owner codec wire change is required.

Per-instance sequences and KV block capacity are held constant while the deployment
can use more D instances. The TP4 functional run below and the later TP2/TP4
performance sweep measured the resulting capacity and throughput.

## Deployment and validation

`ansible/dsa-layerwise` accepts optional `tp_size`, `dp_size`, `dp_local` separately inside
`prefill_config` and `decode_config`; absent values use the previous shared top-level defaults.
Generation validates each node uses 16 NPUs and each role's two-node DP count. Proxy endpoint
lists and startup health loops use each role's DP count. Functional smoke can audit all D engines.
The default inventory keeps the equal-TP deployment. The
[Decode TP2 profile](../ansible/inventories/a3-layerwise-guide/decode-tp2.yml)
pins the commit above and supplies the parameters measured in the
[TP2/TP4 sweep](layerwise-tp-performance-20261109.md).

Experiment: `artifacts/tp-mismatch-20261109/`, SFS `/mnt/sfs_turbo/g00832294/tp-mismatch-20261109`.
Private candidate containers: `vllm-ascend-glm52-tp8-to-tp4-20261109`; proxy8335.
Python overlay preserves the installed SDK native binaries and validates hashes before/after.
Original P meta40 / D refactor-r3 containers, scripts, images and sources are captured for restoration;
original Master52551 and proxy8333 stay alive. No host-global configuration change.

Local CPU: 44 planner/writer/exact receipt/source-consumer tests passed, including actual
ctypes byte copies P8→D4 over two chunks, missing active writers, no-op source epoch retirement,
request replay and reverse/out-of-group topology rejection. Broader owner tests need real Torch;
the local Python3.14 mock environment is insufficient, so these run in the idle SDK candidate.
Equal-TP default generation compared against pre-change templates: byte-identical scripts/config.
Idle native SDK regression: 234 passed (including real connector topology rejection and owner RPC tests).
After final source formatting, the complete 234-test suite passed again; all four candidate package
snapshots match the local source SHA manifest. Native P8/D4 acceptance passed; see the results below.

## Native result (completed; original deployment restored)

- P TP8×DP4 → D TP4×DP8, max length1,048,576, P eager/MTP1/B2,
  D MTP3/async/main-model mode3 FULL_DECODE_ONLY (GLM draft remains eager).
- 24/24 semantic requests: 8 short538-token inputs; 8 multichunk33,660-token inputs;
  8 repeated-prefix requests with changed final question. All eight D instances covered.
  Each request has four exact prepare_decode receipts plus target release.
- Additional128,056-token request completed with `MAPLE`, normal stop and exact usage;
  its four receipts and target release were separately audited. This is functional validation,
  not the random-input performance workload and not full1M request validation.
- P/D success counters each25; all queues and D logical KV zero after validation.
  Actual P external prefix hits267,264 of670,920 queried tokens across warm/fresh/replay requests.
  D MTP produced150 draft tokens and accepted64. No receipt-write/proof/contract/ReadError/traceback
  in the12 engine logs. Decode graph capture finished on all8 D instances.
- All8 D instances still have9,174 blocks×128=1,174,272 logical tokens each.
  Total logical tokens9,394,176 (2× the TP8×DP4 arrangement); configured global D sequence
  limit64 (previous32). Per-rank available KV budget23.61GiB; Host100GiB/DP unchanged.
  This proves capacity/configuration, not C40 running occupancy or throughput/TTFT improvement.
- Evidence: `sdk-unit.stdout`, `native-smoke.stdout`, `native-long.stdout`, `smoke.log`,
  `functional-metrics-summary.json`, `native-evidence.json`, `candidate-capacity.json`,
  full source/provenance manifests, and the Ansible/controller logs in the experiment directory.

Original containers and sources have been restored with the same IDs/images/source checks.
Original Master and proxy control PIDs stayed unchanged; all eight original APIs are healthy.
A fresh request through the original proxy also passed with8 exact receipts, target release,
empty queues and zero logical KV. See `restore.log`, `verify-restored.log`, `restored-*.json`
and `post-restore.stdout`. Candidate containers are stopped and isolated proxy8335 is closed.
No default inventory or original service source upgrade. The native candidate is a Python overlay on the captured SDK images, not a newly
published image. Wider TP direction/model/CP support and performance comparisons remain untested.
