# Codex Reviewer Sandbox/Instructions Consistency E2E

Regression coverage for a Codex reviewer bug: `CODEX_REVIEWER_INSTRUCTIONS` once told
reviewers to submit their final Forgejo verdict with `curl`/`fj` from their own shell,
while the Codex reviewer sandbox profile (`codex_config.rs`, `permissions.reviewer`)
sets `network_access = false`. Every Codex reviewer was structurally unable to submit
a review — see `docs/decisions/agent-sandbox-profiles.md` and
`docs/decisions/codex-integration.md`.

This harness drives the real worktree event watcher (`exomonad serve`, no mocked spawn
logic) against a mock Forgejo API. It creates one open PR and lets the watcher
auto-spawn a real Codex reviewer worktree for it on first sighting — the same code
path production uses. It then inspects the generated `.codex/config.toml` on disk and
asserts:

1. `permissions.reviewer.network_access` is `false` (the sandbox setting itself is
   correct and intentional — see the ADR).
2. `developer_instructions` does not tell the reviewer to reach Forgejo via `curl`/`fj`
   from its own shell, since that can never work under (1).
3. `developer_instructions` does tell the reviewer to submit verdicts through the
   `approve_pr`/`request_changes` MCP tools, which run in the unsandboxed ExoMonad host
   process and always have real network access.
4. The reviewer worktree's `.exo/server.sock` is actually symlinked in — the previous
   (incorrect) justification for the `curl`-based rewrite was that reviewer worktrees
   might not have this socket.

Does not require a live `codex` binary: it only exercises ExoMonad's own spawn and
config-generation code, not an actual Codex process.
