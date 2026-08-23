# Runner contract

Build the development server and WASM first, then run:

```bash
just check-e2e-task-blocked-human-gate
just e2e-task-blocked-human-gate
```

The harness performs three consecutive disposable real-server runs. Set
`KEEP_E2E_WORKDIR=1` only while diagnosing a failure; normal runs remove every
temporary process, tmux session, worktree, and repository before returning.
