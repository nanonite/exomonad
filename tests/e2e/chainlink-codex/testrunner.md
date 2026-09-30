# Chainlink Codex Validator — the migration contract, with no Codex needed

This test validates that the Python TL controller consumes `plan.json` and
dispatches a Codex worker that runs the role-scoped Chainlink session workflow:

```text
harness creates the Chainlink issue  ->  the Python TL controller reads
plan.json  ->  spawn_worker chainlink-codex-worker-codex  ->  the worker
session-starts, marks the issue active, comments on it, ends its session, and
notifies the controller  ->  the controller reaches a durable terminal phase
```

The issue stays open, because the worker role holds neither
`chainlink_issue_create` nor `chainlink_issue_close`, and no Chainlink lock
worktree is created.

Run it through:

```bash
just e2e-chainlink-codex
```

For harness-only validation:

```bash
just check-e2e-chainlink-codex
just check-e2e-python-tl-controller
```

`check-e2e-python-tl-controller` is the cheap gate that runs before anyone
spends a live Codex run. It needs no server, no tmux session, and no `codex`
binary, and it is what proves the scenario was actually migrated rather than
merely re-documented: it fails on a reintroduced `root_agent_type`, on a
validator that looks for the retired project-root `.codex/config.toml`, and on
a scenario that lost its plan-driven controller assertions.
