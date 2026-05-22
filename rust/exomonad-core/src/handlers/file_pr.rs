//! File PR effect handler for the `file_pr.*` namespace.
//!
//! Uses proto-generated types from `exomonad_proto::effects::file_pr`.

use super::non_empty;
use crate::domain::BranchName;
use crate::effects::{
    dispatch_file_pr_effect, EffectError, EffectHandler, EffectResult, FilePrEffects, ResultExt,
};
use crate::services::file_pr::{self, FilePRInput};
use crate::services::repo;
use async_trait::async_trait;
use exomonad_proto::effects::file_pr::*;
use std::sync::Arc;
use tracing::instrument;

use crate::services::{HasEventLog, HasForgejoClient, HasGitWorktreeService, HasProjectDir};

/// File PR effect handler.
///
/// Handles all effects in the `file_pr.*` namespace by delegating to
/// the generated `dispatch_file_pr_effect` function.
pub struct FilePRHandler<C> {
    ctx: Arc<C>,
}

impl<C: HasForgejoClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
    FilePRHandler<C>
{
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<C: HasForgejoClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
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
impl<C: HasForgejoClient + HasEventLog + HasGitWorktreeService + HasProjectDir + 'static>
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
            author_agent: Some(ctx.agent_name.to_string()),
            author_role: Some("dev".to_string()),
        };

        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Err(EffectError::custom(
                "file_pr_error",
                "forgejo_url and forgejo_token are required; local prs.json PR flow has been removed",
            ));
        };
        let output = file_pr::file_pr_async(
            &input,
            self.ctx.git_worktree_service().clone(),
            forgejo.as_ref(),
        )
        .await
        .effect_err("file_pr")?;

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
        let _ = ctx;
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(LocalPrResponse::default());
        };
        let repo_info = repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("file_pr")?;
        let prs = forgejo
            .list_open_pull_requests(&repo_info.owner, &repo_info.repo)
            .await
            .effect_err("file_pr")?;
        Ok(prs
            .into_iter()
            .find(|pr| pr.number.as_u64() == req.pr_number as u64)
            .map(|pr| forgejo_pr_response(&pr))
            .unwrap_or_default())
    }

    #[instrument(skip_all, fields(agent_name = %ctx.agent_name, branch = %req.branch))]
    async fn local_pr_get_for_branch(
        &self,
        req: LocalPrGetForBranchRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<LocalPrResponse> {
        let _ = ctx;
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(LocalPrResponse::default());
        };
        let repo_info = repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("file_pr")?;
        let branch = BranchName::try_from_str(req.branch.as_str())
            .expect("validated string input is non-empty");
        Ok(forgejo
            .find_open_pull_request(&repo_info.owner, &repo_info.repo, &branch)
            .await
            .effect_err("file_pr")?
            .map(|pr| forgejo_pr_response(&pr))
            .unwrap_or_default())
    }
}

fn forgejo_pr_response(pr: &crate::services::forgejo::ForgejoPullRequest) -> LocalPrResponse {
    let author_agent = pr_body_metadata_value(&pr.body, "Authoring-Agent")
        .or_else(|| author_agent_from_branch(pr.head_ref.as_str()))
        .unwrap_or_default();
    LocalPrResponse {
        found: true,
        pr_number: pr.number.as_u64() as i64,
        head_branch: pr.head_ref.to_string(),
        base_branch: pr.base_ref.to_string(),
        author_agent,
        review_state: "pending_review".to_string(),
        last_head_sha: pr.head_sha.clone().unwrap_or_default(),
        reviewer_agent: pr_body_metadata_value(&pr.body, "Reviewer-Agent").unwrap_or_default(),
    }
}

fn pr_body_metadata_value(body: &str, key: &str) -> Option<String> {
    let prefix = format!("{key}:");
    body.lines()
        .find_map(|line| line.trim().strip_prefix(&prefix).map(str::trim))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn author_agent_from_branch(branch: &str) -> Option<String> {
    branch
        .rsplit_once('.')
        .map(|(_, slug)| slug.to_string())
        .filter(|slug| !slug.is_empty())
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
            pr_url: "https://forgejo.local/owner/repo/pulls/42".to_string(),
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

    #[test]
    fn forgejo_pr_response_maps_body_metadata() {
        let pr = crate::services::forgejo::ForgejoPullRequest {
            number: crate::domain::PRNumber::new(7),
            url: "http://forgejo.local/owner/repo/pulls/7".to_string(),
            title: "Fix local PR".to_string(),
            body: "Body

---
Authoring-Agent: fix-local-pr-codex
Reviewer-Agent: review-pr-7-codex"
                .to_string(),
            head_ref: BranchName::try_from_str("main.fix-local-pr-codex")
                .expect("literal branch is valid"),
            base_ref: BranchName::try_from_str("main").expect("literal branch is valid"),
            state: "open".to_string(),
            merged: false,
            head_sha: Some("abc123".to_string()),
        };

        let response = forgejo_pr_response(&pr);

        assert!(response.found);
        assert_eq!(response.pr_number, 7);
        assert_eq!(response.head_branch, "main.fix-local-pr-codex");
        assert_eq!(response.author_agent, "fix-local-pr-codex");
        assert_eq!(response.review_state, "pending_review");
        assert_eq!(response.last_head_sha, "abc123");
        assert_eq!(response.reviewer_agent, "review-pr-7-codex");
    }

    #[test]
    fn forgejo_pr_response_falls_back_to_branch_author() {
        let pr = crate::services::forgejo::ForgejoPullRequest {
            number: crate::domain::PRNumber::new(7),
            url: String::new(),
            title: String::new(),
            body: String::new(),
            head_ref: BranchName::try_from_str("main.fix-local-pr-codex")
                .expect("literal branch is valid"),
            base_ref: BranchName::try_from_str("main").expect("literal branch is valid"),
            state: "open".to_string(),
            merged: false,
            head_sha: None,
        };

        let response = forgejo_pr_response(&pr);

        assert!(response.found);
        assert_eq!(response.author_agent, "fix-local-pr-codex");
    }

    #[tokio::test]
    async fn local_pr_get_missing_returns_not_found_response() {
        let tmp = tempfile::tempdir().unwrap();
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

    #[tokio::test]
    async fn local_pr_get_cache_miss_returns_not_found_response() {
        let tmp = tempfile::tempdir().unwrap();
        let mut services = Services::test();
        services.project_dir = tmp.path().to_path_buf();
        let handler = FilePRHandler::new(Arc::new(services));

        let response = handler
            .local_pr_get(LocalPrGetRequest { pr_number: 12 }, &test_ctx("main"))
            .await
            .unwrap();

        assert!(!response.found);
        assert_eq!(response.pr_number, 0);
    }

    #[tokio::test]
    async fn local_pr_get_for_branch_cache_miss_returns_not_found_response() {
        let tmp = tempfile::tempdir().unwrap();
        let mut services = Services::test();
        services.project_dir = tmp.path().to_path_buf();
        let handler = FilePRHandler::new(Arc::new(services));

        let response = handler
            .local_pr_get_for_branch(
                LocalPrGetForBranchRequest {
                    branch: "main.fix-appview-codex".to_string(),
                },
                &test_ctx("main.fix-appview-codex"),
            )
            .await
            .unwrap();

        assert!(!response.found);
        assert_eq!(response.pr_number, 0);
    }
}
