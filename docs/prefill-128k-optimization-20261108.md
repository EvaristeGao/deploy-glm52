# 128K Prefill 定位与首轮优化验证

> 本文记录第一项32K候选的试验与回退；后续保留16K的CPU元数据优化见[prefill-metadata-optimization-20261108.md](prefill-metadata-optimization-20261108.md)，当前现场以工作总览及该项实时状态为准。

2026-11-08。**32K 候选未推广，已恢复原 16K P 和原镜像 D；八引擎健康空闲、KV归零，恢复8条全TP回收通过。**

32K能降低绝对耗时：128K/C30的P prefill下降19%、TTFT下降45%、吞吐增加36%；
但128K/C40遇到已提交Store前缀无法重新打开，P DP1退出，整轮无效。因此不能
宣称问题已解决，也不能把短输出或C30的收益直接推广到C40。

## 实验配置和边界

- P：e9bd1ce19，TP8×DP4，Main BF16/Indexer INT8，Main B2复用、首末层独立、
  eager、MTP1、seq8、gpu_memory_utilization0.75、max_model_len1048576。
- D：d634632f6，TP8×DP4，Host100GiB/DP，seq8/token32，AsyncReceiptScheduler，
  compilation mode3/FULL_DECODE_ONLY、MTP3，draft eager；D-first代理8333。
- 候选仅改P max_num_batched_tokens16384→32768，保留所有I1/reader/完成门禁。
  Ansible新建两个P容器，使用同一特性镜像
  `sha256:4c7a55de5cdc31eab28a8b97c8f7af099d30825957824d3de67e2e5f6180099d`；
  全镜像源码manifest通过，无诊断overlay/profiler。P0原Master/代理留在原容器。
- 同一v1.7 vendor SHA和7份客户端脚本，seed20261106；逐请求prompt SHA和实际
  input_lens完全匹配，盐值隔离，P跨请求prefix命中0。随机文本只核对SSE、usage、
  输出长度和回收，不做语义精度评测。
- 标注96K/128K为工具目标输入，实际token长度向量以记录为准。服务端P prefill为
  `first_token_ts - scheduled_ts`，包含分块交错等待，不能视为NPU净算子时间。

## 96K到128K为什么超线性

原配置C1、输出16、每点N4：P prefill10.098→14.983s（+48%）；C8/N16：
23.433→44.810s（+91%），两组P running峰值均6，D没有长生成负载。
40条全8-rank回收通过。另用新Store段从0开始、无新增驱逐/分配失败的C8复测，
仍为23.889→46.640s；所以驱逐不是这个性能差距的必要条件。

16K预算时约6→8个chunk，每次恢复历史KV，理想化历史恢复token累计
245760→458752（+86.7%）。TP0代表样本、4DP合计、每长度16请求实测GET
404.27→751.81GB，即每请求每TP rank代表采样约25.27→46.99GB；调用数
43608→80896，平均单次约0.969→1.014ms。大幅增长在次数与读量，而不是单次
调用延迟出现数量级变化。metadata、地址构造和range拆包也随历史块数增加。

实际设备证据支持多个增长项，尚不能把C30/C40全部额外延迟归到一条路径：

| 单TP0/C1诊断，16K chunk | 96K | 128K |
|---|---:|---:|
|SparseFlashAttention kernel累计(s)|2.764|3.698|
|两种Indexer kernel累计(s)|0.596|1.094|
|DispatchFFNCombineW4A8累计(s)|2.961|4.104|
|真实host step窗口(s)|11.537|18.656|
|窗口内CANN Computing union(s)|8.115|11.711|
|未与Computing重叠的通信union(s)|2.046|4.142|
|CANN Free union(s)|1.374|2.802|

SFA本体约+34%，Indexer约+84%。CANN allGather累计elapsed1.986→4.564s，
其中Transit0.286→0.376s，Wait1.690→4.167s；主要增量是等待，尚未指认具体慢rank。
operator累计跨流重叠，不能相加成关键路径占比。

GET host union2.341→4.332s，与CANN Free相交0.103→0.549s。进一步排除AICPU
GET/PUT，仅取实际AI_CORE/AI_VECTOR/MIX kernel并集，GET与model core执行时间
重叠2.168→3.508s，未重叠0.173→0.825s。证明确有大量时序重叠，不能把全部GET
当净损失；时序重叠本身也不证明没有带宽竞争或计算被拉长。attention gate的host
等待包含前层compute和collective，不是纯Store等待。reader及PD drain在该C1/C8
诊断中很小，但不能外推为长输出高并发下的占比。

16K C8每长度16请求、4DP代表TP0的thread CPU累计：build_addrs10.06→19.11s，
range拆包10.87→21.61s，main metadata3.19→7.85s。当前64 keys/16MiB限额下，
代码仍重建大量嵌套列表；地址计算逐block调用Numpy。这些是后续代码优化候选，
本轮未修改生产实现，也没有移除保护门禁。

## 32K短输出单变量对照

| 输入/并发/N | P prefill均值：16K→32K(s) | TTFT P50：16K→32K(s) | P prefill变化 |
|---|---:|---:|---:|
|96K/C1/N4|10.098→9.486|11.993→11.491|−6.1%|
|128K/C1/N4|14.983→12.781|16.990→14.847|−14.7%|
|96K/C8/N16|23.433→20.230|37.201→26.839|−13.7%|
|128K/C8/N16|44.810→32.104|71.574→44.251|−28.4%|

48条含8预热，全部SSE/长度及全8-rank receipt/释放通过，抢占0，结束队列/KV归零。
C1的96K→128K增长约48%→35%，C8约91%→59%，仍有剩余非线性。
现有INFO快照日志确认C1最终generation3/4；混合调度会增加chunk，40条长输入
最大generation分布96K {3:8,4:12}、128K {4:8,5:12}。没有新增worker日志开关。
vLLM iteration histogram在此P路径按最终输出更新，count不能当实际chunk数。

## N80、2K输出正式结果

| 输入/并发 | 完成 | P prefill均值(s) | TTFT P50(s) | TPOT P50(ms) | 输出吞吐(tok/s) |
|---|---:|---:|---:|---:|---:|
|96K/C30|80/80|30.333→21.343|87.285→29.963|40.196→50.693|307.715→381.032|
|128K/C30|80/80|47.883→38.714|211.980→116.786|28.104→37.526|198.655→269.344|
|128K/C40|失败，57完成|不报告|不报告|不报告|不报告|

两个有效cell的160条从重启前D日志归档通过全8-rank receipt/释放；输入和客户端
字节匹配，抢占0、结束队列/KV归零。96K/128K C30的D running峰值17→26、8→16，
MTP每draft接受token2.139→2.193、2.204→2.175。D更忙，TPOT增加约26%/34%；
整体吞吐和单请求解码时延有取舍，不能只报TTFT。

**完整负载96K→128K的P prefill增长，32K下仍约81%，原16K约58%。** 96K收益
更多，因此绝对时延改善不代表相对增长已收敛。单次N80闭环负载含起始/排空，
到达率也随D完成速度变化；不外推为稳态容量或统计显著性。C1单rank轨迹不能
完全解释持续长输出下的P/Store/PD竞争，32K不是完整修复。

## C40故障与恢复

05:47:40 UTC，P0/DP1/TP0在`_open_main_reuse_get_sessions`报
`Main reuse could not open the complete committed Store prefix`；05:47:52该DP
退出，其他P卡在EP/逐层load。57条完成后，23条没有first token，最终人工停止
本轮负载。只看CHECKED的已完成失败数会漏掉这种情况；监控已补入first-token
计数和采样API错误，不能再将“已完成0错误”视为整轮健康。

故障前Master32段均在、分配失败0、占用约80%，驱逐持续增加；P退出约30秒后
才掉到24段，因此段丢失是后果。错误未记录具体失败key/后端返回码，不能断言
必然是驱逐。需排查活动Main前缀的生存期保护、提交到首次读的窗口及RPC失败。
本地固定SDK7bfe1cc中PutEnd授予0ms读租约，BatchGetReplicaList才授予配置的
300000ms租约：这是需要审查的保护边界，不是本次失败key的充分因果证据。

已用Ansible停止CPU负载与候选P。D取消后队列虽清零，但部分目标KV保留；确认
P写者完全停止后，归档全部候选P和原D日志，重启确切ID的两个项目D容器，保持
原0ac34e9镜像/d634632f6/脚本，并恢复容器私有`/dev/shm huge=always`。
原P容器恢复16K引擎，镜像e9bd1ce19不变；原Master/8333代理/init PID与StartedAt
不变。D此次StartedAt已改变，不沿用故障前“D未重启”的结论。

恢复后8条请求成功、四D全8-rank receipt/释放通过；四节点原容器ID/镜像/源码
manifest、NPU进程归属检查通过；八引擎和代理健康，running/waiting/KV全零。
两个候选P及CPU客户端已停止。未改主机全局参数、无关服务或生产vLLM源码。

## 配置与证据

`ansible/inventories/a3-layerwise-guide/prefill-32k.yml`仅为**未推广的实验参数片段**，
不选择镜像或D graph。P-only本地gen与shell语法检查通过；默认inventory未改，
本轮实际切换用冻结P镜像/脚本的独立Ansible，恢复后仍用16K。
P容量从1,905,024降到1,753,216 token/引擎（约−8%）；1M配置启动检查通过，
没有重跑32K下的完整1M请求。

原始材料：`artifacts/prefill-128k-20261108/`。

- `before-summary.json`、`phase2-summary.json`、`after32-short-comparison.json`。
- `after32-completed-comparison.json`：两个有效C30，逐条hash/长度及7客户端文件匹配。
- `after32-short-audit.json`（48）、`after32-completed-audit.json`（160，重启前归档）。
- `results/restored/`：完整客户端材料，含失败C40部分记录和恢复8条。
- `device-summary.json`、`device-host-intersections.json`、`math-get-overlap.json`、
  `communication-summary.json`及相应复算脚本；完整原始NPU/host轨迹另存归档。
- `c40-prefix-failure-context.txt`、`after32-master-series.jsonl`、
  `c40-failure-engine-logs.tar.gz`、`recover-decode.yml/.log`。
- `restored-audit.json`和`recovery-final-verification.json`：最终原配置恢复证明。
- `controller-state.json`：`restored_original_P`；`finish.json`为restore，非keep。
