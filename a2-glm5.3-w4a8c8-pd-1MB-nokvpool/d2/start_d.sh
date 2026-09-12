dp_rank_start=2
node_p0_ip=172.16.0.71

python launch_online_dp.py --dp-size 4 --tp-size 8 --dp-size-local 1 --dp-rank-start $dp_rank_start --dp-address $node_p0_ip --dp-rpc-port 16600 --vllm-start-port 9900
