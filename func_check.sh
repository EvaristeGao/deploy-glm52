#!/bin/bash
# =============================================================================
# func_check.sh — curl 触发的简单 vLLM 功能验证（替代 sglang bench，镜像无该依赖）
# 用法: ./func_check.sh [--model glm-52] [--prompt "The future of AI is"] \
#                       [--max-tokens 50] [--count 3] [--wait]
# 向统一入口 POST /v1/completions，校验 200 + 含 choices，打印延迟。
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${CLUSTER_CONFIG:-$SCRIPT_DIR/config.yaml}"

MODEL=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['model'].get('served_model_name','glm-52'))")
HOST=$(python3 -c "import yaml;c=yaml.safe_load(open('$CONFIG'));n=c['proxy']['node'];r=c['nodes']['prefill'] if n[0]=='p' else c['nodes']['decode'];print(r[int(n[1:])]['ip'])")
PORT=$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['proxy']['port'])")
PROMPT="The future of AI is"
MAX_TOKENS=50
COUNT=3
WAIT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --prompt) PROMPT="$2"; shift 2 ;;
        --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
        --count) COUNT="$2"; shift 2 ;;
        --wait) WAIT=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

URL="http://${HOST}:${PORT}/v1/completions"
echo "[func_check] 目标: $URL (model=$MODEL, count=$COUNT)"

if [ "$WAIT" = "1" ]; then
    echo "[func_check] 等待 proxy 可用..."
    until curl -sf -o /dev/null --max-time 5 "$URL" 2>/dev/null; do sleep 5; done
fi

ok=0
for i in $(seq 1 "$COUNT"); do
    start=$(date +%s%N)
    resp=$(curl -sf --max-time 120 "$URL" -H "Content-Type: application/json" \
        -d "{\"model\": \"$MODEL\", \"prompt\": \"$PROMPT\", \"max_completion_tokens\": $MAX_TOKENS, \"temperature\": 0}" 2>/dev/null) \
        && echo "$resp" | grep -q '"choices"' && code=200 || code=fail
    end=$(date +%s%N)
    ms=$(( (end - start) / 1000000 ))
    if [ "$code" = "200" ]; then
        ok=$((ok+1))
        echo "  ✓ 请求 $i: HTTP 200 (${ms}ms)"
    else
        echo "  ✗ 请求 $i: 失败 (${ms}ms)"
    fi
done
echo "[func_check] 结果: ${ok}/${COUNT} 成功"
[ "$ok" -eq "$COUNT" ]