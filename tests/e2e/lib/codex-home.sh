#!/usr/bin/env bash
# Per-run Codex home for ExoMonad E2E harnesses.
#
# ExoMonad seeds Codex hook trust by rewriting the *user* Codex config that
# `codex_config::codex_user_config_path()` resolves: `$CODEX_HOME/config.toml`
# when the variable is set, `~/.codex/config.toml` when it is not. A harness
# that forgets to export CODEX_HOME therefore edits the operator's real config
# mid-run; a harness that "fixes" the symptom by copying the host config in and
# restoring it afterwards corrupts it instead, because ExoMonad rewrites it in
# place and a restore cannot run if the run dies.
#
# The only safe contract is the one implemented here: create a fresh Codex home
# per run, point the whole run at it before any ExoMonad process starts, and
# copy in only the documented authentication artifacts a live `codex` process
# needs. The host `config.toml` is never read, copied, moved, or restored --
# `e2e_codex_assert_host_config_unchanged` proves that by digest.
#
# Source this file directly from tests/e2e/<name>/run.sh, or through
# tests/e2e/lib/harness.sh, which calls e2e_isolate_codex_home from
# e2e_create_work_dir so a scenario cannot forget to.

if [[ -n "${E2E_CODEX_HOME_HELPER_LOADED:-}" ]]; then
    return 0
fi
E2E_CODEX_HOME_HELPER_LOADED=1

# The only host artifacts a live `codex` process needs in order to
# authenticate. Deliberately narrow, and deliberately *not* config.toml:
# rewriting that file is exactly what this helper exists to contain.
readonly E2E_CODEX_AUTH_ARTIFACTS=(
    auth.json
    installation_id
)

# Directory name of the per-run Codex home inside the run's work dir.
readonly E2E_CODEX_HOME_DIRNAME="codex-home"

e2e_codex_log() {
    printf '  %s\n' "$*"
}

e2e_codex_fail() {
    printf 'ERROR: %s\n' "$*" >&2
    return 1
}

# Absolute path of the host Codex home, which is also the fallback
# `codex_user_config_path()` uses when CODEX_HOME is unset.
e2e_codex_host_home() {
    printf '%s\n' "$HOME/.codex"
}

# Absolute path of the host Codex user config, i.e. the file an unisolated run
# would rewrite.
e2e_codex_host_config() {
    printf '%s\n' "$(e2e_codex_host_home)/config.toml"
}

# Content digest of a file, or the literal "absent" when it does not exist.
# A missing host config is a legitimate starting state, so it is distinguishable
# from an empty one.
e2e_codex_file_digest() {
    local path="${1:?path required}"
    if [[ ! -e "$path" ]]; then
        printf 'absent\n'
        return 0
    fi
    if [[ -d "$path" ]]; then
        e2e_codex_fail "expected a file at $path but found a directory"
        return 1
    fi
    sha256sum -- "$path" | cut -d' ' -f1
}

# Create the per-run Codex home under WORK_DIR, export CODEX_HOME to it, and
# record the host config digest for the sentinel check.
#
# This is the one helper every harness that can generate Codex configuration
# must call before it starts `exomonad serve` or `exomonad init`. It is
# idempotent, so a harness that also calls it explicitly stays correct.
e2e_isolate_codex_home() {
    if [[ -z "${WORK_DIR:-}" ]]; then
        e2e_codex_fail "e2e_isolate_codex_home requires WORK_DIR; call e2e_create_work_dir first"
        return 1
    fi
    if [[ -z "${HOME:-}" ]]; then
        e2e_codex_fail "e2e_isolate_codex_home requires HOME to locate the host Codex artifacts"
        return 1
    fi

    E2E_CODEX_HOST_CONFIG="$(e2e_codex_host_config)"
    E2E_CODEX_HOST_CONFIG_DIGEST="$(e2e_codex_file_digest "$E2E_CODEX_HOST_CONFIG")"
    export E2E_CODEX_HOST_CONFIG E2E_CODEX_HOST_CONFIG_DIGEST

    CODEX_HOME="$WORK_DIR/$E2E_CODEX_HOME_DIRNAME"
    mkdir -p "$CODEX_HOME"
    export CODEX_HOME
    e2e_codex_log "Codex home isolated to $CODEX_HOME (host config $E2E_CODEX_HOST_CONFIG_DIGEST)"
}

# Copy the documented authentication artifacts a *live* Codex process needs out
# of the host home and into the isolated one. Harnesses that substitute a fake
# `codex` binary must not call this: a fake needs no credentials, and copying
# real ones into a run's work dir is needless exposure.
e2e_copy_codex_auth() {
    local host_home artifact copied=0
    if [[ -z "${CODEX_HOME:-}" ]]; then
        e2e_codex_fail "e2e_copy_codex_auth requires an isolated CODEX_HOME; call e2e_isolate_codex_home first"
        return 1
    fi
    if [[ -z "${HOME:-}" ]]; then
        e2e_codex_fail "e2e_copy_codex_auth requires HOME to locate the host Codex artifacts"
        return 1
    fi
    host_home="$(e2e_codex_host_home)"
    for artifact in "${E2E_CODEX_AUTH_ARTIFACTS[@]}"; do
        if [[ -f "$host_home/$artifact" ]]; then
            cp -p "$host_home/$artifact" "$CODEX_HOME/$artifact"
            copied=$((copied + 1))
        fi
    done
    e2e_codex_log "Copied $copied host auth artifact(s) into $CODEX_HOME: ${E2E_CODEX_AUTH_ARTIFACTS[*]}"
    if (( copied == 0 )); then
        e2e_codex_fail "no Codex auth artifact found in $host_home; a live Codex E2E cannot authenticate"
        return 1
    fi
}

# True once this shell has isolated a Codex home. Teardown uses it so a run that
# failed before it got that far is not reported as a host-config leak.
e2e_codex_isolation_active() {
    [[ -n "${E2E_CODEX_HOST_CONFIG_DIGEST:-}" && -n "${WORK_DIR:-}" ]]
}

# Fail unless CODEX_HOME is the per-run home beneath WORK_DIR. Guards against a
# harness that exports a path outside its own run, which would be a different
# kind of leak: two runs sharing one Codex home race on the trust lock.
e2e_codex_assert_home_is_run_scoped() {
    if ! e2e_codex_isolation_active; then
        e2e_codex_log "Codex home was never isolated; nothing to assert"
        return 0
    fi
    if [[ -z "${CODEX_HOME:-}" ]]; then
        e2e_codex_fail "CODEX_HOME is unset; this run would rewrite the host Codex config"
        return 1
    fi
    case "$CODEX_HOME" in
        "$WORK_DIR"/*) ;;
        *)
            e2e_codex_fail "CODEX_HOME ($CODEX_HOME) is not beneath WORK_DIR ($WORK_DIR)"
            return 1
            ;;
    esac
    e2e_codex_assert_host_config_unchanged
}

# Sentinel: the host Codex user config must be byte-for-byte what it was before
# the run. This is the property the whole contract exists to protect, so it is
# asserted at teardown rather than trusted.
e2e_codex_assert_host_config_unchanged() {
    if [[ -z "${E2E_CODEX_HOST_CONFIG:-}" || -z "${E2E_CODEX_HOST_CONFIG_DIGEST:-}" ]]; then
        e2e_codex_fail "no host Codex config digest recorded; call e2e_isolate_codex_home first"
        return 1
    fi
    local current
    current="$(e2e_codex_file_digest "$E2E_CODEX_HOST_CONFIG")" || return 1
    if [[ "$current" != "$E2E_CODEX_HOST_CONFIG_DIGEST" ]]; then
        e2e_codex_fail "host Codex config $E2E_CODEX_HOST_CONFIG changed during the run ($E2E_CODEX_HOST_CONFIG_DIGEST -> $current)"
        return 1
    fi
    e2e_codex_log "Host Codex config unchanged: $E2E_CODEX_HOST_CONFIG ($current)"
}

# Remove the isolated Codex home. Teardown only ever deletes state this run
# created; KEEP_E2E_WORKDIR=1 keeps it, exactly as it keeps the rest of the
# work dir, so a preserved run can still be inspected.
e2e_codex_remove_isolated_home() {
    if [[ "${KEEP_E2E_WORKDIR:-0}" == "1" ]]; then
        e2e_codex_log "Keeping isolated Codex home: ${CODEX_HOME:-unset}"
        return 0
    fi
    if [[ -n "${CODEX_HOME:-}" && -d "$CODEX_HOME" ]]; then
        rm -rf "$CODEX_HOME"
        e2e_codex_log "Removed isolated Codex home $CODEX_HOME"
    fi
}
