//! Integration tests: octocrab against the Python mock_github.py server.
//!
//! These tests start mock_github.py on an ephemeral port, point octocrab at it
//! via GITHUB_API_URL, and verify the PR lifecycle (list, create, list-with-filter).

use octocrab::{params, OctocrabBuilder, params::repos::Commitish};
use std::net::TcpListener;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

/// RAII guard that kills the mock server on drop.
struct MockServer {
    child: Child,
    port: u16,
}

impl MockServer {
    /// Start mock_github.py on an ephemeral port.
    fn start() -> Self {
        let port = pick_free_port();
        let mock_script =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/e2e/mock_github.py");

        let child = Command::new("python3")
            .args([mock_script.to_str().unwrap(), "--port", &port.to_string()])
            .env("MOCK_LOG", "/dev/null")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("Failed to start mock_github.py");

        // Wait until the server is listening
        for _ in 0..40 {
            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                return Self { child, port };
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("mock_github.py did not start within 2s on port {}", port);
    }

    fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    fn octocrab(&self) -> octocrab::Octocrab {
        OctocrabBuilder::new()
            .personal_token("test-token".to_string())
            .base_uri(&self.base_url())
            .unwrap()
            .build()
            .unwrap()
    }
}

impl Drop for MockServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn pick_free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.local_addr().unwrap().port()
}

#[tokio::test]
async fn mock_github_list_pulls_empty() {
    let server = MockServer::start();
    let octo = server.octocrab();

    let page = octo
        .pulls("test-owner", "test-repo")
        .list()
        .state(params::State::Open)
        .send()
        .await
        .expect("list pulls should succeed");

    let prs: Vec<_> = page.into_iter().collect();
    assert!(prs.is_empty(), "No PRs created yet");
}

#[tokio::test]
async fn mock_github_create_and_list_pull() {
    let server = MockServer::start();
    let octo = server.octocrab();

    // Create a PR
    let pr = octo
        .pulls("test-owner", "test-repo")
        .create("Add greet module", "main.greet", "main")
        .body("Adds greet function")
        .send()
        .await
        .expect("create PR should succeed");

    assert_eq!(pr.number, 1);
    assert_eq!(pr.head.ref_field, "main.greet");
    assert_eq!(pr.base.ref_field, "main");

    // List all open PRs — should include our PR
    let page = octo
        .pulls("test-owner", "test-repo")
        .list()
        .state(params::State::Open)
        .send()
        .await
        .expect("list pulls should succeed");

    let prs: Vec<_> = page.into_iter().collect();
    assert_eq!(prs.len(), 1);
    assert_eq!(prs[0].number, 1);
}

#[tokio::test]
async fn mock_github_list_pulls_with_head_filter() {
    let server = MockServer::start();
    let octo = server.octocrab();

    // Create two PRs on different branches
    octo.pulls("owner", "repo")
        .create("PR one", "feature-a", "main")
        .send()
        .await
        .expect("create PR 1");

    octo.pulls("owner", "repo")
        .create("PR two", "feature-b", "main")
        .send()
        .await
        .expect("create PR 2");

    // Filter by head=feature-a — should return only PR 1
    let page = octo
        .pulls("owner", "repo")
        .list()
        .head("feature-a")
        .state(params::State::Open)
        .per_page(1)
        .send()
        .await
        .expect("filtered list should succeed");

    let prs: Vec<_> = page.into_iter().collect();
    assert_eq!(prs.len(), 1, "Should find exactly one PR for feature-a");
    assert_eq!(prs[0].head.ref_field, "feature-a");
}

#[tokio::test]
async fn mock_github_create_then_find_existing() {
    let server = MockServer::start();
    let octo = server.octocrab();

    // Create a PR
    octo.pulls("owner", "repo")
        .create("Test PR", "main.greet", "main")
        .body("test body")
        .send()
        .await
        .expect("create PR");

    // Simulate what file_pr does: list with head filter to find existing
    let page = octo
        .pulls("owner", "repo")
        .list()
        .head("main.greet")
        .state(params::State::Open)
        .per_page(1)
        .send()
        .await
        .expect("find existing PR");

    let prs: Vec<_> = page.into_iter().collect();
    assert_eq!(prs.len(), 1);
    assert_eq!(prs[0].head.ref_field, "main.greet");
    assert_eq!(prs[0].base.ref_field, "main");
}

#[tokio::test]
async fn mock_github_reviews_empty_by_default() {
    let server = MockServer::start();
    let octo = server.octocrab();

    // Create a PR first
    let pr = octo
        .pulls("owner", "repo")
        .create("Test", "branch", "main")
        .send()
        .await
        .expect("create PR");

    // Fetch reviews — should be empty (no auto-approval)
    let reviews: Vec<serde_json::Value> = octo
        .get(
            format!("/repos/owner/repo/pulls/{}/reviews", pr.number),
            None::<&()>,
        )
        .await
        .expect("get reviews");

    assert_eq!(reviews.len(), 0, "No reviews posted yet");
}

#[tokio::test]
async fn mock_github_control_api_posts_review() {
    let server = MockServer::start();
    let octo = server.octocrab();

    // Create a PR first
    let pr = octo
        .pulls("owner", "repo")
        .create("Test", "branch", "main")
        .send()
        .await
        .expect("create PR");

    // Post a review via control API using curl
    let output = Command::new("curl")
        .args([
            "-sf",
            "-X",
            "POST",
            &format!("{}/_control/reviews", server.base_url()),
            "-H",
            "Content-Type: application/json",
            "-d",
            &format!(
                r#"{{"pr_number":{},"state":"APPROVED","body":"Looks good!"}}"#,
                pr.number
            ),
        ])
        .output()
        .expect("curl control API");
    assert!(output.status.success(), "control API POST failed");

    // Fetch reviews — should now have the posted review
    let reviews: Vec<serde_json::Value> = octo
        .get(
            format!("/repos/owner/repo/pulls/{}/reviews", pr.number),
            None::<&()>,
        )
        .await
        .expect("get reviews");

    assert_eq!(reviews.len(), 1);
    assert_eq!(reviews[0]["user"]["login"], "copilot[bot]");
    assert_eq!(reviews[0]["state"], "APPROVED");
    assert_eq!(reviews[0]["body"], "Looks good!");
}

#[tokio::test]
async fn mock_github_update_pr_body() {
    let server = MockServer::start();
    let octo = server.octocrab();

    let pr = octo
        .pulls("owner", "repo")
        .create("Initial title", "branch", "main")
        .body("Initial body")
        .send()
        .await
        .expect("create PR should succeed");

    assert_eq!(pr.body, Some("Initial body".to_string()));

    let updated = octo
        .pulls("owner", "repo")
        .update(pr.number as u64)
        .body("Updated body")
        .send()
        .await
        .expect("update PR body should succeed");

    assert_eq!(updated.body, Some("Updated body".to_string()));
}

#[tokio::test]
async fn mock_github_list_check_runs() {
    let server = MockServer::start();
    let octo = server.octocrab();

    let runs = octo
        .checks("owner", "repo")
        .list_check_runs_for_git_ref(Commitish("abc123".to_string()))
        .send()
        .await
        .expect("list check runs should succeed");

    assert_eq!(runs.total_count, 1);
    assert_eq!(runs.check_runs.len(), 1);
    assert_eq!(runs.check_runs[0].name, "build");
    assert_eq!(runs.check_runs[0].conclusion, Some("success".to_string()));
}
