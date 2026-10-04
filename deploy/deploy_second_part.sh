#!/bin/bash
#
# deploy_second_part.sh
#
# Builds the venv on the VM (if needed) and (re)starts the app.
#
# Safe to re-run:
#   - venv creation / pip install are naturally idempotent, but we still
#     check whether the venv already existed BEFORE this run, so that if
#     something later fails we only remove a venv we created this run -
#     we never delete one that was already there and working.
#   - The app is tracked via a pidfile on the VM. Before starting a new
#     copy we stop any old one first, so re-running never leaves two
#     copies of the app fighting over the same port.
#   - After starting, we health-check the process is actually alive; if
#     not, we roll back (remove the venv we just built, if any) and exit
#     non-zero instead of silently leaving a broken deployment.
#   - If the health checks cannot reach the VM at all (DNS/network trouble),
#     that is NOT treated as a dead app: nothing is rolled back (the app may
#     be running fine) and the script exits with status 3 = "launched, but
#     could not be verified". The watchdog verifies on its next run.
#
set -uo pipefail
 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
 
REPO_NAME="Stock_SuggestorCS1"
trap 'on_error $LINENO' ERR
 
cd tmp
 
if ! remote_key_works "mykey"; then
    err "tmp/mykey doesn't authenticate. Run deploy_first_part.sh first (or re-run it)."
    exit 1
fi
 
SSH_AGENT_STARTED=0
if [ -z "${SSH_AUTH_SOCK:-}" ]; then
    eval "$(ssh-agent -s)" >/dev/null
    SSH_AGENT_STARTED=1
fi
cleanup_agent() {
    if [ "${SSH_AGENT_STARTED}" -eq 1 ] && [ -n "${SSH_AGENT_PID:-}" ]; then
        kill "${SSH_AGENT_PID}" >/dev/null 2>&1 || true
    fi
}
trap cleanup_agent EXIT
ssh-add mykey >/dev/null
 
RSH=(ssh -i mykey -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}")
 
# Small helper: run a command on the VM, fail loudly (and trigger rollback
# via `set -e`/ERR trap) if it doesn't succeed - unlike the original
# script, which fired off every ssh command with no exit-code check at all.
remote() {
    "${RSH[@]}" "$@"
}
 
# ---------------------------------------------------------------------------
# 1. Confirm the repo is actually there (clear error instead of the old
#    script's silent `ls` that nothing downstream checked)
# ---------------------------------------------------------------------------
if ! remote "test -d ${REPO_NAME}"; then
    err "${REPO_NAME} isn't on the VM yet. Run deploy_first_part.sh first."
    exit 1
fi
 
# ---------------------------------------------------------------------------
# 2. Stop any already-running instance of the app before we do anything
#    else, so a re-run never ends up with two instances bound to the
#    same port / writing to the same log file.
# ---------------------------------------------------------------------------
log "Stopping any previous instance of the app..."
remote "
    if [ -f ${REPO_NAME}/app.pid ] && kill -0 \$(cat ${REPO_NAME}/app.pid) 2>/dev/null; then
        kill \$(cat ${REPO_NAME}/app.pid)
        sleep 1
    fi
    pkill -f '[v]env/bin/python3 app.py' 2>/dev/null || true
    sleep 1
    rm -f ${REPO_NAME}/app.pid
"
 
# ---------------------------------------------------------------------------
# 3. System package + venv. Track whether the venv pre-existed so we know
#    whether it's safe to remove during rollback.
# ---------------------------------------------------------------------------
remote "sudo apt install -qq -y python3-venv"
 
VENV_PREEXISTED=0
if remote "test -d ${REPO_NAME}/venv"; then
    VENV_PREEXISTED=1
    log "venv already exists on the VM - reusing it."
else
    log "Creating venv on the VM..."
    remote "cd ${REPO_NAME} && python3 -m venv venv"
    push_rollback "${RSH[*]} 'rm -rf ${REPO_NAME}/venv' || true"
fi
 
log "Installing/updating dependencies..."
 
LOCAL_REQS="${REPO_NAME}/requirements.txt"
REQS_HASH="$(sha256_of_file "${LOCAL_REQS}")"
 
NEED_INSTALL=1
if [ "${VENV_PREEXISTED}" -eq 1 ]; then
    REMOTE_HASH="$(remote "cat ${REPO_NAME}/.deps_hash 2>/dev/null" || true)"
    if [ "${REMOTE_HASH}" = "${REQS_HASH}" ]; then
        NEED_INSTALL=0
    fi
fi
 
if [ "${NEED_INSTALL}" -eq 0 ]; then
    log "requirements.txt unchanged since the last successful install - skipping pip install."
else
    log "Installing dependencies: step 1/2 - torch (trying the small CPU-only wheel first)..."
    if remote "cd ${REPO_NAME} && source venv/bin/activate && pip install -q --index-url https://download.pytorch.org/whl/cpu torch"; then
        log "  CPU-only torch wheel installed (fast path)."
    else
        log "  CPU-only index unreachable or failed - falling back to the default PyPI torch build."
        log "  (that build bundles CUDA and is much larger - this is likely why the install is slow)"
        remote "cd ${REPO_NAME} && source venv/bin/activate && pip install -q torch"
    fi
    log "Installing dependencies: step 2/2 - everything else in requirements.txt..."
    remote "cd ${REPO_NAME} && source venv/bin/activate && \
        pip install -q -r requirements.txt && \
        echo '${REQS_HASH}' > .deps_hash"
fi
 
# ---------------------------------------------------------------------------
# 4. Launch the app, recording its PID so future runs (and any recovery
#    script) can find and manage it. Each sub-step logs on its own line so
#    it's obvious where things are if this hangs or fails partway through.
# ---------------------------------------------------------------------------
log "Starting the app: step 1/4 - launching the process..."
remote "cd ${REPO_NAME}; nohup venv/bin/python3 app.py > log.txt 2>&1 < /dev/null & echo \$! > app.pid"
push_rollback "${RSH[*]} 'if [ -f ${REPO_NAME}/app.pid ]; then kill \$(cat ${REPO_NAME}/app.pid) 2>/dev/null; rm -f ${REPO_NAME}/app.pid; fi' || true"
 
APP_PID="$(remote "cat ${REPO_NAME}/app.pid" 2>/dev/null || true)"
log "Starting the app: step 2/4 - process launched with PID ${APP_PID:-unknown}."
 
# ---------------------------------------------------------------------------
# 5. Health check: poll instead of one blind sleep, so slow-starting apps
#    (e.g. still loading a model into memory) get a fair chance instead of
#    being declared dead after a fixed 2 seconds - but still fail fast if
#    the process dies outright.
# ---------------------------------------------------------------------------
HEALTH_CHECK_ATTEMPTS=10
HEALTH_CHECK_INTERVAL=3
log "Starting the app: step 3/4 - waiting for it to come up (checking every ${HEALTH_CHECK_INTERVAL}s, up to ${HEALTH_CHECK_ATTEMPTS} times)..."
 
# ssh exits with 255 when it cannot connect at all (DNS failure, timeout,
# network outage). That says nothing about the app, so it is counted
# separately from "the app is not running".
APP_ALIVE=0
CONN_FAILURES=0
for attempt in $(seq 1 "${HEALTH_CHECK_ATTEMPTS}"); do
    sleep "${HEALTH_CHECK_INTERVAL}"
    rc=0
    "${RSH[@]}" "kill -0 \$(cat ${REPO_NAME}/app.pid) 2>/dev/null" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        log "  check ${attempt}/${HEALTH_CHECK_ATTEMPTS}: still running."
        APP_ALIVE=1
        break
    elif [ "${rc}" -eq 255 ]; then
        CONN_FAILURES=$((CONN_FAILURES + 1))
        log "  check ${attempt}/${HEALTH_CHECK_ATTEMPTS}: could not reach the VM (network/DNS problem) - says nothing about the app."
    else
        log "  check ${attempt}/${HEALTH_CHECK_ATTEMPTS}: not running yet or already died."
    fi
done
 
log "Starting the app: step 4/4 - verifying final status..."
if [ "${APP_ALIVE}" -eq 0 ] && [ "${CONN_FAILURES}" -eq "${HEALTH_CHECK_ATTEMPTS}" ]; then
    # Not one health check got an answer, so we cannot say the app failed.
    # Do NOT roll back: that would kill an app that may be running fine.
    disarm_rollback
    err "App launched (PID ${APP_PID:-unknown}) but the VM could not be reached for any of the ${HEALTH_CHECK_ATTEMPTS} health checks."
    err "Leaving it running; the next watchdog run will verify it."
    exit 3
fi
if [ "${APP_ALIVE}" -eq 0 ]; then
    err "App process died (or never came up) after ${HEALTH_CHECK_ATTEMPTS} checks. Last lines of log.txt:"
    remote "tail -n 20 ${REPO_NAME}/log.txt" || true
    false   # trigger rollback via ERR trap
fi
 
log "App is running on the VM (pid file: ${REPO_NAME}/app.pid)."
