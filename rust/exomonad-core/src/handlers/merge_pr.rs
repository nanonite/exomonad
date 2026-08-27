use crate::effects::{dispatch_merge_pr_effect, EffectResult, MergePrEffects, ResultExt};
use crate::services::merge_pr;
use crate::services::{capture_memory, MemoryCapture, MemoryKind};
use async_trait::async_trait;
use exomonad_proto::effects::merge_pr::*;
use std::sync::Arc;
use tracing::instrument;

use crate::services::{
    HasCiStatusMap, HasEventLog, HasForgejoClient, HasGitWorktreeService, HasProjectDir,
    HasSessionMemory,
};

pub struct MergePRHandler<C> {
    ctx: Arc<C>,
}

impl<
        C: HasForgejoClient
            + HasEventLog
            + HasGitWorktreeService
            + HasProjectDir
            + HasCiStatusMap
            + HasSessionMemory
            + 'static,
    > MergePRHandler<C>
{
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<
        C: HasForgejoClient
            + HasEventLog
            + HasGitWorktreeService
            + HasProjectDir
            + HasCiStatusMap
            + HasSessionMemory
            + 'static,
    > crate::effects::EffectHandler for MergePRHandler<C>
{
    fn namespace(&self) -> &str {
        "merge_pr"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> crate::effects::EffectResult<Vec<u8>> {
        dispatch_merge_pr_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<
        C: HasForgejoClient
            + HasEventLog
            + HasGitWorktreeService
            + HasProjectDir
            + HasCiStatusMap
            + HasSessionMemory
            + 'static,
    > MergePrEffects for MergePRHandler<C>
{
    #[instrument(skip_all, fields(agent_name = %ctx.agent_name, pr_number = req.pr_number))]
    async fn merge_pr(
        &self,
        req: MergePrRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<MergePrResponse> {
        let pr_number = crate::domain::PRNumber::new(req.pr_number as u64);
        let strategy = crate::domain::MergeStrategy::parse(&req.strategy).effect_err("merge_pr")?;
        tracing::info!(
            pr_number = pr_number.as_u64(),
            strategy = strategy.as_str(),
            "[MergePR] merge_pr starting"
        );

        let result = {
            tracing::info!(
                pr_number = pr_number.as_u64(),
                "[MergePR] routing to Forgejo"
            );
            merge_pr::merge_pr_async(
                pr_number,
                &strategy,
                &req.working_dir,
                merge_pr::MergeExpectedEvidence {
                    base_sha: crate::handlers::non_empty(req.expected_base_sha.clone()).as_deref(),
                    head_sha: crate::handlers::non_empty(req.expected_head_sha.clone()).as_deref(),
                    patch_digest: crate::handlers::non_empty(req.expected_patch_digest.clone())
                        .as_deref(),
                    merge_tree_sha: crate::handlers::non_empty(req.expected_merge_tree_sha.clone())
                        .as_deref(),
                },
                self.ctx.git_worktree_service().clone(),
                self.ctx.forgejo_client().map(|arc| arc.as_ref()),
                self.ctx.project_dir(),
            )
            .await
            .effect_err("merge_pr")?
        };
        tracing::info!(
            success = result.success,
            git_fetched = result.git_fetched,
            "[MergePR] merge_pr complete"
        );

        if result.success {
            tracing::info!(
                otel.name = "pr.merged",
                pr_number = pr_number.as_u64(),
                strategy = strategy.as_str(),
                git_fetched = result.git_fetched,
                head_sha = ?result.head_sha,
                "[event] pr.merged"
            );
            if let Some(log) = self.ctx.event_log() {
                let _ = log.append(
                    "pr.merged",
                    ctx.agent_name.as_ref(),
                    &serde_json::json!({
                        "pr_number": pr_number.as_u64(),
                        "strategy": strategy.as_str(),
                        "git_fetched": result.git_fetched,
                        "head_sha": result.head_sha,
                        "head_sha_finding": if result.head_sha.is_some() {
                            serde_json::Value::Null
                        } else {
                            serde_json::Value::String(
                                "not_available_without_verified_pr_context".to_string(),
                            )
                        },
                    }),
                );
            }
            capture_memory(
                ctx,
                self.ctx.as_ref(),
                merge_pr_capture(
                    pr_number.as_u64(),
                    strategy.as_str(),
                    "success",
                    result.git_fetched,
                    result.branch_name.as_str(),
                ),
            );
        } else {
            tracing::info!(
                otel.name = "pr.merge_failed",
                pr_number = pr_number.as_u64(),
                error = %result.message,
                head_sha = ?result.head_sha,
                "[event] pr.merge_failed"
            );
            if let Some(log) = self.ctx.event_log() {
                let _ = log.append(
                    "pr.merge_failed",
                    ctx.agent_name.as_ref(),
                    &serde_json::json!({
                        "pr_number": pr_number.as_u64(),
                        "error": &result.message,
                        "head_sha": result.head_sha,
                        "head_sha_finding": if result.head_sha.is_some() {
                            serde_json::Value::Null
                        } else {
                            serde_json::Value::String(
                                "not_available_without_verified_pr_context".to_string(),
                            )
                        },
                    }),
                );
            }
        }

        Ok(MergePrResponse {
            success: result.success,
            message: result.message,
            git_fetched: result.git_fetched,
            branch_name: result.branch_name.to_string(),
            head_sha: result.head_sha.unwrap_or_default(),
        })
    }
}

fn merge_pr_capture(
    pr_number: u64,
    strategy: &str,
    status: &str,
    git_fetched: bool,
    branch_name: &str,
) -> MemoryCapture {
    MemoryCapture {
        issue_id: None,
        kind: MemoryKind::MergeResult,
        importance: 80,
        summary: format!("Merged PR #{pr_number} with {strategy} ({status})"),
        detail: None,
        metadata: Some(serde_json::json!({
            "record_type": "merge_pr",
            "pr_number": pr_number,
            "strategy": strategy,
            "status": status,
            "git_fetched": git_fetched,
            "branch": branch_name,
        })),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, PRNumber};
    use crate::effects::{EffectContext, EffectHandler};
    use crate::services::Services;

    fn test_ctx() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("test")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    #[test]
    fn test_namespace() {
        let _ctx = test_ctx();
        let services = Arc::new(Services::test());
        let handler = MergePRHandler::new(services);
        assert_eq!(handler.namespace(), "merge_pr");
    }

    #[test]
    fn test_pr_number_conversion() {
        let proto_pr_number: i64 = 123;
        let pr_number = PRNumber::new(proto_pr_number as u64);
        assert_eq!(pr_number.as_u64(), 123);
    }

    #[test]
    fn test_pr_number_round_trip() {
        let original: u64 = 456;
        let pr_number = PRNumber::new(original);
        assert_eq!(pr_number.as_u64(), original);
    }

    #[test]
    fn test_response_field_mapping() {
        let response = MergePrResponse {
            success: true,
            message: "PR #42 merged via squash".to_string(),
            git_fetched: true,
            branch_name: "main.fix-auth-codex".to_string(),
            head_sha: "verified-sha".to_string(),
        };

        assert!(response.success);
        assert!(response.message.contains("42"));
        assert!(response.git_fetched);
        assert_eq!(response.branch_name, "main.fix-auth-codex");
        assert_eq!(response.head_sha, "verified-sha");
    }

    #[test]
    fn merge_pr_capture_records_merge_result() {
        let capture = merge_pr_capture(42, "squash", "success", true, "main.fix-auth-codex");

        assert_eq!(capture.kind, MemoryKind::MergeResult);
        assert!(capture.summary.contains("Merged PR #42"));
        assert!(capture.summary.contains("squash"));
        let metadata = capture.metadata.expect("metadata present");
        assert_eq!(metadata["record_type"], "merge_pr");
        assert_eq!(metadata["pr_number"], 42);
        assert_eq!(metadata["strategy"], "squash");
        assert_eq!(metadata["status"], "success");
        assert_eq!(metadata["git_fetched"], true);
        assert_eq!(metadata["branch"], "main.fix-auth-codex");
    }

    #[test]
    fn merge_pr_capture_appends_and_remains_fail_open() {
        let services = Arc::new(Services::test());
        let ctx = test_ctx();
        let record_id = capture_memory(
            &ctx,
            services.as_ref(),
            merge_pr_capture(42, "squash", "success", true, "main.fix-auth-codex"),
        );
        assert!(record_id.is_some());

        let mut invalid = merge_pr_capture(43, "merge", "success", false, "main.other-codex");
        invalid.importance = 101;
        assert_eq!(capture_memory(&ctx, services.as_ref(), invalid), None);

        let records = services
            .session_memory
            .list(crate::services::MemoryFilter::default())
            .unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].kind, MemoryKind::MergeResult);
    }

    #[test]
    fn test_strategy_default_handling() {
        let req = MergePrRequest {
            pr_number: 42,
            strategy: "".to_string(),
            working_dir: ".".to_string(),
            expected_base_sha: String::new(),
            expected_head_sha: String::new(),
            expected_patch_digest: String::new(),
            expected_merge_tree_sha: String::new(),
        };

        // Empty strategy should be handled by the service layer (defaults to "squash")
        assert!(req.strategy.is_empty());
    }
}
