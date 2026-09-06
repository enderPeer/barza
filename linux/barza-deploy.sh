#!/usr/bin/env bash
# barza-deploy.sh - continuous deployment on the node: run the newest commit
# of main whose tests passed, and nothing else.
#
# CI (.github/workflows/ci.yml) moves the branch pointer `ci-passed` to a
# main commit once every test job is green. Every 60 s (barza-deploy.timer)
# this fetches that pointer through the node's deploy key - no GitHub token
# on the node, no inbound connection, nothing a fork's pull request could
# reach - and when it moved:
#   1. exports that exact commit into a fresh run directory (git archive),
#      compiles it, and swaps it in; the previous version is kept beside it
#   2. runs its linux/install.sh, which re-renders the units for the new
#      code directory and restarts the service
#   3. waits for /api/health to report that commit; if it does not within
#      30 s, swaps the previous version back, restarts, and posts an alert
#   4. posts one line on the board
# A commit that failed to deploy is remembered and not retried until CI
# promotes a newer one. The git working tree (BARZA_ROOT) is never checked
# out to a different commit: the service keeps committing the record there.
#
# Environment: BARZA_ROOT (the git working tree; default ~/barza),
# BARZA_RUN_DIR (the exported code; default ~/barza-run), BARZA_PORT (8901).
set -uo pipefail

ROOT="${BARZA_ROOT:-$HOME/barza}"
RUN="${BARZA_RUN_DIR:-$HOME/barza-run}"
PORT="${BARZA_PORT:-8901}"
LOG="$ROOT/deploy.log"
FAILED_MARK="$ROOT/.deploy-failed"
REF="ci-passed"

dlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }

# post <type> <title> <body>: one line on the board, best effort
post() {
    python3 - "$1" "$2" "$3" <<'PY' | curl -fsS -m 10 -X POST "http://127.0.0.1:$PORT/api/messages" \
        -H 'Content-Type: application/json' --data-binary @- >/dev/null 2>&1 || true
import json, sys
print(json.dumps({"author": "barza-deploy", "type": sys.argv[1], "title": sys.argv[2], "body": sys.argv[3]}))
PY
}

# healthy <commit>: the service answers and (when a commit is given) runs it
healthy() {
    local want="$1" h
    for _ in $(seq 1 30); do
        sleep 1
        h="$(curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null || true)"
        if [ -n "$h" ]; then
            if [ -z "$want" ]; then return 0; fi
            case "$h" in *"\"commit\": \"$want\""*) return 0 ;; esac
        fi
    done
    return 1
}

run_install() { # <code dir>
    BARZA_ROOT="$ROOT" BARZA_CODE_DIR="$1" bash "$1/linux/install.sh" >> "$LOG" 2>&1
}

cd "$ROOT" || exit 0
if ! git fetch -q origin "+refs/heads/$REF:refs/remotes/origin/$REF" "+refs/heads/main:refs/remotes/origin/main" 2>>"$LOG"; then
    dlog "fetch failed (is the deploy key registered and $REF published by CI yet?)"
    exit 0
fi
want="$(git rev-parse -q --verify "refs/remotes/origin/$REF" 2>/dev/null || true)"
[ -n "$want" ] || exit 0
have="$(cat "$RUN/.sha" 2>/dev/null || true)"
if [ "$want" = "$have" ]; then exit 0; fi
if [ "$want" = "$(cat "$FAILED_MARK" 2>/dev/null || true)" ]; then exit 0; fi
if ! git merge-base --is-ancestor "$want" refs/remotes/origin/main; then
    dlog "refusing $want: $REF is not on main's history"
    exit 0
fi
short="${want:0:7}"
subject="$(git log -1 --format=%s "$want")"
dlog "deploying $short ($subject); running: ${have:-the checkout}"

# One actor at a time: the watchdog's barza-up.sh takes the same lock.
exec 9>"$ROOT/.barza-up.lock"
if ! flock -w 60 9; then dlog "could not take the up-lock; next round"; exit 0; fi

new="$RUN.new"
rm -rf "$new"
mkdir -p "$new"
if ! git archive "$want" | tar -x -C "$new"; then
    dlog "export of $short failed"
    rm -rf "$new"
    exit 0
fi
rm -rf "$new/data" "$new/inbox"
echo "$want" > "$new/.sha"
if ! python3 -m py_compile "$new/barza_server.py" "$new/barza-relay.py" 2>>"$LOG"; then
    dlog "$short does not compile; not deploying"
    echo "$want" > "$FAILED_MARK"
    rm -rf "$new"
    post alert "barza-deploy: $short refused on $(hostname)" "py_compile failed; see deploy.log on the node"
    exit 0
fi

prev="$RUN.prev"
rm -rf "$prev"
if [ -d "$RUN" ]; then mv "$RUN" "$prev"; fi
mv "$new" "$RUN"

if run_install "$RUN" && healthy "$want"; then
    dlog "deployed $short"
    rm -f "$FAILED_MARK"
    post update "barza deployed $short on $(hostname)" \
        "$subject - tests passed in CI (branch $REF), exported and restarted by linux/barza-deploy.sh; before: ${have:0:7}${have:+ }${have:+(kept as ~/barza-run.prev)}${have:-the checkout}."
    exit 0
fi

dlog "deploy of $short FAILED - rolling back"
echo "$want" > "$FAILED_MARK"
rm -rf "$RUN.failed"
mv "$RUN" "$RUN.failed"
if [ -d "$prev" ]; then
    mv "$prev" "$RUN"
    if run_install "$RUN" && healthy "$have"; then
        dlog "rolled back to ${have:0:7}"
        post alert "barza-deploy: $short failed on $(hostname), rolled back to ${have:0:7}" \
            "The new version did not report healthy within 30 s; the previous export is running again. Failed export kept as ~/barza-run.failed, log in deploy.log."
    else
        dlog "rollback to ${have:0:7} did not come up either"
    fi
else
    # nothing exported before: the checkout itself was running
    if run_install "$ROOT" && healthy ""; then
        dlog "rolled back to the checkout"
        post alert "barza-deploy: $short failed on $(hostname), rolled back to the checkout" \
            "The new version did not report healthy within 30 s. Failed export kept as ~/barza-run.failed, log in deploy.log."
    else
        dlog "rollback to the checkout did not come up either"
    fi
fi
exit 0
