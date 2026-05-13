# Codex Integration

Status: accepted

Date: 2026-05-13

Chainlink: #148

## Context

Codex is supported as an ExoMonad-spawned agent runtime. It shares the same Rust host, Haskell WASM tool definitions, and hook dispatch path as Claude Code, Gemini, and OpenCode, but its local configuration model is different.

Codex reads runtime configuration from `.codex/config.toml` and hook commands from `.codex/hooks.json`. It does not need an OpenCode-style TypeScript plugin bridge.

## Decision

ExoMonad writes native Codex config files into each Codex agent worktree:

```
<worktree>/
`-- .codex/
    |-- config.toml
    |-- hooks.json
    `-- exomonad_role.md
```

`config.toml` contains:

- `model = "..."` when a model is configured for the spawned agent
- `approval_policy = "never"`
- `developer_instructions = """..."""`
- `[features] hooks = true`
- `[mcp_servers.exomonad]` with `command = "exomonad"` and args `["mcp-stdio", "--role", <role>, "--name", <agent>]`
- any configured `[extra_mcp_servers]` from `.exo/config.toml`

`hooks.json` contains shell hook commands:

- `exomonad hook pre-tool-use --runtime codex`
- `exomonad hook post-tool-use --runtime codex`
- `exomonad hook stop --runtime codex`

These shell hooks forward Codex events to the existing ExoMonad server over the Unix-domain socket. The server normalizes Codex hook stdin into ExoMonad's internal `HookInput`, calls the Haskell WASM hook handler, then formats the result back into Codex hook stdout semantics.

## Why Shell Hooks Instead Of A Plugin

Codex hooks are shell-native. The hook system executes configured commands with JSON on stdin and consumes stdout/exit status. A Bun or TypeScript bridge would duplicate what Codex already provides.

OpenCode needs a TypeScript plugin because OpenCode exposes lifecycle hooks through its plugin API. Codex does not; `hooks.json` can call `exomonad hook` directly.

## MCP Configuration

Codex MCP servers are configured in `.codex/config.toml`, not `opencode.json` and not `.mcp.json`.

ExoMonad renders the ExoMonad MCP server as a Codex `mcp_servers` table entry. Extra MCP servers, including context-mode or tilth MCP servers, must be listed under `[extra_mcp_servers]` in `.exo/config.toml`; ExoMonad copies those entries into the Codex config.

## Spawn Commands

Fresh Codex agents use:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir> "$(cat <prompt_file>)"
```

Context-inheriting Codex subtrees use:

```bash
codex fork <session_id> --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir>
```

When a model is configured, ExoMonad adds `--model <model>` to the generated command and writes `model = "<model>"` to `.codex/config.toml`.

## Instructions

Codex receives stable role instructions through `.codex/config.toml` as `developer_instructions`. The task-specific spawn prompt remains in the prompt file passed to `codex exec`.

TL/root Codex agents receive `CODEX_TL_INSTRUCTIONS`, which are the shared TL protocol plus Codex runtime notes for shell hooks, manual restart flags, and `codex fork` context inheritance. Dev/leaf/worker Codex agents receive `CODEX_DEV_INSTRUCTIONS`. Reviewer Codex agents receive `CODEX_REVIEWER_INSTRUCTIONS` so they use ExoMonad review MCP tools. Role context is also copied to `.codex/exomonad_role.md` for local inspection.

## Reviewer

Codex reviewers run as ordinary ExoMonad reviewer agents in tmux with `role=reviewer`:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir> "$(cat <prompt_file>)"
```

Do not use `codex exec review` for ExoMonad reviewer agents. That subcommand emits Codex-native review output, but it does not write ExoMonad's `.exo/reviews/pr_N.json` files. ExoMonad reviewer convergence depends on the reviewer agent calling the MCP review tools (`approve_pr`, `request_changes`, or `post_review_comment`) from the `role=reviewer` configuration.

The reviewer identity discipline still applies: reviewer agents use distinct git identities and never review under the identity that authored the PR.

## Authentication

Codex authentication is system-wide and configured with:

```bash
codex login
```

ExoMonad does not inject an auth token or provider-specific environment variable at spawn time.

## Related Code

- `rust/exomonad-core/src/codex_config.rs`
- `rust/exomonad-core/src/services/agent_control/internal.rs`
- `haskell/wasm-guest/src/ExoMonad/Guest/Tools/SpawnCodex.hs`
- `docs/decisions/codex-hook-wire-format.md`
