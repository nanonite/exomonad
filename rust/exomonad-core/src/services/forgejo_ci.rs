use std::sync::Arc;

use axum::{
    body::Bytes,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tracing::warn;

use crate::domain::{BranchName, CIStatus};
use crate::services::HasCiStatusMap;

#[derive(Clone)]
pub struct ForgejoCiWebhookState<C> {
    pub ctx: Arc<C>,
    pub webhook_secret: Option<String>,
}

pub async fn handle<C: HasCiStatusMap>(
    State(state): State<ForgejoCiWebhookState<C>>,
    headers: HeaderMap,
    body: Bytes,
) -> impl IntoResponse {
    let Some(secret) = state.webhook_secret.as_deref() else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            "forgejo webhook secret not configured",
        );
    };

    if !verify_signature(secret, &headers, &body) {
        return (StatusCode::UNAUTHORIZED, "invalid webhook signature");
    }

    let payload: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => return (StatusCode::BAD_REQUEST, "invalid json payload"),
    };

    let Some((branch, sha, conclusion)) = extract_ci_fields(&payload) else {
        return (StatusCode::BAD_REQUEST, "unsupported ci payload");
    };

    let branch = match BranchName::try_from_str(&branch) {
        Ok(branch) => branch,
        Err(_) => return (StatusCode::BAD_REQUEST, "invalid branch"),
    };

    let status = CIStatus::parse(&conclusion);
    state
        .ctx
        .ci_status_map()
        .write()
        .await
        .insert((branch, sha), status);

    (StatusCode::OK, "ok")
}

fn extract_ci_fields(payload: &Value) -> Option<(String, String, String)> {
    if let Some(workflow_run) = payload.get("workflow_run") {
        let branch = workflow_run.get("head_branch")?.as_str()?.to_string();
        let sha = workflow_run.get("head_sha")?.as_str()?.to_string();
        let conclusion = workflow_run
            .get("conclusion")
            .and_then(Value::as_str)
            .or_else(|| workflow_run.get("status").and_then(Value::as_str))
            .unwrap_or("unknown")
            .to_string();
        return Some((branch, sha, conclusion));
    }

    if let Some(check_run) = payload.get("check_run") {
        let branch = check_run
            .get("check_suite")
            .and_then(|v| v.get("head_branch"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .or_else(|| {
                check_run
                    .get("pull_requests")
                    .and_then(Value::as_array)
                    .and_then(|arr| arr.first())
                    .and_then(|pr| pr.get("head"))
                    .and_then(|head| head.get("ref"))
                    .and_then(Value::as_str)
                    .map(ToOwned::to_owned)
            })?;
        let sha = check_run.get("head_sha")?.as_str()?.to_string();
        let conclusion = check_run
            .get("conclusion")
            .and_then(Value::as_str)
            .or_else(|| check_run.get("status").and_then(Value::as_str))
            .unwrap_or("unknown")
            .to_string();
        return Some((branch, sha, conclusion));
    }

    None
}

fn verify_signature(secret: &str, headers: &HeaderMap, body: &[u8]) -> bool {
    let Some(signature) = extract_signature(headers) else {
        warn!("forgejo webhook missing signature header");
        return false;
    };

    let expected = hex_encode(&hmac_sha256(secret.as_bytes(), body));
    signature.eq_ignore_ascii_case(&expected)
}

fn extract_signature(headers: &HeaderMap) -> Option<String> {
    if let Some(raw) = headers
        .get("x-gitea-signature")
        .and_then(|v| v.to_str().ok())
    {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            return Some(trimmed.to_string());
        }
    }

    headers
        .get("x-hub-signature-256")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("sha256="))
        .map(|s| s.trim().to_string())
}

fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK: usize = 64;

    let mut key_block = [0u8; BLOCK];
    if key.len() > BLOCK {
        let digest = Sha256::digest(key);
        key_block[..32].copy_from_slice(&digest);
    } else {
        key_block[..key.len()].copy_from_slice(key);
    }

    let mut ipad = [0x36u8; BLOCK];
    let mut opad = [0x5cu8; BLOCK];
    for i in 0..BLOCK {
        ipad[i] ^= key_block[i];
        opad[i] ^= key_block[i];
    }

    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(message);
    let inner_hash = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_hash);
    let out = outer.finalize();

    let mut digest = [0u8; 32];
    digest.copy_from_slice(&out);
    digest
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}
