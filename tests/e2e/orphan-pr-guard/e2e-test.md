# Codex-TL orphan-PR guard

This fixture is an opt-in live smoke test for the existing-PR resume path. It
creates a disposable bare remote and worktree with:

- `main` as the seeded base branch;
- one open, unmerged PR whose head is exactly
  `main.m7-3a-fixture-oracle-opencode`;
- a historical OpenCode owner identity and a `CHANGES_REQUESTED` review.

The root harness is Codex. The initial prompt describes the review task and
existing-PR requirement without naming the owner, branch, or runtime. The
fixture's `-opencode` suffix is historical branch data only; it does not
exercise an OpenCode harness.

On a live run, the script records the init, mock Forgejo, and tmux logs and
requires an observable typed `resume_pr` call for the seeded PR. It then checks
that the exact branch is retained, no sibling/double-suffix/`-2` branch or
extra worktree exists, and exactly one open PR remains. Cleanup runs on success,
failure, and interruption.

Run the static check with:

```bash
just check-e2e-orphan-pr-guard-codex
```

The live smoke is deliberately not part of the default or required checks:

```bash
just e2e-orphan-pr-guard-codex
```

It requires a built `exomonad` binary, devswarm WASM artifacts, Codex, tmux,
and the local Python mock server. This fixture does not run live Claude,
OpenCode, or Gemini coverage. The deterministic host/service/WASM tests added
by #563 and #564 are the authoritative regression coverage for the typed
resume and canonical-root-protocol behavior.
