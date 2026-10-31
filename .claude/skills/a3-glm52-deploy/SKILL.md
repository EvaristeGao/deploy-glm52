---
name: a3-glm52-deploy
description: A3 集群 GLM-5.2 vLLM-Ascend v0.30 部署（P/D 分离 + mooncake KV 池 + layerwise）的现状检查、部署、验收与排障。当需要确认这套集群是否正常、要重新部署/切配置、或排查 P 起不来/请求 503/输出乱码时使用。
version: "1.0.0"
license: MIT
metadata:
  hermes:
    tags: [deployment, ascend, vllm, mooncake, pd-disaggregation]
---

# A3 GLM-5.2 v0.30 layerwise 部署

## 什么时候用这个 skill

- 要回答「这套集群现在是不是好的」
- 要部署 / 重启 / 切换 `deploy_config`
- 出现 503、引擎 000、首请求 segfault、输出乱码
- 需要判断某个改动是否真的生效（生成脚本层面）

## 先确认三件事（30 秒）

> 所有 `curl 172.168.4.x` 必须在**跳板机 ott9-a31** 上执行（控制机到该网段不通，
> 直接跑会挂住）。控制机上一律用 `ssh root@ott9-a31 '<命令>'` 包裹。

```bash
# 1) 当前用的是哪一档
grep -n '^deploy_config' ansible/inventories/a3/group_vars/all.yml
#    干净档 = 1m-v030-lw（无 patch、无 reuse）；1m-v030-hyb 是 reuse 实验档，in-use 会误导结论

# 2) 四引擎 + proxy
for h in 172.168.4.237 172.168.4.31; do curl -s -o /dev/null -w "$h:9081=%{http_code}\n" http://$h:9081/health; done
for h in 172.168.4.230 172.168.4.226; do curl -s -o /dev/null -w "$h:9900=%{http_code}\n" http://$h:9900/health; done
curl -s http://172.168.4.237:8000/healthcheck

# 3) 生成脚本是否干净（混进 reuse/patch 就是档配错了）
F=ansible/generated/run_dp_template_p0.sh
grep -cE 'use_layerwise_buffer_reuse|layerwise_num_shared_buffers|probe/patch' $F   # 必须 0
```

判断「集群是否 OK」**不要用日志 mtime**——D 引擎不保证按请求写日志。用
`curl http://<d_ip>:9900/metrics | grep '^vllm:request_success_total'` 的累计值。

## 部署

```bash
cd ansible
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml    # 改配置后必跑
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml  # 约 12 分钟
```

`PLAY RECAP` 四个节点 `failed=0` 才算跑完；P 先起、D 后起是正常的。

## 验收判据（缺一不可）

1. 四个引擎 `/health` 都是 200；
2. proxy `/healthcheck` = `{"status":"ok","prefill_instances":2,"decode_instances":2}`；
3. 短请求正确；**>4096 token 的长请求也正确**（验证 chunked prefill + KV 池）；
4. 两个 decode 的 `request_success_total` 都在涨；
5. 四节点 `tail -2000 vllm_*.log | grep -c ERROR` 全为 0。

## 两条硬约束（违反就起不来）

**`max_model_len` 不能给 1M**：不挂 reuse 补丁就没有 KV 预算放大，TP16/1M 需要 54.14 GiB
但只有 37.4 GiB，启动即 `Engine core initialization failed`。当前用 `262144`。

**必须 `hixl_preset: roce`**：缺了会走 hccs P2P，同机 `devPhyId 0↔2` 建链失败
（`aclrtIpcMemImportByKey 107000` / `halDeviceDisableP2P drvRetCode=17`）→ 首请求
`Segfault encountered` + `TBE Subprocess ... main process disappeared`。

## 排障顺序

1. `tail -50 /mnt/sfs_turbo/g00832294/logs/vllm_p0.log`（P 先崩的多）
2. `tail -20 .../logs/proxy.log`（看是 tokenize 失败还是上游 500）
3. `/mnt/sfs_turbo/g00832294/logs/ascend/{p0,d0}/run/device-*/device-*.log`（设备侧）
4. 配置类问题回看 `ansible/generated/run_dp_template_*.sh` 是否真的带上了参数

## 红线

- **不要把 `1m-v030-hyb` 当基线**：那档带 buffer reuse 与诊断挂载，reuse 在 mooncake
  block_key 数据面上已知不可用（共享槽位的传输完成语义没有保证），细节见
  `docs/reuse-buffer-debug-status.md` §11–§13。
- **不要在干净档上挂 `probe/patch/*`**：会引入 producer/consumer 语义错配（D 起不来）。
- 重启前如 `vllm_*.log` 过大，先 `: >` 截断，避免占满共享盘。

## 详细文档

`docs/a3-v030-layerwise-onboarding.md` —— 拓扑、参数、验收清单、故障速查、proxy 选路逻辑。
