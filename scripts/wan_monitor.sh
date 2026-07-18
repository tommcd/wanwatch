#!/usr/bin/env bash
# wan_monitor.sh - log WAN vs LAN reachability to diagnose ISP dropouts.
# Samples every INTERVAL seconds; logs state changes, every sample during
# an outage, and a heartbeat. Single-instance safety is the supervisor's
# job (Task Scheduler "do not start a new instance", or wanctl).
#
# Usage:  ./wan_monitor.sh [logfile]

ROUTER="${WANWATCH_ROUTER:-192.168.1.254}"
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

prev_lan=""
prev_wan=""
last_beat=$(date +%s)

log "$(check "$ROUTER")" "-" "monitor_started"

while true; do
    lan=$(check "$ROUTER")

    wan=DOWN
    if [[ $(check "$WAN_HOST_1") == UP ]] || [[ $(check "$WAN_HOST_2") == UP ]]; then
        wan=UP
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

    sleep "$INTERVAL"
done
