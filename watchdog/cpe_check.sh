#!/bin/sh
# ============================================================
# CPE route/health monitor (2026-09-03 kevis)
# Watches the dual-CPE setup: eth0=wan/CPE1 (192.168.3.x, gw <LAN_IP>)
#                              eth1=wancpe/CPE2 (192.168.5.x, gw <LAN_IP>)
# Logs changes only (no auto-fix; route changes need 先生's decision).
# State hash in /tmp so reboots start fresh without false alarms.
# ============================================================

LOG=/root/watch/log/cpe_route.log
STATE=/tmp/cpe_route_state
IFACE_STATE=/tmp/cpe_iface_state.$$

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Collect a compact status snapshot -> stdout
snapshot() {
    local e0 e1 g0 g1 n0 n1 t1 t2 nft
    # eth0 (CPE1)
    if ip link show eth0 2>/dev/null | grep -q 'state UP'; then e0=UP; else e0=DOWN; fi
    g0=$(ip -4 addr show dev eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
    [ -z "$g0" ] && g0='-'
    n0=$(ip -4 addr show dev eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | cut -d. -f1-3)
    # eth1 (CPE2)
    if ip link show eth1 2>/dev/null | grep -q 'state UP'; then e1=UP; else e1=DOWN; fi
    g1=$(ip -4 addr show dev eth1 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
    [ -z "$g1" ] && g1='-'
    n1=$(ip -4 addr show dev eth1 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | cut -d. -f1-3)
    # default route tables
    t1=$(ip route show table 1 2>/dev/null | grep default | awk '{print $3" "$5}')
    [ -z "$t1" ] && t1='NONE'
    t2=$(ip route show table 2 2>/dev/null | grep default | awk '{print $3" "$5}')
    [ -z "$t2" ] && t2='NONE'
    # nft balance table alive?
    if nft list table inet dae_balance >/dev/null 2>&1; then nft=OK; else nft=MISSING; fi
    echo "e0=${e0}:${g0}(${n0})|e1=${e1}:${g1}(${n1})|t1=${t1}|t2=${t2}|nft=${nft}"
}

# Gateway reachability (separate, since ping fails can be transient)
pinggw() {
    # $1=interface ip to ping, $2=label
    local ip="$1" label="$2" now
    now=$(ts)
    if [ -n "$ip" ] && [ "$ip" != "-" ]; then
        if ping -c 1 -W 2 "$ip" >/dev/null 2>&1; then
            echo "$now INFO  gw $label ($ip) reachable" >> "$LOG"
        else
            echo "$now WARN  gw $label ($ip) UNREACHABLE" >> "$LOG"
        fi
    fi
}

# --- main ---
SNAP=$(snapshot)
if [ -f "$STATE" ]; then
    OLD=$(cat "$STATE")
    if [ "$OLD" != "$SNAP" ]; then
        echo "$(ts) CHANGE" >> "$LOG"
        echo "  old: $OLD" >> "$LOG"
        echo "  new: $SNAP" >> "$LOG"
    fi
else
    echo "$(ts) INIT $SNAP" >> "$LOG"
fi
echo "$SNAP" > "$STATE"

# Every run: verify nft table + emit warning once when missing
SNAP_NFT=$(echo "$SNAP" | grep -o 'nft=[A-Z]*')
case "$SNAP_NFT" in
    nft=MISSING)
        [ -f /tmp/cpe_nft_warned ] || { echo "$(ts) ERROR nft dae_balance table MISSING" >> "$LOG"; touch /tmp/cpe_nft_warned; }
        ;;
    *)
        rm -f /tmp/cpe_nft_warned
        ;;
esac

# Gateway reachability probe - bounded (once per iface per run is cheap:
# 2 pings, 2s timeout each). Log only failures to keep log small.
e0ip=$(ip -4 addr show dev eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
e1ip=$(ip -4 addr show dev eth1 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
for gw in <LAN_IP> <LAN_IP>; do
    if ! ping -c 1 -W 2 "$gw" >/dev/null 2>&1; then
        echo "$(ts) WARN  gateway $gw UNREACHABLE" >> "$LOG"
    fi
done

# rotate log if huge
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 262144 ] && { mv "$LOG" "$LOG.1"; echo "$(ts) log rotated" >> "$LOG"; }

rm -f "$IFACE_STATE"
exit 0