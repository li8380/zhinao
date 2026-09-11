#!/bin/sh
# ============================================================
# Watchdog Platform - keep Windows resident services alive
# ============================================================
# Reads a service registry and watches each entry:
#   alive                        -> silent
#   target unreachable (ssh-lvl) -> INFO once per period, checks suspended
#   dead + relaunch              -> INFO log
#   dead + fail (target online)  -> ERROR log + cooldown (no hammering), retry
#
# Registry: /root/watch/services.conf
#   line format: svc_name|probe_remote_cmd|relaunch_remote_cmd|cooldown_sec
#   probe_remote_cmd  : ssh target + remote command returning exit 0 if alive
#   relaunch_remote_cmd: ssh target + remote command to start the service
#   cooldown_sec      : min seconds between failed relaunch attempts (default 300)
#   '#' comment lines and empty lines are ignored.
#
# Logs: /root/watch/log/<svc>.log   (ERROR/INFO only)
# Cooldown markers: /root/.watch_retry_<svc>
# Offline markers : /root/.watch_offline_<svc>   (2026-09-08 noise-fix)
# ============================================================

CONF=/root/watch/services.conf
LOGDIR=/root/watch/log
SSH_BASE="ssh -i /root/.ssh/id_distill -o StrictHostKeyChecking=no -o BatchMode=yes"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# event sink -> Windows memory dir (watchdog-events-YYYY-MM-DD.md)
# only on state transitions; non-fatal if link is down (svc dead often == link dead)
# NOTE: Windows OpenSSH default shell = cmd.exe -> cmd-compatible encoding:
#   no sh single quotes, '|' must be escaped as '^|' (cmd pipe char)
ev() {
    _d=$(date '+%Y-%m-%d')
    _esc=$(echo "$*" | sed 's/|/^|/g')
    _f="C:\\Users\\<USER>\\.qwenpaw\\workspaces\\default\\memory\\watchdog-events-$_d.md"
    $SSH_BASE <USER>@<WINDOWS_HOST_IP> "echo $_esc >> $_f" < /dev/null >/dev/null 2>&1 || true
}

[ -f "$CONF" ] || { echo "$(ts) FATAL services.conf missing" >> "$LOGDIR/wd.log"; exit 2; }

while IFS='|' read -r svc probe_cmd relaunch_cmd cooldown || [ -n "$svc" ]; do
    # skip blanks / comments
    case "$svc" in ""|\#*) continue ;; esac
    [ -z "$cooldown" ] && cooldown=300
    LOG="$LOGDIR/$svc.log"
    STATE="/root/.watch_retry_$svc"
    OFFLINE="/root/.watch_offline_$svc"

    # 1. probe alive? capture output + rc to classify offline vs dead
    # 2026-09-03 修复：ssh 会消费继承的 stdin（< "$CONF" 文件描述符），把
    # services.conf 剩余行当 stdin 转发走 -> 后续服务行 read 时 EOF 被吞。
    # 所以所有 ssh 调用必须 < /dev/null。
    out=$($SSH_BASE $probe_cmd < /dev/null 2>&1)
    prc=$?
    if [ "$prc" -eq 0 ]; then
        if [ -f "$OFFLINE" ]; then
            rm -f "$OFFLINE"
            echo "$(ts) INFO  target back online, $svc alive (check resumed)" >> "$LOG"
            ev "$(ts) | $svc | BACK_ONLINE | target reachable, $svc alive"
        fi
        if [ -f "$STATE" ]; then
            rm -f "$STATE"
            ev "$(ts) | $svc | RECOVERED | watchdog probe alive after earlier failure"
        fi
        continue
    fi

    # 1b. ssh-level failure (connect refused/timeout/no route/auth): the target
    #     Windows machine is off or asleep -> nothing we can do. Log ONCE per
    #     offline period and suspend (no relaunch hammering, no ERROR spam).
    #     ERROR is reserved for "target reachable but service dead" (real signal).
    #     2026-09-08 noise-fix.
    firstl=$(echo "$out" | tr '\n' ' ' | cut -c1-100)
    if echo "$out" | grep -qiE 'connection refused|timed? ?out|no route to host|network is unreachable|host is down|connection reset|didn.t validate host key|permission denied'; then
        if [ ! -f "$OFFLINE" ]; then
            echo "$(ts) INFO  target unreachable: $firstl ($svc check suspended)" >> "$LOG"
            ev "$(ts) | $svc | OFFLINE | target unreachable; check suspended"
        fi
        echo "$(ts)" > "$OFFLINE"
        continue
    fi

    # 1c. target reachable but service dead -> relaunch flow below

    # 2. respect cooldown after a failed attempt
    now=$(date +%s)
    if [ -f "$STATE" ]; then
        last=$(cat "$STATE" 2>/dev/null)
        if [ -n "$last" ] && [ $((now - last)) -lt "$cooldown" ]; then
            continue
        fi
    fi

    # 3. try relaunch, then confirm
    if $SSH_BASE $relaunch_cmd < /dev/null >/dev/null 2>&1; then
        sleep 3
        if $SSH_BASE $probe_cmd < /dev/null >/dev/null 2>&1; then
            if [ -f "$OFFLINE" ]; then
                rm -f "$OFFLINE"
                echo "$(ts) INFO  target back online, $svc restored (check resumed)" >> "$LOG"
            else
                echo "$(ts) INFO  relaunch OK ($svc restored)" >> "$LOG"
            fi
            rm -f "$STATE"
            ev "$(ts) | $svc | RELINKED | relaunch OK"
            continue
        fi
    fi

    # 4. failed while target reachable -> ERROR + cooldown marker (real signal)
    echo "$(ts) ERROR relaunch FAILED ($svc still dead after attempt); retry in ${cooldown}s" >> "$LOG"
    echo "$now" > "$STATE"
    ev "$(ts) | $svc | FAILED | relaunch failed; retry in ${cooldown}s"
done < "$CONF"
