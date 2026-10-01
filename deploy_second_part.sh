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
    log "Installing dependencies (torch is the slow one - pulling the small CPU-only build instead of the ~2GB default)..."
    remote "cd ${REPO_NAME} && source venv/bin/activate && \
        (pip install -q --index-url https://download.pytorch.org/whl/cpu torch || pip install -q torch) && \
        pip install -q -r requirements.txt && \
        echo '${REQS_HASH}' > ${REPO_NAME}/.deps_hash"
fi
 
# ---------------------------------------------------------------------------
# 4. Launch the app, recording its PID so future runs (and any recovery
#    script) can find and manage it.
# ---------------------------------------------------------------------------
log "Starting the app..."
remote "cd ${REPO_NAME} && nohup venv/bin/python3 app.py > log.txt 2>&1 & echo \$! > ${REPO_NAME}/app.pid"
push_rollback "${RSH[*]} 'if [ -f ${REPO_NAME}/app.pid ]; then kill \$(cat ${REPO_NAME}/app.pid) 2>/dev/null; rm -f ${REPO_NAME}/app.pid; fi' || true"
 
# ---------------------------------------------------------------------------
# 5. Health check: give it a couple seconds, then confirm the process is
#    actually still alive (not just that `nohup ... &` returned, which is
#    all the original script checked - i.e. not at all).
# ---------------------------------------------------------------------------
sleep 2
if ! remote "kill -0 \$(cat ${REPO_NAME}/app.pid) 2>/dev/null"; then
    err "App process died immediately after launch. Last lines of log.txt:"
    remote "tail -n 20 ${REPO_NAME}/log.txt" || true
    false   # trigger rollback via ERR trap
fi
 
log "App is running on the VM (pid file: ${REPO_NAME}/app.pid)."