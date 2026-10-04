#!/bin/bash
#
# watchdog.sh
#
# Meant to run on a SCHEDULE (cron) from a host that's actually able to
# reach the VM - e.g. linux.wpi.edu - since the VM lives inside the WPI
# network and isn't reachable from GitHub Actions or anything external.
#
# Each run first asks "can I even reach the VM?", then distinguishes the
# failure modes and recovers each differently, instead of always rebuilding:
#
#   0) The VM's SSH port does not answer at all (VM down or network trouble).
#      Nothing can be fixed from here and, importantly, nothing should be
#      TOUCHED: no key changes, no redeploys. Alert once and wait.
#
#   A) VM is up, our group key (mykey) still authenticates, but the app
#      process itself died. Fast path: re-run deploy_second_part.sh.
#
#   B) mykey no longer authenticates (checked with retries, so a network
#      blip is not mistaken for a revoked key). The VM was probably
#      rebooted/reimaged and reverted to the default student-admin key. Full
#      path: deploy_first_part.sh then deploy_second_part.sh.
#
#   C) Recovery itself fails. Alert ONCE, then retry only every
#      RETRY_MIN_GAP seconds so a broken box is not hammered every minute
#      (and our own host is not flagged for repeated failed logins).
#
# Slack notifications fire only on STATE TRANSITIONS
# (up -> down, unreachable, failed, and back to up), not on every run.
# States: up | down | unreachable | failed
#
# Create a file named PAUSE next to this script to make every run a no-op
# (handy while repairing keys by hand); delete it to resume.
#
# A mkdir-based lock stops two overlapping runs (including runs started by
# different login nodes) from stepping on each other.
#
set -uo pipefail
 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Where deploy_first_part.sh / deploy_second_part.sh / common.sh live, and
# where tmp/ (containing mykey) will be created/reused. Defaults to this
# script's own directory; override if you keep them elsewhere.
DEPLOY_DIR="${DEPLOY_DIR:-$SCRIPT_DIR}"
 
# shellcheck source=./common.sh
source "${DEPLOY_DIR}/common.sh"
if [ -f "${DEPLOY_DIR}/PAUSE" ]; then exit 0; fi
 
REPO_NAME="Stock_SuggestorCS1"
STATE_FILE="${SCRIPT_DIR}/.watchdog_state"
ATTEMPT_FILE="${SCRIPT_DIR}/.watchdog_last_attempt"
# One log per login node: several nodes share this folder over network
# storage, and simultaneous appends to one file corrupt each other.
LOG_FILE="${SCRIPT_DIR}/watchdog.$(hostname -s).log"
RETRY_MIN_GAP="${RETRY_MIN_GAP:-600}"
# Set this in the crontab's environment (or source a separate, non-git
# secrets file here) - never hardcode it in a file that goes to GitHub.
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"
 
# ---------------------------------------------------------------------------
# Don't let two runs overlap.
# ---------------------------------------------------------------------------
LOCK_DIR="${SCRIPT_DIR}/.watchdog.lock.d"
if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
    # A lock older than 30 minutes is treated as stale (e.g. a crashed run)
    if [ -n "$(find "${LOCK_DIR}" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        rmdir "${LOCK_DIR}" 2>/dev/null
        mkdir "${LOCK_DIR}" 2>/dev/null || exit 0
    else
        echo "[$(date '+%H:%M:%S')] Another watchdog run is already in progress - skipping." >> "${LOG_FILE}"
        exit 0
    fi
fi
trap 'rmdir "${LOCK_DIR}" 2>/dev/null' EXIT
 
# Everything from here on goes to the log file, so cron doesn't need to
# mail output and you have a timestamped trail for the resilience-testing
# writeup.
exec >> "${LOG_FILE}" 2>&1
 
log "===== watchdog run starting ====="
if [ "${MACHINE_VIA_FALLBACK}" -eq 1 ]; then
    log "DNS could not resolve ${MACHINE_NAME_ORIGINAL} from $(hostname -s); using fallback IP ${MACHINE}."
fi
 
notify() {
    local msg="$1"
    log "NOTIFY: ${msg}"
    if [ -n "${SLACK_WEBHOOK_URL}" ]; then
        curl -s -m 10 --retry 2 --retry-delay 2 -X POST -H "Content-Type: application/json" \
            -d "{\"text\": $(printf '%s' "${msg}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
            "${SLACK_WEBHOOK_URL}" >/dev/null \
            || err "Failed to send Slack notification"
    fi
}
 
previous_state="unknown"
if [ -f "${STATE_FILE}" ]; then previous_state="$(cat "${STATE_FILE}")"; fi
set_state() { printf '%s' "$1" > "${STATE_FILE}"; }
 
cd "${DEPLOY_DIR}"
 
# ---------------------------------------------------------------------------
# 0) Can we reach the VM's SSH port at all? If not, there is nothing safe to
#    do. In particular we must NOT start a key rebuild: a login that fails
#    because the network is down looks exactly like a revoked key.
# ---------------------------------------------------------------------------
if ! vm_reachable_retry 3 5; then
    log "The VM's SSH port (${MACHINE}:${PORT}) is not reachable from $(hostname -s). Making no changes."
    if [ "${previous_state}" != "unreachable" ]; then
        notify "🔌 ${MACHINE}:${PORT} is not reachable from the monitoring host. No changes will be made; will keep checking."
    fi
    set_state "unreachable"
    log "===== watchdog run finished (VM unreachable) ====="
    exit 1
fi
 
# ---------------------------------------------------------------------------
# A) Cheap check: does the group key still work, and is the app process
#    actually alive (checked via its pidfile over SSH - this only needs
#    the SSH port, which we know is reachable, rather than assuming the
#    app's own HTTP port is open through any firewall).
# ---------------------------------------------------------------------------
KEY_OK=0
APP_OK=0
if remote_key_works_retry "tmp/mykey" 3 5; then
    KEY_OK=1
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
# (Stay in "failed" if recovery has already failed; that has its own alert.)
# ---------------------------------------------------------------------------
if [ "${previous_state}" != "down" ] && [ "${previous_state}" != "failed" ]; then
    notify "⚠️ Stock Suggestor looks down on ${MACHINE}. Attempting automatic recovery..."
    set_state "down"
fi
 
# ---------------------------------------------------------------------------
# After a failed recovery, wait RETRY_MIN_GAP seconds between attempts.
# ---------------------------------------------------------------------------
if [ "${previous_state}" = "failed" ]; then
    now="$(date +%s)"
    last="$(cat "${ATTEMPT_FILE}" 2>/dev/null || true)"
    [[ "${last}" =~ ^[0-9]+$ ]] || last=0
    if [ $(( now - last )) -lt "${RETRY_MIN_GAP}" ]; then
        log "Recovery failed recently; next attempt in $(( RETRY_MIN_GAP - (now - last) ))s."
        log "===== watchdog run finished (waiting between recovery attempts) ====="
        exit 1
    fi
fi
date +%s > "${ATTEMPT_FILE}"
 
# ---------------------------------------------------------------------------
# B) VM reachable with the group key, app just isn't running -> fast path.
# ---------------------------------------------------------------------------
if [ "${KEY_OK}" -eq 1 ]; then
    log "Group key still works but the app isn't running. Redeploying the app only."
    rc=0
    ./deploy_second_part.sh || rc=$?
    if [ "${rc}" -eq 0 ]; then
        notify "✅ App redeployed successfully on ${MACHINE} (VM itself was fine)."
        set_state "up"
        log "===== watchdog run finished (recovered - app only) ====="
        exit 0
    elif [ "${rc}" -eq 3 ]; then
        log "App was relaunched but could not be verified (VM unreachable during the health check). Not a failure; the next run will verify."
        log "===== watchdog run finished (relaunched, unverified) ====="
        exit 1
    else
        if [ "${previous_state}" != "failed" ]; then
            notify "❌ App redeploy FAILED on ${MACHINE} even though the VM is reachable. Manual attention needed."
        fi
        set_state "failed"
        log "===== watchdog run finished (recovery failed - app redeploy) ====="
        exit 1
    fi
fi
 
# ---------------------------------------------------------------------------
# C-precheck) The VM answers on its SSH port but the group key was rejected
#             on every retry - the VM may have been reset and reverted to the
#             default key. Try the full recovery path, which itself figures
#             out whether the default key works.
# ---------------------------------------------------------------------------
log "Group key no longer authenticates - VM may have been reset. Attempting full recovery."
rc=1
if ./deploy_first_part.sh; then
    rc=0
    ./deploy_second_part.sh || rc=$?
fi
if [ "${rc}" -eq 3 ]; then
    log "Keys and repo are in place and the app was relaunched, but it could not be verified (VM unreachable during the health check). The next run will verify."
    log "===== watchdog run finished (full recovery relaunched, unverified) ====="
    exit 1
fi
if [ "${rc}" -eq 0 ]; then
    notify "✅ Full recovery succeeded on ${MACHINE} (VM appears to have been rebooted/wiped; group key and app were both re-established)."
    set_state "up"
    log "===== watchdog run finished (recovered - full) ====="
    exit 0
fi
 
# ---------------------------------------------------------------------------
# C) Truly unrecoverable from here - alert ONCE, then retry only every
#    RETRY_MIN_GAP seconds instead of every minute.
# ---------------------------------------------------------------------------
if [ "${previous_state}" != "failed" ]; then
    notify "🚨 AUTOMATIC RECOVERY FAILED on ${MACHINE}. Neither the group key nor the default key work, or the redeploy itself failed. This needs a human."
fi
set_state "failed"
log "===== watchdog run finished (unrecoverable) ====="
exit 1
