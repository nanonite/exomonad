# Codex Reviewer Sandbox/Instructions Consistency E2E

Regression coverage for a Codex reviewer bug: `CODEX_REVIEWER_INSTRUCTIONS` once told
reviewers to submit their final Forgejo verdict with `curl`/`fj` from their own shell,
while the Codex reviewer sandbox profile (`codex_config.rs`, `[sandbox_workspace_write]`)
set `network_access = false` at the time. Every Codex reviewer was structurally unable
to submit a review — see `docs/decisions/agent-sandbox-profiles.md` and
`docs/decisions/codex-integration.md`.

`network_access` is now `true` (chainlink #1086 — denying it turned out to be unfixable
on hosts whose AppArmor `unprivileged_userns` profile lacks `capability net_admin`,
chainlink #1085), so the sandbox itself no longer blocks a raw curl/fj call. The
invariant this test guards still applies regardless: reviewer verdicts must go through
the MCP tools, never an ad hoc shell call, per the reviewer-authorship invariant in the
ADR.

This harness drives the real worktree event watcher (`exomonad serve`, no mocked spawn
logic) against a mock Forgejo API. It creates one open PR and lets the watcher
auto-spawn a real Codex reviewer worktree for it on first sighting — the same code
path production uses. It then inspects the generated `.codex/config.toml` on disk and
asserts:

1. `sandbox_workspace_write.network_access` is `true` (matches chainlink #1086 — see
   the ADR for why network denial isn't viable on this host class).
2. `developer_instructions` does not tell the reviewer to reach Forgejo via `curl`/`fj`
   from its own shell — the sandbox no longer blocks this mechanically, so it must be
   enforced by instruction/policy instead.
3. `developer_instructions` does tell the reviewer to submit verdicts through the
   `approve_pr`/`request_changes` MCP tools, which run in the unsandboxed ExoMonad host
   process and always have real network access.
4. The reviewer worktree's `.exo/server.sock` is actually symlinked in — the previous
   (incorrect) justification for the `curl`-based rewrite was that reviewer worktrees
   might not have this socket.

Does not require a live `codex` binary: it only exercises ExoMonad's own spawn and
config-generation code, not an actual Codex process.
