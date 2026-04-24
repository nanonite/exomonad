//! OpenCode ACP integration via `opencode serve` + `opencode run --attach`.
//!
//! OpenCode exposes a headless server via `opencode serve --port 0`. This module
//! spawns the server, captures the listening port, and delivers prompts using
//! `opencode run --attach <url> "message"`.

use anyhow::{Context, Result};
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use tokio::process::Command;
use tokio::sync::RwLock;

use crate::domain::AgentName;

/// Metadata for an OpenCode ACP server connection.
#[derive(Debug, Clone)]
pub struct OpencodeAcpConnection {
    pub agent_id: AgentName,
    /// Base URL of the ACP server (e.g., "http://127.0.0.1:54321").
    pub base_url: String,
    /// Child process handle (kept alive so the server stays running).
    pub child: Arc<tokio::process::Child>,
}

/// Registry of OpenCode ACP connections, keyed by agent name.
#[derive(Debug, Clone, Default)]
pub struct OpencodeAcpRegistry {
    connections: Arc<RwLock<std::collections::HashMap<AgentName, Arc<OpencodeAcpConnection>>>>,
}

impl OpencodeAcpRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn register(&self, conn: OpencodeAcpConnection) {
        let agent_id = conn.agent_id.clone();
        let mut connections = self.connections.write().await;
        tracing::info!(agent = %agent_id, url = %conn.base_url, "Registering OpenCode ACP connection");
        connections.insert(agent_id, Arc::new(conn));
    }

    pub async fn get(&self, agent_id: &str) -> Option<Arc<OpencodeAcpConnection>> {
        self.connections.read().await.get(agent_id).cloned()
    }

    pub async fn remove(&self, agent_id: &str) -> Option<Arc<OpencodeAcpConnection>> {
        let mut connections = self.connections.write().await;
        let removed = connections.remove(agent_id);
        if removed.is_some() {
            tracing::info!(agent = %agent_id, "Removed OpenCode ACP connection");
        }
        removed
    }
}

/// Spawn OpenCode in headless server mode and send the initial prompt.
///
/// Runs `opencode serve --port 0 --cwd <worktree>`, captures the listening port
/// from stdout, and delivers the initial prompt via `opencode run --attach`.
///
/// Returns the OpencodeAcpConnection for registry storage.
pub async fn spawn_and_prompt(
    agent_id: AgentName,
    working_dir: &Path,
    initial_prompt: &str,
    env_vars: Vec<(String, String)>,
) -> Result<OpencodeAcpConnection> {
    tracing::info!(agent = %agent_id, cwd = %working_dir.display(), "Spawning OpenCode ACP server");

    let mut child = Command::new("opencode")
        .arg("serve")
        .arg("--port")
        .arg("0")
        .arg("--cwd")
        .arg(working_dir)
        .current_dir(working_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .envs(env_vars)
        .spawn()
        .context("Failed to spawn opencode serve")?;

    let stdout = child.stdout.take().context("No stdout on child")?;

    let base_url = capture_port(stdout)
        .await
        .context("Failed to capture OpenCode ACP port")?;

    tracing::info!(agent = %agent_id, url = %base_url, "OpenCode ACP server started");

    deliver_prompt(&base_url, working_dir, initial_prompt)
        .await
        .context("Failed to deliver initial prompt via opencode run --attach")?;

    tracing::info!(agent = %agent_id, url = %base_url, "OpenCode ACP prompt delivered");

    let child = Arc::new(child);

    Ok(OpencodeAcpConnection {
        agent_id,
        base_url,
        child,
    })
}

/// Read stdout until we find the listening address line.
async fn capture_port(stdout: tokio::process::ChildStdout) -> Result<String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let reader = BufReader::new(stdout);
    let mut lines = reader.lines();

    let timeout = tokio::time::Duration::from_secs(10);
    let result = tokio::time::timeout(timeout, async {
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(url) = extract_url(&line) {
                return Some(url);
            }
            tracing::debug!(line = %line, "OpenCode ACP startup output");
        }
        None
    })
    .await;

    match result {
        Ok(Some(url)) => Ok(url),
        Ok(None) => anyhow::bail!("OpenCode ACP did not report a listening address within 10s"),
        Err(_) => anyhow::bail!("Timed out waiting for OpenCode ACP port"),
    }
}

/// Extract URL from a log line like "Listening on http://127.0.0.1:12345"
fn extract_url(line: &str) -> Option<String> {
    if let Some(start) = line.find("http://") {
        let rest = &line[start..];
        let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
        let url = &rest[..end];
        let url = url.trim_end_matches(['.', ',', ':']);
        if !url.is_empty() {
            return Some(url.to_string());
        }
    }
    None
}

/// Deliver a prompt to an OpenCode serve instance via `opencode run --attach`.
///
/// Writes the prompt to a temp file to avoid shell quoting issues, then runs
/// `opencode run --attach <url> "$(cat <file>)"`.
async fn deliver_prompt(base_url: &str, working_dir: &Path, prompt: &str) -> Result<()> {
    let prompt_file = working_dir.join(".exo").join("opencode_prompt.tmp");
    tokio::fs::create_dir_all(prompt_file.parent().unwrap())
        .await
        .context("Failed to create .exo directory")?;
    tokio::fs::write(&prompt_file, prompt)
        .await
        .context("Failed to write prompt file")?;

    let escaped_url = shell_escape::escape(base_url.into());
    let escaped_file = shell_escape::escape(prompt_file.to_string_lossy());
    let shell_cmd = format!(
        "opencode run --attach {} \"$(cat {})\"",
        escaped_url, escaped_file
    );

    let status = Command::new("sh")
        .arg("-c")
        .arg(&shell_cmd)
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

/// Send a prompt to an existing OpenCode ACP server via `opencode run --attach`.
pub async fn send_prompt(base_url: &str, prompt: &str) -> Result<()> {
    deliver_prompt(base_url, Path::new("."), prompt).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_url_from_listening_line() {
        assert_eq!(
            extract_url("Listening on http://127.0.0.1:54321"),
            Some("http://127.0.0.1:54321".to_string())
        );
        assert_eq!(
            extract_url("Server started at http://localhost:8080"),
            Some("http://localhost:8080".to_string())
        );
        assert_eq!(extract_url("no url here"), None);
    }
}
