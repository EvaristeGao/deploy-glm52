# A3 Prefill Main KV buffer reuse 使用说明

配置入口是 [group_vars/all.yml](group_vars/all.yml)，主机入口是 [inventory.yaml](inventory.yaml)。
它从原 `a3` 配置复制并精简，只保留 P 侧 Main 共享 HBM 槽、Mooncake Store 和旧 layerwise P/D 流程。
直接修改 `pd_cluster`，不再选择 `200k`、`1m-v030-*` 等档位。

## 1. 版本与适用范围

默认镜像固定到目前恢复使用的 Main reuse 实现：P 使用 r05、D 使用 r03，名称在 `engine_images` 中。
这是各节点本地 Docker tag，不是可从公网拉取的镜像。配置中的 `image` 会按 P/D 角色选择。
P r05包含已验收的 Main reuse 代码；D r03保留普通 HBM KV，不开启 D Host offload。
换镜像时必须确认实际安装的源码，不能用原始rc1或旧`dsabase-m1`镜像仅加几个参数代替。

本 inventory 默认使用已验证的普通 layerwise P/D。实验用的 receipt 配置见第 8 节。

| 项目 | 本配置 |
|---|---|
| 节点 | P0/P1：ott9-a31/32；D0/D1：ott9-a33/34 |
| 并行 | 两侧均TP16 × DP2，每节点1个引擎；CP/DCP保持默认1 |
| 缓存 | Main与Indexer均INT8，只有P的Main跨层共享，Indexer独立 |
| Main物理槽 | 2个共享槽＋首尾2个独立层，共4槽 |
| P调度预算 | `max_num_batched_tokens=32784`、`max_num_seqs=1` |
| 模型长度上限 | 两侧262144；这是配置上限，不是本配置已验收262K端到端的声明 |
| 执行 | eager、无MTP/speculative；P的KV加载失败策略为fail，D沿用现有模板的recompute |
| 通信与服务 | RoCE、旧MooncakeLayerwiseConnector＋AscendStoreConnector、D-first代理 |
| Store | p0单Master，RPC52050；client各贡献40GB，lease TTL300000ms |
| API | P9081、D9900、p0代理8000 |
| 新容器 | `vllm-ascend-glm52-main-reuse` |
| 日志目录 | `/mnt/sfs_turbo/g00832294/logs/main-reuse/` |

32784来自此前P独立1M验收档，保留其MoE预算524544。4112是额外partial-block边界测试档，不作默认性能配置。
`max_num_batched_tokens`限制每个调度step的新token预算，不是整条请求的总长度。
增加它通常减少chunk数，也会改变workspace与显存开销；调参后需重新验证。
`max_num_seqs=1`限制每个P引擎的并行序列数；不要把以前并发4序列的容量/性能直接套过来。

本精简文件已经过本地模板生成与语法检查，**尚未按此完整新inventory实机部署验收**。
四节点按角色选择的本地镜像已通过只读查询；四份生成脚本的Shell语法、连接器及additional-config JSON均已检查。
它采用当前运行档的P调度参数，将共同模型长度限制为262144以匹配普通D配置。
既有P独立1M证据见 [验收记录](../../../docs/p-main-buffer-reuse-p-only-1m.md)，不代表P/D 1M。
最新候选r08的4112档有64K错误答案，见 [回归报告](../../../docs/target-provider-existing-flow-regression.md)；不推荐拿r08作服务镜像。

## 2. 控制机准备和只读检查

以下命令从部署仓库的 `ansible/` 目录运行：

```bash
cd /home/gao/code/Ops/deploy-glm52/ansible
export ANSIBLE_LOCAL_TEMP="$PWD/.ansible-tmp"
export INVENTORY=inventories/a3-reuse/inventory.yaml
.venv/bin/ansible-playbook --version
.venv/bin/ansible-inventory -i "$INVENTORY" --graph
.venv/bin/ansible -i "$INVENTORY" all -m raw -a 'docker ps'
.venv/bin/ansible -i "$INVENTORY" all -m raw -a 'npu-smi info'
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/check.yml
```

使用仓库已有 `.venv`；新控制机按 [Ansible README](../../README.md) 安装依赖。
SSH使用root，p1/d0/d1显式经ott9-a31跳转；修改主机名或账号时，同时改inventory和SSH ProxyCommand。
控制机不能直连172.168.4.x，HTTP检查与推理请求必须从跳板机发起。

`check.yml`检查SSH、Docker和各节点需要的镜像。缺镜像时先安装已有构建产物，不能指望公网pull本地tag。
如已有共享盘镜像tar，可按P/D分别加载（会写Docker镜像存储）：

```bash
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/pull.yml --limit prefill \
  -e image_tar=/mnt/sfs_turbo/path/to/prefill-image.tar
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/pull.yml --limit decode \
  -e image_tar=/mnt/sfs_turbo/path/to/decode-image.tar
```

tar内的tag必须与 `engine_images` 对上；Mooncake Master镜像也要已存在且与client兼容。
当前依赖的Mooncake包含range/session接口，不能混用不兼容的旧Master/client。

## 3. 先生成并审查

```bash
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/gen.yml
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/start.yml --syntax-check
bash -n generated/run_dp_template_p0.sh
bash -n generated/run_dp_template_p1.sh
bash -n generated/run_dp_template_d0.sh
bash -n generated/run_dp_template_d1.sh
rg -n 'max-model-len|max-num-batched-tokens|max-num-seqs|enforce-eager|kv_connector|layerwise_num_shared_buffers' generated/run_dp_template_*.sh
cat generated/mooncake.json
```

`gen.yml`只在控制机写 `ansible/generated/`，不启动集群。所有inventory共用这个输出目录：
**修改配置后必须用同一个inventory重新gen，再start，不要混用其他inventory生成的mooncake.json。**
P脚本应包含共享槽数2、独立层`[0,-1]`；D脚本不应包含共享槽参数。
两侧应有eager、INT8、旧MooncakeLayerwiseConnector和Store；不应有MTP配置、DCP>1或SFA connector。
`mooncake.json`应直连`172.168.4.237:52050`，协议字段为`ascend`；底层RoCE由环境配置选择。

## 4. 完整部署与重启

这是会改变服务状态的路径：`start.yml`会删除重建本配置指定的引擎容器，重建`mooncake-p0`，
在宿主机写日志目录和临时文件，然后启动引擎和proxy。`stop.yml`会删除对应引擎和Master容器。
按照本项目AGENTS.md的约定，执行这些宿主机写操作前需获得相应授权。

**当前A3已有P r05／D r03在运行，不能直接在它们旁边执行start。**
新容器名不同，但设备、API端口及Mooncake Master仍共用。先安排维护窗口，检查所有引擎running/waiting均0，
由原服务负责人按其流程停止原引擎并保留回退容器；再次检查Docker/NPU占用。
不要用本inventory的`stop.yml`去“清理”原服务，也不要用全局pkill、reset或重启Docker来腾卡。

设备和端口已释放、镜像及生成结果已确认后：

```bash
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/start.yml
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/status.yml
```

首次加载与初始化需要数分钟；先看日志进度，不要反复启动相同引擎。
已知通用playbook的HTTP重试可能报`no attribute status`；这时单独检查健康和请求结果，不能仅凭Ansible退出码判断。

部署后需要重启这套服务时，先确认请求排空，再执行：

```bash
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/stop.yml
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/gen.yml
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/start.yml
```

不要直接重复`start.yml --tags engine`，它不会自动保证旧引擎已退出。
只重启P也不能重启共享Master，否则会使D和其他Store client的会话失效。

## 5. 验证请求与实际生效

健康检查在跳板机执行：

```bash
ssh root@ott9-a31 'for h in 172.168.4.237 172.168.4.31; do curl -fsS http://$h:9081/health; done
for h in 172.168.4.230 172.168.4.226; do curl -fsS http://$h:9900/health; done
curl -fsS http://172.168.4.237:8000/healthcheck'
```

P/D持续生成必须请求proxy，而不是把多token生成直接发给P：

```bash
ssh root@ott9-a31 'curl -fsS --max-time 300 http://172.168.4.237:8000/v1/chat/completions \
  -H "Content-Type: application/json" -d @-' <<'JSON'
{"model":"glm-52","messages":[{"role":"user","content":"请用一句中文解释Mooncake如何把KV blocks作为对象存储。"}],"temperature":0,"max_tokens":64,"chat_template_kwargs":{"enable_thinking":false}}
JSON
```

用下面的校验任务检查约32K输入的长请求。它使用新nonce、校验实际token数，并要求返回三个已知词：

```bash
ssh root@ott9-a31 python3 - <<'PY'
import json, urllib.request, uuid
def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)
p = "http://172.168.4.237:9081"
proxy = "http://172.168.4.237:8000"
filler = "Mooncake stores KV blocks as objects. "
text = "Archive " + uuid.uuid4().hex + "\n"
for label, word in [("ALPHA", "maple"), ("BETA", "lemon"), ("GAMMA", "quartz")]:
    text += filler * 1350 + "\nVerification word for " + label + " is " + word + ".\n"
text += "Return only the three verification words in ALPHA BETA GAMMA order."
body = {"model": "glm-52", "messages": [{"role": "user", "content": text}],
    "chat_template_kwargs": {"enable_thinking": False}}
count = post(p, "/tokenize", dict(body, add_generation_prompt=True))["count"]
assert 30000 < count < 35000, count
result = post(proxy, "/v1/chat/completions", dict(body, temperature=0, max_tokens=64))
print(json.dumps(result, ensure_ascii=False, indent=2))
assert result["usage"]["prompt_tokens"] == count
answer = result["choices"][0]["message"]["content"].lower()
assert all(word in answer for word in ("maple", "lemon", "quartz")), answer
PY
```

同时查看D的 `/metrics`，确认 `vllm:request_success_total` 的stop/length计数增加；不要用日志mtime判断是否走D：

```bash
ssh root@ott9-a31 'for h in 172.168.4.230 172.168.4.226; do
  curl -fsS http://$h:9900/metrics | grep -E "^vllm:(request_success_total|num_requests_running|num_requests_waiting)"; done'
```

确认复用实际生效：

```bash
.venv/bin/ansible-playbook -i "$INVENTORY" playbooks/logs.yml -e 'target=p0 tail=200'
ssh root@ott9-a31 'docker exec vllm-ascend-glm52-main-reuse grep -aE "Main-only KV reuse layout|num_blocks:" /mnt/sfs_turbo/g00832294/logs/main-reuse/vllm_p0.log | tail -n 24'
```

需要看到Main layout实际映射到4槽、Indexer独立及真实block容量；不能只看配置中出现`num_shared_buffers`。
block数受剩余显存和workspace影响，不要求固定等于历史某个数字。
验证应包含多chunk、同前缀、并发及断连后请求，且检查答案和错误计数，HTTP200只证明接口成功返回。
D模板的`kv_load_failure_policy`固定为`recompute`，不能仅靠答案和成功计数证明每个请求都传输了完整KV；
需要结合D的KV接收记录、external prefix指标及Store range日志确认实际路径。

## 6. 常见排查

| 现象 | 优先检查 |
|---|---|
| 模型未启动或OOM | 镜像是否真的包含复用代码；共享布局日志；P block预算；D普通HBM容量；chunk和MoE预算 |
| 200健康但请求一直等 | 是否旧layerwise connector配D-first proxy；D是否先发布目标；两侧TP与地址是否匹配 |
| 首请求原生崩溃 | RoCE preset及额外env是否出现在真实脚本；不要只检查HCCL或随意切HCCS |
| 缺外部waiter报错 | connector和Store的屏障接线未满足；保留启动拒绝，不要移除断言绕过 |
| Store前缀/partial覆盖报错 | 保留首次错误、请求身份、token范围和snapshot generation；不要改成忽略KV加载失败 |
| 回答错误但无异常 | 保存提示词、token数、镜像与真实脚本；做同配置代码A/B，不能把无crash当作正确 |
| 图/MTP/DCP相关拒绝 | 保持eager、不开MTP、CP/DCP=1；本特性当前不支持该组合 |

按需将`pd_cluster.range_debug`改为true，重新gen、审查并在排空后重启这套引擎。
小请求重现后检索`KVPOOL_RANGE_DEBUG`的save/load/commit及失败结果；调完恢复false，避免大量日志影响性能。
引擎日志在上述`logs.dir`，proxy为`proxy.log`，Master为`mooncake_p0.log`，ACL日志在`ascend/{node}/`。

排查顺序：真实脚本与镜像 → 布局和容量 → 请求正确性 → Store恢复/屏障 → PD传输 → 性能。
不要叠加旧`debug_mounts`或往无效editable路径复制代码；镜像中的实际安装路径应先用Python `__file__`确认。

## 7. P独立1M的单独入口

P独立prefill＋首token已验收的原始配置为 [p-main-buffer-reuse-p-only-1m.yaml](../../profiles/p-main-buffer-reuse-p-only-1m.yaml)。
其最大长度1048576、chunk32784；只操作两个P，D保持原配置，且P请求只能生成1token。
该profile设置了`reuse_render_only: true`，通用`start.yml`会拒绝它；完整操作见 [P独立1M文档](../../../docs/p-main-buffer-reuse-p-only-1m.md)。
不要把本文件的共同`max_model_len`直接改为1M后全量启动普通D，也不要据此声称P/D端到端1M已支持。

## 8. Receipt P/D 实验档

Decode 模板在 `pd_cluster.decode.receipt_connector_extra` 存在时渲染单个
`MooncakeLayerwiseConnector`、receipt scheduler 和 `kv_load_failure_policy: fail`。
该字段提供 `layerwise_receipt_mode: true`、`target_owner_rpc_base_port` 和
`target_retention_rpc_port`。TP16 每台 D 预留 16 个连续 owner 端口和一个独立 retention 端口。

双端使用已验证的同一份 receipt 源码快照、相同 TP、BF16 Main、INT8 Indexer、eager、
CP/DCP=1、关闭 MTP。P 保留 AscendStore 与逐层 connector，并开启 Main 复用。
D 的 additional-config 配置 `sparse_kv_offload_config.enabled: true`、
`host_backend: mooncake` 和 `use_fused_overlap: true`。这两端的镜像底座仍可分别为 r05 和 r03，
候选的完整源码覆盖包须另外验证，包含 D 运行时需要的 C++ 文件。

实机档的 16,384 上下文、4,112 chunk、B2 及首尾独立层配置保存在
[`p-main-buffer-reuse-receipt.yaml`](../../profiles/p-main-buffer-reuse-receipt.yaml)。
从 `ansible/` 渲染，产物写入 `artifacts/p-main-buffer-reuse/receipt-profile-render/`。
该 profile 默认在 D 选用 `AsyncReceiptScheduler`。将 `pd_cluster.decode.async_scheduling`
设为 `false` 可切回同步 `ReceiptScheduler`，两者都使用 receipt 的目标保留和回收门禁。

```bash
.venv/bin/ansible-playbook -i inventories/a3-reuse/inventory.yaml \
  -e @profiles/p-main-buffer-reuse-receipt.yaml playbooks/gen.yml
```

该档保留 `reuse_render_only: true`。实机使用监督脚本分阶段启动，先在独立容器和
Master（52150）验证源码覆盖，再停止已核对 ID 的基线容器并启动引擎。验证结束后
按相同 ID 恢复基线。候选端口为 P 9181、D 9910、代理 8100。
短请求、三 chunk 冷请求与重放、双 D 并发和取消记录见 [runbook §11.54](../../../docs/m1-buffer-reuse-runbook.md)。
