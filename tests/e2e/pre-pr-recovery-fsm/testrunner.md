# Runner contract

Build the development server and WASM first, then run:

~~~bash
just check-e2e-pre-pr-recovery-fsm
just e2e-pre-pr-recovery-fsm
~~~

The runner performs three consecutive real-server runs. Each run owns a
temporary repository, remote, Forgejo-shaped API, server process, tmux session,
controller state, and worktrees. Set KEEP_E2E_WORKDIR=1 only while diagnosing
a failure; normal completion removes all disposable state.
