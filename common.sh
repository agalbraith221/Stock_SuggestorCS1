#!/bin/bash
# common.sh
#
# Shared helpers sourced by the other deployment scripts. Not meant to be
# run directly.
#
# Provides:
#   - log / err            : timestamped output
#   - push_rollback CMD     : register an "undo" command for the current run
#   - run_rollback          : execute all registered undo commands, most
#                              recently added first (LIFO), then clear them
#   - on_error $LINENO      : trap target -> logs, rolls back, exits 1
#   - ssh_as / scp_as       : ssh/scp using whichever identity key
#                              (default key vs. our generated "mykey")
#                              currently grants access, so re-running the
#                              scripts never assumes a key that may no
#                              longer be authorized
#   - remote_key_works KEY  : true/false check, no side effects
 
set -uo pipefail
 
PORT="${PORT:-22017}"
MACHINE="${MACHINE:-paffenroth-23.dyn.wpi.edu}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes)
 
log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
err()  { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
 
# ---------------------------------------------------------------------------
# Rollback stack
# ---------------------------------------------------------------------------
declare -a ROLLBACK_STACK=()
 
push_rollback() {
    ROLLBACK_STACK+=("$1")
}
 
run_rollback() {
    if [ "${#ROLLBACK_STACK[@]}" -eq 0 ]; then
        log "Nothing to undo."
        return 0
    fi
    err "Undoing ${#ROLLBACK_STACK[@]} step(s) from this run..."
    for (( idx=${#ROLLBACK_STACK[@]}-1 ; idx>=0 ; idx-- )); do
        log "  undo: ${ROLLBACK_STACK[$idx]}"
        eval "${ROLLBACK_STACK[$idx]}" \
            || err "    (that undo step failed too - you may need to check manually)"
    done
    ROLLBACK_STACK=()
}
 
# Register with: trap 'on_error $LINENO' ERR
on_error() {
    local line="$1"
    err "Command failed near line ${line}."
    run_rollback
    exit 1
}
 
# ---------------------------------------------------------------------------
# Key discovery - so scripts never assume "the default key still works"
# ---------------------------------------------------------------------------
 
# remote_key_works KEYFILE -> 0 if we can log in with it, 1 otherwise.
remote_key_works() {
    local keyfile="$1"
    [ -f "$keyfile" ] || return 1
    ssh -i "$keyfile" -p "${PORT}" "${SSH_OPTS[@]}" \
        "student-admin@${MACHINE}" "true" >/dev/null 2>&1
}
 
# Portable sha256 of a file: Linux has sha256sum, macOS has shasum -a 256.
sha256_of_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}
 
# find_working_key CANDIDATE... -> prints the path of the first candidate
# key that successfully authenticates, or nothing (and returns 1) if none do.
find_working_key() {
    local k
    for k in "$@"; do
        if remote_key_works "$k"; then
            printf '%s\n' "$k"
            return 0
        fi
    done
    return 1
}