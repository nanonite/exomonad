use crate::domain::{BranchName, GithubOwner, GithubRepo, PRNumber};
use anyhow::{anyhow, Context, Result};
use reqwest::{header, StatusCode, Url};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
pub struct ForgejoClient {
    base_url: Url,
    token: String,
    http: reqwest::Client,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ForgejoPullRequest {
    pub number: PRNumber,
    pub url: String,
    pub head_ref: BranchName,
    pub base_ref: BranchName,
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

#[derive(Debug, Deserialize)]
struct PullRequestResponse {
    number: u64,
    html_url: Option<String>,
    url: Option<String>,
    head: PullRequestBranch,
    base: PullRequestBranch,
}

#[derive(Debug, Deserialize)]
struct PullRequestBranch {
    #[serde(rename = "ref")]
    ref_name: String,
}

impl ForgejoClient {
    pub fn new(forgejo_url: &str, forgejo_token: &str) -> Result<Arc<Self>> {
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

        Ok(Arc::new(Self {
            base_url,
            token: forgejo_token.to_string(),
            http,
        }))
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
            head_ref: BranchName::try_from(value.head.ref_name)?,
            base_ref: BranchName::try_from(value.base.ref_name)?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
}
