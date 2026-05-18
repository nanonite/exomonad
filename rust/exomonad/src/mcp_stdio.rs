//! MCP stdio translation layer.
//!
//! Speaks JSON-RPC (MCP protocol) on stdin/stdout, translates to
//! domain-typed REST calls against the UDS server.

use crate::uds_client::{self, ServerClient, ToolCallRequest};
use anyhow::Result;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tracing::{debug, error, info, Instrument};

/// Run the stdio MCP translation layer.
///
/// Reads JSON-RPC from stdin, translates to REST calls against the server,
/// writes JSON-RPC responses to stdout.
pub async fn run(role: &str, name: &str) -> Result<()> {
    // Retry socket discovery AND connect — server may still be starting
    // (race with exomonad init) and the socket file may exist as a symlink
    // before the listener is accepting connections. Without this, a leaf
    // agent that spawns at the same moment as the server (or where the
    // socket symlink is dangling for a beat) sees "Failed to connect to
    // <path>" and bails permanently. See chainlink #260.
    let client = {
        let started_at = std::time::Instant::now();
        let mut attempts = 0;
        let mut last_error = String::from("socket discovery has not run");
        info!(
            role,
            name, "mcp-stdio startup: beginning server socket discovery"
        );
        loop {
            let candidate = match uds_client::find_server_socket() {
                Ok(s) => {
                    debug!(role, name, socket = %s.display(), "mcp-stdio startup: discovered server socket candidate");
                    Some(s)
                }
                Err(err) => {
                    last_error = err.to_string();
                    debug!(role, name, error = %err, "mcp-stdio startup: server socket discovery failed");
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
                            "mcp-stdio startup: server health check succeeded"
                        );
                        break trial;
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
                            "mcp-stdio startup: server health check failed"
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
                    "mcp-stdio startup: no server socket candidate"
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
                    "mcp-stdio startup: server socket unreachable"
                );
                anyhow::bail!(
                    "Server socket not reachable after 15s (discovery or health check failed). \
                     Is exomonad serve running? Last error: {}",
                    last_error
                );
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    };

    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin).lines();
    let mut stdout = tokio::io::stdout();

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

                "tools/list" => Some(
                    client
                        .list_tools(role, name)
                        .await
                        .map(|tools| json!({ "tools": tools })),
                ),

                "tools/call" => {
                    let params = msg.get("params").cloned().unwrap_or(json!({}));
                    let req = ToolCallRequest {
                        name: params["name"].as_str().unwrap_or("").to_string(),
                        arguments: params.get("arguments").cloned().unwrap_or(json!({})),
                    };
                    Some(
                        client
                            .call_tool(role, name, &req)
                            .await
                            .map(|output| output.to_mcp_result()),
                    )
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
