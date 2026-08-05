//! Shared, fail-open capture of bounded semantic session-memory records.

use crate::effects::EffectContext;
use crate::services::{HasSessionMemory, MemoryKind, NewMemoryRecord};

const MAX_CAPTURE_SUMMARY_CHARS: usize = 200;
const MAX_CAPTURE_DETAIL_BYTES: usize = 4096;
const MAX_CAPTURE_METADATA_BYTES: usize = 2048;

/// Semantic data captured after an operation has already produced its result.
///
/// Callers should provide references and concise descriptions rather than raw
/// requests, tool output, or diagnostic logs.
#[derive(Debug, Clone)]
pub struct MemoryCapture {
    pub issue_id: Option<i64>,
    pub kind: MemoryKind,
    pub importance: i32,
    pub summary: String,
    pub detail: Option<String>,
    pub metadata: Option<serde_json::Value>,
}

/// Append a bounded memory record without changing the caller's success path.
///
/// The returned id is only an observation aid. `None` means the record was
/// rejected or could not be appended; the append error is intentionally not
/// exposed to the originating operation.
pub fn capture_memory<C>(ctx: &EffectContext, services: &C, capture: MemoryCapture) -> Option<i64>
where
    C: HasSessionMemory,
{
    let kind = capture.kind;
    let agent_id = ctx.agent_name.to_string();
    let detail = capture
        .detail
        .as_deref()
        .map(|value| truncate_utf8(value, MAX_CAPTURE_DETAIL_BYTES));
    let metadata = bounded_metadata(capture.metadata, kind, &agent_id);
    let record = NewMemoryRecord {
        run_id: root_run_id(&ctx.birth_branch),
        agent_id: agent_id.clone(),
        birth_branch: ctx.birth_branch.to_string(),
        issue_id: capture.issue_id,
        kind,
        importance: capture.importance,
        summary: capture
            .summary
            .trim()
            .chars()
            .take(MAX_CAPTURE_SUMMARY_CHARS)
            .collect(),
        detail,
        supersedes_id: None,
        metadata_json: metadata,
    };

    match services.session_memory().append(record) {
        Ok(record_id) => Some(record_id),
        Err(error) => {
            tracing::warn!(
                capture_kind = %kind,
                agent = %agent_id,
                birth_branch = %ctx.birth_branch,
                error = %error,
                "Session memory capture failed; continuing without ledger record"
            );
            None
        }
    }
}

fn root_run_id(branch: &crate::domain::BirthBranch) -> String {
    let mut root = branch.clone();
    while let Some(parent) = root.parent() {
        root = parent;
    }
    root.to_string()
}

fn truncate_utf8(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_string();
    }

    let mut end = 0;
    for (index, character) in value.char_indices() {
        let next = index + character.len_utf8();
        if next > max_bytes {
            break;
        }
        end = next;
    }
    value[..end].to_string()
}

fn bounded_metadata(
    metadata: Option<serde_json::Value>,
    kind: MemoryKind,
    agent_id: &str,
) -> Option<String> {
    let metadata = metadata?;
    let serialized = match serde_json::to_string(&metadata) {
        Ok(serialized) => serialized,
        Err(error) => {
            tracing::warn!(
                capture_kind = %kind,
                agent = %agent_id,
                error = %error,
                "Session memory metadata could not be serialized; omitting metadata"
            );
            return None;
        }
    };
    if serialized.len() > MAX_CAPTURE_METADATA_BYTES {
        tracing::warn!(
            capture_kind = %kind,
            agent = %agent_id,
            metadata_bytes = serialized.len(),
            metadata_cap = MAX_CAPTURE_METADATA_BYTES,
            "Session memory metadata exceeded the capture cap; omitting metadata"
        );
        None
    } else {
        Some(serialized)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;
    use crate::services::{HasSessionMemory, MemoryFilter, SessionMemoryService};
    use std::sync::Arc;

    struct TestServices {
        memory: Arc<SessionMemoryService>,
    }

    impl HasSessionMemory for TestServices {
        fn session_memory(&self) -> &Arc<SessionMemoryService> {
            &self.memory
        }
    }

    fn test_context() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("worker-gemini").unwrap(),
            birth_branch: BirthBranch::try_from_str("main.tl.worker-gemini").unwrap(),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    fn test_services() -> TestServices {
        TestServices {
            memory: Arc::new(SessionMemoryService::open_in_memory().unwrap()),
        }
    }

    fn capture(summary: &str) -> MemoryCapture {
        MemoryCapture {
            issue_id: Some(629),
            kind: MemoryKind::NextAction,
            importance: 80,
            summary: summary.to_string(),
            detail: Some("bounded detail".to_string()),
            metadata: Some(serde_json::json!({"source": "test", "attempt": 1})),
        }
    }

    #[test]
    fn maps_root_run_and_preserves_agent_identity() {
        let services = test_services();
        let record_id = capture_memory(&test_context(), &services, capture("continue work"));
        assert!(record_id.is_some());

        let records = services.memory.list(MemoryFilter::default()).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].run_id, "main");
        assert_eq!(records[0].agent_id, "worker-gemini");
        assert_eq!(records[0].birth_branch, "main.tl.worker-gemini");
        assert_eq!(records[0].issue_id, Some(629));
    }

    #[test]
    fn bounds_detail_and_omits_oversized_metadata() {
        let services = test_services();
        let mut input = capture("bounded capture");
        input.detail = Some("é".repeat(MAX_CAPTURE_DETAIL_BYTES));
        input.metadata = Some(serde_json::json!({
            "output": "x".repeat(MAX_CAPTURE_METADATA_BYTES)
        }));

        assert!(capture_memory(&test_context(), &services, input).is_some());
        let records = services.memory.list(MemoryFilter::default()).unwrap();
        assert_eq!(
            records[0].detail.as_ref().unwrap().len(),
            MAX_CAPTURE_DETAIL_BYTES
        );
        assert_eq!(records[0].metadata_json, None);
    }

    #[test]
    fn invalid_append_is_fail_open() {
        let services = test_services();
        let result = capture_memory(&test_context(), &services, capture("   "));
        assert_eq!(result, None);
        assert!(services
            .memory
            .list(MemoryFilter::default())
            .unwrap()
            .is_empty());
    }

    #[test]
    fn invalid_importance_is_fail_open() {
        let services = test_services();
        let mut input = capture("valid summary");
        input.importance = 101;
        let result = capture_memory(&test_context(), &services, input);
        assert_eq!(result, None);
        assert!(services
            .memory
            .list(MemoryFilter::default())
            .unwrap()
            .is_empty());
    }
}
