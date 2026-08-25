# Claude one-shot lifecycle verification

The agent command builder keeps two independent pieces of runtime metadata:

- `prompt_flag` is a harness subcommand (for example OpenCode's `run`).
- `one_shot_flag` is the mode switch for a role-scoped one-shot invocation
  (Claude's `-p`).

Keeping these fields separate prevents a future harness branch from accidentally
using a mode flag as its prompt subcommand. OpenCode continues to build its
explicit `run --interactive` command, while Claude receives `-p` only for
`dev`, `reviewer`, and `worker` roles. Interactive TL and companion processes
remain unchanged.

## Hook smoke verification

On 2026-08-25, Claude Code 2.1.245 was launched with the exact `-p` print mode
and a temporary `SessionStart` command hook. The hook wrote its marker before
the process attempted to contact the model, proving that print mode does not
skip SessionStart registration. The process then exited with Claude's expected
unauthenticated-environment error; no project or session state was changed.

The authenticated MCP/tool portion is covered by the existing WASM contract
tests and the real-server `claude-teams-inbox` E2E. That E2E must be run with
valid Claude credentials to verify calls through the generated `exomonad`
stdio server. Dev and worker roles expose `file_pr`/`notify_parent`; reviewers
submit Forgejo reviews directly and therefore intentionally do not expose
those parent-notification tools.
