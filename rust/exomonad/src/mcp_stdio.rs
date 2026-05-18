//! MCP stdio translation layer.
//!
//! Speaks JSON-RPC (MCP protocol) on stdin/stdout, translates to
//! domain-typed REST calls against the UDS server.

use crate::uds_client::{self, ServerClient, ToolCallRequest};
use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
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
    let mut stdout = tokio::io::stdout();
    let mut lazy_client = LazyServerClient::new();

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
                    "capabilities": { "tools": { "listChanged": false } },
                    "serverInfo": { "name": "exomonad", "version": env!("CARGO_PKG_VERSION") }
                }))),

                "notifications/initialized" | "notifications/cancelled" => {
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
            stdout.write_all(&serde_json::to_vec(&response)?).await?;
            stdout.write_all(b"\n").await?;
            stdout.flush().await?;
        }
    }

    Ok(())
}
