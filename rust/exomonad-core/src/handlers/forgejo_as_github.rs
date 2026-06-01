//! Forgejo-backed handler for `github.*` effects.
//!
//! Forgejo-only deployments still run WASM code that asks for `github.*`
//! effects. This handler keeps that ABI stable while sourcing hosted PR data
//! from Forgejo.

use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::github::{
    CreatePullRequestRequest, CreatePullRequestResponse, GetIssueRequest, GetIssueResponse,
    GetPullRequestRequest, GetPullRequestResponse, GetPullRequestReviewCommentsRequest,
    GetPullRequestReviewCommentsResponse, IssueState, ListIssuesRequest, ListIssuesResponse,
    ListPullRequestsRequest, ListPullRequestsResponse, PullRequest, Review, ReviewState, User,
};
use prost::Message;

use crate::domain::{BranchName, GithubOwner, GithubRepo, PRNumber};
use crate::effects::{EffectContext, EffectError, EffectHandler, EffectResult};
use crate::services::forgejo::{ForgejoPullRequest, ForgejoPullRequestReview};
use crate::services::{repo, HasForgejoClient, HasProjectDir};

pub struct ForgejoAsGitHubHandler<C> {
    ctx: Arc<C>,
}

impl<C> ForgejoAsGitHubHandler<C>
where
    C: HasForgejoClient + HasProjectDir + Send + Sync + 'static,
{
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }

    async fn handle_get_pull_request(&self, payload: &[u8]) -> EffectResult<Vec<u8>> {
        let req = GetPullRequestRequest::decode(payload)
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        let forgejo = self.forgejo_client()?;
        let (owner, repo) = self.resolve_owner_repo(&req.owner, &req.repo).await?;
        let number = pr_number(req.number)?;

        let pull_request = forgejo
            .get_pull_request(&owner, &repo, number)
            .await
            .map_err(|e| EffectError::network_error(e.to_string()))?;
        let reviews = if req.include_reviews {
            forgejo
                .list_pull_request_reviews(&owner, &repo, number)
                .await
                .map_err(|e| EffectError::network_error(e.to_string()))?
                .into_iter()
                .map(forgejo_review_to_proto)
                .collect()
        } else {
            Vec::new()
        };
        Ok(GetPullRequestResponse {
            pull_request: Some(forgejo_pr_to_proto(pull_request)),
            reviews,
        }
        .encode_to_vec())
    }

    async fn handle_list_pull_requests(&self, payload: &[u8]) -> EffectResult<Vec<u8>> {
        let req = ListPullRequestsRequest::decode(payload)
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        let forgejo = self.forgejo_client()?;
        let (owner, repo) = self.resolve_owner_repo(&req.owner, &req.repo).await?;

        let pull_requests = forgejo
            .list_open_pull_requests(&owner, &repo)
            .await
            .map_err(|e| EffectError::network_error(e.to_string()))?
            .into_iter()
            .map(forgejo_pr_to_proto)
            .collect();
        Ok(ListPullRequestsResponse { pull_requests }.encode_to_vec())
    }

    async fn handle_create_pull_request(&self, payload: &[u8]) -> EffectResult<Vec<u8>> {
        let req = CreatePullRequestRequest::decode(payload)
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        let forgejo = self.forgejo_client()?;
        let (owner, repo) = self.resolve_owner_repo(&req.owner, &req.repo).await?;
        let head = branch_name(&req.head)?;
        let base = if req.base.is_empty() {
            branch_name("main")?
        } else {
            branch_name(&req.base)?
        };

        let pr = forgejo
            .create_pull_request(&owner, &repo, &req.title, &req.body, &base, &head)
            .await
            .map_err(|e| EffectError::network_error(e.to_string()))?;
        let url = pr.url.clone();
        Ok(CreatePullRequestResponse {
            pull_request: Some(forgejo_pr_to_proto(pr)),
            url,
        }
        .encode_to_vec())
    }

    async fn handle_get_pull_request_review_comments(
        &self,
        payload: &[u8],
    ) -> EffectResult<Vec<u8>> {
        GetPullRequestReviewCommentsRequest::decode(payload)
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        Ok(GetPullRequestReviewCommentsResponse {
            comments: Vec::new(),
        }
        .encode_to_vec())
    }

    async fn handle_list_issues(&self, payload: &[u8]) -> EffectResult<Vec<u8>> {
        ListIssuesRequest::decode(payload)
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        Ok(ListIssuesResponse { issues: Vec::new() }.encode_to_vec())
    }

    async fn handle_get_issue(&self, payload: &[u8]) -> EffectResult<Vec<u8>> {
        GetIssueRequest::decode(payload).map_err(|e| EffectError::invalid_input(e.to_string()))?;
        Ok(GetIssueResponse {
            issue: None,
            comments: Vec::new(),
        }
        .encode_to_vec())
    }

    async fn resolve_owner_repo(
        &self,
        owner: &str,
        repo: &str,
    ) -> EffectResult<(GithubOwner, GithubRepo)> {
        if !owner.is_empty() && !repo.is_empty() {
            return Ok((
                GithubOwner::try_from_str(owner)
                    .map_err(|e| EffectError::invalid_input(e.to_string()))?,
                GithubRepo::try_from_str(repo)
                    .map_err(|e| EffectError::invalid_input(e.to_string()))?,
            ));
        }

        let repo_info = repo::get_repo_info(self.ctx.project_dir())
            .await
            .map_err(|e| EffectError::invalid_input(e.to_string()))?;
        Ok((repo_info.owner, repo_info.repo))
    }

    fn forgejo_client(&self) -> EffectResult<Arc<crate::services::ForgejoClient>> {
        self.ctx
            .forgejo_client()
            .cloned()
            .ok_or_else(|| EffectError::not_found("forgejo client unavailable"))
    }
}

#[async_trait]
impl<C> EffectHandler for ForgejoAsGitHubHandler<C>
where
    C: HasForgejoClient + HasProjectDir + Send + Sync + 'static,
{
    fn namespace(&self) -> &str {
        "github"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        _ctx: &EffectContext,
    ) -> EffectResult<Vec<u8>> {
        match effect_type {
            "github.get_pull_request" => self.handle_get_pull_request(payload).await,
            "github.list_pull_requests" => self.handle_list_pull_requests(payload).await,
            "github.create_pull_request" => self.handle_create_pull_request(payload).await,
            "github.get_pull_request_review_comments" => {
                self.handle_get_pull_request_review_comments(payload).await
            }
            "github.list_issues" => self.handle_list_issues(payload).await,
            "github.get_issue" => self.handle_get_issue(payload).await,
            _ => Err(EffectError::not_found(format!(
                "github/{effect_type}: not supported via Forgejo"
            ))),
        }
    }
}

fn pr_number(value: i32) -> EffectResult<PRNumber> {
    PRNumber::try_from(value as u64).map_err(|e| EffectError::invalid_input(e.to_string()))
}

fn branch_name(value: &str) -> EffectResult<BranchName> {
    BranchName::try_from_str(value).map_err(|e| EffectError::invalid_input(e.to_string()))
}

fn forgejo_review_state_to_proto(state: &str) -> i32 {
    match state.trim().to_ascii_uppercase().as_str() {
        "APPROVED" => ReviewState::Approved as i32,
        "CHANGES_REQUESTED" => ReviewState::ChangesRequested as i32,
        "COMMENTED" => ReviewState::Commented as i32,
        "PENDING" => ReviewState::Pending as i32,
        _ => ReviewState::Unspecified as i32,
    }
}

fn forgejo_review_to_proto(review: ForgejoPullRequestReview) -> Review {
    Review {
        id: 0,
        author: Some(User {
            login: "forgejo-reviewer".to_string(),
            id: 0,
            avatar_url: String::new(),
        }),
        state: forgejo_review_state_to_proto(&review.state),
        body: review.body,
        submitted_at: 0,
        commit_id: review.commit_id.unwrap_or_default(),
    }
}

pub fn forgejo_pr_to_proto(pr: ForgejoPullRequest) -> PullRequest {
    let state = if pr.state == "closed" || pr.merged {
        IssueState::Closed
    } else {
        IssueState::Open
    };
    PullRequest {
        number: pr.number.as_u64() as i32,
        title: pr.title,
        body: pr.body,
        state: state as i32,
        author: None,
        head_ref: pr.head_ref.to_string(),
        base_ref: pr.base_ref.to_string(),
        merged: pr.merged,
        draft: false,
        labels: Vec::new(),
        created_at: 0,
        updated_at: 0,
        head_sha: pr.head_sha.unwrap_or_default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn branch(value: &str) -> BranchName {
        BranchName::try_from_str(value).expect("literal branch is non-empty")
    }

    fn pr(state: &str, merged: bool, head_sha: Option<&str>) -> ForgejoPullRequest {
        ForgejoPullRequest {
            number: PRNumber::new(9),
            url: "http://forgejo.local/owner/repo/pulls/9".to_string(),
            title: "Title".to_string(),
            body: "Body".to_string(),
            head_ref: branch("feature"),
            base_ref: branch("main"),
            state: state.to_string(),
            merged,
            head_sha: head_sha.map(ToString::to_string),
        }
    }

    #[test]
    fn forgejo_open_pr_maps_to_open_state() {
        let proto = forgejo_pr_to_proto(pr("open", false, Some("abc123")));

        assert_eq!(proto.state, IssueState::Open as i32);
        assert_eq!(proto.head_sha, "abc123");
    }

    #[test]
    fn forgejo_merged_pr_maps_to_closed_state() {
        let proto = forgejo_pr_to_proto(pr("closed", true, None));

        assert_eq!(proto.state, IssueState::Closed as i32);
        assert_eq!(proto.head_sha, "");
    }

    #[test]
    fn forgejo_review_maps_approved_state_and_commit() {
        let proto = forgejo_review_to_proto(ForgejoPullRequestReview {
            state: "APPROVED".to_string(),
            body: "looks good".to_string(),
            commit_id: Some("abc123".to_string()),
        });

        assert_eq!(proto.state, ReviewState::Approved as i32);
        assert_eq!(proto.body, "looks good");
        assert_eq!(proto.commit_id, "abc123");
    }

    #[test]
    fn forgejo_review_maps_unknown_state_to_unspecified() {
        let proto = forgejo_review_to_proto(ForgejoPullRequestReview {
            state: "STALE".to_string(),
            body: String::new(),
            commit_id: None,
        });

        assert_eq!(proto.state, ReviewState::Unspecified as i32);
        assert_eq!(proto.commit_id, "");
    }
}
