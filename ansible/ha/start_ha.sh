#!/bin/sh
set -e


# 定义配置文件
HAPROXY_CFG="/usr/local/etc/haproxy/haproxy.cfg"

# 1. 检查配置文件是否存在
if [ ! -f "${HAPROXY_CFG}" ]; then
    echo "错误：配置文件 ${HAPROXY_CFG} 不存在" >&2
    exit 1
fi

# 2. 使用 haproxy -c 校验配置语法
if ! haproxy -c -f "${HAPROXY_CFG}" >/dev/null 2>&1; then
    echo "错误：HAProxy 配置语法有误，请检查 ${HAPROXY_CFG}" >&2
    haproxy -c -f "${HAPROXY_CFG}"  # 显示详细错误
    exit 1
fi

# 3. 严格从配置中解析所有前端监听端口（无默认值）：3 个 frontend，每个一个 bind 端口
# 方法一：从 bind 行提取数字端口（支持 bind *:12379 / bind :12379 等）
HAPROXY_PORTS=$(grep -E '^[[:space:]]*bind' "${HAPROXY_CFG}" | \
                grep -oE ':[0-9]+' | sed 's/://' || true)

# 若方法一失败，尝试从 haproxy -c 的输出中抓取 "binding on" 信息
if [ -z "${HAPROXY_PORTS}" ]; then
    HAPROXY_PORTS=$(haproxy -c -f "${HAPROXY_CFG}" 2>&1 | \
                    grep -oE 'binding on [0-9.]+:[0-9]+' | \
                    grep -oE '[0-9]+$' || true)
fi

# 4. 若仍未能解析到端口，报错退出（绝不使用默认值）
if [ -z "${HAPROXY_PORTS}" ]; then
    echo "错误：无法从配置文件 ${HAPROXY_CFG} 中解析出前端监听端口，请确认配置中存在 bind 指令且指定了数字端口" >&2
    exit 1
fi

# 5. 先判断HAProxy进程是否已运行（幂等）
if pgrep haproxy >/dev/null 2>&1; then
    echo "HAProxy 进程已存在，无需重复启动"
else
    # 6. 进程不存在则启动
    echo "启动 HAProxy..."
    haproxy -f "${HAPROXY_CFG}" >/dev/null 2>&1 &
fi

# 7. 等待全部端口就绪
echo "等待 HAProxy 端口监听就绪..."
for port in ${HAPROXY_PORTS}; do
    while ! nc -z 127.0.0.1 ${port}; do
        sleep 1
    done
done
echo "HAProxy 服务就绪（端口：$(echo ${HAPROXY_PORTS} | tr '\n' ' ')）"