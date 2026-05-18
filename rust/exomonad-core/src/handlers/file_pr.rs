//! File PR effect handler for the `file_pr.*` namespace.
//!
//! Uses proto-generated types from `exomonad_proto::effects::file_pr`.

use super::non_empty;
use crate::domain::BranchName;
use crate::effects::{
    dispatch_file_pr_effect, EffectHandler, EffectResult, FilePrEffects, ResultExt,
};
use crate::services::file_pr::{self, FilePRInput};
use crate::services::file_pr_local;
use async_trait::async_trait;
use exomonad_proto::effects::file_pr::*;
use std::sync::Arc;
use tracing::instrument;

use crate::services::{HasEventLog, HasGitHubClient, HasGitWorktreeService, HasProjectDir};

/// File PR effect handler.
///
/// Handles all effects in the `file_pr.*` namespace by delegating to
/// the generated `dispatch_file_pr_effect` function.
pub struct FilePRHandler<C> {
    ctx: Arc<C>,
}

impl<C: HasGitHubClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
    FilePRHandler<C>
{
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<C: HasGitHubClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
    EffectHandler for FilePRHandler<C>
{
    fn namespace(&self) -> &str {
        "file_pr"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_file_pr_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<C: HasGitHubClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
    FilePrEffects for FilePRHandler<C>
{
    #[instrument(skip_all, fields(agent_name = %ctx.agent_name, pr_title = %req.title))]
    async fn file_pr(
        &self,
        req: FilePrRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<FilePrResponse> {
        tracing::info!(title = %req.title, "[FilePR] file_pr starting");
        let base_branch = non_empty(req.base_branch).map(|s| {
            BranchName::try_from_str(s.as_str()).expect("validated string input is non-empty")
        });

        let working_dir = ctx.working_dir.clone();

        let input = FilePRInput {
            title: req.title,
            body: req.body,
            base_branch,
            working_dir: Some(working_dir.to_string_lossy().to_string()),
        };

        let output = if self.ctx.github_client().is_some() {
            file_pr::file_pr_async(
                &input,
                self.ctx.git_worktree_service().clone(),
                self.ctx.github_client().map(|arc| arc.as_ref()),
            )
            .await
            .effect_err("file_pr")?
        } else {
            tracing::info!("[FilePR] No GitHub client — routing to local PR flow");
            file_pr_local::file_pr_local(
                &input,
                self.ctx.git_worktree_service().clone(),
                self.ctx.project_dir(),
                &crate::domain::Role::dev(),
                &ctx.agent_name,
            )
            .await
            .effect_err("file_pr")?
        };

        tracing::info!(
            pr_number = output.pr_number.as_u64(),
            created = output.created,
            "[FilePR] file_pr complete"
        );

        let event_type = if output.created {
            "pr.filed"
        } else {
            "pr.updated"
        };

        tracing::info!(
            otel.name = event_type,
            pr_number = output.pr_number.as_u64(),
            pr_url = %output.pr_url,
            head_branch = %output.head_branch,
            base_branch = %output.base_branch,
            created = output.created,
            title = %input.title,
            "[event] {}",
            event_type
        );

        if let Some(log) = self.ctx.event_log() {
            if let Err(e) = log.append(
                event_type,
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "pr_number": output.pr_number.as_u64(),
                    "pr_url": output.pr_url,
                    "head_branch": output.head_branch.to_string(),
                    "base_branch": output.base_branch.to_string(),
                    "created": output.created,
                    "title": input.title,
                }),
            ) {
                tracing::warn!(error = %e, "Failed to write event log");
            }
        }

        Ok(FilePrResponse {
            pr_url: output.pr_url,
            pr_number: output.pr_number.as_u64() as i64,
            head_branch: output.head_branch.to_string(),
            base_branch: output.base_branch.to_string(),
            created: output.created,
        })
    }

    #[instrument(skip_all, fields(agent_name = %ctx.agent_name, pr_number = req.pr_number))]
    async fn local_pr_get(
        &self,
        req: LocalPrGetRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<LocalPrResponse> {
        let prs_path = self.ctx.project_dir().join(".exo/prs.json");
        let registry = match file_pr_local::read_pr_registry(&prs_path).await {
            Ok(registry) => registry,
            Err(error) => {
                tracing::debug!(
                    error = %error,
                    path = %prs_path.display(),
                    "[FilePR] local PR registry unavailable"
                );
                return Ok(LocalPrResponse::default());
            }
        };

        Ok(registry
            .prs
            .get(&(req.pr_number as u64))
            .map(local_pr_response)
            .unwrap_or_default())
    }

    #[instrument(skip_all, fields(agent_name = %ctx.agent_name, branch = %req.branch))]
    async fn local_pr_get_for_branch(
        &self,
        req: LocalPrGetForBranchRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<LocalPrResponse> {
        let prs_path = self.ctx.project_dir().join(".exo/prs.json");
        let registry = match file_pr_local::read_pr_registry(&prs_path).await {
            Ok(registry) => registry,
            Err(error) => {
                tracing::debug!(
                    error = %error,
                    path = %prs_path.display(),
                    "[FilePR] local PR registry unavailable"
                );
                return Ok(LocalPrResponse::default());
            }
        };

        Ok(registry
            .prs
            .values()
            .find(|entry| entry.head_branch == req.branch)
            .map(local_pr_response)
            .unwrap_or_default())
    }
}

fn local_pr_response(entry: &file_pr_local::PrEntry) -> LocalPrResponse {
    LocalPrResponse {
        found: true,
        pr_number: entry.number as i64,
        head_branch: entry.head_branch.clone(),
        base_branch: entry.base_branch.clone(),
        author_agent: entry.author_agent.clone(),
        review_state: match entry.review_state {
            file_pr_local::LocalReviewState::PendingReview => "pending_review",
            file_pr_local::LocalReviewState::ChangesRequested => "changes_requested",
            file_pr_local::LocalReviewState::Approved => "approved",
        }
        .to_string(),
        last_head_sha: entry.last_head_sha.clone().unwrap_or_default(),
        reviewer_agent: entry.reviewer_agent.clone().unwrap_or_default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::{EffectContext, FilePrEffects};
    use crate::services::Services;
    use std::path::PathBuf;

    fn test_ctx(branch: &str) -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("test")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str(branch)
                .expect("validated string input is non-empty"),
            working_dir: crate::services::agent_control::resolve_working_dir(branch),
        }
    }

    #[test]
    fn test_namespace() {
        let services = Arc::new(Services::test());
        let handler = FilePRHandler::new(services);
        assert_eq!(handler.namespace(), "file_pr");
    }

    #[test]
    fn test_resolve_working_dir_root() {
        let ctx = test_ctx("main");
        let working_dir = ctx.working_dir.clone();
        assert_eq!(working_dir, PathBuf::from("."));
    }

    #[test]
    fn test_resolve_working_dir_spawned() {
        let ctx = test_ctx("main.feature");
        let working_dir = ctx.working_dir.clone();
        assert_eq!(working_dir, PathBuf::from(".exo/worktrees/feature/"));
    }

    #[test]
    fn test_base_branch_conversion_empty_is_none() {
        let base_branch = non_empty("".to_string()).map(|s| {
            BranchName::try_from_str(s.as_str()).expect("validated string input is non-empty")
        });
        assert!(
            base_branch.is_none(),
            "Empty string should become None (auto-detect)"
        );
    }

    #[test]
    fn test_base_branch_conversion_explicit() {
        let base_branch = non_empty("develop".to_string()).map(|s| {
            BranchName::try_from_str(s.as_str()).expect("validated string input is non-empty")
        });
        assert_eq!(base_branch.unwrap().to_string(), "develop");
    }

    #[test]
    fn test_response_field_conversion() {
        let pr_number = crate::domain::PRNumber::new(42);
        let head = BranchName::try_from_str("main.fix-auth-gemini")
            .expect("literal validated string is non-empty");
        let base = BranchName::try_from_str("main").expect("literal validated string is non-empty");

        let response = FilePrResponse {
            pr_url: "https://github.com/owner/repo/pull/42".to_string(),
            pr_number: pr_number.as_u64() as i64,
            head_branch: head.to_string(),
            base_branch: base.to_string(),
            created: true,
        };

        assert_eq!(response.pr_number, 42);
        assert_eq!(response.head_branch, "main.fix-auth-gemini");
        assert_eq!(response.base_branch, "main");
        assert!(response.created);
    }

    #[tokio::test]
    async fn local_pr_get_reads_registry_entry() {
        let tmp = tempfile::tempdir().unwrap();
        write_test_registry(tmp.path());
        let mut services = Services::test();
        services.project_dir = tmp.path().to_path_buf();
        let handler = FilePRHandler::new(Arc::new(services));

        let response = handler
            .local_pr_get(LocalPrGetRequest { pr_number: 7 }, &test_ctx("main"))
            .await
            .unwrap();

        assert!(response.found);
        assert_eq!(response.pr_number, 7);
        assert_eq!(response.head_branch, "main.fix-local-pr-codex");
        assert_eq!(response.author_agent, "fix-local-pr-codex");
        assert_eq!(response.review_state, "approved");
        assert_eq!(response.last_head_sha, "abc123");
        assert_eq!(response.reviewer_agent, "review-pr-7-codex");
    }

    #[tokio::test]
    async fn local_pr_get_for_branch_reads_registry_entry() {
        let tmp = tempfile::tempdir().unwrap();
        write_test_registry(tmp.path());
        let mut services = Services::test();
        services.project_dir = tmp.path().to_path_buf();
        let handler = FilePRHandler::new(Arc::new(services));

        let response = handler
            .local_pr_get_for_branch(
                LocalPrGetForBranchRequest {
                    branch: "main.fix-local-pr-codex".to_string(),
                },
                &test_ctx("main.fix-local-pr-codex"),
            )
            .await
            .unwrap();

        assert!(response.found);
        assert_eq!(response.pr_number, 7);
    }

    #[tokio::test]
    async fn local_pr_get_missing_returns_not_found_response() {
        let tmp = tempfile::tempdir().unwrap();
        write_test_registry(tmp.path());
        let mut services = Services::test();
        services.project_dir = tmp.path().to_path_buf();
        let handler = FilePRHandler::new(Arc::new(services));

        let response = handler
            .local_pr_get(LocalPrGetRequest { pr_number: 404 }, &test_ctx("main"))
            .await
            .unwrap();

        assert!(!response.found);
        assert_eq!(response.pr_number, 0);
        assert!(response.head_branch.is_empty());
    }

    fn write_test_registry(project_dir: &std::path::Path) {
        let exo_dir = project_dir.join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        std::fs::write(
            exo_dir.join("prs.json"),
            r#"{
  "prs": {
    "7": {
      "number": 7,
      "head_branch": "main.fix-local-pr-codex",
      "base_branch": "main",
      "title": "Fix local PR",
      "body": "Body",
      "author_agent": "fix-local-pr-codex",
      "author_role": "dev",
      "created_at": "2026-05-18T00:00:00Z",
      "state": "open",
      "review_state": "approved",
      "last_head_sha": "abc123",
      "reviewer_agent": "review-pr-7-codex"
    }
  },
  "next_number": 8
}
"#,
        )
        .unwrap();
    }
}
