//! Gemini↔OpenAI translation proxy for OpenRouter routing.
//!
//! Listens on `localhost:{port}` and translates Gemini native API requests
//! (`/v1beta/models/{model}:generateContent`) to OpenRouter's OpenAI-compatible
//! `/v1/chat/completions`, then translates responses back to Gemini format.
//!
//! Used when `openrouter.enabled = true` to route Gemini CLI calls through
//! OpenRouter without modifying the Gemini CLI binary.

use anyhow::Result;
use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
    Json, Router,
};
use serde::{Deserialize, Serialize};

// ============================================================================
// Gemini API types (subset used by Gemini CLI)
// ============================================================================

#[derive(Debug, Deserialize)]
struct GeminiRequest {
    contents: Vec<GeminiContent>,
    #[serde(rename = "systemInstruction")]
    system_instruction: Option<GeminiContent>,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
struct GeminiContent {
    role: Option<String>,
    parts: Vec<GeminiPart>,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[serde(untagged)]
enum GeminiPart {
    Text {
        text: String,
    },
    FunctionCall {
        #[serde(rename = "functionCall")]
        function_call: serde_json::Value,
    },
    FunctionResponse {
        #[serde(rename = "functionResponse")]
        function_response: serde_json::Value,
    },
    Other(serde_json::Value),
}

#[derive(Debug, Serialize)]
struct GeminiResponse {
    candidates: Vec<GeminiCandidate>,
    #[serde(rename = "usageMetadata", skip_serializing_if = "Option::is_none")]
    usage_metadata: Option<GeminiUsageMetadata>,
}

#[derive(Debug, Serialize)]
struct GeminiCandidate {
    content: GeminiContent,
    #[serde(rename = "finishReason", skip_serializing_if = "Option::is_none")]
    finish_reason: Option<String>,
    index: u32,
}

#[derive(Debug, Serialize)]
struct GeminiUsageMetadata {
    #[serde(rename = "promptTokenCount")]
    prompt_token_count: u32,
    #[serde(rename = "candidatesTokenCount")]
    candidates_token_count: u32,
    #[serde(rename = "totalTokenCount")]
    total_token_count: u32,
}

// ============================================================================
// OpenAI API types (subset sent to / received from OpenRouter)
// ============================================================================

#[derive(Debug, Serialize)]
struct OpenAiRequest {
    model: String,
    messages: Vec<OpenAiMessage>,
}

#[derive(Debug, Serialize, Deserialize)]
struct OpenAiMessage {
    role: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct OpenAiResponse {
    choices: Vec<OpenAiChoice>,
    usage: Option<OpenAiUsage>,
}

#[derive(Debug, Deserialize)]
struct OpenAiChoice {
    message: OpenAiMessage,
    finish_reason: Option<String>,
}

#[derive(Debug, Deserialize)]
struct OpenAiUsage {
    prompt_tokens: u32,
    completion_tokens: u32,
    total_tokens: u32,
}

// ============================================================================
// Proxy state
// ============================================================================

#[derive(Clone)]
struct ProxyState {
    client: reqwest::Client,
    api_key: String,
}

// ============================================================================
// Translation helpers
// ============================================================================

fn gemini_part_to_text(part: &GeminiPart) -> String {
    match part {
        GeminiPart::Text { text } => text.clone(),
        GeminiPart::FunctionCall { function_call } => {
            serde_json::to_string(function_call).unwrap_or_default()
        }
        GeminiPart::FunctionResponse { function_response } => {
            serde_json::to_string(function_response).unwrap_or_default()
        }
        GeminiPart::Other(v) => serde_json::to_string(v).unwrap_or_default(),
    }
}

fn gemini_role_to_openai(role: Option<&str>) -> &'static str {
    match role {
        Some("model") => "assistant",
        _ => "user",
    }
}

fn translate_gemini_to_openai(req: GeminiRequest, model: &str) -> OpenAiRequest {
    let mut messages: Vec<OpenAiMessage> = Vec::new();

    if let Some(sys) = req.system_instruction {
        let text = sys
            .parts
            .iter()
            .map(gemini_part_to_text)
            .collect::<Vec<_>>()
            .join("\n");
        if !text.is_empty() {
            messages.push(OpenAiMessage {
                role: "system".to_string(),
                content: text,
            });
        }
    }

    for content in &req.contents {
        let text = content
            .parts
            .iter()
            .map(gemini_part_to_text)
            .collect::<Vec<_>>()
            .join("\n");
        messages.push(OpenAiMessage {
            role: gemini_role_to_openai(content.role.as_deref()).to_string(),
            content: text,
        });
    }

    OpenAiRequest {
        model: format!("google/{}", model),
        messages,
    }
}

fn finish_reason_to_gemini(reason: Option<&str>) -> Option<String> {
    match reason {
        Some("stop") => Some("STOP".to_string()),
        Some("length") => Some("MAX_TOKENS".to_string()),
        Some(r) => Some(r.to_uppercase()),
        None => None,
    }
}

fn translate_openai_to_gemini(resp: OpenAiResponse) -> GeminiResponse {
    let candidates = resp
        .choices
        .into_iter()
        .enumerate()
        .map(|(i, choice)| GeminiCandidate {
            content: GeminiContent {
                role: Some("model".to_string()),
                parts: vec![GeminiPart::Text {
                    text: choice.message.content,
                }],
            },
            finish_reason: finish_reason_to_gemini(choice.finish_reason.as_deref()),
            index: i as u32,
        })
        .collect();

    let usage_metadata = resp.usage.map(|u| GeminiUsageMetadata {
        prompt_token_count: u.prompt_tokens,
        candidates_token_count: u.completion_tokens,
        total_token_count: u.total_tokens,
    });

    GeminiResponse {
        candidates,
        usage_metadata,
    }
}

// ============================================================================
// Shared request logic
// ============================================================================

async fn forward_to_openrouter(
    model: &str,
    body: GeminiRequest,
    state: &ProxyState,
) -> Result<GeminiResponse, Response> {
    let openai_req = translate_gemini_to_openai(body, model);
    tracing::debug!(model = %model, "Forwarding to OpenRouter");

    let resp = state
        .client
        .post("https://openrouter.ai/api/v1/chat/completions")
        .bearer_auth(&state.api_key)
        .json(&openai_req)
        .send()
        .await
        .map_err(|e| {
            tracing::error!(error = %e, "Proxy request to OpenRouter failed");
            (StatusCode::BAD_GATEWAY, format!("Proxy error: {}", e)).into_response()
        })?;

    let status = resp.status();
    if !status.is_success() {
        let body_text = resp.text().await.unwrap_or_default();
        tracing::error!(status = %status, body = %body_text, "OpenRouter returned error");
        return Err((
            StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
            body_text,
        )
            .into_response());
    }

    resp.json::<OpenAiResponse>().await.map(translate_openai_to_gemini).map_err(|e| {
        tracing::error!(error = %e, "Failed to parse OpenRouter response");
        (StatusCode::BAD_GATEWAY, format!("Parse error: {}", e)).into_response()
    })
}

// ============================================================================
// Route handlers
// ============================================================================

/// POST /v1beta/models/{model_and_action}
///
/// The wildcard captures e.g. `gemini-2.0-flash:generateContent` or
/// `gemini-2.0-flash:streamGenerateContent`. We split on `:` to extract
/// the model name and the action.
async fn handle_generate(
    Path(model_and_action): Path<String>,
    State(state): State<ProxyState>,
    Json(body): Json<GeminiRequest>,
) -> Response {
    // Extract model name before the colon (e.g. "gemini-2.0-flash")
    let model = model_and_action
        .split(':')
        .next()
        .unwrap_or(&model_and_action);

    match forward_to_openrouter(model, body, &state).await {
        Ok(gemini_resp) => {
            // streamGenerateContent expects SSE; for the action check:
            if model_and_action.ends_with(":streamGenerateContent") {
                match serde_json::to_string(&gemini_resp) {
                    Ok(json) => (
                        StatusCode::OK,
                        [("content-type", "text/event-stream; charset=utf-8")],
                        format!("data: {}\r\n\r\n", json),
                    )
                        .into_response(),
                    Err(e) => (
                        StatusCode::INTERNAL_SERVER_ERROR,
                        format!("Serialize error: {}", e),
                    )
                        .into_response(),
                }
            } else {
                Json(gemini_resp).into_response()
            }
        }
        Err(err_resp) => err_resp,
    }
}

// ============================================================================
// Entry point
// ============================================================================

pub async fn run(port: u16, api_key: String) -> Result<()> {
    let state = ProxyState {
        client: reqwest::Client::new(),
        api_key,
    };

    let app = Router::new()
        .route("/v1beta/models/*model_and_action", post(handle_generate))
        .with_state(state);

    let addr = format!("127.0.0.1:{}", port);
    tracing::info!(port = port, addr = %addr, "Gemini↔OpenAI translation proxy starting");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    tracing::info!(port = port, "Gemini proxy listening");
    axum::serve(listener, app).await?;
    Ok(())
}
