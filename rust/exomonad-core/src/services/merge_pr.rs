use crate::domain::{BranchName, MergeStrategy, PRNumber};
use crate::services::agent_resolver::AgentResolver;
use crate::services::forgejo::ForgejoClient;
use crate::services::pr_registry::verified_current_publication_owner;
use crate::services::repo;
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::path::Path;
use tokio::process::Command;
use tokio::time::Duration;
use tracing::{error, info};

const MERGE_TIMEOUT: Duration = Duration::from_secs(120);

pub struct MergePROutput {
    pub success: bool,
    pub message: String,
    /// Kept for protocol compatibility; `merge_pr` does not fetch or integrate locally.
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
    pub pr_head_tree_sha: String,
    pub merge_commit_sha: Option<String>,
    pub merge_commit_tree_sha: Option<String>,
}

pub struct PublishedPrEvidence {
    pub base_sha: String,
    pub patch_digest: String,
}

pub(crate) struct MergeAuthority<'a> {
    pub resolver: &'a AgentResolver,
    pub project_dir: &'a Path,
    pub caller_agent_id: String,
    pub caller_branch: String,
    pub current_repository_url: String,
    pub parent_approval:
        Option<crate::handlers::merge_pr::aggregate_approval::VerifiedParentApproval>,
}

/// Merge a PR using the Forgejo API.
pub(crate) async fn merge_pr_async(
    pr_number: PRNumber,
    strategy: &MergeStrategy,
    working_dir: &str,
    expected: MergeExpectedEvidence<'_>,
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
    let publication_owner = match verified_current_publication_owner(
        authority.project_dir,
        authority.resolver,
        pr_number.as_u64(),
        pr.head_ref.as_str(),
        pr.base_ref.as_str(),
        actual_head,
    )
    .await
    {
        Ok(owner) => owner,
        Err(error) => {
            return Ok(cas_failure(
                format!("compare_and_swap: publication ownership verification failed: {error}"),
                branch_name,
                head_sha,
            ));
        }
    };
    if let Some(approval) = authority.parent_approval.as_ref() {
        if pr_number.as_u64() != approval.pr_number
            || pr.state != "open"
            || pr.merged
            || pr.head_ref.as_str() != approval.head_branch
            || pr.base_ref.as_str() != approval.parent_branch
        {
            return Ok(cas_failure(
                "aggregate parent approval does not match the current open PR coordinates"
                    .to_string(),
                branch_name,
                head_sha,
            ));
        }
        if publication_owner == authority.caller_agent_id
            || authority.caller_branch == pr.head_ref.as_str()
        {
            return Ok(cas_failure(
                "aggregate parent approval cannot be used by the publication owner".to_string(),
                branch_name,
                head_sha,
            ));
        }
        let live_repository_url = match repo::get_repository_remote_url(dir).await {
            Ok(url) => url,
            Err(error) => {
                return Ok(cas_failure(
                    format!("aggregate repository identity could not be revalidated: {error}"),
                    branch_name,
                    head_sha,
                ));
            }
        };
        if live_repository_url != approval.repository_url
            || authority.current_repository_url != approval.repository_url
        {
            return Ok(cas_failure(
                "aggregate parent approval repository differs from the current repository"
                    .to_string(),
                branch_name,
                head_sha,
            ));
        }
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
            None,
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
        if let Some(approval) = authority.parent_approval.as_ref() {
            if observed.base_sha != approval.base_sha
                || observed.head_sha != approval.head_sha
                || observed.pr_head_tree_sha != approval.head_tree_sha
                || observed.patch_digest != approval.patch_digest
                || observed.merge_tree_sha != approval.merge_tree_sha
            {
                return Ok(cas_failure(
                    "aggregate parent approval compare evidence changed before merge".to_string(),
                    branch_name,
                    head_sha,
                ));
            }
            let ci_status = match forgejo
                .commit_status_for_head(&repo_info.owner, &repo_info.repo, actual_head)
                .await
            {
                Ok(status) => status.as_str(),
                Err(error) => {
                    return Ok(cas_failure(
                        format!("aggregate CI evidence could not be revalidated: {error}"),
                        branch_name,
                        head_sha,
                    ));
                }
            };
            if ci_status != approval.ci_status || !matches!(ci_status, "success" | "neutral") {
                return Ok(cas_failure(
                    format!("aggregate CI evidence changed or is not mergeable: {ci_status}"),
                    branch_name,
                    head_sha,
                ));
            }
            let current_repository_url = match repo::get_repository_remote_url(dir).await {
                Ok(url) => url,
                Err(error) => {
                    return Ok(cas_failure(
                        format!(
                            "aggregate repository identity could not be checked at merge: {error}"
                        ),
                        branch_name,
                        head_sha,
                    ));
                }
            };
            let refreshed_approval =
                match crate::handlers::merge_pr::aggregate_approval::validate_parent_approval(
                    authority.project_dir,
                    &approval.reference,
                    pr_number.as_u64(),
                    &current_repository_url,
                    &authority.caller_agent_id,
                    &authority.caller_branch,
                    authority.resolver,
                )
                .await
                {
                    Ok(refreshed) => refreshed,
                    Err(error) => {
                        return Ok(cas_failure(
                            format!(
                                "aggregate parent approval became invalid before merge: {error}"
                            ),
                            branch_name,
                            head_sha,
                        ));
                    }
                };
            if refreshed_approval.receipt_id != approval.receipt_id
                || current_repository_url != approval.repository_url
            {
                return Ok(cas_failure(
                    "aggregate parent approval or repository changed before merge".to_string(),
                    branch_name,
                    head_sha,
                ));
            }
            let owner_repository_url =
                match repo::get_repository_remote_url(&refreshed_approval.aggregate_worktree).await
                {
                    Ok(url) => url,
                    Err(error) => {
                        return Ok(cas_failure(
                            format!("aggregate owner repository could not be revalidated: {error}"),
                            branch_name,
                            head_sha,
                        ));
                    }
                };
            if owner_repository_url != refreshed_approval.repository_url {
                return Ok(cas_failure(
                    "aggregate owner worktree belongs to a different repository".to_string(),
                    branch_name,
                    head_sha,
                ));
            }
            if let Err(error) = crate::services::aggregate_lineage::verify_parent_aggregate_lineage(
                &refreshed_approval.aggregate_worktree,
                &refreshed_approval.head_sha,
                &refreshed_approval.head_tree_sha,
                &refreshed_approval.lineage_commit_shas,
                &refreshed_approval.lineage_proof_digest,
            )
            .await
            {
                return Ok(cas_failure(
                    format!("aggregate Git lineage could not be revalidated: {error}"),
                    branch_name,
                    head_sha,
                ));
            }
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

    Ok(MergePROutput {
        success: true,
        message: format!("PR #{} merged via {}", pr_number, strategy),
        git_fetched: false,
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
    observe_merge_evidence(dir, &base_sha, head_sha, None).await
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
    merge_commit_sha: Option<&str>,
) -> Result<MergeObservedEvidence> {
    let base_sha = authoritative_base_sha(dir, base_ref).await?;
    ensure_head_object(dir, head_sha, Some(pr_number)).await?;
    observe_merge_evidence(dir, &base_sha, head_sha, merge_commit_sha).await
}

/// Observe the minimum immutable evidence needed to identify a published PR.
/// Unlike merge evidence, this does not require the branch to merge cleanly.
pub async fn observe_published_pr_evidence(
    dir: &str,
    base_ref: &str,
    head_sha: &str,
    pr_number: u64,
) -> Result<PublishedPrEvidence> {
    let base_sha = authoritative_base_sha(dir, base_ref).await?;
    ensure_head_object(dir, head_sha, Some(pr_number)).await?;
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
    Ok(PublishedPrEvidence {
        base_sha,
        patch_digest: format!("{:x}", Sha256::digest(&diff)),
    })
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
    merge_commit_sha: Option<&str>,
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
    let pr_head_tree_sha = commit_tree_sha(dir, head_sha).await?;
    let merge_commit_sha = merge_commit_sha
        .filter(|value| !value.trim().is_empty())
        .map(ToOwned::to_owned);
    let merge_commit_tree_sha = match merge_commit_sha.as_deref() {
        Some(commit_sha) => Some(commit_tree_sha(dir, commit_sha).await?),
        None => None,
    };
    Ok(MergeObservedEvidence {
        base_sha: base_sha.to_string(),
        head_sha: head_sha.to_string(),
        patch_digest: format!("{:x}", Sha256::digest(&diff)),
        merge_tree_sha: merge_tree_sha.to_string(),
        pr_head_tree_sha,
        merge_commit_sha,
        merge_commit_tree_sha,
    })
}

async fn commit_tree_sha(dir: &str, commit_sha: &str) -> Result<String> {
    let commit_object = format!("{commit_sha}^{{commit}}");
    run_git(dir, &["rev-parse", "--verify", &commit_object]).await?;
    let tree_object = format!("{commit_sha}^{{tree}}");
    run_git(dir, &["rev-parse", "--verify", &tree_object]).await
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

    #[tokio::test]
    async fn published_pr_evidence_uses_authoritative_base_and_binary_patch() {
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

        let repo = tempfile::tempdir().expect("repository");
        git(repo.path(), &["init", "-q"]);
        git(repo.path(), &["config", "user.email", "test@example.com"]);
        git(repo.path(), &["config", "user.name", "Test"]);
        git(repo.path(), &["branch", "-M", "main"]);
        std::fs::write(repo.path().join("base.txt"), "base\n").expect("write base");
        git(repo.path(), &["add", "base.txt"]);
        git(repo.path(), &["commit", "-qm", "base"]);
        let base = git(repo.path(), &["rev-parse", "HEAD"]);

        let remote = tempfile::tempdir().expect("remote");
        git(remote.path(), &["init", "--bare", "-q"]);
        git(
            repo.path(),
            &[
                "remote",
                "add",
                "origin",
                remote.path().to_str().expect("remote path"),
            ],
        );
        git(repo.path(), &["push", "-q", "origin", "main"]);
        git(repo.path(), &["switch", "-c", "feature"]);
        std::fs::write(repo.path().join("image.bin"), b"\x00\x10\xffbinary\n")
            .expect("write binary patch");
        git(repo.path(), &["add", "image.bin"]);
        git(repo.path(), &["commit", "-qm", "feature"]);
        let head = git(repo.path(), &["rev-parse", "HEAD"]);
        let range = format!("{base}...{head}");
        let diff = Command::new("git")
            .args(["diff", "--binary", "--no-ext-diff", &range])
            .current_dir(repo.path())
            .output()
            .expect("git diff should start");
        assert!(diff.status.success(), "git diff should succeed");

        let evidence = observe_published_pr_evidence(
            repo.path().to_str().expect("repository path"),
            "main",
            &head,
            17,
        )
        .await
        .expect("published PR evidence should be available");

        assert_eq!(evidence.base_sha, base);
        assert_eq!(
            evidence.patch_digest,
            format!("{:x}", Sha256::digest(&diff.stdout))
        );
    }

    #[test]
    fn compare_and_swap_checks_every_evidence_dimension() {
        let observed = MergeObservedEvidence {
            base_sha: "base".to_string(),
            head_sha: "head".to_string(),
            patch_digest: "patch".to_string(),
            merge_tree_sha: "tree".to_string(),
            pr_head_tree_sha: "pr-tree".to_string(),
            merge_commit_sha: None,
            merge_commit_tree_sha: None,
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
    async fn advanced_parent_has_distinct_prospective_squash_tree() {
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

        let repo = tempfile::tempdir().expect("repo");
        git(repo.path(), &["init", "-q"]);
        git(repo.path(), &["config", "user.email", "test@example.com"]);
        git(repo.path(), &["config", "user.name", "Test"]);
        git(repo.path(), &["branch", "-M", "main"]);
        std::fs::write(repo.path().join("base.txt"), "base\n").expect("write base");
        git(repo.path(), &["add", "base.txt"]);
        git(repo.path(), &["commit", "-qm", "base"]);
        let base = git(repo.path(), &["rev-parse", "HEAD"]);
        git(repo.path(), &["switch", "-c", "feature"]);
        std::fs::write(repo.path().join("feature.txt"), "feature\n").expect("write feature");
        git(repo.path(), &["add", "feature.txt"]);
        git(repo.path(), &["commit", "-qm", "feature"]);
        let pr_head = git(repo.path(), &["rev-parse", "HEAD"]);
        git(repo.path(), &["switch", "main"]);
        git(repo.path(), &["merge", "--squash", "feature"]);
        git(repo.path(), &["commit", "-qm", "squash"]);
        let merge_commit = git(repo.path(), &["rev-parse", "HEAD"]);
        let merge_commit_tree = git(
            repo.path(),
            &["rev-parse", &format!("{merge_commit}^{{tree}}")],
        );
        let historical_prospective = git(
            repo.path(),
            &["merge-tree", "--write-tree", &base, &pr_head],
        );
        std::fs::write(repo.path().join("later.txt"), "later parent change\n")
            .expect("write later parent");
        git(repo.path(), &["add", "later.txt"]);
        git(repo.path(), &["commit", "-qm", "advance parent"]);
        let advanced_parent = git(repo.path(), &["rev-parse", "HEAD"]);

        let observed = observe_merge_evidence(
            repo.path().to_str().expect("repo path"),
            &advanced_parent,
            &pr_head,
            Some(&merge_commit),
        )
        .await
        .expect("collector should observe squash evidence");

        assert_eq!(
            observed.merge_commit_tree_sha.as_deref(),
            Some(merge_commit_tree.as_str())
        );
        assert_eq!(historical_prospective, merge_commit_tree);
        assert_ne!(observed.merge_tree_sha, merge_commit_tree);
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
