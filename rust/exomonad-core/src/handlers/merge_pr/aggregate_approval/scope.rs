use super::receipt::ParentApprovalReceipt;
use crate::domain::AgentName;
use crate::services::agent_resolver::AgentResolver;
use anyhow::{Context, Result};
use exomonad_proto::effects::merge_pr::AggregateApprovalReference;
use serde_json::Value;

pub(super) fn validate_parent_scope(
    state: &Value,
    reference: &AggregateApprovalReference,
    receipt: &ParentApprovalReceipt,
    caller_agent_id: &str,
    caller_branch: &str,
) -> Result<()> {
    let run_id = string_at(state, "/run_id")?;
    let depth = state
        .get("depth")
        .and_then(Value::as_u64)
        .context("parent RunStore depth is missing or malformed")?;
    let expected_path_length = depth
        .checked_add(1)
        .and_then(|value| usize::try_from(value).ok())
        .context("parent RunStore depth is outside the supported range")?;
    let controller_epoch = string_at(state, "/controller_epoch")?;
    if run_id != reference.parent_run_id
        || expected_path_length != reference.parent_scope_path.len()
        || controller_epoch != reference.controller_epoch
        || receipt.parent_run_id != reference.parent_run_id
        || receipt.parent_scope_path != reference.parent_scope_path
        || receipt.candidate_key != reference.candidate_key
        || receipt.parent_agent_id != caller_agent_id
        || receipt.parent_branch != caller_branch
        || string_at(state, "/owner_branch")? != receipt.parent_branch
    {
        anyhow::bail!(
            "aggregate approval caller or parent scope does not match the current RunStore"
        );
    }
    Ok(())
}

pub(super) fn validate_repository(
    state: &Value,
    receipt: &ParentApprovalReceipt,
    current: &str,
) -> Result<()> {
    let stored = state
        .pointer("/repository_identity/remote_url")
        .and_then(Value::as_str)
        .context("parent RunStore has no canonical repository identity")?;
    if stored != receipt.repository_url || current != receipt.repository_url {
        anyhow::bail!(
            "aggregate approval repository identity differs from current repository evidence"
        );
    }
    Ok(())
}

pub(super) async fn validate_parent_identity(
    state: &Value,
    receipt: &ParentApprovalReceipt,
    caller_agent_id: &str,
    caller_branch: &str,
    resolver: &AgentResolver,
) -> Result<()> {
    let name = AgentName::try_from_str(caller_agent_id)
        .context("aggregate approval caller identity is malformed")?;
    let identity = resolver
        .get(&name)
        .await
        .context("aggregate approval caller has no durable agent identity")?;
    let manifest = state
        .get("plan_manifest")
        .and_then(Value::as_object)
        .context("parent RunStore has no accepted plan manifest")?;
    let manifest_scope = string_value(manifest.get("scope_id"), "plan manifest scope")?;
    let receipt_parent_scope = receipt
        .parent_manifest_scope_id
        .as_deref()
        .context("legacy aggregate approval must be renewed from the accepted manifest")?;
    if manifest_scope != receipt_parent_scope {
        anyhow::bail!("aggregate approval parent manifest scope differs from the accepted plan");
    }
    let is_root = manifest.get("role").and_then(Value::as_str) == Some("root");
    let identity_scope_matches = if is_root {
        identity.slice_id.is_none()
    } else {
        identity.slice_id.as_deref() == Some(manifest_scope)
    };
    if !identity.ledger_owned
        || identity.birth_branch.as_str() != receipt.parent_branch
        || caller_branch != receipt.parent_branch
        || !identity_scope_matches
    {
        anyhow::bail!(
            "aggregate approval caller is not the ledger-owned controller for this parent scope"
        );
    }
    Ok(())
}
pub(super) fn string_at<'a>(value: &'a Value, path: &str) -> Result<&'a str> {
    value
        .pointer(path)
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .with_context(|| format!("required aggregate approval coordinate {path} is missing"))
}

pub(super) fn string_value<'a>(value: Option<&'a Value>, name: &str) -> Result<&'a str> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .with_context(|| format!("required aggregate approval coordinate {name} is missing"))
}
