#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/tl-window-state.sh"

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
export TMUX_FIXTURE="$TEMP_DIR/panes"
export PATH="$TEMP_DIR/bin:$PATH"
mkdir -p "$TEMP_DIR/bin"
cat > "$TEMP_DIR/bin/tmux" <<'SH'
#!/usr/bin/env bash
cat "$TMUX_FIXTURE"
SH
chmod +x "$TEMP_DIR/bin/tmux"

e2e_fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

AGGREGATE_SESSION="fixture"
printf 'TL @7 %%9 1\nServer @1 %%1 0\nWatcher @2 %%2 0\n' > "$TMUX_FIXTURE"

old_wait_for_tl_exit_assertion() {
    local count
    for _ in 1 2; do
        count="$(awk '$1 == "TL" { count += 1 } END { print count + 0 }' "$TMUX_FIXTURE")"
        [[ "$count" == "0" ]] && return 0
    done
    return 1
}

old_zero_window_assertion() {
    local count
    count="$(awk '$1 == "TL" { count += 1 } END { print count + 0 }' "$TMUX_FIXTURE")"
    [[ "$count" == "0" ]]
}

if old_wait_for_tl_exit_assertion; then
    e2e_fail "old wait-for-exit assertion unexpectedly accepted a retained terminal TL"
fi
if old_zero_window_assertion; then
    e2e_fail "old zero-window assertion unexpectedly accepted a retained terminal TL"
fi
printf 'PASS: both old no-TL assertions reject the correct retained terminal pane\n'

wait_for_aggregate_tl_terminal "fixture completion"
[[ "$AGGREGATE_TERMINAL_TL_STATE" == "@7 %9" ]] \
    || e2e_fail "terminal pane identity was not retained: $AGGREGATE_TERMINAL_TL_STATE"
assert_aggregate_tl_terminal_unchanged "repeat init" "$AGGREGATE_TERMINAL_TL_STATE"
printf 'PASS: revised checks accept tl_done with the same dead TL pane\n'

printf 'TL @7 %%9 1\nTL @8 %%10 0\nServer @1 %%1 0\nWatcher @2 %%2 0\n' > "$TMUX_FIXTURE"
if (assert_aggregate_tl_terminal_unchanged "repeat init" "$AGGREGATE_TERMINAL_TL_STATE") 2>/dev/null; then
    e2e_fail "revised checks accepted a duplicate/restarted TL pane"
fi
printf 'PASS: revised checks reject an added/restarted TL pane\n'
