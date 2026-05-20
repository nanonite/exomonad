//! MCP stdio translation layer.
//!
//! Speaks JSON-RPC (MCP protocol) on stdin/stdout, translates to
//! domain-typed REST calls against the UDS server.

use crate::uds_client::{self, ServerClient, ToolCallRequest};
use anyhow::Result;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Stdout};
use tokio::sync::Mutex as AsyncMutex;
use tracing::{debug, error, info, Instrument};

struct LazyServerClient {
    client: Option<ServerClient>,
}

impl LazyServerClient {
    fn new() -> Self {
        Self { client: None }
    }

    async fn get(&mut self, role: &str, name: &str) -> Result<&ServerClient> {
        if self.client.is_none() {
            self.client = Some(connect_with_retry(role, name).await?);
        }

        self.client
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("Server client initialization did not complete"))
    }
}

async fn write_json_line(stdout: &Arc<AsyncMutex<Stdout>>, value: &Value) -> Result<()> {
    let mut stdout = stdout.lock().await;
    stdout.write_all(&serde_json::to_vec(value)?).await?;
    stdout.write_all(b"\n").await?;
    stdout.flush().await?;
    Ok(())
}

fn tools_list_changed_notification() -> Value {
    json!({
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed"
    })
}

fn start_tool_list_changed_watcher(role: String, name: String, stdout: Arc<AsyncMutex<Stdout>>) {
    tokio::spawn(async move {
        let mut client: Option<ServerClient> = None;
        let mut last_hash: Option<String> = None;

        loop {
            if client.is_none() {
                match connect_with_retry(&role, &name).await {
                    Ok(next_client) => client = Some(next_client),
                    Err(err) => {
                        debug!(role = %role, name = %name, error = %err, "tools/list_changed watcher could not connect");
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                        continue;
                    }
                }
            }

            let Some(active_client) = client.as_ref() else {
                continue;
            };

            match active_client.health_info().await {
                Ok(health) => {
                    if let Some(previous_hash) = &last_hash {
                        if previous_hash != &health.wasm_hash {
                            let notification = tools_list_changed_notification();
                            if let Err(err) = write_json_line(&stdout, &notification).await {
                                error!(role = %role, name = %name, error = %err, "failed to send tools/list_changed notification");
                                return;
                            }
                            info!(role = %role, name = %name, old_hash = %previous_hash, new_hash = %health.wasm_hash, "sent tools/list_changed notification");
                        }
                    }
                    last_hash = Some(health.wasm_hash);
                }
                Err(err) => {
                    debug!(role = %role, name = %name, error = %err, "tools/list_changed watcher health probe failed");
                    client = None;
                }
            }

            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        }
    });
}

async fn connect_with_retry(role: &str, name: &str) -> Result<ServerClient> {
    let started_at = std::time::Instant::now();
    let mut attempts = 0;
    let mut last_error = String::from("socket discovery has not run");
    info!(
        role,
        name, "mcp-stdio lazy init: beginning server socket discovery"
    );

    loop {
        let candidate = match uds_client::find_server_socket() {
            Ok(socket) => {
                debug!(role, name, socket = %socket.display(), "mcp-stdio lazy init: discovered server socket candidate");
                Some(socket)
            }
            Err(err) => {
                last_error = err.to_string();
                debug!(role, name, error = %err, "mcp-stdio lazy init: server socket discovery failed");
                None
            }
        };

        if let Some(socket) = candidate {
            let trial = ServerClient::new(socket.clone());
            match trial.health_check().await {
                Ok(()) => {
                    info!(
                        role,
                        name,
                        socket = %socket.display(),
                        attempts = attempts + 1,
                        elapsed_ms = started_at.elapsed().as_millis(),
                        "mcp-stdio lazy init: server health check succeeded"
                    );
                    return Ok(trial);
                }
                Err(err) => {
                    last_error = err.to_string();
                    debug!(
                        role,
                        name,
                        socket = %socket.display(),
                        attempt = attempts + 1,
                        max_attempts = 30,
                        error = %err,
                        "mcp-stdio lazy init: server health check failed"
                    );
                }
            }
        } else {
            debug!(
                role,
                name,
                attempt = attempts + 1,
                max_attempts = 30,
                error = %last_error,
                "mcp-stdio lazy init: no server socket candidate"
            );
        }

        attempts += 1;
        if attempts >= 30 {
            error!(
                role,
                name,
                attempts,
                elapsed_ms = started_at.elapsed().as_millis(),
                last_error = %last_error,
                "mcp-stdio lazy init: server socket unreachable"
            );
            anyhow::bail!(
                "Server socket not reachable after 15s (discovery or health check failed). \
                 Is exomonad serve running? Last error: {}",
                last_error
            );
        }

        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
}

/// Run the stdio MCP translation layer.
///
/// Reads JSON-RPC from stdin, translates to REST calls against the server,
/// writes JSON-RPC responses to stdout.
pub async fn run(role: &str, name: &str) -> Result<()> {
    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin).lines();
    let stdout = Arc::new(AsyncMutex::new(tokio::io::stdout()));
    let mut lazy_client = LazyServerClient::new();
    let mut tool_watcher_started = false;

    while let Some(line) = reader.next_line().await? {
        if line.trim().is_empty() {
            continue;
        }

        let msg: Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("Invalid JSON from stdin: {}", e);
                continue;
            }
        };

        let id = msg.get("id").cloned();
        let method = msg.get("method").and_then(|m| m.as_str()).unwrap_or("");

        let span = tracing::info_span!(
            "mcp_stdio.request",
            method = %method,
            jsonrpc_id = ?id,
            role = %role,
            agent = %name,
        );

        let result: Option<Result<Value>> = async {
            // Notifications (no id) — don't send a response
            let is_notification = id.is_none() || id.as_ref().map(|v| v.is_null()).unwrap_or(false);

            match method {
                "initialize" => Some(Ok(json!({
                    "protocolVersion": "2024-11-05",
                    "capabilities": { "tools": { "listChanged": true } },
                    "serverInfo": { "name": "exomonad", "version": env!("CARGO_PKG_VERSION") }
                }))),

                "notifications/initialized" => {
                    if !tool_watcher_started {
                        start_tool_list_changed_watcher(
                            role.to_string(),
                            name.to_string(),
                            stdout.clone(),
                        );
                        tool_watcher_started = true;
                    }
                    None
                }

                "notifications/cancelled" => {
                    // Notifications — no response
                    None
                }

                "tools/list" => {
                    let result = match lazy_client.get(role, name).await {
                        Ok(client) => client
                            .list_tools(role, name)
                            .await
                            .map(|tools| json!({ "tools": tools })),
                        Err(err) => Err(err),
                    };
                    Some(result)
                }

                "tools/call" => {
                    let params = msg.get("params").cloned().unwrap_or(json!({}));
                    let req = ToolCallRequest {
                        name: params["name"].as_str().unwrap_or("").to_string(),
                        arguments: params.get("arguments").cloned().unwrap_or(json!({})),
                    };
                    let result = match lazy_client.get(role, name).await {
                        Ok(client) => client
                            .call_tool(role, name, &req)
                            .await
                            .map(|output| output.to_mcp_result()),
                        Err(err) => Err(err),
                    };
                    Some(result)
                }

                _ if is_notification => None,

                other => Some(Err(anyhow::anyhow!("Unknown method: {}", other))),
            }
        }
        .instrument(span)
        .await;

        if let (Some(result), Some(id)) = (result, id) {
            let response = match result {
                Ok(result) => json!({ "jsonrpc": "2.0", "id": id, "result": result }),
                Err(e) => json!({
                    "jsonrpc": "2.0",
                    "id": id,
                    "error": { "code": -32603, "message": e.to_string() }
                }),
            };
            write_json_line(&stdout, &response).await?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tools_list_changed_notification_uses_mcp_method_name() {
        let notification = tools_list_changed_notification();
        assert_eq!(notification["jsonrpc"], "2.0");
        assert_eq!(notification["method"], "notifications/tools/list_changed");
        assert!(notification.get("id").is_none());
    }
}
