use super::receipt::{manifest_digest, valid_sha256, ParentApprovalReceipt};
use super::scope::string_value;
use super::{canonical_digest, escape_json_pointer, ACTIVE_MERGE_LIFECYCLES};
use anyhow::{Context, Result};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::path::{Component, Path, PathBuf};

pub(super) fn validate_plan_and_candidate(
    state: &Value,
    receipt: &ParentApprovalReceipt,
    pr_number: u64,
) -> Result<(String, String)> {
    let parent_manifest_scope_id = receipt
        .parent_manifest_scope_id
        .as_deref()
        .context("legacy aggregate approval must be renewed from the accepted manifest")?;
    let child_run_id = receipt
        .child_run_id
        .as_deref()
        .context("legacy aggregate approval has no canonical child run identity")?;
    let child_manifest_node_id = receipt
        .child_manifest_node_id
        .as_deref()
        .context("legacy aggregate approval has no canonical child node identity")?;
    let manifest = state
        .get("plan_manifest")
        .and_then(Value::as_object)
        .context("parent RunStore has no accepted plan manifest")?;
    let stored_manifest_digest = string_value(manifest.get("digest"), "plan manifest digest")?;
    if !valid_sha256(stored_manifest_digest)
        || manifest_digest(manifest)? != stored_manifest_digest
        || stored_manifest_digest != receipt.plan_manifest_digest
        || manifest.get("manifest_revision").and_then(Value::as_u64)
            != Some(receipt.plan_manifest_revision)
        || string_value(manifest.get("scope_id"), "plan manifest scope")?
            != parent_manifest_scope_id
    {
        anyhow::bail!("aggregate approval does not match the accepted plan manifest revision");
    }
    let role = string_value(manifest.get("role"), "plan manifest role")?;
    if !matches!(role, "root" | "non_root")
        || string_value(manifest.get("owned_branch"), "parent manifest branch")?
            != receipt.parent_branch
    {
        anyhow::bail!(
            "aggregate approval parent role or branch differs from the accepted manifest"
        );
    }
    let nodes = manifest
        .get("nodes")
        .and_then(Value::as_array)
        .context("accepted plan manifest has no node declarations")?;
    let node = nodes
        .iter()
        .filter_map(Value::as_object)
        .find(|node| {
            node.get("name").and_then(Value::as_str) == Some(child_run_id)
                && node.get("kind").and_then(Value::as_str) == Some("sub_tl")
        })
        .context("aggregate approval child is not a declared sub-TL")?;
    let expected_child_id = format!("{}/sub_tl/{}", parent_manifest_scope_id, child_run_id);
    if receipt.candidate_key != child_run_id
        || string_value(node.get("node_id"), "aggregate manifest node ID")?
            != child_manifest_node_id
        || child_manifest_node_id != expected_child_id
        || string_value(node.get("parent_id"), "aggregate manifest parent ID")?
            != parent_manifest_scope_id
    {
        anyhow::bail!("aggregate candidate key and canonical manifest declaration do not agree");
    }
    let contract = node
        .get("integration_contract")
        .and_then(Value::as_object)
        .context("aggregate plan node has no integration contract")?;
    if contract
        .get("aggregate_approval_mode")
        .is_some_and(|mode| mode.as_str() != Some("parent"))
    {
        anyhow::bail!("accepted plan does not authorize parent aggregate approval mode");
    }
    let aggregate_required = match contract.get("aggregate_pr_required") {
        None => true,
        Some(Value::Bool(required)) => *required,
        Some(_) => anyhow::bail!("accepted aggregate PR policy is malformed"),
    };
    let review_owner_is_unsupported = contract
        .get("aggregate_review_owner")
        .is_some_and(|owner| owner.as_str() != Some("none"));
    if !aggregate_required || review_owner_is_unsupported {
        anyhow::bail!("accepted plan does not authorize parent approval for this aggregate");
    }
    let head_branch = string_value(node.get("owned_branch"), "aggregate head branch")?.to_owned();
    let child_digest = string_value(node.get("child_manifest_digest"), "child manifest digest")?;
    if child_digest != receipt.child_manifest_digest {
        anyhow::bail!("aggregate approval child manifest differs from the accepted plan binding");
    }
    validate_child_manifest_binding(manifest, node, receipt)?;
    let policy = serde_json::json!({
        "approval_mode": "parent",
        "parent_manifest_digest": receipt.plan_manifest_digest,
        "parent_manifest_revision": receipt.plan_manifest_revision,
        "child_manifest_digest": child_digest,
        "integration_contract": contract,
    });
    if canonical_digest(&policy)? != receipt.approval_policy_digest {
        anyhow::bail!("aggregate approval policy digest differs from the accepted plan policy");
    }
    let child_id_pointer = escape_json_pointer(&receipt.candidate_key);
    let candidate = state
        .pointer(&format!("/integration/candidates/{child_id_pointer}"))
        .and_then(Value::as_object)
        .context("aggregate approval candidate is missing from the parent RunStore")?;
    if !ACTIVE_MERGE_LIFECYCLES.contains(
        &candidate
            .get("lifecycle")
            .and_then(Value::as_str)
            .unwrap_or_default(),
    ) || number_field(candidate, "aggregate_pr_number")? != receipt.pr_number
        || receipt.pr_number != pr_number
        || string_value(candidate.get("aggregate_head_sha"), "aggregate head SHA")?
            != receipt.head_sha
        || string_value(candidate.get("head_sha"), "validated head SHA")? != receipt.head_sha
        || string_value(
            candidate.get("aggregate_patch_digest"),
            "aggregate patch digest",
        )? != receipt.patch_digest
        || string_value(candidate.get("patch_digest"), "validated patch digest")?
            != receipt.patch_digest
        || string_value(candidate.get("validated_base_sha"), "validated base SHA")?
            != receipt.base_sha
        || string_value(candidate.get("merge_tree_sha"), "validated merge tree")?
            != receipt.merge_tree_sha
        || string_value(candidate.get("ci_status"), "validated CI status")? != receipt.ci_status
        || string_value(
            candidate.get("integration_owner_run_id"),
            "aggregate owner run",
        )? != child_run_id
        || string_value(
            candidate.get("integration_owner_branch"),
            "aggregate owner branch",
        )? != head_branch
    {
        anyhow::bail!("aggregate approval evidence differs from the active integration candidate");
    }
    let worktree = string_value(
        candidate.get("integration_owner_worktree"),
        "aggregate owner worktree",
    )?
    .to_owned();
    Ok((head_branch, worktree))
}

pub(super) fn resolve_aggregate_worktree(
    project_dir: &Path,
    recorded_path: &str,
) -> Result<PathBuf> {
    let root = project_dir
        .canonicalize()
        .context("aggregate project workspace is unavailable")?;
    let recorded = Path::new(recorded_path);
    if recorded.as_os_str().is_empty()
        || recorded
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        anyhow::bail!("aggregate owner worktree path is empty or escapes its workspace");
    }
    let joined = if recorded.is_absolute() {
        recorded.to_path_buf()
    } else {
        root.join(recorded)
    };
    let canonical = joined
        .canonicalize()
        .context("aggregate owner worktree is unavailable")?;
    if !canonical.starts_with(&root) || !canonical.is_dir() {
        anyhow::bail!("aggregate owner worktree is outside its declared workspace");
    }
    Ok(canonical)
}

fn validate_child_manifest_binding(
    parent: &Map<String, Value>,
    node: &Map<String, Value>,
    receipt: &ParentApprovalReceipt,
) -> Result<()> {
    let node_id = string_value(node.get("node_id"), "aggregate node ID")?;
    if parent
        .get("child_manifest_digests")
        .and_then(Value::as_object)
        .and_then(|digests| digests.get(node_id))
        .and_then(Value::as_str)
        != Some(receipt.child_manifest_digest.as_str())
    {
        anyhow::bail!("aggregate child digest is not indexed by the accepted parent manifest");
    }
    let child = parent
        .get("child_manifests")
        .and_then(Value::as_object)
        .and_then(|manifests| manifests.get(node_id))
        .and_then(Value::as_object)
        .context("accepted parent manifest is missing the child declaration")?;
    let child_manifest_scope_id = receipt
        .child_manifest_scope_id
        .as_deref()
        .context("legacy aggregate approval has no canonical child scope identity")?;
    let parent_manifest_scope_id = receipt
        .parent_manifest_scope_id
        .as_deref()
        .context("legacy aggregate approval has no canonical parent scope identity")?;
    if string_value(child.get("scope_id"), "child manifest scope")? != child_manifest_scope_id
        || string_value(child.get("parent_scope_id"), "child parent scope")?
            != parent_manifest_scope_id
        || string_value(child.get("owned_branch"), "child manifest branch")?
            != string_value(node.get("owned_branch"), "aggregate head branch")?
        || manifest_digest(child)? != receipt.child_manifest_digest
    {
        anyhow::bail!("aggregate child manifest is inconsistent with its accepted parent binding");
    }
    let child_leaf_ids = child
        .get("nodes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .filter(|leaf| leaf.get("kind").and_then(Value::as_str) == Some("leaf"))
        .filter_map(|leaf| leaf.get("node_id").and_then(Value::as_str))
        .collect::<BTreeSet<_>>();
    if receipt
        .reviewed_leaf_ids
        .iter()
        .any(|leaf| !child_leaf_ids.contains(leaf.as_str()))
    {
        anyhow::bail!("aggregate approval cites a leaf outside the accepted child manifest");
    }
    Ok(())
}

fn number_field(value: &Map<String, Value>, name: &str) -> Result<u64> {
    value
        .get(name)
        .and_then(Value::as_u64)
        .with_context(|| format!("required aggregate approval coordinate {name} is missing"))
}
