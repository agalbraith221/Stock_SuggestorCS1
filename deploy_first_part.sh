#!/bin/bash
#
# deploy_first_part.sh
#
# Sets up a group-only SSH key on the VM and pushes the repo to it.
#
# Safe to re-run:
#   - If tmp/mykey already exists AND already works, the key-setup phase
#     is skipped entirely (no-op) and we jump straight to the repo copy.
#   - We never assume the default student-admin_key is still authorized -
#     we probe for whatever key currently works before touching anything.
#   - authorized_keys is only ever OVERWRITTEN after we've verified the
#     new key can log in. Until that verification passes, we only ever
#     APPEND to authorized_keys, and a failure rolls that append back out,
#     leaving the original (default) key intact. That's what prevents the
#     VM from getting bricked.
#   - Local git clone becomes "git pull" if the repo dir already exists.
#
set -uo pipefail
 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
 
STUDENT_ADMIN_KEY_PATH="${STUDENT_ADMIN_KEY_PATH:-$HOME/Desktop}"
REPO_URL="https://github.com/agalbraith221/Stock_SuggestorCS1/tree/CS2.git"
REPO_NAME="Stock_SuggestorCS1"
BRANCH="${BRANCH:-CS2}"
 
trap 'on_error $LINENO' ERR
 
SSH_AGENT_STARTED=0
start_agent_if_needed() {
    if [ -z "${SSH_AUTH_SOCK:-}" ]; then
        eval "$(ssh-agent -s)" >/dev/null
        SSH_AGENT_STARTED=1
    fi
}
cleanup_agent() {
    if [ "${SSH_AGENT_STARTED}" -eq 1 ] && [ -n "${SSH_AGENT_PID:-}" ]; then
        kill "${SSH_AGENT_PID}" >/dev/null 2>&1 || true
    fi
}
trap cleanup_agent EXIT
 
# ---------------------------------------------------------------------------
# 0. Known-hosts hygiene (safe to repeat - if there's nothing to remove,
#    ssh-keygen -R just no-ops)
# ---------------------------------------------------------------------------
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "[${MACHINE}]:${PORT}" >/dev/null 2>&1 || true
 
mkdir -p tmp
chmod 700 tmp
cd tmp
 
# ---------------------------------------------------------------------------
# 1. Fast path: does a key from a previous successful run already work?
#    If so, don't touch authorized_keys at all this run.
# ---------------------------------------------------------------------------
KEY_ALREADY_SET_UP=0
if remote_key_works "mykey"; then
    log "Existing group key (tmp/mykey) already works - skipping key setup."
    KEY_ALREADY_SET_UP=1
fi
 
if [ "${KEY_ALREADY_SET_UP}" -eq 0 ]; then
 
    # -----------------------------------------------------------------
    # 2. Locate whichever credential currently grants access: the
    #    default key WPI issued, or (if a previous run got partway
    #    through) our own mykey.
    # -----------------------------------------------------------------
    cp "${STUDENT_ADMIN_KEY_PATH}"/student-admin_key* . 2>/dev/null || true
    [ -f student-admin_key ] && chmod 600 student-admin_key*
 
    WORKING_KEY="$(find_working_key student-admin_key mykey || true)"
    if [ -z "${WORKING_KEY}" ]; then
        err "Neither the default student-admin_key nor an existing mykey can log in."
        err "Nothing was changed on the VM. Check ${STUDENT_ADMIN_KEY_PATH}/student-admin_key and try again."
        exit 1
    fi
    log "Using '${WORKING_KEY}' to administer the VM for this run."
 
    # -----------------------------------------------------------------
    # 3. Generate our own group keypair (fresh each time key setup runs)
    # -----------------------------------------------------------------
    rm -f mykey mykey.pub
    ssh-keygen -f mykey -t ed25519 -N "" -q
    push_rollback "rm -f '${PWD}/mykey' '${PWD}/mykey.pub'"
 
    start_agent_if_needed
    ssh-add mykey >/dev/null
 
    # -----------------------------------------------------------------
    # 4. Back up the remote authorized_keys, then APPEND (not overwrite)
    #    our new public key. Register the rollback before we act.
    # -----------------------------------------------------------------
    REMOTE_SSH=(ssh -i "${WORKING_KEY}" -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}")
    BACKUP_NAME=".ssh/authorized_keys.bak.$(date +%s)"
 
    "${REMOTE_SSH[@]}" "cp ~/.ssh/authorized_keys ~/${BACKUP_NAME}"
    push_rollback "ssh -i '${WORKING_KEY}' -p ${PORT} ${SSH_OPTS[*]} student-admin@${MACHINE} 'mv ~/${BACKUP_NAME} ~/.ssh/authorized_keys' || true"
 
    log "Appending the new group key to authorized_keys on the VM..."
    cat mykey.pub | "${REMOTE_SSH[@]}" "cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
 
    # -----------------------------------------------------------------
    # 5. Verify the NEW key actually works before we lock anything down.
    #    If this fails, the ERR trap fires -> rollback restores the
    #    original authorized_keys via the key we know still works.
    # -----------------------------------------------------------------
    if ! remote_key_works "mykey"; then
        err "New key was appended but does not authenticate. Rolling back."
        false   # trigger the ERR trap deliberately
    fi
    log "New group key verified working."
 
    # -----------------------------------------------------------------
    # 6. Only now, with the new key proven to work, replace
    #    authorized_keys so it's the ONLY key on the box (per the
    #    assignment: "only your group has access to it"). The remote
    #    backup from step 4 still exists as a safety net if you ever
    #    need to manually restore it.
    # -----------------------------------------------------------------
    log "Locking the VM down to the group key only..."
    cat mykey.pub | ssh -i mykey -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}" \
        "cat > ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
 
    if ! remote_key_works "mykey"; then
        err "Lost access immediately after locking down - restoring backup."
        ssh -i "${WORKING_KEY}" -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}" \
            "mv ~/${BACKUP_NAME} ~/.ssh/authorized_keys" || true
        false
    fi
    log "Lock-down confirmed. Only the group key is authorized now."
fi
 
chmod 600 mykey mykey.pub 2>/dev/null || true
 
# ---------------------------------------------------------------------------
# 7. Clone (or, on a re-run, update) the repo locally
# ---------------------------------------------------------------------------
log "Syncing local clone to branch '${BRANCH}'..."
if [ -d "${REPO_NAME}/.git" ]; then
    git -C "${REPO_NAME}" fetch origin "${BRANCH}"
    git -C "${REPO_NAME}" checkout "${BRANCH}"
    git -C "${REPO_NAME}" merge --ff-only "origin/${BRANCH}"
else
    git clone -b "${BRANCH}" "${REPO_URL}" "${REPO_NAME}"
fi
 
# ---------------------------------------------------------------------------
# 8. Copy the repo to the VM. rsync (if available) makes this idempotent
#    and only transfers what changed; fall back to scp -r otherwise.
# ---------------------------------------------------------------------------
log "Copying ${REPO_NAME} to the VM..."
if command -v rsync >/dev/null 2>&1; then
    rsync -az -e "ssh -i mykey -p ${PORT} ${SSH_OPTS[*]}" \
        --exclude venv --exclude '__pycache__' \
        "${REPO_NAME}/" "student-admin@${MACHINE}:~/${REPO_NAME}/"
else
    scp -i mykey -P "${PORT}" "${SSH_OPTS[@]}" -r "${REPO_NAME}" "student-admin@${MACHINE}:~/"
fi
 
log "Part 1 complete: group key is in place and the repo is on the VM."