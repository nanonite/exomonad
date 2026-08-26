# --continue identity-preservation E2E (Chainlink #1019)

This real-server acceptance harness creates a disposable Git remote and
Forgejo-shaped API, installs the repository's real WASM bundle, starts the
real exomonad init --start path in tmux, and asks a deterministic one-shot
Codex fixture to publish a PR through the real MCP file_pr tool.

The validator records invocation IDs and the publication registry before
tearing down tmux. It then runs exomonad init --continue and asserts, from
observed JSON and ledger state:

- the same invocation IDs remain authoritative;
- no root.invalid archive is created;
- no watcher.ownership_unresolved event is emitted for the published PR;
- the checkpoint exposes a next lifecycle action;
- plan.json bytes are unchanged;
- a malformed unrelated invocation is classified recreate without changing
  the published owner's identity; and
- --start refuses the existing nonterminal run.

The --mutant option clones the repository into a disposable directory, changes
the production classify_agent Preserve arm to mint a fresh invocation, and
runs the existing Rust preservation regression against that mutant. The mutant
must fail the regression; the unmodified source must pass. Run three
consecutive clean executions:

    for attempt in 1 2 3; do just e2e-init-continue; done

All Git, Forgejo, tmux, server, ledger, and worktree state is disposable and
removed by the harness trap.
