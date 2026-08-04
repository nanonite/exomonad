#!/usr/bin/env bash

set -euo pipefail

export LC_ALL=C

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_PATH="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'USAGE'
Usage: check-repo-integrity.sh [--root PATH]
       check-repo-integrity.sh --self-test

Verify the ExoMonad repository without modifying Git state. With no options,
the repository root is the parent of this script's directory.
USAGE
}

canonical_dir() {
    local directory="$1"

    [[ -d "$directory" ]] || die "directory does not exist: $directory"
    (cd -- "$directory" && pwd -P)
}

# Repository-selection variables are intentionally removed for every Git
# subprocess. A current_dir or -C path is not sufficient protection against
# inherited Git environment state.
git_cmd() {
    env \
        -u GIT_DIR \
        -u GIT_WORK_TREE \
        -u GIT_INDEX_FILE \
        -u GIT_COMMON_DIR \
        -u GIT_OBJECT_DIRECTORY \
        -u GIT_ALTERNATE_OBJECT_DIRECTORIES \
        git "$@"
}

contains_line() {
    local needle="$1"
    local values="$2"
    local value

    while IFS= read -r value; do
        if [[ "$value" == "$needle" ]]; then
            return 0
        fi
    done <<< "$values"

    return 1
}

tracked_path_exists() {
    local repository_root="$1"
    local pathspec="$2"
    local tracked_paths

    tracked_paths="$(git_cmd -C "$repository_root" ls-files -- "$pathspec" 2>/dev/null || true)"
    [[ -n "$tracked_paths" ]]
}

check_sentinel() {
    local repository_root="$1"
    local sentinel="$2"

    [[ -f "$repository_root/$sentinel" ]] || \
        die "stable sentinel is missing from the filesystem: $sentinel"

    git_cmd -C "$repository_root" ls-files --error-unmatch -- "$sentinel" \
        >/dev/null 2>&1 || die "stable sentinel is missing from the tracked index: $sentinel"
}

check_repository() {
    local repository_root="$1"
    local resolved_root
    local local_names
    local local_emails

    repository_root="$(canonical_dir "$repository_root")"

    resolved_root="$(git_cmd -C "$repository_root" rev-parse --show-toplevel 2>/dev/null)" || \
        die "cannot resolve the Git workspace root from $repository_root"
    resolved_root="$(canonical_dir "$resolved_root")"
    [[ "$resolved_root" == "$repository_root" ]] || die \
        "Git workspace root mismatch: expected $repository_root, got $resolved_root"

    check_sentinel "$repository_root" "Cargo.toml"
    check_sentinel "$repository_root" "rust/exomonad-core/src/lib.rs"

    # These structural paths distinguish the ExoMonad tree from a tiny
    # synthetic fixture without relying on a repository-wide file count.
    tracked_path_exists "$repository_root" "justfile" || \
        die "tracked-file sanity check failed: justfile is absent"
    tracked_path_exists "$repository_root" "rust/" || \
        die "tracked-file sanity check failed: rust/ is absent"
    tracked_path_exists "$repository_root" "scripts/" || \
        die "tracked-file sanity check failed: scripts/ is absent"

    local_names="$(git_cmd -C "$repository_root" config --local --get-all user.name 2>/dev/null || true)"
    if contains_line "CI Test" "$local_names"; then
        die "local Git config contains the fixture identity user.name=CI Test"
    fi

    local_emails="$(git_cmd -C "$repository_root" config --local --get-all user.email 2>/dev/null || true)"
    if contains_line "ci@test.local" "$local_emails"; then
        die "local Git config contains the fixture identity user.email=ci@test.local"
    fi
}

create_self_test_repository() {
    local repository_root="$1"

    mkdir -p \
        "$repository_root/rust/exomonad-core/src" \
        "$repository_root/scripts"
    printf '[workspace]\nmembers = []\n' >"$repository_root/Cargo.toml"
    printf 'pub fn self_test_sentinel() {}\n' >"$repository_root/rust/exomonad-core/src/lib.rs"
    printf 'default:\n    @true\n' >"$repository_root/justfile"
    printf '#!/usr/bin/env bash\n' >"$repository_root/scripts/placeholder.sh"

    git_cmd -C "$repository_root" init --quiet
    git_cmd -C "$repository_root" -c user.name=ExoMonad -c user.email=exomonad@example.invalid \
        add -- Cargo.toml justfile rust/exomonad-core/src/lib.rs scripts/placeholder.sh
    git_cmd -C "$repository_root" -c user.name=ExoMonad -c user.email=exomonad@example.invalid \
        commit --quiet -m "self-test fixture"
}

expect_failure() {
    local description="$1"
    shift

    if "$@" >/dev/null 2>&1; then
        die "self-test expected failure did not occur: $description"
    fi
    printf 'self-test: rejected %s\n' "$description"
}

run_self_test() {
    local temporary_root
    local healthy_root
    local external_root
    local collapsed_root
    local suspicious_root

    temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/exomonad-integrity.XXXXXX")"
    trap 'rm -rf -- "$temporary_root"' EXIT

    healthy_root="$temporary_root/healthy"
    external_root="$temporary_root/external"
    collapsed_root="$temporary_root/collapsed"
    suspicious_root="$temporary_root/suspicious"

    create_self_test_repository "$healthy_root"
    create_self_test_repository "$external_root"

    if ! GIT_DIR="$external_root/.git" \
        GIT_WORK_TREE="$external_root" \
        GIT_INDEX_FILE="$external_root/.git/index" \
        GIT_COMMON_DIR="$external_root/.git" \
        GIT_OBJECT_DIRECTORY="$external_root/.git/objects" \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="$external_root/.git/objects" \
        "$SCRIPT_PATH" --root "$healthy_root" >/dev/null; then
        die "self-test healthy repository was rejected under hostile GIT_* state"
    fi
    printf 'self-test: accepted healthy repository with hostile GIT_* state\n'

    cp -a -- "$healthy_root" "$collapsed_root"
    rm -f -- "$collapsed_root/rust/exomonad-core/src/lib.rs"
    expect_failure "repository with a missing stable sentinel" \
        "$SCRIPT_PATH" --root "$collapsed_root"

    cp -a -- "$healthy_root" "$suspicious_root"
    git_cmd -C "$suspicious_root" config --local user.name "CI Test"
    git_cmd -C "$suspicious_root" config --local user.email "ci@test.local"
    expect_failure "repository with the fixture identity" \
        "$SCRIPT_PATH" --root "$suspicious_root"

    trap - EXIT
    printf 'Repository integrity self-test passed.\n'
}

main() {
    local mode="check"
    local explicit_root=""

    while (($# > 0)); do
        case "$1" in
            --help|-h)
                usage
                return 0
                ;;
            --self-test)
                [[ "$mode" == "check" ]] || die "--self-test may only be specified once"
                mode="self-test"
                ;;
            --root)
                [[ "$mode" == "check" ]] || die "--root cannot be combined with --self-test"
                (($# >= 2)) || die "--root requires a path"
                explicit_root="$2"
                shift
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
        shift
    done

    if [[ "$mode" == "self-test" ]]; then
        run_self_test
        return 0
    fi

    local expected_root
    if [[ -n "$explicit_root" ]]; then
        expected_root="$(canonical_dir "$explicit_root")"
    else
        expected_root="$(canonical_dir "$SCRIPT_DIR/..")"
    fi

    check_repository "$expected_root"
    printf 'Repository integrity check passed: %s\n' "$expected_root"
}

main "$@"
