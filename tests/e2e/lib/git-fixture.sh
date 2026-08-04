#!/usr/bin/env bash
# Shared Git boundary for ExoMonad E2E fixture repositories.
#
# Source this file and call e2e_git_use_fixture_root before running Git.  The
# git function installed here removes repository-selection environment from
# every subprocess and rejects commands whose repository resolves outside the
# declared fixture tree.

if [[ -n "${E2E_GIT_FIXTURE_HELPER_LOADED:-}" ]]; then
    return 0
fi
E2E_GIT_FIXTURE_HELPER_LOADED=1

readonly E2E_GIT_SCRUBBED_VARIABLES=(
    GIT_DIR
    GIT_WORK_TREE
    GIT_INDEX_FILE
    GIT_COMMON_DIR
    GIT_OBJECT_DIRECTORY
    GIT_ALTERNATE_OBJECT_DIRECTORIES
)

e2e_git_scrubbed() {
    local -a scrubbed_env=()
    local variable

    for variable in "${E2E_GIT_SCRUBBED_VARIABLES[@]}"; do
        scrubbed_env+=(-u "$variable")
    done
    env "${scrubbed_env[@]}" git "$@"
}

e2e_git_canonical_path() {
    local path="$1"
    local parent

    if [[ -e "$path" ]]; then
        (cd -P -- "$path" && pwd)
        return
    fi

    parent="$(dirname -- "$path")"
    printf '%s/%s\n' "$(cd -P -- "$parent" && pwd)" "$(basename -- "$path")"
}

e2e_git_path_is_within() {
    local root="$1"
    local candidate="$2"
    [[ "$candidate" == "$root" || "$candidate" == "$root"/* ]]
}

e2e_git_assert_path_within_fixture() {
    local root="$1"
    local path="$2"
    local canonical_path

    canonical_path="$(e2e_git_canonical_path "$path")" || {
        printf 'ERROR: cannot resolve Git fixture path %s\n' "$path" >&2
        return 1
    }
    if ! e2e_git_path_is_within "$root" "$canonical_path"; then
        printf 'ERROR: Git fixture path escapes root: %s (root %s)\n' \
            "$canonical_path" "$root" >&2
        return 1
    fi
}

e2e_git_command_directory() {
    local directory="$PWD"
    local -a args=("$@")
    local index=0

    while (( index < ${#args[@]} )); do
        case "${args[index]}" in
            -C)
                ((index += 1))
                [[ $index -lt ${#args[@]} ]] || return 1
                directory="${args[index]}"
                ;;
            -C*)
                directory="${args[index]#-C}"
                ;;
            --git-dir|--work-tree)
                ((index += 1))
                ;;
            --git-dir=*|--work-tree=*)
                ;;
            --)
                break
                ;;
        esac
        ((index += 1))
    done

    printf '%s\n' "$directory"
}

e2e_git_subcommand() {
    local -a args=("$@")
    local index=0

    while (( index < ${#args[@]} )); do
        case "${args[index]}" in
            -C|--git-dir|--work-tree)
                ((index += 2))
                ;;
            -C*|--git-dir=*|--work-tree=*)
                ((index += 1))
                ;;
            --)
                ((index += 1))
                [[ $index -lt ${#args[@]} ]] || return 1
                printf '%s\n' "${args[index]}"
                return
                ;;
            -*)
                ((index += 1))
                ;;
            *)
                printf '%s\n' "${args[index]}"
                return
                ;;
        esac
    done
}

e2e_git_init_target() {
    local -a args=("$@")
    local index=0
    local command_seen=0
    local argument

    while (( index < ${#args[@]} )); do
        argument="${args[index]}"
        if (( ! command_seen )); then
            case "$argument" in
                -C|--git-dir|--work-tree)
                    ((index += 2))
                    ;;
                -C*|--git-dir=*|--work-tree=*)
                    ((index += 1))
                    ;;
                --)
                    ((index += 1))
                    [[ $index -lt ${#args[@]} ]] || return 0
                    [[ "${args[index]}" == init ]] || return 1
                    command_seen=1
                    ((index += 1))
                    ;;
                -*)
                    ((index += 1))
                    ;;
                init)
                    command_seen=1
                    ((index += 1))
                    ;;
                *)
                    return 1
                    ;;
            esac
            continue
        fi

        case "$argument" in
            -b|--initial-branch|--template|--separate-git-dir)
                ((index += 2))
                ;;
            --initial-branch=*|--template=*|--separate-git-dir=*)
                ((index += 1))
                ;;
            --)
                ((index += 1))
                [[ $index -lt ${#args[@]} ]] || return 0
                printf '%s\n' "${args[index]}"
                return
                ;;
            -*)
                ((index += 1))
                ;;
            *)
                printf '%s\n' "$argument"
                return
                ;;
        esac
    done
}

e2e_git_assert_resolved_repo() {
    local root="$1"
    local directory="$2"
    local resolved_top
    local resolved_git_dir
    local canonical_path

    canonical_path="$(e2e_git_canonical_path "$directory")" || return 1
    if resolved_top="$(e2e_git_scrubbed -C "$canonical_path" rev-parse --show-toplevel 2>/dev/null)"; then
        resolved_top="$(e2e_git_canonical_path "$resolved_top")" || return 1
        if e2e_git_path_is_within "$root" "$resolved_top"; then
            return 0
        fi
        printf 'ERROR: Git resolved outside fixture root: %s (root %s)\n' \
            "$resolved_top" "$root" >&2
        return 1
    fi

    if resolved_git_dir="$(e2e_git_scrubbed -C "$canonical_path" rev-parse --absolute-git-dir 2>/dev/null)"; then
        resolved_git_dir="$(e2e_git_canonical_path "$resolved_git_dir")" || return 1
        if e2e_git_path_is_within "$root" "$resolved_git_dir"; then
            return 0
        fi
        printf 'ERROR: Git directory resolved outside fixture root: %s (root %s)\n' \
            "$resolved_git_dir" "$root" >&2
        return 1
    fi

    printf 'ERROR: no Git repository resolved for fixture path %s\n' "$canonical_path" >&2
    return 1
}

e2e_git_validate_command() {
    local root="$1"
    shift
    local directory
    local canonical_directory
    local subcommand
    local init_target

    directory="$(e2e_git_command_directory "$@")" || return 1
    canonical_directory="$(e2e_git_canonical_path "$directory")" || return 1
    subcommand="$(e2e_git_subcommand "$@")" || return 1

    if [[ "$subcommand" == init ]]; then
        init_target="$(e2e_git_init_target "$@")"
        if [[ -n "$init_target" ]]; then
            e2e_git_assert_path_within_fixture "$root" "$init_target" || return 1
        else
            e2e_git_assert_path_within_fixture "$root" "$canonical_directory" || return 1
        fi
        return 0
    fi

    e2e_git_assert_path_within_fixture "$root" "$canonical_directory" || return 1
    e2e_git_assert_resolved_repo "$root" "$canonical_directory"
}

e2e_git_use_fixture_root() {
    local root="${1:?fixture root required}"

    [[ -d "$root" ]] || {
        printf 'ERROR: Git fixture root is not a directory: %s\n' "$root" >&2
        return 1
    }
    E2E_GIT_FIXTURE_ROOT="$(e2e_git_canonical_path "$root")"
    export E2E_GIT_FIXTURE_ROOT
}

git() {
    local root="${E2E_GIT_FIXTURE_ROOT:-}"
    local subcommand
    local init_target

    [[ -n "$root" ]] || {
        echo 'ERROR: Git fixture root was not configured before invoking git' >&2
        return 1
    }
    e2e_git_validate_command "$root" "$@" || return 1
    subcommand="$(e2e_git_subcommand "$@")"
    e2e_git_scrubbed "$@" || return

    if [[ "$subcommand" == init ]]; then
        init_target="$(e2e_git_init_target "$@")"
        if [[ -z "$init_target" ]]; then
            init_target="$(e2e_git_command_directory "$@")"
        fi
        e2e_git_assert_resolved_repo "$root" "$init_target"
    fi
}
