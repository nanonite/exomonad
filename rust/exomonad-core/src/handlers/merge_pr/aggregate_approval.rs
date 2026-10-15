use crate::services::agent_resolver::AgentResolver;
use anyhow::{Context, Result};
use exomonad_proto::effects::merge_pr::AggregateApprovalReference;
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

mod plan;
mod receipt;
mod scope;

use plan::{resolve_aggregate_worktree, validate_plan_and_candidate};
#[cfg(test)]
use receipt::canonical_receipt_id;
use receipt::{canonical_digest, parse_receipt, ParentApprovalReceipt};
use scope::{validate_parent_identity, validate_parent_scope, validate_repository};

const RUN_FILENAME: &str = "run.json";
const ACTIVE_MERGE_LIFECYCLES: &[&str] = &["INTEGRATION_VALIDATED", "MERGING"];
#[derive(Debug, Clone)]
pub(crate) struct VerifiedParentApproval {
    pub reference: AggregateApprovalReference,
    pub receipt_id: String,
    pub repository_url: String,
    pub aggregate_worktree: PathBuf,
    pub parent_branch: String,
    pub head_branch: String,
    pub pr_number: u64,
    pub head_sha: String,
    pub head_tree_sha: String,
    pub base_sha: String,
    pub patch_digest: String,
    pub merge_tree_sha: String,
    pub ci_status: String,
    pub lineage_proof_digest: String,
    pub lineage_commit_shas: Vec<String>,
}
pub(crate) async fn validate_parent_approval(
    project_dir: &Path,
    reference: &AggregateApprovalReference,
    pr_number: u64,
    current_repository_url: &str,
    caller_agent_id: &str,
    caller_branch: &str,
    resolver: &AgentResolver,
) -> Result<VerifiedParentApproval> {
    let scope_path = validate_reference(reference)?;
    let state = read_scope_checkpoint(project_dir, &scope_path)?;
    let receipt = active_receipt(&state, reference)?;
    validate_parent_scope(&state, reference, &receipt, caller_agent_id, caller_branch)?;
    validate_repository(&state, &receipt, current_repository_url)?;
    validate_parent_identity(&state, &receipt, caller_agent_id, caller_branch, resolver).await?;
    let (head_branch, worktree_path) = validate_plan_and_candidate(&state, &receipt, pr_number)?;
    let aggregate_worktree = resolve_aggregate_worktree(project_dir, &worktree_path)?;
    Ok(VerifiedParentApproval {
        reference: reference.clone(),
        receipt_id: receipt.receipt_id,
        repository_url: receipt.repository_url,
        aggregate_worktree,
        parent_branch: receipt.parent_branch,
        head_branch,
        pr_number: receipt.pr_number,
        head_sha: receipt.head_sha,
        head_tree_sha: receipt.head_tree_sha,
        base_sha: receipt.base_sha,
        patch_digest: receipt.patch_digest,
        merge_tree_sha: receipt.merge_tree_sha,
        ci_status: receipt.ci_status,
        lineage_proof_digest: receipt.lineage_proof_digest,
        lineage_commit_shas: receipt.lineage_commit_shas,
    })
}

pub(super) fn validate_request_compare(
    approval: Option<&VerifiedParentApproval>,
    expected_base: &str,
    expected_head: &str,
    expected_patch: &str,
    expected_merge_tree: &str,
) -> Result<()> {
    let Some(approval) = approval else {
        return Ok(());
    };
    for (field, supplied, accepted) in [
        ("base", expected_base, approval.base_sha.as_str()),
        ("head", expected_head, approval.head_sha.as_str()),
        (
            "patch digest",
            expected_patch,
            approval.patch_digest.as_str(),
        ),
        (
            "merge tree",
            expected_merge_tree,
            approval.merge_tree_sha.as_str(),
        ),
    ] {
        if supplied.trim().is_empty() || supplied != accepted {
            anyhow::bail!("aggregate approval request {field} does not match its durable receipt");
        }
    }
    Ok(())
}

pub(super) fn reject_unreferenced_aggregate(
    project_dir: &Path,
    pr_number: u64,
    current_repository_url: &str,
) -> Result<()> {
    let loop_root = project_dir.join(".exo/tl-loop");
    if !loop_root.exists() {
        return Ok(());
    }
    let mut pending = vec![(loop_root, 0usize)];
    let mut visited = 0usize;
    while let Some((directory, depth)) = pending.pop() {
        if depth > 32 || visited > 20_000 {
            anyhow::bail!("could not classify merge: TL scope scan exceeded its safety bound");
        }
        visited += 1;
        inspect_unreferenced_candidate(
            &directory.join(RUN_FILENAME),
            pr_number,
            current_repository_url,
        )?;
        for entry in fs::read_dir(&directory).with_context(|| {
            format!(
                "could not inspect TL scope directory {}",
                directory.display()
            )
        })? {
            let entry = entry?;
            let metadata = entry.file_type()?;
            if metadata.is_dir() {
                pending.push((entry.path(), depth + 1));
            }
        }
    }
    Ok(())
}

fn inspect_unreferenced_candidate(
    checkpoint_path: &Path,
    pr_number: u64,
    current_repository_url: &str,
) -> Result<()> {
    if !checkpoint_path.exists() {
        return Ok(());
    }
    let checkpoint = read_json(checkpoint_path)?;
    let Some(manifest) = checkpoint.get("plan_manifest").and_then(Value::as_object) else {
        return Ok(());
    };
    let aggregate_nodes = manifest
        .get("nodes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|node| {
            let object = node.as_object()?;
            (object.get("kind").and_then(Value::as_str) == Some("sub_tl"))
                .then(|| object.get("name").and_then(Value::as_str))
                .flatten()
        })
        .collect::<BTreeSet<_>>();
    if aggregate_nodes.is_empty() {
        return Ok(());
    }
    let same_repository = checkpoint
        .pointer("/repository_identity/remote_url")
        .and_then(Value::as_str);
    let candidates = checkpoint
        .pointer("/integration/candidates")
        .and_then(Value::as_object);
    for (scope_id, candidate) in candidates.into_iter().flatten() {
        if !aggregate_nodes.contains(scope_id.as_str())
            || candidate.get("aggregate_pr_number").and_then(Value::as_u64) != Some(pr_number)
        {
            continue;
        }
        if same_repository.is_some_and(|repository| repository != current_repository_url) {
            continue;
        }
        anyhow::bail!(
            "aggregate PR #{pr_number} requires its active parent-approval receipt reference"
        );
    }
    Ok(())
}
fn validate_reference(reference: &AggregateApprovalReference) -> Result<Vec<String>> {
    let path = &reference.parent_scope_path;
    if reference.receipt_id.trim().is_empty()
        || reference.parent_run_id.trim().is_empty()
        || reference.child_scope_id.trim().is_empty()
        || reference.controller_epoch.trim().is_empty()
        || path.is_empty()
        || path.last().map(String::as_str) != Some(reference.parent_run_id.as_str())
    {
        anyhow::bail!(
            "aggregate approval reference is incomplete or has inconsistent scope coordinates"
        );
    }
    for segment in path {
        if segment.is_empty()
            || segment.contains(['/', '\\'])
            || matches!(segment.as_str(), "." | "..")
        {
            anyhow::bail!("aggregate approval reference contains an unsafe scope path segment");
        }
    }
    Ok(path.clone())
}

fn read_scope_checkpoint(project_dir: &Path, scope_path: &[String]) -> Result<Value> {
    let loop_root = project_dir
        .join(".exo/tl-loop")
        .canonicalize()
        .context("aggregate approval parent TL state is unavailable")?;
    let mut path = loop_root.clone();
    for segment in scope_path {
        path.push(segment);
    }
    let scope_dir = path
        .canonicalize()
        .context("aggregate approval parent scope checkpoint is unavailable")?;
    if !scope_dir.starts_with(&loop_root) {
        anyhow::bail!("aggregate approval scope path escapes the TL state directory");
    }
    read_json(&scope_dir.join(RUN_FILENAME))
}

fn read_json(path: &Path) -> Result<Value> {
    let metadata = fs::symlink_metadata(path)
        .with_context(|| format!("could not inspect checkpoint {}", path.display()))?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        anyhow::bail!("TL checkpoint is not a regular file");
    }
    let content =
        fs::read(path).with_context(|| format!("could not read checkpoint {}", path.display()))?;
    serde_json::from_slice(&content)
        .with_context(|| format!("TL checkpoint {} is malformed", path.display()))
}
fn active_receipt(
    state: &Value,
    reference: &AggregateApprovalReference,
) -> Result<ParentApprovalReceipt> {
    let candidate = state
        .pointer(&format!(
            "/integration/candidates/{}",
            escape_json_pointer(&reference.child_scope_id)
        ))
        .and_then(Value::as_object)
        .context("aggregate approval candidate is absent from its parent RunStore")?;
    if candidate
        .get("active_aggregate_approval_id")
        .and_then(Value::as_str)
        != Some(reference.receipt_id.as_str())
    {
        anyhow::bail!("aggregate approval reference is not the active parent decision");
    }
    let history = candidate
        .get("aggregate_approval_receipts")
        .and_then(Value::as_array)
        .context("aggregate approval history is missing or malformed")?;
    if history.is_empty() {
        anyhow::bail!("aggregate approval history is empty");
    }
    let mut prior_id: Option<String> = None;
    let mut selected = None;
    for (index, value) in history.iter().enumerate() {
        let receipt = parse_receipt(value)?;
        if receipt.child_scope_id != reference.child_scope_id
            || receipt.supersedes_receipt_id != prior_id
        {
            anyhow::bail!("aggregate approval history has a broken scope or supersession link");
        }
        prior_id = Some(receipt.receipt_id.clone());
        if receipt.receipt_id == reference.receipt_id {
            if index + 1 != history.len() {
                anyhow::bail!("aggregate approval receipt was superseded by a later decision");
            }
            selected = Some(receipt);
        }
    }
    selected.context("active aggregate approval receipt is not retained in history")
}
pub(super) fn escape_json_pointer(value: &str) -> String {
    value.replace('~', "~0").replace('/', "~1")
}
#[cfg(test)]
mod tests;
