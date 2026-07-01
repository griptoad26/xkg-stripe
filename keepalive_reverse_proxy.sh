#!/bin/bash
# Reverse-proxy keepalive: auto-restarts the xkg-stripe reverse proxy
# on port 8089 if it crashes. Required for the Tailscale funnel to
# keep serving /api/* requests from the public internet.

PROXY_DIR="/home/x2/.openclaw/workspace/xkg-stripe"
LOG_FILE="/tmp/reverse_proxy.log"
PROXY_PORT=8089
PROXY_URL="http://127.0.0.1:${PROXY_PORT}/"

echo "[keepalive-reverse-proxy] starting..."
cd "$PROXY_DIR"

while true; do
    # Probe the proxy: any 2xx/4xx/5xx means it's up (even an
    # upstream error is fine — it means the proxy is reachable).
    if curl -s --connect-timeout 3 --max-time 5 -o /dev/null "$PROXY_URL"; then
        # only log periodically to avoid spam
        :
    else
        echo "[$(date)] proxy down on :${PROXY_PORT}, restarting..."
        nohup python3 -u reverse_proxy.py > "$LOG_FILE" 2>&1 &
        sleep 3
    fi
    sleep 30
done
