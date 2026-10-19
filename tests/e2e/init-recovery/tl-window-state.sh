aggregate_tl_panes() {
    tmux list-panes -s -t "$AGGREGATE_SESSION" \
        -F '#{window_name} #{window_id} #{pane_id} #{pane_dead}' \
        | awk '$1 == "TL" { print $2, $3, $4 }'
}

wait_for_aggregate_tl_terminal() {
    local context="$1"
    local panes count window pane dead
    for _ in $(seq 1 40); do
        panes="$(aggregate_tl_panes)"
        count="$(printf '%s\n' "$panes" | awk 'NF { count += 1 } END { print count + 0 }')"
        if [[ "$count" == "0" ]]; then
            AGGREGATE_TERMINAL_TL_STATE="absent"
            return
        fi
        [[ "$count" == "1" ]] || e2e_fail "$context left duplicate TL panes ($count found)"
        read -r window pane dead <<< "$panes"
        if [[ "$dead" == "1" ]]; then
            AGGREGATE_TERMINAL_TL_STATE="$window $pane"
            return
        fi
        sleep 0.25
    done
    e2e_fail "$context left a live TL pane after its durable tl_done checkpoint"
}

assert_aggregate_tl_terminal_unchanged() {
    local context="$1"
    local expected="$2"
    local panes count window pane dead actual
    panes="$(aggregate_tl_panes)"
    count="$(printf '%s\n' "$panes" | awk 'NF { count += 1 } END { print count + 0 }')"
    if [[ "$expected" == "absent" ]]; then
        [[ "$count" == "0" ]] || e2e_fail "$context created an unexpected TL pane"
        return
    fi
    [[ "$count" == "1" ]] || e2e_fail "$context changed the terminal TL pane count to $count"
    read -r window pane dead <<< "$panes"
    [[ "$dead" == "1" ]] || e2e_fail "$context restarted a live TL pane after tl_done"
    actual="$window $pane"
    [[ "$actual" == "$expected" ]] || e2e_fail "$context replaced the completed TL pane ($expected -> $actual)"
}
