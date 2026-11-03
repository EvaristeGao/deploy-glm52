# P-only 1M validation image

This image derives from the verified r03 image and adds only a model path alias
for an isolated Mooncake Store namespace. It changes no Python production code,
SDK, transfer backend or weights. Build it only on the two Prefill nodes.

The alias target is the existing read-only model mount. Runtime uses INT8 Main
and Indexer caches, eager V1 execution, no MTP and Main-only shared slots.
The separate Decode nodes are not part of this image experiment.

The image build succeeded on P0/P1 as
`glm52-vllm-ascend:0.30.0-main-reuse-dev-20261103-r05`.
The 10 production modules match the r03 manifest. Their functional source commit
is `9824ec91c`; source HEAD `f9b995c68` only adjusts formatting.

Runtime profiles, generated scripts and evidence live under
`artifacts/p-main-buffer-reuse/`. See
`docs/p-main-buffer-reuse-p-only-1m.md` for validation status; building this image
alone does not establish 1M support.
