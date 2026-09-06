#!/usr/bin/env bash
# barza-up.sh - the Linux twin of barza-up.ps1: bring barza online on this
# node and publish the address book.
#
#   bash linux/barza-up.sh
#
# Idempotent: the service is a systemd unit (started if it is not answering);
# the tunnel is barza-tunnel.service, reused only if its logged URL actually
# answers, restarted (= a new URL) otherwise. Then host.json + status.json are
# written into the checkout - and that is all this script does with them:
# the service commits and pushes them within about 30 s, together with the
# record, so that exactly ONE actor runs git in this working tree.
#
# It publishes only when barza-tunnel.service is enabled on this node. That
# unit is the switch: while it is disabled the tunnel and the address book
# are somebody else's (the workstation's barza-up.ps1), and two publishers
# would fight over host.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${BARZA_PORT:-8901}"
LOCAL="http://127.0.0.1:$PORT"
URL_RE='https://[a-z0-9-]+\.trycloudflare\.com'
# One at a time: two runs (the watchdog and a human, say) would each mint a
# tunnel and publish different names.
exec 9>"$ROOT/.barza-up.lock"
if ! flock -n 9; then echo "another barza-up.sh is running; nothing to do"; exit 0; fi

health() { curl -fsS -m "${2:-3}" "$1/api/health" 2>/dev/null; }
tunnel_urls() { grep -oE "$URL_RE" tunnel.log 2>/dev/null || true; }
# Seconds since barza-tunnel.service last became active (a large number when
# it is not running). A quick-tunnel name looked up within its first ~45 s
# is answered NXDOMAIN, and a caching router (the FRITZ!Box) keeps that
# answer for 20+ minutes - so a fresh name is never probed, only published.
tunnel_age() {
    local t up
    t="$(systemctl show -p ActiveEnterTimestampMonotonic --value barza-tunnel.service 2>/dev/null || echo 0)"
    up="$(awk '{print int($1 * 1000000)}' /proc/uptime)"
    if [ -n "$t" ] && [ "$t" -gt 0 ] 2>/dev/null; then echo $(( (up - t) / 1000000 )); else echo 999999; fi
}

# 1. Service
if health "$LOCAL" >/dev/null; then
    echo "barza service already running"
else
    sudo systemctl start barza.service
    echo "started barza service"
    for _ in $(seq 1 20); do
        sleep 1
        health "$LOCAL" >/dev/null && break
    done
    health "$LOCAL" >/dev/null || { echo "barza.service did not come up - journalctl -u barza" >&2; exit 1; }
fi

# 2. Tunnel - only where this node owns it
if ! systemctl is-enabled --quiet barza-tunnel.service 2>/dev/null; then
    echo "barza-tunnel.service is not enabled on this node: the tunnel and the address book are published elsewhere; nothing more to do here"
    exit 0
fi
url="$(tunnel_urls | tail -1)"
if [ -n "$url" ] && ! systemctl is-active --quiet barza-tunnel.service; then
    url=""
fi
if [ -n "$url" ]; then
    age="$(tunnel_age)"
    # 90 s, not 60: the unit becomes active before cloudflared has minted
    # the name, and the 45 s clock starts at the mint.
    if [ "$age" -lt 90 ]; then
        echo "tunnel started ${age}s ago - not probing a fresh name, publishing it"
    elif ! curl -fsS -m 8 "$url/api/health" >/dev/null 2>&1; then
        echo "tunnel URL in log is not answering - minting a new one"
        url=""
    fi
fi
if [ -z "$url" ]; then
    before="$(tunnel_urls | wc -l)"
    sudo systemctl restart barza-tunnel.service
    echo "started tunnel, waiting for URL..."
    for _ in $(seq 1 30); do
        sleep 1
        now="$(tunnel_urls | wc -l)"
        if [ "$now" -gt "$before" ]; then url="$(tunnel_urls | tail -1)"; break; fi
    done
fi
[ -n "$url" ] || { echo "tunnel URL not found yet - check tunnel.log" >&2; exit 1; }
echo "TUNNEL URL: $url"

# 3. Publish the address book (the service commits + pushes it)
python3 - "$url" "$(health "$LOCAL" || echo '{}')" <<'PY'
import json, socket, sys
from datetime import datetime, timezone
url, health = sys.argv[1], json.loads(sys.argv[2] or "{}")
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
host = health.get("host") or socket.gethostname()
book = {
    "url": url, "urls": [url], "candidates": [url], "updated": now,
    "service": {"host": host, "lan": None},
    "note": ("Written by linux/barza-up.sh on the node and checked every 15 minutes by the "
             "liveness workflow on GitHub's machines. An empty url means the host is offline; "
             "the site then reads the published archive (data/messages.json) instead of "
             "waiting on a dead address."),
}
status = {
    "checked": now, "anyLive": True, "host": url,
    "hosts": [{"url": url, "live": True, "seq": health.get("seq"), "error": None}],
    "checkedBy": f"linux/barza-up.sh on {host}",
    "note": ("Written by barza-up at tunnel start and every 15 minutes by the liveness "
             "workflow. It probes the hosts named in host.json and repoints that file; it "
             "never touches the message record."),
}
for name, obj in (("host.json", book), ("status.json", status)):
    with open(name, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
PY
echo "address book written; the service pushes it within ~30 s (journalctl -u barza | grep 'address book')"
echo "published at: https://enderpeer.github.io/barza/host.json"
