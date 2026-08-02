# Cross-provider one-shot lifecycle

ExoMonad keeps one workflow owner per Chainlink issue: one agent identity, one
worktree, one branch, and one PR. An invocation is process-attempt metadata
inside that owner. `resume_pr` replaces the invocation record in the same
worktree and PR; it does not create a stacked PR or a second owner.

## Provider contract

| Provider | Assignment boundary | Live guidance | Dormant guidance | Authoritative result |
| --- | --- | --- | --- | --- |
| Claude | One assignment per process | Teams inbox, then the exact current pane | Durable inbox, shown on resume | PR/review/CI |
| Gemini | One assignment per process | Exact current tmux pane | Durable inbox, shown on resume | PR/review/CI |
| Shoal | One assignment per process | UDS, then exact current pane | Durable inbox, shown on resume | PR/review/CI |
| OpenCode | One assignment per process | Exact current tmux pane | Durable inbox, shown on resume | PR/review/CI |
| Codex | One assignment per process | Exact current tmux pane | Durable inbox, shown on resume | PR/review/CI |
| Process | External companion process | Exact target only when one is registered | Durable inbox for a managed identity | External process contract |

One-shot does not mean non-interactive. While a process is alive, its unread
inbox guidance may be injected into the exact invocation's validated pane.
Routing uses the current `routing.json` target and rejects stale or unverifiable
targets. It never redirects a stale message to the root pane. When there is no
live invocation, the durable inbox row remains unread for the next `resume_pr`.

## Lifecycle telemetry

The host emits `agent.invocation.started`, `agent.invocation.finished`, and
`agent.guidance.delivery` through the existing tracing and `.exo/events`
JSONL facilities. The structured payload includes:

- provider and role;
- invocation ID and generation;
- trigger (`spawn`, `resume_pr`, or `review`);
- PR number and head SHA when known;
- outcome/status and delivery channel; and
- `delivery_vs_authoritative`, which is `delivery_only` for inbox/tmux/UDS
  guidance and `metadata_only` for process lifecycle events.

An injection success, process exit, or local push is not a PR/review/CI state
transition. The Forgejo publication, exact-SHA review verdict, and CI
observations remain authoritative.

## Staged rollout and rollback

Set `EXOMONAD_ONE_SHOT_LIFECYCLE` explicitly to one of:

| Value | Meaning |
| --- | --- |
| `enabled` (default) | Run lifecycle checks and emit telemetry. |
| `shadow` | Run checks and emit observations, without changing authoritative workflow transitions. |
| `disabled` | Make the new lifecycle contract opt-out observable; retain safe invocation metadata and exact-pane rejection. |

The parser also accepts `on`/`true`/`1`, `observe`, and `off`/`false`/`0`.
Unknown values are logged and safely use `enabled`; they do not enable a root
fallback. Existing one-shot execution, durable inbox persistence, and stale
target rejection remain in force in every mode.

To roll out, set the variable to `shadow`, inspect the structured lifecycle
events across all provider types, then change it to `enabled`. To roll back,
set it to `shadow` first (to keep observations while freezing authoritative
transitions) or `disabled` for an explicit opt-out, then restart the host so
the process reads the setting consistently. Remove the variable to restore
the default `enabled` mode.

