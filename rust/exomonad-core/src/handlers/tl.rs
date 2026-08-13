//! Controller observability effect handler for the tl namespace.
//!
//! The controller owns decisions, but Rust owns the single durable ledger
//! writer. This handler keeps that boundary explicit and does not reuse the
//! generic log.emit_event effect.

use crate::effects::{dispatch_tl_effect, EffectError, EffectHandler, EffectResult, TlEffects};
use crate::services::HasEventLog;
use async_trait::async_trait;
use exomonad_proto::effects::tl::*;
use serde_json::Value;
use std::sync::Arc;

const CONTROLLER_EVENT_TYPES: [&str; 8] = [
    "tl.phase_changed",
    "tl.slice_status_changed",
    "tl.slice_parked",
    "tl.gate_opened",
    "tl.gate_answered",
    "tl.merge_decided",
    "tl.judgment",
    "tl.plan_proposed",
];

fn allowed_fields(event_type: &str) -> &'static [&'static str] {
    match event_type {
        "tl.phase_changed" => &["from_phase", "to_phase", "run_id"],
        "tl.slice_status_changed" => &["slice_id", "from_status", "to_status"],
        "tl.slice_parked" => &["slice_id", "park_cause", "attempts"],
        "tl.gate_opened" => &["gate_name", "run_id"],
        "tl.gate_answered" => &["gate_name", "decision", "source"],
        "tl.merge_decided" => &["slice_id", "pr_number", "decision", "head_sha_hash"],
        "tl.judgment" => &[
            "judgment",
            "attempt",
            "outcome",
            "tokens",
            "replayed",
            "model",
            "latency_ms",
            "redacted_result",
        ],
        "tl.plan_proposed" => &["run_id", "accepted", "rejection_reason"],
        _ => &[],
    }
}

/// Handles controller-owned aggregate observability events.
pub struct TlHandler<C> {
    ctx: Arc<C>,
}

impl<C: HasEventLog + 'static> TlHandler<C> {
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<C: HasEventLog + 'static> EffectHandler for TlHandler<C> {
    fn namespace(&self) -> &str {
        "tl"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_tl_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<C: HasEventLog + 'static> TlEffects for TlHandler<C> {
    async fn emit_event(
        &self,
        req: EmitEventRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<EmitEventResponse> {
        if !CONTROLLER_EVENT_TYPES.contains(&req.event_type.as_str()) {
            return Err(EffectError::invalid_input(format!(
                "unsupported controller event type: {}",
                req.event_type
            )));
        }

        let payload = if req.payload.is_empty() {
            serde_json::json!({})
        } else {
            serde_json::from_slice::<Value>(&req.payload).map_err(|error| {
                EffectError::invalid_input(format!("controller event payload is not JSON: {error}"))
            })?
        };
        if !payload.is_object() {
            return Err(EffectError::invalid_input(
                "controller event payload must be a JSON object",
            ));
        }
        if req.payload.len() > 4096 {
            return Err(EffectError::invalid_input(
                "controller event payload exceeds 4096 bytes",
            ));
        }
        let payload_object = payload.as_object().ok_or_else(|| {
            EffectError::invalid_input("controller event payload must be a JSON object")
        })?;
        for (key, value) in payload_object {
            if !allowed_fields(req.event_type.as_str()).contains(&key.as_str()) {
                return Err(EffectError::invalid_input(format!(
                    "field '{key}' is not allowed for {}",
                    req.event_type
                )));
            }
            if !value.is_string() && !value.is_boolean() && !value.is_number() {
                return Err(EffectError::invalid_input(format!(
                    "field '{key}' must be a scalar aggregate dimension"
                )));
            }
        }

        let event_log = self.ctx.event_log().ok_or_else(|| {
            EffectError::custom("tl_event_log_unavailable", "event log unavailable")
        })?;
        let event_id = event_log
            .append(&req.event_type, ctx.agent_name.as_str(), &payload)
            .map_err(|error| EffectError::custom("tl_event_append_failed", error.to_string()))?;

        Ok(EmitEventResponse { event_id })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;
    use crate::services::{EventLog, Services};
    use std::path::PathBuf;
    use tempfile::tempdir;

    fn context() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("root").unwrap(),
            birth_branch: BirthBranch::try_from_str("main").unwrap(),
            working_dir: PathBuf::from("."),
        }
    }

    #[tokio::test]
    async fn appends_declared_controller_event_to_ledger() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));

        let response = handler
            .emit_event(
                EmitEventRequest {
                    event_type: "tl.phase_changed".to_string(),
                    payload: br#"{"from_phase":"planning","to_phase":"running"}"#.to_vec(),
                },
                &context(),
            )
            .await
            .unwrap();

        assert!(!response.event_id.is_empty());
    }

    #[tokio::test]
    async fn rejects_undeclared_controller_event() {
        let handler = TlHandler::new(Arc::new(Services::test()));
        let error = handler
            .emit_event(
                EmitEventRequest {
                    event_type: "custom.controller".to_string(),
                    payload: Vec::new(),
                },
                &context(),
            )
            .await
            .unwrap_err();

        assert!(matches!(error, EffectError::InvalidInput { .. }));
    }

    #[tokio::test]
    async fn rejects_body_fields_from_controller_projection() {
        let handler = TlHandler::new(Arc::new(Services::test()));
        let error = handler
            .emit_event(
                EmitEventRequest {
                    event_type: "tl.phase_changed".to_string(),
                    payload: br#"{"from_phase":"planning","body":"private"}"#.to_vec(),
                },
                &context(),
            )
            .await
            .unwrap_err();

        assert!(matches!(error, EffectError::InvalidInput { .. }));
    }
}
