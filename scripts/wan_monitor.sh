#!/usr/bin/env bash
# wan_monitor.sh - log WAN vs LAN reachability to diagnose ISP dropouts.
# Samples every INTERVAL seconds; logs state changes, every sample during
# an outage, and a heartbeat. Single-instance safety is the supervisor's
# job (Task Scheduler "do not start a new instance", or wanctl).
#
# Usage:  ./wan_monitor.sh [logfile]

# Optional local config (heartbeat URL etc.) - never committed
[[ -f "$HOME/.wanwatch.env" ]] && source "$HOME/.wanwatch.env"

ROUTER="${WANWATCH_ROUTER:-192.168.1.254}"
PING_URL="${WANWATCH_PING_URL:-}"   # dead-man's-switch heartbeat (empty = off)
WAN_HOST_1="1.1.1.1"        # Cloudflare
WAN_HOST_2="8.8.8.8"        # Google - WAN is UP if either replies
INTERVAL=10                  # seconds between samples
HEARTBEAT=60                 # seconds between "still alive" lines
LOG="${1:-$HOME/wan_log.csv}"

# --- platform ---------------------------------------------------------------
PING="ping -c 1 -W 2"
if [[ "$(uname -s)" == MINGW* ]]; then
    PING="ping -n 1 -w 2000"      # Git Bash: Windows ping syntax
fi

# Require a real echo reply (TTL=). Windows ping exits 0 on "Destination
# host unreachable", so the exit code alone cannot be trusted.
check() { $PING "$1" 2>/dev/null | grep -qi "ttl=" && echo UP || echo DOWN; }
ts()    { date '+%Y-%m-%d %H:%M:%S'; }
log()   { echo "$(ts),$1,$2,$3" >> "$LOG"; }

[[ -f "$LOG" ]] || echo "timestamp,lan,wan,event" > "$LOG"
echo "# wanwatch-monitor host=$(hostname) router=$ROUTER wan=$WAN_HOST_1,$WAN_HOST_2 interval=${INTERVAL}s tz=$(date +%z)" >> "$LOG"

prev_lan=""
prev_wan=""
last_beat=$(date +%s)
last_ping=0

log "$(check "$ROUTER")" "-" "monitor_started"

while true; do
    lan=$(check "$ROUTER")

    wan=DOWN
    if [[ $(check "$WAN_HOST_1") == UP ]] || [[ $(check "$WAN_HOST_2") == UP ]]; then
        wan=UP
    elif curl -s -m 3 -o /dev/null "https://1.1.1.1/" 2>/dev/null; then
        wan=UP_TCP   # HTTPS reachable though ICMP failed: filtering, not outage
    fi

    now=$(date +%s)

    if [[ "$lan" != "$prev_lan" || "$wan" != "$prev_wan" ]]; then
        log "$lan" "$wan" "state_change"
        prev_lan=$lan
        prev_wan=$wan
        last_beat=$now
    elif [[ "$wan" == DOWN || "$lan" == DOWN ]]; then
        log "$lan" "$wan" "outage_sample"     # exact outage duration
    elif (( now - last_beat >= HEARTBEAT )); then
        log "$lan" "$wan" "heartbeat"
        last_beat=$now
    fi

    # heartbeat to the hosted dead-man's switch: sent only while the
    # WAN is up, so silence there = connection (or machine) down
    if [[ -n "$PING_URL" && "$wan" == UP ]] && (( now - last_ping >= 60 )); then
        curl -sfm 5 "$PING_URL" >/dev/null 2>&1 && last_ping=$now
    fi

    sleep "$INTERVAL"
done
