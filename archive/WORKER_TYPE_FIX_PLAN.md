# Fix: `--worker=opencode` is ignored — `spawn_worker` always spawns Gemini

## Context

`exomonad init --tl=opencode --worker=opencode` is supposed to make every spawned worker an OpenCode agent. The CLI flags parse correctly and update config, but workers still come up as Gemini (server logs show `mvp-room-gemini`, `mvp-player-gemini`). The user wants those two flags to act as a hard override on the spawn agent type.

The last commit ([34a7322a](rust/exomonad-core/src/handlers/agent.rs)) added per-call `agent_type` plumbing through proto + Rust handler, but did **not** wire `config.spawn_agent_type` to be the *default* when the caller omits it. The Rust handler still falls back to a hardcoded `Gemini`.

## Root Cause

The `--worker` flag flows correctly into `AgentControlService`, but never reaches the spawn path:

1. **CLI → Config**: [`init.rs:32`](rust/exomonad/src/init.rs#L32) sets `config.spawn_agent_type = parse_agent_type(worker_type)` ✅
2. **Config → Service**: [`serve.rs:941`](rust/exomonad/src/serve.rs#L941) calls `agent_control.with_spawn_agent_type(config.spawn_agent_type)` ✅
3. **Haskell tool `spawn_worker`** ([Spawn.hs:543](haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs#L543)) has no `agent_type` field — `WorkerSpec` is built with `wsType = Nothing` ❌
4. **Proto `SpawnWorkerRequest.agent_type`** therefore arrives unspecified ❌
5. **Rust handler** ([handlers/agent.rs:376](rust/exomonad-core/src/handlers/agent.rs#L376)) does `convert_agent_type(req.agent_type()).unwrap_or(ServiceAgentType::Gemini)` — **hardcoded Gemini fallback**, ignores `config.spawn_agent_type` ❌

The same flaw exists for `fork_wave`: it accepts an explicit `agent_type`, but defaults to `Claude` ([Spawn.hs:134](haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs#L134) — `fromMaybe AC.Claude`) when the caller omits it.

`--tl=opencode` is fine — `init.rs` has `root_agent_type` branches at 182/248/354/504/644 that select the right command for the TL window.

## Fix

Single change: when the caller does not specify an explicit `agent_type`, fall back to the configured `spawn_agent_type` instead of a hardcoded value. The config already plumbs through to `AgentControlService`, so the spawn handler has it available.

### Changes

**1. [`rust/exomonad-core/src/services/agent_control/mod.rs`](rust/exomonad-core/src/services/agent_control/mod.rs)** — add a public getter

If absent, add a one-line accessor:
```rust
pub fn default_spawn_agent_type(&self) -> AgentType {
    self.spawn_agent_type
}
```

**2. [`rust/exomonad-core/src/handlers/agent.rs:376`](rust/exomonad-core/src/handlers/agent.rs#L376)** — `spawn_worker` handler

Replace:
```rust
agent_type: convert_agent_type(req.agent_type()).unwrap_or(ServiceAgentType::Gemini),
```
with a config-aware default:
```rust
let default_type = self.agent_control.default_spawn_agent_type();
agent_type: convert_agent_type(req.agent_type()).unwrap_or(default_type),
```

**3. Same change in the `fork_wave` / `spawn_subtree` handler path** — wherever the proto `agent_type` is converted in [`rust/exomonad-core/src/handlers/agent.rs`](rust/exomonad-core/src/handlers/agent.rs). Search `convert_agent_type(.*).unwrap_or` and apply the same fallback. The default for fork_wave was historically `Claude`; it should now be `config.spawn_agent_type` so `--worker=opencode` covers both tools uniformly.

**4. Verify**: do **not** touch the Haskell tool args. The Haskell tool intentionally omits `agent_type` (it's a server-side default, not a per-call knob for `spawn_worker`). Adding it would re-introduce the failure mode the user just hit (caller forgets to pass it → Gemini).

### Files to Modify

| File | Change |
|------|--------|
| [`rust/exomonad-core/src/services/agent_control/mod.rs`](rust/exomonad-core/src/services/agent_control/mod.rs) | Add `pub fn default_spawn_agent_type(&self) -> AgentType` if absent |
| [`rust/exomonad-core/src/handlers/agent.rs`](rust/exomonad-core/src/handlers/agent.rs) | Replace hardcoded `Gemini` / `Claude` fallback with `agent_control.default_spawn_agent_type()` in `spawn_worker` and `fork_wave`/`spawn_subtree` handlers |

### Files NOT to Modify

- [`haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs`](haskell/wasm-guest/src/ExoMonad/Guest/Tools/Spawn.hs) — tool args stay as-is
- `proto/effects/agent.proto` — schema already supports optional `agent_type` (added in 34a7322a)
- [`rust/exomonad/src/init.rs`](rust/exomonad/src/init.rs) — `--tl` / `--worker` parsing already works
- [`rust/exomonad/src/serve.rs`](rust/exomonad/src/serve.rs) — config already plumbs through

## Verification

End-to-end test that the bug is fixed:

```bash
# 1. Build
just install-all-dev

# 2. Set up a fresh project
cd /tmp && mkdir worker-fix-test && cd worker-fix-test && git init
exomonad new

# 3. Init with the flags that previously failed
exomonad init --tl=opencode --worker=opencode

# 4. From the TL window, ask the agent to spawn a worker
#    (or call the MCP tool directly via curl)
curl --unix-socket .exo/server.sock -X POST \
  http://localhost/agents/root/root/tools/call \
  -H 'Content-Type: application/json' \
  -d '{"name":"spawn_worker","arguments":{"name":"probe","task":"echo hello"}}'

# 5. Check the spawned worker's tmux pane title and branch:
tmux list-windows -t exomonad
# Expect: "probe-opencode" (NOT "probe-gemini")

# 6. Server log should show:
#    "Spawning worker name=probe agent_type=OpenCode"
#    NOT "agent_type=Gemini"
```

Also run unit tests:
```bash
cargo test -p exomonad-core handlers::agent
```

## Out of Scope

- Adding `agent_type` to `SpawnWorkerToolArgs` (deliberate — server config is the right level for this knob).
- Reworking how `root_agent_type` selects the TL command — that path already works.
- Per-companion agent_type overrides — not requested.
