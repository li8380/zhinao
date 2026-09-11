#!/bin/sh
# ============================================================
# Gateway probe/relaunch (model-gateway service, 2026-09-09)
# Deployed on OpenWRT router, runs every minute via crontab.
# Local service -> no SSH indirection (unlike wd.sh Windows svcs)
#   alive                  -> silent
#   dead + relaunch OK     -> INFO log
#   dead + relaunch fail   -> ERROR log + cooldown marker
# ============================================================
GWDIR=/root/model-gateway
LOGFILE=/root/watch/log/gateway.log
STATE=/root/.watch_retry_gateway
COOLDOWN=300
BIN=node

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# 1. probe
if curl -sf -m 5 http://127.0.0.1:4100/healthz >/dev/null 2>&1; then
    # alive: drop any stale failure marker
    [ -f "$STATE" ] && { rm -f "$STATE"; echo "$(ts) INFO  gateway RECOVERED (probe alive after earlier failure)" >> "$LOGFILE"; }
    exit 0
fi

# 2. respect cooldown
now=$(date +%s)
if [ -f "$STATE" ]; then
    last=$(cat "$STATE" 2>/dev/null)
    if [ -n "$last" ] && [ $((now - last)) -lt "$COOLDOWN" ]; then
        exit 0
    fi
fi

# 3. relaunch (detach fully: nohup + redirect, keep pid)
cd "$GWDIR" || { echo "$(ts) ERROR gateway dir missing: $GWDIR" >> "$LOGFILE"; exit 1; }
# kill any zombie process holding the port
pkill -f 'gateway.mjs' 2>/dev/null && sleep 1
nohup $BIN gateway.mjs --config config.json >> /root/watch/log/gateway_svc.log 2>&1 &
sleep 3

# 4. confirm
if curl -sf -m 5 http://127.0.0.1:4100/healthz >/dev/null 2>&1; then
    echo "$(ts) INFO  gateway relaunch OK (restored)" >> "$LOGFILE"
    rm -f "$STATE"
else
    echo "$(ts) ERROR gateway relaunch FAILED (still dead); retry in ${COOLDOWN}s" >> "$LOGFILE"
    echo "$now" > "$STATE"
fi