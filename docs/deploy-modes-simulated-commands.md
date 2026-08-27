# deploy-glm52 部署模式 · 完整模拟命令（逐步骤）

> 由 `ansible/simulate_modes.py` 生成：jinja2 复刻 Ansible 渲染（when 判定 / set_fact 派生 / shell·template·copy 渲染），每个 shell 命令跑 `bash -n`。
> 基座：a2 inventory（8 节点，`ip` = 真实网卡 IP 116.204.x）。复现：`cd ansible && .venv/bin/python simulate_modes.py`
> 四模式 shell 命令全部 bash 语法通过：内嵌 69 / 独立 64 / 单主 51 / 无 kvpool 41。
> standalone 独立 haproxy 容器镜像 = `haproxy.image`（`haproxy-etcd:latest`，构建自 image_build/Dockerfile_ha）。

---


==========================================================================================
模式: HA_per_container — kvpool HA · 内嵌 haproxy（默认）
   enabled=True  enable_ha=True  haproxy.mode=per_container
==========================================================================================

## gen.yml
- 确保 ansible/generated/ 目录存在（generated/ 在 .gitignore，首次运行需创建）：(非命令任务，跳过渲染)
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d0]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d1]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d2]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d3]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p0]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p1]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p2]
    [run_dp_prefill_template.sh.j2] 3545 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p3]
    [run_dp_prefill_template.sh.j2] 3548 字符
- 渲染 mooncake.json（kvpool HA：master 经 etcd://haproxy 发现）
    | {
    |   "metadata_server": "P2PHANDSHAKE",
    |   "protocol": "ascend",
    |   "device_name": "",
    |   "metadata_server": "etcd://127.0.0.1:12489",
    |   "master_server_address": "etcd://127.0.0.1:12489",
    |   "global_segment_size": "80GB",
    |   "default_kv_lease_ttl": 11000
    | }
- [跳过] 渲染 mooncake.json（kvpool 单主：master 直连 p0 rpc 端口，待实机确认）

## start.yml

### 任务 1. 校验部署模式组合（ha+etcd 依赖 kvpool）
  [d0] assert ['not mooncake.ha.enable_ha or mooncake.enabled'] → 通过 ✅

### 任务 2. 删除旧容器（幂等，对照 deploy.sh 的 docker rm -f $CONTAINER_NAME）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker rm -f glm52-ansible-test 2>/dev/null || true

### 任务 3. 起容器（对照 deploy.sh start_node 的 docker run，无遗漏）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker run -itd --name glm52-ansible-test \
    | --net=host --privileged --shm-size=1024g \
    | --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
    | --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
    | -v /usr/local/dcmi:/usr/local/dcmi \
    | -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
    | -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    | -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    | -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
    | -v /etc/ascend_install.info:/etc/ascend_install.info \
    | -v /etc/hccn.conf:/etc/hccn.conf \
    | -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    | -v /mnt/share_space/:/mnt/share_space/ -v /data2:/data2 \
    | \
    | quay.io/ascend/vllm-ascend:v0.23.0 bash

### 任务 4. 校验 generated/mooncake.json 存在（generated/ 被 .gitignore 忽略，首次部署必缺）
  [localhost] stat（假设 generated 已由 gen 生成，exists=true）

### 任务 5. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../launch_online_dp.py', 'dest': '/tmp/launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../launch_online_dp.py → /tmp/launch_online_dp.py

### 任务 6. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../generated/mooncake.json', 'dest': '/tmp/mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../generated/mooncake.json → /tmp/mooncake.json

### 任务 7. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/launch_online_dp.py glm52-ansible-test:/root/pd/

### 任务 8. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/mooncake.json glm52-ansible-test:/root/pd/

### 任务 9. 起 etcd 容器（3 节点，证书挂载，双向证书认证；仅 kvpool+HA）
  执行于 [p0]  bash-OK
    | docker rm -f etcd-p0 2>/dev/null || true
    | docker run -d --name etcd-p0 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s1 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://116.204.91.141:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://116.204.91.141:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key
  执行于 [p1]  bash-OK
    | docker rm -f etcd-p1 2>/dev/null || true
    | docker run -d --name etcd-p1 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s2 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://113.44.111.127:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://113.44.111.127:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key
  执行于 [p2]  bash-OK
    | docker rm -f etcd-p2 2>/dev/null || true
    | docker run -d --name etcd-p2 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s3 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://121.37.88.17:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://121.37.88.17:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key

### 任务 10. 起 mooncake 容器（HA：mooncake.ha.nodes；单主：proxy 节点；独立于 vllm-ascend，含 mooncake_master+haproxy+nc）
  执行于 [p0]  bash-OK
    | docker rm -f mooncake-p0 2>/dev/null || true
    | docker run -itd --name mooncake-p0 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake
  执行于 [p1]  bash-OK
    | docker rm -f mooncake-p1 2>/dev/null || true
    | docker run -itd --name mooncake-p1 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake
  执行于 [p2]  bash-OK
    | docker rm -f mooncake-p2 2>/dev/null || true
    | docker run -itd --name mooncake-p2 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake

### 任务 11. 分发 haproxy 脚本进节点 /tmp（gen_haproxy_etcd.sh + start-ha.sh，原版） [../ha/gen_haproxy_etcd.sh]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../ha/gen_haproxy_etcd.sh → /tmp/gen_haproxy_etcd.sh

### 任务 12. 分发 haproxy 脚本进节点 /tmp（gen_haproxy_etcd.sh + start-ha.sh，原版） [../ha/start-ha.sh]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../ha/start-ha.sh → /tmp/start-ha.sh

### 任务 13. docker cp 脚本进 mooncake 容器并生成/启动 haproxy（CacheMaster 12379）
  执行于 [p0]  bash-OK
    | docker exec mooncake-p0 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh mooncake-p0:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh mooncake-p0:/home/ma-user/haproxy/
    | docker exec mooncake-p0 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'
  执行于 [p1]  bash-OK
    | docker exec mooncake-p1 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh mooncake-p1:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh mooncake-p1:/home/ma-user/haproxy/
    | docker exec mooncake-p1 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'
  执行于 [p2]  bash-OK
    | docker exec mooncake-p2 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh mooncake-p2:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh mooncake-p2:/home/ma-user/haproxy/
    | docker exec mooncake-p2 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'

### 任务 14. docker cp 脚本进 vllm-ascend 容器并生成/启动 haproxy（P/D 引擎 12489）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh glm52-ansible-test:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh glm52-ansible-test:/home/ma-user/haproxy/
    | docker exec glm52-ansible-test bash -c \
    |   'export HAP_LISTEN_START_PORT=12489; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'

### 任务 15. 容器内起 mooncake_master（HA：enable_ha+etcd 选主；单主：直连）
  执行于 [p0]  bash-OK
    | docker exec mooncake-p0 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p0 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p0 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 127.0.0.1:12379 \
    |     --rpc-address 116.204.91.141 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p0.log 2>&1'
    | sleep 2 && docker exec mooncake-p0 pgrep -x mooncake_master >/dev/null
  执行于 [p1]  bash-OK
    | docker exec mooncake-p1 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p1 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p1 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 127.0.0.1:12379 \
    |     --rpc-address 113.44.111.127 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p1.log 2>&1'
    | sleep 2 && docker exec mooncake-p1 pgrep -x mooncake_master >/dev/null
  执行于 [p2]  bash-OK
    | docker exec mooncake-p2 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p2 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p2 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 127.0.0.1:12379 \
    |     --rpc-address 121.37.88.17 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p2.log 2>&1'
    | sleep 2 && docker exec mooncake-p2 pgrep -x mooncake_master >/dev/null

### 任务 16. 渲染 run_dp_template.sh（每节点，dest 为节点 /tmp，模板用 inventory ip 字段）
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.125.57"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.33.178"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.115.71"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.64.115"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="116.204.91.141"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="113.44.111.127"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3545 字符)
          | local_ip="121.37.88.17"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3548 字符)
          | local_ip="116.204.121.119"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'

### 任务 17. docker cp 模板进容器 /root/pd/run_dp_template.sh（对照 deploy.sh start_node）
  执行于 [d0]  bash-OK
    | docker cp /tmp/run_dp_template_d0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d1]  bash-OK
    | docker cp /tmp/run_dp_template_d1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d2]  bash-OK
    | docker cp /tmp/run_dp_template_d2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d3]  bash-OK
    | docker cp /tmp/run_dp_template_d3.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p0]  bash-OK
    | docker cp /tmp/run_dp_template_p0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p1]  bash-OK
    | docker cp /tmp/run_dp_template_p1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p2]  bash-OK
    | docker cp /tmp/run_dp_template_p2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p3]  bash-OK
    | docker cp /tmp/run_dp_template_p3.sh glm52-ansible-test:/root/pd/run_dp_template.sh

### 任务 18. 启动引擎（对照 deploy.sh start_engines，docker exec -d 后台）
  执行于 [d0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 0 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d0.log 2>&1'
  执行于 [d1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 2 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d1.log 2>&1'
  执行于 [d2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 4 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d2.log 2>&1'
  执行于 [d3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 6 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d3.log 2>&1'
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 0 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p0.log 2>&1'
  执行于 [p1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 1 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p1.log 2>&1'
  执行于 [p2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 2 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p2.log 2>&1'
  执行于 [p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 3 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p3.log 2>&1'

### 任务 19. 等待全部实例就绪（uri + until，遍历实例清单）
  [d0] (非命令任务：file/uri 等，跳过渲染)

### 任务 20. 分发 proxy 脚本到节点 /tmp（对照 deploy.sh 的 scpn）
  执行于 [p0]

### 任务 21. docker cp proxy 脚本进容器并启动（对照 deploy.sh start_proxy，pkill 锚定 ^python3）
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd 2>/dev/null || true
    | docker cp /tmp/load_balance_proxy_server_example.py glm52-ansible-test:/root/pd/
    | docker exec glm52-ansible-test chmod +x /root/pd/load_balance_proxy_server_example.py
    | docker exec glm52-ansible-test pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'unset http_proxy https_proxy; python3 /root/pd/load_balance_proxy_server_example.py \
    |   --host 0.0.0.0 --port 1999 \
    |   --prefiller-hosts 116.204.91.141 113.44.111.127 121.37.88.17 116.204.121.119 --prefiller-ports 9081 9081 9081 9081 \
    |   --decoder-hosts 116.204.125.57 116.204.125.57 116.204.33.178 116.204.33.178 116.204.115.71 116.204.115.71 116.204.64.115 116.204.64.115 --decoder-ports 9900 9901 9900 9901 9900 9901 9900 9901 >> /mnt/share_space/g00832294/deploy-glm52/logs/proxy.log 2>&1'

### 任务 22. 等待 proxy /healthcheck 就绪（uri + until，对照 deploy.sh wait_ready "proxy"）
  [p0] (非命令任务：file/uri 等，跳过渲染)

### 任务 23. 冒烟测试（control 端 POST /v1/chat/completions，对照 deploy.sh smoke_test）
  [p0] (非命令任务：file/uri 等，跳过渲染)

—— 汇总：shell 命令 69 条，语法失败 0 条 ✅

==========================================================================================
模式: HA_standalone — kvpool HA · 独立 haproxy
   enabled=True  enable_ha=True  haproxy.mode=standalone
==========================================================================================

## gen.yml
- 确保 ansible/generated/ 目录存在（generated/ 在 .gitignore，首次运行需创建）：(非命令任务，跳过渲染)
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d0]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d1]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d2]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d3]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p0]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p1]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p2]
    [run_dp_prefill_template.sh.j2] 3545 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p3]
    [run_dp_prefill_template.sh.j2] 3548 字符
- 渲染 mooncake.json（kvpool HA：master 经 etcd://haproxy 发现）
    | {
    |   "metadata_server": "P2PHANDSHAKE",
    |   "protocol": "ascend",
    |   "device_name": "",
    |   "metadata_server": "etcd://116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379",
    |   "master_server_address": "etcd://116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379",
    |   "global_segment_size": "80GB",
    |   "default_kv_lease_ttl": 11000
    | }
- [跳过] 渲染 mooncake.json（kvpool 单主：master 直连 p0 rpc 端口，待实机确认）

## start.yml

### 任务 1. 校验部署模式组合（ha+etcd 依赖 kvpool）
  [d0] assert ['not mooncake.ha.enable_ha or mooncake.enabled'] → 通过 ✅

### 任务 2. 删除旧容器（幂等，对照 deploy.sh 的 docker rm -f $CONTAINER_NAME）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker rm -f glm52-ansible-test 2>/dev/null || true

### 任务 3. 起容器（对照 deploy.sh start_node 的 docker run，无遗漏）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker run -itd --name glm52-ansible-test \
    | --net=host --privileged --shm-size=1024g \
    | --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
    | --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
    | -v /usr/local/dcmi:/usr/local/dcmi \
    | -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
    | -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    | -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    | -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
    | -v /etc/ascend_install.info:/etc/ascend_install.info \
    | -v /etc/hccn.conf:/etc/hccn.conf \
    | -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    | -v /mnt/share_space/:/mnt/share_space/ -v /data2:/data2 \
    | \
    | quay.io/ascend/vllm-ascend:v0.23.0 bash

### 任务 4. 校验 generated/mooncake.json 存在（generated/ 被 .gitignore 忽略，首次部署必缺）
  [localhost] stat（假设 generated 已由 gen 生成，exists=true）

### 任务 5. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../launch_online_dp.py', 'dest': '/tmp/launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../launch_online_dp.py → /tmp/launch_online_dp.py

### 任务 6. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../generated/mooncake.json', 'dest': '/tmp/mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../generated/mooncake.json → /tmp/mooncake.json

### 任务 7. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/launch_online_dp.py glm52-ansible-test:/root/pd/

### 任务 8. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/mooncake.json glm52-ansible-test:/root/pd/

### 任务 9. 起 etcd 容器（3 节点，证书挂载，双向证书认证；仅 kvpool+HA）
  执行于 [p0]  bash-OK
    | docker rm -f etcd-p0 2>/dev/null || true
    | docker run -d --name etcd-p0 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s1 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://116.204.91.141:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://116.204.91.141:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key
  执行于 [p1]  bash-OK
    | docker rm -f etcd-p1 2>/dev/null || true
    | docker run -d --name etcd-p1 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s2 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://113.44.111.127:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://113.44.111.127:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key
  执行于 [p2]  bash-OK
    | docker rm -f etcd-p2 2>/dev/null || true
    | docker run -d --name etcd-p2 --network host \
    |   -v /mnt/share_space/certs:/certs \
    |   quay.io/coreos/etcd:v3.5.16 \
    |   etcd --name s3 --data-dir /etcd-data \
    |   --listen-client-urls https://0.0.0.0:2379 \
    |   --advertise-client-urls https://121.37.88.17:2379 \
    |   --listen-peer-urls https://0.0.0.0:2380 \
    |   --initial-advertise-peer-urls https://121.37.88.17:2380 \
    |   --initial-cluster s1=https://116.204.91.141:2380,s2=https://113.44.111.127:2380,s3=https://121.37.88.17:2380 \
    |   --client-cert-auth --trusted-ca-file=/certs/ca.crt --cert-file=/certs/server.crt --key-file=/certs/server.key \
    |   --peer-client-cert-auth --peer-trusted-ca-file=/certs/ca.crt --peer-cert-file=/certs/server.crt --peer-key-file=/certs/server.key

### 任务 10. 起 mooncake 容器（HA：mooncake.ha.nodes；单主：proxy 节点；独立于 vllm-ascend，含 mooncake_master+haproxy+nc）
  执行于 [p0]  bash-OK
    | docker rm -f mooncake-p0 2>/dev/null || true
    | docker run -itd --name mooncake-p0 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake
  执行于 [p1]  bash-OK
    | docker rm -f mooncake-p1 2>/dev/null || true
    | docker run -itd --name mooncake-p1 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake
  执行于 [p2]  bash-OK
    | docker rm -f mooncake-p2 2>/dev/null || true
    | docker run -itd --name mooncake-p2 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake

### 任务 11. 分发 haproxy 脚本进节点 /tmp（gen_haproxy_etcd.sh + start-ha.sh，原版） [../ha/gen_haproxy_etcd.sh]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../ha/gen_haproxy_etcd.sh → /tmp/gen_haproxy_etcd.sh

### 任务 12. 分发 haproxy 脚本进节点 /tmp（gen_haproxy_etcd.sh + start-ha.sh，原版） [../ha/start-ha.sh]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../ha/start-ha.sh → /tmp/start-ha.sh

### 任务 13. 起独立 haproxy 容器（standalone：haproxy.nodes 各 1 个）
  执行于 [p0]  bash-OK
    | docker rm -f haproxy-p0 2>/dev/null || true
    | docker run -itd --name haproxy-p0 --network host \
    |   --entrypoint=bash \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   haproxy-etcd:latest
  执行于 [p1]  bash-OK
    | docker rm -f haproxy-p1 2>/dev/null || true
    | docker run -itd --name haproxy-p1 --network host \
    |   --entrypoint=bash \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   haproxy-etcd:latest
  执行于 [p2]  bash-OK
    | docker rm -f haproxy-p2 2>/dev/null || true
    | docker run -itd --name haproxy-p2 --network host \
    |   --entrypoint=bash \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   haproxy-etcd:latest

### 任务 14. docker cp 脚本进独立 haproxy 容器并生成/启动（standalone 端口 12379）
  执行于 [p0]  bash-OK
    | docker exec haproxy-p0 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh haproxy-p0:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh haproxy-p0:/home/ma-user/haproxy/
    | docker exec haproxy-p0 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'
  执行于 [p1]  bash-OK
    | docker exec haproxy-p1 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh haproxy-p1:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh haproxy-p1:/home/ma-user/haproxy/
    | docker exec haproxy-p1 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'
  执行于 [p2]  bash-OK
    | docker exec haproxy-p2 mkdir -p /home/ma-user/haproxy
    | docker cp /tmp/gen_haproxy_etcd.sh haproxy-p2:/home/ma-user/haproxy/
    | docker cp /tmp/start-ha.sh haproxy-p2:/home/ma-user/haproxy/
    | docker exec haproxy-p2 bash -c \
    |   'export HAP_LISTEN_START_PORT=12379; \
    |    export ETCD_SERVER_LIST="116.204.91.141:2379;113.44.111.127:2379;121.37.88.17:2379"; \
    |    bash /home/ma-user/haproxy/gen_haproxy_etcd.sh && \
    |    bash /home/ma-user/haproxy/start-ha.sh'

### 任务 15. 容器内起 mooncake_master（HA：enable_ha+etcd 选主；单主：直连）
  执行于 [p0]  bash-OK
    | docker exec mooncake-p0 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p0 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p0 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379 \
    |     --rpc-address 116.204.91.141 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p0.log 2>&1'
    | sleep 2 && docker exec mooncake-p0 pgrep -x mooncake_master >/dev/null
  执行于 [p1]  bash-OK
    | docker exec mooncake-p1 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p1 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p1 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379 \
    |     --rpc-address 113.44.111.127 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p1.log 2>&1'
    | sleep 2 && docker exec mooncake-p1 pgrep -x mooncake_master >/dev/null
  执行于 [p2]  bash-OK
    | docker exec mooncake-p2 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p2 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p2 bash -c \
    |   'mooncake_master -enable_ha=true \
    |     --cluster_id=mooncake \
    |     -etcd_endpoints 116.204.91.141:12379,113.44.111.127:12379,121.37.88.17:12379 \
    |     --rpc-address 121.37.88.17 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p2.log 2>&1'
    | sleep 2 && docker exec mooncake-p2 pgrep -x mooncake_master >/dev/null

### 任务 16. 渲染 run_dp_template.sh（每节点，dest 为节点 /tmp，模板用 inventory ip 字段）
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.125.57"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.33.178"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.115.71"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.64.115"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="116.204.91.141"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="113.44.111.127"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3545 字符)
          | local_ip="121.37.88.17"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3548 字符)
          | local_ip="116.204.121.119"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'

### 任务 17. docker cp 模板进容器 /root/pd/run_dp_template.sh（对照 deploy.sh start_node）
  执行于 [d0]  bash-OK
    | docker cp /tmp/run_dp_template_d0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d1]  bash-OK
    | docker cp /tmp/run_dp_template_d1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d2]  bash-OK
    | docker cp /tmp/run_dp_template_d2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d3]  bash-OK
    | docker cp /tmp/run_dp_template_d3.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p0]  bash-OK
    | docker cp /tmp/run_dp_template_p0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p1]  bash-OK
    | docker cp /tmp/run_dp_template_p1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p2]  bash-OK
    | docker cp /tmp/run_dp_template_p2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p3]  bash-OK
    | docker cp /tmp/run_dp_template_p3.sh glm52-ansible-test:/root/pd/run_dp_template.sh

### 任务 18. 启动引擎（对照 deploy.sh start_engines，docker exec -d 后台）
  执行于 [d0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 0 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d0.log 2>&1'
  执行于 [d1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 2 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d1.log 2>&1'
  执行于 [d2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 4 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d2.log 2>&1'
  执行于 [d3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 6 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d3.log 2>&1'
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 0 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p0.log 2>&1'
  执行于 [p1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 1 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p1.log 2>&1'
  执行于 [p2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 2 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p2.log 2>&1'
  执行于 [p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 3 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p3.log 2>&1'

### 任务 19. 等待全部实例就绪（uri + until，遍历实例清单）
  [d0] (非命令任务：file/uri 等，跳过渲染)

### 任务 20. 分发 proxy 脚本到节点 /tmp（对照 deploy.sh 的 scpn）
  执行于 [p0]

### 任务 21. docker cp proxy 脚本进容器并启动（对照 deploy.sh start_proxy，pkill 锚定 ^python3）
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd 2>/dev/null || true
    | docker cp /tmp/load_balance_proxy_server_example.py glm52-ansible-test:/root/pd/
    | docker exec glm52-ansible-test chmod +x /root/pd/load_balance_proxy_server_example.py
    | docker exec glm52-ansible-test pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'unset http_proxy https_proxy; python3 /root/pd/load_balance_proxy_server_example.py \
    |   --host 0.0.0.0 --port 1999 \
    |   --prefiller-hosts 116.204.91.141 113.44.111.127 121.37.88.17 116.204.121.119 --prefiller-ports 9081 9081 9081 9081 \
    |   --decoder-hosts 116.204.125.57 116.204.125.57 116.204.33.178 116.204.33.178 116.204.115.71 116.204.115.71 116.204.64.115 116.204.64.115 --decoder-ports 9900 9901 9900 9901 9900 9901 9900 9901 >> /mnt/share_space/g00832294/deploy-glm52/logs/proxy.log 2>&1'

### 任务 22. 等待 proxy /healthcheck 就绪（uri + until，对照 deploy.sh wait_ready "proxy"）
  [p0] (非命令任务：file/uri 等，跳过渲染)

### 任务 23. 冒烟测试（control 端 POST /v1/chat/completions，对照 deploy.sh smoke_test）
  [p0] (非命令任务：file/uri 等，跳过渲染)

—— 汇总：shell 命令 64 条，语法失败 0 条 ✅

==========================================================================================
模式: kvpool_single — kvpool 单主
   enabled=True  enable_ha=False  haproxy.mode=per_container
==========================================================================================

## gen.yml
- 确保 ansible/generated/ 目录存在（generated/ 在 .gitignore，首次运行需创建）：(非命令任务，跳过渲染)
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d0]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d1]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d2]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d3]
    [run_dp_decode_template.sh.j2] 3466 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p0]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p1]
    [run_dp_prefill_template.sh.j2] 3547 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p2]
    [run_dp_prefill_template.sh.j2] 3545 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p3]
    [run_dp_prefill_template.sh.j2] 3548 字符
- [跳过] 渲染 mooncake.json（kvpool HA：master 经 etcd://haproxy 发现）
- 渲染 mooncake.json（kvpool 单主：master 直连 p0 rpc 端口，待实机确认）
    | {
    |   "metadata_server": "P2PHANDSHAKE",
    |   "protocol": "ascend",
    |   "device_name": "",
    |   "master_server_address": "116.204.91.141:52050",
    |   "global_segment_size": "80GB",
    |   "default_kv_lease_ttl": 11000
    | }

## start.yml

### 任务 1. 校验部署模式组合（ha+etcd 依赖 kvpool）
  [d0] assert ['not mooncake.ha.enable_ha or mooncake.enabled'] → 通过 ✅

### 任务 2. 删除旧容器（幂等，对照 deploy.sh 的 docker rm -f $CONTAINER_NAME）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker rm -f glm52-ansible-test 2>/dev/null || true

### 任务 3. 起容器（对照 deploy.sh start_node 的 docker run，无遗漏）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker run -itd --name glm52-ansible-test \
    | --net=host --privileged --shm-size=1024g \
    | --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
    | --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
    | -v /usr/local/dcmi:/usr/local/dcmi \
    | -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
    | -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    | -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    | -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
    | -v /etc/ascend_install.info:/etc/ascend_install.info \
    | -v /etc/hccn.conf:/etc/hccn.conf \
    | \
    | -v /mnt/share_space/:/mnt/share_space/ -v /data2:/data2 \
    | \
    | quay.io/ascend/vllm-ascend:v0.23.0 bash

### 任务 4. 校验 generated/mooncake.json 存在（generated/ 被 .gitignore 忽略，首次部署必缺）
  [localhost] stat（假设 generated 已由 gen 生成，exists=true）

### 任务 5. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../launch_online_dp.py', 'dest': '/tmp/launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../launch_online_dp.py → /tmp/launch_online_dp.py

### 任务 6. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../generated/mooncake.json', 'dest': '/tmp/mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../generated/mooncake.json → /tmp/mooncake.json

### 任务 7. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/launch_online_dp.py glm52-ansible-test:/root/pd/

### 任务 8. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'mooncake.json', 'only_kvpool': True}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/mooncake.json glm52-ansible-test:/root/pd/

### 任务 9. 起 mooncake 容器（HA：mooncake.ha.nodes；单主：proxy 节点；独立于 vllm-ascend，含 mooncake_master+haproxy+nc）
  执行于 [p0]  bash-OK
    | docker rm -f mooncake-p0 2>/dev/null || true
    | docker run -itd --name mooncake-p0 --shm-size=500g --network host \
    |   --device=/dev/davinci_manager \
    |   --device=/dev/hisi_hdc \
    |   --device=/dev/devmm_svm \
    |   --entrypoint=bash \
    |   -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    |   -v /usr/local/dcmi:/usr/local/dcmi \
    |   -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    |   -v /etc/ascend_install.info:/etc/ascend_install.info \
    |   -v /usr/local/sbin:/usr/local/sbin \
    |   -v /etc/hccn.conf:/etc/hccn.conf \
    |   -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
    |   -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    |   -v /mnt/share_space/certs:/etc/pki/etcd/certs \
    |   -v /mnt/share_space/g00832294/deploy-glm52/logs:/mnt/share_space/g00832294/deploy-glm52/logs \
    |   quay.io/ascend/vllm-ascend:v0.23.0rc1-mooncake

### 任务 10. 容器内起 mooncake_master（HA：enable_ha+etcd 选主；单主：直连）
  执行于 [p0]  bash-OK
    | docker exec mooncake-p0 mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec mooncake-p0 pkill -x mooncake_master 2>/dev/null || true
    | docker exec -d mooncake-p0 bash -c \
    |   'mooncake_master --cluster_id=mooncake \
    |     --rpc-address 116.204.91.141 \
    |     -rpc_port=52050 \
    |     -metrics_port=52052 \
    |     -eviction_high_watermark_ratio=0.9 \
    |     -eviction_ratio=0.2 \
    |     -default_kv_lease_ttl=11000 \
    |     -log_dir=/var/log/mooncake_master \
    |     -max_log_size=1800 -stderrthreshold=4 -stop_logging_if_full_disk=true \
    |     >> /mnt/share_space/g00832294/deploy-glm52/logs/mooncake_p0.log 2>&1'
    | sleep 2 && docker exec mooncake-p0 pgrep -x mooncake_master >/dev/null

### 任务 11. 渲染 run_dp_template.sh（每节点，dest 为节点 /tmp，模板用 inventory ip 字段）
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.125.57"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.33.178"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.115.71"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 3466 字符)
          | local_ip="116.204.64.115"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_consumer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_consumer",
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="116.204.91.141"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3547 字符)
          | local_ip="113.44.111.127"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3545 字符)
          | local_ip="121.37.88.17"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 3548 字符)
          | local_ip="116.204.121.119"
          | export MOONCAKE_CONFIG_PATH="/root/pd/mooncake.json"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MultiConnector",
          | "kv_role": "kv_producer",
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "kv_connector": "AscendStoreConnector",
          | "kv_role": "kv_producer",
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'

### 任务 12. docker cp 模板进容器 /root/pd/run_dp_template.sh（对照 deploy.sh start_node）
  执行于 [d0]  bash-OK
    | docker cp /tmp/run_dp_template_d0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d1]  bash-OK
    | docker cp /tmp/run_dp_template_d1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d2]  bash-OK
    | docker cp /tmp/run_dp_template_d2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d3]  bash-OK
    | docker cp /tmp/run_dp_template_d3.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p0]  bash-OK
    | docker cp /tmp/run_dp_template_p0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p1]  bash-OK
    | docker cp /tmp/run_dp_template_p1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p2]  bash-OK
    | docker cp /tmp/run_dp_template_p2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p3]  bash-OK
    | docker cp /tmp/run_dp_template_p3.sh glm52-ansible-test:/root/pd/run_dp_template.sh

### 任务 13. 启动引擎（对照 deploy.sh start_engines，docker exec -d 后台）
  执行于 [d0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 0 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d0.log 2>&1'
  执行于 [d1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 2 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d1.log 2>&1'
  执行于 [d2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 4 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d2.log 2>&1'
  执行于 [d3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 6 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d3.log 2>&1'
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 0 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p0.log 2>&1'
  执行于 [p1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 1 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p1.log 2>&1'
  执行于 [p2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 2 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p2.log 2>&1'
  执行于 [p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 3 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p3.log 2>&1'

### 任务 14. 等待全部实例就绪（uri + until，遍历实例清单）
  [d0] (非命令任务：file/uri 等，跳过渲染)

### 任务 15. 分发 proxy 脚本到节点 /tmp（对照 deploy.sh 的 scpn）
  执行于 [p0]

### 任务 16. docker cp proxy 脚本进容器并启动（对照 deploy.sh start_proxy，pkill 锚定 ^python3）
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd 2>/dev/null || true
    | docker cp /tmp/load_balance_proxy_server_example.py glm52-ansible-test:/root/pd/
    | docker exec glm52-ansible-test chmod +x /root/pd/load_balance_proxy_server_example.py
    | docker exec glm52-ansible-test pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'unset http_proxy https_proxy; python3 /root/pd/load_balance_proxy_server_example.py \
    |   --host 0.0.0.0 --port 1999 \
    |   --prefiller-hosts 116.204.91.141 113.44.111.127 121.37.88.17 116.204.121.119 --prefiller-ports 9081 9081 9081 9081 \
    |   --decoder-hosts 116.204.125.57 116.204.125.57 116.204.33.178 116.204.33.178 116.204.115.71 116.204.115.71 116.204.64.115 116.204.64.115 --decoder-ports 9900 9901 9900 9901 9900 9901 9900 9901 >> /mnt/share_space/g00832294/deploy-glm52/logs/proxy.log 2>&1'

### 任务 17. 等待 proxy /healthcheck 就绪（uri + until，对照 deploy.sh wait_ready "proxy"）
  [p0] (非命令任务：file/uri 等，跳过渲染)

### 任务 18. 冒烟测试（control 端 POST /v1/chat/completions，对照 deploy.sh smoke_test）
  [p0] (非命令任务：file/uri 等，跳过渲染)

—— 汇总：shell 命令 51 条，语法失败 0 条 ✅

==========================================================================================
模式: no_kvpool — 无 kvpool
   enabled=False  enable_ha=False  haproxy.mode=per_container
==========================================================================================

## gen.yml
- 确保 ansible/generated/ 目录存在（generated/ 在 .gitignore，首次运行需创建）：(非命令任务，跳过渲染)
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d0]
    [run_dp_decode_template.sh.j2] 2855 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d1]
    [run_dp_decode_template.sh.j2] 2855 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d2]
    [run_dp_decode_template.sh.j2] 2855 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [d3]
    [run_dp_decode_template.sh.j2] 2855 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p0]
    [run_dp_prefill_template.sh.j2] 2976 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p1]
    [run_dp_prefill_template.sh.j2] 2976 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p2]
    [run_dp_prefill_template.sh.j2] 2974 字符
- 渲染各节点 run_dp_template.sh 到 ansible/generated/（对照 deploy.sh cmd_gen 的 render_template 循环） [p3]
    [run_dp_prefill_template.sh.j2] 2977 字符
- [跳过] 渲染 mooncake.json（kvpool HA：master 经 etcd://haproxy 发现）
- [跳过] 渲染 mooncake.json（kvpool 单主：master 直连 p0 rpc 端口，待实机确认）

## start.yml

### 任务 1. 校验部署模式组合（ha+etcd 依赖 kvpool）
  [d0] assert ['not mooncake.ha.enable_ha or mooncake.enabled'] → 通过 ✅

### 任务 2. 删除旧容器（幂等，对照 deploy.sh 的 docker rm -f $CONTAINER_NAME）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker rm -f glm52-ansible-test 2>/dev/null || true

### 任务 3. 起容器（对照 deploy.sh start_node 的 docker run，无遗漏）
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker run -itd --name glm52-ansible-test \
    | --net=host --privileged --shm-size=1024g \
    | --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
    | --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
    | -v /usr/local/dcmi:/usr/local/dcmi \
    | -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
    | -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    | -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
    | -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
    | -v /etc/ascend_install.info:/etc/ascend_install.info \
    | -v /etc/hccn.conf:/etc/hccn.conf \
    | \
    | -v /mnt/share_space/:/mnt/share_space/ -v /data2:/data2 \
    | \
    | quay.io/ascend/vllm-ascend:v0.23.0 bash

### 任务 4. 分发启动文件到各节点 /tmp（对照 deploy.sh 的 scpn；mooncake.json 仅 kvpool） [{'src': '../launch_online_dp.py', 'dest': '/tmp/launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]
    | copy ../launch_online_dp.py → /tmp/launch_online_dp.py

### 任务 5. docker cp 启动文件进容器 /root/pd/（对照 deploy.sh start_node；mooncake.json 仅 kvpool） [{'name': 'launch_online_dp.py', 'only_kvpool': False}]
  执行于 [d0, d1, d2, d3, p0, p1, p2, p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd && docker cp /tmp/launch_online_dp.py glm52-ansible-test:/root/pd/

### 任务 6. 渲染 run_dp_template.sh（每节点，dest 为节点 /tmp，模板用 inventory ip 字段）
  ([run_dp_decode_template.sh.j2] 2855 字符)
          | local_ip="116.204.125.57"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "engine_id": "2",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 2855 字符)
          | local_ip="116.204.33.178"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "engine_id": "2",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 2855 字符)
          | local_ip="116.204.115.71"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "engine_id": "2",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_decode_template.sh.j2] 2855 字符)
          | local_ip="116.204.64.115"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 128 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_consumer",
          | "kv_port": "30100",
          | "engine_id": "2",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 3, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 2976 字符)
          | local_ip="116.204.91.141"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "engine_id": "0",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 2976 字符)
          | local_ip="113.44.111.127"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "engine_id": "0",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 2974 字符)
          | local_ip="121.37.88.17"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "engine_id": "0",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'
  ([run_dp_prefill_template.sh.j2] 2977 字符)
          | local_ip="116.204.121.119"
          | vllm serve /mnt/share_space/models/GLM-5.2-w4a8c8 \
          | --data-parallel-address $5 \
          | --max-num-seqs 256 \
          | --kv-transfer-config \
          | "kv_connector": "MooncakeConnectorV1",
          | "kv_role": "kv_producer",
          | "kv_port": "30000",
          | "engine_id": "0",
          | "use_ascend_direct": true,
          | --speculative-config '{"num_speculative_tokens": 1, "method":"deepseek_mtp", "enforce_eager":true}'

### 任务 7. docker cp 模板进容器 /root/pd/run_dp_template.sh（对照 deploy.sh start_node）
  执行于 [d0]  bash-OK
    | docker cp /tmp/run_dp_template_d0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d1]  bash-OK
    | docker cp /tmp/run_dp_template_d1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d2]  bash-OK
    | docker cp /tmp/run_dp_template_d2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [d3]  bash-OK
    | docker cp /tmp/run_dp_template_d3.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p0]  bash-OK
    | docker cp /tmp/run_dp_template_p0.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p1]  bash-OK
    | docker cp /tmp/run_dp_template_p1.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p2]  bash-OK
    | docker cp /tmp/run_dp_template_p2.sh glm52-ansible-test:/root/pd/run_dp_template.sh
  执行于 [p3]  bash-OK
    | docker cp /tmp/run_dp_template_p3.sh glm52-ansible-test:/root/pd/run_dp_template.sh

### 任务 8. 启动引擎（对照 deploy.sh start_engines，docker exec -d 后台）
  执行于 [d0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 0 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d0.log 2>&1'
  执行于 [d1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 2 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d1.log 2>&1'
  执行于 [d2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 4 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d2.log 2>&1'
  执行于 [d3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 8 --tp-size 4 --dp-size-local 2 \
    |   --dp-rank-start 6 --dp-address 116.204.125.57 \
    |   --dp-rpc-port 16600 --vllm-start-port 9900 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_d3.log 2>&1'
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 0 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p0.log 2>&1'
  执行于 [p1]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 1 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p1.log 2>&1'
  执行于 [p2]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 2 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p2.log 2>&1'
  执行于 [p3]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /mnt/share_space/g00832294/deploy-glm52/logs 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'cd /root/pd && python3 launch_online_dp.py \
    |   --dp-size 4 --tp-size 8 --dp-size-local 1 \
    |   --dp-rank-start 3 --dp-address 116.204.91.141 \
    |   --dp-rpc-port 16591 --vllm-start-port 9081 >> /mnt/share_space/g00832294/deploy-glm52/logs/vllm_p3.log 2>&1'

### 任务 9. 等待全部实例就绪（uri + until，遍历实例清单）
  [d0] (非命令任务：file/uri 等，跳过渲染)

### 任务 10. 分发 proxy 脚本到节点 /tmp（对照 deploy.sh 的 scpn）
  执行于 [p0]

### 任务 11. docker cp proxy 脚本进容器并启动（对照 deploy.sh start_proxy，pkill 锚定 ^python3）
  执行于 [p0]  bash-OK
    | docker exec glm52-ansible-test mkdir -p /root/pd 2>/dev/null || true
    | docker cp /tmp/load_balance_proxy_server_example.py glm52-ansible-test:/root/pd/
    | docker exec glm52-ansible-test chmod +x /root/pd/load_balance_proxy_server_example.py
    | docker exec glm52-ansible-test pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true
    | docker exec -d glm52-ansible-test bash -c 'unset http_proxy https_proxy; python3 /root/pd/load_balance_proxy_server_example.py \
    |   --host 0.0.0.0 --port 1999 \
    |   --prefiller-hosts 116.204.91.141 113.44.111.127 121.37.88.17 116.204.121.119 --prefiller-ports 9081 9081 9081 9081 \
    |   --decoder-hosts 116.204.125.57 116.204.125.57 116.204.33.178 116.204.33.178 116.204.115.71 116.204.115.71 116.204.64.115 116.204.64.115 --decoder-ports 9900 9901 9900 9901 9900 9901 9900 9901 >> /mnt/share_space/g00832294/deploy-glm52/logs/proxy.log 2>&1'

### 任务 12. 等待 proxy /healthcheck 就绪（uri + until，对照 deploy.sh wait_ready "proxy"）
  [p0] (非命令任务：file/uri 等，跳过渲染)

### 任务 13. 冒烟测试（control 端 POST /v1/chat/completions，对照 deploy.sh smoke_test）
  [p0] (非命令任务：file/uri 等，跳过渲染)

—— 汇总：shell 命令 41 条，语法失败 0 条 ✅
