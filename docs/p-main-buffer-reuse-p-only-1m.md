# P 侧独立 1M prefill：配置、验证与当前状态

更新：2026-11-03。**P侧独立1M prefill及最终审计已通过：完整1048541-token冷prefill约56.3分钟，中后部前缀复用检索均正确。**

## 范围与实现位置

本轮验证完整长输入prefill与首token产出，使用P侧Main shared-buffer reuse和Mooncake Store。
请求直接发P0:9081，不经过proxy，不提供remote-decode参数，没有P/D KV传输。
P使用两节点TP16 × DP2、EP跨32卡，独立指不依赖D，并非单机。完整1M请求发往P0，P1参与EP。
两个D保留r03，仅观察指标；本轮不开发或替换同事负责的P/D connector。
原MooncakeLayerwiseConnector保留在P配置中用于已有release接口接线，独立请求没有远端读者。

源码：`src/vllm-ascend-reuse`，分支`p-main-buffer-reuse`，功能提交`9824ec91c`，
HEAD `f9b995c68`仅追加格式整理；本轮未修改生产模块。评审入口见
[代码交接](p-main-buffer-reuse-handoff.md)。旧`src/vllm-ascend`保持原状。

## 当前运行配置

| 项目 | 值 |
|---|---|
| 容器／镜像后缀 | `r05`，仅P0/P1运行；D仍为r03 |
| max_model_len | 1048576 |
| max_num_seqs | 1 |
| max_num_batched_tokens | 32784，TP16可整除，每chunk保留非整block尾部 |
| KV | Main INT8、Indexer INT8 |
| Main布局 | 2个共享槽＋首尾独立，总4物理槽；Indexer 21份私有 |
| 执行 | V1、eager、PP=CP=1，无MTP/speculation |
| 内存预算 | 自动blocks，gpu_memory_utilization=0.9，无block强制覆盖 |
| 已观察容量 | P0 TP16均30013 blocks；P1为30010 blocks |
| Store／协议 | Mooncake layerwise，保留RoCE配置，load failure为fail |
| 模型别名 | `/root/reuse-main-20261103-p1m-model`，隔离旧Store keys |

新档profile记录权重约15.22GiB、峰值activation约15.85GiB、non-torch约2.67GiB，
物理KV预算约21.26GiB。日志中的约195GiB是放大后的逻辑预算，不是实际HBM占用。

`mega_moe_max_tokens=524544`实际作为旧`dispatch_ffn_combine`的`max_output_size`传入，
控制其workspace。本档`enable_fused_mc2=1`会关闭MegaMoe，不能把它描述成MegaMoe运行。
镜像中相关Python模块SHA已与本地源码复核一致；计算与记录见`chunk32784-moe-budget-review.json`。

## 已通过的真实请求

所有请求都校准实际输入tokens，生成1个数字，并校验指定位置的随机数字正确。
下表均为ALPHA（约输入10%处）检索；新nonce确保这些请求不命中历史请求前缀。
原4112档用于性能基线，当前32784档已重新验证。输入长度相差几个token且nonce不同，
耗时仅用于本轮粗略运行对比，不是通用性能承诺。

| 目标tokens | 当前档实际输入 | 当前档秒数 | 原4112档秒数 | 检索 |
|---:|---:|---:|---:|---|
| 8192 | 8188 | 3.80 | 12.13 | 通过 |
| 65536 | 65533 | 21.40 | 87.16 | 通过 |
| 131072 | 131065 | 57.16 | 353.77 | 通过 |
| 262144 | 262139 | 209.29 | 1424.30 | 通过 |
| 524288 | 524285 | 813.41 | — | 通过 |
| 1048544 | 1048541 | 3375.79 | — | 通过 |

首个约1M请求：目标1048544，实际1048541输入tokens，3375.79秒，1输出token，前部数字7正确。
`first-1m-live-metrics.json`记录该轮历史前缀命中为0，D0/D1成功和运行请求均0。

## 前中后三个位置的验证方式

首个1M请求使用新nonce完整计算，检索ALPHA。随后显式复用同一个archive，
只把最后的问题改为BETA（50%）或GAMMA（90%），校验长前缀恢复后的检索。
这两次不是新的独立完整重算；实际缓存命中必须由prefix hit指标确认。

`--reuse-prefix-from`要求源请求真实完成、输入长度和答案正确；重建原prompt的SHA及token数
必须与源记录相同，才允许改变最后的问题。64K archive的纯分词重建检查已通过，
未完成archive、答案错误但标记通过等记录均被拒绝。`--calibrate-only`只做分词，不执行模型。

后续检索均已通过：

| 位置 | 实际输入tokens | 秒数 | 答案 |
|---|---:|---:|---|
| BETA（50%） | 1048542 | 189.36 | 1，正确 |
| GAMMA（90%） | 1048542 | 189.54 | 3，正确 |

本地prefix hits累计从0增至1048448、2096896，external prefix hits保持0。
这是同一archive的缓存恢复验证，不是三次独立冷prefill。Main仍需逐层从Store恢复。

最终审计：P0/P1/D0/D1健康200；P无运行／等待请求、错误计数0，D成功请求计数均0。
P0新8189-token请求与P1新8187-token请求答案正确。P1首次检查SSH退出255、答案未采集，
未记为通过；确认引擎健康空闲后，仅重试客户端即通过，没有重启模型。
两个P从本轮启动偏移扫描至日志EOF，未发现ERROR、Traceback、Segfault或malloc错误。
镜像10个生产模块SHA匹配构建清单；两节点运行模块与当前源码HEAD在同一Python解析器下AST一致。
请求审核、运行审核、生成模板SHA和源码审核分别保留原始证据，不以配置代替实际通过。

## 产物与复现入口

均位于当前仓库；`artifacts/`被gitignore，复制部署仓库时需另外保留相关产物。

- 已纳入版本管理的配置：`ansible/profiles/p-main-buffer-reuse-p-only-1m.yaml`；冻结副本为`artifacts/p-main-buffer-reuse/profile-r05-chunk32784.yaml`。
- 当前脚本：`artifacts/p-main-buffer-reuse/generated-r05-chunk32784/`。
- 原档：`profile-r05.yaml`、`generated-r05/`，保留未覆盖。
- 镜像构建：`image_build/reuse_p_1m/Dockerfile`；基于r03仅添加模型路径别名，不改SDK或生产模块。
- 原档证据：`one-million/p-only-r05-8k.log`、`p-only-r05-64k-256k.log`。
- 新档证据：`one-million/chunk32784-v2/alpha-*.log`。
- 1M前缀复用：`one-million/prefix-replay/`及`prefix-replay-controller.log`。
- 结构化进度：`one-million/p-only-capacity-progress.json`。
- 结果核对：`one-million/result-audit.json`，6档冷prefill和2次前缀复用检索通过；最终审计`completion-audit.json`。
- 源码等价：`one-million/source-runtime-ast-equivalence.json`；模板复现：`release-template-check.json`。
- 交付包：`handoff/p-only-1m-review.tar.gz`，文件SHA清单见同目录`p-only-1m-manifest.json`。
- 设备快照：`one-million/first-1m-live-device0-usages.txt`，该时点两个芯片HBM占用94%–95%，
  包含缓存与运行时占用，不等同于激活占用或整轮峰值。

所有相对artifact路径均以`artifacts/p-main-buffer-reuse/`为根。
探针示例（会提交真实P请求，只在P空闲时运行）：

```bash
REUSE_RUN=r05 python3 tools/reuse_long_context_probe.py \
  --target p0 --prefill-only --tokens 8192
```

本轮长请求全部结束；以上命令会产生新流量。切配置使用owner/基线ID检查的容器工具，
不使用通用start.yml；profile的render-only保护仍保留。没有主机／设备reset或系统服务变更。

## 运行与清理

仅操作带owner标签的开发P容器。配置先由Ansible生成并检查脚本；
`tools/reuse_container_experiment.py`使用`--nodes p0 p1`限定范围，
`restart-prefill`搭配`--no-proxy --prefill-template-dir artifacts/p-main-buffer-reuse/generated-r05-chunk32784`。
具体参数先看`--help`，不通过通用start.yml重部署D。新profile的render-only保护保留。
从纳入版本管理的profile重新生成P脚本，与冻结候选和实际容器脚本SHA逐字节一致。

本地旧调度器已在全部请求结束、子进程退出后清理，`controller-cleanup.json`记录终态。
集群模型进程未收到这些信号；没有主机／设备reset或系统服务变更。
完整原生日志保留在开发容器`/root/reuse-main-20261103/logs/vllm_{node}.log`，
体积较大；交付包保存扫描偏移、EOF和结果，不复制几十GB日志。

## 已知边界与历史失败

P独立持续decode未验收。此前r03的8185-token/32输出请求触发
`Main reuse could not open the complete committed Store prefix`，原始证据
`direct-p-r03-8k.log`、`direct-p-r03-8k-errors.txt`保留；不能将本轮prefill+首token通过
推广到持续decode。其具体触发阶段未完成独立确认。

r04的SFA/MemFabric P/D实验偏离目标路线，已停止；它的崩溃不是原Mooncake P/D失败的证据。
完整历史见[runbook](m1-buffer-reuse-runbook.md) §11.29–11.30及
[此前1M实验记录](p-main-buffer-reuse-1m-validation.md)。
