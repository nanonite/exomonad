//! OpenCode HTTP-based ACP integration.
//!
//! OpenCode exposes an ACP server via `opencode acp --port 0`. This module
//! spawns the server, captures the listening port, and provides HTTP-based
//! prompt delivery.

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use tokio::process::Command;
use tokio::sync::RwLock;

use crate::domain::AgentName;

/// Metadata for an OpenCode ACP HTTP endpoint.
#[derive(Debug, Clone)]
pub struct OpencodeAcpConnection {
    pub agent_id: AgentName,
    /// Base URL of the ACP server (e.g., "http://127.0.0.1:54321").
    pub base_url: String,
    /// ACP session ID.
    pub session_id: String,
    /// Child process handle (kept alive so the server stays running).
    pub child: Arc<tokio::process::Child>,
}

/// Registry of OpenCode ACP HTTP connections, keyed by agent name.
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

/// ACP request types for OpenCode HTTP API.
#[derive(Serialize)]
struct AcpInitializeRequest {
    jsonrpc: String,
    method: String,
    params: InitializeParams,
    id: u64,
}

#[derive(Serialize)]
struct InitializeParams {
    protocol_version: String,
    client_info: ClientInfo,
}

#[derive(Serialize)]
struct ClientInfo {
    name: String,
    version: String,
}

#[derive(Serialize)]
struct AcpNewSessionRequest {
    jsonrpc: String,
    method: String,
    params: NewSessionParams,
    id: u64,
}

#[derive(Serialize)]
struct NewSessionParams {
    cwd: String,
}

#[derive(Serialize)]
struct AcpPromptRequest {
    jsonrpc: String,
    method: String,
    params: PromptParams,
    id: u64,
}

#[derive(Serialize)]
struct PromptParams {
    session_id: String,
    prompt: Vec<PromptContent>,
}

#[derive(Serialize)]
struct PromptContent {
    #[serde(rename = "type")]
    content_type: String,
    text: String,
}

#[derive(Deserialize, Debug)]
struct AcpResponse {
    result: Option<serde_json::Value>,
    error: Option<AcpError>,
    id: Option<u64>,
}

#[derive(Deserialize, Debug)]
struct AcpError {
    message: String,
}

/// Spawn OpenCode in headless ACP server mode and send the initial prompt.
///
/// Runs `opencode acp --port 0 --cwd <worktree>`, captures the listening port
/// from stdout, initializes the ACP connection, creates a session, and sends
/// the initial prompt.
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
        .arg("acp")
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
        .context("Failed to spawn opencode acp")?;

    let stdout = child.stdout.take().context("No stdout on child")?;

    // Capture the port from stdout
    let base_url = capture_port(stdout)
        .await
        .context("Failed to capture OpenCode ACP port")?;

    tracing::info!(agent = %agent_id, url = %base_url, "OpenCode ACP server started");

    // Keep child alive
    let child = Arc::new(child);

    // Initialize ACP connection
    let session_id = initialize_and_prompt(&base_url, working_dir, initial_prompt)
        .await
        .context("Failed to initialize OpenCode ACP session")?;

    tracing::info!(agent = %agent_id, session_id = %session_id, "OpenCode ACP session created and prompt sent");

    Ok(OpencodeAcpConnection {
        agent_id,
        base_url,
        session_id,
        child,
    })
}

/// Read stdout until we find the listening address line.
async fn capture_port(stdout: tokio::process::ChildStdout) -> Result<String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let reader = BufReader::new(stdout);
    let mut lines = reader.lines();

    // Wait up to 10 seconds for the port line
    let timeout = tokio::time::Duration::from_secs(10);
    let result = tokio::time::timeout(timeout, async {
        while let Ok(Some(line)) = lines.next_line().await {
            // Look for patterns like "Listening on http://127.0.0.1:12345" or "http://localhost:12345"
            if let Some(url) = extract_url(&line) {
                return Some(url);
            }
            // Also pass through to stderr so we don't lose debug output
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
    // Try to find http:// URL in the line
    if let Some(start) = line.find("http://") {
        let rest = &line[start..];
        // URL ends at whitespace or end of line
        let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
        let url = &rest[..end];
        // Remove trailing punctuation if any
        let url = url.trim_end_matches(|c: char| c == '.' || c == ',' || c == ':');
        if !url.is_empty() {
            return Some(url.to_string());
        }
    }
    None
}

/// Initialize ACP, create session, and send initial prompt.
async fn initialize_and_prompt(
    base_url: &str,
    working_dir: &Path,
    prompt: &str,
) -> Result<String> {
    let client = reqwest::Client::new();

    // Step 1: Initialize
    let init_req = AcpInitializeRequest {
        jsonrpc: "2.0".to_string(),
        method: "initialize".to_string(),
        params: InitializeParams {
            protocol_version: "2024-11-05".to_string(),
            client_info: ClientInfo {
                name: "exomonad".to_string(),
                version: env!("CARGO_PKG_VERSION").to_string(),
            },
        },
        id: 1,
    };

    let resp = client
        .post(format!("{}/acp", base_url))
        .json(&init_req)
        .send()
        .await
        .context("ACP initialize request failed")?;

    let body: AcpResponse = resp
        .json()
        .await
        .context("Failed to parse ACP initialize response")?;

    if let Some(err) = body.error {
        anyhow::bail!("ACP initialize error: {}", err.message);
    }

    // Step 2: Create session
    let session_req = AcpNewSessionRequest {
        jsonrpc: "2.0".to_string(),
        method: "session/new".to_string(),
        params: NewSessionParams {
            cwd: working_dir
                .to_string_lossy()
                .to_string(),
        },
        id: 2,
    };

    let resp = client
        .post(format!("{}/acp", base_url))
        .json(&session_req)
        .send()
        .await
        .context("ACP session/new request failed")?;

    let body: AcpResponse = resp
        .json()
        .await
        .context("Failed to parse ACP session/new response")?;

    if let Some(err) = body.error {
        anyhow::bail!("ACP session/new error: {}", err.message);
    }

    let session_id = body
        .result
        .and_then(|r| r.get("sessionId").and_then(|s| s.as_str()).map(|s| s.to_string()))
        .context("No sessionId in ACP session/new response")?;

    // Step 3: Send prompt
    let prompt_req = AcpPromptRequest {
        jsonrpc: "2.0".to_string(),
        method: "session/prompt".to_string(),
        params: PromptParams {
            session_id: session_id.clone(),
            prompt: vec![PromptContent {
                content_type: "text".to_string(),
                text: prompt.to_string(),
            }],
        },
        id: 3,
    };

    let resp = client
        .post(format!("{}/acp", base_url))
        .json(&prompt_req)
        .send()
        .await
        .context("ACP session/prompt request failed")?;

    let body: AcpResponse = resp
        .json()
        .await
        .context("Failed to parse ACP session/prompt response")?;

    if let Some(err) = body.error {
        anyhow::bail!("ACP session/prompt error: {}", err.message);
    }

    Ok(session_id)
}

/// Send a prompt to an existing OpenCode ACP session via HTTP.
pub async fn send_prompt(
    base_url: &str,
    session_id: &str,
    prompt: &str,
) -> Result<()> {
    let client = reqwest::Client::new();

    let prompt_req = AcpPromptRequest {
        jsonrpc: "2.0".to_string(),
        method: "session/prompt".to_string(),
        params: PromptParams {
            session_id: session_id.to_string(),
            prompt: vec![PromptContent {
                content_type: "text".to_string(),
                text: prompt.to_string(),
            }],
        },
        id: 4,
    };

    let resp = client
        .post(format!("{}/acp", base_url))
        .json(&prompt_req)
        .send()
        .await
        .context("ACP session/prompt request failed")?;

    let body: AcpResponse = resp
        .json()
        .await
        .context("Failed to parse ACP session/prompt response")?;

    if let Some(err) = body.error {
        anyhow::bail!("ACP session/prompt error: {}", err.message);
    }

    Ok(())
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
