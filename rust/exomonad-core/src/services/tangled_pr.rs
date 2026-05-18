use anyhow::{Context, Result};
use exomonad_proto::effects::file_pr::LocalPrResponse;
use reqwest::{Client, Url};
use serde::Deserialize;

#[derive(Debug, Clone)]
pub struct TangledPrClient {
    http: Client,
    appview_url: Url,
    repo: String,
}

impl TangledPrClient {
    pub fn new(appview_url: &str, repo: impl Into<String>) -> Result<Self> {
        let appview_url = Url::parse(appview_url).context("invalid Tangled appview URL")?;
        Ok(Self {
            http: Client::new(),
            appview_url,
            repo: repo.into(),
        })
    }

    pub async fn get_pull(&self, pr_number: i64) -> Result<Option<LocalPrResponse>> {
        let mut url = self.endpoint("sh.tangled.repo.getPull")?;
        url.query_pairs_mut()
            .append_pair("repo", &self.repo)
            .append_pair("pull", &pr_number.to_string());
        self.fetch(url).await
    }

    pub async fn get_pull_for_branch(&self, branch: &str) -> Result<Option<LocalPrResponse>> {
        let mut url = self.endpoint("sh.tangled.repo.getPullForBranch")?;
        url.query_pairs_mut()
            .append_pair("repo", &self.repo)
            .append_pair("branch", branch);
        self.fetch(url).await
    }

    fn endpoint(&self, method: &str) -> Result<Url> {
        self.appview_url
            .join(&format!("xrpc/{method}"))
            .context("failed to build Tangled appview URL")
    }

    async fn fetch(&self, url: Url) -> Result<Option<LocalPrResponse>> {
        let response = self
            .http
            .get(url)
            .send()
            .await
            .context("Tangled appview pull request lookup failed")?;

        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }

        let response = response
            .error_for_status()
            .context("Tangled appview pull request lookup returned an error")?;
        let pr: TangledPrResponse = response
            .json()
            .await
            .context("failed to decode Tangled appview pull request response")?;

        Ok(pr.into_local_response())
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TangledPrResponse {
    #[serde(default)]
    found: Option<bool>,
    #[serde(default, alias = "pull_id", alias = "pullId")]
    pr_number: Option<i64>,
    #[serde(default, alias = "source_branch", alias = "sourceBranch")]
    head_branch: Option<String>,
    #[serde(default, alias = "target_branch", alias = "targetBranch")]
    base_branch: Option<String>,
    #[serde(default, alias = "owner_did", alias = "ownerDid")]
    author_agent: Option<String>,
    #[serde(default)]
    state: Option<String>,
    #[serde(default, alias = "review_state", alias = "reviewState")]
    review_state: Option<String>,
    #[serde(
        default,
        alias = "latest_sha",
        alias = "latestSha",
        alias = "source_rev",
        alias = "sourceRev"
    )]
    last_head_sha: Option<String>,
    #[serde(default, alias = "reviewer_agent", alias = "reviewerAgent")]
    reviewer_agent: Option<String>,
}

impl TangledPrResponse {
    fn into_local_response(self) -> Option<LocalPrResponse> {
        if self.found == Some(false) {
            return None;
        }

        let pr_number = self.pr_number.unwrap_or_default();
        let head_branch = self.head_branch.unwrap_or_default();
        if pr_number <= 0 || head_branch.is_empty() {
            return None;
        }

        Some(LocalPrResponse {
            found: true,
            pr_number,
            head_branch,
            base_branch: self.base_branch.unwrap_or_default(),
            author_agent: self.author_agent.unwrap_or_default(),
            review_state: self
                .review_state
                .or_else(|| self.state.map(review_state_from_pull_state))
                .unwrap_or_else(|| "pending_review".to_string()),
            last_head_sha: self.last_head_sha.unwrap_or_default(),
            reviewer_agent: self.reviewer_agent.unwrap_or_default(),
        })
    }
}

fn review_state_from_pull_state(state: String) -> String {
    match state.as_str() {
        "merged" => "approved",
        "closed" | "abandoned" => "changes_requested",
        _ => "pending_review",
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[tokio::test]
    async fn get_pull_reads_appview_response() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/xrpc/sh.tangled.repo.getPull"))
            .and(query_param("repo", "did:plc:owner"))
            .and(query_param("pull", "12"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "prNumber": 12,
                "headBranch": "main.fix-tangled-pr-codex",
                "baseBranch": "main",
                "ownerDid": "did:plc:owner",
                "state": "open",
                "latestSha": "abc123"
            })))
            .mount(&server)
            .await;

        let client = TangledPrClient::new(&server.uri(), "did:plc:owner").unwrap();
        let response = client.get_pull(12).await.unwrap().unwrap();

        assert!(response.found);
        assert_eq!(response.pr_number, 12);
        assert_eq!(response.head_branch, "main.fix-tangled-pr-codex");
        assert_eq!(response.review_state, "pending_review");
        assert_eq!(response.last_head_sha, "abc123");
    }

    #[tokio::test]
    async fn get_pull_for_branch_returns_none_on_404() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/xrpc/sh.tangled.repo.getPullForBranch"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&server)
            .await;

        let client = TangledPrClient::new(&server.uri(), "did:plc:owner").unwrap();
        let response = client
            .get_pull_for_branch("main.missing-codex")
            .await
            .unwrap();

        assert!(response.is_none());
    }
}
