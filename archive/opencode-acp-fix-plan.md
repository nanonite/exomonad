# OpenCode ACP Fix Plan

## Context

Two issues blocking OpenCode agent spawning discovered during live testing:

1. **Wrong server command** — `opencode_acp.rs` spawns `opencode acp` which exits immediately (it acts as a client, not a server). The correct command is `opencode serve`. Additionally the entire JSON-RPC HTTP layer is wrong — `opencode serve` serves a web UI, not a JSON API. The correct delivery mechanism is `opencode run --attach <url> "message"`.

2. **Git remote noise** — when no remote is configured, `ensure_branch_pushed` silently swallows the push failure. Agents in local-only repos get confused and require manual bare-repo setup. Should be automated.

## Verified facts (live testing, 2026-04-24)

- `opencode serve --port 0` prints `opencode server listening on http://127.0.0.1:4096` to **stdout** within ~200ms. The stdout pipe in `spawn_and_prompt` is already correct — only the command arg needs changing.
- `opencode acp` connects as a *client* and exits within ~1s — it is not a server.
- `opencode run --attach http://127.0.0.1:4096 "message"` successfully delivers a prompt to the running server and streams a response.
- All HTTP paths on the serve server return HTML (no JSON API exposed over HTTP).
- `extract_url` in `opencode_acp.rs` already handles the listening line correctly — no changes needed there.

---

## Fix 1 — `opencode_acp.rs`: wrong command + wrong delivery

**File:** [`rust/exomonad-core/src/services/opencode_acp.rs`](rust/exomonad-core/src/services/opencode_acp.rs)

### 1a. Change spawn command: `acp` → `serve`

```rust
// BEFORE
let mut child = Command::new("opencode")
    .arg("acp")
    .arg("--port").arg("0")
    // ...

// AFTER
let mut child = Command::new("opencode")
    .arg("serve")
    .arg("--port").arg("0")
    // ...
```

### 1b. Replace `initialize_and_prompt` with `opencode run --attach`

Remove the entire `initialize_and_prompt` fn and all its JSON-RPC structs (`AcpInitializeRequest`, `AcpNewSessionRequest`, `AcpPromptRequest`, `AcpResponse`, `AcpError`, and related param types). Replace with a subprocess call:

```rust
async fn deliver_prompt(base_url: &str, working_dir: &Path, prompt: &str) -> Result<()> {
    // Write prompt to temp file to avoid shell quoting issues
    let prompt_file = working_dir.join(".exo").join("opencode_prompt.tmp");
    tokio::fs::write(&prompt_file, prompt).await
        .context("Failed to write prompt file")?;

    let status = tokio::process::Command::new("sh")
        .arg("-c")
        .arg(format!(
            "opencode run --attach {} \"$(cat {})\"",
            shell_escape::escape(base_url.into()),
            shell_escape::escape(prompt_file.to_string_lossy().into())
        ))
        .current_dir(working_dir)
        .status()
        .await
        .context("Failed to run opencode run --attach")?;

    let _ = tokio::fs::remove_file(&prompt_file).await;

    if !status.success() {
        anyhow::bail!("opencode run --attach exited with: {}", status);
    }
    Ok(())
}
```

### 1c. Simplify `OpencodeAcpConnection`

Remove `session_id` field — not needed with `--attach`. Keep `base_url`, `agent_id`, `child`.

### 1d. Update `send_prompt`

Replace the HTTP POST body with the same `opencode run --attach` subprocess call used in `deliver_prompt`.

### 1e. Remove `reqwest` if only used here

```bash
grep -r "reqwest" rust/exomonad-core/src/
```

If only `opencode_acp.rs` uses it, remove from `rust/exomonad-core/Cargo.toml`.

---

## Fix 2 — Git remote auto-fallback

**File:** [`rust/exomonad-core/src/services/agent_control/mod.rs`](rust/exomonad-core/src/services/agent_control/mod.rs)

Add `ensure_remote_exists` — called once before `ensure_branch_pushed` in the spawn path:

```rust
pub(crate) async fn ensure_remote_exists(project_dir: &Path) {
    let output = tokio::process::Command::new("git")
        .args(["remote"])
        .current_dir(project_dir)
        .output()
        .await;

    let has_remote = output
        .map(|o| !String::from_utf8_lossy(&o.stdout).trim().is_empty())
        .unwrap_or(false);

    if has_remote {
        return;
    }

    let dir_name = project_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("repo");
    let bare_path = project_dir
        .parent()
        .unwrap_or(project_dir)
        .join(format!("{}.git-remote", dir_name));

    tracing::info!(
        path = %bare_path.display(),
        "No git remote configured — creating local bare repo as origin"
    );

    let _ = tokio::process::Command::new("git")
        .args(["init", "--bare", bare_path.to_str().unwrap_or("")])
        .output().await;

    let _ = tokio::process::Command::new("git")
        .args(["remote", "add", "origin", bare_path.to_str().unwrap_or("")])
        .current_dir(project_dir)
        .output().await;

    tracing::info!(path = %bare_path.display(), "Local bare repo set as origin");
}
```

**Call site** — [`rust/exomonad-core/src/services/agent_control/spawn.rs`](rust/exomonad-core/src/services/agent_control/spawn.rs): add before `ensure_branch_pushed`:

```rust
ensure_remote_exists(effective_project_dir).await;
ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;
```

---

## Files modified

| File | Change |
|---|---|
| `rust/exomonad-core/src/services/opencode_acp.rs` | `acp`→`serve`, replace JSON-RPC with `opencode run --attach`, remove reqwest structs, simplify `OpencodeAcpConnection` |
| `rust/exomonad-core/src/services/agent_control/mod.rs` | Add `ensure_remote_exists` |
| `rust/exomonad-core/src/services/agent_control/spawn.rs` | Call `ensure_remote_exists` before `ensure_branch_pushed` |
| `rust/exomonad-core/Cargo.toml` | Remove `reqwest` if unused elsewhere |

---

## Verification

```bash
# Build
cargo build -p exomonad-core

# Smoke test: opencode serve port capture
opencode serve --port 0 & PID=$!; sleep 2; kill $PID

# Integration test: in a repo with no remote, invoke fork_wave agent_type=opencode
# Expected: local bare repo auto-created, OpenCode agent tmux window appears, no ACP port error
```
