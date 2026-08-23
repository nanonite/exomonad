//! Cross-provider one-shot lifecycle contracts.
//!
//! This module deliberately contains observability and rollout policy only.
//! PR publication, review verdicts, and CI remain the workflow state machine;
//! an invocation or a delivered message is never treated as authoritative.

use super::agent_control::{AgentType, InvocationRecord, InvocationStatus, InvocationTrigger};
use super::event_log::EventLog;
use super::guidance_queue::GuidanceBatch;
use serde::{Deserialize, Serialize};
use std::path::Path;
use tracing::warn;

pub const ONE_SHOT_LIFECYCLE_ENV: &str = "EXOMONAD_ONE_SHOT_LIFECYCLE";

/// Rollout mode for the cross-provider lifecycle contract.
///
/// The established one-shot behavior remains enabled in every mode. The mode
/// controls the new lifecycle checks and telemetry contract: `shadow` records
/// observations without changing authoritative workflow transitions, while
/// `disabled` makes that choice explicit and observable. Neither mode restores
/// stale pane or root-pane fallback behavior.
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum OneShotLifecycleMode {
    #[default]
    Enabled,
    Shadow,
    Disabled,
}

impl OneShotLifecycleMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Enabled => "enabled",
            Self::Shadow => "shadow",
            Self::Disabled => "disabled",
        }
    }

    pub fn parse(value: &str) -> Result<Self, String> {
        match value.trim().to_ascii_lowercase().as_str() {
            "enabled" | "on" | "true" | "1" => Ok(Self::Enabled),
            "shadow" | "observe" => Ok(Self::Shadow),
            "disabled" | "off" | "false" | "0" => Ok(Self::Disabled),
            other => Err(format!(
                "unsupported {ONE_SHOT_LIFECYCLE_ENV} value `{other}`; expected enabled, shadow, or disabled"
            )),
        }
    }

    /// Parse the process environment without allowing malformed configuration
    /// to disable safe routing or crash the host.
    pub fn from_env() -> Self {
        let Some(value) = std::env::var_os(ONE_SHOT_LIFECYCLE_ENV) else {
            return Self::default();
        };
        let value = value.to_string_lossy();
        match Self::parse(&value) {
            Ok(mode) => mode,
            Err(error) => {
                warn!(%error, "Ignoring malformed one-shot lifecycle rollout setting");
                Self::default()
            }
        }
    }

    pub const fn checks_enabled(self) -> bool {
        !matches!(self, Self::Disabled)
    }
}

/// Stable provider list used by deterministic contract tests and operator
/// documentation. `Process` is included because it is a supported AgentType,
/// although it has no workflow identity or provider-specific MCP setup.
pub const LIFECYCLE_PROVIDERS: [AgentType; 5] = [
    AgentType::Claude,
    AgentType::Codex,
    AgentType::Shoal,
    AgentType::OpenCode,
    AgentType::Process,
];

pub const fn provider_delivery_contract(agent_type: AgentType) -> &'static str {
    match agent_type {
        AgentType::Claude => "teams_inbox -> exact_tmux -> durable_inbox",
        AgentType::Shoal => "uds -> exact_tmux -> durable_inbox",
        AgentType::Codex | AgentType::OpenCode => "exact_tmux -> durable_inbox",
        AgentType::Process => "external_process -> durable_inbox",
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleEventKind {
    InvocationStarted,
    InvocationFinished,
    TaskBudgetExceeded,
    GuidanceDelivery,
}

impl LifecycleEventKind {
    pub const fn event_type(self) -> &'static str {
        match self {
            Self::InvocationStarted => "agent.invocation.started",
            Self::InvocationFinished => "agent.invocation.finished",
            Self::TaskBudgetExceeded => "agent.task_budget_exceeded",
            Self::GuidanceDelivery => "agent.guidance.delivery",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LifecycleTelemetry {
    pub source_agent: Option<String>,
    pub source: String,
    pub batch_id: Option<String>,
    pub item_id: Option<String>,
    pub queue_class: Option<String>,
    pub queue_seq: Option<i64>,
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub provider: String,
    pub runtime: String,
    pub harness: String,
    pub role: String,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
    pub runtime_agent_id: Option<String>,
    pub slice_id: Option<String>,
    pub branch: Option<String>,
    pub worktree: Option<String>,
    pub trigger: Option<String>,
    pub pr_number: Option<u64>,
    pub issue_number: Option<u64>,
    pub head_sha: Option<String>,
    pub outcome: String,
    pub status: Option<String>,
    pub exit_code: Option<i32>,
    pub exit_reason: Option<String>,
    pub exit_classification: Option<String>,
    pub stderr_tail: Option<String>,
    pub delivery_vs_authoritative: String,
    pub rollout_mode: OneShotLifecycleMode,
    pub channel: Option<String>,
}

impl LifecycleTelemetry {
    fn from_invocation(record: &InvocationRecord, outcome: &str) -> Self {
        Self {
            source_agent: None,
            source: "lifecycle".to_string(),
            batch_id: None,
            item_id: None,
            queue_class: None,
            queue_seq: None,
            run_id: None,
            session_id: None,
            provider: record.runtime.suffix().to_string(),
            runtime: record.runtime.suffix().to_string(),
            harness: "exomonad".to_string(),
            role: role_for_trigger(record.trigger).to_string(),
            invocation_id: Some(record.invocation_id.clone()),
            generation: Some(record.generation),
            runtime_agent_id: record.runtime_agent_id.clone(),
            slice_id: record.slice_id.clone(),
            branch: record.branch.clone(),
            worktree: record.worktree.clone(),
            trigger: Some(trigger_label(record.trigger).to_string()),
            pr_number: record.pr_number,
            issue_number: None,
            head_sha: record.head_sha.clone(),
            outcome: outcome.to_string(),
            status: Some(status_label(record.status).to_string()),
            exit_code: record.exit_code,
            exit_reason: record.exit_reason.clone(),
            exit_classification: record.exit_classification.clone(),
            stderr_tail: record.stderr_tail.clone(),
            delivery_vs_authoritative: "metadata_only".to_string(),
            rollout_mode: OneShotLifecycleMode::from_env(),
            channel: None,
        }
    }

    fn guidance(
        provider: AgentType,
        invocation: Option<&InvocationRecord>,
        source_agent: &str,
        outcome: &str,
        channel: &str,
    ) -> Self {
        Self {
            source_agent: Some(source_agent.to_string()),
            source: "lifecycle".to_string(),
            batch_id: None,
            item_id: None,
            queue_class: None,
            queue_seq: None,
            run_id: None,
            session_id: None,
            provider: invocation
                .map(|record| record.runtime.suffix().to_string())
                .unwrap_or_else(|| provider.suffix().to_string()),
            runtime: invocation
                .map(|record| record.runtime.suffix().to_string())
                .unwrap_or_else(|| provider.suffix().to_string()),
            harness: "exomonad".to_string(),
            role: invocation
                .map(|record| role_for_trigger(record.trigger).to_string())
                .unwrap_or_else(|| "unknown".to_string()),
            invocation_id: invocation.map(|record| record.invocation_id.clone()),
            generation: invocation.map(|record| record.generation),
            runtime_agent_id: invocation.and_then(|record| record.runtime_agent_id.clone()),
            slice_id: invocation.and_then(|record| record.slice_id.clone()),
            branch: invocation.and_then(|record| record.branch.clone()),
            worktree: invocation.and_then(|record| record.worktree.clone()),
            trigger: invocation.map(|record| trigger_label(record.trigger).to_string()),
            pr_number: invocation.and_then(|record| record.pr_number),
            issue_number: None,
            head_sha: invocation.and_then(|record| record.head_sha.clone()),
            outcome: outcome.to_string(),
            status: None,
            exit_code: invocation.and_then(|record| record.exit_code),
            exit_reason: invocation.and_then(|record| record.exit_reason.clone()),
            exit_classification: invocation.and_then(|record| record.exit_classification.clone()),
            stderr_tail: invocation.and_then(|record| record.stderr_tail.clone()),
            delivery_vs_authoritative: "delivery_only".to_string(),
            rollout_mode: OneShotLifecycleMode::from_env(),
            channel: Some(channel.to_string()),
        }
    }

    fn with_guidance_batch(mut self, batch: &GuidanceBatch) -> Self {
        self.batch_id = Some(batch.batch_id.clone());
        self.item_id = (batch.items.len() == 1).then(|| batch.items[0].item_id.clone());
        self.queue_class = Some(batch.queue_class.as_str().to_string());
        self.queue_seq = Some(batch.queue_seq);
        self.run_id = batch.identity.run_id.clone();
        self.session_id = batch.identity.session_id.clone();
        if let Some(runtime) = &batch.identity.runtime {
            self.runtime = runtime.clone();
            self.provider = runtime.clone();
        }
        if let Some(harness) = &batch.identity.harness {
            self.harness = harness.clone();
        }
        if let Some(role) = &batch.identity.role {
            self.role = role.clone();
        }
        if let Some(invocation_id) = &batch.identity.invocation_id {
            self.invocation_id = Some(invocation_id.clone());
        }
        if let Some(generation) = batch.identity.generation {
            self.generation = Some(generation);
        }
        self
    }
}

pub fn record_invocation_started(agent_dir: &Path, record: &InvocationRecord) {
    record_invocation_event(
        agent_dir,
        LifecycleEventKind::InvocationStarted,
        LifecycleTelemetry::from_invocation(record, "started"),
    );
}

pub fn record_invocation_finished(agent_dir: &Path, record: &InvocationRecord) {
    record_invocation_event(
        agent_dir,
        LifecycleEventKind::InvocationFinished,
        LifecycleTelemetry::from_invocation(record, "finished"),
    );
}

/// Record the result of guidance delivery. This is intentionally separate from
/// PR/review/CI events: successful injection cannot advance workflow state.
pub async fn record_guidance_delivery(
    project_dir: &Path,
    recipient: &str,
    from: &str,
    channel: &str,
    outcome: &str,
) {
    record_guidance_delivery_with_batch(project_dir, recipient, from, channel, outcome, None).await;
}

pub async fn record_guidance_delivery_with_batch(
    project_dir: &Path,
    recipient: &str,
    from: &str,
    channel: &str,
    outcome: &str,
    batch: Option<&GuidanceBatch>,
) {
    let invocation = current_invocation(project_dir, recipient).await;
    let provider = invocation
        .as_ref()
        .map(|record| record.runtime)
        .unwrap_or_else(|| AgentType::from_dir_name(recipient));
    let telemetry =
        LifecycleTelemetry::guidance(provider, invocation.as_ref(), from, outcome, channel);
    let telemetry = if let Some(batch) = batch {
        telemetry.with_guidance_batch(batch)
    } else {
        telemetry
    };
    tracing::info!(
        otel.name = "agent.guidance.delivery",
        provider = %telemetry.provider,
        role = %telemetry.role,
        invocation_id = ?telemetry.invocation_id,
        generation = ?telemetry.generation,
        trigger = ?telemetry.trigger,
        pr_number = ?telemetry.pr_number,
        issue_number = ?telemetry.issue_number,
        head_sha = ?telemetry.head_sha,
        outcome = %telemetry.outcome,
        channel = %channel,
        delivery_vs_authoritative = %telemetry.delivery_vs_authoritative,
        rollout_mode = %telemetry.rollout_mode.as_str(),
        "[event] agent.guidance.delivery"
    );
    append_project_event(
        project_dir,
        recipient,
        LifecycleEventKind::GuidanceDelivery,
        telemetry,
    );
}

fn record_invocation_event(
    agent_dir: &Path,
    kind: LifecycleEventKind,
    telemetry: LifecycleTelemetry,
) {
    tracing::info!(
        otel.name = kind.event_type(),
        provider = %telemetry.provider,
        role = %telemetry.role,
        invocation_id = ?telemetry.invocation_id,
        generation = ?telemetry.generation,
        trigger = ?telemetry.trigger,
        pr_number = ?telemetry.pr_number,
        issue_number = ?telemetry.issue_number,
        head_sha = ?telemetry.head_sha,
        outcome = %telemetry.outcome,
        status = ?telemetry.status,
        delivery_vs_authoritative = %telemetry.delivery_vs_authoritative,
        rollout_mode = %telemetry.rollout_mode.as_str(),
        "[event] lifecycle invocation"
    );
    if let Some((log, agent_id)) = event_log_for_agent(agent_dir) {
        append_event(&log, kind, &agent_id, telemetry);
    }
}

fn append_project_event(
    project_dir: &Path,
    agent_id: &str,
    kind: LifecycleEventKind,
    telemetry: LifecycleTelemetry,
) {
    let event_dir = project_dir.join(".exo/events");
    let Ok(log) = EventLog::open(event_dir) else {
        return;
    };
    append_event(&log, kind, agent_id, telemetry);
}

fn append_event(
    log: &EventLog,
    kind: LifecycleEventKind,
    agent_id: &str,
    telemetry: LifecycleTelemetry,
) {
    let Ok(data) = serde_json::to_value(telemetry) else {
        return;
    };
    if let Err(error) = log.append(kind.event_type(), agent_id, &data) {
        warn!(%error, event_type = kind.event_type(), "Failed to append lifecycle telemetry");
    }
}

fn event_log_for_agent(agent_dir: &Path) -> Option<(EventLog, String)> {
    let agents_dir = agent_dir.parent()?;
    let exo_dir = agents_dir.parent()?;
    if exo_dir.file_name()?.to_str()? != ".exo" {
        return None;
    }
    let agent_id = agent_dir.file_name()?.to_str()?.to_string();
    let log = EventLog::open(exo_dir.join("events")).ok()?;
    Some((log, agent_id))
}

async fn current_invocation(project_dir: &Path, agent_key: &str) -> Option<InvocationRecord> {
    let agents_dir = project_dir.join(".exo/agents");
    for candidate in agent_dir_candidates(agent_key) {
        let dir = agents_dir.join(candidate);
        if !dir.is_dir() {
            continue;
        }
        if let Some(record) = super::agent_control::read_invocation_conservatively(&dir).await {
            return Some(record);
        }
    }
    None
}

fn agent_dir_candidates(agent_key: &str) -> Vec<String> {
    let slug = agent_key
        .rsplit_once('.')
        .map(|(_, value)| value)
        .unwrap_or(agent_key);
    let mut candidates = vec![agent_key.to_string(), slug.to_string()];
    for suffix in ["claude", "shoal", "opencode", "codex", "process"] {
        candidates.push(format!("{slug}-{suffix}"));
        candidates.push(format!("{agent_key}-{suffix}"));
    }
    candidates
}

fn role_for_trigger(trigger: InvocationTrigger) -> &'static str {
    match trigger {
        InvocationTrigger::Review => "reviewer",
        InvocationTrigger::Spawn | InvocationTrigger::ResumePr => "dev",
    }
}

fn trigger_label(trigger: InvocationTrigger) -> &'static str {
    match trigger {
        InvocationTrigger::Spawn => "spawn",
        InvocationTrigger::ResumePr => "resume_pr",
        InvocationTrigger::Review => "review",
    }
}

fn status_label(status: InvocationStatus) -> &'static str {
    match status {
        InvocationStatus::Running => "running",
        InvocationStatus::Exited => "exited",
        InvocationStatus::Failed => "failed",
        InvocationStatus::Killed => "killed",
        InvocationStatus::TimedOut => "timed_out",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::agent_control::{
        finish_invocation, start_invocation, InvocationFinishResult, RoutingInfo,
    };
    use crate::services::tmux_ipc::WindowId;
    use std::fs;
    use tempfile::tempdir;

    fn routing() -> RoutingInfo {
        RoutingInfo::window(WindowId::parse("@42").expect("valid window"))
    }

    #[test]
    fn rollout_parser_is_explicit_and_safe() {
        assert_eq!(
            OneShotLifecycleMode::parse("enabled"),
            Ok(OneShotLifecycleMode::Enabled)
        );
        assert_eq!(
            OneShotLifecycleMode::parse("shadow"),
            Ok(OneShotLifecycleMode::Shadow)
        );
        assert_eq!(
            OneShotLifecycleMode::parse("disabled"),
            Ok(OneShotLifecycleMode::Disabled)
        );
        assert!(OneShotLifecycleMode::parse("root-fallback").is_err());
        assert!(!OneShotLifecycleMode::Disabled.checks_enabled());
        assert!(OneShotLifecycleMode::Shadow.checks_enabled());
    }

    #[test]
    fn provider_matrix_has_stable_contracts() {
        assert_eq!(LIFECYCLE_PROVIDERS.len(), 5);
        for provider in LIFECYCLE_PROVIDERS {
            assert!(!provider.suffix().is_empty());
            assert!(!provider_delivery_contract(provider).is_empty());
        }
        assert!(provider_delivery_contract(AgentType::Claude).contains("teams_inbox"));
        assert!(provider_delivery_contract(AgentType::Shoal).contains("uds"));
        assert!(provider_delivery_contract(AgentType::Process).contains("external_process"));
    }

    #[tokio::test]
    async fn matrix_records_restart_and_all_terminal_outcomes() {
        for provider in LIFECYCLE_PROVIDERS {
            let dir = tempdir().expect("tempdir");
            let first = start_invocation(
                dir.path(),
                provider,
                InvocationTrigger::Spawn,
                routing(),
                Some(585),
                Some("sha-old".to_string()),
            )
            .await
            .expect("first invocation");
            let second = start_invocation(
                dir.path(),
                provider,
                InvocationTrigger::ResumePr,
                routing(),
                Some(585),
                Some("sha-new".to_string()),
            )
            .await
            .expect("resumed invocation");
            assert_ne!(first.invocation_id, second.invocation_id);
            assert_eq!(second.generation, first.generation + 1);
            assert_eq!(
                finish_invocation(
                    dir.path(),
                    &first.invocation_id,
                    InvocationStatus::Exited,
                    Some(0)
                )
                .await
                .expect("stale finish"),
                InvocationFinishResult::IgnoredStale
            );

            for (status, exit_code) in [
                (InvocationStatus::Exited, Some(0)),
                (InvocationStatus::Failed, Some(17)),
                (InvocationStatus::Killed, Some(143)),
                (InvocationStatus::TimedOut, None),
            ] {
                let record = start_invocation(
                    dir.path(),
                    provider,
                    InvocationTrigger::Review,
                    routing(),
                    Some(585),
                    Some("sha-new".to_string()),
                )
                .await
                .expect("terminal invocation");
                let result =
                    finish_invocation(dir.path(), &record.invocation_id, status, exit_code)
                        .await
                        .expect("finish terminal invocation");
                let InvocationFinishResult::Finished(finished) = result else {
                    panic!("expected current invocation to finish");
                };
                assert_eq!(finished.status, status);
                assert_eq!(finished.exit_code, exit_code);
            }
        }
    }

    #[test]
    fn old_invocation_state_deserializes_without_new_optional_fields() {
        let old = serde_json::json!({
            "invocation_id": "legacy",
            "runtime": "codex",
            "trigger": "spawn",
            "routing": {"window_id": "@1"},
            "started_at": 1,
            "status": "exited"
        });
        let record: InvocationRecord = serde_json::from_value(old).expect("legacy state");
        assert_eq!(record.invocation_id, "legacy");
        assert_eq!(record.pr_number, None);
        assert_eq!(record.head_sha, None);
        assert_eq!(record.exit_classification, None);
        assert_eq!(record.stderr_tail, None);
    }

    #[test]
    fn invocation_telemetry_is_structured_and_delivery_is_non_authoritative() {
        let record = InvocationRecord {
            invocation_id: "inv-1".to_string(),
            runtime: AgentType::Codex,
            trigger: InvocationTrigger::Review,
            routing: routing(),
            started_at: 1,
            ended_at: Some(2),
            status: InvocationStatus::Exited,
            exit_code: Some(0),
            pr_number: Some(585),
            head_sha: Some("sha".to_string()),
            model: Some("gpt-5.6-luna".to_string()),
            effort: Some("xhigh".to_string()),
            generation: 1,
            runtime_agent_id: None,
            slice_id: None,
            branch: None,
            worktree: None,
            exit_reason: None,
            exit_classification: None,
            stderr_tail: None,
            prior_invocation_id: None,
            recovery_round: 0,
            authorization_source: None,
        };
        let telemetry = LifecycleTelemetry::from_invocation(&record, "finished");
        let value = serde_json::to_value(telemetry).expect("telemetry JSON");
        assert_eq!(value["provider"], "codex");
        assert_eq!(value["invocation_id"], "inv-1");
        assert_eq!(value["pr_number"], 585);
        assert_eq!(value["head_sha"], "sha");
        assert_eq!(value["delivery_vs_authoritative"], "metadata_only");

        let guidance = LifecycleTelemetry::guidance(
            AgentType::Codex,
            Some(&record),
            "root",
            "success",
            "tmux",
        );
        let guidance = serde_json::to_value(guidance).expect("guidance JSON");
        assert_eq!(guidance["source_agent"], "root");
        assert_eq!(guidance["delivery_vs_authoritative"], "delivery_only");
        assert_eq!(guidance["outcome"], "success");
    }

    #[test]
    fn invocation_events_are_written_to_existing_event_log_layout() {
        let root = tempdir().expect("tempdir");
        let agent_dir = root.path().join(".exo/agents/codex");
        fs::create_dir_all(&agent_dir).expect("agent dir");
        let record = InvocationRecord {
            invocation_id: "inv-1".to_string(),
            runtime: AgentType::Codex,
            trigger: InvocationTrigger::Spawn,
            routing: routing(),
            started_at: 1,
            ended_at: None,
            status: InvocationStatus::Running,
            exit_code: None,
            pr_number: None,
            head_sha: None,
            model: None,
            effort: None,
            generation: 1,
            runtime_agent_id: None,
            slice_id: None,
            branch: None,
            worktree: None,
            exit_reason: None,
            exit_classification: None,
            stderr_tail: None,
            prior_invocation_id: None,
            recovery_round: 0,
            authorization_source: None,
        };
        record_invocation_started(&agent_dir, &record);
        let files = fs::read_dir(root.path().join(".exo/events"))
            .expect("event directory")
            .collect::<Result<Vec<_>, _>>()
            .expect("event files");
        assert_eq!(files.len(), 1);
        let content = fs::read_to_string(files[0].path()).expect("event data");
        assert!(content.contains("agent.invocation.started"));
        assert!(content.contains("delivery_vs_authoritative"));
    }
}
