//! Host-side verification of the aggregate Git proof accepted by the TL loop.

use anyhow::{Context, Result};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::Path;
use std::process::{Output, Stdio};
use std::time::Duration;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const GIT_TIMEOUT: Duration = Duration::from_secs(10);
const LINEAGE_TIMEOUT: Duration = Duration::from_secs(60);
const MAX_LINEAGE_COMMITS: usize = 256;
const MAX_AGGREGATE_COMMITS: usize = 2_000;

#[derive(serde::Serialize)]
struct CommitRelation {
    aggregate_commit: String,
    receipt_commit: String,
    relation: &'static str,
}

/// Recompute the exact Python aggregate-lineage proof from the owner's Git objects.
pub(crate) async fn verify_parent_aggregate_lineage(
    repository: &Path,
    aggregate_head_sha: &str,
    expected_tree_sha: &str,
    receipt_commits: &[String],
    expected_proof_digest: &str,
) -> Result<()> {
    tokio::time::timeout(
        LINEAGE_TIMEOUT,
        verify_parent_aggregate_lineage_inner(
            repository,
            aggregate_head_sha,
            expected_tree_sha,
            receipt_commits,
            expected_proof_digest,
        ),
    )
    .await
    .context("aggregate Git lineage verification exceeded its 60-second bound")?
}

async fn verify_parent_aggregate_lineage_inner(
    repository: &Path,
    aggregate_head_sha: &str,
    expected_tree_sha: &str,
    receipt_commits: &[String],
    expected_proof_digest: &str,
) -> Result<()> {
    validate_object_id(aggregate_head_sha, "aggregate head")?;
    validate_object_id(expected_tree_sha, "aggregate head tree")?;
    validate_digest(expected_proof_digest)?;
    if receipt_commits.is_empty() || receipt_commits.len() > MAX_LINEAGE_COMMITS {
        anyhow::bail!("aggregate approval has missing or excessive lineage commits");
    }
    let root = successful_text(repository, &["rev-parse", "--show-toplevel"])
        .await
        .context("aggregate owner worktree is not a Git repository")?;
    if root.is_empty() {
        anyhow::bail!("aggregate owner worktree is not a Git repository");
    }
    let resolved_head = successful_text(
        repository,
        &[
            "rev-parse",
            "--verify",
            &format!("{aggregate_head_sha}^{{commit}}"),
        ],
    )
    .await
    .context("aggregate head is unavailable in its owner worktree")?;
    if resolved_head != aggregate_head_sha {
        anyhow::bail!("Git resolved a different aggregate head than the approval receipt");
    }
    let tree_sha = successful_text(
        repository,
        &[
            "rev-parse",
            "--verify",
            &format!("{aggregate_head_sha}^{{tree}}"),
        ],
    )
    .await
    .context("aggregate head tree is unavailable")?;
    if tree_sha != expected_tree_sha {
        anyhow::bail!("aggregate Git tree differs from the approved watcher tree");
    }

    let commits = receipt_commits
        .iter()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let mut relations = Vec::with_capacity(commits.len());
    for receipt_commit in commits {
        validate_object_id(receipt_commit, "completed child commit")?;
        let resolved_commit = successful_text(
            repository,
            &[
                "rev-parse",
                "--verify",
                &format!("{receipt_commit}^{{commit}}"),
            ],
        )
        .await
        .with_context(|| format!("completed child commit {receipt_commit} is unavailable"))?;
        if resolved_commit != receipt_commit {
            anyhow::bail!("Git resolved a different completed child commit");
        }
        let relation =
            verify_commit_relation(repository, receipt_commit, aggregate_head_sha).await?;
        relations.push(CommitRelation {
            aggregate_commit: relation.0,
            receipt_commit: receipt_commit.to_string(),
            relation: relation.1,
        });
    }

    let proof = json!({
        "aggregate_head_sha": aggregate_head_sha,
        "aggregate_tree_sha": tree_sha,
        "relations": relations,
    });
    let proof_bytes =
        serde_json::to_vec(&proof).context("could not encode aggregate lineage proof")?;
    let observed_digest = format!("{:x}", Sha256::digest(proof_bytes));
    if observed_digest != expected_proof_digest {
        anyhow::bail!("aggregate Git lineage proof differs from the durable approval receipt");
    }
    Ok(())
}

async fn verify_commit_relation(
    repository: &Path,
    receipt_commit: &str,
    aggregate_head: &str,
) -> Result<(String, &'static str)> {
    let ancestry = run_git(
        repository,
        &[
            "merge-base",
            "--is-ancestor",
            receipt_commit,
            aggregate_head,
        ],
    )
    .await?;
    if ancestry.status.success() {
        return Ok((receipt_commit.to_string(), "ancestor"));
    }
    if ancestry.status.code() != Some(1) {
        anyhow::bail!("Git could not determine aggregate commit ancestry");
    }
    let aggregate_commit =
        find_patch_equivalent_commit(repository, receipt_commit, aggregate_head).await?;
    Ok((aggregate_commit, "patch_equivalent_rebase"))
}

async fn find_patch_equivalent_commit(
    repository: &Path,
    receipt_commit: &str,
    aggregate_head: &str,
) -> Result<String> {
    let source_parent = successful_text(
        repository,
        &["rev-parse", "--verify", &format!("{receipt_commit}^1")],
    )
    .await
    .context("rebased child commit's source parent is unavailable")?;
    let common_base = successful_text(repository, &["merge-base", &source_parent, aggregate_head])
        .await
        .context("child and aggregate histories have no available common base")?;
    let source_patch = patch_id(repository, &source_parent, receipt_commit)
        .await?
        .context("completed child commit has no verifiable patch")?;
    let count = successful_text(
        repository,
        &[
            "rev-list",
            "--count",
            &format!("{common_base}..{aggregate_head}"),
        ],
    )
    .await
    .context("aggregate history could not be enumerated")?
    .parse::<usize>()
    .context("aggregate history count is malformed")?;
    if count > MAX_AGGREGATE_COMMITS {
        anyhow::bail!("aggregate history exceeds the bounded rebase verification limit");
    }
    let history = successful_text(
        repository,
        &[
            "rev-list",
            &format!("--max-count={}", MAX_AGGREGATE_COMMITS + 1),
            &format!("{common_base}..{aggregate_head}"),
        ],
    )
    .await
    .context("aggregate history could not be read")?;
    let commits = history
        .lines()
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>();
    if commits.len() != count {
        anyhow::bail!("aggregate history changed during lineage verification");
    }
    let mut matches = Vec::new();
    for commit in commits {
        if patch_id(
            repository,
            &commit_parent(repository, commit).await?,
            commit,
        )
        .await?
        .as_deref()
            == Some(source_patch.as_str())
        {
            matches.push(commit);
        }
        if matches.len() > 1 {
            anyhow::bail!("aggregate history contains ambiguous patch-equivalent child commits");
        }
    }
    matches
        .first()
        .map(|commit| (*commit).to_string())
        .context("aggregate history does not contain the completed child patch")
}

async fn commit_parent(repository: &Path, commit: &str) -> Result<String> {
    successful_text(
        repository,
        &["rev-parse", "--verify", &format!("{commit}^1")],
    )
    .await
    .context("aggregate commit parent is unavailable")
}

async fn patch_id(repository: &Path, parent: &str, commit: &str) -> Result<Option<String>> {
    let patch = run_git(
        repository,
        &["diff", "--binary", "--no-ext-diff", parent, commit],
    )
    .await?;
    if !patch.status.success() {
        anyhow::bail!("Git could not read a commit patch for lineage verification");
    }
    let mut child = Command::new("git")
        .arg("-C")
        .arg(repository)
        .args(["patch-id", "--stable"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .context("Git patch-id could not start")?;
    let mut stdin = child
        .stdin
        .take()
        .context("Git patch-id has no input pipe")?;
    tokio::time::timeout(GIT_TIMEOUT, stdin.write_all(&patch.stdout))
        .await
        .context("Git patch-id input timed out")?
        .context("Git patch-id input failed")?;
    drop(stdin);
    let output = tokio::time::timeout(GIT_TIMEOUT, child.wait_with_output())
        .await
        .context("Git patch-id timed out")?
        .context("Git patch-id could not finish")?;
    if !output.status.success() {
        anyhow::bail!("Git patch-id failed during lineage verification");
    }
    let output_text = String::from_utf8_lossy(&output.stdout);
    let Some(identity) = output_text
        .lines()
        .find_map(|line| line.split_whitespace().next())
    else {
        return Ok(None);
    };
    if identity.len() != 40 || !identity.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        anyhow::bail!("Git returned a malformed patch identity");
    }
    Ok(Some(identity.to_string()))
}

async fn successful_text(repository: &Path, arguments: &[&str]) -> Result<String> {
    let output = run_git(repository, arguments).await?;
    if !output.status.success() {
        anyhow::bail!("Git command failed during aggregate lineage verification");
    }
    String::from_utf8(output.stdout)
        .context("Git returned non-UTF-8 aggregate lineage evidence")
        .map(|value| value.trim().to_string())
}

async fn run_git(repository: &Path, arguments: &[&str]) -> Result<Output> {
    tokio::time::timeout(
        GIT_TIMEOUT,
        Command::new("git")
            .arg("-C")
            .arg(repository)
            .args(arguments)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .context("Git lineage command timed out")?
    .context("Git lineage command could not start")
}

fn validate_object_id(value: &str, label: &str) -> Result<()> {
    if !matches!(value.len(), 40 | 64)
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        anyhow::bail!("{label} is not a full lowercase Git object ID");
    }
    Ok(())
}

fn validate_digest(value: &str) -> Result<()> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        anyhow::bail!("aggregate lineage proof digest is malformed");
    }
    Ok(())
}

#[cfg(test)]
mod tests;
