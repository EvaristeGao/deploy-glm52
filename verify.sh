#!/bin/bash
# =============================================================================
# verify.sh — 验证 P/D 实例与 proxy 是否就绪（借鉴合作方）
# 用法: bash verify.sh [--wait] [--timeout 600] [--interval 5] [--no-router]
# 探测全部 vLLM 引擎 /health + proxy /healthcheck
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${CLUSTER_CONFIG:-$SCRIPT_DIR/config.yaml}"
RES=python3

WAIT=0; TIMEOUT=1800; INTERVAL=5; NO_ROUTER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --wait) WAIT=1; shift ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --no-router) NO_ROUTER=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

probe() {
    local role="$1" ip="$2" port="$3" path="/health"
    [ "$role" = "proxy" ] && path="/healthcheck"
    local resp
    resp=$($RES -c "import urllib.request, urllib.error, sys
try:
    r=urllib.request.urlopen('http://$ip:$port$path',timeout=5)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print('000')")
    if [ "$resp" = "200" ]; then printf "  ✓ %-7s %s:%s  READY\n" "$role" "$ip" "$port"; return 0
    elif [ "$resp" = "503" ]; then printf "  ✗ %-7s %s:%s  UNREADY (503)\n" "$role" "$ip" "$port"; return 1
    elif [ "$resp" = "000" ]; then printf "  ✗ %-7s %s:%s  DOWN\n" "$role" "$ip" "$port"; return 1
    else printf "  ✗ %-7s %s:%s  FAIL (%s)\n" "$role" "$ip" "$port" "$resp"; return 1; fi
}

run_once() {
    local rows total=0 ready=0
    rows=$("$RES" "$SCRIPT_DIR/resolve/resolve_instances.py" --config "$CONFIG")
    [ -n "$rows" ] || { echo "[verify] 解析 config 失败" >&2; return 1; }
    echo "[verify] 探测实例状态..."
    while IFS=' ' read -r role ip port; do
        [ -z "$role" ] && continue
        [ "$role" = "proxy" ] && [ "$NO_ROUTER" = "1" ] && continue
        total=$((total+1))
        if probe "$role" "$ip" "$port"; then ready=$((ready+1)); fi
    done <<< "$rows"
    echo "[verify] 汇总: ${ready}/${total} 就绪"
    [ "$ready" -eq "$total" ]
}

if [ "$WAIT" = "1" ]; then
    echo "[verify] 等待模式: 每 ${INTERVAL}s, 超时 ${TIMEOUT}s"
    elapsed=0
    while [ "$elapsed" -lt "$TIMEOUT" ]; do
        echo "-------- 第 $((elapsed/INTERVAL+1)) 次 (已等 ${elapsed}s) --------"
        if run_once; then echo "[verify] ✅ 全部就绪!"; exit 0; fi
        sleep "$INTERVAL"; elapsed=$((elapsed+INTERVAL))
    done
    echo "[verify] ❌ 超时 ${TIMEOUT}s"; exit 1
else
    run_once; exit $?
fi
