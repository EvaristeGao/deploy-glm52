# deploy-glm52 部署方案说明

GLM-5.2 **P/D 分离** + **mooncake KV 池** + **mooncake_master HA（etcd 选主 + haproxy 代理）** + 统一 **proxy 入口**。
部署方式：Ansible agentless（控制机只装 ansible-core，节点通过 SSH + python3 管理，无 agent）。

> 文档基于当前代码状态（A2 已实机验证、A3 待实机验证）。所有变量均可在各集群
> `ansible/inventories/<集群>/group_vars/all.yml` 中调整。

---

## 1. 总体架构

```
                     ┌────────────────── HA 控制面 ──────────────────┐
                     │                                               │
       3×etcd ──2380 peer 互连（选主）                               │
          ▲                    ▲                                    │
          │ https 2379        │ https 2379（roundrobin 负载均衡）   │
          │                   │                                     │
  haproxy(12379)  ── 127.0.0.1:12379 ──► mooncake_master ×3 (52050)  │
  [3 个 mooncake 容器]        -enable_ha=true 经 etcd 选主           │
          │                                                          │
  haproxy(12489)  ── mooncake.json master_server_address ──► 引擎    │
  [每个引擎容器内]                 （当前值 127.0.0.1:12489）         │
                     └──────────────────────────────────────────────┘

                     ┌──────────── 数据面（KV 传输）────────────┐
  Prefill 引擎 (kv_producer)  ── kv_port 30000 (MooncakeConnectorV1
                                   + AscendStoreConnector/backend=mooncake)
                                   + 各引擎 rpc 16591/16600 互连 ──►  Decode 引擎
                     └──────────────────────────────────────────────┘

  客户端 ──► proxy (p0, 1999/8000) ──► Prefill :9081 / Decode :9900（HTTP）
```

## 2. 节点拓扑

### 2.1 A2 集群（8 节点，4P + 4D，已实机验证）

来源：`ansible/inventories/a2/inventory.yaml`

| 节点 | IP | 角色 |
|---|---|---|
| p0 | 192.168.0.245 | Prefill 引擎 + proxy + mooncake_master + etcd |
| p1 | 192.168.0.15 | Prefill 引擎 + mooncake_master + etcd |
| p2 | 192.168.0.160 | Prefill 引擎 + mooncake_master + etcd |
| p3 | 192.168.0.91 | Prefill 引擎 |
| d0 | 192.168.0.127 | Decode 引擎 |
| d1 | 192.168.0.161 | Decode 引擎 |
| d2 | 192.168.0.154 | Decode 引擎 |
| d3 | 192.168.0.140 | Decode 引擎 |

### 2.2 A3 集群（4 节点，2P + 2D，未实机验证）

来源：`ansible/inventories/a3/inventory.yaml`

| 节点 | IP | 角色 |
|---|---|---|
| p0 | 10.246.63.49 | Prefill 引擎 + proxy + mooncake_master + etcd |
| p1 | 10.246.63.51 | Prefill 引擎 + mooncake_master + etcd |
| d0 | 10.246.63.52 | Decode 引擎 + mooncake_master(TODO) + etcd(TODO) |
| d1 | 10.246.63.55 | Decode 引擎 |

> TODO：A3 的 `mooncake.ha.nodes` / `etcd.nodes` 待实机确认（4 节点中选 3 个）。

## 3. 每节点跑哪些容器

| 容器 | 数量 | 落在哪 | 内容 |
|---|---|---|---|
| **vllm-ascend 引擎容器**（`container.name`，--net=host --pid=host） | 每节点 1 个 | 全部 8/4 节点 | vllm serve P/D 引擎 + **P/D haproxy**（监听 12489）+ launch_online_dp.py + mooncake.json |
| **etcd 容器**（`etcd-<node>`，etcd v3.5.16） | 3 个 | A2: p0/p1/p2；A3: p0/p1/d0(TODO) | etcd 选主后端，端口 2379/2380 |
| **mooncake 容器**（`mooncake-<node>`，`mooncake.image`，TODO 待填） | 3 个 | 同 etcd 节点 | mooncake_master + **CacheMaster haproxy**（12379）+ nc |

引擎规模：

- **A2**：prefill dp4 tp8（每节点 1 引擎 :9081）；decode dp8 tp4（每节点 2 引擎 :9900 :9901）
- **A3**：prefill dp4 tp8（每节点 2 引擎 :9081 :9082）；decode dp32 tp1（每节点 16 引擎 :9900–:9915）

## 4. 端口规划

| 服务 | 端口 | 位置 |
|---|---|---|
| etcd client / peer | 2379 / 2380 | 3 个 etcd 容器（https 双向证书） |
| **CacheMaster haproxy** | **12379** | 3 个 mooncake 容器内（代理 etcd） |
| **P/D haproxy** | **12489** | **每个**引擎容器内（代理 etcd） |
| mooncake_master rpc / metrics / health | 52050 / 52052 / 52054 | 3 个 mooncake 容器内 |
| Prefill vllm serve / kv / rpc | 9081+ / 30000 / 16591 | 每 P 引擎 |
| Decode vllm serve / kv / rpc | 9900+ / 30100 / 16600 | 每 D 引擎 |
| proxy | A2: 1999 / A3: 8000 | p0 引擎容器内 |

## 5. 连接关系

### 5.1 HA 控制面

1. **3 个 etcd** 通过 2380 peer 端口互相选主（https 双向证书认证，证书来自 `etcd.certs_dir`）。
2. **3 个 mooncake_master** 各自以 `-enable_ha=true` 启动，`-etcd_endpoints=127.0.0.1:12379`——经本机 **CacheMaster haproxy** 连 etcd 参与选主，选主后仅 leader 提供 KV 服务。
3. **haproxy 是 etcd 代理**（不是 master 代理）：后端 roundrobin 3 个 etcd，`ssl verify none` + `client.pem` 客户端证书 + `sni` + `alpn h2`。
4. **每个 P/D 引擎容器内**也起一个 haproxy（12489），同样代理 etcd；引擎通过 `MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json` 的 `master_server_address` 连接本机 haproxy。

### 5.2 数据面（KV 传输）

- Prefill 引擎为 `kv_producer`，经 `kv_port`（30000，MooncakeConnectorV1 + AscendStoreConnector/backend=mooncake）把 KV 传给 Decode 引擎（30100），实现 P/D 分离下的 KV 池。
- 各引擎经 `rpc_port`（16591/16600）互连协调。

### 5.3 请求入口

- 客户端请求走统一 **proxy**（落在 p0 引擎容器内，A2: 1999 / A3: 8000），proxy 按 P/D 分工把请求转发给对应 Prefill :9081 / Decode :9900 实例（HTTP）。

## 6. HA 关键点（对齐 ETCD.md v1.1）

1. **mooncake_master HA**：3 个 master 各自 `-enable_ha=true -etcd_endpoints=127.0.0.1:12379`（经本机 CacheMaster haproxy 连 etcd，选主后只有一个 leader 提供 KV 服务）。
2. **haproxy 部署**：原版脚本 `ansible/ha/gen_haproxy_etcd.sh` + `ansible/ha/start-ha.sh` 原样通过 docker cp 复制进容器，在容器内以环境变量 `HAP_LISTEN_START_PORT` / `ETCD_SERVER_LIST` 执行（与 `ha_raw/` 完全一致，未修改）。
3. **每实例 1 个 haproxy 监听 1 个端口**：mooncake 容器内 12379，每个 P/D 引擎容器内 12489。
4. **引擎配置**：`MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json`，`master_server_address` 指向本机 haproxy。

## 7. 当前存疑点（待确认）

### 7.1 `master_server_address` 语义问题（进行中）

- 当前 `gen.yml` 渲染的 mooncake.json 里 `master_server_address: "127.0.0.1:12489"`，即本机 **P/D haproxy**。
- **问题**：12489 是 **etcd 代理**（不是 mooncake master）。文档 v1.1 中 P/D 引擎连本机 haproxy 用的是**魔改 cache engine** 的 `cache_engine_master_addrs=etcd://127.0.0.1:12479`（`etcd://` 前缀，经 haproxy 到 etcd 发现 master）；而**原生 mooncake 的 `master_server_address` 是直接连 master KV 服务的 `ip:port`**。
- 若原生 mooncake 不支持 `etcd://`，则指向 12489 到的是 etcd 而非 master，语义不符。**这决定了 `master_server_address` 最终值**：
  - 原生支持 `etcd://` → 可填 `etcd://127.0.0.1:12489`（文档 v1.1 方式）；
  - 原生不支持 → 需直连 master（`<master_ip>:52050`），或让 P/D haproxy 代理 master（当前是代理 etcd，二者取一）。
- 需确认：镜像内原生 mooncake 对 `etcd://` 的支持情况（查镜像内帮助/文档，或实测）。

### 7.2 待用户填写的占位项

| 变量 | 说明 |
|---|---|
| `mooncake.image` | mooncake 容器镜像（须含 mooncake_master + haproxy + nc） |
| `etcd.certs_dir` | etcd 证书目录（须已有 ca.crt / server.crt / server.key / client.pem） |
| A3 `mooncake.ha.nodes` / `etcd.nodes` | A3 的 3 个 master/etcd 节点 |
