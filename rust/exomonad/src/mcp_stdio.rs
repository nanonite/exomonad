//! MCP stdio translation layer.
//!
//! Speaks JSON-RPC (MCP protocol) on stdin/stdout, translates to
//! domain-typed REST calls against the UDS server.

use crate::uds_client::{self, ServerClient, ToolCallRequest};
use anyhow::Result;
use exomonad_core::mcp::ToolDefinition;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, Stdout};
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

#[derive(Clone, Copy)]
enum StdioFraming {
    JsonLine,
    ContentLength,
}

fn encode_message(value: &Value, framing: StdioFraming) -> Result<Vec<u8>> {
    let body = serde_json::to_vec(value)?;
    let mut message = Vec::new();
    match framing {
        StdioFraming::JsonLine => {
            message.extend_from_slice(&body);
            message.push(b'\n');
        }
        StdioFraming::ContentLength => {
            message.extend_from_slice(format!("Content-Length: {}\r\n\r\n", body.len()).as_bytes());
            message.extend_from_slice(&body);
        }
    }
    Ok(message)
}

async fn write_mcp_message(
    stdout: &Arc<AsyncMutex<Stdout>>,
    value: &Value,
    framing: StdioFraming,
) -> Result<()> {
    let mut stdout = stdout.lock().await;
    stdout.write_all(&encode_message(value, framing)?).await?;
    stdout.flush().await?;
    Ok(())
}

async fn read_mcp_message<R>(reader: &mut R) -> Result<Option<(Value, StdioFraming)>>
where
    R: AsyncBufRead + Unpin,
{
    loop {
        let mut line = String::new();
        let bytes_read = reader.read_line(&mut line).await?;
        if bytes_read == 0 {
            return Ok(None);
        }

        let trimmed = line.trim_end_matches(['\r', '\n']);
        if trimmed.trim().is_empty() {
            continue;
        }

        if let Some(length) = parse_content_length(trimmed)? {
            loop {
                line.clear();
                let header_bytes = reader.read_line(&mut line).await?;
                if header_bytes == 0 {
                    anyhow::bail!("Unexpected EOF while reading MCP headers");
                }
                if line.trim_end_matches(['\r', '\n']).is_empty() {
                    break;
                }
            }

            let mut body = vec![0; length];
            reader.read_exact(&mut body).await?;
            let msg = serde_json::from_slice(&body)?;
            return Ok(Some((msg, StdioFraming::ContentLength)));
        }

        match serde_json::from_str(trimmed) {
            Ok(msg) => return Ok(Some((msg, StdioFraming::JsonLine))),
            Err(e) => {
                tracing::warn!("Invalid JSON from stdin: {}", e);
                continue;
            }
        }
    }
}

fn parse_content_length(line: &str) -> Result<Option<usize>> {
    let Some((name, value)) = line.split_once(':') else {
        return Ok(None);
    };
    if !name.eq_ignore_ascii_case("content-length") {
        return Ok(None);
    }
    value
        .trim()
        .parse::<usize>()
        .map(Some)
        .map_err(|e| anyhow::anyhow!("Invalid Content-Length header: {e}"))
}

fn normalize_tool_schema(mut tool: ToolDefinition) -> ToolDefinition {
    if let Some(schema) = tool.input_schema.as_object_mut() {
        schema.entry("type").or_insert_with(|| json!("object"));
        schema.entry("properties").or_insert_with(|| json!({}));
    }
    tool
}

fn tools_list_changed_notification() -> Value {
    json!({
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed"
    })
}

fn start_tool_list_changed_watcher(
    role: String,
    name: String,
    stdout: Arc<AsyncMutex<Stdout>>,
    framing: StdioFraming,
) {
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
                            if let Err(err) =
                                write_mcp_message(&stdout, &notification, framing).await
                            {
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
    let mut reader = BufReader::new(stdin);
    let stdout = Arc::new(AsyncMutex::new(tokio::io::stdout()));
    let mut lazy_client = LazyServerClient::new();
    let mut tool_watcher_started = false;

    while let Some((msg, framing)) = read_mcp_message(&mut reader).await? {
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
                            framing,
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
                        Ok(client) => client.list_tools(role, name).await.map(|tools| {
                            let tools = tools
                                .into_iter()
                                .map(normalize_tool_schema)
                                .collect::<Vec<_>>();
                            json!({ "tools": tools })
                        }),
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
            write_mcp_message(&stdout, &response, framing).await?;
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

    #[test]
    fn encode_message_preserves_json_line_mode() {
        let msg = json!({"jsonrpc": "2.0", "id": 1, "result": {}});
        let encoded = encode_message(&msg, StdioFraming::JsonLine).unwrap();
        assert!(encoded.ends_with(b"\n"));
        assert!(!encoded.starts_with(b"Content-Length"));
    }

    #[test]
    fn encode_message_uses_mcp_content_length_frames() {
        let msg = json!({"jsonrpc": "2.0", "id": 1, "result": {}});
        let encoded = encode_message(&msg, StdioFraming::ContentLength).unwrap();
        let text = String::from_utf8(encoded).unwrap();
        let (header, body) = text.split_once("\r\n\r\n").unwrap();
        let length = header
            .strip_prefix("Content-Length: ")
            .unwrap()
            .parse::<usize>()
            .unwrap();
        assert_eq!(length, body.as_bytes().len());
        assert_eq!(serde_json::from_str::<Value>(body).unwrap(), msg);
    }

    #[test]
    fn parse_content_length_accepts_case_insensitive_header() {
        assert_eq!(
            parse_content_length("content-length: 42").unwrap(),
            Some(42)
        );
        assert_eq!(
            parse_content_length("Content-Type: application/json").unwrap(),
            None
        );
    }
    #[test]
    fn normalize_tool_schema_marks_empty_schema_as_object() {
        let tool = ToolDefinition {
            name: "chainlink_session_status".to_string(),
            description: "Read session status".to_string(),
            input_schema: json!({}),
        };
        let normalized = normalize_tool_schema(tool);
        assert_eq!(normalized.input_schema["type"], "object");
        assert_eq!(normalized.input_schema["properties"], json!({}));
    }
}
