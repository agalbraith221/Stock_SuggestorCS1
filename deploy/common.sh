#!/bin/bash
# common.sh
#
# Shared helpers sourced by the other deployment scripts. Not meant to be
# run directly.
#
# Provides:
#   - log / err                 : timestamped output
#   - push_rollback CMD         : register an "undo" command for the current run
#   - run_rollback              : execute all registered undo commands, most
#                                 recently added first (LIFO), then clear them
#   - disarm_rollback           : forget all registered undo commands (used once
#                                 a run passes its point of no return)
#   - on_error $LINENO          : trap target -> logs, rolls back, exits 1
#   - remote_key_works KEY      : true/false check, no side effects
#   - remote_key_works_retry    : same, but tolerates brief network blips
#   - vm_port_open / vm_reachable_retry : is the VM's SSH port answering at all?
#   - find_working_key          : first candidate key that logs in
#
# IMPORTANT (lesson learned from a real outage): a single failed login is NOT
# proof that a key is dead. A momentary network problem looks identical to a
# revoked key, so every decision that can destroy or replace a key goes through
# the *_retry helpers below.
 
set -uo pipefail
 
PORT="${PORT:-22017}"
MACHINE="${MACHINE:-paffenroth-23.dyn.wpi.edu}"
# If this host's DNS cannot resolve the VM's name (this has happened on the
# WPI login nodes: "Could not resolve hostname"), fall back to the VM host's
# IP address so a DNS hiccup is not mistaken for the VM being down. Host key
# checking is already off for these connections, so the IP works as well as
# the name. Verify the address with:  getent hosts paffenroth-23.dyn.wpi.edu
# Set MACHINE_IP_FALLBACK="" to disable.
MACHINE_IP_FALLBACK="${MACHINE_IP_FALLBACK-130.215.182.120}"
MACHINE_VIA_FALLBACK=0
if command -v getent >/dev/null 2>&1 && [ -n "${MACHINE_IP_FALLBACK}" ] \
   && ! getent hosts "${MACHINE}" >/dev/null 2>&1; then
    MACHINE_NAME_ORIGINAL="${MACHINE}"
    MACHINE="${MACHINE_IP_FALLBACK}"
    MACHINE_VIA_FALLBACK=1
fi
# IdentitiesOnly: use only the key given with -i, never whatever an ssh-agent
# happens to offer (avoids "Too many authentication failures").
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes -o IdentitiesOnly=yes)
 
log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
err()  { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
 
# ---------------------------------------------------------------------------
# Rollback stack
# ---------------------------------------------------------------------------
declare -a ROLLBACK_STACK=()
 
push_rollback() {
    ROLLBACK_STACK+=("$1")
}
 
# Call this once a run has passed its point of no return (for example, once the
# VM has been locked down to the new key). After that, "undoing" earlier steps
# would do more harm than good.
disarm_rollback() {
    ROLLBACK_STACK=()
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
 
# remote_key_works_retry KEYFILE [TRIES] [PAUSE_SECONDS]
# A key only counts as "not working" after every try has failed.
remote_key_works_retry() {
    local keyfile="$1" tries="${2:-3}" pause="${3:-5}" i
    [ -f "$keyfile" ] || return 1
    for (( i=1; i<=tries; i++ )); do
        if remote_key_works "$keyfile"; then
            return 0
        fi
        if [ "$i" -lt "$tries" ]; then
            sleep "$pause"
        fi
    done
    return 1
}
 
# vm_port_open -> 0 if the VM's SSH port accepts a TCP connection.
vm_port_open() {
    if command -v timeout >/dev/null 2>&1; then
        timeout 8 bash -c "exec 3<>/dev/tcp/${MACHINE}/${PORT}" >/dev/null 2>&1
    else
        nc -z -w 8 "${MACHINE}" "${PORT}" >/dev/null 2>&1
    fi
}
 
# vm_reachable_retry [TRIES] [PAUSE_SECONDS]
vm_reachable_retry() {
    local tries="${1:-3}" pause="${2:-5}" i
    for (( i=1; i<=tries; i++ )); do
        if vm_port_open; then
            return 0
        fi
        if [ "$i" -lt "$tries" ]; then
            sleep "$pause"
        fi
    done
    return 1
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
        if remote_key_works_retry "$k" 2 3; then
            printf '%s\n' "$k"
            return 0
        fi
    done
    return 1
