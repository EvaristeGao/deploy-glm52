# 干净配置档：`1m-v030-lw`（v0.30 原生 layerwise + mooncake，不含 reuse）

这份配置只使用 **v0.30.0rc1 镜像自带的 layerwise KV 池能力**：

- 不挂任何 patch 文件（容器内全部走镜像原文件）；
- 不开 buffer reuse（`use_layerwise_buffer_reuse` / `layerwise_num_shared_buffers` /
  `layerwise_independent_layers` 三个键一个都不渲染）；
- 不加诊断日志、不开实验开关。

## 切换方式

```bash
cd ansible
# group_vars/all.yml: deploy_config: 1m-v030-lw
export ANSIBLE_LOCAL_TEMP=$PWD/.ansible-tmp ANSIBLE_HOST_KEY_CHECKING=False
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/gen.yml
.venv/bin/ansible-playbook -i inventories/a3/inventory.yaml playbooks/start.yml
```

## 关键参数（`_1m_v030_lw` 档）

| 位置 | 参数 | 值 |
|---|---|---|
| prefill / decode KV 连接 | `kv_connector` | `AscendStoreConnector`（单实例） |
| | `backend` | `mooncake` |
| | `use_layerwise` | `true` |
| | `layerwise_prefetch_layers` | `2` |
| | `layerwise_max_transfer_blocks` | `64` |
| | `layerwise_max_transfer_bytes` | `16777216`（16 MiB） |
| prefill | `tp_size` / `dp_size` | 16 / 2（与 decode 的 TP 必须一致） |
| | `max_num_batched_tokens` | 4096 |
| | `max_num_seqs` | 64 |
| | `enable_chunked_prefill` | `true` |
| decode | `tp_size` / `dcp` | 16 / 16 |
| | `enforce_eager` | `true` |

## 生成脚本自检（提交前实测）

```bash
F=ansible/generated/run_dp_template_p0.sh
grep -cE 'use_layerwise_buffer_reuse|layerwise_num_shared_buffers|layerwise_independent_layers|probe/patch' $F   # → 0
grep -oE '"backend": "[a-z]+"|"use_layerwise": [a-z]+|"layerwise_[a-z_]+": [0-9]+' $F | sort -u
#   "backend": "mooncake"
#   "use_layerwise": true
#   "layerwise_max_transfer_blocks": 64
#   "layerwise_max_transfer_bytes": 16777216
#   "layerwise_prefetch_layers": 2
```

D 侧（`run_dp_template_d0.sh`）同样零命中，且不含 `use_layerwise_buffer_reuse`。

## 为什么单独留这一档

`1m-v030-hyb` 那档带着 buffer reuse 与一批实验开关（`num_shared_buffers`、诊断
挂载、`max_num_seqs=1` 等），用于排查「共享槽位的传输完成语义」问题。
线上/对外验证需要一个**只代表上游能力**的基准档，便于回答"不复用时是否一切正常"
以及"问题是我们加的机制还是上游的"。

buffer reuse 的排查结论见 [reuse-buffer-debug-status.md](reuse-buffer-debug-status.md) §11–§13。
