# Plan: Codex Integration as ExoMonad Coding Harness

## Context

ExoMonad currently supports three coding agent runtimes: Claude Code (native), Gemini (via ACP), and OpenCode (via TypeScript plugin bridge). This plan adds **Codex** (OpenAI's CLI agent, codex-rs) as a fourth harness, following the same patterns established for OpenCode.

The goal is full parity: Codex agents can act as TL (spawnable via `fork_wave`), dev/leaf (spawnable via `spawn_codex`), reviewer, and worker. All lifecycle stages work — spawn, hook interception, PR filing, reviewer convergence loop, CI/Tangled messaging, and `notify_parent`. E2E tests verify the full pipeline.

**Prerequisite (not in scope here):** Roger will have a working `codex` binary installed with auth configured system-wide via `codex login`. Auth is stored in `~/.codex/` and persists across sessions — no env var needed at spawn time, same model as OpenCode's `use_embedded_key`.

---

## Key Architecture Decisions

### Hook Bridge: Shell Script (not TypeScript)
OpenCode needed a Bun TypeScript plugin because its hook system is plugin-based. Codex's hook system is shell-command-based (configured via `hooks.json`). The bridge is simpler: write a `hooks.json` that calls `exomonad hook <event> --runtime codex` as a shell command. No TypeScript bridge file needed.

### MCP Config: `.codex/config.toml`
OpenCode used `opencode.json`. Codex reads MCP servers from `.codex/config.toml` under `[[mcp.servers]]`.

### Context Fork: Supported
Codex has `codex fork` and `codex exec resume` — context inheritance is possible. `fork_wave` with Codex agents is in scope: use `codex fork --last` (or by session ID) to fork from the parent's session, same pattern as OpenCode's `--session <id> --fork`.

### Spawn Command
```bash
# Dev/leaf agent (fresh context)
EXOMONAD_AGENT_ID=<name> EXOMONAD_ROLE=<role> ... \
  codex exec --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir> "$(cat <prompt_file>)"

# TL fork (context inherited from parent session)
codex fork <parent_session_id>
# ...then deliver task via stdin or resume prompt
```

No API key env var needed — `codex login` stores auth system-wide in `~/.codex/`.

### Reviewer Role
Codex reviewers must run as ordinary ExoMonad reviewer agents in tmux with `role=reviewer`:
```bash
EXOMONAD_AGENT_ID=<name> EXOMONAD_ROLE=reviewer ... \
  codex exec --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir> "$(cat <prompt_file>)"
```

Do **not** use bare `codex exec review` for ExoMonad reviewer agents. Local research confirmed that the built-in review subcommand emits Codex-native review text/events and does not write `.exo/reviews/pr_N.json`. ExoMonad reviewer convergence depends on MCP review tools (`approve_pr`, `request_changes`, `post_review_comment`) from the `role=reviewer` config, so reviewers need `CODEX_REVIEWER_INSTRUCTIONS` and the normal ExoMonad MCP command path.

---

## Implementation Phases

### Phase 1 — Proto: Add `AGENT_TYPE_CODEX`

**File:** `proto/effects/agent.proto`

Add to the `AgentType` enum:
```protobuf
AGENT_TYPE_CODEX = 5;
```

Also add to `rust/exomonad-proto/proto/effects/agent.proto` (kept in sync).

After editing, regenerate:
```bash
just proto-gen
```

Then update all `match agent_type` exhaustive matches in Rust that currently handle `OpenCode` — add a `Codex` arm everywhere. Key files:
- `rust/exomonad-core/src/services/agent_control/spawn.rs`
- `rust/exomonad-core/src/services/agent_control/internal.rs`
- `rust/exomonad-core/src/domain/agent.rs` (or wherever `AgentType` is matched)
- Any role-routing logic that maps `AgentType → Role`

---

### Phase 2 — Rust: Config Generation

**File to create:** `rust/exomonad-core/src/codex_config.rs`

This module (analogous to `opencode_plugin.rs`) holds the constants for Codex config:

```rust
/// Written to <agent_dir>/.codex/config.toml
pub const CODEX_CONFIG_TEMPLATE: &str = r#"
model = "{{model}}"
approval_policy = "never"

[[mcp.servers]]
name = "exomonad"
command = "exomonad"
args = ["mcp-stdio", "--role", "{{role}}", "--name", "{{agent_name}}"]
enabled = true
default_tools_approval_mode = "trusted"
"#;

/// Written to <agent_dir>/.codex/hooks.json
pub const CODEX_HOOKS_JSON: &str = r#"[
  {
    "event": "pre-tool-use",
    "command": "exomonad hook pre-tool-use --runtime codex",
    "timeout_sec": 30
  },
  {
    "event": "post-tool-use",
    "command": "exomonad hook post-tool-use --runtime codex",
    "timeout_sec": 30
  },
  {
    "event": "stop",
    "command": "exomonad hook stop --runtime codex",
    "timeout_sec": 10
  }
]"#;
```

**File to modify:** `rust/exomonad-core/src/services/agent_control/internal.rs`

Add `write_codex_config_files(agent_dir, role, agent_name, model, extra_mcp)`:
```rust
pub async fn write_codex_config_files(
    dir: &Path,
    role: &str,
    agent_name: &str,
    model: Option<&str>,
    extra_mcp_servers: &HashMap<String, serde_json::Value>,
) -> Result<()> {
    let codex_dir = dir.join(".codex");
    fs::create_dir_all(&codex_dir).await?;

    // config.toml — MCP + approval mode
    let config = build_codex_config_toml(role, agent_name, model, extra_mcp_servers);
    fs::write(codex_dir.join("config.toml"), config).await?;

    // hooks.json — shell hook bridge
    fs::write(codex_dir.join("hooks.json"), CODEX_HOOKS_JSON).await?;

    tracing::info!(path = %codex_dir.display(), "Wrote Codex config files");
    Ok(())
}
```

Extra MCP servers from `config.toml`'s `[extra_mcp_servers]` are serialized as additional `[[mcp.servers]]` blocks.

**context-mode and tilth:** These MCP servers are currently configured for Claude Code but Codex agents need them too (for context window management and code intelligence). They are NOT auto-discovered — they must be explicitly added to `.exo/config.toml` as `[extra_mcp_servers]` entries so they get injected into every Codex agent's `.codex/config.toml` at spawn time.

Before the first Codex agent can be spawned, verify the stdio command for each:
```toml
[extra_mcp_servers.context-mode]
type = "stdio"
command = "node"
args = ["/path/to/context-mode/server.js"]   # confirm actual path

[extra_mcp_servers.tilth]
type = "stdio"
command = "tilth"                              # confirm actual binary
args = []
```

This is a **manual one-time setup step** in `.exo/config.toml` before running any Codex E2E tests.

---

### Phase 3 — Rust: Spawn Command Construction

**File to modify:** `rust/exomonad-core/src/services/agent_control/internal.rs`

Add `build_codex_command(worktree_dir, prompt_path, model, fork_session)` alongside `build_agent_command`:

```rust
fn build_codex_command(
    worktree_dir: &Path,
    prompt_path: &Path,
    model: Option<&str>,
    fork_session: Option<&str>,
) -> String {
    let dir_escaped = shell_escape(worktree_dir.to_str().unwrap());
    let prompt_escaped = shell_escape(prompt_path.to_str().unwrap());
    let model_flag = model
        .map(|m| format!(" --model {}", shell_escape(m)))
        .unwrap_or_default();

    match fork_session {
        // Fork from parent's codex session (TL children via fork_wave)
        Some(session_id) => {
            let sid_escaped = shell_escape(session_id);
            format!(
                r#"codex fork {} --dangerously-bypass-approvals-and-sandbox --cd {}{}"#,
                sid_escaped, dir_escaped, model_flag
            )
        }
        // Fresh context (spawn_codex / spawn_leaf)
        None => format!(
            r#"codex exec --dangerously-bypass-approvals-and-sandbox --cd {}{} "$(cat {})""#,
            dir_escaped, model_flag, prompt_escaped
        ),
    }
}
```

Prompt can also be piped via stdin (`-` argument) rather than `$(cat file)` — investigate which is more reliable for large specs.

No API key env var needed — auth is system-wide via `codex login`. The env prefix carries only exomonad identity vars.

**Instructions constants** in `spawn.rs`:

```rust
const CODEX_TL_INSTRUCTIONS: &str = r#"
# ExoMonad TL Protocol (Codex)
You are a TL agent in an ExoMonad agent tree. Decompose and delegate.
- Use spawn_codex or spawn_leaf MCP tools to spawn leaf agents
- Use file_pr and notify_parent MCP tools; never use `gh pr create` directly
- After spawning, stop. Wait for notifications via notify_parent
- Work only in your worktree. Never checkout another branch.
- Git ops use bash (git, gh). MCP tools for orchestration only.
"#;

const CODEX_DEV_INSTRUCTIONS: &str = r#"
# ExoMonad Dev Protocol (Codex)
You are a dev agent. Implement the spec precisely.
- When done, file a PR via the file_pr MCP tool (never `gh pr create`)
- Call notify_parent when the PR is filed or if you fail
- Work only in your worktree. Never spawn child agents.
- Git ops use bash (git, gh). MCP for orchestration only.
"#;
```

Instructions are written as a `instructions` key in `config.toml`:
```toml
instructions = """
# ExoMonad Dev Protocol (Codex)
...
"""
```

(Verify whether Codex reads `instructions` from `config.toml`. If not, deliver via the prompt itself as a preamble section.)

---

### Phase 4 — Rust: Hook Runtime Dispatch

**File to modify:** `rust/exomonad/src/hook.rs` (or wherever `--runtime` is dispatched)

Add `codex` as a recognized runtime alongside `opencode`:

```rust
"codex" => normalize_codex_hook_payload(stdin_json),
```

The Codex pre-tool-use hook stdin format (from the hook system in codex-rs) is approximately:
```json
{
  "event": "pre-tool-use",
  "tool": "<tool_name>",
  "args": { ... }
}
```

The normalizer maps this to exomonad's internal `HookPayload` and maps the response back to Codex's expected stdout format (allow/deny/rewrite).

**Note:** Read `codex-rs/hooks/src/events/pre_tool_use.rs` to confirm exact stdin/stdout schema before implementing the normalizer. This is the highest-risk piece — get the wire format exactly right.

---

### Phase 5 — Haskell WASM: `spawn_codex` Tool

**File to create:** `haskell/wasm-guest/src/ExoMonad/Guest/Tools/SpawnCodex.hs`

Pattern: mirror `SpawnOpenCode.hs` exactly, changing agent type.

```haskell
module ExoMonad.Guest.Tools.SpawnCodex where

import ExoMonad.Guest.Effects
import ExoMonad.Guest.Tools.Spawn (spawnLeafSubtree)

spawnCodexSchema :: ToolSchema
spawnCodexSchema = ToolSchema
  { name = "spawn_codex"
  , description = "Spawn a Codex agent in its own worktree+branch. Files PR when done."
  , inputSchema = spawnLeafInputSchema  -- reuse existing schema
  }

handleSpawnCodex :: ToolInput -> Eff Effects ToolResult
handleSpawnCodex input = spawnLeafSubtree AgentTypeCodex input
```

**File to modify:** `haskell/wasm-guest/src/ExoMonad/Guest/Tools/AllTools.hs` (or equivalent registration)

Add `spawnCodexSchema` and `handleSpawnCodex` to the tool registry for `root` and `tl` roles.

**File to modify:** `haskell/wasm-guest/src/ExoMonad/Guest/Effects.hs` (or agent type enum)

Add `AgentTypeCodex` to the `AgentType` Haskell sum type (mirrors the proto enum).

**File to modify:** role WASM configs in `.exo/roles/devswarm/`:
- `TLRole.hs` — add `spawn_codex` to TL tool list
- `RootRole.hs` — add `spawn_codex` to Root tool list

---

### Phase 6 — Haskell WASM: Hook Handler for Codex Runtime

**File to modify:** `haskell/wasm-guest/src/ExoMonad/Guest/Hooks.hs` (or wherever runtime is dispatched)

Add a `codex` arm that normalizes Codex's hook payload shape and applies role-based allow/deny/rewrite logic (same as opencode arm but with Codex's wire format).

---

### Phase 7 — Worktree Event Watcher / Reviewer Support

No new code required. The worktree event watcher (`exomonad-core/src/services/worktree_watcher/`) fires events based on PR state regardless of agent type. Codex agents get the same `PRReview::ReviewReceived`, `ReviewerApproved`, etc. events injected into their tmux pane.

For **Codex as reviewer**: launch a normal Codex tmux pane using the reviewer role and prompt so the agent can call ExoMonad MCP review tools:
```bash
EXOMONAD_AGENT_ID=<name> EXOMONAD_ROLE=reviewer ... \
  codex exec --dangerously-bypass-approvals-and-sandbox --cd <worktree_dir> "$(cat <prompt_file>)"
```
The reviewer identity discipline (separate git user.name) applies identically. The reviewer agent writes `.exo/reviews/pr_N.json` via `approve_pr`, `request_changes`, or `post_review_comment`; the worktree watcher then routes the result.

---

### Phase 8 — CI/Tangled/Spindle Integration

No new code required. Codex agents live in worktrees; the worktree event watcher picks up CI status events for any PR regardless of agent type. The `tangled_spindle_url` integration fires `CiStatus` events into any agent's pane. No Codex-specific work needed.

---

### Phase 9 — CLAUDE.md and Documentation Updates

**Files to update:**
- `CLAUDE.md` — Add `spawn_codex` to MCP Tools Reference table. Add Codex to Built Infrastructure table. Update Capabilities section.
- `haskell/wasm-guest/CLAUDE.md` — Note `SpawnCodex.hs` in tool directory.
- `rust/exomonad-core/CLAUDE.md` — Note `codex_config.rs` module.

**File to create:** `docs/decisions/codex-integration.md`

Document: hook bridge approach (why shell not TS), MCP config approach, spawn command flags, instructions delivery mechanism, what differs from OpenCode.

---

### Phase 10 — E2E Tests

**Directory structure:**
```
tests/e2e/codex-hooks/
├── run.sh           # Setup: temp repo, codex config, exomonad init
├── testrunner.md    # Testrunner companion plan
└── e2e-test.md      # Root TL rules for this test

tests/e2e/codex-messaging/
├── run.sh
├── testrunner.md
└── e2e-test.md
```

**Test 1: `codex-hooks`** (mirror `oc-rewrite`)
- Spawn Codex dev agent with a task that triggers an MCP tool call
- Testrunner verifies that `exomonad hook pre-tool-use --runtime codex` was called (check logs)
- Testrunner verifies that allow/deny decisions flow correctly

**Test 2: `codex-messaging`** (mirror `messaging`)
- Spawn Codex dev agent
- Agent calls `notify_parent`
- Testrunner verifies the message arrives in the root TL's Teams inbox

**Justfile additions:**
```makefile
e2e-codex-hooks:
    bash tests/e2e/codex-hooks/run.sh

e2e-codex-messaging:
    bash tests/e2e/codex-messaging/run.sh
```

---

## Critical File List

| Action | File |
|--------|------|
| Modify | `proto/effects/agent.proto` |
| Modify | `rust/exomonad-proto/proto/effects/agent.proto` |
| Create | `rust/exomonad-core/src/codex_config.rs` |
| Modify | `rust/exomonad-core/src/services/agent_control/internal.rs` |
| Modify | `rust/exomonad-core/src/services/agent_control/spawn.rs` |
| Modify | `rust/exomonad/src/hook.rs` |
| Create | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/SpawnCodex.hs` |
| Modify | `haskell/wasm-guest/src/ExoMonad/Guest/Tools/AllTools.hs` |
| Modify | `haskell/wasm-guest/src/ExoMonad/Guest/Effects.hs` |
| Modify | `haskell/wasm-guest/src/ExoMonad/Guest/Hooks.hs` |
| Modify | `.exo/roles/devswarm/TLRole.hs` |
| Modify | `.exo/roles/devswarm/RootRole.hs` |
| Create | `docs/decisions/codex-integration.md` |
| Create | `tests/e2e/codex-hooks/` (3 files) |
| Create | `tests/e2e/codex-messaging/` (3 files) |
| Modify | `CLAUDE.md`, `justfile` |

---

## Verification

```bash
# 1. Proto compiles cleanly
just proto-gen

# 2. Rust compiles with no exhaustive match warnings
just build

# 3. WASM builds
just wasm-all

# 4. Full install
just install-all-dev

# 5. Rust tests pass
cargo test --workspace

# 6. Hook bridge works (manual smoke test)
echo '{"event":"pre-tool-use","tool":"bash","args":{"command":"ls"}}' | \
  exomonad hook pre-tool-use --runtime codex

# 7. E2E: hooks
just e2e-codex-hooks

# 8. E2E: messaging
just e2e-codex-messaging
```

---

## Chainlink Issue Decomposition

Suggested issues (wave order):

**Wave 1 (independent, parallel):**
- `[ISSUE] Codex: proto + Rust exhaustive match stubs` — Add proto enum value, update all match arms with `todo!()` stubs. Build must pass.
- `[ISSUE] Codex: research hook wire format` — Read `codex-rs/hooks/src/events/pre_tool_use.rs` and document exact stdin/stdout JSON schema. Output: a markdown doc in `docs/decisions/`.

**Wave 2 (depends on Wave 1):**
- `[ISSUE] Codex: write_codex_config_files + build_codex_command` — Config generation, hooks.json, config.toml, spawn command. Requires proto stubs from Wave 1.
- `[ISSUE] Codex: hook runtime dispatch (--runtime codex)` — Normalizer in hook.rs. Requires wire format research from Wave 1.

**Wave 3 (depends on Wave 2):**
- `[ISSUE] Codex: Haskell WASM spawn_codex tool + hook handler` — SpawnCodex.hs, role registration, AgentTypeCodex, hook dispatch arm. Requires Rust side complete.

**Wave 4 (depends on Wave 3):**
- `[ISSUE] Codex: E2E test — hooks` — codex-hooks e2e test. Full stack must be installed.
- `[ISSUE] Codex: E2E test — messaging` — codex-messaging e2e test.

**Wave 5 (parallel, depends on Wave 4):**
- `[ISSUE] Codex: documentation pass` — CLAUDE.md updates, codex-integration.md decision doc. After E2E tests pass.

---

## Risk Flags

1. **Hook wire format** — Codex's `pre_tool_use.rs` stdin/stdout format must be confirmed by reading the source before implementing the normalizer. Do not guess.
2. **Instructions delivery** — Confirm whether Codex reads `instructions` from `config.toml`. If not, instructions must be prepended to the initial prompt file.
3. **`codex fork` session ID** — Verify how to obtain the parent Codex session ID to pass to `codex fork`. May need to track it similarly to `ClaudeSessionRegistry`, or read it from session files Codex persists to disk.
4. **Stdin vs positional prompt** — For large specs, piping via stdin (`echo "$task" | codex exec -`) may be more reliable than `$(cat file)` in a tmux command string. Test both.
5. **Codex built-in review mismatch** — Confirmed: `codex exec review` supports `--base`, `--commit`, `--uncommitted`, `--model`, JSONL output, and `--output-last-message`, but it emits Codex-native review output and does not post ExoMonad review files. Use the normal Codex MCP reviewer path unless a future translator writes `.exo/reviews/pr_N.json`.
6. **context-mode and tilth MCP for Codex agents** — These servers must be manually added to `.exo/config.toml` as `[extra_mcp_servers]` before E2E tests run. Their stdio command paths need to be verified on the host system (`which tilth`, locate context-mode server entrypoint).

---

## Confirmed CLI Facts (from `codex --help` and `codex exec --help`)

- Non-interactive subcommand: `codex exec` (alias `e`)
- Full-autonomy flag: `--dangerously-bypass-approvals-and-sandbox`
- Sandbox mode flag: `-s danger-full-access` (alternative)
- Working directory: `--cd <DIR>`
- Model: `-m <MODEL>`
- JSON output: `--json` (JSONL events to stdout)
- Prompt: positional arg, or stdin when `-` is used
- Auth: system-wide via `codex login` — no env var needed at spawn time
- Context fork: `codex fork <session_id>` — full context inheritance supported
- Reviewer: use normal `codex exec ... "$(cat <prompt_file>)"` with `role=reviewer`; do not use bare `codex exec review` unless ExoMonad adds an output translator
- Plugin system: `codex plugin` — native plugins (in addition to hooks.json)
