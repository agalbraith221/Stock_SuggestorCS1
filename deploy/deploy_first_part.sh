#!/bin/bash
#
# deploy_first_part.sh
#
# Sets up a group-only SSH key on the VM and pushes the repo to it.
#
# Safe to re-run:
#   - If tmp/mykey already exists AND works, the key-setup phase is skipped
#     entirely (no-op) and we jump straight to the repo copy.
#   - tmp/mykey is NEVER deleted or overwritten while it might still be the
#     only way into the VM. The new key is generated as tmp/mykey.new, proven
#     to work, and only then renamed to tmp/mykey. (An earlier version deleted
#     mykey up front; one brief network blip during that window locked us out.)
#   - We never assume the default student-admin_key is still authorized - we
#     check that it works (with retries) before touching anything.
#   - authorized_keys is only ever OVERWRITTEN after the new key has been
#     verified. Until then we only APPEND, and a failure restores the backup.
#   - After the lock-down is confirmed the rollback stack is cleared: from that
#     point on the new key is the only way in, so later failures (git clone,
#     file copy) must not undo anything. A re-run simply continues from there.
#   - Local git clone becomes "git pull" if the repo dir already exists.
#
set -uo pipefail
 
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"
 
# Where to look for the default key WPI issued, in addition to tmp/ itself.
STUDENT_ADMIN_KEY_PATH="${STUDENT_ADMIN_KEY_PATH:-$HOME/Desktop}"
REPO_URL="https://github.com/agalbraith221/Stock_SuggestorCS1.git"
REPO_NAME="Stock_SuggestorCS1"
BRANCH="${BRANCH:-CS2}"
 
trap 'on_error $LINENO' ERR
 
# ---------------------------------------------------------------------------
# 0. Known-hosts hygiene (safe to repeat - if there's nothing to remove,
#    ssh-keygen -R just no-ops)
# ---------------------------------------------------------------------------
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "[${MACHINE}]:${PORT}" >/dev/null 2>&1 || true
 
mkdir -p tmp
chmod 700 tmp
cd tmp
 
# ---------------------------------------------------------------------------
# 1. Recover from an interrupted earlier run: if a previous run locked the VM
#    down to mykey.new but was killed before renaming it, promote it now.
# ---------------------------------------------------------------------------
if [ -f mykey.new ] && [ -f mykey.new.pub ] && remote_key_works_retry "mykey.new" 2 3; then
    log "Found a verified key from an interrupted earlier run (mykey.new) - promoting it to mykey."
    mv -f mykey.new mykey
    mv -f mykey.new.pub mykey.pub
fi
 
# ---------------------------------------------------------------------------
# 2. Fast path: does the existing group key work? (Retried, because one failed
#    login can just be a network blip and must not trigger a key rebuild.)
# ---------------------------------------------------------------------------
KEY_ALREADY_SET_UP=0
if remote_key_works_retry "mykey" 3 5; then
    log "Existing group key (tmp/mykey) already works - skipping key setup."
    KEY_ALREADY_SET_UP=1
fi
 
if [ "${KEY_ALREADY_SET_UP}" -eq 0 ]; then
 
    # -----------------------------------------------------------------
    # 3. mykey does not work (after retries), so we need the DEFAULT key
    #    to get in. mykey is deliberately not a candidate here - if it
    #    were working we would not be in this branch, and we must never
    #    replace a key we are also using to administer the VM.
    # -----------------------------------------------------------------
    cp "${STUDENT_ADMIN_KEY_PATH}"/student-admin_key* . 2>/dev/null || true
    [ -f student-admin_key ] && chmod 600 student-admin_key*
 
    WORKING_KEY=""
    if remote_key_works_retry "student-admin_key" 3 5; then
        WORKING_KEY="student-admin_key"
    fi
    if [ -z "${WORKING_KEY}" ]; then
        err "Neither tmp/mykey nor tmp/student-admin_key can log in to ${MACHINE}:${PORT}."
        err "Nothing was changed on the VM, and no key file was modified or deleted."
        err "Put a working key at tmp/mykey, or the default key at tmp/student-admin_key, and try again."
        exit 1
    fi
    log "Using '${WORKING_KEY}' to administer the VM for this run."
 
    # -----------------------------------------------------------------
    # 4. Generate the new group keypair under a TEMPORARY name. mykey
    #    itself is not touched until the new key is proven.
    # -----------------------------------------------------------------
    rm -f mykey.new mykey.new.pub
    ssh-keygen -f mykey.new -t ed25519 -N "" -q
    push_rollback "rm -f '${PWD}/mykey.new' '${PWD}/mykey.new.pub'"
 
    # -----------------------------------------------------------------
    # 5. Back up the remote authorized_keys, then APPEND (not overwrite)
    #    the new public key. Register the rollback before we act.
    # -----------------------------------------------------------------
    REMOTE_SSH=(ssh -i "${WORKING_KEY}" -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}")
    BACKUP_NAME=".ssh/authorized_keys.bak.$(date +%s)"
 
    "${REMOTE_SSH[@]}" "cp ~/.ssh/authorized_keys ~/${BACKUP_NAME}"
    push_rollback "ssh -i '${WORKING_KEY}' -p ${PORT} ${SSH_OPTS[*]} student-admin@${MACHINE} 'mv ~/${BACKUP_NAME} ~/.ssh/authorized_keys' || true"
 
    log "Appending the new group key to authorized_keys on the VM..."
    cat mykey.new.pub | "${REMOTE_SSH[@]}" "cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
 
    # -----------------------------------------------------------------
    # 6. Verify the NEW key actually works before we lock anything down.
    #    If this fails, the ERR trap fires -> rollback restores the
    #    original authorized_keys via the key we know still works.
    # -----------------------------------------------------------------
    if ! remote_key_works_retry "mykey.new" 3 3; then
        err "New key was appended but does not authenticate. Rolling back."
        false   # trigger the ERR trap deliberately
    fi
    log "New group key verified working."
 
    # -----------------------------------------------------------------
    # 7. Only now, with the new key proven to work, replace
    #    authorized_keys so it's the ONLY key on the box (per the
    #    assignment: "only your group has access to it"). The remote
    #    backup from step 5 still exists as a safety net.
    # -----------------------------------------------------------------
    log "Locking the VM down to the group key only..."
    cat mykey.new.pub | ssh -i mykey.new -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}" \
        "cat > ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
 
    if ! remote_key_works_retry "mykey.new" 3 3; then
        err "Lost access immediately after locking down - restoring backup."
        ssh -i "${WORKING_KEY}" -p "${PORT}" "${SSH_OPTS[@]}" "student-admin@${MACHINE}" \
            "mv ~/${BACKUP_NAME} ~/.ssh/authorized_keys" || true
        false
    fi
    log "Lock-down confirmed. Only the group key is authorized now."
 
    # -----------------------------------------------------------------
    # 8. POINT OF NO RETURN. The new key is the only way in, so install
    #    it as mykey and forget the rollback steps: undoing them now
    #    (deleting the key, restoring an old authorized_keys we can no
    #    longer reach) could only make things worse.
    # -----------------------------------------------------------------
    mv -f mykey.new mykey
    mv -f mykey.new.pub mykey.pub
    disarm_rollback
    chmod 600 mykey mykey.pub 2>/dev/null || true
    if ! remote_key_works_retry "mykey" 3 3; then
        err "Installed tmp/mykey but it does not authenticate - this should be impossible. Manual check needed."
        exit 1
    fi
fi
 
chmod 600 mykey mykey.pub 2>/dev/null || true
 
# ---------------------------------------------------------------------------
# 9. Clone (or, on a re-run, update) the repo locally
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
# 10. Copy the repo to the VM. rsync (if available) makes this idempotent
#     and only transfers what changed; fall back to scp -r otherwise.
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
