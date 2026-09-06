#!/usr/bin/env bash
# barza-watchdog.sh - one probe; run every 60 s by barza-watchdog.timer.
#
# The Linux twin of barza-watchdog.ps1: is the service answering, is the
# published tunnel answering? If not, run barza-up.sh, then keep a 300 s
# quiet period (a fresh quick-tunnel name looked up within its first 45 s
# gets a 20+ minute NXDOMAIN from a caching router; see the .ps1). The
# tunnel is only this node's business while barza-tunnel.service is
# enabled; otherwise only the service is checked, and systemd's own
# Restart=always has normally already fixed that.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${BARZA_ROOT:-$(dirname "$HERE")}"   # the git working tree
cd "$ROOT" || exit 0
PORT="${BARZA_PORT:-8901}"
LOG="$ROOT/watchdog.log"
STATE="$ROOT/.watchdog-lastfix"
URL_RE='https://[a-z0-9-]+\.trycloudflare\.com'

wlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }

last="$(cat "$STATE" 2>/dev/null || echo 0)"
now="$(date +%s)"
if [ $((now - last)) -lt 300 ]; then exit 0; fi

svc_ok=0
curl -fsS -m 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && svc_ok=1

tunnel_ok=1
url=""
why=""
if systemctl is-enabled --quiet barza-tunnel.service 2>/dev/null; then
    url="$(grep -oE "$URL_RE" tunnel.log 2>/dev/null | tail -1 || true)"
    published="$(python3 -c 'import json; print(json.load(open("host.json", encoding="utf-8-sig")).get("url") or "")' 2>/dev/null || true)"
    t="$(systemctl show -p ActiveEnterTimestampMonotonic --value barza-tunnel.service 2>/dev/null || echo 0)"
    up="$(awk '{print int($1 * 1000000)}' /proc/uptime)"
    age=999999
    if systemctl is-active --quiet barza-tunnel.service && [ -n "$t" ] && [ "$t" -gt 0 ] 2>/dev/null; then
        age=$(( (up - t) / 1000000 ))
    fi
    if [ "$age" -lt 90 ]; then
        : # just (re)started: a fresh name must not be looked up yet (router NXDOMAIN caching); next round
    elif [ -z "$url" ] || ! systemctl is-active --quiet barza-tunnel.service; then
        tunnel_ok=0; why="tunnel not running"
    elif ! curl -fsS -m 8 "$url/api/health" >/dev/null 2>&1; then
        tunnel_ok=0; why="tunnel URL not answering"
    elif [ "$url" != "$published" ]; then
        # the tunnel is fine but the address book names something else (a
        # restart minted a new name, or the workflow cleared a dead one)
        tunnel_ok=0; why="address book names '$published'"
    fi
fi

if [ "$svc_ok" = 1 ] && [ "$tunnel_ok" = 1 ]; then exit 0; fi

echo "$now" > "$STATE"
wlog "fix: svc_ok=$svc_ok tunnel_ok=$tunnel_ok ($why; url=$url) - running barza-up.sh"
bash "$HERE/barza-up.sh" 2>&1 | while IFS= read -r line; do wlog "up: $line"; done
wlog "fix cycle done; next probe in 300 s"
