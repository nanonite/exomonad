use crate::domain::{BranchName, CIStatus, GithubOwner, GithubRepo, PRNumber};
use anyhow::{anyhow, Context, Result};
use reqwest::{header, StatusCode, Url};
use serde::de::{MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
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
    pub base_sha: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoPullRequestReview {
    pub id: Option<u64>,
    pub state: String,
    pub body: String,
    pub commit_id: Option<String>,
    pub author_login: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoPullRequestReviewComment {
    pub body: String,
    pub path: Option<String>,
    pub diff_hunk: Option<String>,
    pub in_reply_to: Option<u64>,
    pub resolved: bool,
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
struct ClosePullRequestBody {
    state: &'static str,
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
    id: Option<u64>,
    #[serde(default)]
    state: String,
    #[serde(default)]
    body: String,
    #[serde(default)]
    commit_id: Option<String>,
    #[serde(default)]
    user: Option<PullRequestReviewAuthor>,
}

#[derive(Debug, Deserialize)]
struct PullRequestReviewAuthor {
    #[serde(default)]
    login: Option<String>,
    #[serde(default)]
    username: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PullRequestReviewCommentResponse {
    #[serde(default)]
    body: String,
    #[serde(default)]
    path: Option<String>,
    #[serde(default)]
    diff_hunk: Option<String>,
    #[serde(default, alias = "in_reply_to_id")]
    in_reply_to: Option<u64>,
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
    #[serde(default)]
    head_branch: Option<String>,
    #[serde(default, rename = "ref")]
    ref_name: Option<String>,
    #[serde(default, rename = "prettyref")]
    pretty_ref: Option<String>,
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

#[derive(Debug)]
enum RunnersResponse {
    Wrapped { runners: Vec<RunnerResponse> },
    Bare(Vec<RunnerResponse>),
}

impl<'de> Deserialize<'de> for RunnersResponse {
    fn deserialize<D>(deserializer: D) -> std::result::Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct RunnersResponseVisitor;

        impl<'de> Visitor<'de> for RunnersResponseVisitor {
            type Value = RunnersResponse;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a Forgejo runner array or object containing runners")
            }

            fn visit_map<A>(self, map: A) -> std::result::Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                #[derive(Deserialize)]
                struct WrappedRunners {
                    runners: Vec<RunnerResponse>,
                }

                WrappedRunners::deserialize(serde::de::value::MapAccessDeserializer::new(map)).map(
                    |wrapped| RunnersResponse::Wrapped {
                        runners: wrapped.runners,
                    },
                )
            }

            fn visit_seq<A>(self, seq: A) -> std::result::Result<Self::Value, A::Error>
            where
                A: SeqAccess<'de>,
            {
                Vec::<RunnerResponse>::deserialize(serde::de::value::SeqAccessDeserializer::new(
                    seq,
                ))
                .map(RunnersResponse::Bare)
            }
        }

        deserializer.deserialize_any(RunnersResponseVisitor)
    }
}

impl RunnersResponse {
    fn into_runners(self) -> Vec<RunnerResponse> {
        match self {
            Self::Wrapped { runners } => runners,
            Self::Bare(runners) => runners,
        }
    }
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

const PULL_REQUEST_PAGE_SIZE: usize = 50;
const FORGEJO_HTTP_TIMEOUT: Duration = Duration::from_secs(10);

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

    /// Find every PR, including closed and merged PRs, whose head branch matches.
    ///
    /// Cleanup needs the full PR history: an open-only lookup would make a
    /// closed orphan look like it has no PR and would either leak resources or
    /// tempt callers into an unsafe forced cleanup.
    pub async fn find_pull_requests_by_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Vec<ForgejoPullRequest>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client.find_pull_requests_by_head(owner, repo, head).await
            }
            ForgejoBackend::Fj(client) => {
                client.find_pull_requests_by_head(owner, repo, head).await
            }
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

    pub async fn list_pull_request_review_comments(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        review_id: u64,
    ) -> Result<Vec<ForgejoPullRequestReviewComment>> {
        match &self.backend {
            ForgejoBackend::Http(client) => {
                client
                    .list_pull_request_review_comments(owner, repo, number, review_id)
                    .await
            }
            ForgejoBackend::Fj(client) => {
                client
                    .list_pull_request_review_comments(owner, repo, number, review_id)
                    .await
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
    ) -> Result<ForgejoPullRequest> {
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

    pub async fn close_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<()> {
        match &self.backend {
            ForgejoBackend::Http(client) => client.close_pull_request(owner, repo, number).await,
            ForgejoBackend::Fj(client) => client.close_pull_request(owner, repo, number).await,
        }
    }
}

fn binary_in_path(binary: &str) -> bool {
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
        .any(|dir| dir.join(binary).is_file())
}

fn next_pull_request_page(
    link_header: Option<&str>,
    total_count: Option<usize>,
    page: u32,
    result_count: usize,
) -> Option<u32> {
    if let Some(next_page) = link_header
        .and_then(next_page_from_link)
        .filter(|next_page| *next_page > page)
    {
        return Some(next_page);
    }

    let next_page = page.saturating_add(1);
    if total_count.is_some_and(|total| (page as usize) * PULL_REQUEST_PAGE_SIZE < total)
        || total_count.is_none() && result_count == PULL_REQUEST_PAGE_SIZE
    {
        Some(next_page)
    } else {
        None
    }
}

fn next_page_from_link(link_header: &str) -> Option<u32> {
    link_header.split(',').find_map(|link| {
        let (target, parameters) = link.split_once(';')?;
        let is_next = parameters
            .split(';')
            .any(|parameter| parameter.trim() == r#"rel="next""#);
        if !is_next {
            return None;
        }
        let target = target.trim().strip_prefix('<')?.strip_suffix('>')?;
        Url::parse(target)
            .ok()?
            .query_pairs()
            .find_map(|(key, value)| (key == "page").then(|| value.parse::<u32>().ok()).flatten())
    })
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
            .timeout(FORGEJO_HTTP_TIMEOUT)
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

    pub async fn find_pull_requests_by_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Vec<ForgejoPullRequest>> {
        let url = self.repo_pulls_url(owner, repo)?;
        let mut page = 1;
        let mut matching_prs = Vec::new();
        loop {
            let page_size = PULL_REQUEST_PAGE_SIZE.to_string();
            let page_number = page.to_string();
            let response = self
                .http
                .get(url.clone())
                .query(&[
                    ("state", "all"),
                    ("limit", page_size.as_str()),
                    ("page", page_number.as_str()),
                ])
                .headers(self.auth_headers()?)
                .send()
                .await
                .context("Forgejo PR list request failed")?;
            let link_header = response
                .headers()
                .get(header::LINK)
                .and_then(|value| value.to_str().ok())
                .map(str::to_owned);
            let total_count = response
                .headers()
                .get("x-total-count")
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<usize>().ok());
            let prs: Vec<PullRequestResponse> = self
                .decode_response(response, "list Forgejo pull requests")
                .await?;
            let result_count = prs.len();
            matching_prs.extend(
                prs.into_iter()
                    .filter(|pr| pr.head.ref_name == head.as_str())
                    .map(ForgejoPullRequest::try_from)
                    .collect::<Result<Vec<_>>>()?,
            );

            let Some(next_page) =
                next_pull_request_page(link_header.as_deref(), total_count, page, result_count)
            else {
                break;
            };
            page = next_page;
        }
        Ok(matching_prs)
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
                id: review.id,
                state: review.state,
                body: review.body,
                commit_id: review.commit_id,
                author_login: review.user.and_then(|user| user.login.or(user.username)),
            })
            .collect())
    }

    pub async fn list_pull_request_review_comments(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        review_id: u64,
    ) -> Result<Vec<ForgejoPullRequestReviewComment>> {
        let number_str = number.as_u64().to_string();
        let review_id_str = review_id.to_string();
        let url = self.api_url(&[
            "repos",
            owner.as_str(),
            repo.as_str(),
            "pulls",
            &number_str,
            "reviews",
            &review_id_str,
            "comments",
        ])?;
        let response = self
            .http
            .get(url)
            .headers(self.auth_headers()?)
            .send()
            .await
            .context("Forgejo PR review comments request failed")?;

        let comments: Vec<PullRequestReviewCommentResponse> = self
            .decode_response(response, "list Forgejo pull request review comments")
            .await?;
        Ok(comments
            .into_iter()
            .map(|c| ForgejoPullRequestReviewComment {
                body: c.body,
                path: c.path,
                diff_hunk: c.diff_hunk,
                in_reply_to: c.in_reply_to,
                resolved: false,
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
        let matching_statuses = runs
            .workflow_runs
            .iter()
            .filter(|run| workflow_run_matches_head(run, branch, head_sha))
            .map(workflow_status)
            .collect::<Vec<_>>();
        if matching_statuses.is_empty() {
            tracing::debug!(
                head_sha,
                branch = %branch,
                total_runs,
                "[Forgejo] No Actions run matched exact head SHA; CI status unknown"
            );
            return Ok(CIStatus::Unknown);
        }
        Ok(combine_workflow_statuses(matching_statuses))
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
        Ok(runs
            .workflow_runs
            .into_iter()
            .next()
            .map(|run| workflow_status(&run)))
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
            .into_runners()
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
    ) -> Result<ForgejoPullRequest> {
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

        let pr: PullRequestResponse = self
            .decode_response(response, "update Forgejo pull request")
            .await?;
        ForgejoPullRequest::try_from(pr)
    }

    async fn close_pull_request(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<()> {
        let url = self.repo_pull_url(owner, repo, number)?;
        let response = self
            .http
            .patch(url)
            .headers(self.auth_headers()?)
            .json(&ClosePullRequestBody { state: "closed" })
            .send()
            .await
            .context("Forgejo PR close request failed")?;
        self.expect_success(response, "close Forgejo pull request")
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
        let _ = body;
        Err(anyhow!("{action} failed with HTTP {status}"))
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

    async fn find_pull_requests_by_head(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        head: &BranchName,
    ) -> Result<Vec<ForgejoPullRequest>> {
        Ok(self
            .list_pull_requests(owner, repo)
            .await?
            .into_iter()
            .filter(|pr| pr.head_ref == *head)
            .collect())
    }

    async fn list_pull_requests(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
    ) -> Result<Vec<ForgejoPullRequest>> {
        let prs: Vec<PullRequestResponse> = self
            .fj_json(["pr", "list", "--state", "all", "--json"])
            .await?;
        prs.into_iter().map(ForgejoPullRequest::try_from).collect()
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
                id: review.id,
                state: review.state,
                body: review.body,
                commit_id: review.commit_id,
                author_login: review.user.and_then(|user| user.login.or(user.username)),
            })
            .collect())
    }

    async fn list_pull_request_review_comments(
        &self,
        owner: &GithubOwner,
        repo: &GithubRepo,
        number: PRNumber,
        review_id: u64,
    ) -> Result<Vec<ForgejoPullRequestReviewComment>> {
        let path = format!(
            "/repos/{}/{}/pulls/{}/reviews/{}/comments",
            owner.as_str(),
            repo.as_str(),
            number.as_u64(),
            review_id
        );
        let comments: Vec<PullRequestReviewCommentResponse> =
            self.fj_api_json("GET", &path).await?;
        Ok(comments
            .into_iter()
            .map(|c| ForgejoPullRequestReviewComment {
                body: c.body,
                path: c.path,
                diff_hunk: c.diff_hunk,
                in_reply_to: c.in_reply_to,
                resolved: false,
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
        let runners: RunnersResponse = self
            .fj_api_json("GET", "/admin/actions/runners?limit=100")
            .await?;
        Ok(runners
            .into_runners()
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
    ) -> Result<ForgejoPullRequest> {
        let pr_number = number;
        let number = pr_number.as_u64().to_string();
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
        .await?;
        self.get_pull_request(_owner, _repo, pr_number).await
    }

    async fn close_pull_request(
        &self,
        _owner: &GithubOwner,
        _repo: &GithubRepo,
        number: PRNumber,
    ) -> Result<()> {
        let number = number.as_u64().to_string();
        self.fj_status(["pr", "close", number.as_str()]).await
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
        parse_json_bytes(&output.stdout, "fj")
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

fn workflow_run_branch(run: &WorkflowRunResponse) -> Option<&str> {
    run.head_branch
        .as_deref()
        .and_then(normalize_workflow_branch)
        .or_else(|| run.ref_name.as_deref().and_then(normalize_workflow_branch))
}

fn normalize_workflow_branch(value: &str) -> Option<&str> {
    let value = value.trim();
    let branch = value.strip_prefix("refs/heads/").unwrap_or(value);
    if branch.is_empty() || branch.starts_with('#') || branch.starts_with("refs/") {
        None
    } else {
        Some(branch)
    }
}

fn workflow_run_matches_head(
    run: &WorkflowRunResponse,
    branch: &BranchName,
    head_sha: &str,
) -> bool {
    run.head_sha.as_deref() == Some(head_sha)
        && workflow_run_branch(run).is_none_or(|run_branch| run_branch == branch.as_str())
}

fn combine_workflow_statuses(statuses: impl IntoIterator<Item = CIStatus>) -> CIStatus {
    let statuses = statuses.into_iter().collect::<Vec<_>>();
    if statuses.contains(&CIStatus::Failure) {
        CIStatus::Failure
    } else if statuses.contains(&CIStatus::Pending) {
        CIStatus::Pending
    } else if statuses.contains(&CIStatus::Success) {
        CIStatus::Success
    } else if statuses.contains(&CIStatus::Neutral) {
        CIStatus::Neutral
    } else {
        CIStatus::Unknown
    }
}

fn workflow_status(run: &WorkflowRunResponse) -> CIStatus {
    run.conclusion
        .as_deref()
        .or(run.status.as_deref())
        .map(CIStatus::parse)
        .unwrap_or(CIStatus::Unknown)
}

struct ResponseBody(String);

impl ResponseBody {
    fn parse_json<T: serde::de::DeserializeOwned>(self, action: &str) -> Result<T> {
        parse_json_bytes(self.0.as_bytes(), action)
    }
}

fn parse_json_bytes<T: serde::de::DeserializeOwned>(body: &[u8], action: &str) -> Result<T> {
    let mut deserializer = serde_json::Deserializer::from_slice(body);
    serde_path_to_error::deserialize(&mut deserializer).map_err(|error| {
        let path = error.path().to_string();
        let source = error.into_inner();
        anyhow!(
            "{action} returned unparseable JSON at path {path} (line {}, column {}): {}",
            source.line(),
            source.column(),
            redact_serde_error(&source.to_string()),
        )
    })
}

fn redact_serde_error(message: &str) -> String {
    let mut redacted = String::with_capacity(message.len());
    let mut quote = None;
    let mut escaped = false;
    for character in message.chars() {
        if let Some(delimiter) = quote {
            if escaped {
                escaped = false;
            } else if character == '\\' && delimiter == '"' {
                escaped = true;
            } else if character == delimiter {
                quote = None;
                redacted.push_str("<redacted>");
            }
            continue;
        }
        if character == '"' || character == '\'' {
            quote = Some(character);
        } else {
            redacted.push(character);
        }
    }
    if quote.is_some() {
        redacted.push_str("<redacted>");
    }
    redacted
}

impl TryFrom<PullRequestResponse> for ForgejoPullRequest {
    type Error = anyhow::Error;

    fn try_from(value: PullRequestResponse) -> Result<Self> {
        Ok(Self {
            number: PRNumber::try_from(value.number)?,
            url: value.html_url.or(value.url).unwrap_or_default(),
            title: value.title,
            body: value.body,
            head_ref: BranchName::try_from(value.head.ref_name)?,
            base_ref: BranchName::try_from(value.base.ref_name)?,
            state: value.state,
            merged: value.merged,
            head_sha: value.head.sha,
            base_sha: value.base.sha,
        })
    }
}

impl From<WorkflowRunResponse> for ForgejoWorkflowRun {
    fn from(value: WorkflowRunResponse) -> Self {
        let status = value.status.unwrap_or_else(|| "unknown".to_string());
        Self {
            name: value.name.unwrap_or_else(|| "workflow".to_string()),
            display_title: value.display_title.unwrap_or_default(),
            head_branch: value.head_branch.or(value.ref_name).or(value.pretty_ref),
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

    #[tokio::test]
    async fn http_requests_have_a_bounded_timeout() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .respond_with(
                ResponseTemplate::new(200).set_delay(FORGEJO_HTTP_TIMEOUT + Duration::from_secs(1)),
            )
            .mount(&server)
            .await;
        let client = ForgejoClient::new(&server.uri(), "token-123").unwrap();

        let result = client.list_open_pull_requests(&owner(), &repo()).await;

        assert!(result.is_err(), "a slow Forgejo response must time out");
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

    fn workflow_run(
        head_branch: Option<&str>,
        ref_name: Option<&str>,
        pretty_ref: Option<&str>,
        head_sha: Option<&str>,
        status: Option<&str>,
        conclusion: Option<&str>,
    ) -> WorkflowRunResponse {
        WorkflowRunResponse {
            name: None,
            display_title: None,
            head_branch: head_branch.map(str::to_string),
            ref_name: ref_name.map(str::to_string),
            pretty_ref: pretty_ref.map(str::to_string),
            head_sha: head_sha.map(str::to_string),
            status: status.map(str::to_string),
            conclusion: conclusion.map(str::to_string),
            created_at: None,
            updated_at: None,
        }
    }

    #[test]
    fn actions_matching_uses_exact_sha_before_display_ref() {
        let display_ref = workflow_run(
            None,
            None,
            Some("#43"),
            Some("abc123"),
            Some("completed"),
            Some("success"),
        );
        let stale_display_ref = workflow_run(
            None,
            None,
            Some("#43"),
            Some("old-sha"),
            Some("completed"),
            Some("success"),
        );

        assert!(workflow_run_matches_head(
            &display_ref,
            &branch("main.feature"),
            "abc123"
        ));
        assert!(!workflow_run_matches_head(
            &stale_display_ref,
            &branch("main.feature"),
            "abc123"
        ));
    }

    #[test]
    fn actions_matching_uses_real_branch_or_ref_as_disambiguation() {
        let matching_branch = workflow_run(
            Some("refs/heads/main.feature"),
            None,
            Some("#43"),
            Some("abc123"),
            Some("completed"),
            Some("success"),
        );
        let other_branch = workflow_run(
            Some("other"),
            None,
            Some("#43"),
            Some("abc123"),
            Some("completed"),
            Some("success"),
        );

        assert!(workflow_run_matches_head(
            &matching_branch,
            &branch("main.feature"),
            "abc123"
        ));
        assert!(!workflow_run_matches_head(
            &other_branch,
            &branch("main.feature"),
            "abc123"
        ));
    }

    #[test]
    fn workflow_status_combination_preserves_failure_and_pending_precedence() {
        assert_eq!(
            combine_workflow_statuses([CIStatus::Success, CIStatus::Pending]),
            CIStatus::Pending
        );
        assert_eq!(
            combine_workflow_statuses([CIStatus::Success, CIStatus::Failure]),
            CIStatus::Failure
        );
        assert_eq!(
            combine_workflow_statuses([CIStatus::Success, CIStatus::Neutral]),
            CIStatus::Success
        );
    }

    #[tokio::test]
    async fn actions_status_http_errors_omit_response_payloads() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/actions/runs"))
            .respond_with(ResponseTemplate::new(500).set_body_string("sensitive-forgejo-payload"))
            .mount(&server)
            .await;

        let error = client
            .actions_status_for_head(&owner(), &repo(), &branch("main.feature"), "abc123")
            .await
            .expect_err("HTTP failure should be returned to the dashboard");

        assert!(error.to_string().contains("HTTP 500"));
        assert!(!error.to_string().contains("sensitive-forgejo-payload"));
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
                "head": { "ref": "main.feature", "sha": "sha-create" },
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
        assert_eq!(pr.head_sha.as_deref(), Some("sha-create"));
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
                "head": { "ref": "main.feature", "sha": "sha-update" },
                "base": { "ref": "main" }
            })))
            .mount(&server)
            .await;

        let pr = client
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
        assert_eq!(pr.head_sha.as_deref(), Some("sha-update"));
    }

    #[tokio::test]
    async fn list_reviews_accepts_real_forgejo_author_payload() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls/43/reviews"))
            .and(header("authorization", "token token-123"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_string(include_str!("fixtures/forgejo-pr43-reviews.json")),
            )
            .mount(&server)
            .await;

        let reviews = client
            .list_pull_request_reviews(&owner(), &repo(), PRNumber::new(43))
            .await
            .expect("captured Forgejo review response must deserialize");

        assert_eq!(reviews.len(), 1);
        assert_eq!(reviews[0].id, Some(403));
        assert_eq!(
            reviews[0].author_login.as_deref(),
            Some("exomonad-reviewer")
        );
    }

    #[test]
    fn rejects_missing_pr_number_without_panicking() {
        let response = PullRequestResponse {
            number: 0,
            title: String::new(),
            body: String::new(),
            state: "open".to_string(),
            merged: false,
            html_url: None,
            url: None,
            head: PullRequestBranch {
                ref_name: "main.feature".to_string(),
                sha: Some("sha-1".to_string()),
            },
            base: PullRequestBranch {
                ref_name: "main".to_string(),
                sha: None,
            },
        };

        assert!(ForgejoPullRequest::try_from(response).is_err());
    }

    #[test]
    fn parse_json_reports_path_and_position_without_response_body() {
        #[allow(dead_code)]
        #[derive(Debug, Deserialize)]
        struct Payload {
            count: u64,
        }

        let body = br#"{"count":"sensitive-sentinel"}"#;
        let error = parse_json_bytes::<Payload>(body, "decode test payload")
            .expect_err("string count must not deserialize as u64")
            .to_string();

        assert!(error.contains("path count"), "{error}");
        assert!(error.contains("line 1"), "{error}");
        assert!(error.contains("column"), "{error}");
        assert!(!error.contains("sensitive-sentinel"), "{error}");
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
                        "prettyref": "#43",
                        "commit_sha": "abc123",
                        "status": "completed",
                        "conclusion": "success"
                    },
                    {
                        "head_branch": "main.feature",
                        "prettyref": "#43",
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
    async fn finds_pull_request_by_head_across_paginated_history() {
        let (client, server) = client().await;
        let first_page: Vec<_> = (54..=103)
            .map(|number| {
                serde_json::json!({
                    "number": number,
                    "html_url": format!("http://forgejo.local/owner/repo/pulls/{number}"),
                    "head": { "ref": format!("branch-{number}") },
                    "base": { "ref": "main" },
                    "state": "closed"
                })
            })
            .collect();
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .and(query_param("state", "all"))
            .and(query_param("limit", "50"))
            .and(query_param("page", "1"))
            .respond_with(
                ResponseTemplate::new(200)
                    .insert_header(
                        "Link",
                        format!(
                            "<{}/api/v1/repos/owner/repo/pulls?page=2>; rel=\"next\"",
                            server.uri()
                        ),
                    )
                    .set_body_json(first_page),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .and(query_param("state", "all"))
            .and(query_param("limit", "50"))
            .and(query_param("page", "2"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
                {
                    "number": 24,
                    "html_url": "http://forgejo.local/owner/repo/pulls/24",
                    "head": { "ref": "issue-33-performance-debugging-opencode" },
                    "base": { "ref": "main" },
                    "state": "closed"
                }
            ])))
            .mount(&server)
            .await;

        let prs = client
            .find_pull_requests_by_head(
                &owner(),
                &repo(),
                &branch("issue-33-performance-debugging-opencode"),
            )
            .await
            .unwrap();

        assert_eq!(
            prs.iter().map(|pr| pr.number.as_u64()).collect::<Vec<_>>(),
            [24]
        );
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

    #[tokio::test]
    async fn lists_global_runners_from_bare_array_for_dashboard() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/admin/actions/runners"))
            .and(query_param("limit", "100"))
            .and(header("authorization", "token token-123"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([
                {
                    "name": "bare-runner",
                    "status": "online",
                    "busy": false,
                    "disabled": false,
                    "last_online": "2026-05-24T03:05:00Z"
                }
            ])))
            .mount(&server)
            .await;

        let runners = client.list_global_runners().await.unwrap();

        assert_eq!(runners.len(), 1);
        assert_eq!(runners[0].name, "bare-runner");
        assert!(!runners[0].busy);
        assert_eq!(
            runners[0].last_online.as_deref(),
            Some("2026-05-24T03:05:00Z")
        );
    }

    #[tokio::test]
    async fn malformed_bare_runner_reports_path_without_response_body() {
        let (client, server) = client().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/admin/actions/runners"))
            .and(query_param("limit", "100"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_string(r#"[{"name":["sensitive-runner"],"status":"online"}]"#),
            )
            .mount(&server)
            .await;

        let error = client
            .list_global_runners()
            .await
            .expect_err("malformed runner fields must fail closed");
        let message = error.to_string();

        assert!(message.contains("path [0].name"), "{message}");
        assert!(!message.contains("sensitive-runner"), "{message}");
    }
}
