# Agent Sandbox Profiles

**Status:** Proposed

**Date:** 2026-05-19

**Chainlink:** #309

## Context

Codex agents are spawned with per-agent `.codex/config.toml` files, but before #309 the files only described hooks, MCP servers, and instructions. Runtime hook policy from #308 blocks known edit tools, and lifecycle hardening from #298/#304 prevents reviewer and worker provenance failures, but filesystem confinement belongs in the runtime sandbox when the runtime provides one.

Codex provides native filesystem sandboxing through named permission profiles selected by `default_permissions`. ExoMonad now renders one profile per role into every generated Codex config and selects the active profile from the agent role.

## Investigation

The write-path inventory below was produced with `strace -f -e trace=%file` against representative commands and cross-checked against ExoMonad role responsibilities. Commands that require network credentials (`gh`, `git fetch`) were classified by their local write surfaces rather than their remote side effects.

| Workflow | Observed or expected writes | Role implication |
|----------|-----------------------------|------------------|
| `git status`, `git diff`, `git log`, `git show` | May refresh `.git/index`; otherwise read-only | TL/root need `.git` write for safe read-only git inspection; reviewer gets no commit path because #308/#298 deny mutating git commands. |
| `git fetch` | `.git/FETCH_HEAD`, remote refs, pack files under `.git/objects` | TL/root need `.git` write for branch discovery and merge orchestration. Reviewers may need read-only diff context; fetch should stay coordinator-owned unless a reviewer test workflow proves otherwise. |
| `cargo test`, `cargo check`, `cargo metadata` | `target/`, sometimes `Cargo.lock` if dependency graph changes | Reviewers need build output writes only if they run tests themselves. `Cargo.lock` updates are not allowed in reviewer profile. |
| `just <test target>` | Delegates to build tools; writes `target/`, `dist-newstyle/`, generated logs under project temp/cache directories | Reviewer profile includes common build artifact roots but not arbitrary source writes. |
| `nix develop --command ...` | Nix store is external and managed by the host; project-local writes come from the nested command | No extra project write root beyond the nested workflow. |
| `gh pr view`, `gh pr diff`, `gh pr checks` | User cache/config under `$HOME`, not project source | Agents should prefer ExoMonad MCP tools over raw `gh`; #308 still blocks gh from hook shell commands. |
| ExoMonad MCP calls | `.exo/` state (events, session metadata), `.git` only for coordinator merge/write tools | TL/root need `.exo` and `.git`; reviewers need event/session surfaces and build output roots; dev/worker need full workspace write. |

## Decision

Codex config rendering writes:

```toml
default_permissions = "<role-profile>"

[permissions.root]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo", ".git"]

[permissions.tl]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo", ".git"]

[permissions.reviewer]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = [".exo/events", ".exo/tmp", "target", "rust/target", "dist-newstyle", "haskell/dist-newstyle", ".stack-work", ".cache"]

[permissions.dev]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = ["."]

[permissions.worker]
sandbox_mode = "workspace-write"
network_access = false
writable_roots = ["."]
```

Role mapping is conservative:

| ExoMonad role | Codex profile |
|---------------|---------------|
| `root` | `root` |
| `tl` | `tl` |
| `reviewer` | `reviewer` |
| `worker` | `worker` |
| `dev` and custom roles | `dev` |

The custom-role fallback keeps unknown implementation roles functional while preserving strict profiles for the coordination and review roles.

## Reviewer Cargo Tradeoff

Reviewer test runs are the tension point. A reviewer that can run `cargo test` needs broad build-artifact writes, but a reviewer that can write arbitrary source files undermines #298. The current profile chooses the middle path: reviewers may write common build/cache output roots and ExoMonad event/session state, but not source paths or `Cargo.lock`. Review verdicts are submitted through Forgejo rather than written to workspace files.

If this proves too narrow, prefer moving reviewer test execution into an ExoMonad MCP tool that runs outside the Codex sandbox and returns structured results. Broadening reviewer workspace writes should be the fallback, not the default, because it weakens the reviewer authorship invariant.

## Relationship To Hook Policy

This is the structural fix for Codex. The runtime hook parity from #308 remains belt-and-braces for runtimes that do not sandbox, and for defense in depth when a Codex profile is missing or disabled. The reviewer edit deny from #298 and worker clean-tree/provenance invariant from #304 remain authoritative behavioral policy; sandbox profiles only reduce the filesystem blast radius.

## Consequences

- Root and TL Codex agents can mutate orchestration state and git metadata, but not source files through ordinary file writes.
- Reviewers can submit review records through Forgejo and write build artifacts, but source modification attempts must fail at the sandbox layer and at the PreToolUse hook layer.
- Dev leaves and workers keep full workspace write because implementation is their job.
- Network remains disabled in all generated profiles; networked operations should route through approved tools or explicit operator policy.
