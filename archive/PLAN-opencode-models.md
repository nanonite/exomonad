# Plan: Per-role OpenCode model selection (`--tl-model`, `--worker-model`)

## Context

`exomonad init` already accepts `--tl=opencode` and `--worker=opencode` to choose the
agent runtime, but every opencode invocation in the codebase runs without `-m`,
so it falls back to opencode's default model. We want to pick the model per role:

```
exomonad init --tl=opencode --tl-model=anthropic/claude-sonnet-4-5 \
              --worker=opencode --worker-model=openrouter/deepseek/deepseek-r1
```

opencode's CLI supports this natively — `opencode run -m provider/model` and
`opencode serve -m provider/model` both accept the flag, and `opencode models
[provider]` enumerates the valid set.

User decisions captured during planning:

- **Separate flags** (`--tl-model`, `--worker-model`), not combined shorthand —
  opencode model IDs use slashes (`anthropic/claude-sonnet-4-5`), so dash-jamming
  the agent type and model would be ambiguous.
- **Validate at init time** by shelling out to `opencode models` and rejecting
  unknown `provider/model` strings before any tmux window is created.

## Approach

Thread two `Option<String>` values (`tl_model`, `worker_model`) from the CLI
through `Config` into all three opencode call-sites, validate up front, and
expose `exomonad models` for discovery.

Three opencode invocation sites that need the `-m` flag threaded through:

1. Root TL: [rust/exomonad/src/init.rs:651-669](rust/exomonad/src/init.rs#L651-L669) (`opencode run` / `opencode`).
2. Companions: [rust/exomonad/src/init.rs:982-988](rust/exomonad/src/init.rs#L982-L988) (`opencode run`).
3. Spawned children:
   - [rust/exomonad-core/src/services/opencode_acp.rs:62-96](rust/exomonad-core/src/services/opencode_acp.rs#L62-L96) (`opencode serve`).
   - [rust/exomonad-core/src/services/agent_control/internal.rs:275-309](rust/exomonad-core/src/services/agent_control/internal.rs#L275-L309) (worker-pane `opencode run`).

## Files to modify

### 1. CLI surface — [rust/exomonad/src/main.rs](rust/exomonad/src/main.rs)

Add to `Commands::Init`:

```rust
/// Model for the root TL when --tl=opencode (e.g. anthropic/claude-sonnet-4-5)
#[arg(long)]
tl_model: Option<String>,
/// Model for spawned workers when --worker=opencode
#[arg(long)]
worker_model: Option<String>,
```

Forward both to `init::run(...)`.

Add a sibling subcommand for discovery:

```rust
/// List models available to opencode (passes through to `opencode models`).
Models { provider: Option<String> },
```

Implementation: `tokio::process::Command::new("opencode").arg("models").args(provider).status()`.

### 2. Config — [rust/exomonad/src/config.rs](rust/exomonad/src/config.rs)

Extend `OpencodeConfig` (around line 82):

```rust
pub struct OpencodeConfig {
    #[serde(default)]
    pub use_embedded_key: bool,
    pub tl_model: Option<String>,
    pub worker_model: Option<String>,
}
```

Surface the resolved values on the `Config` struct so spawn paths can read them
without re-walking the raw config. Resolution order: CLI flag →
`config.local.toml` → `config.toml` → `None`.

### 3. Validation — new helper in [rust/exomonad/src/init.rs](rust/exomonad/src/init.rs)

```rust
async fn validate_opencode_model(model: &str) -> Result<()> {
    let out = tokio::process::Command::new("opencode")
        .args(["models"])
        .output().await
        .context("Failed to run `opencode models` for validation")?;
    if !out.status.success() {
        anyhow::bail!("`opencode models` exited {}: {}",
            out.status, String::from_utf8_lossy(&out.stderr));
    }
    let known: HashSet<&str> = std::str::from_utf8(&out.stdout)?
        .lines().map(str::trim).filter(|l| !l.is_empty()).collect();
    if !known.contains(model) {
        anyhow::bail!(
            "Unknown opencode model `{model}`. Run `exomonad models` to see the list."
        );
    }
    Ok(())
}
```

Call once for `tl_model` (only if `root_agent_type == OpenCode`) and once for
`worker_model` (only if `spawn_agent_type == OpenCode`), early in `init::run`,
before the tmux session is created.

### 4. Root TL invocation — [init.rs:651-669](rust/exomonad/src/init.rs#L651-L669)

Build a model fragment and inject into both OpenCode arms:

```rust
let opencode_model = config.opencode.tl_model.as_deref()
    .map(|m| format!(" --model {}", shell_escape::escape(m.into())))
    .unwrap_or_default();
// ...
(AgentType::OpenCode, Some(prompt)) =>
    format!("opencode run{yolo}{opencode_model} '{}'", prompt.replace('\'', "'\\''")),
(AgentType::OpenCode, None) =>
    format!("opencode{opencode_model}{yolo}"),
```

### 5. Companion invocation — [init.rs:982-988](rust/exomonad/src/init.rs#L982-L988)

The existing `companion.model` field is already plumbed for Claude/Gemini but
ignored for OpenCode. Honor it:

```rust
AgentType::OpenCode => {
    let model_flag = companion.model.as_deref()
        .map(|m| format!(" --model {}", m)).unwrap_or_default();
    format!("{env_prefix}opencode run{yolo}{model_flag}{task_part}")
}
```

### 6. Worker-model plumbing on `Services` / `AgentControlService`

Add `spawn_agent_model: Option<String>` next to `spawn_agent_type` on the
service struct (the same path used today in
[rust/exomonad-core/src/services/agent_control/](rust/exomonad-core/src/services/agent_control/)).
Initialize from `Config::opencode.worker_model` in `serve.rs` when the registry
is built (`opencode_acp_registry` is constructed at `serve.rs:923`).

### 7. Spawned worker — `opencode serve` argv

In [rust/exomonad-core/src/services/opencode_acp.rs](rust/exomonad-core/src/services/opencode_acp.rs)
extend `spawn_and_prompt`:

```rust
pub async fn spawn_and_prompt(
    agent_id: AgentName,
    working_dir: &Path,
    initial_prompt: &str,
    env_vars: Vec<(String, String)>,
    model: Option<&str>,           // NEW
) -> Result<OpencodeAcpConnection> {
    let mut cmd = Command::new("opencode");
    cmd.arg("serve").arg("--port").arg("0").arg("--cwd").arg(working_dir);
    if let Some(m) = model { cmd.arg("--model").arg(m); }
    // ...existing stdin/stdout/env wiring...
}
```

Update its single caller (the OpenCode subtree spawn path in `handlers/agent.rs`)
to pass `services.spawn_agent_model()`.

### 8. Worker pane — [internal.rs:275-309](rust/exomonad-core/src/services/agent_control/internal.rs#L275-L309)

`build_agent_command` builds the OpenCode worker-pane command. Inject the model
flag via a new optional parameter (or read `self.spawn_agent_model`):

```rust
AgentType::OpenCode => {
    let m = self.spawn_agent_model.as_deref()
        .map(|m| format!(" --model {}", m)).unwrap_or_default();
    format!("{} run{}{} \"$(cat {})\"", cmd, perms_flags, m, escaped_path)
}
```

Match the same pattern in the `--session ... --fork` branch above it.

## Verification

1. `just install-all-dev` — must build clean.
2. `exomonad models | head` — prints the opencode catalog (smoke test).
3. `exomonad init --tl=opencode --tl-model=does-not-exist` should fail with
   "Unknown opencode model …" before the tmux session opens.
4. In a scratch project:
   ```
   exomonad init --tl=opencode --tl-model=anthropic/claude-sonnet-4-5 \
                 --worker=opencode --worker-model=openrouter/deepseek/deepseek-r1
   ```
   - In the TL window: `ps -o args | grep opencode` shows `--model anthropic/claude-sonnet-4-5`.
   - From the TL: `fork_wave` an opencode child; verify the spawned `opencode serve`
     argv (visible via `ps`) includes `--model openrouter/deepseek/deepseek-r1`.
5. `cargo test --workspace` — existing tests keep passing.

## Out of scope

- `--variant` (reasoning-effort) flag passthrough.
- Per-child model overrides via `fork_wave` arguments. Could be a follow-up,
  but the initial feature is per-role static config.

## Issue decomposition

Sized so that each issue is a single focused PR a worker can complete and
file independently. Dependency graph:

```
[Foundation] ──→ [TL/companion init.rs] (parallel)
              ├─→ [Worker plumbing] ──→ [opencode_acp --model]
              │                     └─→ [internal.rs --model]
              └─→ [exomonad models subcommand] (parallel, could be folded in)
```

| # | Title | Depends on | Files |
|---|-------|-----------|-------|
| 1 | OpenCode model: config + CLI flags | — | `main.rs`, `config.rs` |
| 2 | OpenCode model: TL + companion + validation | #1 | `init.rs` |
| 3 | OpenCode model: worker plumbing on `Services` | #1 | `services/`, `serve.rs` |
| 4 | OpenCode model: thread to `opencode serve` | #3 | `opencode_acp.rs`, `handlers/agent.rs` |
| 5 | OpenCode model: thread to worker pane | #3 | `agent_control/internal.rs` |
| 6 | `exomonad models` subcommand | — (independent) | `main.rs` |
