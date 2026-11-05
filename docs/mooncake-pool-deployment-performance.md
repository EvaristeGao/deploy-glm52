# Mooncake 池化下的五部署档比较

2026-11-05。**实验结束，B2/P1 的 64K 分组未通过；原服务恢复审计 PASS。** 本文记录 P-only prefill 比较，不代表新 PD transfer connector 或 1M 容量验收。

## 范围与配置

用户确认比较 200K、1M、普通逐层无复用、B2/P1、B4/P3，所有组都开启 Mooncake。每个长度依次从 P0 冷写入，再从 P1 读取相同 prompt／cache_salt；每对使用唯一 salt，排除同实例本地 prefix cache 的影响。

| 部署档 | DCP | Main／Indexer dtype 参数 | chunk token 上限 | MTP | 其它计算差异 |
| --- | ---: | --- | ---: | --- | --- |
| 200K 兼容（0.88） | 1 | auto（BF16）／INT8 | 16384 | 1 | DSA-CP；use_index_cache=true，index_topk_freq=4 |
| 原始 1M | 16 | auto（BF16）／INT8 | 8192 | 1 | DSA-CP |
| 逐层全独立 Main | 1 | INT8／INT8 | 32784 | 关闭 | 所有 Main 层独立；保留当前 feature 搬运／同步路径，其余与 B2/P1 相同 |
| B2/P1 | 1 | INT8／INT8 | 32784 | 关闭 | 首尾独立，2 个共享 Main buffer，prefetch=1 |
| B4/P3 | 1 | INT8／INT8 | 32784 | 关闭 | 首尾独立，4 个共享 Main buffer，prefetch=3 |

共同 TP16×DP2、eager、32K／64K 输入、输出 1 token。为了短请求对照，各档 max_model_len 统一为 131072；不以此证明原始 1M 配置的容量。原始档源于当前 Ansible `_200k`／`_1m`，仅移除源码已不支持的 `c8_enable_reshape_optim`、显式迁移 Indexer INT8，并采用已验证的 RoCE 设置。为暴露加载故障设置 `kv_load_failure_policy=fail`，普通未命中仍正常计算。原档 MTP／Index cache／chunk／dtype 差异保留，结果是部署档比较，不能把全部差距归为 DCP 或 reuse 单变量。

每档每长度 1 对预热＋3 对正式请求，共计划 80 请求，60 次正式。主表用三次正式请求 server prefill 中位数，保留范围、server TTFT 和 client TTFT。冷请求要求 local／external hits=0 且 computed=actual；远端请求要求 local hits=0、external hits>0、computed<actual，并校验答案及实际 token 数。外部命中比例和尾部补算 token 单列，错误答案、无命中、HTTP／native 故障不计入有效排名。

## 保存与命中证据

P0 响应完成后，先确认所有实际 writer 的最终请求累计 target watermark，再发 P1；早期 chunk 完成标记不足以放行。普通块级档按 16 writer 检查；DCP1 逐层档根据复制布局检查 TP0。逐层分支还依赖现有末层 `_wait_for_final_layer_save()` 的队列排空：本轮没有开启 deferred-last-save，前一 chunk 的保存已在 forward 返回前结束。逐层计数可能在不同层间多次归零，因此 marker 单独不能证明所有层完成，不能当通用异步保存协议。最终仍以所有可匹配 blocks 实际 external hit、computed tokens 和输出为准：DCP16 以 2048 tokens 对齐，其余以 128 对齐。

观测 overlay 仅给两个文件加入 CPU 标记调用，另加一个私有 helper，移除探针后 AST 与冻结源码完全相同；未新增设备同步、改变 DMA／模型逻辑或生产源文件。6 项 CPU 检查验证最终 watermark、早期 chunk 排除、接收线程排除及探针失败不影响原处理。计时覆盖上述小额 CPU 观测成本，无逐层 profile。

## 结果

共尝试 76 条请求，已取得 75 条结果，其中 73 条有效、54 条正式记录。五档矩阵已执行，B2/P1 的 64K 未取得正式有效样本。每个单元为三次正式有效样本的中位数，单位秒；冷指 **KV 未命中但运行时已预热**。所有样本的范围、TTFT 与命中量在 results.csv／summary.json。

| 部署档 | 32K KV未命中 | 32K 跨P命中 | 64K KV未命中 | 64K 跨P命中 |
| --- | ---: | ---: | ---: | ---: |
| 200K 兼容（显存预算 0.88） | 2.270 | 7.689 | 4.578 | 15.296 |
| 原始 1M | 3.286 | 1.070 | 6.775 | 1.492 |
| 逐层全独立 Main | 9.582 | 5.378 | 20.335 | 10.692 |
| B2/P1 | 9.587 | 5.299 | 无有效样本 | 无有效样本 |
| B4/P3 | 9.586 | 5.159 | 20.444 | 10.352 |

B4/P3 本轮 16 条请求全部通过；32K／64K 冷 prefill 相对全独立 Main 增量约 0.04%／0.53%，远端命中反而约快 4.1%／3.2%。相对原始 1M 的远端命中，它仍约慢 4.8×／6.9×。B2/P1 的 64K 没有正式有效数字；B4 本次未复现其阻塞，不代表已经证明或修复根因。

B 是共享 Main buffer 数，P 是 prefetch 层数。首尾 Main 各独立一槽，B2／B4 的总 Main 槽为 4／6；本轮 num_blocks 日志为 30010／24614，B4 在固定预算下约少 18% blocks。这里不外推 1M 容量。

### 客户端 TTFT

| 部署档 | 32K KV未命中 | 32K 跨P命中 | 64K KV未命中 | 64K 跨P命中 |
| --- | ---: | ---: | ---: | ---: |
| 200K 兼容（显存预算 0.88） | 2.590 | 7.782 | 4.957 | 15.478 |
| 原始 1M | 3.699 | 1.168 | 7.127 | 1.681 |
| 逐层全独立 Main | 9.617 | 5.412 | 20.391 | 10.751 |
| B2/P1 | 9.620 | 5.337 | 无有效样本 | 无有效样本 |
| B4/P3 | 9.620 | 5.190 | 20.505 | 10.412 |

### 命中后的耗时变化

下表为（命中 prefill／未命中 prefill−1）×100%；负数表示命中更快。

| 部署档 | 32K | 64K |
| --- | ---: | ---: |
| 200K 兼容（显存预算 0.88） | +238.73% | +234.11% |
| 原始 1M | -67.44% | -77.97% |
| 逐层全独立 Main | -43.88% | -47.42% |
| B2/P1 | -44.73% | 无有效样本 |
| B4/P3 | -46.19% | -49.37% |

### 共享 buffer 相对逐层无复用的增量

计算参数相同，仍是各三次样本的观察值，小幅差异不等于已证明收益。

| 配置 | 32K 未命中 | 32K 命中 | 64K 未命中 | 64K 命中 |
| --- | ---: | ---: | ---: | ---: |
| B2/P1 | +0.06% | -1.46% | 无有效样本 | 无有效样本 |
| B4/P3 | +0.04% | -4.07% | +0.53% | -3.18% |

### 命中完整性

所有远端样本要求零本地命中。离线另核验全部可匹配块命中，而不止 external hits>0：原始 1M／DCP16 按 2048 tokens 对齐，32K 命中 30720、64K 命中 63488；其余 DCP1 按 128 对齐，命中 32640／65408。尾部补算约 2042–2046／2043–2045 tokens 与 122–126／123–125 tokens（预热略有差异）。因此命中率不同不是隐藏的 cache miss，记录为实际对齐与补算差异。

### 可以得出的结论与待定位部分

1. 池化收益依赖部署：200K 兼容档的池命中明显慢于重算，原始 1M 档则明显受益。原始 200K 采用 DCP1，并开启 legacy DSA-CP 和 Index cache，冷请求比本轮 1M 档更快；不能把所有计算差距称为 DCP16 的单变量收益。DSA-CP、Main dtype、Index cache、chunk、MTP 等共同变化。

2. 逐层无复用在近完整远端命中下仍约 5.38／10.69 秒，慢于原始 1M。这个成本已经存在于无共享 buffer 路径；共享 buffer 的实际增量应看上表，不能把整段加载时延算成 reuse 竞争。

3. 命中后新计算量只有约 122／125 tokens，不能沿用此前冷请求完整 32K／64K 计算的掩盖窗口。即使 GET 与本层计算并行，计算缩短后也可能暴露更多加载。这是时序推断，本轮没有 hot-request CANN profile，不能给它分配确定的时延占比。

4. 源码核对：普通 KVCacheStoreRecvingThread 跨 group 构造所有 key／buffer 后一次调用 backend.get；block-key layerwise 路径按层、group 和 range batch 调用 get_ranges。DCP16 与 DCP1 的 key rank／上下文分片布局也不同。本轮未逐 rank 统计传输 bytes、调用耗时或网络带宽，因此不能把差距直接断言为某个同步点或网络瓶颈。下一项应固定本次请求，在原始 1M、逐层无复用与 B2 上采 hot 路径 native GET／CANN Free／队列／计算边界，再决定是否聚合提交或调整复制布局。

### 第一对预热（不进入正式中位数）

首次链接／算子初始化可能明显更慢，按预先制定的计划每长度第一对单列，保留原始数字。

| 部署档 | 32K 冷 | 32K 命中 | 64K 冷 | 64K 命中 |
| --- | ---: | ---: | ---: | ---: |
| 200K 兼容（显存预算 0.88） | 2.610 | 12.210 | 4.608 | 14.881 |
| 原始 1M | 4.080 | 7.098 | 6.776 | 1.547 |
| 逐层全独立 Main | 10.547 | 9.211 | 20.333 | 10.778 |
| B2/P1 | 10.786 | 5.563 | 22.261 | 无有效样本 |
| B4/P3 | 10.571 | 5.381 | 20.700 | 10.297 |

### B2/P1 混合长度序列阻塞

B2/P1 跑完 32K cold／remote 后，首个 64K 冷预热请求超过 256 秒没有返回，P0 反复等待逐层 KV load。已保存两端日志，再受控停止自有 r19；本轮没有定位根因，不能把随后客户端断链当 native 崩溃原因。证据为 b2p1-64k-hang-stop.json 与 b2p1-original-sequence-evidence/。主监督在本地 hold 下等待一次相同参数、重启后仅测 64K 的隔离复测。若表中获得 64K 数字，它们来自 isolated64 标签，不能据此宣称原混合请求序列稳定通过。隔离复测的首个 64K 冷预热完成（22.261 秒），随后 P1 远端加载再次阻塞；这不是三次正式样本，不能填入正常性能中位数。后续请求设置 300 秒绝对截止；具体终态见 b2p1-isolated64-outcome.json。

### 初始故障和测试修订

原始 200K 的 gpu_memory_utilization=0.92 在首个 32K 预热请求失败：空闲 P1 的 MTP dummy logits all-gather 申请 2.37 GiB，当时仅剩 1.38 GiB，触发 NPU OOM；P0 的 Gloo peer 断开为继发错误。该失败不计有效性能，原脚本／配置／请求及两端日志在 initial092-evidence/。表中的 200K 兼容档仅降低 KV 显存预算至 0.88，保留 MTP／chunk／seq／dtype／DSA-CP；不是原始 .92 原封不动的性能数字。

兼容重测一度因监控扫描到自有容器内上轮残留 traceback 而提前退出，没有发出请求；已修正为重启前清空该私有 arm 日志，本地故障证据保留。没有修改生产源代码或将监控故障归为模型故障。

### 适用范围

单并发、固定数字答案、max_tokens=1 的 P-only prefill 时延；不代表并发吞吐、开放问答质量、完整 PD 数据面、新 receipt connector 或 1M 容量。相同 cold／remote 对使用完全相同 body／salt，原计划 40 对独立 key 空间；实际 37 对有两端结果，其中 36 对双端通过、1 对远端超时。另一次 B2 原混合序列冷请求受控取消，只有 start／fatal 记录；跨部署档保留配置差异。观测 CPU 标记有小额成本，未新增设备同步。

## 环境与产物

实验 r19，源分支 `p-main-buffer-reuse`，HEAD `717f6e6e0`，原 732 个 Python 文件不改；实际测试快照 733 文件。仅切换捕获身份的 P r05 和自有 r19，D r03、Mooncake Master 与生产 inventory 不改。所有 HTTP 从跳板机执行。退出后恢复原 P，核验四节点原 ID／image／script／各 10 个源文件 manifest、健康空闲、新 P0／P1 单 token 数学请求，测试容器停止。

证据目录：[mooncake-pool-deployment-comparison](../artifacts/p-main-buffer-reuse/mooncake-pool-deployment-comparison/)。关键文件：`PLAN.md`、`configuration-adjustments.json`、`profile-*.json`、`generated-*`、`requests-*.jsonl`、`save-ready-*.json`、`summary.json`、`results.csv`、`restoration-audit.json`。运行 `python3 artifacts/p-main-buffer-reuse/mooncake-pool-deployment-comparison/analyze.py` 重算汇总。

恢复审计 PASS：原 P r05／D r03 的 ID、image、脚本、各 10 个源文件、健康空闲与两个 P 单 token 数学题均通过；r19 已停止，proxy 维持未运行。


[证据与恢复汇总审计](../artifacts/p-main-buffer-reuse/mooncake-pool-deployment-comparison/completion-audit.json) 为 PASS，范围是记录／源码／恢复完整性；性能矩阵终态为 `COMPLETED_WITH_FAILURES`，B2/P1 的 64K 稳定性为 FAIL。没有将此审计表述为特性全绿。
