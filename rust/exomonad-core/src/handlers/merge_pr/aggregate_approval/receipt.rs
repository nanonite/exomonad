use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ParentApprovalReceipt {
    pub(super) receipt_id: String,
    pub(super) policy_mode: String,
    pub(super) parent_run_id: String,
    pub(super) parent_scope_path: Vec<String>,
    pub(super) parent_manifest_scope_id: Option<String>,
    pub(super) candidate_key: String,
    pub(super) child_run_id: Option<String>,
    pub(super) child_manifest_node_id: Option<String>,
    pub(super) child_manifest_scope_id: Option<String>,
    #[serde(default, skip_serializing)]
    child_scope_id: Option<String>,
    #[serde(skip)]
    pub(super) legacy_identity: bool,
    pub(super) parent_agent_id: String,
    pub(super) parent_branch: String,
    pub(super) repository_url: String,
    pub(super) plan_manifest_digest: String,
    pub(super) plan_manifest_revision: u64,
    pub(super) approval_policy_digest: String,
    pub(super) child_manifest_digest: String,
    pub(super) pr_number: u64,
    pub(super) head_sha: String,
    pub(super) head_tree_sha: String,
    pub(super) base_sha: String,
    pub(super) patch_digest: String,
    pub(super) merge_tree_sha: String,
    pub(super) ci_status: String,
    pub(super) readiness_receipt_digest: String,
    pub(super) lineage_proof_digest: String,
    pub(super) lineage_commit_shas: Vec<String>,
    pub(super) reviewed_leaf_ids: Vec<String>,
    pub(super) created_at: String,
    pub(super) supersedes_receipt_id: Option<String>,
}
pub(super) fn parse_receipt(value: &Value) -> Result<ParentApprovalReceipt> {
    let object = value
        .as_object()
        .context("aggregate approval receipt must be an object")?;
    let expected = [
        "receipt_id",
        "policy_mode",
        "parent_run_id",
        "parent_scope_path",
        "parent_manifest_scope_id",
        "candidate_key",
        "child_run_id",
        "child_manifest_node_id",
        "child_manifest_scope_id",
        "parent_agent_id",
        "parent_branch",
        "repository_url",
        "plan_manifest_digest",
        "plan_manifest_revision",
        "approval_policy_digest",
        "child_manifest_digest",
        "pr_number",
        "head_sha",
        "head_tree_sha",
        "base_sha",
        "patch_digest",
        "merge_tree_sha",
        "ci_status",
        "readiness_receipt_digest",
        "lineage_proof_digest",
        "lineage_commit_shas",
        "reviewed_leaf_ids",
        "created_at",
        "supersedes_receipt_id",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let legacy_expected = [
        "receipt_id",
        "policy_mode",
        "parent_run_id",
        "parent_scope_path",
        "child_scope_id",
        "parent_agent_id",
        "parent_branch",
        "repository_url",
        "plan_manifest_digest",
        "plan_manifest_revision",
        "approval_policy_digest",
        "child_manifest_digest",
        "pr_number",
        "head_sha",
        "head_tree_sha",
        "base_sha",
        "patch_digest",
        "merge_tree_sha",
        "ci_status",
        "readiness_receipt_digest",
        "lineage_proof_digest",
        "lineage_commit_shas",
        "reviewed_leaf_ids",
        "created_at",
        "supersedes_receipt_id",
    ]
    .into_iter()
    .collect::<BTreeSet<_>>();
    let actual = object.keys().map(String::as_str).collect::<BTreeSet<_>>();
    let legacy_identity = actual == legacy_expected;
    if actual != expected && !legacy_identity {
        anyhow::bail!("aggregate approval receipt has missing or unknown fields");
    }
    let mut normalized = value.clone();
    if legacy_identity {
        let normalized_object = normalized
            .as_object_mut()
            .context("legacy aggregate approval receipt is not an object")?;
        let child_scope_id = normalized_object
            .get("child_scope_id")
            .and_then(Value::as_str)
            .context("legacy aggregate approval child scope is malformed")?
            .to_string();
        normalized_object.insert("candidate_key".to_string(), Value::String(child_scope_id));
    }
    let mut receipt: ParentApprovalReceipt = serde_json::from_value(normalized)
        .context("aggregate approval receipt fields are malformed")?;
    receipt.legacy_identity = legacy_identity;
    validate_receipt(&receipt, value)?;
    Ok(receipt)
}

fn validate_receipt(receipt: &ParentApprovalReceipt, original: &Value) -> Result<()> {
    let text = [
        receipt.receipt_id.as_str(),
        receipt.parent_run_id.as_str(),
        receipt.parent_agent_id.as_str(),
        receipt.parent_branch.as_str(),
        receipt.repository_url.as_str(),
        receipt.plan_manifest_digest.as_str(),
        receipt.approval_policy_digest.as_str(),
        receipt.child_manifest_digest.as_str(),
        receipt.head_sha.as_str(),
        receipt.head_tree_sha.as_str(),
        receipt.base_sha.as_str(),
        receipt.patch_digest.as_str(),
        receipt.merge_tree_sha.as_str(),
        receipt.readiness_receipt_digest.as_str(),
        receipt.lineage_proof_digest.as_str(),
        receipt.created_at.as_str(),
    ];
    if text.iter().any(|value| value.trim().is_empty())
        || (!receipt.legacy_identity
            && [
                receipt.parent_manifest_scope_id.as_deref(),
                Some(receipt.candidate_key.as_str()),
                receipt.child_run_id.as_deref(),
                receipt.child_manifest_node_id.as_deref(),
                receipt.child_manifest_scope_id.as_deref(),
            ]
            .iter()
            .any(|value| match value {
                Some(text) => text.trim().is_empty(),
                None => true,
            }))
        || (receipt.legacy_identity
            && receipt
                .child_scope_id
                .as_deref()
                .is_none_or(|scope| scope.trim().is_empty()))
    {
        anyhow::bail!("aggregate approval receipt contains an empty required field");
    }
    if receipt.policy_mode != "parent"
        || receipt.plan_manifest_revision == 0
        || receipt.pr_number == 0
        || !matches!(receipt.ci_status.as_str(), "success" | "neutral")
        || receipt.lineage_commit_shas.is_empty()
        || receipt
            .lineage_commit_shas
            .iter()
            .any(|sha| !valid_git_sha(sha))
        || !is_sorted_unique(&receipt.lineage_commit_shas)
        || receipt
            .reviewed_leaf_ids
            .iter()
            .any(|leaf| leaf.trim().is_empty())
        || !is_sorted_unique(&receipt.reviewed_leaf_ids)
        || receipt.parent_scope_path.is_empty()
        || receipt.parent_scope_path.last() != Some(&receipt.parent_run_id)
        || (!receipt.legacy_identity
            && (receipt.child_run_id.as_deref() != Some(receipt.candidate_key.as_str())
                || receipt.child_manifest_node_id != receipt.child_manifest_scope_id))
    {
        anyhow::bail!("aggregate approval receipt violates its parent-mode evidence contract");
    }
    for (name, sha) in [
        ("head", receipt.head_sha.as_str()),
        ("head tree", receipt.head_tree_sha.as_str()),
        ("base", receipt.base_sha.as_str()),
        ("merge tree", receipt.merge_tree_sha.as_str()),
    ] {
        if !valid_git_sha(sha) {
            anyhow::bail!("aggregate approval {name} SHA is malformed");
        }
    }
    for (name, digest) in [
        ("receipt", receipt.receipt_id.as_str()),
        ("plan manifest", receipt.plan_manifest_digest.as_str()),
        ("approval policy", receipt.approval_policy_digest.as_str()),
        ("child manifest", receipt.child_manifest_digest.as_str()),
        ("patch", receipt.patch_digest.as_str()),
        ("readiness", receipt.readiness_receipt_digest.as_str()),
        ("lineage", receipt.lineage_proof_digest.as_str()),
    ] {
        if !valid_sha256(digest) {
            anyhow::bail!("aggregate approval {name} digest is malformed");
        }
    }
    if original.get("receipt_id").and_then(Value::as_str)
        != Some(canonical_receipt_id(original)?.as_str())
    {
        anyhow::bail!("aggregate approval receipt ID does not match its content");
    }
    Ok(())
}
pub(super) fn canonical_receipt_id(value: &Value) -> Result<String> {
    let mut identity = value
        .as_object()
        .cloned()
        .context("aggregate approval receipt is not an object")?;
    identity.remove("receipt_id");
    identity.remove("created_at");
    canonical_digest(&Value::Object(identity))
}

pub(super) fn canonical_digest(value: &Value) -> Result<String> {
    let mut bytes = Vec::new();
    write_python_canonical_json(value, &mut bytes)?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

pub(super) fn manifest_digest(manifest: &Map<String, Value>) -> Result<String> {
    let mut payload = manifest.clone();
    payload.remove("digest");
    canonical_digest(&Value::Object(payload))
}

fn write_python_canonical_json(value: &Value, output: &mut Vec<u8>) -> Result<()> {
    match value {
        Value::Null => output.extend_from_slice(b"null"),
        Value::Bool(true) => output.extend_from_slice(b"true"),
        Value::Bool(false) => output.extend_from_slice(b"false"),
        Value::Number(number) => output.extend_from_slice(number.to_string().as_bytes()),
        Value::String(value) => write_python_string(value, output),
        Value::Array(values) => {
            output.push(b'[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(b',');
                }
                write_python_canonical_json(value, output)?;
            }
            output.push(b']');
        }
        Value::Object(values) => write_python_object(values, output)?,
    }
    Ok(())
}

fn write_python_object(values: &Map<String, Value>, output: &mut Vec<u8>) -> Result<()> {
    let mut entries = values.iter().collect::<Vec<_>>();
    entries.sort_by(|left, right| left.0.cmp(right.0));
    output.push(b'{');
    for (index, (key, value)) in entries.into_iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        write_python_string(key, output);
        output.push(b':');
        write_python_canonical_json(value, output)?;
    }
    output.push(b'}');
    Ok(())
}

fn write_python_string(value: &str, output: &mut Vec<u8>) {
    output.push(b'"');
    for character in value.chars() {
        match character {
            '"' => output.extend_from_slice(b"\\\""),
            '\\' => output.extend_from_slice(b"\\\\"),
            '\u{0008}' => output.extend_from_slice(b"\\b"),
            '\u{000c}' => output.extend_from_slice(b"\\f"),
            '\n' => output.extend_from_slice(b"\\n"),
            '\r' => output.extend_from_slice(b"\\r"),
            '\t' => output.extend_from_slice(b"\\t"),
            value if value <= '\u{001f}' || value > '\u{007f}' => {
                write_python_unicode_escape(value, output)
            }
            value => {
                let mut buffer = [0u8; 4];
                output.extend_from_slice(value.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    output.push(b'"');
}

fn write_python_unicode_escape(value: char, output: &mut Vec<u8>) {
    let scalar = value as u32;
    if scalar <= 0xffff {
        write_hex_escape(scalar, output);
        return;
    }
    let adjusted = scalar - 0x1_0000;
    write_hex_escape(0xd800 + (adjusted >> 10), output);
    write_hex_escape(0xdc00 + (adjusted & 0x3ff), output);
}

fn write_hex_escape(value: u32, output: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    output.extend_from_slice(b"\\u");
    for shift in [12, 8, 4, 0] {
        output.push(HEX[((value >> shift) & 0xf) as usize]);
    }
}

pub(super) fn valid_git_sha(value: &str) -> bool {
    matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn is_sorted_unique(values: &[String]) -> bool {
    values.windows(2).all(|pair| pair[0] < pair[1])
}
