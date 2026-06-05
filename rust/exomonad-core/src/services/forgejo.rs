use crate::domain::{BranchName, CIStatus, GithubOwner, GithubRepo, PRNumber};
use anyhow::{anyhow, Context, Result};
use reqwest::{header, StatusCode, Url};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Arc;
use tokio::process::Command;

#[derive(Clone)]
pub struct ForgejoClient {
    backend: ForgejoBackend,
}

#[derive(Clone)]
enum ForgejoBackend {
    Http(HttpForgejoClient),
    Fj(FjForgejoClient),
}

#[derive(Clone)]
struct HttpForgejoClient {
    base_url: Url,
    token: String,
    http: reqwest::Client,
}

#[derive(Clone)]
pub struct FjForgejoClient {
    project_dir: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoPullRequest {
    pub number: PRNumber,
    pub url: String,
    pub title: String,
    pub body: String,
    pub head_ref: BranchName,
    pub base_ref: BranchName,
    pub state: String,
    pub merged: bool,
    pub head_sha: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoPullRequestReview {
    pub state: String,
    pub body: String,
    pub commit_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoCommitStatus {
    pub status: CIStatus,
    pub context: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoWorkflowRun {
    pub name: String,
    pub display_title: String,
    pub head_branch: Option<String>,
    pub head_sha: Option<String>,
    pub status: String,
    pub conclusion: Option<String>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoRunner {
    pub name: String,
    pub status: String,
    pub busy: bool,
    pub disabled: bool,
    pub last_online: Option<String>,
}

#[derive(Debug, Serialize)]
struct CreatePullRequestBody<'a> {
    title: &'a str,
    body: &'a str,
    head: &'a str,
    base: &'a str,
}

#[derive(Debug, Serialize)]
struct UpdatePullRequestBody<'a> {
    title: &'a str,
    body: &'a str,
    base: &'a str,
}

#[derive(Debug, Serialize)]
struct MergePullRequestBody<'a> {
    #[serde(rename = "Do")]
    method: &'a str,
}

#[derive(Debug, Serialize)]
struct SubmitPullRequestReviewBody<'a> {
    event: &'a str,
    body: &'a str,
}

#[derive(Debug, Deserialize)]
struct PullRequestResponse {
    number: u64,
    #[serde(default)]
    title: String,
    #[serde(default)]
    body: String,
    #[serde(default)]
    state: String,
    #[serde(default)]
    merged: bool,
    html_url: Option<String>,
    url: Option<String>,
    head: PullRequestBranch,
    base: PullRequestBranch,
}

#[derive(Debug, Deserialize)]
struct PullRequestBranch {
    #[serde(rename = "ref")]
    ref_name: String,
    #[serde(default)]
    sha: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PullRequestReviewResponse {
    #[serde(default)]
    state: String,
    #[serde(default)]
    body: String,
    #[serde(default)]
    commit_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CommitStatusResponse {
    #[serde(default, alias = "state")]
    status: String,
    #[serde(default)]
    context: Option<String>,
}

#[derive(Debug, Deserialize)]
struct WorkflowRunsResponse {
    #[serde(default)]
    workflow_runs: Vec<WorkflowRunResponse>,
}

#[derive(Debug, Deserialize)]
struct WorkflowRunResponse {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    display_title: Option<String>,
    #[serde(default, rename = "prettyref", alias = "head_branch")]
    head_branch_ref: Option<String>,
    #[serde(default, alias = "commit_sha")]
    head_sha: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    conclusion: Option<String>,
    #[serde(default)]
    created_at: Option<String>,
    #[serde(default)]
    updated_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct RunnersResponse {
    #[serde(default)]
    runners: Vec<RunnerResponse>,
}

#[derive(Debug, Deserialize)]
struct RunnerResponse {
    #[serde(default)]
    name: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    busy: bool,
    #[serde(default)]
    disabled: bool,
    #[serde(default, alias = "last_online")]
    last_online: Option<String>,
}

impl ForgejoClient {
    pub fn new(forgejo_url: &str, forgejo_token: &str) -> Result<Arc<Self>> {
        Ok(Arc::new(Self {
            backend: ForgejoBackend::Http(HttpForgejoClient::new(forgejo_url, forgejo_token)?),
        }))
    }

    pub fn new_fj(project_dir: impl Into<PathBuf>) -> Arc<Self> {
        Arc::new(Self {
            backend: ForgejoBackend::Fj(FjForgejoClient::new(project_dir)),
        })
    }

    pub fn fj_binary_in_path() -> bool {
        binary_in_path("fj")
    }

    pub fn git_auth_token(&self) -> Option<&str> {
        match &self.backend {
            ForgejoBackend::Http(client) => Some(client.token.as_str()),
            ForgejoBackend::Fj(_) => None,
        }
    }

    pub async fn find_open_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Option<ForgejoPullRequest>> {
        match &self.backend {
            ForgejoBackend::Http(client) => client.find_open_pull_request(owner, repo, head).await,
            ForgejoBackend::Fj(client) => client.find_open_pull_request(owner, repo, head).await,
        }
    }

    pub async fn list_open_pull_requests(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
    ) -> Result<Vec<ForgejoPullRequest>> {
        match &self.backend {
            ForgejoBackend::Http(client) => client.list_open_pull_requests(owner, repo).await,
            ForgejoBackend::Fj(client) => client.list_open_pull_requests(owner, repo).await,
        }
    }

    pub async fn list_pull_request_reviews(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<Vec<ForgejoPullRequestReview>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client.list_pull_request_reviews(owner, repo, number).await
            }
            ForgejoBackend::Fj(client) => {
                client.list_pull_request_reviews(owner, repo, number).await
            }
        }
    }

    pub async fn submit_pull_request_review(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        event: &str,
        body: &str,
    ) -> Result<()> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .submit_pull_request_review(owner, repo, number, event, body)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .submit_pull_request_review(owner, repo, number, event, body)
                    .await
            }
        }
    }

    pub async fn list_commit_statuses(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<Vec<ForgejoCommitStatus>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client.list_commit_statuses(owner, repo, head_sha).await
            }
            ForgejoBackend::Fj(client) => client.list_commit_statuses(owner, repo, head_sha).await,
        }
    }

    pub async fn commit_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<CIStatus> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client.commit_status_for_head(owner, repo, head_sha).await
            }
            ForgejoBackend::Fj(client) => {
                client.commit_status_for_head(owner, repo, head_sha).await
            }
        }
    }

    pub async fn actions_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        head_sha: &str,
    ) -> Result<CIStatus> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .actions_status_for_head(owner, repo, branch, head_sha)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .actions_status_for_head(owner, repo, branch, head_sha)
                    .await
            }
        }
    }

    pub async fn latest_actions_status_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
    ) -> Result<Option<CIStatus>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .latest_actions_status_for_branch(owner, repo, branch)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .latest_actions_status_for_branch(owner, repo, branch)
                    .await
            }
        }
    }

    pub async fn list_workflow_runs_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        limit: usize,
    ) -> Result<Vec<ForgejoWorkflowRun>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .list_workflow_runs_for_branch(owner, repo, branch, limit)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .list_workflow_runs_for_branch(owner, repo, branch, limit)
                    .await
            }
        }
    }

    pub async fn list_global_runners(&self) -> Result<Vec<ForgejoRunner>> {
        match &self.backend {
            ForgejoBackend::Http(client) => client.list_global_runners().await,
            ForgejoBackend::Fj(client) => client.list_global_runners().await,
        }
    }

    pub async fn create_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        title: &str,
        body: &str,
        base: &BranchName,
        head: &BranchName,
    ) -> Result<ForgejoPullRequest> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .create_pull_request(owner, repo, title, body, base, head)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .create_pull_request(owner, repo, title, body, base, head)
                    .await
            }
        }
    }

    pub async fn get_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<ForgejoPullRequest> {
        match &self.backend {
            ForgejoBackend::Http(client) => client.get_pull_request(owner, repo, number).await,
            ForgejoBackend::Fj(client) => client.get_pull_request(owner, repo, number).await,
        }
    }

    pub async fn merge_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        method: &str,
    ) -> Result<()> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client.merge_pull_request(owner, repo, number, method).await
            }
            ForgejoBackend::Fj(client) => {
                client.merge_pull_request(owner, repo, number, method).await
            }
        }
    }

    pub async fn update_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        title: &str,
        body: &str,
        base: &BranchName,
    ) -> Result<()> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .update_pull_request(owner, repo, number, title, body, base)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .update_pull_request(owner, repo, number, title, body, base)
                    .await
            }
        }
    }
}

fn binary_in_path(binary: &str) -> bool {
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
        .any(|dir| dir.join(binary).is_file())
}

impl HttpForgejoClient {
    fn new(forgejo_url: &str, forgejo_token: &str) -> Result<Self> {
        let forgejo_url = forgejo_url.trim();
        let forgejo_token = forgejo_token.trim();
        if forgejo_url.is_empty() {
            return Err(anyhow!("forgejo_url is required for Forgejo PR operations"));
        }
        if forgejo_token.is_empty() {
            return Err(anyhow!(
                "forgejo_token is required for Forgejo PR operations"
            ));
        }

        let normalized_url = if forgejo_url.ends_with('/') {
            forgejo_url.to_string()
        } else {
            format!("{forgejo_url}/")
        };
        let base_url = Url::parse(&normalized_url).context("invalid forgejo_url")?;
        let http = reqwest::Client::builder()
            .user_agent("exomonad")
            .build()
            .context("failed to build Forgejo HTTP client")?;

        Ok(Self {
            base_url,
            token: forgejo_token.to_string(),
            http,
        })
    }

    pub async fn find_open_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Option<ForgejoPullRequest>> {
        let url = self.repo_pulls_url(owner, repo)?;
        let response = self
            .http
            .get(url)
            .query(&[("state", "open"), ("limit", "100")])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo PR list request failed")?;

        let prs: Vec<PullRequestResponse> = self
            .decode_response(response, "list Forgejo pull requests")
            .await?;
        prs.into_iter()
            .find(|pr| pr.head.ref_name == head.as_str())
            .map(ForgejoPullRequest::try_from)
            .transpose()
    }

    pub async fn list_open_pull_requests(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
    ) -> Result<Vec<ForgejoPullRequest>> {
        let url = self.repo_pulls_url(owner, repo)?;
        let response = self
            .http
            .get(url)
            .query(&[("state", "open"), ("limit", "100")])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo PR list request failed")?;

        let prs: Vec<PullRequestResponse> = self
            .decode_response(response, "list Forgejo pull requests")
            .await?;
        prs.into_iter().map(ForgejoPullRequest::try_from).collect()
    }

    pub async fn list_pull_request_reviews(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<Vec<ForgejoPullRequestReview>> {
        let number = number.as_u64().to_string();
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "pulls",
            &number,
            "reviews",
        ])?;
        let response = self
            .http
            .get(url)
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo PR reviews request failed")?;

        let reviews: Vec<PullRequestReviewResponse> = self
            .decode_response(response, "list Forgejo pull request reviews")
            .await?;
        Ok(reviews
            .into_iter()
            .map(|review| ForgejoPullRequestReview {
                state: review.state,
                body: review.body,
                commit_id: review.commit_id,
            })
            .collect())
    }

    pub async fn submit_pull_request_review(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        event: &str,
        body: &str,
    ) -> Result<()> {
        let number = number.as_u64().to_string();
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "pulls",
            &number,
            "reviews",
        ])?;
        let response = self
            .http
            .post(url)
            .headers(self.auth_headers()?)
            .json(&SubmitPullRequestReviewBody { event, body })
            .send()
            .await
            .context("Forgejo PR review submit request failed")?;

        self.expect_success(response, "submit Forgejo pull request review")
            .await
    }

    pub async fn list_commit_statuses(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<Vec<ForgejoCommitStatus>> {
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "commits",
            head_sha,
            "statuses",
        ])?;
        let response = self
            .http
            .get(url)
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo commit statuses request failed")?;

        let statuses: Vec<CommitStatusResponse> = self
            .decode_response(response, "list Forgejo commit statuses")
            .await?;
        Ok(statuses
            .into_iter()
            .map(|status| ForgejoCommitStatus {
                status: CIStatus::parse(&status.status),
                context: status.context,
            })
            .collect())
    }

    pub async fn commit_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<CIStatus> {
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "commits",
            head_sha,
            "status",
        ])?;
        let response = self
            .http
            .get(url)
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo combined commit status request failed")?;

        let status: CommitStatusResponse = self
            .decode_response(response, "get Forgejo combined commit status")
            .await?;
        Ok(CIStatus::parse(&status.status))
    }

    pub async fn actions_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        head_sha: &str,
    ) -> Result<CIStatus> {
        let url = self.api_url(&["repos", owner.as_str(), repo.as_str(), "actions", "runs"])?;
        let response = self
            .http
            .get(url)
            .query(&[("branch", branch.as_str()), ("limit", "20")])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo Actions runs request failed")?;

        let runs: WorkflowRunsResponse = self
            .decode_response(response, "list Forgejo Actions runs")
            .await?;
        let total_runs = runs.workflow_runs.len();
        let Some(run) = runs.workflow_runs.into_iter().find(|run| {
            let sha_matches = run.head_sha.as_deref() == Some(head_sha);
            let branch_matches = run
                .head_branch_ref
                .as_deref()
                .map(|ref_name| ref_name.trim_start_matches("refs/heads/"))
                == Some(branch.as_str());
            sha_matches && branch_matches
        }) else {
            tracing::debug!(
                head_sha,
                branch = %branch,
                total_runs,
                "[Forgejo] No Actions run matched branch+SHA; CI status unknown"
            );
            return Ok(CIStatus::Unknown);
        };
        Ok(workflow_status(run))
    }

    pub async fn latest_actions_status_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
    ) -> Result<Option<CIStatus>> {
        let url = self.api_url(&["repos", owner.as_str(), repo.as_str(), "actions", "runs"])?;
        let response = self
            .http
            .get(url)
            .query(&[("branch", branch.as_str()), ("limit", "1")])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo Actions runs request failed")?;

        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }

        let runs: WorkflowRunsResponse = self
            .decode_response(response, "list Forgejo Actions runs")
            .await?;
        Ok(runs.workflow_runs.into_iter().next().map(workflow_status))
    }

    pub async fn list_workflow_runs_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        limit: usize,
    ) -> Result<Vec<ForgejoWorkflowRun>> {
        let url = self.api_url(&["repos", owner.as_str(), repo.as_str(), "actions", "runs"])?;
        let limit = limit.max(1).to_string();
        let response = self
            .http
            .get(url)
            .query(&[("branch", branch.as_str()), ("limit", limit.as_str())])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo Actions runs request failed")?;

        if response.status() == StatusCode::NOT_FOUND {
            return Ok(Vec::new());
        }

        let runs: WorkflowRunsResponse = self
            .decode_response(response, "list Forgejo Actions runs")
            .await?;
        Ok(runs
            .workflow_runs
            .into_iter()
            .map(ForgejoWorkflowRun::from)
            .collect())
    }

    pub async fn list_global_runners(&self) -> Result<Vec<ForgejoRunner>> {
        let url = self.api_url(&["admin", "actions", "runners"])?;
        let response = self
            .http
            .get(url)
            .query(&[("limit", "100")])
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo runner list request failed")?;

        if matches!(
            response.status(),
            StatusCode::FORBIDDEN | StatusCode::NOT_FOUND
        ) {
            return Ok(Vec::new());
        }

        let runners: RunnersResponse = self
            .decode_response(response, "list Forgejo runners")
            .await?;
        Ok(runners
            .runners
            .into_iter()
            .map(ForgejoRunner::from)
            .collect())
    }

    pub async fn create_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        title: &str,
        body: &str,
        base: &BranchName,
        head: &BranchName,
    ) -> Result<ForgejoPullRequest> {
        let url = self.repo_pulls_url(owner, repo)?;
        let request_body = CreatePullRequestBody {
            title,
            body,
            head: head.as_str(),
            base: base.as_str(),
        };

        let response = self
            .http
            .post(url)
            .headers(self.auth_headers()?)
            .json(&request_body)
            .send()
            .await
            .context("Forgejo PR create request failed")?;

        let pr: PullRequestResponse = self
            .decode_response(response, "create Forgejo pull request")
            .await?;
        ForgejoPullRequest::try_from(pr)
    }

    pub async fn get_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<ForgejoPullRequest> {
        let url = self.repo_pull_url(owner, repo, number)?;
        let response = self
            .http
            .get(url)
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo PR get request failed")?;
        let pr: PullRequestResponse = self
            .decode_response(response, "get Forgejo pull request")
            .await?;
        ForgejoPullRequest::try_from(pr)
    }

    pub async fn merge_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        method: &str,
    ) -> Result<()> {
        let number_segment = number.as_u64().to_string();
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "pulls",
            &number_segment,
            "merge",
        ])?;
        let response = self
            .http
            .post(url)
            .headers(self.auth_headers()?)
            .json(&MergePullRequestBody { method })
            .send()
            .await
            .context("Forgejo PR merge request failed")?;
        self.expect_success(response, "merge Forgejo pull request")
            .await
    }

    pub async fn update_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        title: &str,
        body: &str,
        base: &BranchName,
    ) -> Result<()> {
        let url = self.repo_pull_url(owner, repo, number)?;
        let request_body = UpdatePullRequestBody {
            title,
            body,
            base: base.as_str(),
        };

        let response = self
            .http
            .patch(url)
            .headers(self.auth_headers()?)
            .json(&request_body)
            .send()
            .await
            .context("Forgejo PR update request failed")?;

        self.expect_success(response, "update Forgejo pull request")
            .await
    }

    fn repo_pulls_url(&self, owner: &GithubOwner, repo: &GithubRepo) -> Result<Url> {
        self.api_url(&["repos", owner.as_str(), repo.as_str(), "pulls"])
    }

    fn repo_pull_url(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<Url> {
        let number = number.as_u64().to_string();
        self.api_url(&["repos", owner.as_str(), repo.as_str(), "pulls", &number])
    }

    fn api_url(&self, segments: &[&str]) -> Result<Url> {
        let mut url = self
            .base_url
            .join("api/v1/")
            .context("invalid forgejo_url API base")?;
        {
            let mut path = url
                .path_segments_mut()
                .map_err(|_| anyhow!("forgejo_url cannot be used as a base URL"))?;
            path.pop_if_empty();
            for segment in segments {
                path.push(segment);
            }
        }
        Ok(url)
    }

    fn auth_headers(&self) -> Result<header::HeaderMap> {
        let mut headers = header::HeaderMap::new();
        let value = format!("token {}", self.token);
        headers.insert(
            header::AUTHORIZATION,
            header::HeaderValue::from_str(&value)
                .context("forgejo_token contains invalid header characters")?,
        );
        Ok(headers)
    }

    async fn decode_response<T: serde::de::DeserializeOwned>(
        &self,
        response: reqwest::Response,
        action: &str,
    ) -> Result<T> {
        self.expect_success_status(response.status(), response.text().await?, action)?
            .parse_json(action)
    }

    async fn expect_success(&self, response: reqwest::Response, action: &str) -> Result<()> {
        let status = response.status();
        let body = response.text().await?;
        self.expect_success_status(status, body, action).map(|_| ())
    }

    fn expect_success_status(
        &self,
        status: StatusCode,
        body: String,
        action: &str,
    ) -> Result<ResponseBody> {
        if status.is_success() {
            return Ok(ResponseBody(body));
        }
        Err(anyhow!(
            "{action} failed with HTTP {status}: {}",
            body.trim()
        ))
    }
}

impl FjForgejoClient {
    fn new(project_dir: impl Into<PathBuf>) -> Self {
        Self {
            project_dir: project_dir.into(),
        }
    }

    async fn find_open_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Option<ForgejoPullRequest>> {
        Ok(self
            .list_open_pull_requests(owner, repo)
            .await?
            .into_iter()
            .find(|pr| pr.head_ref == *head))
    }

    async fn list_open_pull_requests(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
    ) -> Result<Vec<ForgejoPullRequest>> {
        let prs: Vec<PullRequestResponse> = self
            .fj_json(["pr", "list", "--state", "open", "--json"])
            .await?;
        prs.into_iter().map(ForgejoPullRequest::try_from).collect()
    }

    async fn list_pull_request_reviews(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<Vec<ForgejoPullRequestReview>> {
        let path = format!(
            "/repos/{}/{}/pulls/{}/reviews",
            owner.as_str(),
            repo.as_str(),
            number.as_u64()
        );
        let reviews: Vec<PullRequestReviewResponse> = self.fj_api_json("GET", &path).await?;
        Ok(reviews
            .into_iter()
            .map(|review| ForgejoPullRequestReview {
                state: review.state,
                body: review.body,
                commit_id: review.commit_id,
            })
            .collect())
    }

    async fn submit_pull_request_review(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        event: &str,
        body: &str,
    ) -> Result<()> {
        let path = format!(
            "/repos/{}/{}/pulls/{}/reviews",
            owner.as_str(),
            repo.as_str(),
            number.as_u64()
        );
        self.fj_status([
            "api",
            "POST",
            path.as_str(),
            "-f",
            &format!("event={event}"),
            "-f",
            &format!("body={body}"),
        ])
        .await
    }

    async fn list_commit_statuses(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<Vec<ForgejoCommitStatus>> {
        let path = format!(
            "/repos/{}/{}/commits/{}/statuses",
            owner.as_str(),
            repo.as_str(),
            head_sha
        );
        let statuses: Vec<CommitStatusResponse> = self.fj_api_json("GET", &path).await?;
        Ok(statuses
            .into_iter()
            .map(|status| ForgejoCommitStatus {
                status: CIStatus::parse(&status.status),
                context: status.context,
            })
            .collect())
    }

    async fn commit_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head_sha: &str,
    ) -> Result<CIStatus> {
        Ok(combine_commit_statuses(
            self.list_commit_statuses(owner, repo, head_sha).await?,
        ))
    }

    async fn actions_status_for_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        head_sha: &str,
    ) -> Result<CIStatus> {
        let runs = self
            .list_workflow_runs_for_branch(owner, repo, branch, 20)
            .await?;
        Ok(runs
            .into_iter()
            .find(|run| run.head_sha.as_deref() == Some(head_sha))
            .map(|run| CIStatus::parse(run.conclusion.as_deref().unwrap_or(&run.status)))
            .unwrap_or(CIStatus::Unknown))
    }

    async fn latest_actions_status_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
    ) -> Result<Option<CIStatus>> {
        Ok(self
            .list_workflow_runs_for_branch(owner, repo, branch, 1)
            .await?
            .into_iter()
            .next()
            .map(|run| CIStatus::parse(run.conclusion.as_deref().unwrap_or(&run.status))))
    }

    async fn list_workflow_runs_for_branch(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        branch: &BranchName,
        limit: usize,
    ) -> Result<Vec<ForgejoWorkflowRun>> {
        let path = format!(
            "/repos/{}/{}/actions/runs?branch={}&limit={}",
            owner.as_str(),
            repo.as_str(),
            branch.as_str(),
            limit.max(1)
        );
        let runs: WorkflowRunsResponse = self.fj_api_json("GET", &path).await?;
        Ok(runs
            .workflow_runs
            .into_iter()
            .map(ForgejoWorkflowRun::from)
            .collect())
    }

    async fn list_global_runners(&self) -> Result<Vec<ForgejoRunner>> {
        let runners: RunnersResponse = self.fj_api_json("GET", "/admin/actions/runners").await?;
        Ok(runners
            .runners
            .into_iter()
            .map(ForgejoRunner::from)
            .collect())
    }

    async fn create_pull_request(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
        title: &str,
        body: &str,
        base: &BranchName,
        head: &BranchName,
    ) -> Result<ForgejoPullRequest> {
        let pr: PullRequestResponse = self
            .fj_json([
                "pr",
                "create",
                "--title",
                title,
                "--body",
                body,
                "--base",
                base.as_str(),
                "--head",
                head.as_str(),
                "--json",
            ])
            .await?;
        ForgejoPullRequest::try_from(pr)
    }

    async fn get_pull_request(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<ForgejoPullRequest> {
        let number = number.as_u64().to_string();
        let pr: PullRequestResponse = self
            .fj_json(["pr", "view", number.as_str(), "--json"])
            .await?;
        ForgejoPullRequest::try_from(pr)
    }

    async fn merge_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        method: &str,
    ) -> Result<()> {
        let path = format!(
            "/repos/{}/{}/pulls/{}/merge",
            owner.as_str(),
            repo.as_str(),
            number.as_u64()
        );
        self.fj_status(["api", "POST", path.as_str(), "-f", &format!("Do={method}")])
            .await
    }

    async fn update_pull_request(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
        number: PRNumber,
        title: &str,
        body: &str,
        base: &BranchName,
    ) -> Result<()> {
        let number = number.as_u64().to_string();
        self.fj_status([
            "pr",
            "edit",
            number.as_str(),
            "--title",
            title,
            "--body",
            body,
            "--base",
            base.as_str(),
        ])
        .await
    }

    async fn fj_api_json<T: serde::de::DeserializeOwned>(
        &self,
        method: &str,
        path: &str,
    ) -> Result<T> {
        self.fj_json(["api", method, path]).await
    }

    async fn fj_json<I, S, T>(&self, args: I) -> Result<T>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
        T: serde::de::DeserializeOwned,
    {
        let output = self.fj_output(args).await?;
        if !output.status.success() {
            anyhow::bail!(
                "fj command failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }
        serde_json::from_slice(&output.stdout).context("fj returned invalid JSON")
    }

    async fn fj_status<I, S>(&self, args: I) -> Result<()>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let output = self.fj_output(args).await?;
        if output.status.success() {
            return Ok(());
        }
        anyhow::bail!(
            "fj command failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )
    }

    async fn fj_output<I, S>(&self, args: I) -> Result<std::process::Output>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut command = Command::new("fj");
        command.current_dir(&self.project_dir);
        for arg in args {
            command.arg(arg.as_ref());
        }
        command
            .output()
            .await
            .with_context(|| format!("failed to execute fj in {}", self.project_dir.display()))
    }
}

fn combine_commit_statuses(statuses: Vec<ForgejoCommitStatus>) -> CIStatus {
    if statuses
        .iter()
        .any(|status| status.status == CIStatus::Failure)
    {
        return CIStatus::Failure;
    }
    if statuses
        .iter()
        .any(|status| status.status == CIStatus::Pending)
    {
        return CIStatus::Pending;
    }
    if statuses
        .iter()
        .any(|status| status.status == CIStatus::Success)
    {
        return CIStatus::Success;
    }
    CIStatus::Unknown
}

fn workflow_status(run: WorkflowRunResponse) -> CIStatus {
    run.conclusion
        .or(run.status)
        .map(|status| CIStatus::parse(&status))
        .unwrap_or(CIStatus::Unknown)
}

struct ResponseBody(String);

impl ResponseBody {
    fn parse_json<T: serde::de::DeserializeOwned>(self, action: &str) -> Result<T> {
        serde_json::from_str(&self.0).with_context(|| format!("{action} returned invalid JSON"))
    }
}

impl TryFrom<PullRequestResponse> for ForgejoPullRequest {
    type Error = anyhow::Error;

    fn try_from(value: PullRequestResponse) -> Result<Self> {
        Ok(Self {
            number: PRNumber::new(value.number),
            url: value.html_url.or(value.url).unwrap_or_default(),
            title: value.title,
            body: value.body,
            head_ref: BranchName::try_from(value.head.ref_name)?,
            base_ref: BranchName::try_from(value.base.ref_name)?,
            state: value.state,
            merged: value.merged,
            head_sha: value.head.sha,
        })
    }
}

impl From<WorkflowRunResponse> for ForgejoWorkflowRun {
    fn from(value: WorkflowRunResponse) -> Self {
        let status = value.status.unwrap_or_else(|| "unknown".to_string());
        Self {
            name: value.name.unwrap_or_else(|| "workflow".to_string()),
            display_title: value.display_title.unwrap_or_default(),
            head_branch: value.head_branch_ref,
            head_sha: value.head_sha,
            status,
            conclusion: value.conclusion,
            created_at: value.created_at,
            updated_at: value.updated_at,
        }
    }
}

impl From<RunnerResponse> for ForgejoRunner {
    fn from(value: RunnerResponse) -> Self {
        Self {
            name: value.name,
            status: value.status,
            busy: value.busy,
            disabled: value.disabled,
            last_online: value.last_online,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http_client_exposes_git_auth_token() {
        let client = ForgejoClient::new("http://forgejo.local", "secret-token")
            .expect("literal Forgejo config is valid");

        assert_eq!(client.git_auth_token(), Some("secret-token"));
    }

    #[test]
    fn fj_client_has_no_git_auth_token() {
        let client = ForgejoClient::new_fj("/tmp/project");

        assert_eq!(client.git_auth_token(), None);
    }
    use wiremock::matchers::{header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn owner() -> GithubOwner {
        GithubOwner::try_from_str("owner").expect("literal owner is non-empty")
    }

    fn repo() -> GithubRepo {
        GithubRepo::try_from_str("repo").expect("literal repo is non-empty")
    }

    fn branch(value: &str) -> BranchName {
        BranchName::try_from_str(value).expect("literal branch is non-empty")
    }

    async fn client() -> (Arc<ForgejoClient>, MockServer) {
        let server = MockServer::start().await;
        let client = ForgejoClient::new(&server.uri(), "token-123").unwrap();
        (client, server)
    }

    #[test]
    fn new_fj_selects_fj_backend() {
        let client = ForgejoClient::new_fj("/tmp/exomonad-project");
        match &client.backend {
            ForgejoBackend::Fj(fj) => {
                assert_eq!(fj.project_dir, PathBuf::from("/tmp/exomonad-project"))
            }
            ForgejoBackend::Http(_) => panic!("expected fj backend"),
        }
    }

    #[test]
    fn combine_commit_statuses_prefers_failure_then_pending_then_success() {
        let status = |status| ForgejoCommitStatus {
            status,
            context: None,
        };
        assert_eq!(
            combine_commit_statuses(vec![status(CIStatus::Success), status(CIStatus::Failure)]),
            CIStatus::Failure
        );
        assert_eq!(
            combine_commit_statuses(vec![status(CIStatus::Success), status(CIStatus::Pending)]),
            CIStatus::Pending
        );
        assert_eq!(
            combine_commit_statuses(vec![status(CIStatus::Success)]),
            CIStatus::Success
        );
        assert_eq!(combine_commit_statuses(Vec::new()), CIStatus::Unknown);
    }

    #[tokio::test]
    async fn creates_pull_request_with_forgejo_token() {
        let (client, server) = client().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(201).set_body_json(serde_json::json!({
                "number": 9,
                "html_url": "http://forgejo.local/owner/repo/pulls/9",
                "head": { "ref": "main.feature" },
                "base": { "ref": "main" }
            })))
            .mount(&server)
            .await;

        let pr = client
            .create_pull_request(
                &owner(),
                &repo(),
                "Title",
                "Body",
                &branch("main"),
                &branch("main.feature"),
            )
            .await
            .unwrap();

        assert_eq!(pr.number.as_u64(), 9);
        assert_eq!(pr.head_ref.as_str(), "main.feature");
        assert_eq!(pr.base_ref.as_str(), "main");
    }

    #[tokio::test]
    async fn updates_pull_request_with_forgejo_token() {
        let (client, server) = client().await;
        Mock::given(method("PATCH"))
            .and(path("/api/v1/repos/owner/repo/pulls/9"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "number": 9,
                "html_url": "http://forgejo.local/owner/repo/pulls/9",
                "head": { "ref": "main.feature" },
                "base": { "ref": "main" }
            })))
            .mount(&server)
            .await;

        client
            .update_pull_request(
                &owner(),
                &repo(),
                PRNumber::new(9),
                "Title",
                "Body",
                &branch("main"),
            )
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn actions_status_for_head_matches_forgejo_actions_fields() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/actions/runs"))
            .and(query_param("branch", "main.feature"))
            .and(query_param("limit", "20"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "workflow_runs": [
                    {
                        "prettyref": "refs/heads/other",
                        "commit_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success"
                    },
                    {
                        "prettyref": "refs/heads/main.feature",
                        "commit_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success"
                    }
                ]
            })))
            .mount(&server)
            .await;

        let status = client
            .actions_status_for_head(&owner(), &repo(), &branch("main.feature"), "abc123")
            .await
            .unwrap();

        assert_eq!(status, CIStatus::Success);
    }

    #[tokio::test]
    async fn submits_pull_request_review_with_forgejo_token() {
        let (client, server) = client().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/repos/owner/repo/pulls/9/reviews"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200))
            .mount(&server)
            .await;

        client
            .submit_pull_request_review(
                &owner(),
                &repo(),
                PRNumber::new(9),
                "APPROVED",
                "Looks good",
            )
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn commit_status_for_head_reads_forgejo_combined_status() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/commits/abc123/status"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "state": "success",
                "statuses": [
                    { "status": "pending", "context": "cargo test" },
                    { "status": "success", "context": "cargo test" }
                ]
            })))
            .mount(&server)
            .await;

        let status = client
            .commit_status_for_head(&owner(), &repo(), "abc123")
            .await
            .unwrap();

        assert_eq!(status, CIStatus::Success);
    }

    #[tokio::test]
    async fn latest_actions_status_for_branch_reads_newest_run() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/actions/runs"))
            .and(query_param("branch", "main"))
            .and(query_param("limit", "1"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "workflow_runs": [
                    {
                        "head_branch": "main",
                        "head_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success"
                    }
                ]
            })))
            .mount(&server)
            .await;

        let status = client
            .latest_actions_status_for_branch(&owner(), &repo(), &branch("main"))
            .await
            .unwrap();

        assert_eq!(status, Some(CIStatus::Success));
    }

    #[tokio::test]
    async fn latest_actions_status_for_branch_treats_missing_actions_as_absent() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/actions/runs"))
            .and(query_param("branch", "main"))
            .and(query_param("limit", "1"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&server)
            .await;

        let status = client
            .latest_actions_status_for_branch(&owner(), &repo(), &branch("main"))
            .await
            .unwrap();

        assert_eq!(status, None);
    }

    #[tokio::test]
    async fn finds_existing_pull_request_by_head_branch() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .and(query_param("state", "open"))
            .and(query_param("limit", "100"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
                {
                    "number": 8,
                    "html_url": "http://forgejo.local/owner/repo/pulls/8",
                    "head": { "ref": "other" },
                    "base": { "ref": "main" }
                },
                {
                    "number": 9,
                    "html_url": "http://forgejo.local/owner/repo/pulls/9",
                    "head": { "ref": "main.feature" },
                    "base": { "ref": "main" }
                }
            ])))
            .mount(&server)
            .await;

        let pr = client
            .find_open_pull_request(&owner(), &repo(), &branch("main.feature"))
            .await
            .unwrap()
            .unwrap();

        assert_eq!(pr.number.as_u64(), 9);
    }

    #[tokio::test]
    async fn lists_workflow_runs_for_branch_with_dashboard_fields() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/actions/runs"))
            .and(query_param("branch", "main.feature"))
            .and(query_param("limit", "4"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "workflow_runs": [
                    {
                        "name": "ci",
                        "display_title": "cargo test",
                        "prettyref": "refs/heads/main.feature",
                        "commit_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-05-24T03:00:00Z",
                        "updated_at": "2026-05-24T03:02:00Z"
                    }
                ]
            })))
            .mount(&server)
            .await;

        let runs = client
            .list_workflow_runs_for_branch(&owner(), &repo(), &branch("main.feature"), 4)
            .await
            .unwrap();

        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].name, "ci");
        assert_eq!(runs[0].display_title, "cargo test");
        assert_eq!(runs[0].conclusion.as_deref(), Some("success"));
        assert_eq!(runs[0].head_sha.as_deref(), Some("abc123"));
    }

    #[tokio::test]
    async fn lists_global_runners_for_dashboard() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/admin/actions/runners"))
            .and(query_param("limit", "100"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "runners": [
                    {
                        "name": "local-runner",
                        "status": "online",
                        "busy": true,
                        "disabled": false,
                        "last_online": "2026-05-24T03:04:00Z"
                    }
                ],
                "total_count": 1
            })))
            .mount(&server)
            .await;

        let runners = client.list_global_runners().await.unwrap();

        assert_eq!(runners.len(), 1);
        assert_eq!(runners[0].name, "local-runner");
        assert!(runners[0].busy);
        assert_eq!(
            runners[0].last_online.as_deref(),
            Some("2026-05-24T03:04:00Z")
        );
    }
}
