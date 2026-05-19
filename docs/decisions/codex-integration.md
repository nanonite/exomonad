# Codex Integration

Status: accepted

Date: 2026-05-13

Chainlink: #148

## Context

Codex is supported as an ExoMonad-spawned agent runtime. It shares the same Rust host, Haskell WASM tool definitions, and hook dispatch path as Claude Code, Gemini, and OpenCode, but its local configuration model is different.

Codex reads runtime configuration from `.codex/config.toml`. ExoMonad writes hook commands into each Codex agent's project config and seeds matching hook trust state in the active Codex user config (`$CODEX_HOME/config.toml` or `~/.codex/config.toml`). It does not need an OpenCode-style TypeScript plugin bridge.

## Decision

ExoMonad writes native Codex identity config files into each Codex agent worktree:

```
<worktree>/
`-- .codex/
    |-- config.toml
    `-- exomonad_role.md
```

`config.toml` contains:

- `model = "..."` when a model is configured for the spawned agent
- `approval_policy = "never"`
- `developer_instructions = """..."""`
- `[features] hooks = true`
- command hooks for `PreToolUse`, `PostToolUse`, and `Stop`
- `[mcp_servers.exomonad]` with `command = "exomonad"` and args `["mcp-stdio", "--role", <role>, "--name", <agent>]`
- any configured `[extra_mcp_servers]` from `.exo/config.toml`

ExoMonad renders hook commands with the absolute `exomonad` binary path:

- `<exomonad> hook pre-tool-use --runtime codex`
- `<exomonad> hook post-tool-use --runtime codex`
- `<exomonad> hook stop --runtime codex`

Codex only honors hook trust state from user/session config layers, not project-local `.codex/config.toml` files. For every project config it writes, ExoMonad computes Codex-compatible `trusted_hash` values from the rendered hook definitions and stores them under `[hooks.state]` in the user config. The state keys use the absolute project config path plus Codex's event labels, for example:

```toml
[hooks.state."<worktree>/.codex/config.toml:pre_tool_use:0:0"]
trusted_hash = "sha256:..."
```

The user config update is protected by a sidecar flock in `CODEX_HOME` and written atomically, so parallel Codex spawns do not lose trust entries. Legacy ExoMonad global hook blocks are stripped from the user config to avoid duplicate hook execution.

These shell hooks forward Codex events to the existing ExoMonad server over the Unix-domain socket. The server normalizes Codex hook stdin into ExoMonad's internal `HookInput`, calls the Haskell WASM hook handler, then formats the result back into Codex hook stdout semantics.

## Why Shell Hooks Instead Of A Plugin

Codex hooks are shell-native. The hook system executes configured commands with JSON on stdin and consumes stdout/exit status. A Bun or TypeScript bridge would duplicate what Codex already provides.

OpenCode needs a TypeScript plugin because OpenCode exposes lifecycle hooks through its plugin API. Codex does not; native command hooks can call `exomonad hook` directly.

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

### `codex exec review` Research

Local CLI/source research for #163 found that `codex exec review` is useful as a standalone Codex review mode, but it is not an ExoMonad reviewer transport:

- Supported targets are mutually exclusive: `--uncommitted`, `--base <BRANCH>`, `--commit <SHA>`, or a positional custom prompt. If none is supplied, the CLI errors.
- `--base <BRANCH>` resolves the merge base with `HEAD` and prompts Codex to inspect `git diff <merge_base_sha>`, so it is a valid standalone diff-review primitive.
- The command accepts normal exec options including `--model`, `--json`, `--output-last-message <FILE>`, and `--dangerously-bypass-approvals-and-sandbox`.
- Output is Codex-native review-mode data rendered as plain text or JSONL events. It does not call ExoMonad review MCP tools and does not write `.exo/reviews/pr_N.json`.
- Exit status indicates execution success or failure only; it does not encode approve versus changes-requested.

If ExoMonad ever wants to use `codex exec review` directly, it needs an explicit translator from the Codex review output into `.exo/reviews/pr_N.json`. Until then, Codex reviewer agents must run through the normal `role=reviewer` MCP path.

## Authentication

Codex authentication is system-wide and configured with:

```bash
codex login
```

ExoMonad does not inject an auth token or provider-specific environment variable at spawn time.

## Related Code

- `rust/exomonad-core/src/codex_config.rs`
- `rust/exomonad-core/src/services/agent_control/internal.rs`
- `rust/exomonad/src/init.rs`
- `tests/e2e/codex-hooks/validate.sh`
- `haskell/wasm-guest/src/ExoMonad/Guest/Tools/SpawnCodex.hs`
- `docs/decisions/codex-hook-wire-format.md`

- `docs/decisions/agent-sandbox-profiles.md`
