# P Main reuse 的 1M 验证

> 本文为已停止的r04 SFA历史实验。当前P独立1M已通过，见[当前验收](p-main-buffer-reuse-p-only-1m.md)；P/D端到端1M仍未验收。

历史状态：2026-11-03，D Host offload已实际启动，但首次短请求触发MemFabric DEVICE_RDMA原生崩溃；1M端到端未通过。r04已发起停止，准备恢复r03。

D 侧已有 `SparseKVOffloadManager` 和 `SfaRemoteD2HConnector`，不是待开发能力。
r03 的 D 配置未开启 Host offload，5508 个 block 的容量不足不能用于判断该能力不存在。
本轮使用现有 MemFabric layerwise P/D 路径与 Host offload，P 继续使用 Main 共享槽。

验证配置位于 `artifacts/p-main-buffer-reuse/profile-r04.yaml`，生成脚本位于 `generated-r04/`。
模型上限 1048576；P 共享 buffer 2、首尾独立，chunk 4112；P/D Main BF16、Indexer int8。
D Host 预算 256 GiB，topk buffer 2048，关闭 device Main 常驻和 fused overlap。
现有 SFA offload 不接受 C8/TQ4 Main，因此这里不能沿用 r03 的 Main INT8 配置。
传输使用 MemFabric device_rdma；P/D 缺失 KV 均 fail，不允许静默 recompute。
模型路径别名隔离旧 INT8 Store key，原 master 未变更。

四节点 r04 镜像基于 r03，10 个 vllm-ascend 生产文件 SHA 校验一致。
唯一 SDK 修改：把已存在于 `_pymf_transfer` 的 `TransDataOpType` 从
`memfabric_hybrid` 公开导出，修复安装的 1.2.0 包与适配器导入不匹配。
构建脚本见 `image_build/reuse_1m/`；构建时仅编辑 Python 文件，不初始化设备。
四节点镜像和枚举导入校验结果均保留在 `artifacts/p-main-buffer-reuse/one-million/`。

r03、旧开发容器和原基线均停止保留；r04 独占设备。操作仅涉及开发容器和本地文件，
没有执行主机重启、设备 reset 或系统服务变更。

后续先检查实际缓存规划与短请求，再用 `tools/reuse_long_context_probe.py` 分级验证。
工具从真实 `/tokenize` 校准输入长度，记录实际 usage，并在上下文前中后放置随机检索词。
长请求存在 metaserver read timeout 600 秒的现有边界，需按实测耗时评估，尚未修改。

首轮实际规划：D final=8193 blocks，Host 87.76 GiB、device 3.28 GiB，容量覆盖 1M；P BF16 共享布局自动39085 blocks。
D 首个错误是缺少 `sparse_kv_offload.cpp`，与显存不足无关。已确认该文件相对4234a1f18无修改，
仅补入 d0/d1 的 r04 容器；修复记录见 `one-million/cpp-packaging-repair.json`。
当前容器因此比镜像多一个基线 C++ 文件，最终可复现镜像需包含该文件，不能只依赖原 r04 Dockerfile。

第二轮：C++ JIT编译已通过，但 MemFabric Shared DRAM 初始化失败，原生日志明确缺少
`libmf_hybm_accoffload.so`，SDK目录、容器常用路径和主机安装目录均未找到。
现有 Mooncake Host 后端的 `shared_segment_supported(mmap=True, host_register=True)` 探测为 True。
转为验证 `host_backend=mooncake, use_fused_overlap=true`；P/D传输仍为SFA MemFabric，二者是不同后端。
原配置保存为 `profile-r04-memfabric-host.yaml`，新生成脚本在 `generated-r04-mooncake-host/`。
尚未验证这条组合的端到端可用性。

第三轮：两个D节点成功完成约88GiB Mooncake Host pool创建、注册和模型warmup，API启动。
首个约535-token短请求失败（客户端32.11秒收到空/非JSON响应）；D收到78层MF_META后，
原生 `device_rdma_transport_manager.cpp:611 RaInit` 返回328002，随后出现Segfault。
P在共享槽上等待D读完成，未观察到绕过屏障写入；随后停止r04以终止等待。
不能据此断言D算法未实现，也不能报告1M可用：当前阻塞点是所选MemFabric传输路径的运行时兼容性。
未执行主机reset、驱动/系统服务修改，未下载安装来源不明的原生扩展。
关键证据：`runtime-d0-r04.log`（约4994行起）、`one-million/smoke-r04-mooncake-host.log`。

回退状态：r04 p0/p1/d0已exited；d1 docker stop报“tried to kill container, but did not receive an exit event”。
再次inspect仍running，docker top仅一个Zl僵尸worker。未启动r03，避免与残留容器重叠。
当前整套服务未恢复，master未动。未执行host reset/reboot/daemon restart/手动进程kill。
状态证据 `one-million/final-container-states.json` 与 `d1-teardown-processes.txt`。
恢复前必须先确认d1容器退出及设备释放；如需主机层处理，遵守AGENTS.md先取得用户授权。

后续状态更新：d1自行完成退出，复核四个r04均exited，随后已启动原r03四节点与proxy回退。
未进行额外主机处理；回退启动日志 `one-million/restore-r03.log`。健康恢复尚待检查。

交接复核：r03四引擎健康均200，恢复完成；尚未重新进行请求回归。
进一步读取plog发现RaInit告警后仍有mock_comm初始化和RaQpCreate调用，
因此328002仅为崩溃前现象，不能把它直接定为Segfault根因。底层日志归档在 `one-million/diagnosis/`。

诊断进度补充：已从停止的r04容器读取并归档P0/D0原始plog（仅docker cp读取）。
D0 TP0在07:38:39 RaInit告警后，07:38:42仍成功初始化mock_comm_1，
最后记录为07:38:42.975 RaQpCreate(qpMode=4)，随后服务日志出现Segfault。
上游MemFabric源码也存在RaInit非零后建立mock HCCL组获取RDMA handle的fallback，
但已下载源码为master(004d931)，不能直接当作安装1.2.0的精确代码。
当前待核实候选为该fallback的RDMA handle／QP创建兼容性及通信库加载组合，尚未确认根因。
已读取r03存活进程maps作为对照，其中同时映射MemFabric hashed/unhashed两份core库；
这是诊断线索，不是充分因果证据。未启动新设备实验或切换服务。

路线澄清：r04由本轮验证主动把P/D connector从MooncakeLayerwiseConnector换成SfaRemoteD2HConnector，
后者使用MemFabric DEVICE_RDMA。P Store仍是Mooncake，D host_backend=mooncake仅指Host内存分配，
不代表P/D传输仍为Mooncake。这一替代路径的Segfault不能作为原Mooncake P/D路径失败的证据。
本项目主路线仍为Mooncake layerwise + RoCE；后续应先核对该connector与已有D Host offload的接入，
不继续把修复MemFabric替代路径当作主线必需工作。当前运行r03使用原Mooncake layerwise路径。
