//! Controller observability effect handler for the tl namespace.
//!
//! The controller owns decisions, but Rust owns the single durable ledger
//! writer. This handler keeps that boundary explicit and does not reuse the
//! generic log.emit_event effect.

use crate::effects::{dispatch_tl_effect, EffectError, EffectHandler, EffectResult, TlEffects};
use crate::services::HasEventLog;
use async_trait::async_trait;
use exomonad_proto::effects::tl::*;
use serde::Deserialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::OnceLock;

const CONTROLLER_EVENT_CONTRACT: &str =
    include_str!("../../../../docs/observability/controller-event-contract.v1.json");

#[derive(Debug, Deserialize)]
struct EventDefinition {
    fields: Vec<String>,
    #[serde(default)]
    string_array_fields: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ControllerEventContract {
    contract_id: String,
    version: u32,
    events: BTreeMap<String, EventDefinition>,
}

fn event_contract() -> &'static ControllerEventContract {
    static CONTRACT: OnceLock<ControllerEventContract> = OnceLock::new();
    CONTRACT.get_or_init(|| {
        let contract: ControllerEventContract = serde_json::from_str(CONTROLLER_EVENT_CONTRACT)
            .expect("controller event contract must be valid JSON");
        assert_eq!(
            contract.contract_id, "exomonad.controller-event-fields",
            "controller event contract id drifted"
        );
        assert_eq!(
            contract.version, 1,
            "unsupported controller event contract version"
        );
        contract
    })
}

fn allowed_fields(event_type: &str) -> Option<&'static [String]> {
    event_contract()
        .events
        .get(event_type)
        .map(|definition| definition.fields.as_slice())
}

fn valid_field_value(event_type: &str, key: &str, value: &Value) -> bool {
    let Some(definition) = event_contract().events.get(event_type) else {
        return false;
    };
    if value.is_null() {
        return true;
    }
    if definition
        .string_array_fields
        .iter()
        .any(|field| field == key)
    {
        return value
            .as_array()
            .is_some_and(|items| items.iter().all(Value::is_string));
    }
    value.is_string() || value.is_boolean() || value.is_number()
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
        if allowed_fields(req.event_type.as_str()).is_none() {
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
            if !allowed_fields(req.event_type.as_str())
                .is_some_and(|fields| fields.iter().any(|field| field == key))
            {
                return Err(EffectError::invalid_input(format!(
                    "field '{key}' is not allowed for {}",
                    req.event_type
                )));
            }
            if !valid_field_value(req.event_type.as_str(), key, value) {
                return Err(EffectError::invalid_input(format!(
                    "field '{key}' must be a scalar aggregate dimension"
                )));
            }
        }

        let event_log = self.ctx.event_log().ok_or_else(|| {
            EffectError::custom("tl_event_log_unavailable", "event log unavailable")
        })?;
        let (event_id, run_seq) = event_log
            .append_with_seq(&req.event_type, ctx.agent_name.as_str(), &payload)
            .map_err(|error| EffectError::custom("tl_event_append_failed", error.to_string()))?;

        Ok(EmitEventResponse { event_id, run_seq })
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
                    event_type: "tl.dispatch_intended".to_string(),
                    payload: br#"{"slice_id":"leaf-a","intent_id":"intent-1","boundary":"dispatch_intended","started_at":1}"#.to_vec(),
                },
                &context(),
            )
            .await
            .unwrap();

        assert!(!response.event_id.is_empty());
    }

    #[tokio::test]
    async fn accepts_dispatch_telemetry_agent_type_and_model() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));

        let response = handler
            .emit_event(
                EmitEventRequest {
                    event_type: "tl.spawn_requested".to_string(),
                    payload: br#"{"slice_id":"leaf-a","intent_id":"intent-1","boundary":"spawn_requested","started_at":1,"harness":"opencode","agent_type":"opencode","model":"opencode-go/deepseek-v4-pro"}"#.to_vec(),
                },
                &context(),
            )
            .await
            .unwrap();

        assert!(!response.event_id.is_empty());
    }

    #[test]
    fn shared_contract_carries_attempt_and_review_reason() {
        let dispatch_fields = allowed_fields("tl.dispatch_confirmed").unwrap();
        assert!(dispatch_fields.iter().any(|field| field == "attempt"));
        let parked_fields = allowed_fields("tl.slice_parked").unwrap();
        assert!(parked_fields.iter().any(|field| field == "reason"));
    }

    #[tokio::test]
    async fn accepts_dispatch_attempt_and_review_park_reason() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));

        for event_type in [
            "tl.dispatch_intended",
            "tl.spawn_requested",
            "tl.spawn_request_accepted",
            "tl.spawn_request_failed",
            "tl.dispatch_confirmed",
            "tl.dispatch_reconciliation_started",
            "tl.dispatch_reconciliation_completed",
        ] {
            handler
                .emit_event(
                    EmitEventRequest {
                        event_type: event_type.to_string(),
                        payload: format!(
                            r#"{{"slice_id":"leaf-a","intent_id":"intent-1","boundary":"{event_type}","started_at":1,"attempt":2}}"#
                        )
                        .into_bytes(),
                    },
                    &context(),
                )
                .await
                .unwrap();
        }

        handler
            .emit_event(
                EmitEventRequest {
                    event_type: "tl.slice_parked".to_string(),
                    payload: br#"{"slice_id":"leaf-a","park_cause":"review_stuck","reason":"reviewer unavailable"}"#.to_vec(),
                },
                &context(),
            )
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn accepts_task_budget_and_worker_reconciliation_events() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));
        for (event_type, payload) in [
            (
                "agent.task_budget_exceeded",
                br#"{"slice_id":"leaf-a","runtime_agent_id":"agent-leaf-a","generation":null,"elapsed_seconds":3601}"#.to_vec(),
            ),
            (
                "worker.terminal_reconciled",
                br#"{"slice_id":"leaf-a","runtime_agent_id":"agent-leaf-a","classification":"terminal_exit","stderr_tail":null}"#.to_vec(),
            ),
        ] {
            handler
                .emit_event(
                    EmitEventRequest {
                        event_type: event_type.to_string(),
                        payload,
                    },
                    &context(),
                )
                .await
                .unwrap();
        }
    }

    #[tokio::test]
    async fn accepts_slice_abandonment_dimensions() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));

        let response = handler
            .emit_event(
                EmitEventRequest {
                    event_type: "tl.slice_abandoned".to_string(),
                    payload: br#"{"slice_id":"leaf-a","attempt":2,"pr_number":42,"head_sha":"abc123","invocation_id":"inv-2","operator_source":"operator","cause":"retry"}"#.to_vec(),
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

    #[tokio::test]
    async fn appends_ordered_stage_observability_events() {
        let directory = tempdir().unwrap();
        let mut services = Services::test();
        services.event_log = Some(Arc::new(
            EventLog::open(directory.path().join("logs")).unwrap(),
        ));
        let handler = TlHandler::new(Arc::new(services));

        for event_type in ["tl.stage_started", "tl.stage_completed"] {
            let response = handler
                .emit_event(
                    EmitEventRequest {
                        event_type: event_type.to_string(),
                        payload: br#"{"order":1,"sub_tl_ids":["sub-a","sub-b"],"run_id":"root"}"#
                            .to_vec(),
                    },
                    &context(),
                )
                .await
                .unwrap();
            assert!(!response.event_id.is_empty());
        }
    }
}
