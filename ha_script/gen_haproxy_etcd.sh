#!/bin/bash
set -euo pipefail

# ====== 固定路径（用户不可配置） ======
HAP_CFG="/usr/local/etc/haproxy/haproxy.cfg"

# ====== 读取环境变量 ======
HAP_LISTEN_START_PORT="${HAP_LISTEN_START_PORT:-}"
ETCD_SERVER_LIST="${ETCD_SERVER_LIST:-}"

# ====== 校验函数 ======
check_empty() {
    if [[ -z "${2}" ]]; then
        echo -e "\033[31m[ERROR] 环境变量 $1 未配置或为空！\033[0m" >&2
        exit 1
    fi
}
check_empty "HAP_LISTEN_START_PORT" "${HAP_LISTEN_START_PORT}"
check_empty "ETCD_SERVER_LIST" "${ETCD_SERVER_LIST}"

# ====== 校验端口 ======
if ! [[ "${HAP_LISTEN_START_PORT}" =~ ^[0-9]+$ ]] || [[ ${HAP_LISTEN_START_PORT} -lt 1 || ${HAP_LISTEN_START_PORT} -gt 65535 ]]; then
    echo -e "\033[31m[ERROR] 端口无效或超出范围 (1-65535)：${HAP_LISTEN_START_PORT}\033[0m" >&2
    exit 1
fi

# ====== 解析并校验节点列表 ======
IFS=';' read -ra NODES <<< "${ETCD_SERVER_LIST}"
[[ ${#NODES[@]} -eq 0 ]] && { echo -e "\033[31m[ERROR] 节点列表为空\033[0m" >&2; exit 1; }

for node in "${NODES[@]}"; do
    node="$(echo "$node" | xargs)"
    [[ -z "$node" ]] && continue
    if [[ "${node}" != *:* ]]; then
        echo -e "\033[31m[ERROR] 节点格式错误 (需 IP:PORT)：${node}\033[0m" >&2
        exit 1
    fi
    port="${node##*:}"
    if ! [[ "${port}" =~ ^[0-9]+$ ]] || [[ ${port} -lt 1 || ${port} -gt 65535 ]]; then
        echo -e "\033[31m[ERROR] 节点端口无效：${node}\033[0m" >&2
        exit 1
    fi
done

# ====== 创建输出目录 ======
mkdir -p "$(dirname "${HAP_CFG}")"

# ====== 生成配置（覆盖写入） ======
cat > "${HAP_CFG}" <<'EOF'
global
    log         127.0.0.1 local0
    maxconn     4096
    daemon

# ==================== 默认配置 ====================
defaults
    log                     global
    mode                    tcp
    option                  tcplog
    timeout connect         10s
    timeout client          30s
    timeout server          30s
    timeout check           10s
    timeout tunnel          1h

# ==================== 前端：接收客户端请求 ====================
frontend etcd_front
    bind *:PORT_PLACEHOLDER
    mode tcp
    option tcplog
    default_backend etcd_backend

# ==================== 后端：etcd节点 ====================
backend etcd_backend
    mode tcp
    balance roundrobin
EOF

# 替换监听端口
sed -i "s/PORT_PLACEHOLDER/${HAP_LISTEN_START_PORT}/" "${HAP_CFG}"

# 追加每个 server 行
seq=1
for node in "${NODES[@]}"; do
    node="$(echo "$node" | xargs)"
    [[ -z "$node" ]] && continue
    ip="${node%%:*}"
    port="${node##*:}"
    echo "    # etcd 节点 ${seq}" >> "${HAP_CFG}"
    echo "    server etcd-${seq} ${ip}:${port} ssl verify none crt /etc/pki/etcd/certs/client.pem sni str(${ip}) alpn h2 check" >> "${HAP_CFG}"
    ((seq++))
done

# ====== 成功提示 ======
echo -e "\033[32m=============================================\033[0m"
echo -e "\033[32m✅ HAProxy ETCD 代理配置生成完成\033[0m"
echo -e "配置文件路径：\033[1m${HAP_CFG}\033[0m"
echo -e "监听端口：${HAP_LISTEN_START_PORT}"
echo -e "后端节点数：$((seq - 1))"
echo -e "\033[32m=============================================\033[0m"

