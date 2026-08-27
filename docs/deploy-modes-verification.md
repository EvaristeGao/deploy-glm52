# deploy-glm52 部署模式模拟执行验证

> 无实机、纯渲染模拟。基座：`a2` inventory（8 节点，`ip` = 真实网卡 IP `116.204.x`，`ansible_host` = SSH 目标）。
> 基于当前代码（含 `599a1c6` 起的 ip 字段 / 部署模式 / standalone haproxy 改动）。
>
> 📄 **完整分步命令见 [deploy-modes-simulated-commands.md](deploy-modes-simulated-commands.md)**（4 模式 × start/gen 全部任务的完整渲染命令，含每个 shell 的 `bash -n` 结果）；本文是核对结论。

## 1. 方法

用 `ansible/simulate_modes.py`（jinja2 复刻 Ansible 渲染：`trim_blocks=True` + 自定义 filter `dirname/basename/product` + 链式假值 Undefined）逐模式执行 `start.yml` / `gen.yml`：

- 按 `when` 判定哪些任务在哪些节点跑；
- 按 `set_fact` 派生节点参数（dp/tp/端口/etcd 端点/haproxy 端点等）；
- 渲染每个 `shell` / `template` / `copy` 任务的实际命令；
- 每个 `shell` 命令跑 `bash -n` 语法检查 + 人工核对关键参数。

复现：`cd ansible && .venv/bin/python simulate_modes.py`

## 2. 结论

| 模式 | shell 命令数 | bash 语法失败 |
|---|---|---|
| A kvpool HA · 内嵌 haproxy（默认） | 69 | 0 ✅ |
| B kvpool HA · 独立 haproxy | 64 | 0 ✅ |
| C kvpool 单主 | 51 | 0 ✅ |
| D 无 kvpool | 41 | 0 ✅ |

四种模式全部命令渲染正确、语法通过；组件间互通地址（etcd / haproxy / mooncake / 引擎 / proxy）全部为网卡 IP（`ip` 字段），`ansible_host` 仅用于 SSH。

## 3. 模式矩阵

| 模式 | enabled | enable_ha | haproxy.mode | 部署的 mooncake 相关组件 |
|---|---|---|---|---|
| A 内嵌（默认） | true | true | per_container | 3×etcd + 3×mooncake_master + 每容器内嵌 haproxy + kvpool |
| B 独立 | true | true | standalone | 3×etcd + 3×mooncake_master + 3×独立 haproxy 容器 + kvpool |
| C 单主 | true | false | — | 1×mooncake_master（p0 直连）+ kvpool |
| D 无 kvpool | false | — | — | 无 master/etcd/haproxy，KV 直连 |

## 4. 各模式验证详情

### 4.1 模式 A：kvpool HA · 内嵌 haproxy（默认）

- **gen.yml**：`mooncake.json` → `master_server_address: etcd://127.0.0.1:12489`；引擎模板用 **MultiConnector**（MooncakeConnectorV1 + AscendStoreConnector）+ `MOONCAKE_CONFIG_PATH`。
- **etcd 容器**（p0/p1/p2，advertise/peer/initial-cluster 全用网卡 IP）：
  ```bash
  docker run -d --name etcd-p0 --network host \
    -v /mnt/share_space/certs:/certs \
    quay.io/coreos/etcd:v3.5.16 \
    etcd --name s1 --data-dir /etcd-data \
    --listen-client-urls https://0.0.0.0:2379 \
    --advertise-client-urls https://116.204.91.141:2379 \
    --listen-peer-urls https://0.0.0.0:2380 \
    --initial-advertise-peer-urls https://116.204.91.141:2380 \
    --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    --client-cert-auth ... --peer-client-cert-auth ...
  ```
- **mooncake 容器**（p0/p1/p2）：`docker run -itd --name mooncake-<node> --shm-size=500g --network host ... --entrypoint=bash ...`
- **CacheMaster haproxy**（mooncake 容器内，3 个）：`HAP_LISTEN_START_PORT=12379`
- **P/D 引擎 haproxy**（每个引擎容器内，8 个）：`HAP_LISTEN_START_PORT=12489`
- **mooncake_master**（3 个，`-enable_ha` + 本机 CacheMaster haproxy）：
  ```bash
  'mooncake_master -enable_ha=true \
    --cluster_id=mooncake \
    -etcd_endpoints 127.0.0.1:12379 \
    --rpc-address 116.204.91.141 \
    -rpc_port=52050 -metrics_port=52052 ...'
  ```
- **引擎启动**（p0 prefill 示例）：`launch_online_dp.py --dp-size 4 --tp-size 8 --dp-size-local 1 --dp-rank-start 0 --dp-address 116.204.91.141 --dp-rpc-port 16591 --vllm-start-port 9081`
- **proxy**（p0）：`--prefiller-hosts <4 个 P 网卡IP> --decoder-hosts <8 个 D 网卡IP> --port 1999`
- 检查：组合校验 assert 通过 ✅；shell 69 条 bash-OK。

### 4.2 模式 B：kvpool HA · 独立 haproxy

- **gen.yml**：`mooncake.json` → `master_server_address: etcd://116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379`（3 个独立 haproxy）。
- **独立 haproxy 容器**（haproxy-p0/p1/p2，镜像 `haproxy.image`=`haproxy-etcd:latest`（构建自 `image_build/Dockerfile_ha`），挂 etcd 证书）：
  ```bash
  docker run -itd --name haproxy-p0 --network host \
    --entrypoint=bash \
    -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake
  ```
  启动脚本：`HAP_LISTEN_START_PORT=12379; ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"`（统一端口 12379，后端 3 个 etcd）。
- **mooncake_master**：`-etcd_endpoints 116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379`（改连 3 个 haproxy）。
- **不再**在每个 mooncake/引擎容器内跑 haproxy（per_container 任务被跳过）。
- 检查：shell 64 条 bash-OK。

### 4.3 模式 C：kvpool 单主

- **gen.yml**：`mooncake.json` → `master_server_address: 116.204.91.141:52050`（直连 p0 的 rpc 端口）。
- **mooncake 容器**：仅 p0 1 个。
- **mooncake_master**（无 `-enable_ha` / `-etcd_endpoints`）：
  ```bash
  'mooncake_master --cluster_id=mooncake \
    --rpc-address 116.204.91.141 \
    -rpc_port=52050 -metrics_port=52052 ...'
  ```
- **无 etcd、无 haproxy**（对应任务全部跳过）。
- 引擎模板仍用 MultiConnector + `MOONCAKE_CONFIG_PATH`（kvpool 仍在，只是 master 直连）。
- 检查：shell 51 条 bash-OK。

### 4.4 模式 D：无 kvpool

- **gen.yml**：两个 mooncake.json 渲染任务都跳过 → **不生成** `mooncake.json`。
- **start.yml**：不 copy `mooncake.json`（only_kvpool 项跳过）；**无** etcd/mooncake 容器、无 haproxy、无 mooncake_master 任务。
- **引擎模板**（kv-transfer 直连，无 KV 池）：
  ```json
  "kv_connector": "MooncakeConnectorV1",
  "kv_role": "kv_producer",            // decode 为 kv_consumer
  "kv_port": "30000",                  // decode 为 30100
  "engine_id": "0",                    // decode 为 "2"
  "kv_connector_extra_config": {
    "use_ascend_direct": true,
    "prefill": { "dp_size": 4, "tp_size": 8 },
    "decode":  { "dp_size": 8, "tp_size": 4 }
  }
  ```
  无 `MultiConnector` / `AscendStoreConnector` / `MOONCAKE_CONFIG_PATH`。
- 引擎/代理/健康检查等数据面照常（`dp_address`、proxy hosts 均为网卡 IP）。
- 检查：shell 41 条 bash-OK。

## 5. 核对结论与注意事项

### 符合预期 ✅
- 三种 kvpool 模式的 etcd / haproxy / mooncake_master / 引擎 / proxy 命令全部正确，`bash -n` 全过。
- standalone 形态：3 独立 haproxy 容器、master/引擎经 3 个 haproxy 的 `IP:12379` 访问 etcd，per_container 内嵌任务被正确跳过。
- 单主：1 个 master 直连 p0，无 etcd/haproxy，命令不带 `-enable_ha`。
- 无 kvpool：不生成/分发 mooncake.json，模板为直连 `MooncakeConnectorV1`，无任何 mooncake 组件。
- 组合校验 `enable_ha → enabled` 四模式均通过。
- 全部组件间互通地址统一为网卡 IP（`ip` 字段），`ansible_host` 仅 SSH。

### ⚠️ 待实机确认（代码层面已按文档/惯例实现）
1. **单主** mooncake_master 无 `-enable_ha` 的调用、以及 `master_server_address` 直连 `<ip>:52050` 的原生语义。
2. **无 kvpool** 直连 `MooncakeConnectorV1`（`engine_id`/`use_ascend_direct`），官方标注 w4a8c8 在 PD 分离下有已知精度问题。
3. **standalone** 下 `etcd://<hp1>:<port>,<hp2>:<port>,<hp3>:<port>` 多地址是否被原生 mooncake 解析（`etcd://` 语义存疑，见 architecture.md §7.1）。

### 模拟器已知限制
- `run_once + delegate_to localhost` 的 set_fact（etcd 集群参数）在模拟中直接注入所有 host；真实 Ansible 依赖 linear 策略的 fact 传播，既有代码未变。
- `uri` 健康检查 / 冒烟等非 shell 任务不渲染命令（只标注）。
- 任务名中的 `{{ }}` 不渲染（仅显示，不影响命令内容）。
