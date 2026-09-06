#!/usr/bin/env bash
# install.sh - make this Linux box a barza node. Idempotent.
#
#   bash linux/install.sh
#
# Run as the user that owns this checkout; it uses sudo for apt and systemd.
# What it does:
#   1. cloudflared from Cloudflare's apt repository (needed only by the
#      optional tunnel unit, installed now so that enabling it later is one
#      command)
#   2. a repo-local git identity and pull.rebase, and an ssh push url - the
#      service pushes the record with the node's deploy key (deploy-node.ps1
#      on the workstation generates and registers it)
#   3. the systemd units from linux/, rendered for this user, code directory,
#      working tree, LAN address and port; barza.service enabled and
#      (re)started; barza-deploy.timer enabled - from then on the node
#      deploys whatever CI promotes to `ci-passed` (linux/barza-deploy.sh)
#   4. a health check on both addresses
#
# barza-tunnel.service and barza-watchdog.timer are installed but enabled
# only with BARZA_TUNNEL=1: a reverse tunnel to the internet is the network
# owner's call, not an installer's default. With BARZA_TUNNEL=1 the tunnel
# is minted by linux/barza-up.sh (never probed while fresh, see there) and
# the address book is published from this node.
#
# Two directories: the CODE the units run (this script's tree - the checkout
# on the first run, the deployer's export afterwards) and the git working
# tree ROOT that holds the record and the address book.
#
# Environment: BARZA_ROOT (working tree; default: this script's tree),
# BARZA_CODE_DIR (default: this script's tree), BARZA_LAN_IP (default: the
# source address of the default route), BARZA_PORT (default 8901),
# BARZA_TUNNEL=1 (see above).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="${BARZA_CODE_DIR:-$(dirname "$HERE")}"
ROOT="${BARZA_ROOT:-$CODE}"
USER_NAME="$(id -un)"
PORT="${BARZA_PORT:-8901}"
LAN_IP="${BARZA_LAN_IP:-}"
if [ -z "$LAN_IP" ]; then
    LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
fi
[ -n "$LAN_IP" ] || { echo "install: cannot determine the LAN address; set BARZA_LAN_IP" >&2; exit 1; }

say() { echo "== $* =="; }
die() { echo "install: $*" >&2; exit 1; }

say "prerequisites"
command -v python3 >/dev/null 2>&1 || die "need python3"
command -v git >/dev/null 2>&1 || die "need git"
command -v curl >/dev/null 2>&1 || die "need curl"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "need python3 >= 3.10 (found $(python3 --version))"
echo "python3: $(python3 --version)   git: $(git --version)"

if ! command -v cloudflared >/dev/null 2>&1; then
    say "cloudflared (Cloudflare's apt repository)"
    sudo mkdir -p --mode=0755 /usr/share/keyrings
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
        | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
        | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
    sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared
fi
echo "cloudflared: $(cloudflared --version)"

say "git (repo-local identity, rebase pulls, ssh push url)"
cd "$ROOT"
git config user.name  >/dev/null 2>&1 || git config user.name  "barza on $(hostname)"
git config user.email >/dev/null 2>&1 || git config user.email "barza-$(hostname)@users.noreply.github.com"
git config pull.rebase true
origin="$(git remote get-url origin)"
case "$origin" in
    https://github.com/*)
        slug="${origin#https://github.com/}"; slug="${slug%.git}"
        git remote set-url --push origin "git@github.com:${slug}.git" ;;
esac
echo "identity: $(git config user.name) <$(git config user.email)>"
echo "push url: $(git remote get-url --push origin)"

say "systemd units (user=$USER_NAME code=$CODE tree=$ROOT lan=$LAN_IP port=$PORT)"
render() {
    sed -e "s#@USER@#$USER_NAME#g" -e "s#@ROOT@#$ROOT#g" -e "s#@CODE@#$CODE#g" \
        -e "s#@LAN_IP@#$LAN_IP#g" -e "s#@PORT@#$PORT#g" "$1"
}
for unit in barza.service barza-tunnel.service barza-watchdog.service barza-watchdog.timer \
            barza-deploy.service barza-deploy.timer; do
    render "$CODE/linux/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now barza.service
if systemctl is-active --quiet barza.service; then
    # a running service does not reload its code by itself
    sudo systemctl restart barza.service
fi
sudo systemctl enable --now barza-deploy.timer

say "health"
ok=0
for _ in $(seq 1 20); do
    sleep 1
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok=1; break; fi
done
[ "$ok" = 1 ] || { systemctl status barza.service --no-pager || true; die "barza.service did not come up on 127.0.0.1:$PORT"; }
echo "loopback: $(curl -fsS -m 3 "http://127.0.0.1:$PORT/api/health")"
echo "lan:      $(curl -fsS -m 3 "http://$LAN_IP:$PORT/api/health")"
echo "unit:     barza.service $(systemctl is-active barza.service), $(systemctl is-enabled barza.service)"

if [ "${BARZA_TUNNEL:-0}" = 1 ]; then
    say "tunnel + watchdog (BARZA_TUNNEL=1)"
    # enable, not --now: barza-up.sh starts the tunnel itself, on the path
    # that never looks up a freshly minted name. The timer comes last: its
    # OnBootSec has long passed, so "--now" fires the watchdog immediately,
    # and that must find a tunnel already minted rather than mint a second.
    sudo systemctl enable barza-tunnel.service
    BARZA_ROOT="$ROOT" bash "$CODE/linux/barza-up.sh"
    sudo systemctl enable --now barza-watchdog.timer
fi

echo
echo "barza node installed."
echo "  code      $CODE   (tree: $ROOT)"
echo "  service   http://127.0.0.1:$PORT and http://$LAN_IP:$PORT   (journalctl -u barza -f)"
echo "  deploy    barza-deploy.timer every 60 s: runs what CI promotes to ci-passed (deploy.log)"
if systemctl is-enabled --quiet barza-tunnel.service 2>/dev/null; then
    echo "  tunnel    barza-tunnel.service + barza-watchdog.timer enabled; URL in $ROOT/tunnel.log,"
    echo "            published to host.json by the service (journalctl -u barza | grep 'address book')"
else
    echo "  tunnel    not enabled (a reverse tunnel is the network owner's call)."
    echo "            To enable:  BARZA_TUNNEL=1 bash $ROOT/linux/install.sh"
fi
echo "BARZA_LAN_IP=$LAN_IP"
