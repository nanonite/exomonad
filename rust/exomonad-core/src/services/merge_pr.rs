use crate::domain::{BranchName, MergeStrategy, PRNumber};
use crate::services::agent_resolver::AgentResolver;
use crate::services::forgejo::ForgejoClient;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::pr_registry::verify_current_publication_ownership;
use crate::services::repo;
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::path::Path;
use std::sync::Arc;
use tokio::process::Command;
use tokio::time::Duration;
use tracing::{error, info};

const MERGE_TIMEOUT: Duration = Duration::from_secs(120);

pub struct MergePROutput {
    pub success: bool,
    pub message: String,
    pub git_fetched: bool,
    pub branch_name: BranchName,
    pub head_sha: Option<String>,
}

pub struct MergeExpectedEvidence<'a> {
    pub base_sha: Option<&'a str>,
    pub head_sha: Option<&'a str>,
    pub patch_digest: Option<&'a str>,
    pub merge_tree_sha: Option<&'a str>,
}

pub struct MergeObservedEvidence {
    pub base_sha: String,
    pub head_sha: String,
    pub patch_digest: String,
    pub merge_tree_sha: String,
}

pub struct MergeAuthority<'a> {
    pub resolver: &'a AgentResolver,
    pub project_dir: &'a Path,
}

/// Merge a PR using the Forgejo API.
pub async fn merge_pr_async(
    pr_number: PRNumber,
    strategy: &MergeStrategy,
    working_dir: &str,
    expected: MergeExpectedEvidence<'_>,
    git_wt: Arc<GitWorktreeService>,
    forgejo: Option<&ForgejoClient>,
    authority: MergeAuthority<'_>,
) -> Result<MergePROutput> {
    let dir = if working_dir.is_empty() {
        "."
    } else {
        working_dir
    };

    info!(
        pr_number = pr_number.as_u64(),
        strategy = strategy.as_str(),
        working_dir = dir,
        "Merging Forgejo PR"
    );

    let Some(forgejo) = forgejo else {
        anyhow::bail!("Forgejo client is required for merge_pr");
    };
    let repo_info = repo::get_repo_info(dir).await?;
    let pr = forgejo
        .get_pull_request(&repo_info.owner, &repo_info.repo, pr_number)
        .await?;
    let branch_name = pr.head_ref.clone();
    let head_sha = pr.head_sha.clone();
    let Some(actual_head) = pr.head_sha.as_deref() else {
        return Ok(cas_failure(
            "compare_and_swap: PR has no authoritative head SHA".to_string(),
            branch_name,
            head_sha,
        ));
    };
    if let Err(error) = verify_current_publication_ownership(
        authority.project_dir,
        authority.resolver,
        pr_number.as_u64(),
        pr.head_ref.as_str(),
        pr.base_ref.as_str(),
        actual_head,
    )
    .await
    {
        return Ok(cas_failure(
            format!("compare_and_swap: publication ownership verification failed: {error}"),
            branch_name,
            head_sha,
        ));
    }
    if let Some(expected) = expected.head_sha.filter(|value| !value.is_empty()) {
        if pr.head_sha.as_deref() != Some(expected) {
            return Ok(MergePROutput {
                success: false,
                message: format!(
                    "compare_and_swap: expected head {expected}, found {:?}",
                    pr.head_sha
                ),
                git_fetched: false,
                branch_name,
                head_sha,
            });
        }
    }
    if expected.base_sha.is_some()
        || expected.patch_digest.is_some()
        || expected.merge_tree_sha.is_some()
    {
        let Some(actual_head) = pr.head_sha.as_deref() else {
            return Ok(cas_failure(
                "compare_and_swap: PR has no authoritative head SHA".to_string(),
                branch_name,
                head_sha,
            ));
        };
        let observed = match observe_pr_evidence_for_pr(
            dir,
            pr.base_ref.as_str(),
            actual_head,
            pr_number.as_u64(),
        )
        .await
        {
            Ok(observed) => observed,
            Err(error) => {
                return Ok(cas_failure(
                    format!("compare_and_swap: evidence observation failed: {error}"),
                    branch_name,
                    head_sha,
                ));
            }
        };
        if let Err(message) = compare_expected_evidence(&expected, &observed) {
            return Ok(cas_failure(message, branch_name, head_sha));
        }
    }

    let method = match strategy {
        MergeStrategy::Squash => "squash",
        MergeStrategy::Merge => "merge",
        MergeStrategy::Rebase => "rebase",
    };
    let merge_result = tokio::time::timeout(
        MERGE_TIMEOUT,
        forgejo.merge_pull_request(
            &repo_info.owner,
            &repo_info.repo,
            pr_number,
            method,
            actual_head,
        ),
    )
    .await;

    if let Err(error) = match merge_result {
        Ok(result) => result.map_err(|error| anyhow::anyhow!("Forgejo merge failed: {error}")),
        Err(_) => Err(anyhow::anyhow!(
            "Forgejo merge timed out after {}s",
            MERGE_TIMEOUT.as_secs()
        )),
    } {
        error!(error = %error, "Forgejo merge failed");
        return Ok(MergePROutput {
            success: false,
            message: error.to_string(),
            git_fetched: false,
            branch_name,
            head_sha,
        });
    }

    info!(pr_number = pr_number.as_u64(), "PR merged successfully");

    let dir_path = std::path::PathBuf::from(dir);
    let git_wt_clone = git_wt.clone();
    let git_result = tokio::task::spawn_blocking(move || git_wt_clone.fetch(&dir_path)).await;

    let git_fetched = match git_result {
        Ok(Ok(())) => {
            info!("git fetch succeeded");
            true
        }
        Ok(Err(e)) => {
            info!(error = %e, "git fetch failed");
            false
        }
        Err(e) => {
            info!(error = %e, "git fetch spawn_blocking failed");
            false
        }
    };

    Ok(MergePROutput {
        success: true,
        message: format!("PR #{} merged via {}", pr_number, strategy),
        git_fetched,
        branch_name,
        head_sha,
    })
}

fn cas_failure(
    message: String,
    branch_name: BranchName,
    head_sha: Option<String>,
) -> MergePROutput {
    MergePROutput {
        success: false,
        message,
        git_fetched: false,
        branch_name,
        head_sha,
    }
}

async fn authoritative_base_sha(dir: &str, base_ref: &str) -> Result<String> {
    let remote = configured_remote(dir).await?;
    run_git(dir, &["fetch", "--prune", &remote]).await?;
    let remote_ref = format!("refs/remotes/{remote}/{base_ref}");
    run_git(dir, &["rev-parse", "--verify", &remote_ref]).await
}

/// Observe the same authoritative evidence used by merge compare-and-swap.
pub async fn observe_pr_evidence(
    dir: &str,
    base_ref: &str,
    head_sha: &str,
) -> Result<MergeObservedEvidence> {
    let base_sha = authoritative_base_sha(dir, base_ref).await?;
    ensure_head_object(dir, head_sha, None).await?;
    observe_merge_evidence(dir, &base_sha, head_sha).await
}

/// Observe PR evidence after attempting one bounded recovery of a missing
/// Forgejo pull-request head ref. A missing object is expected after branch
/// deletion, so callers can classify the pr_head_unreachable: error without
/// treating it as a controller-fatal transport failure.
pub async fn observe_pr_evidence_for_pr(
    dir: &str,
    base_ref: &str,
    head_sha: &str,
    pr_number: u64,
) -> Result<MergeObservedEvidence> {
    let base_sha = authoritative_base_sha(dir, base_ref).await?;
    ensure_head_object(dir, head_sha, Some(pr_number)).await?;
    observe_merge_evidence(dir, &base_sha, head_sha).await
}

async fn ensure_head_object(dir: &str, head_sha: &str, pr_number: Option<u64>) -> Result<()> {
    let object = format!("{head_sha}^{{commit}}");
    if run_git(dir, &["cat-file", "-e", &object]).await.is_ok() {
        return Ok(());
    }
    let Some(pr_number) = pr_number else {
        anyhow::bail!("pr_head_unreachable: local object {head_sha} is unavailable");
    };
    let remote = configured_remote(dir).await?;
    let refspec = format!("refs/pull/{pr_number}/head");
    if let Err(error) = run_git(dir, &["fetch", "--no-tags", &remote, &refspec]).await {
        anyhow::bail!("pr_head_unreachable: fetch of {refspec} from {remote} failed: {error}");
    }
    if run_git(dir, &["cat-file", "-e", &object]).await.is_err() {
        anyhow::bail!(
            "pr_head_unreachable: fetched {refspec}, but local object {head_sha} is still absent"
        );
    }
    Ok(())
}

async fn configured_remote(dir: &str) -> Result<String> {
    let configured = run_git(dir, &["config", "--get", "exomonad.remote"])
        .await
        .ok()
        .filter(|remote| !remote.is_empty());
    if let Some(remote) = configured {
        return Ok(remote);
    }
    let remotes = run_git(dir, &["remote"]).await?;
    if remotes.lines().any(|remote| remote.trim() == "origin") {
        return Ok("origin".to_string());
    }
    remotes
        .lines()
        .map(str::trim)
        .find(|remote| !remote.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("no git remote is configured"))
}

async fn observe_merge_evidence(
    dir: &str,
    base_sha: &str,
    head_sha: &str,
) -> Result<MergeObservedEvidence> {
    let diff = run_git_bytes(
        dir,
        &[
            "diff",
            "--binary",
            "--no-ext-diff",
            &format!("{base_sha}...{head_sha}"),
        ],
    )
    .await?;
    let merge_tree = run_git(dir, &["merge-tree", "--write-tree", base_sha, head_sha]).await?;
    let merge_tree_sha = merge_tree
        .split_whitespace()
        .next()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow::anyhow!("git merge-tree returned no tree SHA"))?;
    Ok(MergeObservedEvidence {
        base_sha: base_sha.to_string(),
        head_sha: head_sha.to_string(),
        patch_digest: format!("{:x}", Sha256::digest(&diff)),
        merge_tree_sha: merge_tree_sha.to_string(),
    })
}

async fn run_git(dir: &str, args: &[&str]) -> Result<String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .await?;
    if !output.status.success() {
        anyhow::bail!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

async fn run_git_bytes(dir: &str, args: &[&str]) -> Result<Vec<u8>> {
    let output = Command::new("git")
        .args(args)
        .current_dir(dir)
        .output()
        .await?;
    if !output.status.success() {
        anyhow::bail!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(output.stdout)
}

fn compare_expected_evidence(
    expected: &MergeExpectedEvidence<'_>,
    observed: &MergeObservedEvidence,
) -> std::result::Result<(), String> {
    let checks = [
        ("base", expected.base_sha, observed.base_sha.as_str()),
        ("head", expected.head_sha, observed.head_sha.as_str()),
        (
            "patch digest",
            expected.patch_digest,
            observed.patch_digest.as_str(),
        ),
        (
            "merge tree",
            expected.merge_tree_sha,
            observed.merge_tree_sha.as_str(),
        ),
    ];
    for (label, expected_value, actual) in checks {
        if let Some(expected_value) = expected_value.filter(|value| !value.is_empty()) {
            if expected_value != actual {
                return Err(format!(
                    "compare_and_swap: expected {label} {expected_value}, found {actual}"
                ));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_strategy_strings_are_stable() {
        assert_eq!(MergeStrategy::Squash.as_str(), "squash");
        assert_eq!(MergeStrategy::Merge.as_str(), "merge");
        assert_eq!(MergeStrategy::Rebase.as_str(), "rebase");
    }

    #[test]
    fn compare_and_swap_checks_every_evidence_dimension() {
        let observed = MergeObservedEvidence {
            base_sha: "base".to_string(),
            head_sha: "head".to_string(),
            patch_digest: "patch".to_string(),
            merge_tree_sha: "tree".to_string(),
        };
        for (label, value) in [
            ("base", "other-base"),
            ("head", "other-head"),
            ("patch digest", "other-patch"),
            ("merge tree", "other-tree"),
        ] {
            let expected = MergeExpectedEvidence {
                base_sha: (label == "base").then_some(value),
                head_sha: (label == "head").then_some(value),
                patch_digest: (label == "patch digest").then_some(value),
                merge_tree_sha: (label == "merge tree").then_some(value),
            };
            let error = compare_expected_evidence(&expected, &observed).unwrap_err();
            assert!(error.contains(label), "{error}");
        }
    }

    #[tokio::test]
    async fn missing_pr_head_is_recovered_from_pull_ref_once() {
        use std::process::Command;

        fn git(dir: &std::path::Path, args: &[&str]) -> String {
            let output = Command::new("git")
                .args(args)
                .current_dir(dir)
                .output()
                .expect("git should start");
            assert!(
                output.status.success(),
                "git {:?} failed: {}",
                args,
                String::from_utf8_lossy(&output.stderr)
            );
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        }

        let source = tempfile::tempdir().expect("source repo");
        git(source.path(), &["init", "-q"]);
        git(source.path(), &["config", "user.email", "test@example.com"]);
        git(source.path(), &["config", "user.name", "Test"]);
        std::fs::write(source.path().join("file.txt"), "head\n").expect("write source");
        git(source.path(), &["add", "file.txt"]);
        git(source.path(), &["commit", "-qm", "head"]);
        let head = git(source.path(), &["rev-parse", "HEAD"]);

        let remote = tempfile::tempdir().expect("bare remote");
        git(remote.path(), &["init", "--bare", "-q"]);
        git(
            source.path(),
            &["remote", "add", "origin", remote.path().to_str().unwrap()],
        );
        git(
            source.path(),
            &["push", "-q", "origin", "HEAD:refs/heads/main"],
        );
        git(remote.path(), &["update-ref", "refs/pull/7/head", &head]);

        let local = tempfile::tempdir().expect("local repo");
        git(local.path(), &["init", "-q"]);
        git(
            local.path(),
            &["remote", "add", "origin", remote.path().to_str().unwrap()],
        );

        ensure_head_object(local.path().to_str().unwrap(), &head, Some(7))
            .await
            .expect("pull ref should recover the missing head");
        git(
            local.path(),
            &["cat-file", "-e", &format!("{head}^{{commit}}")],
        );
    }

    #[tokio::test]
    async fn missing_head_without_pr_identity_is_typed() {
        let repo = tempfile::tempdir().expect("repo");
        let error = ensure_head_object(repo.path().to_str().unwrap(), "deadbeef", None)
            .await
            .expect_err("missing identity must not trigger a broad fetch");
        assert!(error.to_string().contains("pr_head_unreachable:"));
    }
}
