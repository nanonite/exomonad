# One-shot lifecycle E2E runner

Run the live headless test with:

```bash
just e2e-one-shot-lifecycle
```

Run the harness-only syntax checks without launching the server, Codex fixture,
or tmux pane with:

```bash
just check-e2e-one-shot-lifecycle
```

The runner creates an isolated git repository, bare push remote, and
Forgejo-compatible HTTP mock. Its `fake-codex.sh` is selected through `PATH`
only inside that fixture; the production server still creates Codex invocations
and the validator calls the real MCP endpoint over the server UDS.

The test must leave no `e2e-one-shot-lifecycle` tmux session behind. On failure,
set `KEEP_E2E_WORKDIR=1` to retain the validator result, server log, mock log,
and fake Codex transcript for diagnosis.
