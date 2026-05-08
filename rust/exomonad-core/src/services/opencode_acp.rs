//! OpenCode headless server integration via `opencode serve` + HTTP REST API.
//!
//! `opencode serve --port 0` starts an HTTP server on a random port and prints:
//!   `opencode server listening on http://127.0.0.1:{port}`
//!
//! Delivery flow:
//!   1. Spawn `opencode serve --port 0 --cwd <worktree>`
//!   2. Capture port from stdout
//!   3. POST /session → session ID
//!   4. POST /session/{id}/prompt_async → 204 (fire-and-forget)
//!
//! Subsequent messages (notify_parent → worker): POST /session/{id}/prompt_async

use anyhow::{Context, Result};
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use tokio::process::Command;
use tokio::sync::RwLock;

use crate::domain::AgentName;

/// Relative path from project root to the chainlink TL protocol context file.
const CHAINLINK_TL_RELATIVE_PATH: &str = ".exo/roles/devswarm/context/chainlink-tl.md";

/// Metadata for a running OpenCode server connection.
#[derive(Debug, Clone)]
pub struct OpencodeAcpConnection {
    pub agent_id: AgentName,
    /// Base URL of the HTTP server (e.g., "http://127.0.0.1:54321").
    pub base_url: String,
    /// Session ID created on startup (e.g., "ses_abc123").
    pub session_id: String,
    /// Child process handle — kept alive so the server stays running.
    pub child: Arc<tokio::process::Child>,
}

/// Registry of OpenCode server connections, keyed by agent name.
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
        tracing::info!(
            agent = %agent_id,
            url = %conn.base_url,
            session = %conn.session_id,
            "Registering OpenCode server connection"
        );
        connections.insert(agent_id, Arc::new(conn));
    }

    pub async fn get(&self, agent_id: &str) -> Option<Arc<OpencodeAcpConnection>> {
        self.connections.read().await.get(agent_id).cloned()
    }

    pub async fn remove(&self, agent_id: &str) -> Option<Arc<OpencodeAcpConnection>> {
        let mut connections = self.connections.write().await;
        let removed = connections.remove(agent_id);
        if removed.is_some() {
            tracing::info!(agent = %agent_id, "Removed OpenCode server connection");
        }
        removed
    }
}

/// Spawn OpenCode in headless server mode, create a session, and send the initial prompt.
///
/// Returns the OpencodeAcpConnection for registry storage.
pub async fn spawn_and_prompt(
    agent_id: AgentName,
    working_dir: &Path,
    initial_prompt: &str,
    project_dir: &Path,
    env_vars: Vec<(String, String)>,
    model: Option<&str>,
) -> Result<OpencodeAcpConnection> {
    tracing::info!(
        agent = %agent_id,
        cwd = %working_dir.display(),
        model = ?model,
        "Spawning OpenCode server"
    );

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

    let stdout = child.stdout.take().context("No stdout on opencode serve child")?;
    let base_url = capture_port(stdout)
        .await
        .context("Failed to capture OpenCode server port")?;
    tracing::info!(agent = %agent_id, url = %base_url, "OpenCode server started");

    let http = reqwest::Client::new();

    let session_id = create_session(&http, &base_url, agent_id.as_str())
        .await
        .context("Failed to create OpenCode session")?;
    tracing::info!(agent = %agent_id, session = %session_id, "OpenCode session created");

    let augmented_prompt = inject_chainlink_tl_protocol(initial_prompt, project_dir).await;
    send_prompt_to_session(&http, &base_url, &session_id, &augmented_prompt, model)
        .await
        .context("Failed to deliver initial prompt to OpenCode session")?;
    tracing::info!(agent = %agent_id, session = %session_id, "Initial prompt delivered");

    Ok(OpencodeAcpConnection {
        agent_id,
        base_url,
        session_id,
        child: Arc::new(child),
    })
}

/// Send a prompt to an existing OpenCode server connection.
pub async fn send_prompt(base_url: &str, session_id: &str, prompt: &str) -> Result<()> {
    let http = reqwest::Client::new();
    send_prompt_to_session(&http, base_url, session_id, prompt, None).await
}

// ============================================================================
// Internal helpers
// ============================================================================

/// Create a session via POST /session. Returns session ID.
async fn create_session(http: &reqwest::Client, base_url: &str, title: &str) -> Result<String> {
    let url = format!("{}/session", base_url);
    let resp = http
        .post(&url)
        .json(&serde_json::json!({ "title": title }))
        .send()
        .await
        .context("POST /session request failed")?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await.unwrap_or_default();
        anyhow::bail!("POST /session returned {}: {}", status, body);
    }

    let body: serde_json::Value = resp.json().await.context("Failed to parse session response")?;
    let id = body["id"]
        .as_str()
        .context("Session response missing 'id' field")?
        .to_string();
    Ok(id)
}

/// Send a prompt to a session via POST /session/{id}/prompt_async (204, fire-and-forget).
///
/// Model string format: "provider/model-id" (e.g. "opencode-go/deepseek-v4-flash").
/// When None, omit model field and let opencode use its configured default.
async fn send_prompt_to_session(
    http: &reqwest::Client,
    base_url: &str,
    session_id: &str,
    prompt: &str,
    model: Option<&str>,
) -> Result<()> {
    let url = format!("{}/session/{}/prompt_async", base_url, session_id);

    let mut body = serde_json::json!({
        "parts": [{ "type": "text", "text": prompt }]
    });

    if let Some(m) = model {
        if let Some((provider_id, model_id)) = m.split_once('/') {
            body["model"] = serde_json::json!({
                "providerID": provider_id,
                "modelID": model_id
            });
        }
    }

    let resp = http
        .post(&url)
        .json(&body)
        .send()
        .await
        .context("POST /session/{id}/prompt_async request failed")?;

    let status = resp.status();
    if !status.is_success() {
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!(
            "POST /session/{}/prompt_async returned {}: {}",
            session_id,
            status,
            text
        );
    }

    Ok(())
}

/// Inject chainlink-tl.md from the project directory into the prompt (non-fatal).
async fn inject_chainlink_tl_protocol(prompt: &str, project_dir: &Path) -> String {
    let path = project_dir.join(CHAINLINK_TL_RELATIVE_PATH);
    match tokio::fs::read_to_string(&path).await {
        Ok(content) => {
            let protocol = strip_yaml_frontmatter(&content);
            format!("{}\n\n---\n\n{}", protocol, prompt)
        }
        Err(e) => {
            tracing::warn!(path = %path.display(), error = %e, "Failed to read chainlink TL protocol (non-fatal)");
            prompt.to_string()
        }
    }
}

/// Strip YAML frontmatter (delimited by `---` lines) from markdown content.
fn strip_yaml_frontmatter(content: &str) -> String {
    if content.starts_with("---") {
        if let Some(end) = content[3..].find("---") {
            content[3 + end + 3..].trim().to_string()
        } else {
            content.to_string()
        }
    } else {
        content.to_string()
    }
}

/// Read stdout until the listening address line appears.
async fn capture_port(stdout: tokio::process::ChildStdout) -> Result<String> {
    use tokio::io::{AsyncBufReadExt, BufReader};

    let reader = BufReader::new(stdout);
    let mut lines = reader.lines();

    let result = tokio::time::timeout(tokio::time::Duration::from_secs(15), async {
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(url) = extract_url(&line) {
                return Some(url);
            }
            tracing::debug!(line = %line, "OpenCode server startup output");
        }
        None
    })
    .await;

    match result {
        Ok(Some(url)) => Ok(url),
        Ok(None) => anyhow::bail!("OpenCode server did not report a listening address"),
        Err(_) => anyhow::bail!("Timed out waiting for OpenCode server port (15s)"),
    }
}

/// Extract URL from a log line like `opencode server listening on http://127.0.0.1:12345`.
fn extract_url(line: &str) -> Option<String> {
    if let Some(start) = line.find("http://") {
        let rest = &line[start..];
        let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
        let url = rest[..end].trim_end_matches(['.', ',', ':']);
        if !url.is_empty() {
            return Some(url.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_url_from_listening_line() {
        assert_eq!(
            extract_url("opencode server listening on http://127.0.0.1:54321"),
            Some("http://127.0.0.1:54321".to_string())
        );
        assert_eq!(
            extract_url("Server started at http://localhost:8080"),
            Some("http://localhost:8080".to_string())
        );
        assert_eq!(extract_url("no url here"), None);
    }

    #[test]
    fn test_strip_yaml_frontmatter_with_frontmatter() {
        let md = "---\npaths:\n  - \"**\"\n---\n\n# Title\n\nBody";
        assert_eq!(strip_yaml_frontmatter(md), "# Title\n\nBody");
    }

    #[test]
    fn test_strip_yaml_frontmatter_no_frontmatter() {
        let md = "# Title\n\nBody";
        assert_eq!(strip_yaml_frontmatter(md), "# Title\n\nBody");
    }

    #[test]
    fn test_strip_yaml_frontmatter_unclosed() {
        let md = "---\npaths:\n  - \"**\"\n\n# Title\n\nBody";
        assert_eq!(strip_yaml_frontmatter(md), md);
    }
}
