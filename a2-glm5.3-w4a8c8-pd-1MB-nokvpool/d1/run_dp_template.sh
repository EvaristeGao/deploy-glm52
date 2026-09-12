export VLLM_RPC_TIMEOUT=3600000
export VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS=30000
export HCCL_EXEC_TIMEOUT=204
export HCCL_CONNECT_TIMEOUT=120

# 自动获取配置
nic_name="enp67s0f5"
local_ip=172.16.0.108

# 以下环境变量无需修改
export HCCL_IF_IP=$local_ip
export GLOO_SOCKET_IFNAME=$nic_name
export TP_SOCKET_IFNAME=$nic_name
export HCCL_SOCKET_IFNAME=$nic_name
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
#export LD_LIBRARY_PATH=/usr/local/python3.11.10/lib/python3.11/site-packages/mooncake:$LD_LIBRARY_PATH

export PYTHONHASHSEED=0
#export MOONCAKE_CONFIG_PATH="/workspace/mooncake.json"
export HCCL_INTRA_ROCE_ENABLE=1

export ACL_OP_INIT_MODE=1

# vllm起服务配置
vllm serve /mnt/sfs_turbo/models/GLM-5.3-w4a8c8 \
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
    --served-model-name glm-5 \
    --async-scheduling \
    --max-model-len 1024000 \
    --max-num-batched-tokens 16 \
    --trust-remote-code \
    --max-num-seqs 16 \
    --prefill-context-parallel-size 1 \
    --decode-context-parallel-size 8 \
    --cp-kv-cache-interleave-size 128 \
    --gpu-memory-utilization 0.9 \
    --safetensors-load-strategy prefetch \
    --quantization ascend \
    --enable-auto-tool-choice \
    --tool-call-parser glm47 \
    --reasoning-parser glm45 \
    --kv-transfer-config \
   '{"kv_connector": "MooncakeConnectorV1",
  "kv_role": "kv_consumer",
  "kv_port": "30100",
  "engine_id": "1",
  "kv_connector_extra_config": {
    "use_ascend_direct": true,
    "prefill": {
   "dp_size": 4,
   "tp_size": 8
    },
    "decode": {
   "dp_size": 4,
   "tp_size": 8
    }
  }
   }' \
     --compilation-config \
    '{
        "cudagraph_mode": "FULL_DECODE_ONLY",
        "cudagraph_capture_sizes": [4,8,16]
    }' \
    --profiler-config \
    '{
        "profiler": "torch", 
        "torch_profiler_dir": "/home/lh/prof", 
        "torch_profiler_with_stack": false
    }' \
    --additional-config '{"enable_flashcomm1": false, "enable_dsa_cp": false, "ascend_compilation_config": {"enable_npugraph_ex": true, "enable_static_kernel": false}, "fuse_muls_add": true, "multistream_overlap_shared_expert": true, "enable_mc2_hierarchy_comm": false, "enable_sparse_sfa_c8": true, "enable_sparse_li_c8": true, "enable_cpu_binding": true, "recompute_scheduler_enable": true}' \
    --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'

