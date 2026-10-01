#!/bin/bash
#
# watchdog.sh
#
# Meant to run on a SCHEDULE (cron) from a host that's actually able to
# reach the VM - e.g. linux.wpi.edu - since the VM lives inside the WPI
# network and isn't reachable from GitHub Actions or anything external.
#
# Each run distinguishes two different failure modes and recovers each
# differently, instead of always doing a full rebuild:
#
#   A) VM is up, our group key (mykey) still authenticates, but the app
#      process itself died (crashed, OOM-killed, etc). Fast path: just
#      re-run deploy_second_part.sh to relaunch the app. No need to touch
#      keys or re-clone anything.
#
#   B) mykey no longer authenticates at all. This means the VM was
#      rebooted/reimaged/wiped and reverted to the default student-admin
#      key. Full path: re-run deploy_first_part.sh (which auto-detects
#      whichever key currently works and re-establishes the group key)
#      followed by deploy_second_part.sh.
#
#   C) Neither key works, or recovery itself fails. Nothing this script
#      can do - alert and stop, so it isn't left hammering a genuinely
#      dead machine every few minutes.
#
# A Slack webhook notification fires only on STATE TRANSITIONS (healthy
# -> down, down -> recovered, recovery failed) rather than on every run,
# so a 5-minute cron doesn't spam the channel while things are fine.
#
# A flock-based lock stops two overlapping runs (e.g. a slow recovery
# still in progress when the next cron tick fires) from stepping on each
# other.
#
set -uo pipefail
 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Where deploy_first_part.sh / deploy_second_part.sh / common.sh live, and
# where tmp/ (containing mykey) will be created/reused. Defaults to this
# script's own directory; override if you keep them elsewhere.
DEPLOY_DIR="${DEPLOY_DIR:-$SCRIPT_DIR}"
 
# shellcheck source=./common.sh
source "${DEPLOY_DIR}/common.sh"
 
REPO_NAME="Stock_SuggestorCS1"
STATE_FILE="${SCRIPT_DIR}/.watchdog_state"
LOCK_FILE="${SCRIPT_DIR}/.watchdog.lock"
LOG_FILE="${SCRIPT_DIR}/watchdog.log"
# Set this in the crontab's environment (or source a separate, non-git
# secrets file here) - never hardcode it in a file that goes to GitHub.
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
 
# ---------------------------------------------------------------------------
# Don't let two runs overlap.
# ---------------------------------------------------------------------------
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[$(date '+%H:%M:%S')] Another watchdog run is already in progress - skipping." >> "${LOG_FILE}"
    exit 0
fi
 
# Everything from here on goes to the log file, so cron doesn't need to
# mail output and you have a timestamped trail for the resilience-testing
# writeup.
exec >> "${LOG_FILE}" 2>&1
 
log "===== watchdog run starting ====="
 
notify() {
    local msg="$1"
    log "NOTIFY: ${msg}"
    if [ -n "${SLACK_WEBHOOK_URL}" ]; then
        curl -s -X POST -H "Content-Type: application/json" \
            -d "{\"text\": $(printf '%s' "${msg}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
            "${SLACK_WEBHOOK_URL}" >/dev/null \
            || err "Failed to send Slack notification"
    fi
}
 
previous_state="unknown"
[ -f "${STATE_FILE}" ] && previous_state="$(cat "${STATE_FILE}")"
set_state() { printf '%s' "$1" > "${STATE_FILE}"; }
 
cd "${DEPLOY_DIR}"
 
# ---------------------------------------------------------------------------
# A) Cheap check: does the group key still work, and is the app process
#    actually alive (checked via its pidfile over SSH - this only needs
#    the SSH port, which we know is reachable, rather than assuming the
#    app's own HTTP port is open through any firewall).
# ---------------------------------------------------------------------------
APP_OK=0
if remote_key_works "tmp/mykey"; then
    if ssh -i tmp/mykey -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}" \
        "test -f ${REPO_NAME}/app.pid && kill -0 \$(cat ${REPO_NAME}/app.pid) 2>/dev/null"; then
        APP_OK=1
    fi
fi
 
if [ "${APP_OK}" -eq 1 ]; then
    log "App is up and the group key works. Nothing to do."
    if [ "${previous_state}" != "up" ]; then
        notify "✅ Stock Suggestor is back up and healthy on ${MACHINE}."
    fi
    set_state "up"
    log "===== watchdog run finished (healthy) ====="
    exit 0
fi
 
# ---------------------------------------------------------------------------
# Something's wrong - only alert on the DOWN transition, not every run.
# ---------------------------------------------------------------------------
if [ "${previous_state}" != "down" ]; then
    notify "⚠️ Stock Suggestor looks down on ${MACHINE}. Attempting automatic recovery..."
fi
set_state "down"
 
# ---------------------------------------------------------------------------
# B) VM reachable with the group key, app just isn't running -> fast path.
# ---------------------------------------------------------------------------
if remote_key_works "tmp/mykey"; then
    log "Group key still works but the app isn't running. Redeploying the app only."
    if ./deploy_second_part.sh; then
        notify "✅ App redeployed successfully on ${MACHINE} (VM itself was fine)."
        set_state "up"
        log "===== watchdog run finished (recovered - app only) ====="
        exit 0
    else
        notify "❌ App redeploy FAILED on ${MACHINE} even though the VM is reachable. Manual attention needed."
        log "===== watchdog run finished (recovery failed - app redeploy) ====="
        exit 1
    fi
fi
 
# ---------------------------------------------------------------------------
# C-precheck) Group key is dead - VM may have been rebooted/wiped and
#             reverted to the default key. Try the full recovery path,
#             which itself figures out whichever key currently works.
# ---------------------------------------------------------------------------
log "Group key no longer authenticates - VM may have been reset. Attempting full recovery."
if ./deploy_first_part.sh && ./deploy_second_part.sh; then
    notify "✅ Full recovery succeeded on ${MACHINE} (VM appears to have been rebooted/wiped; group key and app were both re-established)."
    set_state "up"
    log "===== watchdog run finished (recovered - full) ====="
    exit 0
fi
 
# ---------------------------------------------------------------------------
# C) Truly unrecoverable from here - alert loudly and stop, rather than
#    retrying a keygen/scp cycle every few minutes against a dead box.
# ---------------------------------------------------------------------------
notify "🚨 AUTOMATIC RECOVERY FAILED on ${MACHINE}. Neither the group key nor the default key work, or the redeploy itself failed. This needs a human."
log "===== watchdog run finished (unrecoverable) ====="
exit 1