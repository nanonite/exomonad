use super::{ExternalService, ServiceError};
use crate::protocol::{
    ChatMessage, ContentBlock, ServiceRequest, ServiceResponse, StopReason, Tool, Usage,
};
use async_trait::async_trait;
use reqwest::{Client, Url};
use serde::{Deserialize, Serialize};
use std::time::Duration;
use tracing::warn;

/// Service client for the Anthropic Messages API.
///
/// Handles chat completions with support for tools, system prompts, and
/// automatic retry (exponential backoff) for 529 Overloaded errors.
///
/// Supports two auth modes:
/// - `x-api-key` header (direct Anthropic)
/// - `Authorization: Bearer` header (OpenRouter)
pub struct AnthropicService {
    client: Client,
    api_key: String,
    base_url: Url,
    /// When true, use `Authorization: Bearer` and prefix model names with `anthropic/`.
    use_bearer_auth: bool,
}

impl AnthropicService {
    /// Create a new Anthropic service with the given API key.
    ///
    /// Uses the default endpoint: `https://api.anthropic.com`.
    pub fn new(api_key: String) -> Result<Self, ServiceError> {
        let base_url = Url::parse("https://api.anthropic.com").map_err(|e| ServiceError::Api {
            code: 500,
            message: format!("Invalid hardcoded URL: {}", e),
        })?;
        Ok(Self {
            client: Client::new(),
            api_key,
            base_url,
            use_bearer_auth: false,
        })
    }

    /// Create a new Anthropic service with a custom base URL.
    ///
    /// Useful for testing (mock servers) or proxies.
    pub fn with_base_url(api_key: String, base_url: Url) -> Self {
        Self {
            client: Client::new(),
            api_key,
            base_url,
            use_bearer_auth: false,
        }
    }

    /// Create an Anthropic-compatible service routed through OpenRouter.
    ///
    /// Uses `Authorization: Bearer` auth and prefixes model names with `anthropic/`.
    pub fn with_openrouter(api_key: String) -> Result<Self, ServiceError> {
        let base_url = Url::parse("https://openrouter.ai/api").map_err(|e| ServiceError::Api {
            code: 500,
            message: format!("Invalid hardcoded URL: {}", e),
        })?;
        Ok(Self {
            client: Client::new(),
            api_key,
            base_url,
            use_bearer_auth: true,
        })
    }

    /// Create a new Anthropic service from environment variables.
    ///
    /// Required: `ANTHROPIC_API_KEY`.
    /// Optional: `ANTHROPIC_BASE_URL`.
    pub fn from_env() -> Result<Self, anyhow::Error> {
        let api_key = std::env::var("ANTHROPIC_API_KEY")?;
        let base_url_str = std::env::var("ANTHROPIC_BASE_URL")
            .unwrap_or_else(|_| "https://api.anthropic.com".to_string());
        let base_url = Url::parse(&base_url_str)?;

        Ok(Self::with_base_url(api_key, base_url))
    }

    /// Qualify a model name for the current endpoint.
    ///
    /// OpenRouter requires `anthropic/claude-*` format. Direct Anthropic uses bare names.
    fn qualify_model(&self, model: &str) -> String {
        if self.use_bearer_auth && !model.contains('/') {
            format!("anthropic/{}", model)
        } else {
            model.to_string()
        }
    }
}

#[derive(Serialize)]
struct AnthropicRequestPayload {
    model: String,
    messages: Vec<ChatMessage>,
    max_tokens: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    tools: Option<Vec<Tool>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    system: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    thinking: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct AnthropicResponsePayload {
    content: Vec<ContentBlock>,
    stop_reason: Option<String>,
    usage: Usage,
}

#[async_trait]
impl ExternalService for AnthropicService {
    type Request = ServiceRequest;
    type Response = ServiceResponse;

    async fn call(&self, req: Self::Request) -> Result<Self::Response, ServiceError> {
        let (model, messages, max_tokens, tools, system, thinking) = match req {
            ServiceRequest::AnthropicChat {
                model,
                messages,
                max_tokens,
                tools,
                system,
                thinking,
            } => (model, messages, max_tokens, tools, system, thinking),
            _ => {
                return Err(ServiceError::Api {
                    code: 400,
                    message: "Invalid request type for AnthropicService".to_string(),
                })
            }
        };

        let payload = AnthropicRequestPayload {
            model: self.qualify_model(&model),
            messages,
            max_tokens,
            tools,
            system,
            thinking,
        };
        let url = self
            .base_url
            .join("/v1/messages")
            .map_err(|e| ServiceError::Api {
                code: 500,
                message: format!("URL join failed: {}", e),
            })?;

        let policy = crate::services::resilience::RetryPolicy::filtered(
            3,
            crate::services::resilience::Backoff::Exponential {
                initial: Duration::from_millis(500),
                max: Duration::from_secs(2),
            },
            |e| {
                // Only retry 529 (overloaded) errors
                e.downcast_ref::<ServiceError>()
                    .map(|se| matches!(se, ServiceError::RateLimited { .. }))
                    .unwrap_or(false)
            },
        );

        let result = crate::services::resilience::retry(&policy, || async {
            let mut request = self
                .client
                .post(url.clone())
                .header("content-type", "application/json");
            request = if self.use_bearer_auth {
                request.header("authorization", format!("Bearer {}", self.api_key))
            } else {
                request
                    .header("x-api-key", &self.api_key)
                    .header("anthropic-version", "2023-06-01")
            };
            let response = request
                .json(&payload)
                .send()
                .await
                .map_err(ServiceError::from)?;

            if response.status().as_u16() == 529 {
                return Err(ServiceError::RateLimited { retry_after_ms: 0 }.into());
            }

            if !response.status().is_success() {
                let code = response.status().as_u16() as i32;
                let message = response.text().await.unwrap_or_else(|e| {
                    warn!("Failed to read error response body: {}", e);
                    String::new()
                });
                return Err(ServiceError::Api { code, message }.into());
            }

            let body: AnthropicResponsePayload =
                response.json().await.map_err(ServiceError::from)?;

            let stop_reason = match body.stop_reason.as_deref() {
                Some("end_turn") => StopReason::EndTurn,
                Some("max_tokens") => StopReason::MaxTokens,
                Some("stop_sequence") => StopReason::StopSequence,
                Some("tool_use") => StopReason::ToolUse,
                _ => StopReason::EndTurn,
            };

            Ok(ServiceResponse::AnthropicChat {
                content: body.content,
                stop_reason,
                usage: body.usage,
            })
        })
        .await;

        result.map_err(|e| {
            e.downcast::<ServiceError>()
                .unwrap_or_else(|e| ServiceError::Api {
                    code: 500,
                    message: e.to_string(),
                })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn get_fixture_path(subpath: &str) -> PathBuf {
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        PathBuf::from(manifest_dir)
            .join("test/fixtures/claude-api")
            .join(subpath)
    }

    fn load_fixture(subpath: &str) -> serde_json::Value {
        let path = get_fixture_path(subpath);
        let content = std::fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("Failed to read fixture {:?}: {}", path, err));
        serde_json::from_str(&content).expect("Invalid JSON in fixture")
    }

    #[tokio::test]
    async fn test_anthropic_chat() {
        let mock_server = MockServer::start().await;

        let mock_response = serde_json::json!({
            "content": [{"type": "text", "text": "Hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}
        });

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(mock_response))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "Hi".into(),
            }],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        match service.call(req).await.unwrap() {
            ServiceResponse::AnthropicChat {
                content,
                stop_reason,
                ..
            } => {
                assert_eq!(content[0].text.as_deref(), Some("Hello"));
                assert_eq!(stop_reason, StopReason::EndTurn);
            }
            _ => panic!("Wrong response type"),
        }
    }

    #[tokio::test]
    async fn test_request_golden_simple_message() {
        let mock_server = MockServer::start().await;
        let expected_json = load_fixture("request/simple_message.json");

        // Verify the request body matches the golden fixture
        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .and(move |req: &wiremock::Request| {
                let body: serde_json::Value = req.body_json().unwrap();
                // Compare body with expected_json
                // Note: expected_json has "system" but our simple request might not if None.
                // The fixture has "system": "You are a helpful assistant."

                // We need to construct the ServiceRequest to match the fixture exactly.
                body == expected_json
            })
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 0, "output_tokens": 0}
            })))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        // Construct request matching request/simple_message.json
        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus-20240229".into(),
            max_tokens: 1024,
            system: Some("You are a helpful assistant.".into()),
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "Hello".into(),
            }],
            tools: None,
            thinking: None,
        };

        service.call(req).await.unwrap();
    }

    #[tokio::test]
    async fn test_request_golden_with_tools() {
        let mock_server = MockServer::start().await;
        let expected_json = load_fixture("request/with_tools.json");

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .and(move |req: &wiremock::Request| {
                let body: serde_json::Value = req.body_json().unwrap();
                body == expected_json
            })
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 0, "output_tokens": 0}
            })))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        // Construct request matching request/with_tools.json
        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus-20240229".into(),
            max_tokens: 1024,
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "What is the weather?".into(),
            }],
            tools: Some(vec![Tool {
                name: "get_weather".into(),
                description: "Get weather for a location".into(),
                input_schema: serde_json::json!({
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"}
                    },
                    "required": ["location"]
                }),
            }]),
            system: None, // Fixture doesn't have system
            thinking: None,
        };

        service.call(req).await.unwrap();
    }

    #[tokio::test]
    async fn test_response_golden_text() {
        let mock_server = MockServer::start().await;
        let response_json = load_fixture("response/text_response.json");

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(response_json))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 10,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await.unwrap();

        match resp {
            ServiceResponse::AnthropicChat {
                content,
                stop_reason,
                usage,
            } => {
                assert_eq!(content.len(), 1);
                assert_eq!(content[0].block_type, "text");
                assert_eq!(content[0].text.as_deref(), Some("Hello!"));
                assert_eq!(stop_reason, StopReason::EndTurn);
                assert_eq!(usage.input_tokens, 10);
                assert_eq!(usage.output_tokens, 5);
            }
            _ => panic!("Wrong response type"),
        }
    }

    #[tokio::test]
    async fn test_response_golden_tool_use() {
        let mock_server = MockServer::start().await;
        let response_json = load_fixture("response/tool_use_response.json");

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(response_json))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 10,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await.unwrap();

        match resp {
            ServiceResponse::AnthropicChat {
                content,
                stop_reason,
                usage,
            } => {
                assert_eq!(content.len(), 2);

                // Block 1: Text
                assert_eq!(content[0].block_type, "text");
                assert_eq!(
                    content[0].text.as_deref(),
                    Some("I will check the weather.")
                );

                // Block 2: Tool Use
                assert_eq!(content[1].block_type, "tool_use");
                assert_eq!(content[1].id.as_deref(), Some("toolu_01234"));
                assert_eq!(content[1].name.as_deref(), Some("get_weather"));

                let input = content[1].input.as_ref().unwrap();
                assert_eq!(input["location"], "San Francisco");

                assert_eq!(stop_reason, StopReason::ToolUse);
                assert_eq!(usage.input_tokens, 20);
                assert_eq!(usage.output_tokens, 30);
            }
            _ => panic!("Wrong response type"),
        }
    }

    // === Error path tests ===

    #[tokio::test]
    async fn test_529_retry_once_then_success() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

        let mock_server = MockServer::start().await;
        let call_count = Arc::new(AtomicUsize::new(0));
        let call_count_clone = call_count.clone();

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(move |_req: &wiremock::Request| {
                let count = call_count_clone.fetch_add(1, Ordering::SeqCst);
                if count == 0 {
                    // First call: return 529
                    ResponseTemplate::new(529)
                } else {
                    // Second call: return success
                    ResponseTemplate::new(200).set_body_json(serde_json::json!({
                        "content": [{"type": "text", "text": "Success!"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 10, "output_tokens": 5}
                    }))
                }
            })
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "Hi".into(),
            }],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_ok());
        assert_eq!(call_count.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn test_529_retry_exhausted() {
        let mock_server = MockServer::start().await;

        // Always return 529
        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(529))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![ChatMessage {
                role: "user".into(),
                content: "Hi".into(),
            }],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_err());
        match resp.unwrap_err() {
            ServiceError::RateLimited { .. } => {} // Expected
            other => panic!("Expected RateLimited error, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_api_error_400() {
        let mock_server = MockServer::start().await;

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(400).set_body_string("Bad request"))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_err());
        match resp.unwrap_err() {
            ServiceError::Api { code, message } => {
                assert_eq!(code, 400);
                assert!(message.contains("Bad request"));
            }
            other => panic!("Expected Api error, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_api_error_500() {
        let mock_server = MockServer::start().await;

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(500).set_body_string("Internal server error"))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_err());
        match resp.unwrap_err() {
            ServiceError::Api { code, .. } => {
                assert_eq!(code, 500);
            }
            other => panic!("Expected Api error, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_api_error_401_unauthorized() {
        let mock_server = MockServer::start().await;

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(401).set_body_string("Unauthorized"))
            .mount(&mock_server)
            .await;

        let service = AnthropicService::with_base_url(
            "invalid-key".into(),
            mock_server.uri().parse().unwrap(),
        );

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_err());
        match resp.unwrap_err() {
            ServiceError::Api { code, .. } => {
                assert_eq!(code, 401);
            }
            other => panic!("Expected Api error, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_malformed_json_response() {
        let mock_server = MockServer::start().await;

        Mock::given(method("POST"))
            .and(path("/v1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_string("not valid json"))
            .mount(&mock_server)
            .await;

        let service =
            AnthropicService::with_base_url("test-key".into(), mock_server.uri().parse().unwrap());

        let req = ServiceRequest::AnthropicChat {
            model: "claude-3-opus".into(),
            messages: vec![],
            max_tokens: 100,
            tools: None,
            system: None,
            thinking: None,
        };

        let resp = service.call(req).await;
        assert!(resp.is_err());
        // Should be an HTTP error (deserialization failed)
        match resp.unwrap_err() {
            ServiceError::Http(_) => {} // Expected
            other => panic!("Expected Http error (from json parse), got {:?}", other),
        }
    }
}
