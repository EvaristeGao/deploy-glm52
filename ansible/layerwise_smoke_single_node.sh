#!/usr/bin/env bash
# =============================================================================
# layerwise 单节点最小验证（Mooncake 分层 KV offload · TP8 · kv_both）
#
# 用途：在正式跑 4 节点 2P+2D（deploy_config: 1m-v030-lw）之前，先用**一个节点、
#       一个 co-located 实例**把机制跑通：单实例既存又取（kv_role=kv_both），
#       不涉及 P/D 分离、不涉及 CP/DCP、不涉及 P/D TP 一致性 —— 正好绕开 layerwise
#       的三条硬约束（见 docs/a3-layerwise-kv-offload.md §3）。
#
# 官方文档的原话是「首次硬件验证从 TP=1 开始」，但 GLM-5.2 单卡放不下，所以这里用
# 本模型在单节点可行且最小的 TP（8 卡，节点共 16 卡）。其余按官方建议：
#   eager、prefetch_layers=1、chunked-prefill、max-num-batched-tokens 4096。
#
# 前置条件（缺一不可）：
#   1) 节点上的 engine 镜像里的 mooncake client 必须带 PR #2881 的 7 个
#      range/session 接口。否则启动会 fail fast：
#        "Mooncake client does not support batch_put_session_start" /
#        "must include Mooncake PR #2881"
#      默认用 image_build/Dockerfile_mooncake_layerwise 构建出来的镜像，用 IMAGE=<其它镜像> 覆盖。
#   2) p0 上已有 mooncake_master（本项目 kvpool 单主），且 mooncake.json 指向它。
#      下面默认从 ansible/generated/mooncake.json 拷（先跑一次 gen.yml 生成）。
#   3) 该节点有模型权重（默认 /mnt/sfs_turbo/models/GLM-5.2-w4a8c8）。
#
# 用法（在能 ssh 该节点、且已 ssh 到节点上时执行；脚本本身在节点上跑）：
#   IMAGE=<你的镜像> ./layerwise_smoke_single_node.sh
#   # 或指定节点/端口/上下文：
#   IMAGE=... NODE=ott9-a31 PORT=9088 MAX_MODEL_LEN=131072 ./layerwise_smoke_single_node.sh
#
# 验证：脚本会打 /health，并打印一条 curl 示例。请按 docs 里的验收清单对比
#       「冷/温 token 一致性 + remote hit 为正 + 多个 chunk 边界」。
# =============================================================================
set -euo pipefail

# 默认用 image_build/Dockerfile_mooncake_layerwise 构建的新镜像（已实测：mooncake 0.3.13.post1，
# 含 PR #2881 的 7 个 range/session 接口 + mooncake_master）；用 IMAGE= 覆盖
IMAGE=${IMAGE:-glm52-vllm-ascend:v0.30.0rc1-a3-mc0.3.13}
NAME=${NAME:-glm52-layerwise-smoke}
MODEL=${MODEL:-/mnt/sfs_turbo/models/GLM-5.2-w4a8c8}
NIC=${NIC:-enp23s0f3}
PORT=${PORT:-9088}
TP=${TP:-8}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-131072}
MAX_NUM_BATCHED_TOKENS=${MAX_NUM_BATCHED_TOKENS:-4096}
GPU_MEM=${GPU_MEM:-0.85}
# kvpool 的 mooncake.json（默认取仓库 gen 出来的；也可直接给节点上的路径）
MOONCAKE_JSON=${MOONCAKE_JSON:-}
LOGS_DIR=${LOGS_DIR:-/mnt/sfs_turbo/g00832294/logs}
RANGE_DEBUG=${RANGE_DEBUG:-0}     # 1 = 打每层 range/commit 的 JSON 审计日志（排查用）

LOCAL_IP=$(ip -4 addr show "$NIC" | grep -oP 'inet \K[0-9.]+' | head -1)
[ -n "$LOCAL_IP" ] || { echo "无法从 $NIC 取到 IP"; exit 1; }

if [ -z "$MOONCAKE_JSON" ]; then
  MOONCAKE_JSON=$(ls "$(dirname "$0")"/../generated/mooncake.json 2>/dev/null \
                  || ls "$(dirname "$0")"/generated/mooncake.json 2>/dev/null \
                  || true)
fi
[ -n "$MOONCAKE_JSON" ] && [ -f "$MOONCAKE_JSON" ] || {
  echo "找不到 mooncake.json（先跑 ansible-playbook ... playbooks/gen.yml，或用 MOONCAKE_JSON= 指定）"; exit 1; }

echo "== layerwise smoke =="
echo "  image      : $IMAGE"
echo "  model      : $MODEL"
echo "  本机 nic/ip: $NIC / $LOCAL_IP"
echo "  port       : $PORT   tp: $TP   max-model-len: $MAX_MODEL_LEN"
echo "  mooncake   : $MOONCAKE_JSON"
echo "  range debug: $RANGE_DEBUG"

# ---------- 1) 起容器（与 start.yml 的引擎容器保持一致） ----------
docker rm -f "$NAME" 2>/dev/null || true
DEVICES=""
for i in $(seq 0 15); do DEVICES="$DEVICES --device /dev/davinci$i"; done
# shellcheck disable=SC2086
docker run -itd --name "$NAME" \
  --net=host --privileged --shm-size=1024g \
  $DEVICES \
  --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
  -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /etc/hccn.conf:/etc/hccn.conf \
  -v /mnt/sfs_turbo:/mnt/sfs_turbo \
  "$IMAGE" bash

docker exec "$NAME" mkdir -p /root/pd "$LOGS_DIR"
docker cp "$MOONCAKE_JSON" "$NAME":/root/pd/mooncake.json

# ---------- 2) 启动 co-located 实例（kv_both + use_layerwise） ----------
# 与官方示例一致的关键项：eager / prefix-caching / chunked-prefill / 小 chunk，
# AscendStoreConnector + backend=mooncake + use_layerwise；kv_role=kv_both 单实例自取自存。
docker exec -d "$NAME" bash -c "
export NIC_NAME=$NIC
export LOCAL_IP=$LOCAL_IP
export MOONCAKE_CONFIG_PATH=/root/pd/mooncake.json
export HCCL_IF_IP=\$LOCAL_IP
export GLOO_SOCKET_IFNAME=\$NIC_NAME
export TP_SOCKET_IFNAME=\$NIC_NAME
export HCCL_SOCKET_IFNAME=\$NIC_NAME
export OMP_PROC_BIND=false
export OMP_NUM_THREADS=1
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export HCCL_BUFFSIZE=400
export HCCL_OP_EXPANSION_MODE=AIV
export ACL_OP_INIT_MODE=1
export ASCEND_A3_ENABLE=1
export VLLM_ASCEND_ENABLE_FUSED_MC2=1
export VLLM_ASCEND_ENABLE_MLAPO=1
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
export LD_LIBRARY_PATH=\$LD_LIBRARY_PATH:/usr/local/lib
export VLLM_ASCEND_KVPOOL_RANGE_DEBUG=$RANGE_DEBUG
vllm serve $MODEL \
  --host 0.0.0.0 --port $PORT \
  --tensor-parallel-size $TP \
  --enable-expert-parallel \
  --enforce-eager \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --max-num-batched-tokens $MAX_NUM_BATCHED_TOKENS \
  --served-model-name glm-52 \
  --max-model-len $MAX_MODEL_LEN \
  --quantization ascend \
  --gpu-memory-utilization $GPU_MEM \
  --trust-remote-code \
  --kv-transfer-config '{
    \"kv_connector\": \"AscendStoreConnector\",
    \"kv_role\": \"kv_both\",
    \"kv_connector_extra_config\": {
      \"backend\": \"mooncake\",
      \"use_layerwise\": true,
      \"layerwise_prefetch_layers\": 1,
      \"layerwise_max_transfer_blocks\": 64,
      \"layerwise_max_transfer_bytes\": 16777216
    }
  }' >> $LOGS_DIR/layerwise_smoke_$PORT.log 2>&1
"

echo "  已后台启动，日志：$LOGS_DIR/layerwise_smoke_$PORT.log"
echo "  等待就绪（最多 40 分钟；1M 级模型加载慢）..."
for i in $(seq 1 160); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$LOCAL_IP:$PORT/health" || true)
  if [ "$code" = "200" ]; then echo "  ✅ 就绪：http://$LOCAL_IP:$PORT/health"; break; fi
  sleep 15
done
[ "${code:-}" = "200" ] || { echo "  ❌ 未就绪，检查日志：docker exec $NAME tail -100 $LOGS_DIR/layerwise_smoke_$PORT.log"; exit 1; }

echo
echo "== 冒烟 =="
curl -s --max-time 120 "http://$LOCAL_IP:$PORT/v1/completions" -H 'Content-Type: application/json' \
  -d '{"model":"glm-52","prompt":"The future of AI is","max_tokens":16,"temperature":0}' | head -c 400
echo
echo
echo "== 验收提示（详见 docs/a3-layerwise-kv-offload.md §6）=="
echo "  1) 同一长 prompt 连发两次：第 2 次应命中远端（prefix cache hit > 0），且输出 token 与第 1 次一致"
echo "  2) 用 RANGE_DEBUG=1 重跑，日志里应有 [KVPOOL_RANGE_DEBUG] 的 whole_key / 逐层 range / commit 事件"
echo "  3) 出现 'Mooncake client does not support batch_*' 说明镜像的 mooncake client 还没带 PR #2881"
