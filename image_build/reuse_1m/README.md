# 历史 1M 容量实验兼容镜像

该目录保留早期实验构建材料。脚本仅补充已安装 MemFabric wheel 的 Python enum 导出，
不初始化设备；Dockerfile 使用 r03 并增加隔离模型路径。

当前 P 独立 1M 已验证镜像流程见相邻 `reuse_p_1m/`。本目录不是新 Mooncake receipt
特性的部署入口，也不包含其剩余功能；不要将该兼容镜像视为新完整 PD 已验收的镜像。
