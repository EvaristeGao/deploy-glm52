# Layerwise TP2/TP4 versus DCP performance — 2026-11-09

Status: all12 formal points,960/960 requests passed; original service restored and verified.

## Fixed workload and deployments

12 formal cells: mode {D TP2, D TP4, stock1M DCP} × cache {0%, about90%} × concurrency {30,40}.
Each cell has80 random128,000-content-token inputs,2,000 output tokens, seed20261106,
request-rate infinity and bounded concurrency. Same v1.7 vendor benchmark source SHA
`b109ab8e4163355cc102f575c0cf209acb91672e9d869ff6b3e775e92a70405a`.
The wrapper validates SSE completion, actual input/output usage and output count only.
Random output text is not judged semantically. Native completion receipts are audited after timing.

- Ours: P TP8×DP4, eager/MTP1/Main B2, chunk16K/seq8, Store40GiB/rank;
  D TP2×DP16 or TP4×DP8, CP1, Host100GiB/DP, seq8/token32, async/MTP3,
  main model mode3 FULL_DECODE_ONLY and eager GLM draft. Max model length1M.
- DCP: frozen production scripts/image from the previous90% comparison;
  P TP16×DP2/DCP16, eager/MTP1, seq64/token8192;
  D TP4×DP8/DCP4, seq32/token192, async/MTP5, decode graph. Max length1M.
- The two feature variants use the identical812-file Python/example snapshot from the
  previous native P8→D4 validation, including the two uncommitted TP patches; native SDK
  artifacts are hash-checked before/after overlay. No service source edit for this performance run.
- DCP uses unmodified stock image090eeee66e3, no source overlay. Each mode has private
  four-node containers and logs. Default inventory and original service code are unchanged.

All three receive equal excluded warmup:16 short2K/16-output requests at C16 and16
128K/32-output requests at C16. C16 pilots at0/90% run before formal cells. Each90% cell
prepares and warms its own116,736-token common content prefix, with unique request suffixes
and isolated cache salts. Warmup ends before the timed metrics snapshot. Actual cache ratio
must be zero or88.5–91.5% respectively, and is reported separately (DCP/MTP block alignment differs).
Inputs/hashes and actual lengths must match across deployments for each formal condition.

Only API metrics sampled every5s and independent Master HTTP every15s; no worker attach,
profiler, process pause or device synchronization during timing. Counters must show80/80 on
both P/D, no preemptions, no pending requests and zero logical KV after each successful cell.
Failures are preserved and do not count as valid performance points.

This is a whole-deployment comparison with different P topology, MTP and software stacks;
it does not isolate a single KV-offload variable. Throughput, TTFT mean/P50/P99 and TPOT
P50/P99 will be reported; a TTFT improvement alone is not declared an overall win.

## Operation and evidence

Local `artifacts/pd-tp-sweep-20261109/`; SFS `/mnt/sfs_turbo/g00832294/pd-tp-sweep-20261109`.
Owned containers `vllm-ascend-glm52-tpperf-{tp2,tp4,dcp}-20261109`, proxies8336/8337/8446.
Order TP2 → TP4 → DCP. The original P meta40/D refactor-r3 model containers are captured,
then stopped once all preparation succeeds. Original Master52551 and proxy8333 control
processes stay alive. The controller stops candidates and restores original IDs/images/sources,
private shm settings and exact launch commands in its finalizer, then performs a separate
original-topology request/receipt/idle check. No host-global changes.

Host preflight: each D node has about1.7TiB available; TP2 needs800GiB Host backing per D node.

## 完整结果

**0%命中下，TP2与TP4均在C30/C40超过DCP；约90%命中下，两档均提高输出吞吐、降低TPOT，DCP仍有更短的首token延迟。**
TP2四点吞吐最高、TPOT最低；2K输出的E2E中位数也都是TP2最低。
所有0%点实测0%，90%点特性组91.0957%、DCP89.5958%。同一条件80条的输入文本哈希与实际长度逐条一致。
0%实际长度117,340–128,006，均值127,601.51；90%实际每条128,006。所有输出都是2,000 tokens。

|Cache|C|Mode|TTFT mean s|TTFT P50 s|TTFT P99 s|TPOT P50 ms|Output tok/s|Hit %|D running peak|D KV peak %|
|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
|0|30|dcp|144.527|152.738|232.829|36.437|232.376|0.000|13|17.62|
|0|30|tp4|121.783|125.590|196.258|24.912|294.503|0.000|11|43.91|
|0|30|tp2|116.101|123.792|180.610|23.068|308.958|0.000|11|22.13|
|0|40|dcp|197.768|226.535|308.158|37.605|234.701|0.000|13|17.62|
|0|40|tp4|160.844|177.743|251.349|25.559|296.319|0.000|12|65.75|
|0|40|tp2|158.041|181.743|243.561|23.082|310.136|0.000|10|32.96|
|90|30|dcp|10.723|6.152|30.764|39.444|565.451|89.596|30|35.24|
|90|30|tp4|19.278|6.932|65.569|31.417|647.912|91.096|30|44.23|
|90|30|tp2|18.784|11.758|55.978|25.598|753.160|91.096|30|22.14|
|90|40|dcp|15.084|7.654|39.947|41.365|652.862|89.596|40|44.02|
|90|40|tp4|31.423|21.933|87.747|34.934|694.168|91.096|40|55.24|
|90|40|tp2|29.011|21.213|81.093|27.668|794.338|91.096|39|33.18|


## 相对本轮DCP的变化

| 命中 | 并发 | D TP | TTFT均值 | TTFT P50 | TTFT P99 | TPOT P50 | 输出吞吐 | E2E P50 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
|0%|30|4|-15.7%|-17.8%|-15.7%|-31.6%|+26.7%|-22.1%|
|0%|30|2|-19.7%|-19.0%|-22.4%|-36.7%|+33.0%|-25.5%|
|0%|40|4|-18.7%|-21.5%|-18.4%|-32.0%|+26.3%|-23.0%|
|0%|40|2|-20.1%|-19.8%|-21.0%|-38.6%|+32.1%|-24.0%|
|90%|30|4|+79.8%|+12.7%|+113.1%|-20.3%|+14.6%|-14.2%|
|90%|30|2|+75.2%|+91.1%|+82.0%|-35.1%|+33.2%|-31.7%|
|90%|40|4|+108.3%|+186.5%|+119.7%|-15.5%|+6.3%|-10.3%|
|90%|40|2|+92.3%|+177.1%|+103.0%|-33.1%|+21.7%|-27.0%|

## 阶段与容量

0%/C30→C40，TP2吞吐309→310 tok/s、TPOT基本不变，平均P排队83.16→123.91s。
P prefill仍约29.5s，高于DCP约17s；端到端TTFT较短主要来自更短的P队列。
0%/C40的D running峰仅10（TP2）/12（TP4），D仍有大量空余，并发加大主要增加P等待。

90%/C40，D running峰为TP2 39、TP4 40、DCP40；单实例KV峰33.18%、55.24%、44.02%。
两档特性的5秒采样容量等待估计均为0。TP2仍比TP4吞吐高14.4%、TPOT低20.8%，虽然TP4已经可以承载40条。
TP2/TP4各自D每实例9,174块、1,174,272逻辑tokens；总逻辑容量18,788,352/9,394,176，
配置序列上限128/64。每D节点Host backing约800/400GiB；两组D卡数同为32。

90%/C40的TTFT均值DCP15.084s、TP229.011s、TP431.423s；对应P排队4.963/6.450/7.527s、
P prefill2.910/15.564/16.212s。TP2相对DCP的TTFT差13.927s、P两阶段差14.141s，
剩余差额约−0.214s。主要差距在P prefill（阶段包括缓存恢复等工作），不能归因成D容量等待，
也不能把TTFT减去P阶段后的差额当作纯PD传输。本轮未做算子级跟踪。

每种部署先跑0%再跑90%，独立salt和前缀预热保证实际命中率，但不清空进程的KV池。
两档特性Store均1,280GiB，正式点Master峰约79.3%–80.3%，存在LRU淘汰，采样分配失败增量为0。
与历史直接从冷池跑90%的结果有不同池状态；本文胜负只使用本轮匹配的数据。

## 证据与恢复

12点P/D各80成功、无抢占，结束队列和逻辑KV均为零。TP2和TP4各390条含预热/探针的
精确接收及释放审计全通过，覆盖16/8个D；两档使用2/4个期望rank。所有模式运行前后镜像/ID/启动SHA
相同，特性模式完整812文件及native SDK哈希通过，DCP为原stock镜像。保存的引擎/代理日志无
ReadError、协议、接收证明或通知错误。只检查计数和协议，随机文本没有语义精度判定。

原始产物在 `artifacts/pd-tp-sweep-20261109/`：`comparison.json`（完整P99/阶段指标）、
`comparison-table.txt`、`results/`、`*-native-audit.json`、`master-formal-summary.json`、
`native-log-audit.json`、`before/after-*.json`、各模式engine archive和controller状态。
原P meta40 / D refactor-r3已按原容器ID、镜像、源码、启动脚本、Store配置及私有shm恢复。
原Master/8333控制PID保留，原8个API健康，原拓扑独立回归完成8-rank接收与释放，队列/KV为空。
本轮12个候选容器、三个独立代理和CPU客户端均已停止；默认inventory未改，性能轮无运行时代码改动。
恢复证据 `restored-*.json`、`post-restore.stdout`、`restore-tp2.log`、`verify-restored-tp2.log`、
`finished-*.json`；controller `complete_original_restored`、`performance_passed=true`。
