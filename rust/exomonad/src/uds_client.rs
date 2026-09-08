//! Typed HTTP-over-UDS client for communicating with the exomonad server.
//!
//! Callers work with JSON types, not raw bytes or HTTP status codes.

use anyhow::{Context, Result};
use exomonad_core::mcp::ToolDefinition;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::path::PathBuf;

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct StructuredControlError {
    pub kind: String,
    pub error: String,
}

#[derive(Debug, thiserror::Error)]
#[error("server returned HTTP {status} for {path}: {message}")]
pub struct ControlRequestError {
    pub status: u16,
    pub path: String,
    pub kind: Option<String>,
    pub message: String,
}

/// Walk up from CWD to find `.exo/server.sock`. Returns the canonical
/// (symlink-resolved) path so `connect()` sees the real socket location
/// rather than a worktree symlink that may exceed `sun_path`'s 108-byte limit.
pub fn find_server_socket() -> Result<PathBuf> {
    let start = std::env::current_dir()?;
    let mut current = start.as_path();
    loop {
        let sock = current.join(".exo/server.sock");
        if sock.exists() {
            return sock.canonicalize().with_context(|| {
                format!(
                    "Failed to canonicalize server socket path {}",
                    sock.display()
                )
            });
        }
        match current.parent() {
            Some(parent) => current = parent,
            None => {
                return Err(anyhow::anyhow!(
                    "No .exo/server.sock found (walked up from {}). Is exomonad serve running?",
                    start.display()
                ));
            }
        }
    }
}

/// Request body for calling a tool via the REST API.
#[derive(Serialize)]
pub struct ToolCallRequest {
    pub name: String,
    pub arguments: Value,
}

/// Response from the tools list endpoint.
#[derive(Deserialize)]
struct ToolListResponse {
    tools: Vec<ToolDefinition>,
}

#[derive(Debug, Deserialize)]
pub struct HealthResponse {
    pub wasm_hash: String,
}

/// Client for the exomonad UDS server.
pub struct ServerClient {
    socket: PathBuf,
}

impl ServerClient {
    pub fn new(socket: PathBuf) -> Self {
        Self { socket }
    }

    /// Check if the server is alive and responding.
    pub async fn is_healthy(&self) -> bool {
        self.health_check().await.is_ok()
    }

    /// Check if the server is alive and return the transport/HTTP error when it is not.
    pub async fn health_check(&self) -> Result<()> {
        self.health_info().await.map(|_| ())
    }

    pub async fn health_info(&self) -> Result<HealthResponse> {
        match self.get_json("/health").await {
            Ok(response) => {
                tracing::debug!(socket = %self.socket.display(), "UDS health check succeeded via GET /health");
                Ok(response)
            }
            Err(err) => {
                tracing::debug!(socket = %self.socket.display(), error = %err, "UDS health check failed via GET /health");
                Err(err)
                    .with_context(|| format!("GET /health failed via {}", self.socket.display()))
            }
        }
    }

    /// List available tools for an agent.
    pub async fn list_tools(&self, role: &str, name: &str) -> Result<Vec<ToolDefinition>> {
        let path = format!("/agents/{}/{}/tools", role, name);
        let resp: ToolListResponse = self.get_json(&path).await?;
        Ok(resp.tools)
    }

    /// Call a tool via the REST API.
    pub async fn call_tool(
        &self,
        role: &str,
        name: &str,
        request: &ToolCallRequest,
    ) -> Result<exomonad_core::mcp::tools::MCPCallOutput> {
        let path = format!("/agents/{}/{}/tools/call", role, name);
        self.post_json(&path, request).await
    }

    /// POST typed JSON, receive typed JSON response.
    pub async fn post_json<T: Serialize, R: DeserializeOwned>(
        &self,
        path: &str,
        body: &T,
    ) -> Result<R> {
        let body_bytes = serde_json::to_vec(body).context("Failed to serialize request")?;
        let resp = self.raw_post(path, &body_bytes).await?;
        serde_json::from_slice(&resp)
            .with_context(|| format!("Failed to deserialize response from {}", path))
    }

    /// POST typed JSON to an authenticated control endpoint.
    ///
    /// Control requests deliberately use their own credential header and do
    /// not carry the MCP mail-piggyback marker used by agent tool calls.
    pub async fn post_control_json<T: Serialize, R: DeserializeOwned>(
        &self,
        path: &str,
        body: &T,
        credential: &str,
    ) -> Result<R> {
        let body_bytes = serde_json::to_vec(body).context("Failed to serialize request")?;
        let response = self
            .raw_request_with_status(
                hyper::Method::POST,
                path,
                Some(&body_bytes),
                &[(
                    crate::control::CONTROL_CREDENTIAL_HEADER.as_str(),
                    credential,
                )],
            )
            .await?;
        if !(200..300).contains(&response.status) {
            return Err(ControlRequestError::from_response(path, response).into());
        }
        serde_json::from_slice(&response.body)
            .with_context(|| format!("Failed to deserialize response from {}", path))
    }

    /// GET typed JSON response.
    async fn get_json<R: DeserializeOwned>(&self, path: &str) -> Result<R> {
        let resp = self.raw_get(path).await?;
        serde_json::from_slice(&resp)
            .with_context(|| format!("Failed to deserialize response from {}", path))
    }

    /// Low-level GET over UDS. Returns response body bytes.
    async fn raw_get(&self, path: &str) -> Result<Vec<u8>> {
        use hyper::Method;
        self.raw_request(Method::GET, path, None, &[]).await
    }

    /// Low-level POST over UDS. Returns response body bytes.
    ///
    /// POST is only used for MCP tool calls, which are translated back into MCP
    /// content format by the `mcp-stdio` binary. The mail-piggyback header marks
    /// this caller so the server appends unread inbox mail to the tool result.
    async fn raw_post(&self, path: &str, body: &[u8]) -> Result<Vec<u8>> {
        use hyper::Method;
        self.raw_request(
            Method::POST,
            path,
            Some(body),
            &[(crate::control::MAIL_PIGGYBACK_HEADER.as_str(), "1")],
        )
        .await
    }

    /// Shared low-level request logic over UDS.
    async fn raw_request(
        &self,
        method: hyper::Method,
        path: &str,
        body: Option<&[u8]>,
        extra_headers: &[(&str, &str)],
    ) -> Result<Vec<u8>> {
        let response = self
            .raw_request_with_status(method, path, body, extra_headers)
            .await?;
        if !(200..300).contains(&response.status) {
            return Err(anyhow::anyhow!(
                "Server returned {} for {}: {}",
                response.status,
                path,
                String::from_utf8_lossy(&response.body)
            ));
        }
        Ok(response.body)
    }

    async fn raw_request_with_status(
        &self,
        method: hyper::Method,
        path: &str,
        body: Option<&[u8]>,
        extra_headers: &[(&str, &str)],
    ) -> Result<RawResponse> {
        use http_body_util::{BodyExt, Full};
        use hyper::Request;
        use hyper_util::rt::TokioIo;
        use tokio::net::UnixStream;

        let stream = UnixStream::connect(&self.socket)
            .await
            .with_context(|| format!("Failed to connect to {}", self.socket.display()))?;
        let io = TokioIo::new(stream);

        let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
            .await
            .context("HTTP handshake failed")?;
        tokio::spawn(async move {
            let _ = conn.await;
        });

        let mut req_builder = Request::builder()
            .method(&method)
            .uri(path)
            .header("host", "localhost");

        for (name, value) in extra_headers {
            req_builder = req_builder.header(*name, *value);
        }

        if body.is_some() {
            req_builder = req_builder.header("content-type", "application/json");
        }

        let req = if let Some(b) = body {
            req_builder.body(Full::new(hyper::body::Bytes::from(b.to_vec())))?
        } else {
            req_builder.body(Full::new(hyper::body::Bytes::new()))?
        };

        let resp = sender
            .send_request(req)
            .await
            .with_context(|| format!("{} request failed", method))?;
        let status = resp.status().as_u16();
        let resp_body = resp
            .into_body()
            .collect()
            .await
            .context("Failed to read response body")?
            .to_bytes()
            .to_vec();

        Ok(RawResponse {
            status,
            body: resp_body,
        })
    }
}

struct RawResponse {
    status: u16,
    body: Vec<u8>,
}

impl ControlRequestError {
    fn from_response(path: &str, response: RawResponse) -> Self {
        let fallback = String::from_utf8_lossy(&response.body).to_string();
        let structured = serde_json::from_slice::<StructuredControlError>(&response.body).ok();
        let (kind, message) = match structured {
            Some(error) => (Some(error.kind), error.error),
            None => (None, fallback),
        };
        Self {
            status: response.status,
            path: path.to_string(),
            kind,
            message,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io;
    use tempfile::tempdir;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::UnixListener;

    fn find_header_end(bytes: &[u8]) -> Option<usize> {
        bytes.windows(4).position(|window| window == b"\r\n\r\n")
    }

    async fn read_request(stream: &mut tokio::net::UnixStream) -> io::Result<Vec<u8>> {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 4096];
        loop {
            let read = stream.read(&mut chunk).await?;
            if read == 0 {
                return Ok(request);
            }
            request.extend_from_slice(&chunk[..read]);
            let Some(header_end) = find_header_end(&request) else {
                continue;
            };
            let headers = String::from_utf8_lossy(&request[..header_end]).to_ascii_lowercase();
            let content_length = headers
                .lines()
                .find_map(|line| line.strip_prefix("content-length:"))
                .and_then(|value| value.trim().parse::<usize>().ok())
                .unwrap_or(0);
            if request.len() >= header_end + 4 + content_length {
                return Ok(request);
            }
        }
    }

    #[tokio::test]
    async fn control_post_uses_control_credential_without_mail_piggyback() {
        let directory = tempdir().unwrap();
        let socket = directory.path().join("server.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let request = read_request(&mut stream).await.unwrap();
            let request = String::from_utf8_lossy(&request).to_ascii_lowercase();
            assert!(request.contains("x-exomonad-control-credential: secret"));
            assert!(!request.contains("x-exomonad-mail-piggyback"));
            assert!(request.contains("\"apply\":false"));
            let body = r#"{"schema_version":1,"operation_id":"op","plan_id":"plan","started_at":1,"finished_at":2,"dry_run":true,"entries":[]}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).await.unwrap();
        });

        let client = ServerClient::new(socket);
        let receipt: serde_json::Value = client
            .post_control_json(
                "/control/cleanup",
                &serde_json::json!({"sweep": true, "apply": false}),
                "secret",
            )
            .await
            .unwrap();
        assert_eq!(receipt["dry_run"], true);
        server.await.unwrap();
    }

    #[test]
    fn control_errors_preserve_structured_statuses() {
        for (status, kind, message) in [
            (400, "invalid_request", "bad request"),
            (401, "unauthorized", "credential required"),
            (409, "busy", "cleanup is busy"),
            (413, "invalid_request", "request too large"),
            (500, "service_error", "cleanup failed"),
        ] {
            let body = serde_json::to_vec(&serde_json::json!({
                "kind": kind,
                "error": message,
            }))
            .unwrap();
            let error = ControlRequestError::from_response(
                "/control/cleanup",
                RawResponse { status, body },
            );
            assert_eq!(error.status, status);
            assert_eq!(error.kind.as_deref(), Some(kind));
            assert_eq!(error.message, message);
        }
    }
}
