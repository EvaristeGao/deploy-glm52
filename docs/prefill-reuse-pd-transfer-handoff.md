# Prefill reuse／PD transfer 最新交接入口

2026-11-05。原功能目标已暂停；本轮仅整理交接，没有恢复开发、部署或模型测试。

主文档：[项目交接](../src/vllm-ascend-reuse/PROJECT_HANDOFF.md)。
它位于源码仓，便于随源码交给 reviewer／接手开发者，包含：

- 完整目标、用户约束、双方实现、提交历史与交付文件范围。
- P 共享槽、Store、SourceReady writer、D 目标、receipt／可见性和回收时序。
- 已有 CPU、真实依赖、P 独立 1M、TP1 原生探针的证据及边界。
- 六项明确功能缺口、后续实施顺序和正式完整模型验收条件。
- Mooncake 池化五部署档性能及归因限制。
- 当前四节点／容器状态、安全操作规则与证据保存清单。

生产实现基准：`p-main-buffer-reuse` 的 `4204f545d`。新增原生探针和交接说明随本次整理提交，未 push。
当前原四引擎健康空闲，r19／r20／r21／r22 候选均停止，代理未运行；详细身份见
`artifacts/p-main-buffer-reuse/pd-transfer-handoff-20261105/runtime-audit.json`。

`src/` 和 `artifacts/` 不随部署仓自动提交。交接时需要同时保留源码工作区、部署配置和主文档列出的证据目录。
根 AGENTS.md 和部分历史 handoff 含旧分支／旧部署描述，应按主文档中的固定快照和最新审计辨别，安全约束仍适用。
