# 64K错误答案定位

> 2026-11-04：用户要求暂不继续定位；保留未解决项与证据，后续先整理P／receipt两分支实现。本记录不表示问题已修复。

2026-11-04。本轮隔离验证已完成，原服务已恢复；**具体数值算子根因仍未确认，没有生产修复**。
原始证据：`artifacts/p-main-buffer-reuse/k64-diagnosis/`；此前失败记录保留在`target-regression/`。

## 已有事实

此前r08、源码`06e16d717`、P chunk4112／max_num_seqs4／max_model_len262144的65535-token请求，
预期ALPHA数字4，P0返回`!`，P1重放返回`I`，没有Python异常或原生crash。
恢复原r05后同一提示词正确，但r05的代码、chunk32784和其他预算同时不同，不能用作单变量归因。

提示词SHA：`ae234ecd2afdd9a097fc115f97f35d6e0cc48a1ecf9822c9284c98360d8cc264`。
本轮所有“同一提示词”都重建并核对这个SHA及实际token数。

## 无探针重放

r09继承r08，15个生产Python模块逐字节相同，没有生产源码修改、receipt接入或测试reader。
首先保持4112／4／262144和原MoE预算8192，使用新Store命名空间`reuse-k64-low`避免旧KV干扰。
原P r05在排空、核对ID／image／脚本SHA后停机保留；D r03及Master不动，没有proxy或P/D请求。

| 样本 | 实际输入tokens | 结果 | 秒数 |
|---|---:|---|---:|
| 原8K样本，P0 | 8188 | 3，正确 | 12.09 |
| 原32K样本，P0 | 32765 | 6，正确 | 22.00 |
| 原64K失败样本，P0 | 65535 | 4，正确 | 83.72 |
| 同64K样本，P1 Store恢复 | 65535 | 4，正确 | 19.92 |

原错误没有在这次重放中出现。不能据此删除失败记录或声称问题消失。

## MoE容量假设与实测

8192曾被列为候选：当前`enable_fused_mc2=1`回退到旧`dispatch_ffn_combine`，
`mega_moe_max_tokens`作为它的`max_output_size`传入；W4A8 kernel有明确的容量截断逻辑。
这不是CANN MegaMoe运行，也不是Main KV共享槽容量。

但参数存在风险不代表这次已经超限。为验证，构建单独的r10诊断镜像：

- 只在KV注册处增加两行安装hook，另加`_k64_mc2_probe.py`；16模块manifest明确包含测试改动。
- hook先调用原`FusedMC2CommImpl.fused_experts`，再读取已有`expert_token_nums`，不修改输入、权重或输出。
- W4A8 kernel把跨EP累加的逐专家原始数量写入这个输出，之后的GMM按容量截断，因此可检查截断前负载。
- CPU读回会增加同步，诊断镜像的时序和性能不能冒充无探针版本。
- 每个文件在请求开始前记录读取偏移，只收集该请求之后的新增记录。

同一65535-token请求在诊断版返回4，97.66s。
共70620条记录，包括单token dummy调用；剔除input_tokens=1的记录后，
**32个rank各完整覆盖15个257-token分片chunk和1个241-token尾chunk，每chunk75个MoE层，共38400条prefill记录。**
最大实际接收量5230，配置8192，超过容量的调用数0。

证据：`counter-audit.json`、`counter-records-{p0,p1}.jsonl.gz`、`counter-exact-64k.log`。
当前没有实测证据支持“原64K失败由MoE容量截断引起”，没有以增大这个参数作为修复。
此结果只约束这次请求，不保证8192适用于其它数据分布或并发负载。

## 原命名空间与新冷样本

回到无探针r09，恢复最初r08的模型别名`reuse-main-20261104-regression-model`，
P0/P1生成后的vLLM argv与原失败档完全一致（additional-config JSON做格式归一后比较）。
预算保持8192，chunk保持4112；原8K／32K／64K仍通过，64K为85.12s。
两个P新增4个不同nonce的64K冷样本均正确，首token logprobs一并保留；模型别名本身未重现问题。

## 不共享Main时出现NaN

保持同一r09源码、INT8、4112、max_num_seqs4、MoE预算8192，将independent_layers设为all。
此时layout.has_layer_reuse=false，78层Main各自独立；固定4096 blocks容纳64K，避免普通全层缓存占用过多显存。
因此它是功能对照，不是严格的同容量性能对比。Store仍开启，冷请求没有外部前缀命中。

原8K／32K／64K和前两个新样本通过；原64K为21.86s。
重放第3个新样本时，输入65532 tokens、期望ALPHA=1，开启logprobs后的响应为HTTP400：

`Out of range float values are not JSON compliant: nan`

该样本SHA为`4c53c93760ea259f22256dd35681a70e47de4f72d0419a01dec75d088150e206`，
在共享槽模式下此前回答1正确，且首token概率接近1。
证据：`noreuse-fresh-2-p0.log`、`namespace-fresh-2-p0.log`。
请求结束后四引擎健康且空闲；`noreuse-nan-native-excerpts.json`没有指定异常匹配。
这表明“健康200／模型error计数0”不能排除输出数值异常，也证明该异常可以在没有跨层共享槽时发生。
尚不能把它直接归因于某个计算算子：此对照仍有Store PUT，也可能是logits／logprobs处理的问题。

关闭logprobs重试同一提示词返回1、耗时0.33s，但属于前缀命中后的短重算，不是完整冷prefill通过的证据。
词表token0经API detokenize的结果单独记录在`token-zero.json`；最初`!`是否与NaN采样有关仍需证据。

## 纯计算也复现NaN，继续追踪首个非有限值

r11移除P侧整个kv-transfer-config，不启用Store或P/D connector，保持普通4096-block缓存、相同计算设置。
诊断镜像仅在model_runner初始化KV处安装测试hook（不改生产源码工作区），记录Norm／Attention／MLP及
fused MoE输入输出中的首个非有限值；使用实际token数量及MC2 mask排除padding，发现异常也不在单rank抛错。
启动及首轮请求时数值检查关闭，原8K／32K／64K及4个新64K样本全部通过。
随后对先前NaN样本使用每次不同的cache_salt，保持同一提示词SHA和65532-token数，
三次完整冷重算中前两次正确，第3次再次HTTP400 JSON nan；每次本地前缀命中增量均为0。
证据：`finite-pure-salted-repeat{0,1,2}.log`、对应metrics和`finite-request-summary.json`。
**因此新捕获的NaN异常在没有Store／P/D传输／跨层共享时也会发生**，其必要条件不包含上述路径。
这仍不证明原r08的错误答案一定来自同一个数值问题，也没有定位到具体算子。

实际API的reset_prefix_cache端点返回404，测试脚本停止且保留错误；未启用额外服务接口。
改用实际OpenAPI支持的cache_salt，并核对prefix hit delta，保证完整重算。
NaN复现后才在排空状态启用逐层数值检查，继续重放。探针会改变时序，不能替代检查关闭时的证据。
3次启用同步检查的冷重算均通过，未记录first_nonfinite。
因此又准备r12：各层只在设备上计算NaN／正负Inf标志，不调用item／cpu；
在model forward和logits输出边界统一读回，并记录checked_batch覆盖信息。
logits允许合法负Inf，但检查正Inf和NaN。该方案仍有额外kernel和边界同步，不能视为完全无干扰。
`finite-install-audit.json`逐rank核对r11实际KV transfer关闭、4096 blocks，共32个worker。
r12在P0/P1交替完成8次相同提示词的冷重算，全部正确、prefix hit delta均为0。
32个rank共4096个检查批次、2387968次张量检查，未记录first_nonfinite；证据`deferred-audit.json`。
此结果不能确定原异常为竞态，或排除探针对分配／时序的影响。

r13补充采样边界检查：不注册模型层hook，在原log_softmax执行之后读回原始logits与计算后的logprobs，
并记录实际选择的token和最终logprobs；没有在模型forward中新增检查kernel。
若捕获异常则将CPU快照保存在自有容器日志目录，避免仅凭API报错猜测发生阶段。
本轮12次P0/P1交替冷重算均正确：前5次逐chunk采集，后7次按请求客户端start+18秒打开采集，
实际覆盖末尾1–2个chunk，避免前面chunk的检查／读回干扰。32 rank共1408对compute/output记录，
未捕获NaN／正Inf，CPU argmax与实际采样token一致；证据`sampler-audit.json`及`sampler-tail-gate-events.json`。
随后关闭采集，2次普通和4次流式冷重算均正确，后者包含2次原65535-token失败提示词，
SSE原文与chosen logprob保留且为有限值；证据`wire-audit.json`、`wire-no-probe-repeat*.log`。
这些后续通过不撤销此前纯计算NaN的失败记录；尚不能确认它与r08错误答案同因。

## 操作与收尾约束

只操作带owner标签的r09/r10/r11/r12/r13开发容器，不重启Master或D，不做host/device reset。
原P容器ID、镜像与脚本保留在`previous-containers.json`，结束前需恢复并通过新请求和健康检查。
已按原ID／image／owner／启动脚本SHA恢复P r05与D r03，四节点10模块manifest均通过。
D全程未重启、成功请求计数仍为0；四引擎健康、空闲、error计数0，proxy保持原先未运行状态。
恢复后P0原8188-token样本及P1新NaN复现用65532-token样本冷请求均正确，prefix hit delta为0。
r09/r10/r11/r12/r13均停止保留，诊断镜像标记退役，生产源码06e16d717保持干净。
证据`restoration-audit.json`与`restored-r05-requests-summary.json`。
