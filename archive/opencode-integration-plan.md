# OpenCode Integration Plan

## Background

OpenCode (`opencode` CLI) is supported as `AgentType::OpenCode` but agents crash immediately on spawn and message routing is broken. This plan tracks the fix across four milestones.

### Root Cause Summary

| Problem | Location | Effect |
|---|---|---|
| Invalid `--dangerously-skip-permissions` flag | `internal.rs:243-249` | Process exits immediately (zombie) |
| Invalid `--resume`/`--fork-session` flags | `internal.rs:258-269` | Fork-session path also crashes |
| No TeamRegistry entry for OpenCode TL | `handlers/agent.rs` | `notify_parent` routing silently drops messages |
| `propagate_team_to_child` Claude-only | `handlers/agent.rs:129-189` | Routing broken at depth > 1 |
| `spawn_opencode` duplicates `fork_wave` path | `spawn.rs:1017-1140` + `handlers/agent.rs:582-635` | Two diverged paths, bugs fixed in one missed in other |

### OpenCode CLI Reference (verified)

```
opencode run [message..]          # one-shot: prompt as positional args
opencode run --session <id>       # continue a session
opencode run --session <id> --fork  # fork a session (≈ claude --resume --fork-session)
opencode acp --port 0 --cwd <dir> # headless ACP server (bidirectional, dynamic port)
opencode serve                    # headless HTTP server
```

`--dangerously-skip-permissions` does **not** exist. Permissions are not a flag in OpenCode.

---

## Milestone 1 — Tier 1: Fix OpenCode Crash
**Chainlink: #1**  
**Issues: #1, #2**  
**Effort: ~1 day**  
**Unblocks: everything**

### Issue #1 — Strip invalid `--dangerously-skip-permissions` flag

**File:** [rust/exomonad-core/src/services/agent_control/internal.rs:243-249](rust/exomonad-core/src/services/agent_control/internal.rs#L243-L249)

```rust
// BEFORE
AgentType::OpenCode => {
    if yolo {
        " --dangerously-skip-permissions".to_string()
    } else {
        String::new()
    }
}

// AFTER
AgentType::OpenCode => String::new(),
```

OpenCode has no permissions flag. Remove the `yolo` branch entirely for this arm.

### Issue #2 — Fix fork-session flags

**File:** [rust/exomonad-core/src/services/agent_control/internal.rs:258-269](rust/exomonad-core/src/services/agent_control/internal.rs#L258-L269)

```rust
// BEFORE (invalid for OpenCode)
AgentType::OpenCode => {
    format!(
        "{} run{} --resume {} --fork-session \"$(cat {})\"",
        cmd, perms_flags, escaped_session, escaped_path
    )
}

// AFTER
AgentType::OpenCode => {
    format!(
        "{} run --session {} --fork \"$(cat {})\"",
        cmd, escaped_session, escaped_path
    )
}
```

Note: parent session ID must be exposed by the parent OpenCode process at spawn time (not yet implemented — the fork path can be left unused until Milestone 4 establishes ACP-based context passing).

---

## Milestone 2 — Tier 2: Routing Fix
**Chainlink: #2**  
**Issues: #3, #4, #5**  
**Effort: ~2-3 days**  
**Depends on: Milestone 1**

### Issue #3 — Server-side auto-register OpenCode agents in AgentStore

**File:** [rust/exomonad-core/src/handlers/agent.rs](rust/exomonad-core/src/handlers/agent.rs)

In the `spawn_subtree` handler, after `finalize_spawn` succeeds for `AgentType::OpenCode`, directly insert an `AgentIdentityRecord` into `AgentStore`. This is what Claude gets via the `SessionStart` hook → `ClaudeSessionRegistry` → `TeamCreate` chain — OpenCode bypasses that entirely by having the server populate it at spawn time.

The record is already constructed in `spawn.rs:850-859` (`identity_record`). The handler just needs to write it through without waiting for the agent to self-register.

### Issue #4 — tmux STDIN injection as primary delivery for OpenCode

**File:** delivery routing layer (search `deliver_to_agent`)

Claude path: Teams inbox → tmux STDIN fallback  
OpenCode path: tmux STDIN (primary, no fallback needed until Milestone 4)

Add an early branch in `deliver_to_agent`:
```rust
if identity.agent_type == AgentType::OpenCode {
    return self.inject_tmux_stdin(&identity.window_id, message).await;
}
```

This reuses the already-working tmux injection path that currently serves as the fallback.

### Issue #5 — Propagate routing chain to OpenCode children

**File:** [rust/exomonad-core/src/handlers/agent.rs:129-189](rust/exomonad-core/src/handlers/agent.rs#L129-L189)

`propagate_team_to_child` is called for Claude children only. Add an equivalent `propagate_agent_store_to_child` for OpenCode: when an OpenCode TL spawns an OpenCode child, copy the parent's `AgentStore` entry into the child's routing context so `notify_parent` can resolve the parent at depth > 1.

---

## Milestone 3 — Cleanup: Remove `spawn_opencode`
**Chainlink: #3**  
**Issues: #6, #7, #8**  
**Effort: ~1 day**  
**Can run parallel with Milestone 2**

`spawn_opencode` is a diverged duplicate of `fork_wave agent_type=opencode`. Two paths means bugs fixed in one get missed in the other. Remove it entirely.

### Issue #6 — Remove Haskell tool

**File:** [haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs](haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs)

Remove:
- `spawnOpencodeCore` function
- `SpawnOpencodeC` effect constructor
- `spawn_opencode` tool registration in `AllTools`
- `SpawnOpencodeConfig` type

### Issue #7 — Remove Rust handler

**File:** [rust/exomonad-core/src/handlers/agent.rs:582-635](rust/exomonad-core/src/handlers/agent.rs#L582-L635)

Remove the `spawn_opencode` HTTP handler and its route registration. Check for `register_synthetic_member` / `register_child_supervisor` calls — remove them here but verify they're still wired for the `fork_wave` path (Milestone 2 adds the equivalent directly there).

### Issue #8 — Remove Rust implementation

**File:** [rust/exomonad-core/src/services/agent_control/spawn.rs:1017-1140](rust/exomonad-core/src/services/agent_control/spawn.rs#L1017-L1140)

Remove `pub async fn spawn_opencode`. Also remove `generate_opencode_tl_settings` if its only caller is `spawn_opencode` (verify with grep before deleting).

---

## Milestone 4 — Tier 3: ACP Integration
**Chainlink: #4**  
**Issues: #9, #10, #11, #12**  
**Effort: ~1 week**  
**Depends on: Milestones 1, 2, 3**

OpenCode has a native ACP (Agent Client Protocol) server: `opencode acp`. The exomonad codebase already has `AcpRegistry` and `connect_and_prompt()` serving Gemini agents. This milestone plugs OpenCode into the same path for true bidirectional messaging.

### Issue #9 — Spawn OpenCode in headless ACP mode

**File:** [rust/exomonad-core/src/services/agent_control/spawn.rs](rust/exomonad-core/src/services/agent_control/spawn.rs) + [internal.rs](rust/exomonad-core/src/services/agent_control/internal.rs)

Replace the `opencode run ...` command with:
```
opencode acp --port 0 --cwd <worktree_path>
```

`--port 0` assigns a random free port. The process stays alive (server mode) instead of exiting after the prompt. Update `build_agent_command` and the OpenCode spawn branch.

### Issue #10 — Capture ACP port and register in AcpRegistry

**File:** spawn path, new helper

After the tmux window opens, read stdout from the OpenCode process to capture the listening address (e.g. `Listening on 127.0.0.1:XXXXX`). Parse the port, construct `http://127.0.0.1:XXXXX`, and call `AcpRegistry::register(agent_name, url)`.

Options for capturing stdout before window detach:
- Pipe the process through a port-capture script that writes port to a temp file, then reads it
- Use `opencode acp --print-logs` to get structured output on stderr

Reference the existing Gemini ACP registration for the `AcpRegistry` API.

### Issue #11 — Deliver initial task via ACP connect_and_prompt()

**File:** spawn path, after Issue #10

Once ACP is registered, use `AcpRegistry::connect_and_prompt(agent_name, task)` to send the initial task. This replaces the `$(cat prompt_file)` approach entirely for ACP-mode agents.

Reference: `services/external/acp.rs` — `connect_and_prompt()` already handles the full ACP handshake for Gemini.

### Issue #12 — Wire ACP into send_message/notify_parent routing

**File:** delivery routing layer

Update `deliver_to_agent` for `AgentType::OpenCode`:

```
AcpRegistry has entry?  →  ACP delivery (connect_and_prompt)
otherwise               →  tmux STDIN injection (fallback from Milestone 2)
```

This gives OpenCode agents the same delivery priority as Gemini: ACP when available, tmux when not.

---

## Sequencing

```
Milestone 1 (crash fix)
    │
    ├── Milestone 2 (routing)  ──┐
    │                            ├── Milestone 4 (ACP)
    └── Milestone 3 (cleanup)  ──┘
```

Milestones 2 and 3 touch different files and can be done in parallel. Milestone 4 depends on all three since it replaces the spawn command (M1 fix), relies on routing infrastructure (M2), and removes the redundant path (M3) before building on top.
