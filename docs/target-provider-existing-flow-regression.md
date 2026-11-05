# TargetProvider提交后的既有流程回归

2026-11-04；源码 `06e16d717b95f7720c0b85457351a6c27d30576c`。

**已测的旧 Mooncake layerwise P/D 流程通过，但整轮不能报告全绿：64K P独立检索出现错误答案。**
这次没有启用receipt模式，也没有真实TargetProvider owner runtime，不能作为新receipt传输的端到端验收。
原始证据在 `artifacts/p-main-buffer-reuse/target-regression/`，机器报告为 `candidate-audit.json`，其中 `passed: false`。

## 实际运行配置

- 四节点独立开发容器r08；使用当前源码的15个生产Python模块，启动和结束均核对逐文件SHA；无测试reader注入。
- P：TP16 × DP2、Main B2＋首尾独立、Indexer独立、INT8、chunk4112、max_num_seqs4、max_model_len262144。
- D：既有普通HBM INT8配置、5508 blocks；无Host offload。
- eager、无MTP、CP/DCP=1；旧Mooncake layerwise connector、D-first代理、原RoCE设置。
- Receipt配置键缺省。新TargetProvider及共享contract只由新模块自身引用，没有旧connector/runner的自动导入或构造入口。

本次候选取此前r03的短上下文配置；不是测试前P r05的chunk32784／1M配置。
耗时只是功能请求观测值，不是严格性能对比。

## 请求结果

| 场景 | 实际输入tokens | 结果 | 秒数 |
|---|---:|---|---:|
| P0独立prefill＋1token | 8188 | 数字检索正确 | 12.05 |
| P0独立prefill＋1token | 32765 | 数字检索正确 | 21.92 |
| P0同archive、改问BETA | 32766 | 数字检索正确 | 5.42 |
| P/D短请求 | 536／5017／9656 | 均正确概括KV对象存储，均连续输出15tokens | 13.30／6.27／8.24 |
| P/D三位置检索 | 32762 | `maple lemon quartz`全部正确，输出5tokens | 23.54 |
| P/D并发同前缀，两请求 | 各5004 | 均正确 | 5.34／25.23 |
| P/D重复前缀 | 5004 | 正确 | 4.01 |
| 关闭自身stream连接后的新请求 | 46 | 新请求正确，输出10tokens | 5.26 |
| P0独立64K检索 | 65535 | **错误：期望4，返回`!`** | 85.23 |
| P1重放同一64K提示词 | 65535 | **错误：期望4，返回`I`** | 10.15 |
| 恢复原r05后，P0重放同一64K提示词 | 65535 | 数字4正确；chunk32784，代码与配置均不同 | 21.23 |

64K提示词SHA为 `ae234ecd2afdd9a097fc115f97f35d6e0cc48a1ecf9822c9284c98360d8cc264`。
两次均HTTP正常结束、生成1token；请求完成不等于正确性通过。
P1重放会受到共享Store数据影响，不视为独立冷prefill或失败根因的隔离实验。
64K首个失败使原顺序runner停止；没有提交原计划的64K BETA测试，后续改用已成功32K archive验证前缀。
失败日志完整保留，没有通过重试覆盖失败记录。

## 指标与日志

候选结束时四引擎health均200、running/waiting均0、finished_reason=error均0。
D0／D1成功stop计数分别为7／1，证明两个D均实际收到并完成请求。
断连探针证明客户端关闭后新请求可完成；没有据此声称底层在途传输已被取消。

最终完整扫描r08引擎日志：P0约1.86GB、P1约154MB、D0／D1各约1MB；
未匹配ERROR、Traceback、Segmentation fault、malloc失败/损坏或double free。
正常Mooncake endpoint fallback等warning仍保留在容器日志中；无crash不能排除静默KV错误。

初次请求runner在Docker只读预检遇到SSH255／跳板复用broken pipe，当时尚未发送模型请求。
修复测试工具的逐命令SSH连接设置，禁用跳板与目标连接复用，再核对15模块SHA后开始请求；未因此重启引擎。
修改的三个工具Ruff lint及format检查通过。

## 结论边界及后续定位

可以确认当前提交在上述配置下，已测旧P/D持续生成、32K检索、并发、前缀和断连恢复正常。
不能确认所有既有功能无回归，也不能把64K错误直接归因于TargetProvider：尚无同提示词、同配置、仅切换该提交的A/B证据。
源码调用关系支持“TargetProvider尚未进入旧运行路径”，这只是静态证据。
恢复原r05后，同一个失败64K提示词正确返回4；其chunk32784、max_num_seqs1、模型上限1M、
MoE预算及代码版本均与r08不同，不能据此单独判定是chunk问题或某个源码提交的问题。

下一步应在相同配置下，对比`47f983157`／`06e16d717`及必要时更早的复用实现，
分别区分冷prefill、Store恢复和前缀缓存；保留prompt、block/range和槽位代次证据。
此前r05／r07的64K或1M成功不能替代本次失败样本的定位。

## 收尾

测试前P为r05、D为r03，proxy8000未运行。测试前记录了原容器ID、镜像和启动脚本SHA。
候选已完成请求并确认空闲后停止，原P r05／D r03的ID、镜像、脚本SHA和10模块manifest均已复核。
四引擎健康200，新8191-token请求正确；同一65535-token失败提示词在原r05也正确返回4。
附加请求后再次核对四引擎健康、空闲、错误计数0；proxy恢复为原先未运行状态。
最终证据为 `restoration-audit.json`、`restored-after-64k-metrics.json`。r08已添加本地退役标记，原服务配置未修改。
