#!/usr/bin/env bash
# =============================================================================
# deploy.sh — GLM-5.2 P/D 分离一键部署 (deploy-glm52)
# 配置源: config.yaml (默认) 或 CLUSTER_CONFIG=config-a3.yaml
# 以跑通的 a2.md 为蓝本: MultiConnector + AscendStore KV pool (mooncake_master)
#
# 命令:
#   check    预检: config 可解析 + SSH 连通 + 远端 docker
#   pull     并行到所有节点拉取镜像
#   start    起容器 + mooncake_master + 全部引擎 + 全端口就绪 + proxy + 冒烟
#   stop     停 proxy/mooncake_master, 删除所有节点容器
#   status   查看各节点容器状态与 API 健康
#   logs <node|mooncake>  查看节点 vllm / mooncake 日志
#   gen      只渲染各节点模板与 mooncake.json 到 generated/ (调试用)
# 全局选项: --dry-run  打印将执行的命令而不实际执行
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${CLUSTER_CONFIG:-$SCRIPT_DIR/config.yaml}"
GEN_DIR="$SCRIPT_DIR/generated"
LAUNCH_PY="$SCRIPT_DIR/launch_online_dp.py"
RESOLVE="$SCRIPT_DIR/resolve"
DRY_RUN=0

log()  { echo -e "\033[1;34m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

run() {
    if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] $*"; else "$@"; fi
}

sshn() { local node="$1" cmd="$2"; run ssh $SSH_OPTS "${SSH_USER}@${IP[$node]}" "$cmd"; }
scpn() { local node="$1" file="$2"; run scp $SSH_OPTS "$file" "${SSH_USER}@${IP[$node]}:/tmp/$(basename "$file")"; }

# ---------- 参数解析 ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        *) break ;;
    esac
done
CMD="${1:-}"; shift || true

# ---------- 加载配置 ----------
[[ -f "$CONFIG" ]] || die "配置文件不存在: $CONFIG (用 CLUSTER_CONFIG=config-a3.yaml 切换)"
# TODO: 解析器需要在控制机本地跑 pyyaml, 但本机系统 python3(3.9) 无 pyyaml, 仅 .venv 内 python 有。
#   当前依赖执行前手动 `source .venv/bin/activate` (README "前置条件" 已注明)。
#   更稳的做法: 顶部自动检测 .venv/bin/python 存在则用之, 否则回退 python3, 消除对 source 的依赖。
python3 -c "import yaml" 2>/dev/null || die "远程/控制机缺少 pyyaml, 请 pip install pyyaml"

# 推导节点表 (p0..pN, d0..dN) 与各自 IP/NIC
NODES=()
declare -A IP NIC ROLE IDX
_parse_nodes() {
    local role_prefix="$1" role="$2" i
    # 节点数从 config 的 nodes 派生
    local n
    n=$(python3 -c "
import yaml,sys
cfg=yaml.safe_load(open('$CONFIG'))
print(len(cfg['nodes']['$role']))")
    for i in $(seq 0 $((n-1))); do
        local node="${role_prefix}${i}" out
        NODES+=("$node")
        out=$(python3 "$RESOLVE/resolve_node.py" --config "$CONFIG" --node "$node") || die "解析节点 $node 失败"
        [[ -n "$out" ]] || die "解析节点 $node 失败"
        eval "$out"
        IP[$node]="$LOCAL_IP"; NIC[$node]="$NIC"; ROLE[$node]="$ROLE"; IDX[$node]="$NODE_INDEX"
    done
}
_parse_nodes p prefill
_parse_nodes d decode

require_vars() {
    local missing=() i
    for v in SSH_USER IMAGE MODEL_PATH CONTAINER_NAME; do
        [[ -n "${!v:-}" ]] || missing+=("$v")
    done
    [[ ${#NODES[@]} -gt 0 ]] || missing+=(nodes)
    [[ ${#missing[@]} -eq 0 ]] || die "$(basename "$CONFIG") 中以下配置缺失: ${missing[*]}"
}

# ---------- 渲染模板 ----------
render_template() {
    local node="$1"
    local out="$GEN_DIR/run_dp_template_${node}.sh"
    mkdir -p "$GEN_DIR"
    eval "$(python3 "$RESOLVE/resolve_node.py" --config "$CONFIG" --node "$node")"
    sed -e "s|__NIC__|${NIC[$node]}|g" \
        -e "s|__LOCAL_IP__|${IP[$node]}|g" \
        -e "s|__MODEL_PATH__|$MODEL_PATH|g" \
        -e "s|__MODEL_NAME__|$SERVED_MODEL_NAME|g" \
        -e "s|__MOONCAKE_CONFIG_PATH__|/root/pd/mooncake.json|g" \
        -e "s|__CLUSTER_TYPE__|$CLUSTER_TYPE|g" \
        -e "s|__MAX_MODEL_LEN__|$MAX_MODEL_LEN|g" \
        -e "s|__KV_PORT__|$KV_PORT|g" \
        -e "s|__PREFILL_DP__|$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['pd_cluster']['prefill']['dp_size'])")|g" \
        -e "s|__PREFILL_TP__|$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['pd_cluster']['prefill']['tp_size'])")|g" \
        -e "s|__DECODE_DP__|$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['pd_cluster']['decode']['dp_size'])")|g" \
        -e "s|__DECODE_TP__|$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['pd_cluster']['decode']['tp_size'])")|g" \
        "$SCRIPT_DIR/templates/run_dp_${ROLE[$node]}_template.sh" > "$out"
    log "已渲染 $out"
}

render_mooncake_json() {
    mkdir -p "$GEN_DIR"
    python3 "$RESOLVE/render_mooncake.py" --config "$CONFIG" --output "$GEN_DIR/mooncake.json"
    log "已渲染 $GEN_DIR/mooncake.json"
}

# ---------- 命令 ----------
cmd_check() {
    require_vars
    log "配置检查通过"
    [[ -f "$LAUNCH_PY" ]] || die "launch_online_dp.py 缺失: $LAUNCH_PY"
    local n fails=0
    for n in "${NODES[@]}"; do
        if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] ssh ${SSH_USER}@${IP[$n]} docker version"; continue; fi
        if sshn "$n" "docker version >/dev/null 2>&1 && echo ok" | grep -q ok; then
            log "$n (${ROLE[$n]}, ${IP[$n]}) ssh + docker 正常"
        else
            warn "$n (${ROLE[$n]}, ${IP[$n]}) ssh 或 docker 异常"; fails=$((fails+1))
        fi
    done
    [[ $fails -eq 0 ]] || die "$fails 个节点预检失败"
    log "全部 ${#NODES[@]} 个节点预检通过"
}

cmd_pull() {
    require_vars
    local n pids=()
    for n in "${NODES[@]}"; do log "$n (${IP[$n]}) 拉取 $IMAGE"; sshn "$n" "docker pull $IMAGE" & pids+=($!); done
    local rc=0 p
    for p in "${pids[@]}"; do wait "$p" || rc=1; done
    [[ $rc -eq 0 ]] || die "部分节点镜像拉取失败"
    log "镜像拉取完成"
}

wait_ready() {
    local name="$1" url="$2" timeout="${3:-$READY_TIMEOUT}" elapsed=0
    log "等待 $name 就绪 ($url, 超时 ${timeout}s)"
    if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] poll $url"; return 0; fi
    while true; do
        if curl -sf -o /dev/null --max-time 5 "$url"; then log "$name 已就绪 (${elapsed}s)"; return 0; fi
        [[ $elapsed -ge $timeout ]] && return 1
        sleep 15; elapsed=$((elapsed+15))
    done
}

start_node() {
    local node="$1"
    local devices="" i
    for i in $(seq 0 $((NUM_CARDS-1))); do devices+="--device /dev/davinci$i "; done
    local model_mount=""
    [[ -n "${MODEL_DIR_HOST:-}" ]] && model_mount="-v $MODEL_DIR_HOST:/root/.cache"
    local extra_mounts=""
    while read -r m; do [[ -n "$m" ]] && extra_mounts+=" -v $m"; done < <(python3 -c "import yaml;print('\n'.join(yaml.safe_load(open('$CONFIG'))['model'].get('mounts', [])))")

    log "启动节点 $node (${ROLE[$node]}, ${IP[$node]})"
    sshn "$node" "docker rm -f $CONTAINER_NAME 2>/dev/null || true"
    sshn "$node" "docker run -itd --name $CONTAINER_NAME \
        --net=host --pid=host --privileged --shm-size=$SHM_SIZE \
        $devices \
        --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
        -v /usr/local/dcmi:/usr/local/dcmi \
        -v /usr/local/Ascend/driver/tools/hccn_tool:/usr/local/Ascend/driver/tools/hccn_tool \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
        -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
        -v /etc/ascend_install.info:/etc/ascend_install.info \
        -v /etc/hccn.conf:/etc/hccn.conf \
        $extra_mounts \
        $model_mount \
        $IMAGE bash"
    scpn "$node" "$LAUNCH_PY"
    scpn "$node" "$GEN_DIR/run_dp_template_${node}.sh"
    scpn "$node" "$GEN_DIR/mooncake.json"
    sshn "$node" "docker exec $CONTAINER_NAME mkdir -p /root/pd && \
        docker cp /tmp/launch_online_dp.py $CONTAINER_NAME:/root/pd/ && \
        docker cp /tmp/run_dp_template_${node}.sh $CONTAINER_NAME:/root/pd/run_dp_template.sh && \
        docker cp /tmp/mooncake.json $CONTAINER_NAME:/root/pd/mooncake.json"
}

start_mooncake_master() {
    log "在 p0 (${IP[p0]}) 容器内拉起 mooncake_master (:$MOONCAKE_PORT)"
    # 注意容器 --pid=host: pkill/pgrep 会看到 host 全部进程, 必须用 -x 精确匹配进程名,
    # 否则 -f 会匹配控制机 ssh 会话自身命令行而误杀/误判
    sshn p0 "docker exec $CONTAINER_NAME pkill -x mooncake_master 2>/dev/null || true; \
        docker exec -d $CONTAINER_NAME bash -c \
        'mooncake_master -port $MOONCAKE_PORT -eviction_high_watermark_ratio $MOONCAKE_EVICT >> $MOONCAKE_LOG 2>&1' && \
        sleep 2 && docker exec $CONTAINER_NAME pgrep -x mooncake_master >/dev/null && echo mooncake_master alive"
}

start_engines() {
    local node="$1"
    eval "$(python3 "$RESOLVE/resolve_node.py" --config "$CONFIG" --node "$node")"
    sshn "$node" "docker exec -d $CONTAINER_NAME bash -c \
        'cd /root/pd && python3 launch_online_dp.py \
        --dp-size $DP_SIZE --tp-size $TP_SIZE --dp-size-local $DP_SIZE_LOCAL \
        --dp-rank-start $DP_RANK_START --dp-address $DP_ADDRESS \
        --dp-rpc-port $DP_RPC_PORT --vllm-start-port $VLLM_START_PORT >> $VLLM_LOG 2>&1'"
    log "$node vllm 已后台拉起"
}

cmd_start() {
    require_vars
    local n
    for n in "${NODES[@]}"; do render_template "$n"; done
    render_mooncake_json
    for n in "${NODES[@]}"; do start_node "$n"; done
    start_mooncake_master
    for n in "${NODES[@]}"; do start_engines "$n"; done

    # 全部引擎逐一就绪等待（resolve_instances 返回全部实例）
    while read -r role ip port; do
        [[ -n "$role" ]] || continue
        [[ "$role" == proxy ]] && continue
        wait_ready "$role:$ip:$port" "http://$ip:$port/health" || die "实例 $role:$ip:$port 就绪超时"
    done < <(python3 "$RESOLVE/resolve_instances.py" --config "$CONFIG")

    start_proxy
    wait_ready "proxy" "http://${IP[$PROXY_NODE]:-}:$PROXY_PORT/healthcheck" "$PROXY_READY_TIMEOUT" || die "proxy /healthcheck 就绪超时"
    smoke_test
    log "部署完成, 统一入口: http://${IP[$PROXY_NODE]:-}:$PROXY_PORT/v1 (model: $MODEL_NAME)"
}

proxy_args() {
    eval "$(python3 "$RESOLVE/resolve_router.py" --config "$CONFIG")"
    echo "--host 0.0.0.0 --port $PROXY_PORT \
        --prefiller-hosts $PREFILLER_HOSTS --prefiller-ports $PREFILLER_PORTS \
        --decoder-hosts $DECODER_HOSTS --decoder-ports $DECODER_PORTS"
}

start_proxy() {
    # 解析 proxy 节点名与端口
    local pnode pnode_ip pport
    pnode=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy']['node'])")
    pport=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy']['port'])")
    PROXY_PORT=$pport
    pnode_ip="${IP[$pnode]:-}"
    [[ -n "$pnode_ip" ]] || die "PROXY_NODE=$pnode 无效, 可选: ${NODES[*]}"
    PROXY_NODE=$pnode
    local args; args="$(proxy_args)"

    # proxy 脚本: 优先 config.proxy.script_path(容器内路径); 否则把项目自带脚本分发进容器 /root/pd/
    local script_path
    script_path=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy'].get('script_path','') or '')")
    if [[ -z "$script_path" ]]; then
        scpn "$pnode" "$SCRIPT_DIR/load_balance_proxy_server_example.py"
        sshn "$pnode" "docker exec $CONTAINER_NAME mkdir -p /root/pd && \
            docker cp /tmp/load_balance_proxy_server_example.py $CONTAINER_NAME:/root/pd/ && \
            docker exec $CONTAINER_NAME chmod +x /root/pd/load_balance_proxy_server_example.py"
        script_path="/root/pd/load_balance_proxy_server_example.py"
    fi
    log "在节点 $pnode 拉起 proxy: $script_path"
    # 注意容器 --pid=host: pkill 会看到 host 全部进程, proxy 进程 cmdline 以 python3 开头,
    # 锚定 ^python3 避免误杀控制机 ssh 会话(其 cmdline 以 ssh 开头)
    sshn "$pnode" "docker exec $CONTAINER_NAME pkill -f '^python3 .*load_balance_proxy_server_example' 2>/dev/null || true; \
        docker exec -d $CONTAINER_NAME bash -c \
        'unset http_proxy https_proxy; python3 $script_path $args >> $PROXY_LOG 2>&1' && echo proxy started"
}

smoke_test() {
    local url="http://${IP[$PROXY_NODE]}:$PROXY_PORT"
    log "冒烟测试: POST $url/v1/completions"
    if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] curl $url/v1/completions"; return 0; fi
    if resp=$(curl -sf --max-time 120 "$url/v1/completions" -H "Content-Type: application/json" \
        -d "{\"model\": \"$MODEL_NAME\", \"prompt\": \"The future of AI is\", \"max_completion_tokens\": 20, \"temperature\": 0}"); then
        log "冒烟测试通过: $(echo "$resp" | head -c 200)"
    else
        die "冒烟测试失败, 请查看 proxy.log 与各节点日志"
    fi
}

cmd_stop() {
    local pnode
    pnode=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy']['node'])")
    # --pid=host: pkill 用精确匹配(mooncake -x / proxy 锚定 ^python3), 避免误杀控制机 ssh 会话
    if [[ -n "${IP[$pnode]:-}" ]]; then sshn "$pnode" "docker exec $CONTAINER_NAME pkill -f '^python3 .*load_balance_proxy_server_example' || true"; fi
    if [[ -n "${IP[p0]:-}" ]]; then sshn p0 "docker exec $CONTAINER_NAME pkill -x mooncake_master || true"; fi
    local n
    for n in "${NODES[@]}"; do log "停止 $n (${IP[$n]}) 容器"; sshn "$n" "docker rm -f $CONTAINER_NAME" || true; done
    log "已全部停止"
}

cmd_status() {
    local n
    for n in "${NODES[@]}"; do
        local st
        if [[ $DRY_RUN -eq 1 ]]; then echo "[dry-run] ssh ${IP[$n]} docker inspect $CONTAINER_NAME"; continue; fi
        st=$(sshn "$n" "docker inspect -f '{{.State.Status}}' $CONTAINER_NAME 2>/dev/null" || echo "no-container")
        log "$n (${ROLE[$n]}, ${IP[$n]}): 容器=${st:-no-container}"
    done
    if [[ $DRY_RUN -eq 0 ]]; then
        while read -r role ip port; do
            [[ -n "$role" ]] || continue
            if [[ "$role" == proxy ]]; then
                curl -sf -o /dev/null --max-time 5 "http://$ip:$port/healthcheck" && log "proxy :$port healthy" || warn "proxy :$port 未就绪"
            else
                curl -sf -o /dev/null --max-time 5 "http://$ip:$port/health" && log "$role:$ip:$port healthy" || warn "$role:$ip:$port 未就绪"
            fi
        done < <(python3 "$RESOLVE/resolve_instances.py" --config "$CONFIG")
        sshn p0 "docker exec $CONTAINER_NAME pgrep -f mooncake_master >/dev/null && echo mooncake_master alive" || warn "mooncake_master 未运行"
    fi
}

cmd_logs() {
    local node="${1:-}"
    [[ -n "$node" ]] || die "用法: ./deploy.sh logs <节点|mooncake|proxy>"
    if [[ "$node" == "mooncake" ]]; then sshn p0 "docker exec $CONTAINER_NAME tail -n ${TAIL:-100} $MOONCAKE_LOG"; return; fi
    if [[ "$node" == "proxy" ]]; then
        local pn; pn=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy']['node'])")
        sshn "$pn" "docker exec $CONTAINER_NAME tail -n ${TAIL:-100} $PROXY_LOG"; return
    fi
    [[ -n "${IP[$node]:-}" ]] || die "未知节点: $node (可选: ${NODES[*]})"
    sshn "$node" "docker exec $CONTAINER_NAME tail -n ${TAIL:-100} $VLLM_LOG"
}

cmd_gen() {
    require_vars
    local n
    for n in "${NODES[@]}"; do render_template "$n"; done
    render_mooncake_json
}

# ---------- 加载集群级变量（供 require_vars / 渲染）----------
eval "$(python3 "$RESOLVE/resolve_node.py" --config "$CONFIG" --node p0)"
# ssh 缺省 (fixture/最小配置可能无 ssh 段); 实机配置在 config.ssh 提供
SSH_USER=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('ssh',{}).get('user','root'))")
SSH_OPTS=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('ssh',{}).get('opts',''))")
IMAGE=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['image'])")
MODEL_PATH=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['model']['path'])")
MODEL_NAME=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['model'].get('served_model_name','glm-52'))")
SERVED_MODEL_NAME=$MODEL_NAME
MODEL_DIR_HOST=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['model'].get('dir_host','') or '')")
CONTAINER_NAME=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['container']['name'])")
SHM_SIZE=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['container'].get('shm_size','1024g'))")
READY_TIMEOUT=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['runtime'].get('ready_timeout',2400))")
MOONCAKE_PORT=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['mooncake']['port'])")
MOONCAKE_EVICT=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['mooncake'].get('evict',0.9))")
NUM_CARDS=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['pd_cluster'].get('num_cards',8))")
CLUSTER_TYPE=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['cluster']['name'])")
# 日志路径与 proxy 超时（config logs 段 / runtime.proxy_ready_timeout，缺省按 deploy.sh 原默认）
MOONCAKE_LOG=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('logs',{}).get('mooncake','/root/mooncake.log'))")
VLLM_LOG=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('logs',{}).get('vllm','/root/vllm.log'))")
PROXY_LOG=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('logs',{}).get('proxy','/root/proxy.log'))")
PROXY_READY_TIMEOUT=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['runtime'].get('proxy_ready_timeout',600))")

case "$CMD" in
    check)  cmd_check ;;
    pull)   cmd_pull ;;
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    logs)   cmd_logs "$@" ;;
    gen)    cmd_gen ;;
    *)      grep '^#   ' "$0" | sed 's/^#   //'; exit 1 ;;
esac