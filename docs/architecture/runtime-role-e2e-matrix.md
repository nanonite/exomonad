# Runtime Role E2E Matrix

Chainlink: #171

This matrix defines the release gate for ExoMonad runtime-role coverage. The regular gate stays cheap enough for normal development. The release-only gate covers token-burning or externally stateful flows before a production release.

## Gate Tiers

| Tier | When | Required checks | Scope |
| --- | --- | --- | --- |
| Static contract | Every PR that changes roles, tools, hooks, or launch metadata | `just role-hook-tests`, `just test-wasm-integration`, `just check-e2e-mcp-tool-visibility` | WASM role exports, hook deny rules, and live MCP tool visibility match `docs/architecture/agent-system.md`. |
| Local runtime | Every PR that changes runtime launch, delivery, or Chainlink session behavior | Relevant `check-e2e-*` target plus the smallest live E2E for the changed path | Codex and OpenCode paths that run from local temp repos without Forgejo or Claude token pressure. |
| Release runtime | Before production release | Full live runtime-role table below | Claude-only and mixed-runtime flows that consume credits or depend on Forgejo reviewer state. |
| Lifecycle/provenance | Before production release and after reviewer/lifecycle changes | Reviewer authorship E2E and lifecycle invariants E2E | Reviewers remain read-only; child commits keep child authorship; stop/shutdown hooks preserve required phase invariants. |

## Runtime Role Coverage

| Role path | Runtime(s) | Gate | Current target or tracker | Required evidence |
| --- | --- | --- | --- | --- |
| Root TL startup and Teams registration | Claude | Release runtime | `just e2e-claude-only`; `just check-e2e-claude-only` | SessionStart registers the Claude root, TeamCreate succeeds, and no child agents are spawned by the bounded smoke prompt. |
| Root TL -> sub-TL -> worker notify delivery | Codex | Local runtime | `just e2e-subtl-worker-notify`; `just check-e2e-subtl-worker-notify` | Worker `notify_parent` is routed to the sub-TL pane 0 while a worker pane is active. |
| Root TL -> sub-TL recursive fork_wave | Codex, OpenCode | Local runtime | `just e2e-subtl-recursive-fork-wave runtime=codex`; `just e2e-subtl-recursive-fork-wave runtime=opencode`; `just check-e2e-subtl-recursive-fork-wave` | A sub-TL can fork a second-level worker and deliver completion through the parent tree. |
| Root TL -> sub-TL recursive fork_wave | Claude | Release runtime | `just e2e-subtl-recursive-fork-wave runtime=claude`; #421 | Same as the local recursive path, but run only when Claude credits are available. |
| TL -> Chainlink worker flow | Codex | Local runtime | `just e2e-chainlink-codex`; `just check-e2e-chainlink-codex` | TL and worker use only allowed Chainlink MCP tools; coordinator closes the issue. |
| SessionStart Chainlink DB failsafe | Claude-shaped, Codex-shaped, OpenCode-shaped hooks | Static contract | `just e2e-chainlink-env-failsafe`; `just check-e2e-chainlink-env-failsafe` | SessionStart fails loudly when `CHAINLINK_DB` is unset or points at a phantom DB. |
| Direct sqlite access block | Claude-shaped, Codex-shaped, OpenCode-shaped hooks | Static contract | `just e2e-chainlink-sqlite-block`; `just check-e2e-chainlink-sqlite-block` | PreToolUse blocks direct `.chainlink/issues.db` reads and writes. |
| Reviewer hardening and authorship preservation | Reviewer plus dev leaf | Lifecycle/provenance | #301 | Reviewer submits reviews through Forgejo API only, cannot write files or commit, and merge history preserves the dev leaf author. |
| Agent lifecycle invariants | TL, dev leaf, reviewer, worker | Lifecycle/provenance | #305 | Stop/shutdown behavior is denied or allowed by phase, and critical review phases cannot self-teardown. |

## Release Rule

A production release cannot ship with a changed role/tool/hook/runtime path unless its row is either green in `E2E_STATUS.md` or explicitly deferred with a Chainlink issue that is not part of the release milestone.
