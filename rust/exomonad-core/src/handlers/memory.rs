//! Session-memory effect handler for the `memory.*` namespace.

use async_trait::async_trait;
use exomonad_proto::effects::memory::{
    MemoryAppendRequest, MemoryAppendResponse, MemoryKind as ProtoMemoryKind, MemoryListRequest,
    MemoryListResponse, MemoryRecord,
};
use prost::Message;
use std::sync::Arc;

use crate::domain::BirthBranch;
use crate::effects::{
    dispatch_memory_effect, EffectContext, EffectError, EffectHandler, EffectResult, MemoryEffects,
};
use crate::services::agent_control::AgentControlService;
use crate::services::continuation::adapters::{
    AgentAdapter, ChainlinkAdapter, ForgejoAdapter, InboxAdapter,
};
use crate::services::continuation::{renderer, BriefInputs, SectionData};
use crate::services::{
    AgentResolver, HasAgentResolver, HasForgejoClient, HasGitHubClient, HasGitWorktreeService,
    HasInboxStore, HasProjectDir, HasSessionMemory, HasTeamRegistry, MemoryFilter, MemoryKind,
    NewMemoryRecord,
};

/// Host-side handler for the append-only session-memory ledger and continuation brief.
pub struct MemoryHandler<C> {
    ctx: Arc<C>,
    agent_control: Arc<AgentControlService<C>>,
    resolver: Arc<AgentResolver>,
}

impl<C> MemoryHandler<C> {
    pub fn new(
        ctx: Arc<C>,
        agent_control: Arc<AgentControlService<C>>,
        resolver: Arc<AgentResolver>,
    ) -> Self {
        Self {
            ctx,
            agent_control,
            resolver,
        }
    }
}

#[derive(Clone, PartialEq, Message)]
struct MemoryBriefResponse {
    #[prost(string, tag = "1")]
    markdown: String,
}

#[async_trait]
impl<C> EffectHandler for MemoryHandler<C>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    fn namespace(&self) -> &str {
        "memory"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &EffectContext,
    ) -> EffectResult<Vec<u8>> {
        tracing::info!(
            effect_type,
            payload_bytes = payload.len(),
            "Handling memory effect"
        );
        if effect_type == "memory.brief" {
            let response = self.brief(ctx).await?;
            return Ok(response.encode_to_vec());
        }
        dispatch_memory_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<C> MemoryEffects for MemoryHandler<C>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    async fn append(
        &self,
        req: MemoryAppendRequest,
        ctx: &EffectContext,
    ) -> EffectResult<MemoryAppendResponse> {
        let kind = service_kind(req.kind)?;
        let run_id = root_run_id(&ctx.birth_branch);
        let record = NewMemoryRecord {
            run_id,
            agent_id: ctx.agent_name.to_string(),
            birth_branch: ctx.birth_branch.to_string(),
            issue_id: (req.issue_id != 0).then_some(req.issue_id),
            kind,
            importance: if req.importance == 0 {
                50
            } else {
                req.importance
            },
            summary: req.summary,
            detail: non_empty(req.detail),
            supersedes_id: (req.supersedes_id != 0).then_some(req.supersedes_id),
            metadata_json: non_empty(req.metadata_json),
        };
        let kind_name = record.kind.to_string();
        let summary_len = record.summary.len();
        let id = self.ctx.session_memory().append(record).map_err(|error| {
            tracing::warn!(kind = %kind_name, error = %error, "Rejected session memory append");
            EffectError::invalid_input(error.to_string())
        })?;
        tracing::info!(record_id = id, kind = %kind_name, summary_len, "Appended session memory effect");
        Ok(MemoryAppendResponse { id })
    }

    async fn list(
        &self,
        req: MemoryListRequest,
        ctx: &EffectContext,
    ) -> EffectResult<MemoryListResponse> {
        let kind = if req.kind == 0 {
            None
        } else {
            Some(service_kind(req.kind)?)
        };
        let limit = if req.limit < 0 {
            return Err(EffectError::invalid_input(
                "memory list limit must not be negative",
            ));
        } else if req.limit == 0 {
            None
        } else {
            Some(req.limit as usize)
        };
        let filter = MemoryFilter {
            run_id: Some(root_run_id(&ctx.birth_branch)),
            agent_id: non_empty(req.agent_id),
            issue_id: (req.issue_id != 0).then_some(req.issue_id),
            kind,
            min_importance: (req.min_importance != 0).then_some(req.min_importance),
            limit,
        };
        let records = self.ctx.session_memory().list(filter).map_err(|error| {
            tracing::error!(error = %error, "Failed to list session memory records");
            EffectError::custom("memory_service_error", error.to_string())
        })?;
        tracing::info!(
            record_count = records.len(),
            "Listed session memory records"
        );
        Ok(MemoryListResponse {
            records: records.into_iter().map(proto_record).collect(),
        })
    }
}

impl<C> MemoryHandler<C>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    async fn brief(&self, ctx: &EffectContext) -> EffectResult<MemoryBriefResponse> {
        let project_dir = self.ctx.project_dir().to_path_buf();
        let chainlink = ChainlinkAdapter::new(project_dir.clone());
        let agent_adapter = AgentAdapter::new(self.agent_control.clone(), self.resolver.clone());
        let forgejo = ForgejoAdapter::new(project_dir, self.ctx.forgejo_client().cloned());
        let inbox = InboxAdapter::new(self.ctx.inbox_store().clone());

        let (issues, sessions, agents, open_prs) = tokio::join!(
            chainlink.issues(),
            chainlink.sessions(),
            agent_adapter.agents(),
            forgejo.open_prs(),
        );
        let issue_detail = active_issue_detail(&chainlink, &sessions, ctx).await;
        let run_id = root_run_id(&ctx.birth_branch);
        let ledger = self
            .ctx
            .session_memory()
            .list(MemoryFilter {
                run_id: Some(run_id.clone()),
                ..Default::default()
            })
            .map_err(|error| {
                tracing::error!(error = %error, "Failed to load session memory for continuation brief");
                EffectError::custom("memory_service_error", error.to_string())
            })?;
        let inputs = BriefInputs {
            run_id,
            agent_id: ctx.agent_name.to_string(),
            role: if ctx.birth_branch.depth() == 0 {
                "root".to_string()
            } else {
                "tl".to_string()
            },
            issues,
            issue_detail,
            sessions,
            unread_summary: inbox.unread_summary(),
            agents,
            open_prs,
        };
        let markdown = renderer::render_tl(&inputs, &ledger);
        tracing::info!(
            markdown_bytes = markdown.len(),
            ledger_records = ledger.len(),
            "Rendered continuation brief"
        );
        Ok(MemoryBriefResponse { markdown })
    }
}

async fn active_issue_detail(
    chainlink: &ChainlinkAdapter,
    sessions: &SectionData<Vec<crate::services::continuation::adapters::ChainlinkSession>>,
    ctx: &EffectContext,
) -> SectionData<crate::services::continuation::adapters::ChainlinkIssueDetail> {
    let SectionData::Available(sessions) = sessions else {
        return match sessions {
            SectionData::Unavailable { reason } => SectionData::Unavailable {
                reason: format!("sessions: {reason}"),
            },
            SectionData::Available(_) => unreachable!(),
        };
    };
    let issue_id = sessions
        .iter()
        .find(|session| {
            session.agent_id.as_deref() == Some(ctx.agent_name.as_str())
                || session.agent_id.as_deref() == Some(ctx.birth_branch.as_str())
        })
        .and_then(|session| session.active_issue_id);
    match issue_id {
        Some(issue_id) => chainlink.issue_detail(issue_id).await,
        None => SectionData::Unavailable {
            reason: "no active Chainlink issue for the current agent".to_string(),
        },
    }
}

fn service_kind(value: i32) -> EffectResult<MemoryKind> {
    let proto_kind = ProtoMemoryKind::try_from(value).map_err(|_| {
        EffectError::invalid_input(format!("unrecognized session memory kind: {value}"))
    })?;
    Ok(match proto_kind {
        ProtoMemoryKind::Unspecified => MemoryKind::Unspecified,
        ProtoMemoryKind::OriginalPlan => MemoryKind::OriginalPlan,
        ProtoMemoryKind::WavePlan => MemoryKind::WavePlan,
        ProtoMemoryKind::SpawnedChild => MemoryKind::SpawnedChild,
        ProtoMemoryKind::ChildHandoff => MemoryKind::ChildHandoff,
        ProtoMemoryKind::Blocker => MemoryKind::Blocker,
        ProtoMemoryKind::Decision => MemoryKind::Decision,
        ProtoMemoryKind::ReviewFeedback => MemoryKind::ReviewFeedback,
        ProtoMemoryKind::FixDirection => MemoryKind::FixDirection,
        ProtoMemoryKind::MergeResult => MemoryKind::MergeResult,
        ProtoMemoryKind::CiResult => MemoryKind::CiResult,
        ProtoMemoryKind::NextAction => MemoryKind::NextAction,
        ProtoMemoryKind::HumanClarification => MemoryKind::HumanClarification,
        ProtoMemoryKind::SessionSummary => MemoryKind::SessionSummary,
    })
}

fn proto_kind(kind: MemoryKind) -> i32 {
    match kind {
        MemoryKind::Unspecified => ProtoMemoryKind::Unspecified as i32,
        MemoryKind::OriginalPlan => ProtoMemoryKind::OriginalPlan as i32,
        MemoryKind::WavePlan => ProtoMemoryKind::WavePlan as i32,
        MemoryKind::SpawnedChild => ProtoMemoryKind::SpawnedChild as i32,
        MemoryKind::ChildHandoff => ProtoMemoryKind::ChildHandoff as i32,
        MemoryKind::Blocker => ProtoMemoryKind::Blocker as i32,
        MemoryKind::Decision => ProtoMemoryKind::Decision as i32,
        MemoryKind::ReviewFeedback => ProtoMemoryKind::ReviewFeedback as i32,
        MemoryKind::FixDirection => ProtoMemoryKind::FixDirection as i32,
        MemoryKind::MergeResult => ProtoMemoryKind::MergeResult as i32,
        MemoryKind::CiResult => ProtoMemoryKind::CiResult as i32,
        MemoryKind::NextAction => ProtoMemoryKind::NextAction as i32,
        MemoryKind::HumanClarification => ProtoMemoryKind::HumanClarification as i32,
        MemoryKind::SessionSummary => ProtoMemoryKind::SessionSummary as i32,
    }
}

fn proto_record(record: crate::services::MemoryRecordRow) -> MemoryRecord {
    MemoryRecord {
        id: record.id,
        run_id: record.run_id,
        agent_id: record.agent_id,
        birth_branch: record.birth_branch,
        issue_id: record.issue_id.unwrap_or_default(),
        kind: proto_kind(record.kind),
        importance: record.importance,
        summary: record.summary,
        detail: record.detail.unwrap_or_default(),
        created_at: record.created_at,
        supersedes_id: record.supersedes_id.unwrap_or_default(),
        metadata_json: record.metadata_json.unwrap_or_default(),
    }
}

fn root_run_id(branch: &BirthBranch) -> String {
    let mut root = branch.clone();
    while let Some(parent) = root.parent() {
        root = parent;
    }
    root.to_string()
}

fn non_empty(value: String) -> Option<String> {
    (!value.is_empty()).then_some(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;
    use crate::services::Services;
    use exomonad_proto::effects::memory::MemoryKind;

    fn test_context() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("root").expect("literal is non-empty"),
            birth_branch: BirthBranch::try_from_str("main").expect("literal is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    fn handler() -> MemoryHandler<Services> {
        let services = Arc::new(Services::test());
        let agent_control = Arc::new(AgentControlService::new(services.clone()));
        MemoryHandler::new(
            services.clone(),
            agent_control,
            services.agent_resolver.clone(),
        )
    }

    fn append_request(kind: i32) -> MemoryAppendRequest {
        MemoryAppendRequest {
            kind,
            summary: "recorded decision".to_string(),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn append_and_list_effect_envelopes_round_trip() {
        let handler = handler();
        let ctx = test_context();
        let append = handler
            .handle(
                "memory.append",
                &append_request(MemoryKind::Decision as i32).encode_to_vec(),
                &ctx,
            )
            .await
            .unwrap();
        let response = MemoryAppendResponse::decode(append.as_slice()).unwrap();
        assert!(response.id > 0);

        let list = handler
            .handle(
                "memory.list",
                &MemoryListRequest::default().encode_to_vec(),
                &ctx,
            )
            .await
            .unwrap();
        let response = MemoryListResponse::decode(list.as_slice()).unwrap();
        assert_eq!(response.records.len(), 1);
        assert_eq!(response.records[0].summary, "recorded decision");
    }

    #[tokio::test]
    async fn invalid_kind_preserves_service_message_as_invalid_input() {
        let error = handler()
            .handle(
                "memory.append",
                &append_request(99).encode_to_vec(),
                &test_context(),
            )
            .await
            .unwrap_err();
        assert!(matches!(
            error,
            EffectError::InvalidInput { message }
                if message == "unrecognized session memory kind: 99"
        ));
    }

    #[tokio::test]
    async fn over_cap_preserves_service_message_as_invalid_input() {
        let mut request = append_request(MemoryKind::Blocker as i32);
        request.detail = "x".repeat(4097);
        let error = handler()
            .handle("memory.append", &request.encode_to_vec(), &test_context())
            .await
            .unwrap_err();
        assert!(matches!(
            error,
            EffectError::InvalidInput { message }
                if message == "session memory detail must be at most 4096 bytes"
        ));
    }

    #[tokio::test]
    async fn brief_effect_returns_rendered_markdown() {
        let response = handler()
            .handle("memory.brief", &[], &test_context())
            .await
            .unwrap();
        let response = MemoryBriefResponse::decode(response.as_slice()).unwrap();
        assert!(response
            .markdown
            .starts_with("<exomonad-continuation-brief>"));
    }
}
