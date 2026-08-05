#!/usr/bin/bash
# run_dp_template.sh — Decode 节点模板（kv_consumer，MultiConnector + AscendStore）
# 以跑通的 a2.md 为蓝本。占位符由 deploy.sh 按节点替换。
nic_name="__NIC__"
local_ip="__LOCAL_IP__"

export HCCL_IF_IP=$local_ip
export GLOO_SOCKET_IFNAME=$nic_name
export TP_SOCKET_IFNAME=$nic_name
export HCCL_SOCKET_IFNAME=$nic_name
export VLLM_HOST_IP=$local_ip

export OMP_PROC_BIND=false
export OMP_NUM_THREADS=10
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_ASCEND_ENABLE_MLAPO=1
export HCCL_BUFFSIZE=2560
export TASK_QUEUE_ENABLE=1
export HCCL_OP_EXPANSION_MODE="AIV"

export VLLM_USE_V1=1
export ASCEND_RT_VISIBLE_DEVICES=$1
export LD_LIBRARY_PATH=/usr/local/python3.11.10/lib:/usr/local/lib:$LD_LIBRARY_PATH

export PYTHONHASHSEED=0
export MOONCAKE_CONFIG_PATH="__MOONCAKE_CONFIG_PATH__"
if [ "__CLUSTER_TYPE__" = "a2" ]; then
  export HCCL_INTRA_ROCE_ENABLE=1
fi

export ACL_OP_INIT_MODE=1

vllm serve __MODEL_PATH__ \
    --host 0.0.0.0 \
    --port $2 \
    --data-parallel-size $3 \
    --data-parallel-rank $4 \
    --data-parallel-address $5 \
    --data-parallel-rpc-port $6 \
    --tensor-parallel-size $7 \
    --enable-expert-parallel \
    --enable-prefix-caching \
    --seed 1024 \
    --served-model-name __MODEL_NAME__ \
    --async-scheduling \
    --max-model-len __MAX_MODEL_LEN__ \
    --max-num-batched-tokens 256 \
    --trust-remote-code \
    --max-num-seqs 128 \
    --gpu-memory-utilization 0.92 \
    --safetensors-load-strategy prefetch \
    --quantization ascend \
    --enable-auto-tool-choice \
    --tool-call-parser glm47 \
    --reasoning-parser glm45 \
    --kv-transfer-config \
    '{
    "kv_connector": "MultiConnector",
    "kv_role": "kv_consumer",
    "kv_load_failure_policy": "recompute",
    "kv_connector_extra_config": {
        "connectors": [
            {
                "kv_connector": "MooncakeConnectorV1",
                "kv_role": "kv_consumer",
                "kv_port": "__KV_PORT__",
                "kv_connector_extra_config": {
                    "prefill": { "dp_size": __PREFILL_DP__, "tp_size": __PREFILL_TP__ },
                    "decode": { "dp_size": __DECODE_DP__, "tp_size": __DECODE_TP__ }
                }
            },
            {
                "kv_connector": "AscendStoreConnector",
                "kv_role": "kv_consumer",
                "kv_connector_extra_config": {
                    "lookup_rpc_port":"0",
                    "load_async": true,
                    "backend": "mooncake"
                }
            }
        ]
    }
    }' \
     --compilation-config \
    '{
        "cudagraph_mode": "FULL_DECODE_ONLY",
        "cudagraph_capture_sizes": [4,8,16,24,32,40,48,56,64,96,128,160,192,224,256,298,320,352,384]
    }' \
    --additional-config '{"enable_flashcomm1": false, "enable_dsa_cp": false, "ascend_compilation_config": {"enable_npugraph_ex": true, "enable_static_kernel": false}, "fuse_muls_add": true, "multistream_overlap_shared_expert": true, "enable_mc2_hierarchy_comm": false, "enable_sparse_sfa_c8": false, "enable_sparse_li_c8": true, "enable_cpu_binding": true, "recompute_scheduler_enable": true}' \
    --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'