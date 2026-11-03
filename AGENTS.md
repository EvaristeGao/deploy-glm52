# AGENTS.md —— deploy-glm52 项目上下文

> 本文件是给 AI/新同学看的**项目现状与操作手册**。内容随时效性变化，最后更新：2026-11-03。
> 详细过程记录见 `docs/`，本文件只做索引与关键结论。

---

## 1. 这是什么项目

A3 集群（4 节点 × 16 卡）上 **GLM-5.2（glm_moe_dsa）的 P/D 分离部署**，基于 vLLM-Ascend
v0.30.0rc1，带 Mooncake KV 池（block-key 数据面），并在其上开发
**P（prefill）侧 KV buffer reuse**（跨层共享 HBM 槽位，放大可用 block 数）。

- 部署仓库：本仓库（branch `glm52-kvoffloading`），Ansible 驱动
- 上游源码工作区：`src/vllm-ascend`（本地 clone，**已 gitignore**，不随本仓库提交）
  - remote `maz` = `https://github.com/mazifan0120/vllm-ascend.git`
  - remote `shichang` = gitcode（取参考修复用）
  - 基线：`maz/dsa_offload_rebase_30` HEAD = **`4234a1f18`**（= 我们的镜像基线 `dsa-base`）

---

## 2. 集群与访问

| 角色 | 主机 | 地址 | 引擎端口 |
|---|---|---|---|
| p0（跳板 + proxy） | `ott9-a31` | 172.168.4.237 | 9081（proxy 8000） |
| p1 | `ott9-a32` | 172.168.4.31 | 9081 |
| d0 | `ott9-a33` | 172.168.4.230 | 9900 |
| d1 | `ott9-a34` | 172.168.4.226 | 9900 |

- 容器名统一 `vllm-ascend-gwh-glm52`
- 日志：`/mnt/sfs_turbo/g00832294/logs/vllm_{p0,p1,d0,d1}.log`、`proxy.log`、`mooncake_{node}.log`
- ACL 日志在容器内 `/root/ascend/log`（已持久化挂载到 `logs/ascend/{node}`）

**⚠️ 控制机到 `172.168.4.x` 不通**，所有 curl/健康检查必须在跳板机上执行：

```bash
ssh root@ott9-a31 'curl -s http://172.168.4.237:8000/healthcheck'
```

---

## 3. 部署

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml     # 生成启动脚本
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml   # 部署（~12 分钟）
```

- **改了 `group_vars` 或模板后必须先跑 `gen.yml`**，再 `start.yml`
- `start.yml` 末尾的 smoke test 有个已知 bug（`until: smoke.status == 200` 在失败结果上报
  `no attribute 'status'`），p0 上 `failed=1` 多半是它，不代表部署失败，需自己发请求确认

### 配置档（`group_vars/all.yml` 的 `deploy_config`）

| 档名 | max_model_len | 连接器 | 共享槽 | 单 Store |
|---|---|---|---|---|
| `200k` / `1m` / `1m-v030` | 202752 / 1048576 | blockwise | — | — |
| **`1m-v030-lw`（当前）** | **262144** | **layerwise** | 3 | — |
| `1m-v030-hyb` / `-hyb-1m` / `-hyb-tp8` | 202752 / 640000 / 65536 | blockwise | 9 | ✅ |
| `1m-v030-poff` | 204800 | blockwise | — | — |

当前生效：`deploy_config: 1m-v030-lw`、`image: glm52-vllm-ascend:0.30.0-dsabase-m1`、
`pd_connector: layerwise`、`proxy.layerwise: true`、无 `debug_mounts`。

---

## 4. 镜像构建与分发

镜像 = rc1 基础镜像 + 用 dsa-base 源码编译的 `vllm_ascend` wheel（见
`image_build/Dockerfile_glm52_dsabase`）。

```bash
# 1) 在 p0 上构建（构建上下文放 SFS，四节点共享）
#    Dockerfile 会：卸载旧 editable、装 wheel、COPY m1/pool_worker.py（b3222a7 补丁）、find_spec 自检
cd /mnt/sfs_turbo/g00832294/build/imgctx
docker build -f Dockerfile -t glm52-vllm-ascend:<tag> .

# 2) 导出 tar 到 SFS（约 21G）
docker save glm52-vllm-ascend:<tag> -o /mnt/sfs_turbo/g00832294/build/images/<tag>.tar

# 3) 其余节点导入
docker load -i /mnt/sfs_turbo/g00832294/build/images/<tag>.tar
```

**已有的镜像**

| tag | 内容 | 状态 |
|---|---|---|
| `glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13` | 原始 rc1 | 旧基线 |
| `...:0.30.0-dsabase-4234a1f` | dsa-base wheel | 可用 |
| `...:0.30.0-dsabase-m1` | + `b3222a7` start_block 补丁 | **当前使用** |
| `...:0.30.0-dsabase-m1b` | + 复用接入首版（有 `Any` 导入 bug） | 已废弃，勿用 |
| `...:0.30.0-dsabase-m1c` | + P 侧复用折叠代码（可用版） | 容量 5.9×，但多 chunk 乱码，勿用于服务 |

**构建踩坑（已固化在 Dockerfile/流程里）**

- git `dubious ownership` → `git config --global --add safe.directory "*"`
- catlass 子模块拉不到 → 直接 clone `https://gitcode.com/cann/catlass.git` 到
  `csrc/third_party/catlass`（**目标目录要先 rm -rf，否则会套一层**）
- CANN 算子包安装做加载校验 → 给 `csrc/build_aclnn.sh` 的 installer 加 `--force`
- CMake 阶段 torch 自动加载 torch_npu 失败 → `TORCH_DEVICE_BACKEND_AUTOLOAD=0` +
  `LD_LIBRARY_PATH=.../cann-9.1.0/aarch64-linux/devlib/linux/aarch64`
- 源码树无 `.git` 时版本号变 `0.0.0` → `SETUPTOOLS_SCM_PRETEND_VERSION=0.30.0`

---

## 5. 当前在开发的特性：P 侧 KV buffer reuse

**目标**：把多个层折叠到少量共享 HBM 槽（`num_shared_buffers: 3` + 独立层），
同样显存下放大可用 block 数；必须保证「写共享槽前，上一代所有读者已结束」（设计 I1）。

### 5.1 已完成（有实测）

- 容量折叠：`num_blocks` **5508 → 32768（5.9×）**，4 引擎 200、启动无断言
- 关键落点：模型**不走** glm5next 专用构建器（诊断 `glm5next=False`），改在
  `patch_kv_cache_utils` 的默认路径接管，按页大小分组折叠 `KVCacheTensor`
- 全 rank 一致：块数 = 基线块数 × 逻辑页/物理页，再向下取 2 的幂（绕过
  `assert all(num_blocks == ...)`）
- `extract_layout_config()` 从恒 None 改为显式 opt-in；`worker.py` 避免重复缩放

### 5.2 未完成（当前卡点）

折叠开启后**双 chunk 输出乱码**，`grep "release physical KV slot"` 计数恒为 0。

**走错的路（第 7–11 轮）**：把屏障钩子挂在模型 forward（`mla_v1` / `attention_c8_mxfp` /
`dsa_v1` / `dsa_v41`），甚至加无条件 warning 探针，服务期**一次都不触发** ——
说明 ACLGraph/融合算子把逐层 Python 语义吞掉了。

**文档口径（正解）**：设计文档 §6 第 1 步写的是
`MultiConnector._wait_for_external_slot_release(M)`，即 **Store 侧逐层入口**
（源码里已有 `kv_transfer.py` 的 `external_slot_release_waiter(layer_id)`）。
真正缺的是文档 §8 的**读者登记**（store_put / store_get 的 `reserve_read`/`complete_read`）
和 §5.2 的 `assert_reuse_wired()`。

### 5.3 下一步（按顺序）

1. 加日志确认 Store 侧 waiter 在 P 上是否被调用（区分「没调用」/「调用了但事件已 set」）
2. 实现 §8 的 reader 登记（多 chunk 乱码的直接病根）
3. 补 `assert_reuse_wired()`：启用复用但屏障未接线 → 启动失败
4. 清掉第 7–11 轮模型侧钩子（路线错误）
5. 全绿后重建镜像 → `docker save` → 各节点 `docker load -i`

### 5.4 分支

```
仓库: src/vllm-ascend（gitignore，本地）
分支: m1-prefill-reuse        19 个 commit，基于 dsa-base(4234a1f)
tag : m1-prefill-reuse-wip-20261103
改动: 10 个文件，+345/−18
```

---

## 6. 文档索引

| 文档 | 用途 |
|---|---|
| `docs/m1-work-summary.md` | **工作总览**：已完成/未完成、代码清单、下一步、恢复步骤 |
| `docs/m1-buffer-reuse-runbook.md` | M1 运行手册 + §11 逐轮实验记录（含每轮证据与失败原因） |
| `docs/mc-layerwise-detailed-design.md` | 详细设计（M0–M3、不变量 I1–I5、§6 逐层时序、§8 reader 登记） |
| `docs/mc-layerwise-pd-offload-design.md` | 对上游 mc.pdf 方案的分析与修正 |
| `docs/a3-v030-layerwise-onboarding.md` | 环境上手、端口、日志、验证方法 |
| `docs/layerwise-mooncake-clean-profile.md` | 干净档（无复用）配置说明 |
| `docs/a3-glm52-configs.md`、`docs/architecture.md` | 配置与架构背景 |
| `docs/reuse-buffer-debug-status.md` | 早期自造 reuse 实现的失败记录（已废弃，仅存档） |
| `docs/a3-layerwise-kv-offload.md`、`docs/a3-prefill-kv-offload.md` | 逐层 KV / prefill offload 的方案演进 |
| `docs/a3-layerwise-pool-load-deadlock.md` | 曾遇到的 pool load 死锁分析 |
| `docs/a3-v030rc1-vs-official.md`、`docs/a3-cluster-reference-comparison.md` | 与官方/参考实现的差异对比 |
| `docs/b348e1e-port-plan-v030.md` | 迁移方案（v0.30 port plan） |
| `docs/mooncake-prefill-reuse-design.md` | 早期 prefill reuse 设计（已被 m1 runbook 取代） |
| `docs/deploy-modes-verification.md`、`docs/deploy-modes-simulated-commands.md` | 各部署档的验证记录与命令 |
| `docs/subagent-context-budget.md` | 子任务上下文预算约定 |
| `.claude/skills/a3-glm52-deploy/SKILL.md` | 部署 skill（含「两档配置」与 M1 三个必要条件） |

---

## 7. 注意事项（踩过的坑）

### 部署与验证

- 所有 curl 走跳板机 `ott9-a31`；控制机直连 `172.168.4.x` 会挂
- 判断 D 侧是否有流量看 `/metrics` 的 `vllm:request_success_total`，**不要看日志 mtime**
- `gen.yml` 必须重跑（改配置/模板后）
- 首次请求 60–90 秒是新镜像的图编译冷启动，属正常

### 硬约束（缺了会崩）

- `max_model_len: 262144`：不挂 KV 预算放大补丁时，TP16/1M 需要 54.14 GiB > 可用 37.4 GiB
- `hixl_preset: roce` + 对应 `extra_env`：缺了会走 hccs P2P，同机跨 device 建链失败 → 首个请求 segfault

### 复用/逐层相关

- 逐层 P/D **必须**用 `MooncakeLayerwiseConnector`（`pd_connector: layerwise`），
  块级 `MooncakeConnectorV1` 不提供 `wait_for_layer_reuse` 屏障
- **代理必须 D-first**：`proxy.layerwise: true`（D 先 POST `/v1/metaserver`）；
  用普通 P-first 代理会表现为「引擎 200 但请求永不返回，日志只有 `/tokenize`」
- `debug_mounts` 的**目标路径必须是 site-packages**
  （`/usr/local/python3.12.13/lib/python3.12/site-packages/vllm_ascend/...`），
  写 `/vllm-workspace/vllm-ascend/...` 不会生效（那是 rc1 的 editable 路径）
- `num_shared_buffers` 配了**不等于**复用生效：要看日志里
  `Layerwise KV cache reuse maps ... scale logical KV budget by ...` 和 `num_blocks` 是否放大
- 不要用「逐 token 与对照组一致」做复用正确性判据：多 chunk 本身有数值非确定性
  （不共享的对照组同样会变）

### 其它

- 构建容器需要挂 `/usr/local/Ascend/driver`，否则 CANN 算子包安装校验失败
- 每个镜像约 21G，`docker save` 出来的 tar 很大，用完及时清理（当前 `images/` 只保留 `m1c`）
- commit 信息含引号时用 `git commit -F <file>`；`src/vllm-ascend` 要求 `-s` 签名

---

## 8. 常用命令速查

```bash
# 健康检查（跳板机）
ssh root@ott9-a31 'for h in 172.168.4.237 172.168.4.31; do printf "P:%s " "$(curl -s -o /dev/null -w %{http_code} http://$h:9081/health)"; done; \
 for h in 172.168.4.230 172.168.4.226; do printf "D:%s " "$(curl -s -o /dev/null -w %{http_code} http://$h:9900/health)"; done; echo; \
 curl -s http://172.168.4.237:8000/healthcheck'

# 请求回归（单/双/三 chunk）
ssh root@ott9-a31 '/root/bin/rtest 1 60 16; /root/bin/rtest 1 526 24; /root/bin/rtest 1 620 24'

# 复用是否生效
grep -aoE '"layerwise_num_shared_buffers": [0-9]+' ansible/generated/run_dp_template_p0.sh
ssh root@ott9-a31 'grep -aoE "num_blocks: [0-9]+" /mnt/sfs_turbo/g00832294/logs/vllm_p0.log | tail -1'

# 屏障是否真的在等
ssh root@ott9-a31 'grep -c "release physical KV slot" /mnt/sfs_turbo/g00832294/logs/vllm_p0.log'

# M1b/M1c 镜像回到可用基线
#   group_vars: image: glm52-vllm-ascend:0.30.0-dsabase-m1，删除 debug_mounts
#   然后 gen.yml + start.yml
```

---

## 9. 约定

- 文档随代码更新：改行为 → 更新 `docs/m1-work-summary.md` 与相关 runbook
- 实验记录写进 runbook 的 §11（每轮：做了什么、证据、失败原因、下一步）
- 未经确认不要把「配置在位」当成「特性可用」；一切以日志/指标/请求结果为准
