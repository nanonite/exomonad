use crate::{clean, uds_client};
use anyhow::{Context, Result};
use exomonad::config::{
    Config, EffortLevel, ResolvedEffort, REVIEWER_EFFORT_ENV, REVIEWER_MAX_ROUNDS_ENV,
    REVIEWER_MODEL_ENV, TL_PREFLIGHT_RUNTIME_PATHS_ENV,
};
use exomonad_core::services::runtime_manifest::{
    PUBLICATION_REGISTRY_SCHEMA_VERSION, RUNTIME_PROTOCOL_VERSION,
};
use exomonad_core::services::{
    agent_control::{read_invocation_conservatively, InvocationRecord},
    pr_registry::{
        invocation_succession_reaches_current, read_published_heads,
        remove_published_heads_for_prs, PublishedHead,
    },
    repo::{get_repo_info, RepoInfo},
    AgentType, ForgejoClient, GitWorktreeService,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, info, warn};

/// Explicit lifecycle choice for an `exomonad init` invocation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SessionMode {
    Start,
    Continue,
    Recreate,
}

impl SessionMode {
    pub(crate) fn resolve(start: bool, cont: bool, recreate: bool) -> Result<Self> {
        let selected = [
            (start, "--start"),
            (cont, "--continue"),
            (recreate, "--recreate"),
        ]
        .into_iter()
        .filter_map(|(enabled, name)| enabled.then_some(name))
        .collect::<Vec<_>>();
        match selected.as_slice() {
            [] => Ok(Self::Continue),
            [name] => match *name {
                "--start" => Ok(Self::Start),
                "--continue" => Ok(Self::Continue),
                "--recreate" => Ok(Self::Recreate),
                _ => unreachable!(),
            },
            names => anyhow::bail!(
                "session modes are mutually exclusive; choose exactly one of {}",
                names.join(", ")
            ),
        }
    }

    pub(crate) fn is_recreate(self) -> bool {
        matches!(self, Self::Recreate)
    }

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Start => "start",
            Self::Continue => "continue",
            Self::Recreate => "recreate",
        }
    }
}

/// The durable decision made for an existing agent when a session continues.
/// The invocation ID is never rewritten by this classifier; a new ID is only
/// created later if the controller explicitly recreates the invocation.
#[derive(Debug, Clone, PartialEq, Eq)]
enum AgentContinuation {
    Preserve { invocation_id: String },
    Recreate { reason: &'static str },
}

impl AgentContinuation {
    fn classification(&self) -> &'static str {
        match self {
            Self::Preserve { .. } => "preserve",
            Self::Recreate { .. } => "recreate",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProtectedPr {
    number: u64,
    reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OrderedBranchSpec {
    branch: String,
    parent_branch: String,
    agent_name: String,
    slice_id: String,
    identity_worktree: PathBuf,
    worktree: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum OrderedIdentityState {
    Missing,
    Matching,
    Mismatched,
}

impl OrderedIdentityState {
    fn label(&self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Matching => "matching",
            Self::Mismatched => "mismatched",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OrderedBranchObservation {
    branch_exists: bool,
    agent_dir_exists: bool,
    identity: OrderedIdentityState,
    worktree_exists: bool,
    worktree_branch: Option<String>,
    attached_worktree: Option<PathBuf>,
    dirty_worktree: bool,
    live_invocation: bool,
    unique_commits: Option<u64>,
    publications: Vec<u64>,
    protected_publications: Vec<u64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum OrderedBranchAction {
    Remove,
    Preserve(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct OrderedBranchCleanup {
    spec: OrderedBranchSpec,
    observation: OrderedBranchObservation,
    action: OrderedBranchAction,
}

impl OrderedBranchCleanup {
    fn gate(&self) -> Option<&str> {
        match &self.action {
            OrderedBranchAction::Remove => None,
            OrderedBranchAction::Preserve(reason) => Some(reason),
        }
    }

    fn render(&self) -> String {
        let observation = &self.observation;
        let unique_commits = observation
            .unique_commits
            .map_or_else(|| "unknown".to_owned(), |count| count.to_string());
        let publications = if observation.publications.is_empty() {
            "none".to_owned()
        } else {
            observation
                .publications
                .iter()
                .map(|number| {
                    if observation.protected_publications.contains(number) {
                        format!("#{number} [PROTECTED]")
                    } else {
                        format!("#{number}")
                    }
                })
                .collect::<Vec<_>>()
                .join(", ")
        };
        let action = match &self.action {
            OrderedBranchAction::Remove => "remove".to_owned(),
            OrderedBranchAction::Preserve(reason) => format!("preserve [GATE: {reason}]"),
        };
        format!(
            "  {} -> {} (parent={}, identity={}, worktree={}, unique_commits={}, publication={})",
            self.spec.branch,
            action,
            self.spec.parent_branch,
            observation.identity.label(),
            if observation.worktree_exists {
                "present"
            } else {
                "absent"
            },
            unique_commits,
            publications,
        )
    }
}

/// Disposal decision for one issue-owned leaf branch published outside the
/// ordered sub-TL tree.
#[derive(Debug, Clone, PartialEq, Eq)]
struct LeafBranchCleanup {
    branch: String,
    base_branch: String,
    head_sha: String,
    pr_number: u64,
    agent: Option<String>,
    remote_name: String,
    worktree: Option<PathBuf>,
    local_head: Option<String>,
    remote_head: Option<String>,
    unmerged_commits: Option<u64>,
    dirty: bool,
    live: bool,
    protected: bool,
    action: OrderedBranchAction,
}

impl LeafBranchCleanup {
    fn gate(&self) -> Option<&str> {
        match &self.action {
            OrderedBranchAction::Remove => None,
            OrderedBranchAction::Preserve(reason) => Some(reason),
        }
    }

    fn render(&self) -> String {
        let action = match &self.action {
            OrderedBranchAction::Remove => {
                let preserve_action = match self.unmerged_commits {
                    Some(0) => "no unmerged commits".to_owned(),
                    Some(1) => "preserve 1 unmerged commit".to_owned(),
                    Some(count) => format!("preserve {count} unmerged commits"),
                    None => {
                        "unmerged commit count unknown; preservation will be verified".to_owned()
                    }
                };
                let remote_action = self.remote_head.as_ref().map_or_else(
                    || "no remote ref".to_owned(),
                    |sha| {
                        format!(
                            "delete {}/{} with lease {sha}",
                            self.remote_name, self.branch
                        )
                    },
                );
                format!("remove [{preserve_action}, {remote_action}]")
            }
            OrderedBranchAction::Preserve(reason) => format!("preserve [GATE: {reason}]"),
        };
        format!(
            "  {} -> {} (pr=#{}, head={}, local_head={}, remote={}/{}, remote_head={}, worktree={}, dirty={}, protected={})",
            self.branch,
            action,
            self.pr_number,
            self.head_sha,
            self.local_head.as_deref().unwrap_or("absent"),
            self.remote_name,
            self.branch,
            self.remote_head.as_deref().unwrap_or("absent"),
            self.worktree
                .as_ref()
                .map_or_else(|| "none".to_owned(), |path| path.display().to_string()),
            self.dirty,
            self.protected,
        )
    }
}

#[allow(clippy::too_many_arguments)]
fn leaf_branch_action(
    head_sha: &str,
    local_head: Option<&str>,
    remote_head: Option<&str>,
    worktree: Option<&Path>,
    dirty: bool,
    live: bool,
    protected: bool,
    project_dir: &Path,
) -> OrderedBranchAction {
    if live {
        return OrderedBranchAction::Preserve("live leaf invocation".to_owned());
    }
    if protected {
        return OrderedBranchAction::Preserve("protected publication".to_owned());
    }
    if dirty {
        return OrderedBranchAction::Preserve("dirty leaf worktree".to_owned());
    }
    if let Some(path) = worktree {
        if !path.starts_with(project_dir) {
            return OrderedBranchAction::Preserve(
                "leaf worktree is outside the project".to_owned(),
            );
        }
    }
    // A same-name branch is never proof of ownership: every present ref must
    // equal the recorded publication head before the branch may be disposed.
    if let Some(head) = local_head {
        if head != head_sha {
            return OrderedBranchAction::Preserve(
                "local branch head does not match the recorded publication head".to_owned(),
            );
        }
    }
    if let Some(head) = remote_head {
        if head != head_sha {
            return OrderedBranchAction::Preserve(
                "remote branch head does not match the recorded publication head".to_owned(),
            );
        }
    }
    OrderedBranchAction::Remove
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct RecreatePlan {
    worktrees: Vec<PathBuf>,
    ordered_branches: Vec<OrderedBranchCleanup>,
    leaf_branches: Vec<LeafBranchCleanup>,
    prs_to_close: Vec<u64>,
    prs_to_remove: Vec<u64>,
    protected: Vec<ProtectedPr>,
    dirty_worktrees: Vec<PathBuf>,
}

impl RecreatePlan {
    fn render(&self) -> String {
        let mut output = String::from("ExoMonad --recreate destruction plan:");
        output.push_str("\nWorktrees:");
        if self.worktrees.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for path in &self.worktrees {
                let dirty = self.dirty_worktrees.iter().any(|item| item == path);
                output.push_str(&format!(
                    "\n  {}{}",
                    path.display(),
                    if dirty { " [DIRTY]" } else { "" }
                ));
            }
        }
        output.push_str("\nPRs to close:");
        if self.prs_to_close.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for number in &self.prs_to_close {
                output.push_str(&format!("\n  #{number}"));
            }
        }
        output.push_str("\nPublication records to remove:");
        if self.prs_to_remove.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for number in &self.prs_to_remove {
                output.push_str(&format!("\n  #{number}"));
            }
        }
        output.push_str("\nOrdered controller branches:");
        if self.ordered_branches.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for branch in &self.ordered_branches {
                output.push('\n');
                output.push_str(&branch.render());
            }
        }
        output.push_str("\nIssue-owned leaf branches:");
        if self.leaf_branches.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for branch in &self.leaf_branches {
                output.push('\n');
                output.push_str(&branch.render());
            }
        }
        output.push_str("\nProtected PRs:");
        if self.protected.is_empty() {
            output.push_str("\n  (none)");
        } else {
            for protected in &self.protected {
                output.push_str(&format!("\n  #{} ({})", protected.number, protected.reason));
            }
        }
        output
    }
}

fn ordered_branch_action(
    spec: OrderedBranchSpec,
    observation: OrderedBranchObservation,
    authorized_publications: &[u64],
) -> Option<OrderedBranchCleanup> {
    let has_evidence = observation.branch_exists
        || observation.agent_dir_exists
        || observation.worktree_exists
        || !observation.publications.is_empty();
    if !has_evidence {
        return None;
    }

    let preserve = |reason: &str| OrderedBranchCleanup {
        spec: spec.clone(),
        observation: observation.clone(),
        action: OrderedBranchAction::Preserve(reason.to_owned()),
    };
    if observation.live_invocation {
        return Some(preserve("live ordered controller invocation"));
    }
    if observation.dirty_worktree {
        return Some(preserve("dirty ordered controller worktree"));
    }
    if observation.identity == OrderedIdentityState::Mismatched {
        return Some(preserve(
            "durable identity does not match the same-plan owner",
        ));
    }
    // Crash residue: a prior disposal removed the branch before the durable
    // identity. The branch is already gone, so completing the interrupted
    // cleanup (removing the matching identity) is the only idempotent action.
    if !observation.branch_exists
        && observation.identity == OrderedIdentityState::Matching
        && !observation.worktree_exists
        && observation.attached_worktree.is_none()
        && observation.publications.is_empty()
    {
        return Some(OrderedBranchCleanup {
            spec,
            observation,
            action: OrderedBranchAction::Remove,
        });
    }
    if observation.agent_dir_exists && observation.identity == OrderedIdentityState::Missing {
        return Some(preserve("agent metadata exists without a durable identity"));
    }
    if observation.worktree_exists && observation.identity == OrderedIdentityState::Missing {
        return Some(preserve(
            "ordered worktree exists without a durable identity",
        ));
    }
    if observation.worktree_exists && observation.worktree_branch.as_deref() != Some(&spec.branch) {
        return Some(preserve(
            "ordered worktree is not checked out on its owned branch",
        ));
    }
    if observation.attached_worktree.is_some() && !observation.worktree_exists {
        return Some(preserve(
            "git still records an ordered worktree whose path is missing",
        ));
    }
    if observation.attached_worktree.is_some()
        && observation.attached_worktree.as_deref() != Some(spec.worktree.as_path())
    {
        return Some(preserve(
            "owned branch is attached to an unexpected worktree",
        ));
    }
    if observation.unique_commits != Some(0) {
        return Some(preserve(
            "ordered branch base or unique commits cannot be verified",
        ));
    }
    if observation
        .publications
        .iter()
        .any(|number| !authorized_publications.contains(number))
    {
        return Some(preserve(
            "publication evidence is not scheduled for disposal by this plan",
        ));
    }

    Some(OrderedBranchCleanup {
        spec,
        observation,
        action: OrderedBranchAction::Remove,
    })
}

fn plan_worktree_path(project_dir: &Path, parent_worktree: &Path, entry: &Value) -> PathBuf {
    let Some(raw) = entry.get("worktree").and_then(Value::as_str) else {
        return parent_worktree.to_path_buf();
    };
    let path = PathBuf::from(raw);
    if path.is_absolute() {
        path
    } else {
        project_dir.join(path)
    }
}

fn collect_ordered_branch_specs(
    project_dir: &Path,
    plan: &Value,
    parent_branch: &str,
    parent_worktree: &Path,
    specs: &mut Vec<OrderedBranchSpec>,
) -> Result<()> {
    let Some(entries) = plan.get("sub_tls").and_then(Value::as_array) else {
        return Ok(());
    };
    for entry in entries {
        let object = entry
            .as_object()
            .context("ordered sub-TL plan entry must be an object")?;
        let name = object
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .context("ordered sub-TL plan entry is missing a name")?;
        let agent_name = object
            .get("agent_id")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .unwrap_or(name);
        let branch = format!("{parent_branch}.{agent_name}");
        let worktree = if object.contains_key("worktree") {
            plan_worktree_path(project_dir, parent_worktree, entry)
        } else {
            parent_worktree.join(agent_name)
        };
        specs.push(OrderedBranchSpec {
            branch: branch.clone(),
            parent_branch: parent_branch.to_owned(),
            agent_name: agent_name.to_owned(),
            slice_id: name.to_owned(),
            identity_worktree: worktree.clone(),
            worktree: worktree.clone(),
        });
        let nested = object.get("plan").cloned().unwrap_or_else(|| {
            let mut inline = serde_json::Map::new();
            for key in ["workers", "leaves", "sub_tls"] {
                if let Some(value) = object.get(key) {
                    inline.insert(key.to_owned(), value.clone());
                }
            }
            Value::Object(inline)
        });
        collect_ordered_branch_specs(project_dir, &nested, &branch, &worktree, specs)?;
    }
    Ok(())
}

fn recreate_root_branch(project_dir: &Path) -> String {
    let identity = project_dir.join(".exo/agents/root/identity.json");
    if let Ok(contents) = std::fs::read_to_string(identity) {
        if let Ok(value) = serde_json::from_str::<Value>(&contents) {
            if let Some(branch) = value.get("birth_branch").and_then(Value::as_str) {
                if !branch.is_empty() {
                    return branch.to_owned();
                }
            }
        }
    }
    std::fs::read_to_string(project_dir.join(".exo/agents/root/.birth_branch"))
        .map(|branch| branch.trim().to_owned())
        .ok()
        .filter(|branch| !branch.is_empty())
        .unwrap_or_else(|| "main".to_owned())
}

fn ordered_branch_specs(project_dir: &Path) -> Result<Vec<OrderedBranchSpec>> {
    let plan = read_plan_snapshot_bytes(project_dir)?.or(requested_plan_bytes(project_dir)?);
    let Some(bytes) = plan else {
        return Ok(Vec::new());
    };
    let document = serde_json::from_slice::<Value>(&bytes)
        .context("failed to parse the plan while inspecting ordered branch ownership")?;
    let plan = document.get("plan").unwrap_or(&document);
    let root_branch = recreate_root_branch(project_dir);
    let mut specs = Vec::new();
    collect_ordered_branch_specs(
        project_dir,
        plan,
        &root_branch,
        &project_dir.join(".exo/worktrees"),
        &mut specs,
    )?;
    specs.sort_by(|left, right| {
        right
            .branch
            .matches('.')
            .count()
            .cmp(&left.branch.matches('.').count())
            .then_with(|| left.branch.cmp(&right.branch))
    });
    Ok(specs)
}

fn git_branch_exists(project_dir: &Path, branch: &str) -> Result<bool> {
    let output = std::process::Command::new("git")
        .args(["show-ref", "--verify", "--quiet"])
        .arg(format!("refs/heads/{branch}"))
        .current_dir(project_dir)
        .output()
        .context("failed to inspect ordered branch")?;
    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => anyhow::bail!(
            "failed to inspect ordered branch {branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ),
    }
}

fn git_worktree_for_branch(project_dir: &Path, branch: &str) -> Result<Option<PathBuf>> {
    let output = std::process::Command::new("git")
        .args(["worktree", "list", "--porcelain"])
        .current_dir(project_dir)
        .output()
        .context("failed to inspect ordered branch worktrees")?;
    if !output.status.success() {
        anyhow::bail!(
            "failed to list ordered branch worktrees: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let mut path = None;
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        if let Some(value) = line.strip_prefix("worktree ") {
            path = Some(PathBuf::from(value));
        } else if line.strip_prefix("branch refs/heads/") == Some(branch) {
            return Ok(path);
        }
    }
    Ok(None)
}

fn ordered_unique_commits(project_dir: &Path, parent: &str, branch: &str) -> Result<u64> {
    let revision = format!("{parent}..{branch}");
    let output = std::process::Command::new("git")
        .args(["rev-list", "--count", &revision])
        .current_dir(project_dir)
        .output()
        .context("failed to inspect ordered branch commits")?;
    if !output.status.success() {
        anyhow::bail!(
            "cannot verify ordered branch base {parent} for {branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .with_context(|| format!("git returned an invalid commit count for {branch}"))
}

fn ordered_identity_state(
    project_dir: &Path,
    spec: &OrderedBranchSpec,
) -> Result<OrderedIdentityState> {
    let path = project_dir
        .join(".exo/agents")
        .join(&spec.agent_name)
        .join("identity.json");
    if !path.exists() {
        return Ok(OrderedIdentityState::Missing);
    }
    let Ok(contents) = std::fs::read_to_string(&path) else {
        return Ok(OrderedIdentityState::Mismatched);
    };
    let Ok(value) = serde_json::from_str::<Value>(&contents) else {
        return Ok(OrderedIdentityState::Mismatched);
    };
    let expected = serde_json::json!({
        "agent_name": spec.agent_name,
        "slug": spec.agent_name,
        "agent_type": "codex",
        "birth_branch": spec.branch,
        "parent_branch": spec.parent_branch,
        "working_dir": spec.identity_worktree,
        "display_name": format!("🤖 {}", spec.agent_name),
        "topology": "worktree_per_agent",
        "model": null,
        "effort": null,
        "ledger_owned": true,
        "slice_id": spec.slice_id,
    });
    Ok((value == expected)
        .then_some(OrderedIdentityState::Matching)
        .unwrap_or(OrderedIdentityState::Mismatched))
}

async fn inspect_ordered_branch(
    project_dir: &Path,
    spec: OrderedBranchSpec,
    publications: &[PublishedHead],
    protected: &[ProtectedPr],
    authorized_publications: &[u64],
) -> Result<Option<OrderedBranchCleanup>> {
    let branch_exists = git_branch_exists(project_dir, &spec.branch)?;
    let agent_dir = project_dir.join(".exo/agents").join(&spec.agent_name);
    let worktree_exists = spec.worktree.exists();
    let worktree_branch = if worktree_exists {
        Some(
            std::process::Command::new("git")
                .args(["branch", "--show-current"])
                .current_dir(&spec.worktree)
                .output()
                .context("failed to inspect ordered worktree branch")
                .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())?,
        )
    } else {
        None
    };
    let attached_worktree = git_worktree_for_branch(project_dir, &spec.branch)?;
    let unique_commits = branch_exists
        .then(|| ordered_unique_commits(project_dir, &spec.parent_branch, &spec.branch))
        .transpose()?;
    let branch_publications = publications
        .iter()
        .filter(|publication| publication.head_branch == spec.branch)
        .map(|publication| publication.pr_number)
        .collect::<Vec<_>>();
    let protected_publications = protected
        .iter()
        .filter(|publication| branch_publications.contains(&publication.number))
        .map(|publication| publication.number)
        .collect::<Vec<_>>();
    let live_invocation = read_invocation_conservatively(&agent_dir)
        .await
        .is_some_and(|invocation| invocation.is_live());
    let observation = OrderedBranchObservation {
        branch_exists,
        agent_dir_exists: agent_dir.exists(),
        identity: ordered_identity_state(project_dir, &spec)?,
        worktree_exists,
        worktree_branch,
        attached_worktree,
        dirty_worktree: worktree_exists && worktree_is_dirty(&spec.worktree),
        live_invocation,
        unique_commits,
        publications: branch_publications,
        protected_publications,
    };
    let mut cleanup = ordered_branch_action(spec, observation, authorized_publications);
    if let Some(branch) = cleanup.as_mut() {
        if !branch.spec.worktree.starts_with(project_dir) {
            branch.action =
                OrderedBranchAction::Preserve("ordered worktree is outside the project".to_owned());
        }
    }
    Ok(cleanup)
}

fn recreate_worktree_paths(project_dir: &Path) -> Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    for root in [
        project_dir.join(".exo/worktrees"),
        project_dir.join(".exo/companions"),
    ] {
        if !root.is_dir() {
            continue;
        }
        for entry in std::fs::read_dir(root)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                paths.push(entry.path());
            }
        }
    }
    paths.sort();
    Ok(paths)
}

fn git_ref_sha(project_dir: &Path, reference: &str) -> Result<Option<String>> {
    let output = std::process::Command::new("git")
        .args(["rev-parse", "--verify", "--quiet", reference])
        .current_dir(project_dir)
        .output()
        .context("failed to resolve git reference")?;
    if output.status.success() {
        Ok(Some(
            String::from_utf8_lossy(&output.stdout).trim().to_owned(),
        ))
    } else {
        Ok(None)
    }
}

fn recreate_remote_name(project_dir: &Path) -> String {
    std::process::Command::new("git")
        .args(["config", "--get", "exomonad.remote"])
        .current_dir(project_dir)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "origin".to_owned())
}

fn remote_branch_sha(project_dir: &Path, remote: &str, branch: &str) -> Result<Option<String>> {
    let output = std::process::Command::new("git")
        .args([
            "ls-remote",
            "--heads",
            remote,
            &format!("refs/heads/{branch}"),
        ])
        .current_dir(project_dir)
        .output()
        .with_context(|| format!("failed to inspect remote branch {remote}/{branch}"))?;
    if !output.status.success() {
        anyhow::bail!(
            "failed to inspect remote branch {remote}/{branch}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .split_whitespace()
        .next()
        .map(str::to_owned))
}

fn delete_remote_branch_with_lease(
    project_dir: &Path,
    remote: &str,
    branch: &str,
    expected_sha: &str,
) -> Result<()> {
    let lease = format!("--force-with-lease=refs/heads/{branch}:{expected_sha}");
    let refspec = format!(":refs/heads/{branch}");
    let output = std::process::Command::new("git")
        .args(["push", remote, &lease, &refspec])
        .current_dir(project_dir)
        .output()
        .with_context(|| format!("failed to delete remote branch {remote}/{branch}"))?;
    if !output.status.success() {
        anyhow::bail!(
            "failed to delete remote branch {remote}/{branch} with expected head {expected_sha}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

fn leaf_identity_mismatch(
    project_dir: &Path,
    publication: &PublishedHead,
    branch: &str,
) -> Option<String> {
    let Some(agent) = publication
        .author_agent
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    else {
        return Some("publication is missing a durable author identity".to_owned());
    };
    let path = project_dir
        .join(".exo/agents")
        .join(agent)
        .join("identity.json");
    let Ok(contents) = std::fs::read_to_string(&path) else {
        return Some("durable leaf identity is missing".to_owned());
    };
    let Ok(value) = serde_json::from_str::<Value>(&contents) else {
        return Some("durable leaf identity is unreadable".to_owned());
    };
    if value.get("birth_branch").and_then(Value::as_str) != Some(branch) {
        return Some("durable leaf identity does not own this branch".to_owned());
    }
    // The recorded working directory must be inside this project when present.
    if let Some(worktree) = value.get("working_dir").and_then(Value::as_str) {
        let path = PathBuf::from(worktree);
        let absolute = if path.is_absolute() {
            path
        } else {
            project_dir.join(path)
        };
        if !absolute.starts_with(project_dir) {
            return Some("durable leaf identity worktree is outside the project".to_owned());
        }
    }
    None
}

/// Compare a live PR against the recorded publication. Ownership is verified
/// regardless of the PR's state: a closed or merged PR still records the exact
/// head/base it was opened from, so a stale publication can never authorize
/// cleanup of a different PR.
fn forgejo_pr_mismatch(
    pr: &exomonad_core::services::forgejo::ForgejoPullRequest,
    publication: &PublishedHead,
    branch: &str,
) -> Option<String> {
    if pr.head_ref.as_str() != branch {
        return Some("Forgejo PR head branch does not match the recorded publication".to_owned());
    }
    if pr.base_ref.as_str() != publication.base_branch {
        return Some("Forgejo PR base branch does not match the recorded publication".to_owned());
    }
    match pr.head_sha.as_deref() {
        Some(sha) if sha == publication.head_sha => None,
        Some(_) => Some("Forgejo PR head does not match the recorded publication".to_owned()),
        None => Some("Forgejo PR head is unavailable; ownership cannot be verified".to_owned()),
    }
}

async fn leaf_forgejo_mismatch(
    client: &ForgejoClient,
    repo: &RepoInfo,
    publication: &PublishedHead,
    branch: &str,
) -> Result<Option<String>> {
    let pr = client
        .get_pull_request(
            &repo.owner,
            &repo.repo,
            exomonad_core::domain::PRNumber::new(publication.pr_number),
        )
        .await?;
    Ok(forgejo_pr_mismatch(&pr, publication, branch))
}

async fn leaf_branch_cleanups(
    project_dir: &Path,
    registry: &[PublishedHead],
    protected: &[ProtectedPr],
    covered_branches: &HashSet<String>,
    forgejo: Option<(&ForgejoClient, &RepoInfo)>,
) -> Result<Vec<LeafBranchCleanup>> {
    let remote = recreate_remote_name(project_dir);
    let mut cleanups = Vec::new();
    let mut seen = HashSet::new();
    for publication in registry {
        let branch = publication.head_branch.trim();
        if branch.is_empty() || covered_branches.contains(branch) || !seen.insert(branch.to_owned())
        {
            continue;
        }
        let local_head = git_ref_sha(project_dir, &format!("refs/heads/{branch}"))?;
        let worktree = git_worktree_for_branch(project_dir, branch)?.filter(|path| path.is_dir());
        // Remote-only residue from an interrupted cleanup must still be
        // enumerated so the next recreate can finish it.
        let remote_head = remote_branch_sha(project_dir, &remote, branch)?;
        if local_head.is_none() && worktree.is_none() && remote_head.is_none() {
            continue;
        }
        let dirty = worktree
            .as_ref()
            .is_some_and(|path| worktree_is_dirty(path));
        let live = match publication.author_agent.as_deref() {
            Some(agent) => {
                let agent_dir = project_dir.join(".exo/agents").join(agent);
                read_invocation_conservatively(&agent_dir)
                    .await
                    .is_some_and(|invocation| invocation.is_live())
            }
            None => false,
        };
        let is_protected = protected
            .iter()
            .any(|item| item.number == publication.pr_number);
        let mut preserve_reason = leaf_identity_mismatch(project_dir, publication, branch);
        if preserve_reason.is_none() {
            if let Some((client, repo)) = forgejo {
                preserve_reason = leaf_forgejo_mismatch(client, repo, publication, branch).await?;
            }
        }
        let action = match preserve_reason {
            Some(reason) => OrderedBranchAction::Preserve(reason),
            None => leaf_branch_action(
                &publication.head_sha,
                local_head.as_deref(),
                remote_head.as_deref(),
                worktree.as_deref(),
                dirty,
                live,
                is_protected,
                project_dir,
            ),
        };
        let unmerged_commits =
            leaf_unique_commits(project_dir, &publication.base_branch, &publication.head_sha).ok();
        cleanups.push(LeafBranchCleanup {
            branch: branch.to_owned(),
            base_branch: publication.base_branch.clone(),
            head_sha: publication.head_sha.clone(),
            pr_number: publication.pr_number,
            agent: publication.author_agent.clone(),
            remote_name: remote.clone(),
            worktree,
            local_head,
            remote_head,
            unmerged_commits,
            dirty,
            live,
            protected: is_protected,
            action,
        });
    }
    cleanups.sort_by(|left, right| left.branch.cmp(&right.branch));
    Ok(cleanups)
}

fn worktree_is_dirty(path: &Path) -> bool {
    std::process::Command::new("git")
        .args([
            "-C",
            path.to_string_lossy().as_ref(),
            "status",
            "--porcelain",
        ])
        .output()
        .map(|output| output.status.success() && !output.stdout.is_empty())
        .unwrap_or(true)
}

fn same_path(left: &Path, right: &Path) -> bool {
    match (left.canonicalize(), right.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => left == right,
    }
}

/// A fresh observation of one leaf branch taken immediately before a
/// destructive step. Every present ref must still equal the recorded
/// publication head; a moved head, a dirty worktree, or an out-of-project
/// worktree refuses the disposal.
struct LeafObservation {
    local_head: Option<String>,
    remote_head: Option<String>,
    worktree: Option<PathBuf>,
    dirty: bool,
}

fn observe_leaf_branch(project_dir: &Path, leaf: &LeafBranchCleanup) -> Result<LeafObservation> {
    let local_head = git_ref_sha(project_dir, &format!("refs/heads/{}", leaf.branch))?;
    let worktree = git_worktree_for_branch(project_dir, &leaf.branch)?.filter(|path| path.is_dir());
    let dirty = worktree
        .as_ref()
        .is_some_and(|path| worktree_is_dirty(path));
    let remote_head = remote_branch_sha(project_dir, &leaf.remote_name, &leaf.branch)?;
    Ok(LeafObservation {
        local_head,
        remote_head,
        worktree,
        dirty,
    })
}

fn ensure_leaf_unchanged(leaf: &LeafBranchCleanup, observation: &LeafObservation) -> Result<()> {
    if observation.dirty {
        anyhow::bail!(
            "leaf branch {} became dirty during recreate; refusing to dispose it",
            leaf.branch
        );
    }
    if let Some(head) = observation.local_head.as_deref() {
        if head != leaf.head_sha {
            anyhow::bail!(
                "leaf branch {} head changed from {} to {} during recreate; refusing",
                leaf.branch,
                leaf.head_sha,
                head
            );
        }
    }
    if let Some(sha) = observation.remote_head.as_deref() {
        if sha != leaf.head_sha {
            anyhow::bail!(
                "remote branch {}/{} head changed from {} to {} during recreate; refusing",
                leaf.remote_name,
                leaf.branch,
                leaf.head_sha,
                sha
            );
        }
    }
    Ok(())
}

fn leaf_preservation_path(project_dir: &Path, branch: &str, head_sha: &str) -> PathBuf {
    let slug = branch.replace('/', "_");
    let short = &head_sha[..head_sha.len().min(12)];
    project_dir
        .join(".exo")
        .join("recreate-preserved")
        .join(format!("{slug}-{short}.bundle"))
}

fn leaf_unique_commits(project_dir: &Path, base_branch: &str, head_sha: &str) -> Result<u64> {
    let revision = format!("{base_branch}..{head_sha}");
    let output = std::process::Command::new("git")
        .args(["rev-list", "--count", &revision])
        .current_dir(project_dir)
        .output()
        .context("failed to inspect leaf branch commits")?;
    if !output.status.success() {
        anyhow::bail!(
            "cannot verify leaf base {base_branch} for {head_sha}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .with_context(|| format!("git returned an invalid commit count for {head_sha}"))
}

fn verify_preservation_bundle(project_dir: &Path, path: &Path, expected_head: &str) -> Result<()> {
    let verify = std::process::Command::new("git")
        .args(["bundle", "verify", path.to_string_lossy().as_ref()])
        .current_dir(project_dir)
        .output()
        .with_context(|| format!("failed to verify preservation bundle {}", path.display()))?;
    if !verify.status.success() {
        anyhow::bail!(
            "preservation bundle {} did not verify: {}",
            path.display(),
            String::from_utf8_lossy(&verify.stderr).trim()
        );
    }
    // `git bundle verify` only proves the prerequisites are present. Prove the
    // bundle also advertises the exact recorded head, so a bundle for a
    // different commit can never authorize deleting the last refs to ours.
    let heads = std::process::Command::new("git")
        .args(["bundle", "list-heads", path.to_string_lossy().as_ref()])
        .current_dir(project_dir)
        .output()
        .with_context(|| format!("failed to list heads for {}", path.display()))?;
    if !heads.status.success() {
        anyhow::bail!(
            "failed to list heads for preservation bundle {}: {}",
            path.display(),
            String::from_utf8_lossy(&heads.stderr).trim()
        );
    }
    let advertised = String::from_utf8_lossy(&heads.stdout)
        .lines()
        .filter_map(|line| line.split_whitespace().next().map(str::to_owned))
        .collect::<Vec<_>>();
    if !advertised.iter().any(|sha| sha == expected_head) {
        anyhow::bail!(
            "preservation bundle {} does not advertise recorded head {}",
            path.display(),
            expected_head
        );
    }
    Ok(())
}

fn run_git(project_dir: &Path, args: &[&str], context: &str) -> Result<()> {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(project_dir)
        .output()
        .with_context(|| format!("failed to {context}"))?;
    if !output.status.success() {
        anyhow::bail!(
            "{context}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

/// Preserve unmerged commits in a verified bundle before any ref is deleted.
/// The bundle is idempotent: an interrupted disposal that already produced it
/// is verified and reused rather than recreated. A temporary ref is used
/// because `git bundle` only accepts refs, not raw object ids; it is removed
/// after the bundle verifies so no durable branch or ref is left behind.
fn preserve_leaf_unmerged_commits(
    project_dir: &Path,
    remote_name: &str,
    base_branch: &str,
    branch: &str,
    head_sha: &str,
) -> Result<Option<PathBuf>> {
    let path = leaf_preservation_path(project_dir, branch, head_sha);
    if path.exists() {
        // Fail closed: a bundle that does not verify against the exact recorded
        // head must never be trusted to authorize deleting the last refs.
        verify_preservation_bundle(project_dir, &path, head_sha)?;
        return Ok(Some(path));
    }
    let temp_ref = format!(
        "refs/exomonad/recreate-preserve/{}",
        branch.replace('/', "_")
    );
    // Stage the exact head object when it is already present locally (including
    // dangling objects from a deleted branch); otherwise fetch the remote ref
    // into the temporary preservation ref for remote-only residue.
    let staged = run_git(
        project_dir,
        &["update-ref", &temp_ref, head_sha],
        "stage leaf commits for preservation",
    )
    .is_ok();
    if !staged {
        let refspec = format!("+refs/heads/{branch}:{temp_ref}");
        run_git(
            project_dir,
            &["fetch", "--no-tags", remote_name, &refspec],
            "fetch remote leaf commits for preservation",
        )?;
    }
    let outcome = (|| -> Result<Option<PathBuf>> {
        if leaf_unique_commits(project_dir, base_branch, &temp_ref)? == 0 {
            return Ok(None);
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("failed to create {}", parent.display()))?;
        }
        let exclusion = format!("^{base_branch}");
        run_git(
            project_dir,
            &[
                "bundle",
                "create",
                path.to_string_lossy().as_ref(),
                &temp_ref,
                &exclusion,
            ],
            "create leaf preservation bundle",
        )?;
        verify_preservation_bundle(project_dir, &path, head_sha)?;
        Ok(Some(path.clone()))
    })();
    let _ = run_git(
        project_dir,
        &["update-ref", "-d", &temp_ref],
        "clear temporary preservation ref",
    );
    outcome
}

const RECREATE_RECEIPT_SCHEMA_VERSION: u32 = 2;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct RecreateCleanupReceipt {
    schema_version: u32,
    #[serde(default)]
    entries: Vec<RecreateCleanupReceiptEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct RecreateCleanupReceiptEntry {
    branch: String,
    pr_number: u64,
    head_sha: String,
    remote_name: String,
    /// True once the unmerged-commit preservation decision has been recorded
    /// for this exact identity. Prevents a retry from re-trusting a stale
    /// "no bundle needed" conclusion without re-deriving it from current state.
    #[serde(default)]
    preservation_checked: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    preserved_bundle: Option<String>,
    /// Ordered log of completed disposal steps, so a resumed disposal can prove
    /// the exact sequence (remote ref before local branch) it executed.
    #[serde(default)]
    actions: Vec<String>,
    remote_deleted: bool,
    worktree_removed: bool,
    local_branch_deleted: bool,
    identity_removed: bool,
    completed_at_millis: u64,
}

impl RecreateCleanupReceiptEntry {
    fn matches_identity(
        &self,
        branch: &str,
        head_sha: &str,
        pr_number: u64,
        remote_name: &str,
    ) -> bool {
        self.branch == branch
            && self.head_sha == head_sha
            && self.pr_number == pr_number
            && self.remote_name == remote_name
    }

    fn for_leaf(leaf: &LeafBranchCleanup) -> Self {
        Self {
            branch: leaf.branch.clone(),
            pr_number: leaf.pr_number,
            head_sha: leaf.head_sha.clone(),
            remote_name: leaf.remote_name.clone(),
            preservation_checked: false,
            preserved_bundle: None,
            actions: Vec::new(),
            remote_deleted: false,
            worktree_removed: false,
            local_branch_deleted: false,
            identity_removed: false,
            completed_at_millis: 0,
        }
    }
}

fn recreate_receipts_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo").join("recreate-receipts.json")
}

async fn read_recreate_receipts(project_dir: &Path) -> Result<Vec<RecreateCleanupReceiptEntry>> {
    let path = recreate_receipts_path(project_dir);
    let contents = match tokio::fs::read_to_string(&path).await {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error).with_context(|| format!("failed to read {}", path.display()))
        }
    };
    let receipt = serde_json::from_str::<RecreateCleanupReceipt>(&contents)
        .with_context(|| format!("failed to parse {}", path.display()))?;
    if receipt.schema_version > RECREATE_RECEIPT_SCHEMA_VERSION {
        anyhow::bail!(
            "{} uses unsupported recreate receipt schema version {}",
            path.display(),
            receipt.schema_version
        );
    }
    Ok(receipt.entries)
}

async fn write_recreate_receipts(
    project_dir: &Path,
    entries: &[RecreateCleanupReceiptEntry],
) -> Result<()> {
    let path = recreate_receipts_path(project_dir);
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .with_context(|| format!("failed to create {}", parent.display()))?;
    }
    let document = RecreateCleanupReceipt {
        schema_version: RECREATE_RECEIPT_SCHEMA_VERSION,
        entries: entries.to_vec(),
    };
    let serialized =
        serde_json::to_vec_pretty(&document).context("failed to serialize recreate receipts")?;
    let temp = path.with_extension("json.tmp");
    tokio::fs::write(&temp, &serialized)
        .await
        .with_context(|| format!("failed to write {}", temp.display()))?;
    tokio::fs::rename(&temp, &path)
        .await
        .with_context(|| format!("failed to publish {}", path.display()))?;
    Ok(())
}

async fn record_recreate_receipt(
    project_dir: &Path,
    entry: RecreateCleanupReceiptEntry,
) -> Result<()> {
    let mut entries = read_recreate_receipts(project_dir).await?;
    match entries.iter_mut().find(|existing| {
        existing.matches_identity(
            &entry.branch,
            &entry.head_sha,
            entry.pr_number,
            &entry.remote_name,
        )
    }) {
        Some(existing) => *existing = entry,
        None => entries.push(entry),
    }
    write_recreate_receipts(project_dir, &entries).await
}

fn verify_pr_association(
    pr: &exomonad_core::services::forgejo::ForgejoPullRequest,
    publication: &PublishedHead,
) -> Result<()> {
    if pr.head_ref.as_str() != publication.head_branch {
        anyhow::bail!(
            "Forgejo PR #{} head branch '{}' does not match publication '{}'",
            publication.pr_number,
            pr.head_ref.as_str(),
            publication.head_branch
        );
    }
    if pr.base_ref.as_str() != publication.base_branch {
        anyhow::bail!(
            "Forgejo PR #{} base branch '{}' does not match publication '{}'",
            publication.pr_number,
            pr.base_ref.as_str(),
            publication.base_branch
        );
    }
    if pr.head_sha.as_deref() != Some(publication.head_sha.as_str()) {
        anyhow::bail!(
            "Forgejo PR #{} head '{}' does not match publication '{}'",
            publication.pr_number,
            pr.head_sha.as_deref().unwrap_or("absent"),
            publication.head_sha
        );
    }
    Ok(())
}

fn project_remote_is_forgejo(project_dir: &Path) -> bool {
    let remote = recreate_remote_name(project_dir);
    let output = std::process::Command::new("git")
        .args(["remote", "get-url", &remote])
        .current_dir(project_dir)
        .output();
    match output {
        Ok(output) if output.status.success() => {
            let url = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            exomonad_core::services::repo::parse_github_url(&url).is_ok()
        }
        _ => false,
    }
}

fn recreate_forgejo_client(
    project_dir: &Path,
    config: &Config,
) -> Result<Option<Arc<ForgejoClient>>> {
    match (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        (Some(url), Some(token)) => Ok(Some(
            ForgejoClient::new(url, token).context("failed to create Forgejo client")?,
        )),
        // The `fj` CLI can only reach a Forgejo-backed remote. When the
        // configured remote is a local path (as in the local-git tests), no
        // Forgejo client is available and ownership falls back to the durable
        // identity plus exact-ref checks.
        (None, None)
            if ForgejoClient::fj_binary_in_path() && project_remote_is_forgejo(project_dir) =>
        {
            Ok(Some(ForgejoClient::new_fj(project_dir.to_path_buf())))
        }
        (None, None) => Ok(None),
        _ => anyhow::bail!(
            "both forgejo_url and forgejo_token are required to close PRs during --recreate"
        ),
    }
}

async fn build_recreate_plan(
    project_dir: &Path,
    config: &Config,
    force: bool,
) -> Result<RecreatePlan> {
    let worktrees = recreate_worktree_paths(project_dir)?;
    let dirty_worktrees = worktrees
        .iter()
        .filter(|path| worktree_is_dirty(path))
        .cloned()
        .collect::<Vec<_>>();
    let registry = read_published_heads(project_dir).await?;
    let client = (!registry.is_empty())
        .then(|| recreate_forgejo_client(project_dir, config))
        .transpose()?
        .flatten();
    let repo = if client.is_some() {
        Some(get_repo_info(project_dir).await?)
    } else {
        None
    };

    let mut prs_to_close = Vec::new();
    let mut prs_to_remove = Vec::new();
    let mut protected = Vec::new();
    for publication in &registry {
        let number = publication.pr_number;
        prs_to_remove.push(number);
        let Some(client) = client.as_ref() else {
            prs_to_close.push(number);
            protected.push(ProtectedPr {
                number,
                reason: "Forgejo client unavailable; PR safety cannot be verified".to_owned(),
            });
            continue;
        };
        let repo = repo
            .as_ref()
            .context("repository identity is required for PR cleanup")?;
        let pr = client
            .get_pull_request(
                &repo.owner,
                &repo.repo,
                exomonad_core::domain::PRNumber::new(number),
            )
            .await?;
        if pr.merged || pr.state != "open" {
            continue;
        }
        let reviews = client
            .list_pull_request_reviews(
                &repo.owner,
                &repo.repo,
                exomonad_core::domain::PRNumber::new(number),
            )
            .await?;
        let approved = reviews.iter().any(|review| {
            review.state.eq_ignore_ascii_case("approved")
                && review
                    .commit_id
                    .as_deref()
                    .is_none_or(|commit| Some(commit) == pr.head_sha.as_deref())
        });
        let statuses = if let Some(head_sha) = pr.head_sha.as_deref() {
            client
                .list_commit_statuses(&repo.owner, &repo.repo, head_sha)
                .await?
        } else {
            Vec::new()
        };
        let ci_green = !statuses.is_empty()
            && statuses.iter().all(|status| {
                matches!(
                    status.status,
                    exomonad_core::domain::CIStatus::Success
                        | exomonad_core::domain::CIStatus::Neutral
                )
            });
        if approved && ci_green {
            protected.push(ProtectedPr {
                number,
                reason: "approved and CI-green".to_owned(),
            });
        }
        prs_to_close.push(number);
    }
    prs_to_close.sort_unstable();
    prs_to_close.dedup();
    prs_to_remove.sort_unstable();
    prs_to_remove.dedup();
    protected.sort_by_key(|item| item.number);
    // Authorize every publication the plan disposes of, including merged or
    // already-closed PRs whose records are removed without a close call.
    let authorized_publications = if force {
        prs_to_remove.clone()
    } else {
        let protected_numbers = protected.iter().map(|item| item.number).collect::<Vec<_>>();
        prs_to_remove
            .iter()
            .copied()
            .filter(|number| !protected_numbers.contains(number))
            .collect::<Vec<_>>()
    };
    let (ordered_branches, covered_branches) = if project_dir.join(".git").exists() {
        let specs = ordered_branch_specs(project_dir)?;
        let covered = specs
            .iter()
            .map(|spec| spec.branch.clone())
            .collect::<HashSet<_>>();
        let mut branches = Vec::new();
        for spec in specs {
            if let Some(branch) = inspect_ordered_branch(
                project_dir,
                spec,
                &registry,
                &protected,
                &authorized_publications,
            )
            .await?
            {
                branches.push(branch);
            }
        }
        (branches, covered)
    } else {
        (Vec::new(), HashSet::new())
    };
    // Issue-owned leaf branches are enumerated from the publication registry,
    // never inferred from a branch name alone.
    let leaf_branches = if project_dir.join(".git").exists() {
        leaf_branch_cleanups(
            project_dir,
            &registry,
            &protected,
            &covered_branches,
            client.as_deref().zip(repo.as_ref()),
        )
        .await?
    } else {
        Vec::new()
    };
    Ok(RecreatePlan {
        worktrees,
        ordered_branches,
        leaf_branches,
        prs_to_close,
        prs_to_remove,
        protected,
        dirty_worktrees,
    })
}

async fn prepare_recreate(
    project_dir: &Path,
    config: &Config,
    confirm: bool,
    force: bool,
    dry_run: bool,
    allow_pending_gate: bool,
) -> Result<Option<RecreatePlan>> {
    ensure_recreate_allowed(project_dir, allow_pending_gate)?;
    let plan = build_recreate_plan(project_dir, config, force).await?;
    println!("{}", plan.render());
    if dry_run {
        return Ok(None);
    }
    let gates = plan
        .ordered_branches
        .iter()
        .filter_map(|branch| {
            branch
                .gate()
                .map(|gate| (branch.spec.branch.as_str(), gate))
        })
        .chain(
            plan.leaf_branches
                .iter()
                .filter_map(|branch| branch.gate().map(|gate| (branch.branch.as_str(), gate))),
        )
        .collect::<Vec<_>>();
    if !gates.is_empty() {
        let mut message = String::from("refusing --recreate: branch cleanup is gated:");
        for (branch, gate) in gates {
            message.push_str(&format!("\n  {branch}: {gate}"));
        }
        anyhow::bail!(message);
    }
    if !confirm {
        anyhow::bail!(
            "refusing destructive --recreate without --confirm-recreate; use --recreate-dry-run to inspect the plan"
        );
    }
    if !plan.protected.is_empty() && !force {
        anyhow::bail!(
            "refusing --recreate because protected PRs are present; pass --force-recreate together with --confirm-recreate to override"
        );
    }
    Ok(Some(plan))
}

async fn destroy_recreate_resources(
    project_dir: &Path,
    config: &Config,
    plan: &RecreatePlan,
    force: bool,
) -> Result<()> {
    let _plan_lock = acquire_plan_transition_lock_async(project_dir).await?;
    let git_wt = Arc::new(GitWorktreeService::new(project_dir.to_path_buf()));
    let ordered_worktrees = plan
        .ordered_branches
        .iter()
        .map(|branch| branch.spec.worktree.clone())
        .collect::<HashSet<_>>();
    // Leaf worktrees are disposed by the leaf loop below, which removes the
    // worktree, local branch, and durable identity in a leased, receipted
    // order. Excluding them from the generic worktree sweep prevents a
    // worktree that became dirty after planning from being force-removed
    // before its own revalidation can refuse.
    let leaf_worktrees = plan
        .leaf_branches
        .iter()
        .filter_map(|branch| branch.worktree.clone())
        .collect::<Vec<_>>();
    let publications = read_published_heads(project_dir).await?;
    let publication_by_pr = publications
        .iter()
        .map(|publication| (publication.pr_number, publication))
        .collect::<HashMap<_, _>>();
    let publication_by_branch = publications
        .iter()
        .filter(|publication| !publication.head_branch.trim().is_empty())
        .map(|publication| {
            (
                (
                    publication.head_branch.as_str(),
                    publication.head_sha.as_str(),
                ),
                publication,
            )
        })
        .collect::<HashMap<_, _>>();
    let protected_numbers = plan
        .protected
        .iter()
        .map(|item| item.number)
        .collect::<Vec<_>>();
    // Publications slated for record removal are authorized for branch
    // cleanup: merged or closed PRs have nothing left to close, while open PRs
    // are closed below before any local ownership is removed.
    let authorized_publications = if force {
        plan.prs_to_remove.clone()
    } else {
        plan.prs_to_remove
            .iter()
            .copied()
            .filter(|number| !protected_numbers.contains(number))
            .collect::<Vec<_>>()
    };
    let client = recreate_forgejo_client(project_dir, config)?;
    // Live Forgejo verification is required whenever a client is available; a
    // repository-resolution failure must stop cleanup rather than silently
    // disabling ownership checks.
    let repo = match client.as_ref() {
        Some(_) => Some(
            get_repo_info(project_dir)
                .await
                .context("cannot resolve the Forgejo repository for ownership verification")?,
        ),
        None => None,
    };
    // Phase one revalidates every ordered branch without mutating anything, so
    // a gate discovered late can never follow already-removed resources.
    let mut validated: Vec<OrderedBranchCleanup> = Vec::new();
    for branch in &plan.ordered_branches {
        if let Some(gate) = branch.gate() {
            anyhow::bail!(
                "ordered branch cleanup gate for {}: {}",
                branch.spec.branch,
                gate
            );
        }
        let Some(current) = inspect_ordered_branch(
            project_dir,
            branch.spec.clone(),
            &publications,
            &plan.protected,
            &authorized_publications,
        )
        .await?
        else {
            continue;
        };
        if let Some(gate) = current.gate() {
            anyhow::bail!(
                "ordered branch cleanup gate for {}: {}",
                current.spec.branch,
                gate
            );
        }
        validated.push(current);
    }
    // Leaf branches are revalidated before any destructive step: durable
    // identity, live Forgejo PR association, and the exact local and remote
    // refs must still match the recorded publication. This runs before the
    // generic worktree sweep and before PR closure so a late gate can never
    // follow a removed resource.
    let mut validated_leaves: Vec<&LeafBranchCleanup> = Vec::new();
    for branch in &plan.leaf_branches {
        if let Some(gate) = branch.gate() {
            anyhow::bail!("leaf branch cleanup gate for {}: {gate}", branch.branch);
        }
        let observation = observe_leaf_branch(project_dir, branch)?;
        ensure_leaf_unchanged(branch, &observation)?;
        // Ownership evidence is only required while a ref or worktree remains.
        // Once an interrupted disposal has removed every ref, the remaining
        // identity/record cleanup is proven by the plan and its receipts.
        let refs_present = observation.local_head.is_some()
            || observation.remote_head.is_some()
            || observation.worktree.is_some();
        if refs_present {
            let publication = publication_by_branch
                .get(&(branch.branch.as_str(), branch.head_sha.as_str()))
                .with_context(|| {
                    format!(
                        "cannot dispose leaf branch {}: no publication record to verify ownership",
                        branch.branch
                    )
                })?;
            if let Some(reason) = leaf_identity_mismatch(project_dir, publication, &branch.branch) {
                anyhow::bail!("leaf branch cleanup gate for {}: {reason}", branch.branch);
            }
            if let Some((client, repo)) = client.as_deref().zip(repo.as_ref()) {
                if let Some(reason) =
                    leaf_forgejo_mismatch(client, repo, publication, &branch.branch).await?
                {
                    anyhow::bail!("leaf branch cleanup gate for {}: {reason}", branch.branch);
                }
            }
        }
        validated_leaves.push(branch);
    }
    // Close published PRs before removing local ownership. Each closure
    // re-verifies that the live Forgejo PR still matches the recorded
    // publication, so a stale record can never close an unrelated PR. If
    // closure fails, the branch and identity remain so the disposal can be
    // retried safely.
    if !plan.prs_to_close.is_empty() {
        let client = client
            .as_ref()
            .context("cannot close published PRs: no Forgejo client is configured")?;
        let repo = repo
            .as_ref()
            .context("repository identity is required for PR cleanup")?;
        for number in &plan.prs_to_close {
            // A missing record means a prior disposal already removed it, so
            // there is nothing left to close; never close without the record
            // that proves ownership.
            let Some(publication) = publication_by_pr.get(number) else {
                warn!(
                    pr_number = number,
                    "skipping PR closure: publication record is absent"
                );
                continue;
            };
            let pr = client
                .get_pull_request(
                    &repo.owner,
                    &repo.repo,
                    exomonad_core::domain::PRNumber::new(*number),
                )
                .await?;
            verify_pr_association(&pr, publication)?;
            if pr.merged || pr.state != "open" {
                continue;
            }
            client
                .close_pull_request(
                    &repo.owner,
                    &repo.repo,
                    exomonad_core::domain::PRNumber::new(*number),
                )
                .await
                .with_context(|| format!("failed to close PR #{number}"))?;
        }
    }
    // Phase two performs disposal only after revalidation and PR closure.
    for current in &validated {
        if current.observation.worktree_exists {
            let path = current.spec.worktree.clone();
            let git_wt = git_wt.clone();
            tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path))
                .await
                .context("ordered worktree disposal task failed")??;
        }
        if git_branch_exists(project_dir, &current.spec.branch)? {
            let branch_name =
                exomonad_core::domain::BranchName::try_from_str(&current.spec.branch)?;
            let git_wt = git_wt.clone();
            tokio::task::spawn_blocking(move || git_wt.delete_bookmark(&branch_name))
                .await
                .context("ordered branch disposal task failed")??;
        }
        let agent_dir = project_dir
            .join(".exo/agents")
            .join(&current.spec.agent_name);
        if agent_dir.exists() {
            std::fs::remove_dir_all(&agent_dir)
                .with_context(|| format!("failed to remove {}", agent_dir.display()))?;
        }
    }

    for path in &plan.worktrees {
        if ordered_worktrees.contains(path) {
            continue;
        }
        if leaf_worktrees.iter().any(|leaf| same_path(leaf, path)) {
            continue;
        }
        if path
            .parent()
            .is_some_and(|parent| parent.ends_with(".exo/worktrees"))
        {
            if let Some(slug) = path.file_name().and_then(|name| name.to_str()) {
                exomonad_core::services::agent_resources::dispose_agent_resources(
                    project_dir,
                    git_wt.clone(),
                    slug,
                )
                .await;
            }
        } else {
            let path_for_git = path.clone();
            let git_wt = git_wt.clone();
            tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path_for_git))
                .await
                .context("worktree disposal task failed")??;
            if path.exists() {
                std::fs::remove_dir_all(path)
                    .with_context(|| format!("failed to remove {}", path.display()))?;
            }
        }
    }
    // Dispose issue-owned leaf branches enumerated in the plan. Every leaf is
    // re-observed immediately before each destructive step, then steps run in a
    // fixed, recoverable order: preserve unmerged commits in a verified bundle,
    // delete the exact remote ref with a SHA lease, remove the worktree, delete
    // the local branch, and remove the durable identity. A durable receipt is
    // persisted after each step so an interrupted disposal can resume
    // idempotently without losing evidence.
    let existing_receipts = read_recreate_receipts(project_dir).await?;
    for branch in &validated_leaves {
        let mut receipt = match existing_receipts.iter().find(|entry| {
            entry.matches_identity(
                &branch.branch,
                &branch.head_sha,
                branch.pr_number,
                &branch.remote_name,
            )
        }) {
            Some(entry) => entry.clone(),
            None => {
                // A receipt for the same branch under a different PR, head, or
                // remote belongs to another publication. Never borrow its
                // completed steps: stop with recoverable evidence instead.
                if let Some(conflict) = existing_receipts
                    .iter()
                    .find(|entry| entry.branch == branch.branch)
                {
                    anyhow::bail!(
                        "conflicting recreate receipt for branch {}: recorded pr=#{} head={} remote={} does not match current pr=#{} head={} remote={}",
                        branch.branch,
                        conflict.pr_number,
                        conflict.head_sha,
                        conflict.remote_name,
                        branch.pr_number,
                        branch.head_sha,
                        branch.remote_name,
                    );
                }
                RecreateCleanupReceiptEntry::for_leaf(branch)
            }
        };
        // Preservation is reconciled against current state, never trusted from
        // the receipt alone. While a ref still exists it is re-derived and a
        // bundle is (re)created and verified against the exact recorded head.
        // Once every ref is gone, any receipted bundle is re-verified from
        // disk. A missing or mismatched bundle fails closed before any ref is
        // deleted.
        let preservation = observe_leaf_branch(project_dir, branch)?;
        ensure_leaf_unchanged(branch, &preservation)?;
        let refs_present = preservation.local_head.is_some() || preservation.remote_head.is_some();
        if refs_present {
            match preserve_leaf_unmerged_commits(
                project_dir,
                &branch.remote_name,
                &branch.base_branch,
                &branch.branch,
                &branch.head_sha,
            )? {
                Some(path) => {
                    let display = path.display().to_string();
                    if receipt.preserved_bundle.as_deref() != Some(display.as_str()) {
                        receipt.actions.push("preserve_unmerged_commits".to_owned());
                    }
                    receipt.preserved_bundle = Some(display);
                }
                None => receipt.preserved_bundle = None,
            }
            receipt.preservation_checked = true;
            receipt.completed_at_millis = current_time_millis() as u64;
            record_recreate_receipt(project_dir, receipt.clone()).await?;
        } else if !receipt.preservation_checked {
            // Every ref vanished before preservation was ever recorded, so
            // recoverability of any unmerged commits cannot be proven.
            anyhow::bail!(
                "cannot verify unmerged-commit preservation for {}: no refs remain and no receipted bundle exists",
                branch.branch
            );
        } else if let Some(bundle) = receipt.preserved_bundle.clone() {
            let path = PathBuf::from(&bundle);
            if !path.exists() {
                anyhow::bail!(
                    "receipted preservation bundle {} for {} is missing; refusing to delete refs",
                    path.display(),
                    branch.branch
                );
            }
            verify_preservation_bundle(project_dir, &path, &branch.head_sha)?;
        }
        // Remote ref: always reconcile against the observed remote. A ref that
        // reappeared after a prior receipt is deleted again under a fresh SHA
        // lease; a changed head fails closed.
        {
            let observation = observe_leaf_branch(project_dir, branch)?;
            ensure_leaf_unchanged(branch, &observation)?;
            if let Some(sha) = observation.remote_head.as_deref() {
                delete_remote_branch_with_lease(
                    project_dir,
                    &branch.remote_name,
                    &branch.branch,
                    sha,
                )?;
                receipt.actions.push("delete_remote_branch".to_owned());
            } else if !receipt.remote_deleted {
                receipt
                    .actions
                    .push("remote_branch_already_absent".to_owned());
            }
            receipt.remote_deleted = true;
            receipt.completed_at_millis = current_time_millis() as u64;
            record_recreate_receipt(project_dir, receipt.clone()).await?;
        }
        // Worktree: always reconcile against the observed worktree so one that
        // reappeared after a receipt is removed again.
        {
            let observation = observe_leaf_branch(project_dir, branch)?;
            ensure_leaf_unchanged(branch, &observation)?;
            if let Some(path) = observation.worktree.clone() {
                let git_wt = git_wt.clone();
                tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path))
                    .await
                    .context("leaf worktree disposal task failed")??;
                receipt.actions.push("remove_worktree".to_owned());
            }
            receipt.worktree_removed = true;
            receipt.completed_at_millis = current_time_millis() as u64;
            record_recreate_receipt(project_dir, receipt.clone()).await?;
        }
        // Local branch: always reconcile against the observed local ref.
        {
            let observation = observe_leaf_branch(project_dir, branch)?;
            ensure_leaf_unchanged(branch, &observation)?;
            if observation.local_head.is_some() {
                let branch_name = exomonad_core::domain::BranchName::try_from_str(&branch.branch)?;
                let git_wt = git_wt.clone();
                tokio::task::spawn_blocking(move || git_wt.delete_bookmark(&branch_name))
                    .await
                    .context("leaf branch disposal task failed")??;
                receipt.actions.push("delete_local_branch".to_owned());
            } else if !receipt.local_branch_deleted {
                receipt
                    .actions
                    .push("local_branch_already_absent".to_owned());
            }
            receipt.local_branch_deleted = true;
            receipt.completed_at_millis = current_time_millis() as u64;
            record_recreate_receipt(project_dir, receipt.clone()).await?;
        }
        // Durable identity: always reconcile against the observed directory.
        {
            if let Some(agent) = branch
                .agent
                .as_deref()
                .filter(|value| !value.trim().is_empty())
            {
                let agent_dir = project_dir.join(".exo/agents").join(agent);
                if agent_dir.exists() {
                    std::fs::remove_dir_all(&agent_dir)
                        .with_context(|| format!("failed to remove {}", agent_dir.display()))?;
                    receipt.actions.push("remove_identity".to_owned());
                }
            }
            receipt.identity_removed = true;
            receipt.completed_at_millis = current_time_millis() as u64;
            record_recreate_receipt(project_dir, receipt.clone()).await?;
        }
    }
    let numbers = plan.prs_to_remove.iter().copied().collect::<HashSet<_>>();
    remove_published_heads_for_prs(project_dir, &numbers).await?;
    Ok(())
}

#[cfg(test)]
const TL_LOOP_ARCHIVE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/tl_loop.pyz"));
const TL_LOOP_PYPROJECT: &str = include_str!("../../../tl_loop/pyproject.toml");
const TL_LOOP_INTERPRETER_POLICY: &str = include_str!("../../../tl_loop/interpreter_policy.toml");
const TL_CONTROLLER_STARTUP_TIMEOUT: Duration = Duration::from_secs(5);
const EXOMONAD_BUILD_GIT_COMMIT: &str = env!("EXOMONAD_BUILD_GIT_COMMIT");
const EXOMONAD_TL_LOOP_GIT_COMMIT: &str = env!("EXOMONAD_TL_LOOP_GIT_COMMIT");

fn read_root_tl_protocol(cwd: &Path, wasm_name: &str) -> Option<String> {
    exomonad_core::services::agent_control::load_role_context(cwd, wasm_name, "root")
}

fn codex_root_instructions(cwd: &Path, wasm_name: &str) -> String {
    read_root_tl_protocol(cwd, wasm_name)
        .map(|protocol| {
            format!(
                "{protocol}\n\n{}",
                exomonad_core::services::agent_control::CODEX_TL_RUNTIME_NOTES
            )
        })
        .unwrap_or_else(|| {
            exomonad_core::services::agent_control::CODEX_TL_RUNTIME_NOTES.to_string()
        })
}

fn watcher_dashboard_command(cwd: &Path) -> Result<String> {
    let watcher_log_dir = cwd.join(".exo/logs");
    let watcher_log_path = watcher_log_dir.join("watcher.log");
    std::fs::create_dir_all(&watcher_log_dir).with_context(|| {
        format!(
            "failed to create watcher log directory {}",
            watcher_log_dir.display()
        )
    })?;
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&watcher_log_path)
        .with_context(|| format!("failed to create {}", watcher_log_path.display()))?;
    Ok("exomonad watch".to_string())
}

fn redact_init_argv(args: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut redact_next = false;
    args.into_iter()
        .map(|arg| {
            if redact_next {
                redact_next = false;
                return "<redacted>".to_string();
            }
            let lower = arg.to_ascii_lowercase();
            let sensitive = ["token", "secret", "password", "api-key", "api_key"]
                .iter()
                .any(|needle| lower.contains(needle));
            if sensitive && !arg.contains('=') {
                redact_next = true;
            }
            if sensitive {
                match arg.split_once('=') {
                    Some((flag, _)) => format!("{flag}=<redacted>"),
                    None => arg,
                }
            } else {
                arg
            }
        })
        .collect()
}

fn append_init_invocation_log(
    cwd: &Path,
    config: &Config,
    argv: &[String],
    mode: SessionMode,
) -> Result<()> {
    let log_dir = cwd.join(".exo/logs");
    std::fs::create_dir_all(&log_dir)
        .with_context(|| format!("failed to create init log directory {}", log_dir.display()))?;
    let payload = serde_json::json!({
        "timestamp_ms": current_time_millis(),
        "argv": argv,
        "session": config.tmux_session.as_str(),
        "session_mode": mode.as_str(),
        "resolved": {
            "root_agent_type": agent_type_str(config.root_agent_type),
            "spawn_agent_type": agent_type_str(config.spawn_agent_type),
            "reviewer_agent_type": agent_type_str(config.reviewer.agent_type),
            "root_model": config.model.as_deref(),
            "opencode_tl_model": config.opencode.tl_model.as_deref(),
            "opencode_worker_model": config.opencode.worker_model.as_deref(),
            "reviewer_model": config.reviewer.model.as_deref(),
            "tl_effort": config.tl_effort_level,
            "worker_effort": config.worker_effort_level,
            "reviewer_effort": config.reviewer_effort_level,
        }
    });
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("init.jsonl"))?;
    use std::io::Write as _;
    writeln!(file, "{}", serde_json::to_string(&payload)?)?;
    Ok(())
}

fn record_session_mode(cwd: &Path, mode: SessionMode) -> Result<()> {
    let path = cwd.join(".exo/tl-loop/session-mode.json");
    let parent = path
        .parent()
        .context("session mode record has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let temporary = path.with_extension("tmp");
    let payload = serde_json::json!({
        "session_mode": mode.as_str(),
        "recorded_at_ms": current_time_millis(),
    });
    std::fs::write(&temporary, serde_json::to_string_pretty(&payload)?)?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

#[cfg(test)]
fn ensure_start_allowed(project_dir: &Path) -> Result<()> {
    match read_startup_checkpoint(project_dir)? {
        StartupCheckpoint::Nonterminal { phase } => anyhow::bail!(
            "refusing --start: existing non-terminal TL run is at phase {phase}; use --continue to resume it or --recreate --confirm-recreate to replace it"
        ),
        _ => Ok(()),
    }
}

#[derive(Debug, PartialEq, Eq)]
enum StartPlanDecision {
    Continue {
        validated_plan: Vec<u8>,
    },
    NewRun {
        archive_terminal: bool,
        validated_plan: Option<Vec<u8>>,
    },
}

fn requested_plan_bytes(project_dir: &Path) -> Result<Option<Vec<u8>>> {
    let plan_path = project_dir.join(".exo/tl-loop/plan.json");
    match std::fs::read(&plan_path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", plan_path.display())),
    }
}

fn resolve_start_plan(project_dir: &Path) -> Result<StartPlanDecision> {
    let checkpoint = read_startup_checkpoint(project_dir)?;
    let requested = requested_plan_bytes(project_dir)?;
    let snapshot_path = plan_snapshot_path(project_dir);
    let snapshot = match std::fs::read(&snapshot_path) {
        Ok(bytes) => Some(bytes),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => {
            Err(error).with_context(|| format!("failed to read {}", snapshot_path.display()))?
        }
    };
    // Plan identity is intentionally byte-for-byte. The snapshot preserves the
    // exact authored document so formatting-only edits are still explicit drift.
    let identical = requested.is_some() && requested == snapshot;
    let new_run = |archive_terminal| StartPlanDecision::NewRun {
        archive_terminal,
        validated_plan: requested.clone(),
    };

    match checkpoint {
        StartupCheckpoint::Missing => Ok(new_run(false)),
        StartupCheckpoint::TerminalOrParked { .. } if identical => {
            Ok(StartPlanDecision::Continue {
                validated_plan: requested
                    .clone()
                    .context("identical start plan has no captured bytes")?,
            })
        }
        StartupCheckpoint::TerminalOrParked { .. } => Ok(new_run(true)),
        StartupCheckpoint::Nonterminal { phase: _ } if identical => {
            Ok(StartPlanDecision::Continue {
                validated_plan: requested
                    .clone()
                    .context("identical start plan has no captured bytes")?,
            })
        }
        StartupCheckpoint::Nonterminal { phase } => {
            let identity = if snapshot.is_some() {
                "the requested plan differs from its persisted session snapshot"
            } else {
                "the requested plan cannot be matched because its persisted session snapshot is missing"
            };
            anyhow::bail!(
                "refusing --start: existing non-terminal TL run is at phase {phase}; {identity}; use --continue to resume it or --recreate --confirm-recreate to replace it"
            )
        }
    }
}

fn prepare_start_plan(project_dir: &Path) -> Result<StartPlanDecision> {
    resolve_start_plan(project_dir)
}

fn ensure_plan_matches(project_dir: &Path, expected: &[u8]) -> Result<()> {
    let actual = requested_plan_bytes(project_dir)?.with_context(|| {
        format!(
            "refusing start transition: {} disappeared after validation",
            project_dir.join(".exo/tl-loop/plan.json").display()
        )
    })?;
    if actual != expected {
        anyhow::bail!(
            "refusing start transition: plan.json changed after validation; no runtime state was replaced"
        );
    }
    Ok(())
}

fn read_plan_snapshot_bytes(project_dir: &Path) -> Result<Option<Vec<u8>>> {
    let path = plan_snapshot_path(project_dir);
    match std::fs::read(&path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

fn plan_snapshot_digest_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/plan.snapshot.sha256")
}

fn read_plan_snapshot_digest(project_dir: &Path) -> Result<Option<String>> {
    let path = plan_snapshot_digest_path(project_dir);
    match std::fs::read_to_string(&path) {
        Ok(value) => {
            let digest = value.trim().to_owned();
            if digest.is_empty() {
                anyhow::bail!("persisted TL plan digest is empty: {}", path.display());
            }
            Ok(Some(digest))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

fn write_plan_snapshot_digest(project_dir: &Path, digest: &str) -> Result<()> {
    write_plan_digest_file(&plan_snapshot_digest_path(project_dir), digest)
}

fn write_plan_digest_file(path: &Path, digest: &str) -> Result<()> {
    let parent = path
        .parent()
        .context("plan snapshot digest has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let temporary = path.with_file_name(format!(
        "{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .context("plan snapshot digest path is not UTF-8")?
    ));
    std::fs::write(&temporary, format!("{digest}\n"))?;
    std::fs::rename(&temporary, path)?;
    Ok(())
}

const PLAN_TRANSITION_PHASE_PREPARED: &str = "prepared";
const PLAN_TRANSITION_PHASE_ARCHIVED: &str = "archived";
const PLAN_TRANSITION_PHASE_ROLLED_BACK: &str = "rolled_back";

fn acquire_init_lifecycle_lock(
    project_dir: &Path,
) -> Result<claude_teams_bridge::file_lock::FileLock> {
    let lock_target = project_dir.join(".exo/tl-loop/init-reconcile");
    if let Some(parent) = lock_target.parent() {
        std::fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create init lifecycle lock directory {}",
                parent.display()
            )
        })?;
    }
    claude_teams_bridge::file_lock::FileLock::acquire(&lock_target, Duration::from_secs(3600))
        .context("failed to acquire project init lifecycle lock")
}

fn acquire_plan_transition_lock(
    project_dir: &Path,
) -> Result<claude_teams_bridge::file_lock::FileLock> {
    let lock_target = project_dir.join(".exo/tl-loop/plan-transition");
    if let Some(parent) = lock_target.parent() {
        std::fs::create_dir_all(parent).with_context(|| {
            format!(
                "failed to create plan transition lock directory {}",
                parent.display()
            )
        })?;
    }
    claude_teams_bridge::file_lock::FileLock::acquire(&lock_target, Duration::from_secs(3600))
        .context("failed to acquire project plan transition lock")
}

fn plan_transition_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/plan-transition.json")
}

fn plan_transition_previous_snapshot_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/plan-transition.previous.snapshot")
}

fn plan_transition_previous_digest_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/plan-transition.previous.sha256")
}

fn write_plan_transition(project_dir: &Path, phase: &str, archive: Option<&Path>) -> Result<()> {
    let archive_name = archive
        .map(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .context("plan transition archive path is not UTF-8")
        })
        .transpose()?;
    let payload = serde_json::json!({
        "phase": phase,
        "archive_name": archive_name,
    });
    let path = plan_transition_path(project_dir);
    let parent = path
        .parent()
        .context("plan transition has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, serde_json::to_vec(&payload)?)?;
    std::fs::rename(&temporary, path)?;
    Ok(())
}

fn read_plan_transition(project_dir: &Path) -> Result<Option<(String, Option<String>)>> {
    let path = plan_transition_path(project_dir);
    let contents = match std::fs::read_to_string(&path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error).with_context(|| format!("failed to read {}", path.display()))
        }
    };
    let value: Value = serde_json::from_str(&contents)
        .with_context(|| format!("invalid plan transition journal {}", path.display()))?;
    let phase = value
        .get("phase")
        .and_then(Value::as_str)
        .context("plan transition journal has no phase")?
        .to_owned();
    if phase != PLAN_TRANSITION_PHASE_PREPARED
        && phase != PLAN_TRANSITION_PHASE_ARCHIVED
        && phase != PLAN_TRANSITION_PHASE_ROLLED_BACK
    {
        anyhow::bail!("unsupported plan transition phase {phase}");
    }
    let archive_name = value
        .get("archive_name")
        .and_then(Value::as_str)
        .map(str::to_owned);
    if archive_name.as_deref().is_some_and(|name| {
        name == "." || name == ".." || name.contains('/') || name.contains('\\')
    }) {
        anyhow::bail!("plan transition journal contains an invalid archive name");
    }
    Ok(Some((phase, archive_name)))
}

fn remove_if_present(path: &Path) -> Result<()> {
    if let Err(error) = std::fs::remove_file(path) {
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).with_context(|| format!("failed to remove {}", path.display()));
        }
    }
    Ok(())
}

fn maybe_fail_transition(failure: &mut Option<&str>, step: &str) -> Result<()> {
    if failure.is_some_and(|expected| expected == step) {
        *failure = None;
        anyhow::bail!("injected plan transition failure at {step}");
    }
    Ok(())
}

fn clear_plan_transition_locked(project_dir: &Path, failure: &mut Option<&str>) -> Result<()> {
    maybe_fail_transition(failure, "backup-snapshot-delete")?;
    remove_if_present(&plan_transition_previous_snapshot_path(project_dir))?;
    maybe_fail_transition(failure, "backup-digest-delete")?;
    remove_if_present(&plan_transition_previous_digest_path(project_dir))?;
    maybe_fail_transition(failure, "journal-delete")?;
    remove_if_present(&plan_transition_path(project_dir))
}

#[cfg(test)]
fn clear_plan_transition(project_dir: &Path) -> Result<()> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    clear_plan_transition_locked(project_dir, &mut failure)
}

fn begin_plan_transition_locked(
    project_dir: &Path,
    previous_snapshot: Option<&[u8]>,
    previous_digest: Option<&str>,
    archive: Option<&Path>,
) -> Result<()> {
    match previous_snapshot {
        Some(bytes) => {
            write_plan_snapshot(&plan_transition_previous_snapshot_path(project_dir), bytes)?
        }
        None => remove_if_present(&plan_transition_previous_snapshot_path(project_dir))?,
    }
    match previous_digest {
        Some(digest) => {
            write_plan_digest_file(&plan_transition_previous_digest_path(project_dir), digest)?
        }
        None => remove_if_present(&plan_transition_previous_digest_path(project_dir))?,
    }
    write_plan_transition(project_dir, PLAN_TRANSITION_PHASE_PREPARED, archive)
}

#[cfg(test)]
fn begin_plan_transition(
    project_dir: &Path,
    previous_snapshot: Option<&[u8]>,
    previous_digest: Option<&str>,
    archive: Option<&Path>,
) -> Result<()> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    begin_plan_transition_locked(project_dir, previous_snapshot, previous_digest, archive)
}

fn recover_plan_transition_locked(project_dir: &Path, failure: &mut Option<&str>) -> Result<()> {
    let Some((phase, archive_name)) = read_plan_transition(project_dir)? else {
        return Ok(());
    };
    if phase == PLAN_TRANSITION_PHASE_ARCHIVED || phase == PLAN_TRANSITION_PHASE_ROLLED_BACK {
        return clear_plan_transition_locked(project_dir, failure);
    }
    if phase != PLAN_TRANSITION_PHASE_PREPARED {
        anyhow::bail!("unsupported plan transition phase {phase}");
    }
    let archive = archive_name.map(|name| project_dir.join(".exo/tl-loop").join(name));
    if let Some(archive) = archive.as_deref() {
        let root = project_dir.join(".exo/tl-loop/root");
        if root.exists() && archive.exists() {
            anyhow::bail!(
                "cannot recover plan transition: both TL root {} and archive {} exist",
                root.display(),
                archive.display()
            );
        }
        if !root.exists() {
            if !archive.exists() {
                anyhow::bail!(
                    "cannot recover plan transition: archived TL root {} is missing",
                    archive.display()
                );
            }
            std::fs::rename(archive, &root).with_context(|| {
                format!("failed to restore archived TL root {}", archive.display())
            })?;
        }
    }
    let previous_snapshot = plan_transition_previous_snapshot_path(project_dir);
    if previous_snapshot.is_file() {
        let bytes = std::fs::read(&previous_snapshot)?;
        write_plan_snapshot(&plan_snapshot_path(project_dir), &bytes)?;
    } else if previous_snapshot.exists() {
        anyhow::bail!(
            "cannot recover plan transition: snapshot backup {} is not a file",
            previous_snapshot.display()
        );
    } else {
        remove_if_present(&plan_snapshot_path(project_dir))?;
    }
    let previous_digest = plan_transition_previous_digest_path(project_dir);
    if previous_digest.is_file() {
        let digest = std::fs::read_to_string(&previous_digest)?;
        write_plan_snapshot_digest(project_dir, digest.trim())?;
    } else if previous_digest.exists() {
        anyhow::bail!(
            "cannot recover plan transition: digest backup {} is not a file",
            previous_digest.display()
        );
    } else {
        remove_plan_snapshot_digest(project_dir)?;
    }
    write_plan_transition(
        project_dir,
        PLAN_TRANSITION_PHASE_ROLLED_BACK,
        archive.as_deref(),
    )?;
    clear_plan_transition_locked(project_dir, failure)
}

#[cfg(test)]
fn recover_plan_transition(project_dir: &Path) -> Result<()> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    recover_plan_transition_locked(project_dir, &mut failure)
}

fn rollback_plan_transition_locked(
    project_dir: &Path,
    error: anyhow::Error,
    failure: &mut Option<&str>,
) -> anyhow::Error {
    match recover_plan_transition_locked(project_dir, failure) {
        Ok(()) => error,
        Err(rollback_error) => {
            anyhow::anyhow!("{error}; transition rollback also failed: {rollback_error}")
        }
    }
}

fn remove_plan_snapshot_digest(project_dir: &Path) -> Result<()> {
    if let Err(error) = std::fs::remove_file(plan_snapshot_digest_path(project_dir)) {
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).context("failed to clear stale TL plan digest");
        }
    }
    Ok(())
}

fn apply_plan_snapshot_locked(
    project_dir: &Path,
    plan: Option<&[u8]>,
    digest: Option<&str>,
    failure: &mut Option<&str>,
) -> Result<()> {
    if let Some(plan) = plan {
        maybe_fail_transition(failure, "snapshot-write")?;
        write_plan_snapshot(&plan_snapshot_path(project_dir), plan)?;
        if let Some(digest) = digest {
            maybe_fail_transition(failure, "digest-write")?;
            write_plan_snapshot_digest(project_dir, digest)?;
        } else {
            remove_plan_snapshot_digest(project_dir)?;
        }
        return Ok(());
    }
    remove_plan_snapshot_digest(project_dir)?;
    if let Err(error) = std::fs::remove_file(plan_snapshot_path(project_dir)) {
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error).context("failed to clear stale TL plan snapshot");
        }
    }
    Ok(())
}

#[cfg(test)]
fn apply_plan_snapshot(
    project_dir: &Path,
    plan: Option<&[u8]>,
    digest: Option<&str>,
) -> Result<()> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    apply_plan_snapshot_locked(project_dir, plan, digest, &mut failure)
}

fn ensure_snapshot_matches(project_dir: &Path, expected: &[u8]) -> Result<()> {
    let actual = read_plan_snapshot_bytes(project_dir)?.with_context(|| {
        format!(
            "refusing start transition: {} disappeared after validation",
            plan_snapshot_path(project_dir).display()
        )
    })?;
    if actual != expected {
        anyhow::bail!(
            "refusing start transition: plan.snapshot changed after validation; no runtime state was replaced"
        );
    }
    let expected_digest = plan_digest(expected);
    match read_plan_snapshot_digest(project_dir)? {
        Some(actual_digest) if actual_digest != expected_digest => anyhow::bail!(
            "refusing start transition: persisted plan identity changed after validation; no runtime state was replaced"
        ),
        Some(_) => {}
        None => write_plan_snapshot_digest(project_dir, &expected_digest)?,
    }
    Ok(())
}

fn ensure_snapshot_digest_matches(project_dir: &Path, expected: &str) -> Result<()> {
    let snapshot = read_plan_snapshot_bytes(project_dir)?.with_context(|| {
        format!(
            "refusing controller recovery: {} is missing",
            plan_snapshot_path(project_dir).display()
        )
    })?;
    if plan_digest(&snapshot) != expected {
        anyhow::bail!(
            "refusing controller recovery: plan.snapshot no longer matches the captured plan identity"
        );
    }
    Ok(())
}

fn apply_start_plan_locked(
    project_dir: &Path,
    decision: StartPlanDecision,
    failure: &mut Option<&str>,
) -> Result<SessionMode> {
    match decision {
        StartPlanDecision::Continue { validated_plan } => {
            ensure_plan_matches(project_dir, &validated_plan)?;
            ensure_snapshot_matches(project_dir, &validated_plan)?;
            Ok(SessionMode::Continue)
        }
        StartPlanDecision::NewRun {
            archive_terminal,
            validated_plan,
        } => {
            let previous_snapshot = read_plan_snapshot_bytes(project_dir)?;
            let previous_digest = read_plan_snapshot_digest(project_dir)?;
            let validated_digest = validated_plan.as_deref().map(plan_digest);
            let archive = if archive_terminal {
                root_archive_path_at(project_dir, current_time_millis())?
            } else {
                None
            };
            begin_plan_transition_locked(
                project_dir,
                previous_snapshot.as_deref(),
                previous_digest.as_deref(),
                archive.as_deref(),
            )?;
            if let Err(error) = apply_plan_snapshot_locked(
                project_dir,
                validated_plan.as_deref(),
                validated_digest.as_deref(),
                failure,
            ) {
                return Err(rollback_plan_transition_locked(project_dir, error, failure));
            }
            if let Some(archive) = archive.as_deref() {
                if let Err(error) = maybe_fail_transition(failure, "archive") {
                    return Err(rollback_plan_transition_locked(project_dir, error, failure));
                }
                if let Err(error) = archive_root_tl_run_to(project_dir, archive) {
                    return Err(rollback_plan_transition_locked(project_dir, error, failure));
                }
            }
            if let Err(error) = write_plan_transition(
                project_dir,
                PLAN_TRANSITION_PHASE_ARCHIVED,
                archive.as_deref(),
            ) {
                return Err(rollback_plan_transition_locked(project_dir, error, failure));
            }
            clear_plan_transition_locked(project_dir, failure)?;
            Ok(SessionMode::Start)
        }
    }
}

#[cfg(test)]
fn apply_start_plan(project_dir: &Path, decision: StartPlanDecision) -> Result<SessionMode> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    apply_start_plan_locked(project_dir, decision, &mut failure)
}

#[cfg(test)]
fn apply_start_plan_with_failure(
    project_dir: &Path,
    decision: StartPlanDecision,
    failure: &'static str,
) -> Result<SessionMode> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = Some(failure);
    apply_start_plan_locked(project_dir, decision, &mut failure)
}

impl StartPlanDecision {
    fn validated_plan(&self) -> Option<&[u8]> {
        match self {
            Self::Continue { validated_plan } => Some(validated_plan),
            Self::NewRun { validated_plan, .. } => validated_plan.as_deref(),
        }
    }
}

#[cfg(test)]
fn apply_start_plan_after_validation<F>(
    project_dir: &Path,
    decision: StartPlanDecision,
    validate: F,
) -> Result<SessionMode>
where
    F: FnOnce() -> Result<()>,
{
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    validate()?;
    apply_start_plan_locked(project_dir, decision, &mut failure)
}

fn apply_start_plan_after_validation_locked<F>(
    project_dir: &Path,
    decision: StartPlanDecision,
    validate: F,
    _lock: &claude_teams_bridge::file_lock::FileLock,
    failure: &mut Option<&str>,
) -> Result<SessionMode>
where
    F: FnOnce() -> Result<()>,
{
    validate()?;
    apply_start_plan_locked(project_dir, decision, failure)
}

fn apply_recreate_plan_after_validation_locked<F>(
    project_dir: &Path,
    validated_plan: Option<&[u8]>,
    validate: F,
    _lock: &claude_teams_bridge::file_lock::FileLock,
    failure: &mut Option<&str>,
) -> Result<Option<String>>
where
    F: FnOnce() -> Result<()>,
{
    validate()?;
    if let Some(plan) = validated_plan {
        ensure_plan_matches(project_dir, plan)?;
    }

    let previous_snapshot = read_plan_snapshot_bytes(project_dir)?;
    let previous_digest = read_plan_snapshot_digest(project_dir)?;
    let validated_digest = validated_plan.map(plan_digest);
    let archive = root_archive_path_at(project_dir, current_time_millis())?;
    begin_plan_transition_locked(
        project_dir,
        previous_snapshot.as_deref(),
        previous_digest.as_deref(),
        archive.as_deref(),
    )?;
    if let Err(error) = apply_plan_snapshot_locked(
        project_dir,
        validated_plan,
        validated_digest.as_deref(),
        failure,
    ) {
        return Err(rollback_plan_transition_locked(project_dir, error, failure));
    }
    Ok(validated_digest)
}

fn complete_recreate_plan_transition_locked(
    project_dir: &Path,
    failure: &mut Option<&str>,
) -> Result<()> {
    let Some((phase, archive_name)) = read_plan_transition(project_dir)? else {
        anyhow::bail!("cannot commit recreate plan: transition journal is missing");
    };
    if phase != PLAN_TRANSITION_PHASE_PREPARED {
        anyhow::bail!("cannot commit recreate plan from transition phase {phase}");
    }
    let archive = archive_name.map(|name| project_dir.join(".exo/tl-loop").join(name));
    if let Some(archive) = archive.as_deref() {
        if let Err(error) = maybe_fail_transition(failure, "archive")
            .and_then(|_| archive_root_tl_run_to(project_dir, archive))
        {
            return Err(rollback_plan_transition_locked(project_dir, error, failure));
        }
    }
    if let Err(error) = write_plan_transition(
        project_dir,
        PLAN_TRANSITION_PHASE_ARCHIVED,
        archive.as_deref(),
    ) {
        return Err(rollback_plan_transition_locked(project_dir, error, failure));
    }
    clear_plan_transition_locked(project_dir, failure)
}

#[cfg(test)]
fn complete_recreate_plan_transition(
    project_dir: &Path,
    failure: Option<&'static str>,
) -> Result<()> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = failure;
    complete_recreate_plan_transition_locked(project_dir, &mut failure)
}

#[cfg(test)]
fn apply_recreate_plan_after_validation(
    project_dir: &Path,
    validated_plan: Option<&[u8]>,
    validate: impl FnOnce() -> Result<()>,
) -> Result<Option<String>> {
    let _lock = acquire_plan_transition_lock(project_dir)?;
    let mut failure = None;
    apply_recreate_plan_after_validation_locked(
        project_dir,
        validated_plan,
        validate,
        &_lock,
        &mut failure,
    )
}

fn report_legacy_session(project_dir: &Path, mode: SessionMode) {
    if mode != SessionMode::Continue
        || !project_dir.join(".exo/tl-loop/root/run.json").is_file()
        || project_dir.join(".exo/tl-loop/session-mode.json").is_file()
    {
        return;
    }
    info!(
        "Detected legacy runtime state; --continue will validate it without archiving or reinterpreting it"
    );
}

fn plan_snapshot_path(cwd: &Path) -> PathBuf {
    cwd.join(".exo/tl-loop/plan.snapshot")
}

fn write_plan_snapshot(snapshot_path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = snapshot_path
        .parent()
        .context("plan snapshot has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let temporary = snapshot_path.with_extension("tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, snapshot_path)?;
    Ok(())
}

/// Preserve the original plan bytes so continue can fail closed on drift.
/// The snapshot is separate from plan.json and is never used as a replacement
/// plan, which keeps the authoritative plan untouched.
fn validate_or_record_plan_snapshot_locked(cwd: &Path, mode: SessionMode) -> Result<()> {
    let plan_path = cwd.join(".exo/tl-loop/plan.json");
    let snapshot_path = plan_snapshot_path(cwd);
    let plan_exists = plan_path.is_file();
    let snapshot_exists = snapshot_path.is_file();

    if mode == SessionMode::Continue {
        if !plan_exists && !snapshot_exists {
            remove_plan_snapshot_digest(cwd)?;
            return Ok(());
        }
        if !plan_exists {
            anyhow::bail!(
                "cannot continue: plan.json is missing while its persisted session snapshot exists"
            );
        }
        let current = std::fs::read(&plan_path)
            .with_context(|| format!("failed to read {}", plan_path.display()))?;
        if !snapshot_exists {
            write_plan_snapshot(&snapshot_path, &current)?;
            write_plan_snapshot_digest(cwd, &plan_digest(&current))?;
            info!(
                path = %snapshot_path.display(),
                "Adopted legacy plan bytes as the initial TL plan snapshot"
            );
            return Ok(());
        }
        let original = std::fs::read(&snapshot_path)
            .with_context(|| format!("failed to read {}", snapshot_path.display()))?;
        if current != original {
            anyhow::bail!(
                "cannot continue: {} differs from its persisted session snapshot; use --start or explicitly reconcile the plan",
                plan_path.display()
            );
        }
        let expected_digest = plan_digest(&original);
        match read_plan_snapshot_digest(cwd)? {
            Some(actual_digest) if actual_digest != expected_digest => anyhow::bail!(
                "cannot continue: persisted plan identity differs from its snapshot; use --recreate --confirm-recreate to reconcile it"
            ),
            Some(_) => {}
            None => write_plan_snapshot_digest(cwd, &expected_digest)?,
        }
        return Ok(());
    }

    if plan_exists && !snapshot_exists {
        let original = std::fs::read(&plan_path)
            .with_context(|| format!("failed to read {}", plan_path.display()))?;
        write_plan_snapshot(&snapshot_path, &original)?;
        write_plan_snapshot_digest(cwd, &plan_digest(&original))?;
        info!(path = %snapshot_path.display(), "Recorded initial TL plan snapshot");
    }
    Ok(())
}

#[cfg(test)]
fn validate_or_record_plan_snapshot(cwd: &Path, mode: SessionMode) -> Result<()> {
    let _lock = acquire_plan_transition_lock(cwd)?;
    validate_or_record_plan_snapshot_locked(cwd, mode)
}

fn capture_validated_plan_digest(cwd: &Path) -> Result<Option<String>> {
    let plan = requested_plan_bytes(cwd)?;
    let snapshot = read_plan_snapshot_bytes(cwd)?;
    match (plan, snapshot) {
        (None, None) => Ok(None),
        (Some(plan), Some(snapshot)) if plan == snapshot => {
            let expected = plan_digest(&snapshot);
            let persisted = read_plan_snapshot_digest(cwd)?.with_context(|| {
                format!(
                    "missing persisted plan identity for {}",
                    plan_snapshot_path(cwd).display()
                )
            })?;
            if persisted != expected {
                anyhow::bail!(
                    "persisted plan identity does not match plan.json and plan.snapshot"
                );
            }
            Ok(Some(persisted))
        }
        (None, Some(_)) => anyhow::bail!(
            "cannot use persisted plan identity: plan.json is missing while its session snapshot exists"
        ),
        (Some(_), None) => anyhow::bail!(
            "cannot use persisted plan identity: plan.snapshot is missing while plan.json exists"
        ),
        (Some(_), Some(_)) => {
            anyhow::bail!("persisted plan identity cannot be captured because plan bytes differ")
        }
    }
}

fn publication_matches_agent(
    agent_name: &str,
    record: &InvocationRecord,
    publication: &PublishedHead,
) -> bool {
    let owner_matches = publication.author_agent.as_deref() == Some(agent_name)
        || record
            .runtime_agent_id
            .as_deref()
            .is_some_and(|runtime_id| publication.author_agent.as_deref() == Some(runtime_id));
    if !owner_matches {
        return false;
    }

    let invocation_matches = publication.invocation_id.as_deref()
        == Some(record.invocation_id.as_str())
        || invocation_succession_reaches_current(publication, &record.invocation_id);
    if !invocation_matches {
        return false;
    }

    let branch_matches = record
        .branch
        .as_deref()
        .is_none_or(|branch| branch == publication.head_branch);
    let slice_matches = record
        .slice_id
        .as_deref()
        .is_none_or(|slice| publication.slice_id.as_deref() == Some(slice));
    branch_matches && slice_matches
}

fn classify_agent(agent_dir: &Path, registry: &[PublishedHead]) -> AgentContinuation {
    let agent_name = agent_dir
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty());
    let Some(agent_name) = agent_name else {
        return AgentContinuation::Recreate {
            reason: "agent directory has no usable name",
        };
    };
    let invocation_path =
        agent_dir.join(exomonad_core::services::agent_control::INVOCATION_FILENAME);
    let contents = match std::fs::read_to_string(&invocation_path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return AgentContinuation::Recreate {
                reason: "invocation record is missing",
            };
        }
        Err(_) => {
            return AgentContinuation::Recreate {
                reason: "invocation record is unreadable",
            };
        }
    };
    let record = match serde_json::from_str::<InvocationRecord>(&contents) {
        Ok(record) => record,
        Err(_) => {
            return AgentContinuation::Recreate {
                reason: "invocation record is malformed",
            };
        }
    };
    if record.invocation_id.trim().is_empty() {
        return AgentContinuation::Recreate {
            reason: "invocation record has an empty invocation_id",
        };
    }
    if registry
        .iter()
        .any(|publication| publication_matches_agent(agent_name, &record, publication))
    {
        AgentContinuation::Preserve {
            invocation_id: record.invocation_id,
        }
    } else {
        AgentContinuation::Recreate {
            reason: "invocation has no matching verified publication ownership",
        }
    }
}

fn write_continuation_decision(agent_dir: &Path, decision: &AgentContinuation) -> Result<()> {
    let path = agent_dir.join("continuation.json");
    let payload = match decision {
        AgentContinuation::Preserve { invocation_id } => serde_json::json!({
            "schema_version": 1,
            "classification": decision.classification(),
            "invocation_id": invocation_id,
        }),
        AgentContinuation::Recreate { reason } => serde_json::json!({
            "schema_version": 1,
            "classification": decision.classification(),
            "reason": reason,
        }),
    };
    let bytes = serde_json::to_vec_pretty(&payload)?;
    if std::fs::read(&path).ok().as_deref() == Some(bytes.as_slice()) {
        return Ok(());
    }
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, &path)?;
    Ok(())
}

async fn reconcile_agent_continuations(project_dir: &Path) -> Result<(usize, usize)> {
    let agents_dir = project_dir.join(".exo/agents");
    if !agents_dir.is_dir() {
        return Ok((0, 0));
    }
    let registry = read_published_heads(project_dir).await?;
    let mut agents = std::fs::read_dir(&agents_dir)?
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false))
        .filter(|entry| entry.file_name().to_string_lossy() != "root")
        .collect::<Vec<_>>();
    agents.sort_by_key(|entry| entry.file_name());

    let mut preserved = 0;
    let mut recreated = 0;
    for entry in agents {
        let decision = classify_agent(&entry.path(), &registry);
        match &decision {
            AgentContinuation::Preserve { invocation_id } => {
                preserved += 1;
                info!(
                    agent = %entry.file_name().to_string_lossy(),
                    invocation_id,
                    "Preserving agent invocation for --continue"
                );
            }
            AgentContinuation::Recreate { reason } => {
                recreated += 1;
                warn!(
                    agent = %entry.file_name().to_string_lossy(),
                    reason,
                    "Classifying agent invocation for explicit recreation"
                );
            }
        }
        write_continuation_decision(&entry.path(), &decision)?;
    }
    Ok((preserved, recreated))
}

const WATCHER_WINDOW_NAME: &str = "Watcher";

fn has_watcher_dashboard_window<'a>(window_names: impl IntoIterator<Item = &'a str>) -> bool {
    window_names
        .into_iter()
        .any(|name| name == WATCHER_WINDOW_NAME)
}

async fn ensure_watcher_dashboard_window(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    cwd: &Path,
    shell: &str,
) -> Result<()> {
    let windows = ipc.list_windows().await?;

    if has_watcher_dashboard_window(windows.iter().map(|window| window.window_name.as_str())) {
        let existing = windows
            .iter()
            .find(|window| window.window_name == WATCHER_WINDOW_NAME)
            .expect("has_watcher_dashboard_window confirmed a matching window exists");
        let routing = exomonad_core::domain::RoutingInfo::window(existing.window_id.clone());
        if ipc.routing_target_process_alive(&routing).await? {
            debug!("Watcher dashboard window already exists");
            return Ok(());
        }
        info!(
            window = %existing.window_id,
            "Watcher dashboard window is dead, removing before recovery"
        );
        ipc.kill_window(&existing.window_id).await?;
    }

    let watcher_cmd = watcher_dashboard_command(cwd)?;
    let watcher_win = ipc
        .new_window(WATCHER_WINDOW_NAME, cwd, shell, &watcher_cmd)
        .await?;
    // Without remain-on-exit, tmux destroys the window the instant the
    // watcher process dies, so the liveness check above would never observe
    // a dead-but-present window to repair — it would just see the name gone
    // and recreate anyway. Setting it here makes a crashed Watcher behave
    // like Server/TL: inspectable evidence until the next reconciliation.
    ipc.set_window_remain_on_exit(&watcher_win, true).await?;
    info!(window = %watcher_win, "Watcher dashboard window created");
    Ok(())
}

fn forgejo_host_from_url(input: &str) -> Option<String> {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return None;
    }
    let no_scheme = trimmed
        .strip_prefix("http://")
        .or_else(|| trimmed.strip_prefix("https://"))
        .unwrap_or(trimmed);
    let host = no_scheme.split('/').next().unwrap_or_default().trim();
    if host.is_empty() {
        None
    } else {
        Some(host.to_string())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct RemoteRepoParts {
    host: String,
    owner: String,
    repo: String,
    has_http_auth: bool,
}

/// Git config key holding an explicit remote-name override. Mirrors
/// `exomonad_core::services::repo`'s `REMOTE_OVERRIDE_KEY` — kept as a
/// literal here since this module doesn't depend on that private const.
const GIT_REMOTE_OVERRIDE_KEY: &str = "exomonad.remote";

/// Resolve which git remote exomonad's PR/CI operations should use: the
/// `exomonad.remote` git config override if set, else `"origin"`.
fn resolve_git_remote(cwd: &Path) -> String {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["config", "--get", GIT_REMOTE_OVERRIDE_KEY])
        .output();
    match output {
        Ok(o) if o.status.success() => {
            let value = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if value.is_empty() {
                "origin".to_string()
            } else {
                value
            }
        }
        _ => "origin".to_string(),
    }
}

/// Validate `remote` names an existing git remote, then persist it as the
/// `exomonad.remote` git config override (`git config --local`). Used by
/// `exomonad init --set-git-remote <name>` to pin which remote PR/CI
/// operations use when a repo has multiple remotes (e.g. a GitHub `origin`
/// alongside a Forgejo remote) — worktrees share the main repo's
/// `.git/config`, so this applies to every spawned agent automatically.
fn set_git_remote_override(cwd: &Path, remote: &str) -> Result<()> {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .arg("remote")
        .output()
        .context("failed to run git remote")?;
    if !output.status.success() {
        anyhow::bail!("git remote exited with {}", output.status);
    }
    let remotes: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|r| !r.is_empty())
        .map(str::to_string)
        .collect();
    if !remotes.iter().any(|r| r == remote) {
        anyhow::bail!(
            "--set-git-remote {remote}: no such git remote configured (found: {}). \
             Add it first with `git remote add {remote} <url>`.",
            remotes.join(", ")
        );
    }

    let status = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["config", "--local", GIT_REMOTE_OVERRIDE_KEY, remote])
        .status()
        .with_context(|| format!("failed to run git config --local {GIT_REMOTE_OVERRIDE_KEY}"))?;
    if !status.success() {
        anyhow::bail!("git config --local {GIT_REMOTE_OVERRIDE_KEY} {remote} exited with {status}");
    }

    info!(remote, "Configured exomonad.remote git config override");
    Ok(())
}

fn configure_forgejo_remote(
    cwd: &Path,
    forgejo_url: &str,
    forgejo_token: &str,
    remote: &str,
) -> Result<()> {
    let output = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "get-url", remote])
        .output()
        .with_context(|| format!("failed to run git remote get-url {remote}"))?;
    if !output.status.success() {
        warn!(
            remote,
            "No such remote found; skipping Forgejo remote token auth setup"
        );
        return Ok(());
    }

    let old_url = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let Some(new_url) = forgejo_token_remote_url(&old_url, forgejo_url, forgejo_token) else {
        return Ok(());
    };
    let status = std::process::Command::new("git")
        .current_dir(cwd)
        .args(["remote", "set-url", remote, &new_url])
        .status()
        .with_context(|| format!("failed to run git remote set-url {remote}"))?;
    if !status.success() {
        anyhow::bail!("git remote set-url {remote} exited with {status}");
    }

    info!(
        remote,
        old_url = %redact_remote_token(&old_url, forgejo_token),
        new_url = %redact_remote_token(&new_url, forgejo_token),
        "Configured git remote to use Forgejo HTTP token auth"
    );
    Ok(())
}

fn forgejo_token_remote_url(
    remote_url: &str,
    forgejo_url: &str,
    forgejo_token: &str,
) -> Option<String> {
    let forgejo_token = forgejo_token.trim();
    if forgejo_token.is_empty() {
        debug!("Skipping Forgejo remote token auth setup without a forgejo_token");
        return None;
    }

    let remote = parse_remote_repo_parts(remote_url)?;
    let forgejo_host_raw = forgejo_host_from_url(forgejo_url)?;
    let forgejo_host = host_without_port(&forgejo_host_raw);
    if remote.host != forgejo_host {
        debug!(
            remote_host = %remote.host,
            forgejo_host,
            "Skipping Forgejo remote token auth setup for non-Forgejo origin"
        );
        return None;
    }
    if remote_url.contains(forgejo_token) || remote.has_http_auth {
        debug!("Forgejo remote already has HTTP auth; skipping remote rewrite");
        return None;
    }

    tokenized_forgejo_url(forgejo_url, forgejo_token, &remote.owner, &remote.repo)
}

// Token is embedded in the URL and visible in local git config.
// Acceptable for local Forgejo instances. Do not use for public hosts.
fn tokenized_forgejo_url(
    forgejo_url: &str,
    forgejo_token: &str,
    owner: &str,
    repo: &str,
) -> Option<String> {
    let base = forgejo_url.trim().trim_end_matches('/');
    let (scheme, rest) = base.split_once("://")?;
    Some(format!(
        "{scheme}://forgejo_pat:{forgejo_token}@{rest}/{owner}/{repo}.git"
    ))
}

fn parse_remote_repo_parts(remote_url: &str) -> Option<RemoteRepoParts> {
    let trimmed = remote_url.trim();
    if let Some(rest) = trimmed.strip_prefix("git@") {
        let (host, path) = rest.split_once(':')?;
        return remote_parts(host, path, false);
    }
    if let Some(rest) = trimmed.strip_prefix("ssh://") {
        let rest = rest.split_once('@').map(|(_, path)| path).unwrap_or(rest);
        let (host, path) = rest.split_once('/')?;
        return remote_parts(host, path, false);
    }
    let (_, rest) = trimmed.split_once("://")?;
    let (authority, path) = rest.split_once('/')?;
    let has_http_auth = authority.contains('@');
    let host = authority
        .rsplit_once('@')
        .map(|(_, host)| host)
        .unwrap_or(authority);
    remote_parts(host, path, has_http_auth)
}

fn remote_parts(host: &str, path: &str, has_http_auth: bool) -> Option<RemoteRepoParts> {
    let cleaned = path
        .trim_start_matches('/')
        .strip_suffix(".git")
        .unwrap_or(path);
    let mut segments = cleaned.split('/').filter(|segment| !segment.is_empty());
    let repo = segments.next_back()?.to_string();
    let owner = segments.next_back()?.to_string();
    Some(RemoteRepoParts {
        host: host_without_port(host).to_string(),
        owner,
        repo,
        has_http_auth,
    })
}

fn host_without_port(host: &str) -> &str {
    host.split(':').next().unwrap_or(host)
}

fn redact_remote_token(url: &str, token: &str) -> String {
    if token.is_empty() {
        url.to_string()
    } else {
        url.replace(token, "<token>")
    }
}

fn check_fj_cli_configuration(cwd: &Path) {
    if !exomonad_core::services::ForgejoClient::fj_binary_in_path() {
        warn!(
            "[Forgejo] Not configured - forgejo_url/forgejo_token are absent and fj was not found in PATH"
        );
        return;
    }

    info!(
        "[Forgejo] fj found in PATH; exomonad serve will use the fj CLI backend when HTTP config is absent"
    );
    match std::process::Command::new("fj")
        .args(["auth", "status"])
        .current_dir(cwd)
        .status()
    {
        Ok(status) if status.success() => {
            info!("[Forgejo] fj auth status succeeded");
        }
        Ok(status) => {
            warn!(
                status = %status,
                "[Forgejo] fj is in PATH but `fj auth status` failed; file_pr, watcher_pr_state, and spawn_reviewer may fail until fj is authenticated"
            );
        }
        Err(error) => {
            warn!(
                error = %error,
                "[Forgejo] failed to run `fj auth status`; file_pr, watcher_pr_state, and spawn_reviewer may fail until fj is authenticated"
            );
        }
    }
}

fn mailbox_protocol_available_for_config(config: &Config) -> bool {
    config.root_agent_type == AgentType::Claude && config.spawn_agent_type == AgentType::Claude
}

fn forgejo_env_vars(
    forgejo_url: &str,
    forgejo_token: &str,
    forgejo_reviewer_token: Option<&str>,
) -> Vec<(&'static str, String)> {
    let forgejo_token = forgejo_token.trim();
    let forgejo_reviewer_token = forgejo_reviewer_token
        .map(str::trim)
        .filter(|token| !token.is_empty());
    if forgejo_token.is_empty() && forgejo_reviewer_token.is_none() {
        return Vec::new();
    }

    let mut vars = Vec::new();
    if let Some(forgejo_host) = forgejo_host_from_url(forgejo_url) {
        vars.push(("FORGEJO_HOST", forgejo_host.clone()));
        vars.push(("GH_HOST", forgejo_host));
    }
    if !forgejo_token.is_empty() {
        vars.push(("FORGEJO_TOKEN", forgejo_token.to_string()));
        vars.push(("GH_TOKEN", forgejo_token.to_string()));
    }
    if let Some(token) = forgejo_reviewer_token {
        vars.push(("FORGEJO_REVIEWER_TOKEN", token.to_string()));
    }
    vars.push(("FORGEJO_URL", forgejo_url.to_string()));
    vars
}

fn current_time_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

fn extra_mcp_server_to_json(server: &crate::config::McpServerConfig) -> Result<Value> {
    Ok(match server {
        crate::config::McpServerConfig::Http { url, headers } => {
            let mut entry = serde_json::json!({"type": "http", "url": url});
            if !headers.is_empty() {
                entry["headers"] = serde_json::to_value(headers)?;
            }
            entry
        }
        crate::config::McpServerConfig::Stdio { command, args } => {
            serde_json::json!({"type": "stdio", "command": command, "args": args})
        }
    })
}

fn exomonad_mcp_server(binary_path: &Path, role: &str, name: &str) -> Value {
    serde_json::json!({
        "type": "stdio",
        "command": binary_path.display().to_string(),
        "args": ["mcp-stdio", "--role", role, "--name", name]
    })
}

fn extra_mcp_servers_to_json(
    servers: &std::collections::HashMap<String, crate::config::McpServerConfig>,
) -> Result<std::collections::HashMap<String, Value>> {
    servers
        .iter()
        .map(|(name, server)| Ok((name.clone(), extra_mcp_server_to_json(server)?)))
        .collect()
}

fn write_codex_companion_config(
    config: &Config,
    dir: &Path,
    name: &str,
    role: &str,
    model: Option<&str>,
) -> Result<()> {
    let codex_dir = dir.join(".codex");
    std::fs::create_dir_all(&codex_dir)?;
    let root_instructions;
    let instructions = match role {
        "tl" | "root" => {
            root_instructions = codex_root_instructions(dir, &config.wasm_name);
            &root_instructions
        }
        "worker" => exomonad_core::services::agent_control::CODEX_WORKER_INSTRUCTIONS,
        "reviewer" => exomonad_core::services::agent_control::CODEX_REVIEWER_INSTRUCTIONS,
        _ => exomonad_core::services::agent_control::CODEX_DEV_INSTRUCTIONS,
    };
    let extra_mcp_servers = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
    let configured_effort = config.worker_effort_level.level.to_string();
    let rendered = exomonad_core::codex_config::render_codex_config_with_effort(
        name,
        role,
        instructions,
        model,
        Some(&configured_effort),
        &extra_mcp_servers,
        &exomonad_core::find_exomonad_binary(),
        dir,
    );
    std::fs::write(codex_dir.join("config.toml"), rendered)?;
    Ok(())
}

/// Reject `--tl-model` / `--worker-model` values that opencode doesn't recognise.
/// Caller must only invoke this when the model is `Some` and the agent type is OpenCode.
/// Validate a Claude model string against known aliases and the `claude-*` prefix convention.
///
/// Accepts short aliases ("sonnet", "opus", "haiku") and full model IDs ("claude-sonnet-4-6").
/// Rejects arbitrary strings that match neither pattern — catches typos before a window is opened.
fn validate_claude_model(model: &str) -> Result<()> {
    // Aliases from `claude --help --model`: "sonnet" or "opus"
    const KNOWN_ALIASES: &[&str] = &["sonnet", "opus"];
    let is_alias = KNOWN_ALIASES.contains(&model);
    let is_full_id = model.starts_with("claude-");
    if !is_alias && !is_full_id {
        anyhow::bail!(
            "Unknown Claude model `{model}`. Use a short alias ('sonnet', 'opus') \
             or a full model ID starting with 'claude-' (e.g. 'claude-sonnet-4-6')."
        );
    }
    Ok(())
}

fn parse_opencode_model_catalog(
    text: &str,
) -> std::collections::HashMap<String, std::collections::BTreeSet<String>> {
    let mut catalog = std::collections::HashMap::new();
    let mut json = String::new();
    let mut label = None;

    for line in text.lines() {
        if json.is_empty() {
            let trimmed = line.trim();
            if trimmed.starts_with('{') {
                json.push_str(trimmed);
            } else if !trimmed.is_empty() {
                label = Some(trimmed.to_string());
            }
            continue;
        }

        json.push('\n');
        json.push_str(line);
        let Ok(value) = serde_json::from_str::<Value>(&json) else {
            continue;
        };
        let Some(object) = value.as_object() else {
            json.clear();
            label = None;
            continue;
        };
        let id = object
            .get("id")
            .and_then(Value::as_str)
            .or(label.as_deref());
        let provider = object.get("providerID").and_then(Value::as_str);
        let variants: std::collections::BTreeSet<String> = object
            .get("variants")
            .and_then(Value::as_object)
            .map(|variants| variants.keys().cloned().collect())
            .unwrap_or_default();
        if let Some(id) = id {
            catalog.insert(id.to_string(), variants.clone());
            if let Some(provider) = provider {
                catalog.insert(format!("{provider}/{id}"), variants);
            }
        }
        json.clear();
        label = None;
    }

    catalog
}

async fn validate_opencode_model(model: &str, effort: Option<&str>) -> Result<()> {
    let out = tokio::process::Command::new("opencode")
        .args(["models", "--verbose"])
        .output()
        .await
        .context("Failed to run `opencode models --verbose` for validation")?;
    if !out.status.success() {
        anyhow::bail!(
            "`opencode models --verbose` exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let text = std::str::from_utf8(&out.stdout)?;
    let catalog = parse_opencode_model_catalog(text);
    let Some(variants) = catalog.get(model) else {
        anyhow::bail!("Unknown opencode model `{model}`. Run `exomonad models` to see the list.");
    };
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        if !variants.contains(effort) {
            let supported = if variants.is_empty() {
                "none".to_string()
            } else {
                variants.iter().cloned().collect::<Vec<_>>().join(", ")
            };
            anyhow::bail!(
                "Unsupported OpenCode effort `{effort}` for model `{model}`. Supported variants: {supported}. Correct with the role-specific effort flag."
            );
        }
    }
    Ok(())
}

fn validate_codex_model_name(model: &str) -> Result<()> {
    if !model.starts_with("gpt-") {
        anyhow::bail!(
            "Unknown Codex model `{model}`. Use a Codex/OpenAI model ID starting with `gpt-` \
             (for example `gpt-5.2-codex`)."
        );
    }
    Ok(())
}

async fn validate_codex_model(model: &str, effort: Option<&str>) -> Result<()> {
    validate_codex_model_name(model)?;
    let out = tokio::process::Command::new("codex")
        .args(["debug", "models"])
        .output()
        .await
        .context("Failed to run `codex debug models` for validation")?;
    if !out.status.success() {
        anyhow::bail!(
            "`codex debug models` exited {}: {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let catalog: Value =
        serde_json::from_slice(&out.stdout).context("Codex model catalog was not valid JSON")?;
    let models = catalog
        .get("models")
        .and_then(Value::as_array)
        .context("Codex model catalog did not contain a models array")?;
    let Some(model_record) = models
        .iter()
        .find(|record| record.get("slug").and_then(Value::as_str) == Some(model))
    else {
        anyhow::bail!("Unknown Codex model `{model}`. Run `codex debug models` to see the list.");
    };
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        let supported = model_record
            .get("supported_reasoning_levels")
            .and_then(Value::as_array)
            .map(|levels| {
                levels
                    .iter()
                    .filter_map(|level| level.get("effort").and_then(Value::as_str))
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        if !supported.iter().any(|level| level == effort) {
            let supported = if supported.is_empty() {
                "none".to_string()
            } else {
                supported.join(", ")
            };
            anyhow::bail!(
                "Unsupported Codex effort `{effort}` for model `{model}`. Supported reasoning levels: {supported}. Correct with the role-specific effort flag."
            );
        }
    }
    Ok(())
}

fn validate_opencode_model_owner(
    agent_type: AgentType,
    model: Option<&str>,
    model_field: &str,
    harness_field: &str,
) -> Result<()> {
    if agent_type == AgentType::OpenCode || model.is_none() {
        return Ok(());
    }

    let model = model.expect("checked above");
    anyhow::bail!(
        "{model_field} is set to `{model}`, but {harness_field} is `{}`. \
         OpenCode model fields only apply when the matching harness is `opencode`.",
        agent_type_str(agent_type)
    );
}

async fn validate_reviewer_model_for_harness(
    agent_type: AgentType,
    model: Option<&str>,
    effort: Option<&str>,
) -> Result<()> {
    let Some(model) = model else {
        return Ok(());
    };

    match agent_type {
        AgentType::Claude => validate_claude_model(model),
        AgentType::Codex => validate_codex_model(model, effort).await,
        AgentType::OpenCode => validate_opencode_model(model, effort).await,
        AgentType::Shoal | AgentType::Process => Ok(()),
    }
}

fn reviewer_max_rounds_tmux_args(session: &str, value: Option<u32>) -> Vec<String> {
    let mut args = vec![
        "set-environment".to_string(),
        "-t".to_string(),
        session.to_string(),
    ];
    match value {
        Some(rounds) => {
            args.push(REVIEWER_MAX_ROUNDS_ENV.to_string());
            args.push(rounds.to_string());
        }
        None => {
            args.push("-u".to_string());
            args.push(REVIEWER_MAX_ROUNDS_ENV.to_string());
        }
    }
    args
}

fn set_reviewer_max_rounds_environment(session: &str, value: Option<u32>) -> Result<()> {
    let args = reviewer_max_rounds_tmux_args(session, value);
    let output = std::process::Command::new("tmux")
        .args(&args)
        .output()
        .context("Failed to propagate reviewer round limit to tmux session")?;
    if !output.status.success() {
        anyhow::bail!(
            "tmux set-environment failed for {}: {}",
            REVIEWER_MAX_ROUNDS_ENV,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

fn agent_configuration_environment(config: &Config) -> String {
    let mut parts = Vec::new();
    if let Some(model) = &config.opencode.tl_model {
        parts.push(format!(
            "EXOMONAD_TL_MODEL={}",
            shell_escape::escape(model.clone().into())
        ));
    }
    if let Some(model) = &config.opencode.worker_model {
        parts.push(format!(
            "EXOMONAD_WORKER_MODEL={}",
            shell_escape::escape(model.clone().into())
        ));
    }
    if let Some(model) = &config.reviewer.model {
        parts.push(format!(
            "{}={}",
            REVIEWER_MODEL_ENV,
            shell_escape::escape(model.clone().into())
        ));
    }
    parts.push(format!(
        "{}={}",
        REVIEWER_EFFORT_ENV,
        shell_escape::escape(config.reviewer_effort_level.level.to_string().into())
    ));
    format!(" {}", parts.join(" "))
}

fn ensure_harness_capability(cwd: &Path) -> Result<()> {
    let exo = cwd.join(".exo");
    let policy = exo.join("harness_policy.toml");
    let capability = exo.join("harness_capability.toml");
    if capability.exists() || !policy.exists() {
        return Ok(());
    }
    let policy_text = std::fs::read_to_string(&policy)
        .with_context(|| format!("failed to read {}", policy.display()))?;
    for line in policy_text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with("allow") {
            for entry in trimmed.split('"').skip(1).step_by(2) {
                if !crate::new::HARNESS_CAPABILITY_CONTENT.contains(&format!("\"{entry}\"")) {
                    anyhow::bail!(
                        "cannot backfill {} without widening the policy allowlist; add it explicitly",
                        capability.display()
                    );
                }
            }
        }
    }
    std::fs::write(&capability, crate::new::HARNESS_CAPABILITY_CONTENT)
        .with_context(|| format!("failed to backfill {}", capability.display()))?;
    info!(path = %capability.display(), "Backfilled harness capability map from the canonical default");
    Ok(())
}

fn tl_loop_python_with<F>(env: F) -> String
where
    F: Fn(&str) -> Option<String>,
{
    let environment = TL_LOOP_INTERPRETER_POLICY
        .lines()
        .find_map(|line| line.strip_prefix("environment = "))
        .map(|value| value.trim_matches('"'))
        .unwrap_or("EXOMONAD_TL_LOOP_PYTHON");
    if let Some(interpreter) = env(environment) {
        if !interpreter.trim().is_empty() {
            return interpreter;
        }
    }
    TL_LOOP_INTERPRETER_POLICY
        .lines()
        .find_map(|line| line.strip_prefix("fallback = "))
        .map(|value| value.trim_matches('"').to_string())
        .unwrap_or_else(|| "python3".to_string())
}

fn tl_loop_python(_cwd: &Path) -> String {
    tl_loop_python_with(|name| std::env::var(name).ok())
}

fn tl_loop_required_python() -> Result<(u32, u32)> {
    let requirement = TL_LOOP_PYPROJECT
        .lines()
        .find_map(|line| line.strip_prefix("requires-python = "))
        .and_then(|value| value.trim_matches('"').strip_prefix(">="))
        .context("tl_loop/pyproject.toml must declare requires-python >= a version")?;
    let mut parts = requirement.split('.');
    let major = parts
        .next()
        .context("requires-python is missing a major version")?
        .parse()
        .context("requires-python has an invalid major version")?;
    let minor = parts
        .next()
        .context("requires-python is missing a minor version")?
        .parse()
        .context("requires-python has an invalid minor version")?;
    Ok((major, minor))
}

fn validate_tl_loop_python_version(found: (u32, u32, u32), required: (u32, u32)) -> Result<()> {
    if (found.0, found.1) < required {
        anyhow::bail!(
            "TL controller requires Python >= {}.{}; resolved interpreter is Python {}.{}.{}",
            required.0,
            required.1,
            found.0,
            found.1,
            found.2
        );
    }
    Ok(())
}

fn check_tl_loop_python(cwd: &Path) -> Result<String> {
    let interpreter = tl_loop_python(cwd);
    let output = std::process::Command::new(&interpreter)
        .args([
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ])
        .output()
        .with_context(|| format!("failed to resolve TL controller interpreter `{interpreter}`"))?;
    if !output.status.success() {
        anyhow::bail!(
            "resolved TL controller interpreter `{interpreter}` could not report its version"
        );
    }
    let version = String::from_utf8_lossy(&output.stdout);
    let mut parts = version.trim().split('.');
    let found = (
        parts
            .next()
            .context("interpreter reported no major version")?
            .parse()?,
        parts
            .next()
            .context("interpreter reported no minor version")?
            .parse()?,
        parts
            .next()
            .context("interpreter reported no patch version")?
            .parse()?,
    );
    let required = tl_loop_required_python()?;
    validate_tl_loop_python_version(found, required)?;
    info!(
        interpreter = %interpreter,
        version = %version.trim(),
        required = %format!("{}.{}", required.0, required.1),
        "Resolved TL controller interpreter"
    );
    Ok(interpreter)
}

fn run_tl_loop_preflight(
    cwd: &Path,
    package_root: &Path,
    allow_missing_plan: bool,
    expected_plan_digest: Option<&str>,
) -> Result<()> {
    let interpreter = check_tl_loop_python(cwd)?;
    let mut command = std::process::Command::new(interpreter);
    command.args([
        package_root
            .to_str()
            .context("TL archive path is not UTF-8")?,
        "preflight",
        "--project-root",
    ]);
    command.arg(cwd);
    if allow_missing_plan {
        command.arg("--allow-missing-plan");
    }
    if let Some(plan_digest) = expected_plan_digest {
        command.args(["--expected-plan-digest", plan_digest]);
    }
    let status = command
        .status()
        .context("failed to run TL controller preflight")?;
    if !status.success() {
        anyhow::bail!("TL controller preflight failed with {status}");
    }
    Ok(())
}

fn plan_digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub fn record_plan_snapshot_from_stdin(project_dir: &Path, expected_digest: &str) -> Result<()> {
    use std::io::Read;

    let mut plan_bytes = Vec::new();
    std::io::stdin()
        .read_to_end(&mut plan_bytes)
        .context("failed to read accepted plan bytes")?;
    record_plan_snapshot_bytes(project_dir, &plan_bytes, expected_digest)
}

fn record_plan_snapshot_bytes(
    project_dir: &Path,
    plan_bytes: &[u8],
    expected_digest: &str,
) -> Result<()> {
    let actual_digest = plan_digest(plan_bytes);
    if actual_digest != expected_digest {
        anyhow::bail!(
            "accepted plan digest {actual_digest} does not match expected identity {expected_digest}"
        );
    }

    let _lock = acquire_plan_transition_lock(project_dir)?;
    if let Some((phase, archive_name)) = read_plan_transition(project_dir)? {
        let journal_path = plan_transition_path(project_dir);
        let archive = archive_name
            .map(|name| format!(" archive {name}"))
            .unwrap_or_default();
        anyhow::bail!(
            "cannot record recreate plan identity while plan transition is in progress at {}: phase {phase}{archive}",
            journal_path.display(),
        );
    }
    let previous_snapshot = read_plan_snapshot_bytes(project_dir)?;
    let previous_digest = read_plan_snapshot_digest(project_dir)?;
    if let Some(snapshot) = previous_snapshot.as_deref() {
        if snapshot != plan_bytes {
            anyhow::bail!("accepted plan differs from its immutable session snapshot");
        }
        return match previous_digest.as_deref() {
            Some(digest) if digest == expected_digest => Ok(()),
            Some(_) => anyhow::bail!("plan snapshot identity differs from its immutable bytes"),
            None => write_plan_snapshot_digest(project_dir, expected_digest),
        };
    }
    if previous_digest.is_some() {
        anyhow::bail!("cannot record plan identity without its snapshot");
    }

    let mut failure = None;
    begin_plan_transition_locked(project_dir, None, None, None)?;
    if let Err(error) = apply_plan_snapshot_locked(
        project_dir,
        Some(plan_bytes),
        Some(expected_digest),
        &mut failure,
    ) {
        return Err(rollback_plan_transition_locked(
            project_dir,
            error,
            &mut failure,
        ));
    }
    if let Err(error) = write_plan_transition(project_dir, PLAN_TRANSITION_PHASE_ARCHIVED, None) {
        return Err(rollback_plan_transition_locked(
            project_dir,
            error,
            &mut failure,
        ));
    }
    clear_plan_transition_locked(project_dir, &mut failure)
}

fn validate_publication_registry_schema(cwd: &Path) -> Result<()> {
    let path = cwd.join(".exo/published-heads.json");
    if !path.is_file() {
        return Ok(());
    }
    let contents = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read publication registry {}", path.display()))?;
    let document: Value = serde_json::from_str(&contents)
        .with_context(|| format!("invalid publication registry {}", path.display()))?;
    let object = document.as_object().with_context(|| {
        format!(
            "publication registry {} must be a JSON object",
            path.display()
        )
    })?;
    let schema = object
        .get("schema_version")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    if schema > u64::from(PUBLICATION_REGISTRY_SCHEMA_VERSION) {
        anyhow::bail!(
            "publication registry {} uses unsupported schema version {}; supported through {}. Upgrade ExoMonad before restarting; --recreate does not migrate runtime artifacts",
            path.display(),
            schema,
            PUBLICATION_REGISTRY_SCHEMA_VERSION,
        );
    }
    Ok(())
}

fn validate_runtime_builds(server_build: &str, controller_build: &str) -> Result<()> {
    if server_build == "unknown" || controller_build == "unknown" {
        anyhow::bail!(
            "incompatible ExoMonad runtime artifacts: unknown ExoMonad build identity (server_build={}, controller_build={}). Rebuild and install synchronized artifacts with just install-all-dev; the managed project revision is unrelated",
            server_build,
            controller_build,
        );
    }
    if server_build != controller_build {
        anyhow::bail!(
            "incompatible ExoMonad runtime artifacts: server/controller build identities differ (server_build={}, controller_build={}). Rebuild and install synchronized artifacts with just install-all-dev; the managed project revision is unrelated",
            server_build,
            controller_build,
        );
    }
    Ok(())
}

fn validate_runtime_contract(
    protocol_version: u32,
    publication_registry_schema: u32,
) -> Result<()> {
    if protocol_version != RUNTIME_PROTOCOL_VERSION {
        anyhow::bail!(
            "unsupported ExoMonad runtime protocol version {}; supported version is {}. Rebuild and install synchronized artifacts with just install-all-dev",
            protocol_version,
            RUNTIME_PROTOCOL_VERSION,
        );
    }
    if publication_registry_schema > PUBLICATION_REGISTRY_SCHEMA_VERSION {
        anyhow::bail!(
            "unsupported publication registry schema version {}; supported through {}. Upgrade ExoMonad before restarting",
            publication_registry_schema,
            PUBLICATION_REGISTRY_SCHEMA_VERSION,
        );
    }
    Ok(())
}

fn validate_runtime_compatibility(cwd: &Path) -> Result<()> {
    validate_runtime_builds(EXOMONAD_BUILD_GIT_COMMIT, EXOMONAD_TL_LOOP_GIT_COMMIT)?;
    validate_runtime_contract(
        RUNTIME_PROTOCOL_VERSION,
        PUBLICATION_REGISTRY_SCHEMA_VERSION,
    )?;
    validate_publication_registry_schema(cwd)?;
    info!(
        server_build = EXOMONAD_BUILD_GIT_COMMIT,
        controller_build = EXOMONAD_TL_LOOP_GIT_COMMIT,
        protocol_version = RUNTIME_PROTOCOL_VERSION,
        publication_registry_schema = PUBLICATION_REGISTRY_SCHEMA_VERSION,
        "Validated ExoMonad runtime compatibility"
    );
    Ok(())
}

fn tl_loop_package_root() -> Result<PathBuf> {
    let home = std::env::var("HOME").context("HOME is required to install the TL controller")?;
    tl_loop_package_root_at(&PathBuf::from(home).join(".exo"))
}

fn tl_loop_package_root_at(install_dir: &Path) -> Result<PathBuf> {
    let archive = install_dir.join("tl_loop.pyz");
    if !archive.is_file() {
        anyhow::bail!(
            "installed TL controller archive is missing at {}; run just install-all-dev before restarting",
            archive.display()
        );
    }
    Ok(archive)
}

fn write_tl_loop_plan(cwd: &Path, initial_prompt: Option<&str>) -> Result<()> {
    let Some(prompt) = initial_prompt
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return Ok(());
    };
    let value = serde_json::from_str::<Value>(prompt)
        .context("initial_prompt must be a JSON WorkPlan document for the programmatic TL")?;
    if !value.is_object() {
        anyhow::bail!("initial_prompt must be a JSON object containing the TL WorkPlan");
    }
    let plan_path = cwd.join(".exo/tl-loop/plan.json");
    if plan_path.exists() {
        return Ok(());
    }
    if let Some(parent) = plan_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(plan_path, serde_json::to_string_pretty(&value)?)?;
    info!("Wrote structured TL plan from initial_prompt");
    Ok(())
}

fn write_tl_loop_identity(cwd: &Path, branch: &str) -> Result<()> {
    let agent_dir = cwd.join(".exo/agents/root");
    std::fs::create_dir_all(&agent_dir)?;
    let identity = serde_json::json!({
        "agent_name": "root",
        "slug": "root",
        "agent_type": "codex",
        "birth_branch": branch,
        "parent_branch": branch,
        "working_dir": ".",
        "display_name": "TL loop",
        "topology": "shared_dir",
        "model": null,
        "effort": null,
        "ledger_owned": true,
    });
    std::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_string_pretty(&identity)?,
    )?;
    Ok(())
}

/// Bundled TL-loop timeout overrides threaded into the controller launch command.
struct TlLoopTimeouts {
    transport: f64,
    active_tail: f64,
    task: f64,
}

impl From<&Config> for TlLoopTimeouts {
    fn from(config: &Config) -> Self {
        Self {
            transport: config.tl_transport_timeout_seconds,
            active_tail: config.tl_active_tail_timeout_seconds,
            task: config.tl_task_timeout_seconds,
        }
    }
}

fn tl_loop_command(
    cwd: &Path,
    package_root: &Path,
    timeouts: &TlLoopTimeouts,
    expected_plan_digest: Option<&str>,
) -> String {
    let binary = shell_escape::escape(
        exomonad_core::find_exomonad_binary()
            .display()
            .to_string()
            .into(),
    );
    let package = shell_escape::escape(package_root.display().to_string().into());
    let project = shell_escape::escape(cwd.display().to_string().into());
    let plan = shell_escape::escape(
        cwd.join(".exo/tl-loop/plan.json")
            .display()
            .to_string()
            .into(),
    );
    let expected_plan_arg = expected_plan_digest
        .map(|digest| {
            format!(
                " --expected-plan-digest {}",
                shell_escape::escape(digest.into())
            )
        })
        .unwrap_or_default();
    let controller = format!(
        "EXOMONAD_BINARY={binary} EXOMONAD_AGENT_ID=root EXOMONAD_ROLE=tl {} {package} run --project-root {project} --plan {plan} --run-id root --wait-for-plan{expected_plan_arg} \
           --transport-timeout {} --active-tail-timeout {} --task-timeout {}",
        shell_escape::escape(tl_loop_python(cwd).into()),
        timeouts.transport,
        timeouts.active_tail,
          timeouts.task,
    );
    tl_controller_wrapper_command(cwd, &controller)
}

fn tl_controller_wrapper_command(cwd: &Path, controller: &str) -> String {
    let exit_marker = shell_escape::escape(controller_exit_path(cwd).display().to_string().into());
    let output_log = shell_escape::escape(controller_output_path(cwd).display().to_string().into());
    format!(
        "{{ {controller}; status=$?; if [ \"$status\" -ne 0 ] || [ -f {exit_marker} ]; then \
             tmux set-window-option -t \"${{TMUX_PANE}}\" remain-on-exit on 2>/dev/null || true; \
             printf '\\nTL controller exited unexpectedly (status %s).\\n' \"$status\"; \
             if [ -f {exit_marker} ]; then \
                 printf '%s\\n' 'Controller failure marker:' {exit_marker}; \
                 cat {exit_marker}; \
             else \
                 printf '%s %s.\\n' 'Controller failure reason: exit status' \"$status\"; \
             fi; \
             printf '%s\\n' 'Controller output log:' {output_log}; \
         fi; exit \"$status\"; }}"
    )
}

fn server_command(session: &str, config: &Config, verbose: bool) -> String {
    let model_env = agent_configuration_environment(config);
    let verbose_prefix = if verbose {
        "RUST_LOG=info EXOMONAD_HOOK_TRACE=1 EXOMONAD_CHAINLINK_TRACE=1 "
    } else {
        ""
    };
    let binary = shell_escape::escape(
        exomonad_core::find_exomonad_binary()
            .display()
            .to_string()
            .into(),
    );
    format!(
        "{}EXOMONAD_TMUX_SESSION={} EXOMONAD_ROOT_AGENT_TYPE={} EXOMONAD_SPAWN_AGENT_TYPE={} EXOMONAD_REVIEWER_AGENT_TYPE={}{} {} serve",
        verbose_prefix,
        session,
        agent_type_str(config.root_agent_type),
        agent_type_str(config.spawn_agent_type),
        agent_type_str(config.reviewer.agent_type),
        model_env,
        binary,
    )
}

fn root_tl_needs_resume(project_dir: &Path) -> Result<bool> {
    let run_path = project_dir.join(".exo/tl-loop/root/run.json");
    if !run_path.exists() {
        return Ok(true);
    }
    let payload = std::fs::read_to_string(&run_path)
        .with_context(|| format!("failed to read {}", run_path.display()))?;
    let value: Value = serde_json::from_str(&payload)
        .with_context(|| format!("invalid TL checkpoint {}", run_path.display()))?;
    let phase = value
        .pointer("/fsm/phase")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            anyhow::anyhow!(
                "unsupported TL checkpoint {}: missing string /fsm/phase",
                run_path.display()
            )
        })?;
    match phase {
        "tl_done" | "tl_failed" => Ok(false),
        "tl_planning" | "tl_dispatching" | "tl_waiting" | "tl_merging" | "tl_all_merged"
        | "tl_pr_filed" => Ok(true),
        other => Err(anyhow::anyhow!(
            "unsupported TL checkpoint {}: unknown phase '{other}'",
            run_path.display()
        )),
    }
}

#[derive(Debug, PartialEq, Eq)]
enum StartupCheckpoint {
    Missing,
    Nonterminal {
        phase: String,
    },
    TerminalOrParked {
        phase: String,
        pending_gates: Vec<String>,
        dispatch_errors: Vec<(String, String)>,
    },
}

fn read_startup_checkpoint(project_dir: &Path) -> Result<StartupCheckpoint> {
    let run_path = project_dir.join(".exo/tl-loop/root/run.json");
    if !run_path.exists() {
        return Ok(StartupCheckpoint::Missing);
    }
    let payload = std::fs::read_to_string(&run_path)
        .with_context(|| format!("failed to read {}", run_path.display()))?;
    let value: Value = serde_json::from_str(&payload)
        .with_context(|| format!("invalid TL checkpoint {}", run_path.display()))?;
    let phase = value
        .pointer("/fsm/phase")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            anyhow::anyhow!(
                "unsupported TL checkpoint {}: missing string /fsm/phase",
                run_path.display()
            )
        })?;
    let pending_gates = value
        .get("gates")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|gate| gate.get("status").and_then(Value::as_str) == Some("pending"))
        .filter_map(|gate| {
            gate.get("name")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .collect::<Vec<_>>();
    let dispatch_errors = value
        .get("slices")
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(|slices| slices.iter())
        .filter_map(|(slice_id, slice)| {
            slice
                .get("dispatch_error")
                .and_then(Value::as_str)
                .filter(|error| !error.is_empty())
                .map(|error| (slice_id.clone(), error.to_string()))
        })
        .collect::<Vec<_>>();
    if matches!(phase, "tl_done" | "tl_failed") {
        return Ok(StartupCheckpoint::TerminalOrParked {
            phase: phase.to_string(),
            pending_gates,
            dispatch_errors,
        });
    }
    Ok(StartupCheckpoint::Nonterminal {
        phase: phase.to_string(),
    })
}

fn pending_gate_names(project_dir: &Path) -> Result<Option<Vec<String>>> {
    match read_startup_checkpoint(project_dir)? {
        StartupCheckpoint::TerminalOrParked { pending_gates, .. } if !pending_gates.is_empty() => {
            Ok(Some(pending_gates))
        }
        _ => Ok(None),
    }
}

fn ensure_recreate_allowed(project_dir: &Path, allow_pending_gate: bool) -> Result<()> {
    if allow_pending_gate {
        return Ok(());
    }
    if let Some(gates) = pending_gate_names(project_dir)? {
        anyhow::bail!(
            "refusing --recreate: unanswered human gate(s) {} would be archived; pass --allow-pending-gate to override",
            gates.join(", ")
        );
    }
    Ok(())
}

fn startup_checkpoint_message(checkpoint: &StartupCheckpoint) -> Option<String> {
    let StartupCheckpoint::TerminalOrParked {
        phase,
        pending_gates,
        dispatch_errors,
    } = checkpoint
    else {
        return None;
    };
    let mut message = format!("TL controller completed with durable checkpoint phase={phase}.");
    for gate in pending_gates {
        message.push_str(&format!(
            "\nHuman gate pending: {gate}. Answer with `python3 -m tl_loop gate --run-id root --name {gate} --approve|--reject`."
        ));
    }
    for (slice_id, error) in dispatch_errors {
        message.push_str(&format!("\nSlice {slice_id} dispatch_error: {error}"));
    }
    Some(message)
}

fn record_startup_checkpoint_classification(
    project_dir: &Path,
    checkpoint: &StartupCheckpoint,
) -> Result<()> {
    let Some(message) = startup_checkpoint_message(checkpoint) else {
        return Ok(());
    };
    let StartupCheckpoint::TerminalOrParked {
        phase,
        pending_gates,
        ..
    } = checkpoint
    else {
        unreachable!();
    };
    let path = project_dir.join(".exo/tl-loop/root/startup-classification.json");
    let parent = path
        .parent()
        .context("startup classification has no parent")?;
    std::fs::create_dir_all(parent)?;
    let payload = serde_json::json!({
        "classification": "terminal_or_parked",
        "phase": phase,
        "pending_gates": pending_gates,
        "message": message,
        "recorded_at": current_time_millis() as f64 / 1000.0,
    });
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, serde_json::to_string_pretty(&payload)?)?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

fn archive_controller_exit_reason(project_dir: &Path) -> Result<Option<PathBuf>> {
    let source = controller_exit_path(project_dir);
    if !source.exists() {
        return Ok(None);
    }
    let parent = source
        .parent()
        .context("controller exit marker has no parent directory")?;
    let stamp = current_time_millis();
    let mut suffix = 0u32;
    loop {
        let name = if suffix == 0 {
            format!("controller-exit-{stamp}.json")
        } else {
            format!("controller-exit-{stamp}-{suffix}.json")
        };
        let archive = parent.join(name);
        if archive.exists() {
            suffix = suffix
                .checked_add(1)
                .context("too many controller exit marker collisions")?;
            continue;
        }
        std::fs::rename(&source, &archive).with_context(|| {
            format!(
                "failed to archive controller exit marker {}",
                source.display()
            )
        })?;
        return Ok(Some(archive));
    }
}

async fn server_socket_is_healthy(project_dir: &Path) -> bool {
    let socket_path = project_dir.join(".exo/server.sock");
    if !socket_path.exists() {
        return false;
    }
    uds_client::ServerClient::new(socket_path)
        .is_healthy()
        .await
}

async fn launch_server_recovery(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    shell: &str,
    session: &str,
    config: &Config,
    verbose: bool,
) -> Result<()> {
    prepare_server_socket_for_start(project_dir)?;
    let server_window = ipc
        .new_window(
            "Server",
            project_dir,
            shell,
            &server_command(session, config, verbose),
        )
        .await?;
    ipc.set_window_remain_on_exit(&server_window, true).await?;
    wait_for_server_socket(project_dir).await?;
    report_observability_health(project_dir);
    Ok(())
}

async fn launch_tl_recovery(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    shell: &str,
    tl_loop_root: &Path,
    config: &Config,
    controller_plan_digest: Option<&str>,
) -> Result<()> {
    if let Some(expected_digest) = controller_plan_digest {
        ensure_snapshot_digest_matches(project_dir, expected_digest)?;
    }
    let controller_epoch = prepare_controller_spawn(project_dir)?;
    let tl_window = ipc
        .new_window(
            "TL",
            project_dir,
            shell,
            &tl_loop_command(
                project_dir,
                tl_loop_root,
                &TlLoopTimeouts::from(config),
                controller_plan_digest,
            ),
        )
        .await?;
    ipc.set_window_remain_on_exit(&tl_window, true).await?;
    let startup =
        wait_for_tl_controller_startup(ipc, project_dir, &tl_window, &controller_epoch).await;
    if startup.is_ok() {
        ipc.set_window_remain_on_exit(&tl_window, false).await?;
    }
    startup
}

/// Serializes concurrent `exomonad init` invocations against the same
/// project so two processes never race between inspecting session health
/// and creating repair windows. The lock target need not exist; `FileLock`
/// only ever touches the sibling `.lock` path.
async fn acquire_init_lifecycle_lock_async(
    project_dir: &Path,
) -> Result<claude_teams_bridge::file_lock::FileLock> {
    let project_dir = project_dir.to_path_buf();
    tokio::task::spawn_blocking(move || acquire_init_lifecycle_lock(&project_dir))
        .await
        .context("init lifecycle lock task panicked")?
}

async fn acquire_plan_transition_lock_async(
    project_dir: &Path,
) -> Result<claude_teams_bridge::file_lock::FileLock> {
    let project_dir = project_dir.to_path_buf();
    tokio::task::spawn_blocking(move || acquire_plan_transition_lock(&project_dir))
        .await
        .context("plan transition lock task panicked")?
}

#[allow(clippy::too_many_arguments)]
async fn reconcile_existing_session(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    shell: &str,
    session: &str,
    config: &Config,
    tl_loop_root: &Path,
    verbose: bool,
    restart_tl: bool,
    controller_plan_digest: Option<&str>,
) -> Result<()> {
    // Held for the whole inspect-then-create sequence below so a second
    // concurrent `init` cannot observe the same dead window and race to
    // create a duplicate repair window.
    let windows = ipc.list_windows().await?;
    let server_window = windows.iter().find(|window| window.window_name == "Server");
    let server_healthy = server_socket_is_healthy(project_dir).await;
    if !server_healthy {
        let server_alive = match server_window {
            Some(window) => {
                let routing = exomonad_core::domain::RoutingInfo::window(window.window_id.clone());
                ipc.routing_target_process_alive(&routing).await?
            }
            None => false,
        };
        if server_alive {
            wait_for_server_socket(project_dir).await?;
        } else {
            if let Some(window) = server_window {
                info!(window = %window.window_id, "Server window is dead, removing before recovery");
                ipc.kill_window(&window.window_id).await?;
            }
            launch_server_recovery(ipc, project_dir, shell, session, config, verbose).await?;
        }
    }

    ensure_watcher_dashboard_window(ipc, project_dir, shell).await?;

    let windows = ipc.list_windows().await?;
    let tl_window = windows.iter().find(|window| window.window_name == "TL");
    let tl_alive = match tl_window {
        Some(window) => {
            let routing = exomonad_core::domain::RoutingInfo::window(window.window_id.clone());
            ipc.routing_target_process_alive(&routing).await?
        }
        None => false,
    };
    let root_needs_resume = root_tl_needs_resume(project_dir)?;
    if matches!(
        tl_window_recovery_action(restart_tl, tl_alive, root_needs_resume),
        TlWindowRecoveryAction::Keep
    ) {
        return Ok(());
    }

    if let Some(window) = tl_window {
        info!(
            window = %window.window_id,
            restart_tl,
            "Removing TL window before recovery"
        );
        ipc.kill_window(&window.window_id).await?;
    }

    launch_tl_recovery(
        ipc,
        project_dir,
        shell,
        tl_loop_root,
        config,
        controller_plan_digest,
    )
    .await
}

fn controller_exit_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/root/controller-exit.json")
}

fn controller_output_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/root/controller-output.log")
}

#[cfg(test)]
fn controller_exit_reason(project_dir: &Path) -> Option<String> {
    let payload = std::fs::read_to_string(controller_exit_path(project_dir)).ok()?;
    let value = serde_json::from_str::<Value>(&payload).ok()?;
    render_controller_exit_reason(&value)
}

fn controller_epoch_path(project_dir: &Path) -> PathBuf {
    project_dir.join(".exo/tl-loop/root.controller-epoch")
}

#[cfg(test)]
fn read_controller_epoch(project_dir: &Path) -> Option<String> {
    std::fs::read_to_string(controller_epoch_path(project_dir))
        .ok()
        .map(|epoch| epoch.trim().to_string())
        .filter(|epoch| !epoch.is_empty())
}

fn render_controller_exit_reason(value: &Value) -> Option<String> {
    let reason = value
        .get("reason")
        .and_then(Value::as_str)
        .filter(|reason| !reason.is_empty())
        .map(ToString::to_string)?;
    let output = value
        .get("recent_output")
        .and_then(Value::as_str)
        .filter(|output| !output.is_empty());
    Some(match output {
        Some(output) => format!("{reason}; recent controller output: {output}"),
        None => reason,
    })
}

fn controller_exit_reason_for_attempt(
    project_dir: &Path,
    controller_epoch: &str,
) -> Result<Option<String>> {
    let path = controller_exit_path(project_dir);
    let payload = match std::fs::read_to_string(&path) {
        Ok(payload) => payload,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error).with_context(|| format!("failed to read {}", path.display()));
        }
    };
    let value = match serde_json::from_str::<Value>(&payload) {
        Ok(value) => value,
        Err(error) => {
            if let Some(archive) = archive_controller_exit_reason(project_dir)? {
                info!(
                    archive = %archive.display(),
                    %error,
                    "Archived invalid controller exit marker before a new spawn attempt"
                );
            }
            return Ok(None);
        }
    };
    let marker_epoch = value.get("controller_epoch").and_then(Value::as_str);
    if marker_epoch != Some(controller_epoch) {
        if let Some(archive) = archive_controller_exit_reason(project_dir)? {
            info!(
                archive = %archive.display(),
                expected_epoch = controller_epoch,
                marker_epoch = ?marker_epoch,
                "Archived controller exit marker from an earlier spawn attempt"
            );
        }
        return Ok(None);
    }
    Ok(render_controller_exit_reason(&value))
}

#[cfg(test)]
fn clear_controller_exit_reason(project_dir: &Path) -> Result<()> {
    let path = controller_exit_path(project_dir);
    if path.exists() {
        std::fs::remove_file(&path)
            .with_context(|| format!("failed to clear {}", path.display()))?;
    }
    Ok(())
}

fn write_controller_epoch(project_dir: &Path) -> Result<String> {
    let marker = controller_epoch_path(project_dir);
    let epoch = format!(
        "controller-{}-{}",
        current_time_millis(),
        std::process::id()
    );
    if let Some(parent) = marker.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("failed to create {}", parent.display()))?;
    }
    std::fs::write(&marker, format!("{epoch}\n"))
        .with_context(|| format!("failed to write {}", marker.display()))?;
    info!(path = %marker.display(), %epoch, "Wrote new controller spawn epoch");
    Ok(epoch)
}

fn prepare_controller_spawn(project_dir: &Path) -> Result<String> {
    let controller_epoch = write_controller_epoch(project_dir)?;
    // A marker without this epoch belongs to an older controller process. Archive it
    // before creating the new window so the first liveness poll cannot misattribute it.
    let _ = controller_exit_reason_for_attempt(project_dir, &controller_epoch)?;
    Ok(controller_epoch)
}

fn root_archive_path_at(project_dir: &Path, timestamp_ms: u128) -> Result<Option<PathBuf>> {
    let root_dir = project_dir.join(".exo/tl-loop/root");
    if !root_dir.exists() {
        return Ok(None);
    }
    let parent = root_dir
        .parent()
        .context("TL root checkpoint has no parent directory")?;
    let stem = format!("root.invalid-{timestamp_ms}");
    let mut suffix = 0u32;
    loop {
        let name = if suffix == 0 {
            stem.clone()
        } else {
            format!("{stem}-{suffix}")
        };
        let archive = parent.join(name);
        if archive.exists() {
            suffix = suffix
                .checked_add(1)
                .context("too many TL root checkpoint archive collisions")?;
            continue;
        }
        return Ok(Some(archive));
    }
}

#[cfg(test)]
fn archive_root_tl_run_at(project_dir: &Path, timestamp_ms: u128) -> Result<Option<PathBuf>> {
    let Some(archive) = root_archive_path_at(project_dir, timestamp_ms)? else {
        return Ok(None);
    };
    archive_root_tl_run_to(project_dir, &archive)?;
    Ok(Some(archive))
}

fn archive_root_tl_run_to(project_dir: &Path, archive: &Path) -> Result<()> {
    let root_dir = project_dir.join(".exo/tl-loop/root");
    if !root_dir.is_dir() {
        anyhow::bail!(
            "cannot recreate TL run: expected {} to be a directory",
            root_dir.display()
        );
    }
    match std::fs::rename(&root_dir, archive) {
        Ok(()) => {
            info!(
                source = %root_dir.display(),
                archive = %archive.display(),
                "Archived prior TL root checkpoint for a new run"
            );
            Ok(())
        }
        Err(error) if archive.exists() => anyhow::bail!(
            "failed to archive prior TL root checkpoint {}: archive {} already exists ({error})",
            root_dir.display(),
            archive.display()
        ),
        Err(error) => Err(error).with_context(|| {
            format!(
                "failed to archive prior TL root checkpoint {}",
                root_dir.display()
            )
        }),
    }
}

#[cfg(test)]
fn record_controller_exit_reason(project_dir: &Path, reason: &str) -> Result<()> {
    let controller_epoch = read_controller_epoch(project_dir);
    record_controller_exit_reason_for_attempt(project_dir, reason, controller_epoch.as_deref())
}

fn record_controller_exit_reason_for_attempt(
    project_dir: &Path,
    reason: &str,
    controller_epoch: Option<&str>,
) -> Result<()> {
    let path = controller_exit_path(project_dir);
    if path.exists() {
        return Ok(());
    }
    let parent = path
        .parent()
        .context("controller exit path has no parent directory")?;
    std::fs::create_dir_all(parent)?;
    let mut payload = serde_json::json!({
        "reason": reason,
        "recorded_at": current_time_millis() as f64 / 1000.0,
        "source": "exomonad init",
    });
    if let Some(controller_epoch) = controller_epoch {
        payload["controller_epoch"] = Value::String(controller_epoch.to_string());
    }
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, serde_json::to_string(&payload)?)?;
    std::fs::rename(&temporary, &path)?;
    Ok(())
}

async fn wait_for_tl_controller_startup(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    window_id: &exomonad_core::services::tmux_ipc::WindowId,
    controller_epoch: &str,
) -> Result<()> {
    let deadline = Instant::now() + TL_CONTROLLER_STARTUP_TIMEOUT;
    let routing = exomonad_core::domain::RoutingInfo::window(window_id.clone());
    let mut observed_alive = false;

    loop {
        let process_alive = ipc.routing_target_process_alive(&routing).await?;
        if process_alive {
            observed_alive = true;
        } else {
            match read_startup_checkpoint(project_dir) {
                Ok(checkpoint @ StartupCheckpoint::TerminalOrParked { .. }) => {
                    if let Some(message) = startup_checkpoint_message(&checkpoint) {
                        println!("{message}");
                        if let Err(error) =
                            record_startup_checkpoint_classification(project_dir, &checkpoint)
                        {
                            warn!(%error, "Failed to persist TL startup classification");
                        }
                        return Ok(());
                    }
                }
                Ok(StartupCheckpoint::Missing) => {
                    debug!(
                        project_dir = %project_dir.display(),
                        "No durable TL checkpoint found while classifying startup exit"
                    );
                }
                Ok(StartupCheckpoint::Nonterminal { phase }) => {
                    debug!(phase, "TL startup exited with a nonterminal checkpoint");
                }
                Err(error) => {
                    warn!(%error, "Failed to read TL startup checkpoint; retaining diagnostic");
                }
            }
            if let Some(reason) = controller_exit_reason_for_attempt(project_dir, controller_epoch)?
            {
                return tl_controller_startup_failure(
                    ipc,
                    project_dir,
                    window_id,
                    controller_epoch,
                    reason,
                )
                .await;
            }
            if observed_alive {
                return tl_controller_startup_failure(
                    ipc,
                    project_dir,
                    window_id,
                    controller_epoch,
                    format!("tmux window {window_id} exited before TL startup completed"),
                )
                .await;
            }
        }

        if process_alive {
            if let Some(reason) = controller_exit_reason_for_attempt(project_dir, controller_epoch)?
            {
                return tl_controller_startup_failure(
                    ipc,
                    project_dir,
                    window_id,
                    controller_epoch,
                    reason,
                )
                .await;
            }
        }

        if Instant::now() >= deadline {
            if observed_alive {
                return Ok(());
            }
            return tl_controller_startup_failure(
                ipc,
                project_dir,
                window_id,
                controller_epoch,
                format!("tmux window {window_id} never became live during TL startup"),
            )
            .await;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

async fn tl_controller_startup_failure(
    ipc: &exomonad_core::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    window_id: &exomonad_core::services::tmux_ipc::WindowId,
    controller_epoch: &str,
    fallback_reason: String,
) -> Result<()> {
    let reason = match controller_exit_reason_for_attempt(project_dir, controller_epoch)? {
        Some(reason) => reason,
        None => match ipc.capture_pane(window_id.as_str()).await {
            Ok(output) => startup_failure_with_pane_output(fallback_reason, &output),
            Err(error) => {
                format!("{fallback_reason}; unable to capture controller output: {error}")
            }
        },
    };
    if let Err(error) =
        record_controller_exit_reason_for_attempt(project_dir, &reason, Some(controller_epoch))
    {
        warn!(%error, "Failed to persist TL controller startup failure");
    }
    anyhow::bail!("TL controller failed during startup in tmux window {window_id}: {reason}");
}

fn startup_failure_with_pane_output(fallback_reason: String, output: &str) -> String {
    let lines: Vec<&str> = output
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    let tail = lines
        .into_iter()
        .rev()
        .take(8)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>();
    if tail.is_empty() {
        return format!("{fallback_reason}; controller produced no captured output");
    }
    let captured = tail.join(" | ");
    format!("{fallback_reason}; controller output: {captured}")
}

/// Run the init command: create or attach to tmux session.
// The CLI exposes these independent initialization options as separate flags.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    session_override: Option<String>,
    mode: SessionMode,
    allow_pending_gate: bool,
    confirm_recreate: bool,
    force_recreate: bool,
    recreate_dry_run: bool,
    openrouter: bool,
    worker: Option<String>,
    worker_model: Option<String>,
    worker_effort_level: Option<EffortLevel>,
    reviewer_effort_level: Option<EffortLevel>,
    reviewer: Option<String>,
    reviewer_model: Option<String>,
    reviewer_max_rounds: Option<u32>,
    verbose: bool,
    skip_preflight: bool,
    set_git_remote: Option<String>,
    reset_inbox: bool,
    import_legacy: Vec<PathBuf>,
    import_legacy_dry_run: bool,
) -> Result<()> {
    use exomonad_core::services::tmux_ipc::TmuxIpc;
    use exomonad_core::services::{resolve_role_context_path, AgentType, InboxStore};
    use std::io::{IsTerminal, Write};
    let cwd = std::env::current_dir()?;
    if reviewer_max_rounds == Some(0) {
        anyhow::bail!("--reviewer-max-rounds must be at least 1, got 0");
    }
    let config_path = cwd.join(".exo/config.toml");
    if !config_path.exists() {
        anyhow::bail!("No exomonad project found. Run `exomonad new` first.");
    }

    // Validate ExoMonad-owned runtime artifacts before any configuration,
    // inbox, capability, plan, or orchestration side effect.
    let mut config = Config::discover()?;
    validate_runtime_compatibility(&cwd)?;
    let init_lifecycle_lock = acquire_init_lifecycle_lock_async(&cwd).await?;
    let mut no_transition_failure = None;
    {
        let _plan_transition_lock = acquire_plan_transition_lock_async(&cwd).await?;
        recover_plan_transition_locked(&cwd, &mut no_transition_failure)?;
    }
    let mut mode = mode;
    let recreate = mode.is_recreate();
    report_legacy_session(&cwd, mode);
    if !recreate && (confirm_recreate || force_recreate || recreate_dry_run) {
        anyhow::bail!(
            "--confirm-recreate, --force-recreate, and --recreate-dry-run require --recreate"
        );
    }
    let start_plan_decision = if mode == SessionMode::Start {
        write_tl_loop_plan(&cwd, config.initial_prompt.as_deref())?;
        Some(prepare_start_plan(&cwd)?)
    } else {
        None
    };
    let recreate_plan = if recreate {
        prepare_recreate(
            &cwd,
            &config,
            confirm_recreate,
            force_recreate,
            recreate_dry_run,
            allow_pending_gate,
        )
        .await?
    } else {
        None
    };
    if recreate_dry_run {
        return Ok(());
    }

    if let Some(ref remote_name) = set_git_remote {
        set_git_remote_override(&cwd, remote_name)?;
    }

    if reset_inbox {
        InboxStore::open(&cwd)?.clear_all()?;
        info!("cleared inbox messages and metadata");
    }

    // Resolve runtime paths and capability configuration after compatibility
    // has been established.
    let runtime_paths = config.tl_preflight_runtime_paths.join(",");
    std::env::set_var(TL_PREFLIGHT_RUNTIME_PATHS_ENV, &runtime_paths);
    ensure_harness_capability(&cwd)?;
    let tl_loop_root = tl_loop_package_root()?;
    let controller_plan_digest;
    if let Some(decision) = start_plan_decision {
        let expected_plan_digest = decision.validated_plan().map(plan_digest);
        let plan_transition_lock = acquire_plan_transition_lock_async(&cwd).await?;
        mode = apply_start_plan_after_validation_locked(
            &cwd,
            decision,
            || {
                if skip_preflight {
                    if expected_plan_digest.is_some() {
                        anyhow::bail!(
                            "--skip-preflight cannot be used with --start when plan.json exists; the requested plan must be validated before replacing runtime state"
                        );
                    }
                    warn!("Skipping TL controller preflight for --start without a plan");
                    return Ok(());
                }
                run_tl_loop_preflight(&cwd, &tl_loop_root, true, expected_plan_digest.as_deref())
            },
            &plan_transition_lock,
            &mut no_transition_failure,
        )?;
        controller_plan_digest = expected_plan_digest;
    } else if mode == SessionMode::Recreate {
        write_tl_loop_plan(&cwd, config.initial_prompt.as_deref())?;
        let recreate_plan = requested_plan_bytes(&cwd)?;
        let expected_plan_digest = recreate_plan.as_deref().map(plan_digest);
        let plan_transition_lock = acquire_plan_transition_lock_async(&cwd).await?;
        controller_plan_digest = apply_recreate_plan_after_validation_locked(
            &cwd,
            recreate_plan.as_deref(),
            || {
                if skip_preflight {
                    if expected_plan_digest.is_some() {
                        anyhow::bail!(
                            "--skip-preflight cannot be used with --recreate when plan.json exists; the requested plan must be validated before replacing runtime state"
                        );
                    }
                    warn!("Skipping TL controller preflight for --recreate without a plan");
                    return Ok(());
                }
                run_tl_loop_preflight(&cwd, &tl_loop_root, false, expected_plan_digest.as_deref())
            },
            &plan_transition_lock,
            &mut no_transition_failure,
        )?;
    } else {
        let _plan_transition_lock = acquire_plan_transition_lock_async(&cwd).await?;
        validate_or_record_plan_snapshot_locked(&cwd, mode)?;
        controller_plan_digest = capture_validated_plan_digest(&cwd)?;
        if skip_preflight {
            warn!("Skipping TL controller preflight by explicit request");
        } else {
            run_tl_loop_preflight(
                &cwd,
                &tl_loop_root,
                false,
                controller_plan_digest.as_deref(),
            )?;
        }
    }

    if !import_legacy.is_empty() {
        crate::logs::run(
            &cwd,
            import_legacy,
            "auto".to_string(),
            import_legacy_dry_run,
            false,
        )?;
        info!(
            dry_run = import_legacy_dry_run,
            "explicit legacy observability import completed before init"
        );
    }

    // CLI flags override config
    if let Some(ref worker_type) = worker {
        config.spawn_agent_type = parse_agent_type(worker_type)?;
    }
    if let Some(m) = worker_model {
        if config.spawn_agent_type == AgentType::OpenCode {
            config.opencode.worker_model = Some(m);
        }
    }
    if let Some(level) = worker_effort_level {
        config.worker_effort_level = ResolvedEffort::from_cli(level);
    }
    if let Some(level) = reviewer_effort_level {
        config.reviewer_effort_level = ResolvedEffort::from_cli(level);
    }
    if let Some(ref reviewer_type) = reviewer {
        config.reviewer.agent_type = parse_agent_type(reviewer_type)?;
    }
    if let Some(m) = reviewer_model {
        config.reviewer.model = Some(m);
    }
    if openrouter {
        config.openrouter.enabled = true;
    }

    validate_opencode_model_owner(
        config.spawn_agent_type,
        config.opencode.worker_model.as_deref(),
        "[opencode].worker_model",
        "spawn_agent_type",
    )?;

    let tl_effort = config.tl_effort_level.level.to_string();
    let worker_effort = config.worker_effort_level.level.to_string();
    let reviewer_effort = config.reviewer_effort_level.level.to_string();
    log_ignored_effort("tl", config.root_agent_type, &tl_effort);
    log_ignored_effort("worker", config.spawn_agent_type, &worker_effort);
    log_ignored_effort("reviewer", config.reviewer.agent_type, &reviewer_effort);
    if config.spawn_agent_type == AgentType::OpenCode {
        if let Some(m) = config.opencode.worker_model.as_deref() {
            validate_opencode_model(m, Some(&worker_effort)).await?;
        }
    } else if config.spawn_agent_type == AgentType::Codex {
        if let Some(m) = config.opencode.worker_model.as_deref() {
            validate_codex_model(m, Some(&worker_effort)).await?;
        }
    }
    validate_reviewer_model_for_harness(
        config.reviewer.agent_type,
        config.reviewer.model.as_deref(),
        Some(&reviewer_effort),
    )
    .await?;

    let root_model = None::<&str>;
    let worker_model = if config.spawn_agent_type == AgentType::OpenCode {
        config.opencode.worker_model.as_deref()
    } else {
        None
    };
    let init_argv = redact_init_argv(std::env::args().collect::<Vec<_>>());
    if let Err(e) = append_init_invocation_log(&cwd, &config, &init_argv, mode) {
        warn!(error = %e, "Failed to append init invocation log");
    } else {
        info!(
            root_agent_type = agent_type_str(config.root_agent_type),
            spawn_agent_type = agent_type_str(config.spawn_agent_type),
            reviewer_agent_type = agent_type_str(config.reviewer.agent_type),
            root_model = ?root_model,
            worker_model = ?worker_model,
            reviewer_model = ?config.reviewer.model,
            tl_effort = %config.tl_effort_level.level,
            tl_effort_source = %config.tl_effort_level.source,
            worker_effort = %config.worker_effort_level.level,
            worker_effort_source = %config.worker_effort_level.source,
            reviewer_effort = %config.reviewer_effort_level.level,
            reviewer_effort_source = %config.reviewer_effort_level.source,
            "Resolved exomonad init agent configuration"
        );
    }
    if config.uses_codex_anywhere() {
        crate::new::warn_if_codex_sandbox_unavailable();
    }
    record_session_mode(&cwd, mode)?;

    if mode == SessionMode::Continue {
        let (preserved, recreated) = reconcile_agent_continuations(&cwd).await?;
        info!(
            preserved,
            recreated, "Reconciled existing agent invocation identities for --continue"
        );
    }

    // Check OTel endpoint reachability if configured
    if let Some(ref endpoint) = config.otlp_endpoint {
        if let Some(host_port) = endpoint
            .strip_prefix("http://")
            .or_else(|| endpoint.strip_prefix("https://"))
        {
            let hp = host_port.to_string();
            let reachable = tokio::task::spawn_blocking(move || {
                use std::net::ToSocketAddrs;
                match hp.to_socket_addrs() {
                    Ok(mut addrs) => {
                        if let Some(addr) = addrs.next() {
                            std::net::TcpStream::connect_timeout(
                                &addr,
                                std::time::Duration::from_secs(2),
                            )
                            .is_ok()
                        } else {
                            false
                        }
                    }
                    Err(_) => false,
                }
            })
            .await
            .unwrap_or(false);

            if reachable {
                info!(endpoint = %endpoint, "OTel endpoint reachable");
            } else if config.yolo || !std::io::stdin().is_terminal() {
                warn!(
                    endpoint = %endpoint,
                    "OTel endpoint unreachable — proceeding without tracing (YOLO or headless)"
                );
            } else {
                eprint!(
                    "OTel endpoint {} unreachable — continue without tracing? [y/N] ",
                    endpoint
                );
                std::io::stderr().flush().ok();
                let input = tokio::task::spawn_blocking(|| {
                    let mut buf = String::new();
                    std::io::stdin().read_line(&mut buf).ok();
                    buf
                })
                .await
                .unwrap_or_default();
                if !input.trim().eq_ignore_ascii_case("y") {
                    anyhow::bail!(
                        "OTel endpoint unreachable. Start it with:\n  docker compose -f ~/.exo/otel/docker-compose.yml up -d"
                    );
                }
            }
        }
    }

    let session = session_override.unwrap_or(config.tmux_session.clone());
    let session_alive = TmuxIpc::has_session(&session).await?;
    let session_transition = if recreate {
        exomonad_core::services::SessionTransition::Recreate
    } else if session_alive {
        exomonad_core::services::SessionTransition::Attach
    } else {
        exomonad_core::services::SessionTransition::Fresh
    };
    exomonad_core::services::transition_session(&cwd, session_transition, "exomonad init")?;
    if should_attach_existing_session(recreate, session_alive) {
        if reviewer_max_rounds.is_some() {
            anyhow::bail!(
                "--reviewer-max-rounds applies when starting a server; use --recreate to restart the existing session"
            );
        }
        if reset_inbox {
            warn!(
                session = %session,
                "--reset-inbox cleared the inbox while an existing session was alive; attaching without restarting it"
            );
        }
        let ipc = TmuxIpc::new(&session);
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());
        reconcile_existing_session(
            &ipc,
            &cwd,
            &shell,
            &session,
            &config,
            &tl_loop_root,
            verbose,
            mode == SessionMode::Start,
            controller_plan_digest.as_deref(),
        )
        .await?;
        if mode == SessionMode::Continue {
            clean::report_continue_cleanup(&cwd).await;
        }
        report_orphaned_agent_windows(&session, &cwd).await;
        info!(session = %session, "Attaching to existing session");
        drop(init_lifecycle_lock);
        return TmuxIpc::attach_session(&session).await;
    }

    let reset_count = refresh_agent_session_timestamps(&cwd)?;
    if reset_count > 0 {
        info!(
            agents = reset_count,
            "Reset orphan reconciler session timers for existing agents"
        );
    }

    // Auto-build or copy WASM if it doesn't exist yet
    let wasm_filename = format!("wasm-guest-{}.wasm", config.wasm_name);
    let wasm_path = config.wasm_dir.join(&wasm_filename);
    let roles_dir = cwd.join(".exo/roles");
    let has_roles = roles_dir.is_dir();

    if !wasm_path.exists() {
        if has_roles {
            info!(path = %wasm_path.display(), "WASM not found, building...");
            exomonad::recompile::run_recompile(
                &config.wasm_name,
                &cwd,
                config.flake_ref.as_deref(),
            )
            .await?;
        } else if let Ok(home) = std::env::var("HOME") {
            let home = PathBuf::from(home);
            // Fall back to globally installed WASM from ~/.exo/wasm/
            let global_wasm = home.join(".exo/wasm").join(&wasm_filename);
            if global_wasm.exists() {
                info!(
                    src = %global_wasm.display(),
                    dst = %wasm_path.display(),
                    "Copying WASM from global install"
                );
                std::fs::create_dir_all(&config.wasm_dir)?;
                std::fs::copy(&global_wasm, &wasm_path)?;
            } else {
                warn!(
                    path = %wasm_path.display(),
                    "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
                );
            }
        } else {
            warn!(
                path = %wasm_path.display(),
                "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
            );
        }
    } else if !has_roles {
        // Refresh stale WASM from global install if it's newer
        if let Ok(home) = std::env::var("HOME") {
            let global_wasm = PathBuf::from(home).join(".exo/wasm").join(&wasm_filename);
            if global_wasm.exists() {
                let local_mtime = std::fs::metadata(&wasm_path).and_then(|m| m.modified());
                let global_mtime = std::fs::metadata(&global_wasm).and_then(|m| m.modified());

                match (local_mtime, global_mtime) {
                    (Ok(local), Ok(global)) if global > local => {
                        info!(
                            src = %global_wasm.display(),
                            dst = %wasm_path.display(),
                            local_mtime = ?local,
                            global_mtime = ?global,
                            "Refreshing project WASM from global install (global is newer)"
                        );
                        std::fs::copy(&global_wasm, &wasm_path)?;
                    }
                    (Err(e), _) | (_, Err(e)) => {
                        debug!(error = %e, "Failed to compare WASM mtimes, skipping refresh");
                    }
                    _ => {}
                }
            }
        }
    }

    // Write root agent birth branch so child spawning resolves the correct parent prefix.
    // Without this, BirthBranch::root() falls back to `git branch --show-current` in the
    // server process CWD, which may differ from the TL's actual branch.
    {
        let root_agent_dir = cwd.join(".exo/agents/root");
        std::fs::create_dir_all(&root_agent_dir)?;
        let current_branch = std::process::Command::new("git")
            .args(["branch", "--show-current"])
            .current_dir(&cwd)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
            .unwrap_or_else(|| "main".to_string());
        write_tl_loop_identity(&cwd, &current_branch)?;
        std::fs::write(root_agent_dir.join(".birth_branch"), &current_branch)?;
        info!(branch = %current_branch, "Wrote root agent birth branch");
    }

    if let (Some(forgejo_url), Some(forgejo_token)) = (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        let git_remote = resolve_git_remote(&cwd);
        if let Err(e) = configure_forgejo_remote(&cwd, forgejo_url, forgejo_token, &git_remote) {
            warn!(error = %e, "Failed to auto-configure Forgejo remote URL (non-fatal)");
        }
    } else if config.forgejo_url.is_none() && config.forgejo_token.is_none() {
        check_fj_cli_configuration(&cwd);
    }

    // Hooks remain available to Claude workers and companions; the root TL is
    // the Python controller below and never launches an interactive harness.
    let binary_path = exomonad_core::find_exomonad_binary();
    exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
        .context("Failed to write hook configuration")?;
    info!("Hook configuration written to .claude/settings.local.json");

    // Copy Claude rules template if available and not already present
    {
        let rules_dest = cwd.join(".claude/rules/exomonad.md");
        if !rules_dest.exists() {
            // Resolution: project-local .exo/rules/ → global ~/.exo/rules/
            let local_template = cwd.join(".exo/rules/exomonad.md");
            let global_template = std::env::var("HOME")
                .ok()
                .map(|h| PathBuf::from(h).join(".exo/rules/exomonad.md"));

            let source = if local_template.exists() {
                Some(local_template)
            } else {
                global_template.filter(|p| p.exists())
            };

            if let Some(src) = source {
                std::fs::create_dir_all(cwd.join(".claude/rules"))?;
                std::fs::copy(&src, &rules_dest)?;
                info!(
                    src = %src.display(),
                    "Copied Claude rules to .claude/rules/exomonad.md"
                );
            }
        }
    }

    // Validate tmux is available
    let tmux_check = std::process::Command::new("tmux").arg("-V").output();
    match tmux_check {
        Ok(output) if output.status.success() => {
            let version = String::from_utf8_lossy(&output.stdout);
            info!("tmux version: {}", version.trim());
        }
        Ok(output) => {
            anyhow::bail!(
                "tmux -V failed (status {}). Is tmux installed correctly?",
                output.status
            );
        }
        Err(e) => {
            anyhow::bail!(
                "tmux not found: {}. Install tmux before running exomonad init.",
                e
            );
        }
    }

    if recreate {
        let recreate_plan = recreate_plan
            .as_ref()
            .context("recreate plan was not prepared before destructive transition")?;
        // Stop the running server and verify termination before any destructive
        // cleanup so it cannot provision ordered controllers during
        // revalidation or disposal.
        stop_server_for_recreate(&cwd).await?;

        destroy_recreate_resources(&cwd, &config, recreate_plan, force_recreate).await?;

        if session_alive {
            info!(session = %session, "Deleting session (--recreate)");
            TmuxIpc::kill_session(&session).await?;
        }

        let _plan_transition_lock = acquire_plan_transition_lock_async(&cwd).await?;
        complete_recreate_plan_transition_locked(&cwd, &mut no_transition_failure)?;
    }

    // Create fresh session
    info!(session = %session, "Creating session");

    // 1. Write .mcp.json (for Claude Code discovery)
    let mut mcp_servers = serde_json::Map::new();
    mcp_servers.insert(
        "exomonad".to_string(),
        exomonad_mcp_server(&binary_path, "tl", "root"),
    );

    // Add extra MCP servers from config
    for (name, server) in &config.extra_mcp_servers {
        let entry = match server {
            exomonad::config::McpServerConfig::Http { url, headers } => {
                let mut e = serde_json::json!({"type": "http", "url": url});
                if !headers.is_empty() {
                    e["headers"] = serde_json::to_value(headers)?;
                }
                e
            }
            exomonad::config::McpServerConfig::Stdio { command, args } => {
                serde_json::json!({"type": "stdio", "command": command, "args": args})
            }
        };
        mcp_servers.insert(name.clone(), entry);
    }

    let mcp_json = serde_json::json!({ "mcpServers": mcp_servers });
    std::fs::write(
        cwd.join(".mcp.json"),
        serde_json::to_string_pretty(&mcp_json)?,
    )?;
    info!("Wrote .mcp.json with {} MCP server(s)", mcp_servers.len());

    // 2. Create session in background
    let server_window_id = TmuxIpc::new_session(&session, &cwd).await?;

    // Verify session
    if !TmuxIpc::has_session(&session).await? {
        anyhow::bail!(
            "tmux session '{}' was created but is not responding.",
            session
        );
    }

    set_reviewer_max_rounds_environment(&session, reviewer_max_rounds)?;

    if let Some(forgejo_url) = config.forgejo_url.as_deref() {
        for (var, value) in forgejo_env_vars(
            forgejo_url,
            config.forgejo_token.as_deref().unwrap_or(""),
            config.forgejo_reviewer_token.as_deref(),
        ) {
            std::env::set_var(var, &value);
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, &value])
                .status();
        }
    }

    let mailbox_protocol_available = if mailbox_protocol_available_for_config(&config) {
        "1"
    } else {
        "0"
    };
    std::env::set_var(
        "EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE",
        mailbox_protocol_available,
    );
    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE",
            mailbox_protocol_available,
        ])
        .status();

    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            TL_PREFLIGHT_RUNTIME_PATHS_ENV,
            &runtime_paths,
        ])
        .status();

    // Set EXOMONAD_TMUX_SESSION
    let env_output = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "EXOMONAD_TMUX_SESSION",
            &session,
        ])
        .output()
        .context("Failed to set EXOMONAD_TMUX_SESSION in tmux session")?;
    if !env_output.status.success() {
        warn!(
            "tmux set-environment failed: {}",
            String::from_utf8_lossy(&env_output.stderr)
        );
    }

    // Anchor chainlink to the root workspace DB so worktree windows don't create their own.
    // Use the directory form (no /issues.db suffix) to match every spawn-site propagation —
    // build_spawn_env in services/agent_control/internal.rs is the canonical form, this is the
    // tmux-level fallback for any process that does not go through build_spawn_env.
    let chainlink_db = cwd.join(".chainlink");
    let _ = std::process::Command::new("tmux")
        .args([
            "set-environment",
            "-t",
            &session,
            "CHAINLINK_DB",
            chainlink_db.to_str().unwrap_or_default(),
        ])
        .status();

    // Propagate CODEX_HOME into the tmux session env so Codex panes see the
    // same hook-trust DB that init's install_codex_hook_trust just seeded.
    // Without this, when tmux server is already running from another session
    // (e.g., a parallel workspace), the new session attaches to that server
    // and inherits the server's captured env — NOT the env exported by the
    // shell that ran `exomonad init`. Codex then falls back to ~/.codex and
    // sees the hooks as untrusted, firing "3 hooks need review". The e2e
    // tests/e2e/reviewer-convergence-loop hit this reliably (chainlink #253).
    if let Ok(codex_home) = std::env::var("CODEX_HOME") {
        if !codex_home.is_empty() {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, "CODEX_HOME", &codex_home])
                .status();
        }
    }

    // Set EXOMONAD_ROLE=root so hook CLI passes &role=root to server
    let role_output = std::process::Command::new("tmux")
        .args(["set-environment", "-t", &session, "EXOMONAD_ROLE", "root"])
        .output()
        .context("Failed to set EXOMONAD_ROLE in tmux session")?;
    if !role_output.status.success() {
        warn!(
            "tmux set-environment EXOMONAD_ROLE failed: {}",
            String::from_utf8_lossy(&role_output.stderr)
        );
    }

    // Propagate verbose trace flags session-wide so spawned worktrees inherit them
    if verbose {
        for (var, val) in [
            ("EXOMONAD_VERBOSE", "1"),
            ("EXOMONAD_HOOK_TRACE", "1"),
            ("EXOMONAD_CHAINLINK_TRACE", "1"),
        ] {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, val])
                .status();
        }
        info!("Verbose mode enabled: EXOMONAD_VERBOSE=1 EXOMONAD_HOOK_TRACE=1 EXOMONAD_CHAINLINK_TRACE=1 set in session environment");
    }

    // Set terminal window title to project/session name
    let _ = std::process::Command::new("tmux")
        .args(["set-option", "-t", &session, "set-titles", "on"])
        .output();
    let _ = std::process::Command::new("tmux")
        .args([
            "set-option",
            "-t",
            &session,
            "set-titles-string",
            "#{session_name}:#{window_name}",
        ])
        .output();

    // 3. Setup windows
    let ipc = TmuxIpc::new(&session);
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".to_string());

    let server_target = server_window_id;
    let rename_status = std::process::Command::new("tmux")
        .args(["rename-window", "-t", server_target.as_str(), "Server"])
        .status()
        .context("Failed to rename server window")?;
    if !rename_status.success() {
        warn!("tmux rename-window failed with status {}", rename_status);
    }
    // Set env vars via tmux set-environment so they're inherited cleanly
    // (avoids inlining secrets in send-keys command strings / terminal scrollback)
    for var in ["FORGEJO_TOKEN", "FORGEJO_API_URL"] {
        if let Ok(val) = std::env::var(var) {
            let _ = std::process::Command::new("tmux")
                .args(["set-environment", "-t", &session, var, &val])
                .status();
        }
    }

    // OpenRouter: propagate LLM routing env vars to all windows in this session.
    if config.openrouter.enabled {
        if let Some(ref api_key) = config.openrouter.resolved_api_key() {
            for (var, val) in [
                ("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"),
                ("ANTHROPIC_AUTH_TOKEN", api_key.as_str()),
                ("ANTHROPIC_API_KEY", ""),
            ] {
                let _ = std::process::Command::new("tmux")
                    .args(["set-environment", "-t", &session, var, val])
                    .status();
            }
            info!("OpenRouter routing enabled: session env vars injected");
        } else {
            warn!("openrouter.enabled = true but no API key found (set openrouter.api_key or OPENROUTER_API_KEY)");
        }
    }

    prepare_server_socket_for_start(&cwd)?;

    let serve_cmd = server_command(&session, &config, verbose);
    let send_status = std::process::Command::new("tmux")
        .args([
            "send-keys",
            "-t",
            server_target.as_str(),
            &serve_cmd,
            "Enter",
        ])
        .status()
        .context("Failed to send server start command to tmux")?;
    if !send_status.success() {
        anyhow::bail!(
            "Failed to start server in tmux (send-keys exited with {})",
            send_status
        );
    }

    // 4. Wait for the server before launching the controller or Watcher.
    wait_for_server_socket(&cwd).await?;
    report_observability_health(&cwd);
    if mode == SessionMode::Continue {
        clean::report_continue_cleanup(&cwd).await;
    }
    ensure_watcher_dashboard_window(&ipc, &cwd, &shell).await?;

    // The human-facing TL window runs one coordinator: the Python controller.
    // Root harness settings and root_command are intentionally ignored.
    let tl_cwd = cwd.clone();
    let base_command = tl_loop_command(
        &cwd,
        &tl_loop_root,
        &TlLoopTimeouts::from(&config),
        controller_plan_digest.as_deref(),
    );

    let tl_command = match config.shell_command {
        Some(ref sc) => format!("{} -c \"{}\"", sc, base_command.replace('"', "\\\"")),
        None => base_command,
    };

    let controller_epoch = prepare_controller_spawn(&cwd)?;
    let tl_window = ipc.new_window("TL", &tl_cwd, &shell, &tl_command).await?;
    ipc.set_window_remain_on_exit(&tl_window, true).await?;
    let startup = wait_for_tl_controller_startup(&ipc, &cwd, &tl_window, &controller_epoch).await;
    if startup.is_ok() {
        ipc.set_window_remain_on_exit(&tl_window, false).await?;
    }
    startup?;
    drop(init_lifecycle_lock);

    // 5. Spawn companion agents
    let companions_to_spawn: Vec<&crate::config::CompanionConfig> =
        config.companions.iter().collect();

    for companion in companions_to_spawn {
        // Validate companion name (alphanumeric, hyphens, underscores only)
        if !companion
            .name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            anyhow::bail!(
                "Invalid companion name '{}': must contain only [A-Za-z0-9_-]",
                companion.name
            );
        }

        // Resolve agent_type: explicit or default to Claude with warning
        let agent_type = match companion.agent_type {
            Some(t) => t,
            None => {
                warn!(
                    name = %companion.name,
                    "Companion '{}' missing agent_type, defaulting to claude. Add agent_type = \"claude\" to silence this warning.",
                    companion.name
                );
                AgentType::Claude
            }
        };

        // Process companions: plain command in a tmux window, no agent infrastructure
        if agent_type == AgentType::Process {
            let companion_cmd = &companion.command;
            info!(
                name = %companion.name,
                cmd = %companion_cmd,
                "Spawning companion process"
            );
            let window_id = ipc
                .new_window(&companion.name, &cwd, &shell, companion_cmd)
                .await?;
            info!(
                name = %companion.name,
                window = %window_id.as_str(),
                cmd = %companion_cmd,
                "Companion process spawned"
            );
            continue;
        }

        info!(name = %companion.name, role = %companion.role, agent_type = ?agent_type, "Spawning companion agent");

        // Create agent identity directory
        let agent_dir = cwd.join(".exo/agents").join(&companion.name);
        std::fs::create_dir_all(&agent_dir)?;

        // Write birth_branch identity
        std::fs::write(agent_dir.join(".birth_branch"), &companion.name)?;

        // Determine CWD for the companion window
        let companion_cwd = if agent_type == AgentType::Claude {
            // Claude companions get their own git worktree for isolated .mcp.json discovery
            let worktree_path = cwd.join(".exo/companions").join(&companion.name);
            let branch_name = format!("companion/{}", companion.name);

            if !worktree_path.exists() {
                // Ensure HEAD exists — worktree creation needs a valid ref
                let head_valid = std::process::Command::new("git")
                    .args(["rev-parse", "--verify", "HEAD"])
                    .current_dir(&cwd)
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false);

                if !head_valid {
                    info!("No commits in repo, creating initial commit for worktree support");
                    let _ = std::process::Command::new("git")
                        .args(["commit", "--allow-empty", "-m", "initial commit"])
                        .current_dir(&cwd)
                        .output();
                }

                // Create worktree (reuse branch if it already exists)
                let branch_exists = std::process::Command::new("git")
                    .args(["rev-parse", "--verify", &branch_name])
                    .current_dir(&cwd)
                    .output()
                    .map(|o| o.status.success())
                    .unwrap_or(false);

                std::fs::create_dir_all(cwd.join(".exo/companions"))?;

                let worktree_result = if branch_exists {
                    std::process::Command::new("git")
                        .args(["worktree", "add"])
                        .arg(&worktree_path)
                        .arg(&branch_name)
                        .current_dir(&cwd)
                        .output()
                } else {
                    std::process::Command::new("git")
                        .args(["worktree", "add", "-b", &branch_name])
                        .arg(&worktree_path)
                        .arg("HEAD")
                        .current_dir(&cwd)
                        .output()
                };

                match worktree_result {
                    Ok(output) if output.status.success() => {
                        info!(
                            name = %companion.name,
                            path = %worktree_path.display(),
                            branch = %branch_name,
                            "Created companion worktree"
                        );
                    }
                    Ok(output) => {
                        anyhow::bail!(
                            "Failed to create worktree for companion '{}': {}",
                            companion.name,
                            String::from_utf8_lossy(&output.stderr)
                        );
                    }
                    Err(e) => {
                        anyhow::bail!(
                            "Failed to run git worktree add for companion '{}': {}",
                            companion.name,
                            e
                        );
                    }
                }
            } else {
                info!(
                    name = %companion.name,
                    path = %worktree_path.display(),
                    "Reusing existing companion worktree"
                );
            }

            // Write .mcp.json to worktree root — Claude discovers via CWD
            let mut companion_mcp_servers = serde_json::Map::new();
            companion_mcp_servers.insert(
                "exomonad".to_string(),
                exomonad_mcp_server(&binary_path, &companion.role, &companion.name),
            );
            // Include extra MCP servers from config
            for (name, server) in &config.extra_mcp_servers {
                let entry = match server {
                    exomonad::config::McpServerConfig::Http { url, headers } => {
                        let mut e = serde_json::json!({"type": "http", "url": url});
                        if !headers.is_empty() {
                            e["headers"] = serde_json::to_value(headers)?;
                        }
                        e
                    }
                    exomonad::config::McpServerConfig::Stdio { command, args } => {
                        serde_json::json!({"type": "stdio", "command": command, "args": args})
                    }
                };
                companion_mcp_servers.insert(name.clone(), entry);
            }
            let companion_mcp_json = serde_json::json!({ "mcpServers": companion_mcp_servers });
            std::fs::write(
                worktree_path.join(".mcp.json"),
                serde_json::to_string_pretty(&companion_mcp_json)?,
            )?;

            // Write .claude/settings.local.json to worktree root (hooks)
            exomonad_core::hooks::HookConfig::write_persistent(
                &worktree_path,
                &binary_path,
                None,
                Some(&cwd),
            )
            .context("Failed to write companion hook configuration")?;

            // Copy role context into companion's rules dir.
            // Must be a copy, not a symlink — symlinks escape the worktree boundary
            // and cause Claude Code to discover parent context files.
            {
                let context_source =
                    resolve_role_context_path(&cwd, &config.wasm_name, &companion.role);
                if let Some(src) = context_source {
                    let rules_dir = worktree_path.join(".claude/rules");
                    let _ = std::fs::create_dir_all(&rules_dir);
                    let dest = rules_dir.join("exomonad_role.md");
                    let _ = std::fs::remove_file(&dest); // idempotent
                    match std::fs::copy(&src, &dest) {
                        Ok(_) => {
                            info!(name = %companion.name, src = %src.display(), dest = %dest.display(), "Copied role context for companion")
                        }
                        Err(e) => {
                            warn!(name = %companion.name, error = %e, "Failed to copy role context (non-fatal)")
                        }
                    }
                }
            }

            // Symlink server socket into worktree's .exo/
            let worktree_exo = worktree_path.join(".exo");
            std::fs::create_dir_all(&worktree_exo)?;
            let socket_target = worktree_exo.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            let socket_source = cwd.join(".exo/server.sock");
            std::os::unix::fs::symlink(&socket_source, &socket_target)?;
            info!(
                source = %socket_source.display(),
                target = %socket_target.display(),
                created_at_ms = current_time_millis(),
                "Symlinked server socket into companion worktree"
            );

            worktree_path
        } else if agent_type == AgentType::OpenCode {
            use exomonad_core::services::agent_control::AgentControlService;
            use exomonad_core::services::Services;
            let exo_dir = agent_dir.join(".exo");
            std::fs::create_dir_all(&exo_dir)?;
            let socket_target = exo_dir.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            std::os::unix::fs::symlink(cwd.join(".exo/server.sock"), &socket_target)?;
            let extra_mcp = extra_mcp_servers_to_json(&config.extra_mcp_servers)?;
            let effort = config.worker_effort_level.level.to_string();
            let opencode_config =
                AgentControlService::<Services>::generate_opencode_tl_settings_with_effort(
                    &companion.name,
                    &companion.role,
                    &extra_mcp,
                    Some(&effort),
                );
            std::fs::write(
                agent_dir.join("opencode.json"),
                serde_json::to_string_pretty(&opencode_config)?,
            )?;
            AgentControlService::<Services>::write_opencode_plugin_files(&agent_dir)
                .await
                .context("Failed to write companion OpenCode plugin files")?;
            agent_dir.clone()
        } else if agent_type == AgentType::Codex {
            let exo_dir = agent_dir.join(".exo");
            std::fs::create_dir_all(&exo_dir)?;
            let socket_target = exo_dir.join("server.sock");
            let _ = std::fs::remove_file(&socket_target);
            std::os::unix::fs::symlink(cwd.join(".exo/server.sock"), &socket_target)?;
            write_codex_companion_config(
                &config,
                &agent_dir,
                &companion.name,
                &companion.role,
                companion.model.as_deref(),
            )?;
            agent_dir.clone()
        } else {
            // Shoal companions use the project root CWD.
            cwd.clone()
        };

        // Build command per agent type.
        // Prefix with identity env vars so hook CLI resolves the correct agent.
        let escaped_task = companion.task.as_deref().map(|t| t.replace('\'', "'\\''"));
        let model_flag = companion
            .model
            .as_ref()
            .map(|m| format!(" --model {}", m))
            .unwrap_or_default();
        let worker_effort_flag = format!(
            " --effort {}",
            shell_escape::escape(config.worker_effort_level.level.to_string().into())
        );
        let worker_variant_flag = format!(
            " --variant {}",
            shell_escape::escape(config.worker_effort_level.level.to_string().into())
        );
        let env_prefix = format!(
            "EXOMONAD_AGENT_ID={} EXOMONAD_ROLE={} ",
            companion.name, companion.role
        );
        let companion_cmd = match agent_type {
            AgentType::Claude => {
                // Pure CWD discovery — no --mcp-config, no --strict-mcp-config
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                format!(
                    "{env_prefix}{}{model_flag}{worker_effort_flag}{task_part}; echo; echo '[{} exited]'; exec bash -l",
                    companion.command, companion.name
                )
            }
            AgentType::Shoal => {
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                format!("{env_prefix}{}{}", companion.command, task_part)
            }
            AgentType::OpenCode => {
                let yolo = if config.yolo {
                    " --dangerously-skip-permissions"
                } else {
                    ""
                };
                let model_flag = companion
                    .model
                    .as_deref()
                    .map(|m| format!(" --model {}", shell_escape::escape(m.into())))
                    .unwrap_or_default();
                let task_part = match &escaped_task {
                    Some(t) => format!(" '{}'", t),
                    None => String::new(),
                };
                if escaped_task.is_some() {
                    format!(
                        "{env_prefix}opencode run{yolo}{model_flag}{worker_variant_flag}{task_part}"
                    )
                } else {
                    format!(
                        "{env_prefix}opencode{yolo}{model_flag} --agent exomonad-{}",
                        companion.role
                    )
                }
            }
            AgentType::Codex => {
                let task_part = match &escaped_task {
                    Some(task) => format!(" '{}'", task),
                    None => String::new(),
                };
                let configured_effort = config.worker_effort_level.level.to_string();
                let effort = configured_effort.as_str();
                let codex_model_flag = companion
                    .model
                    .as_deref()
                    .map(|model| format!(" --model {}", shell_escape::escape(model.into())))
                    .unwrap_or_default();
                let effort_flag = format!(" -c model_reasoning_effort=\"{}\"", effort);
                format!(
                    "{env_prefix}{} --dangerously-bypass-approvals-and-sandbox --cd {}{codex_model_flag}{effort_flag}{task_part}",
                    agent_type.command(),
                    shell_escape::escape(companion_cwd.display().to_string().into())
                )
            }
            AgentType::Process => unreachable!("Process companions handled above"),
        };
        let window_id = ipc
            .new_window(&companion.name, &companion_cwd, &shell, &companion_cmd)
            .await?;

        // Write routing.json with window_id
        let routing = serde_json::json!({
            "window_id": window_id.as_str()
        });
        std::fs::write(
            agent_dir.join("routing.json"),
            serde_json::to_string_pretty(&routing)?,
        )?;

        info!(name = %companion.name, window = %window_id.as_str(), "Companion agent spawned");
    }

    // 6. Attach
    info!(session = %session, "Attaching to session");
    TmuxIpc::attach_session(&session).await
}

fn should_attach_existing_session(recreate: bool, session_alive: bool) -> bool {
    session_alive && !recreate
}

#[derive(Debug, PartialEq, Eq)]
enum TlWindowRecoveryAction {
    Keep,
    RemoveAndRelaunch,
}

fn tl_window_recovery_action(
    force_restart: bool,
    tl_alive: bool,
    root_needs_resume: bool,
) -> TlWindowRecoveryAction {
    if force_restart || (!tl_alive && root_needs_resume) {
        return TlWindowRecoveryAction::RemoveAndRelaunch;
    }
    TlWindowRecoveryAction::Keep
}

/// Refresh orphan timeout baselines for agents that predate this `exomonad init` session.
fn refresh_agent_session_timestamps(cwd: &Path) -> Result<usize> {
    let agents_dir = cwd.join(".exo/agents");
    if !agents_dir.is_dir() {
        return Ok(0);
    }

    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .to_string();
    let mut updated = 0;

    for entry in std::fs::read_dir(&agents_dir)? {
        let entry = entry?;
        if !entry.file_type()?.is_dir() {
            continue;
        }
        if entry.file_name().to_string_lossy() == "root" {
            continue;
        }

        std::fs::write(entry.path().join("spawned_at"), &now_secs)?;
        updated += 1;
    }

    Ok(updated)
}

async fn report_orphaned_agent_windows(session: &str, cwd: &Path) {
    let output = std::process::Command::new("tmux")
        .args([
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_name}\t#{pane_current_command}",
        ])
        .output();

    let output = match output {
        Ok(o) if o.status.success() => o,
        Ok(o) => {
            warn!(
                session,
                stderr = %String::from_utf8_lossy(&o.stderr),
                "Could not list tmux windows for orphan report"
            );
            return;
        }
        Err(error) => {
            warn!(session, error = %error, "tmux list-windows failed for orphan report");
            return;
        }
    };

    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let mut rows: Vec<String> = Vec::new();
    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let mut parts = line.split('\t');
        let window_name = match parts.next() {
            Some(name) => name.trim(),
            None => continue,
        };
        let pane_cmd = parts.next().unwrap_or("").trim();

        if window_name.is_empty() || window_name == "Server" || window_name == "TL" {
            continue;
        }

        let is_shell_prompt = matches!(pane_cmd, "bash" | "zsh" | "fish" | "sh");
        if !is_shell_prompt {
            continue;
        }

        let agent_dir = cwd.join(".exo/agents").join(window_name);
        if !agent_dir.exists() {
            continue;
        }

        let issue = std::fs::read_to_string(agent_dir.join("active_issue"))
            .ok()
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "(none)".to_string());

        let age = std::fs::read_to_string(agent_dir.join("spawned_at"))
            .ok()
            .and_then(|v| v.trim().parse::<u64>().ok())
            .map(|spawned| format!("{}m", now.saturating_sub(spawned) / 60))
            .unwrap_or_else(|| "unknown".to_string());

        rows.push(format!("- {window_name}: issue={issue}, age={age}"));
    }

    if rows.is_empty() {
        return;
    }

    warn!(
        session,
        count = rows.len(),
        "Orphaned agent windows detected (not auto-killed)"
    );
    for row in rows {
        warn!(session, "{}", row);
    }
}

pub fn ensure_gitignore(project_dir: &Path) -> Result<()> {
    let gitignore_path = project_dir.join(".gitignore");
    let content = if gitignore_path.exists() {
        std::fs::read_to_string(&gitignore_path)?
    } else {
        String::new()
    };

    let has_line = |line: &str| content.lines().any(|l| l.trim() == line);
    let needed: Vec<&str> = [
        ".exo/*",
        "!.exo/config.toml",
        "!.exo/roles/",
        "!.exo/lib/",
        "!.exo/rules/",
        ".codex/",
        ".claude/settings.local.json",
        ".opencode/",
        "opencode.json",
        ".chainlink/issues.db",
    ]
    .into_iter()
    .filter(|line| !has_line(line))
    .collect();

    if needed.is_empty() {
        return Ok(());
    }

    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&gitignore_path)?;
    use std::io::Write;
    if !content.is_empty() && !content.ends_with('\n') {
        writeln!(file)?;
    }
    if !has_line(".exo/*") {
        writeln!(
            file,
            "# ExoMonad - track config and source, ignore runtime artifacts"
        )?;
    }
    for line in &needed {
        writeln!(file, "{}", line)?;
    }
    Ok(())
}

const SERVER_STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const SERVER_HEALTH_CHECK_TIMEOUT: Duration = Duration::from_secs(2);
const SERVER_HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(500);

/// The recorded server pid, preserving the distinction between "no record" and
/// "record that cannot be trusted".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ServerPidRecord {
    /// No pid file exists; there is nothing to stop or verify.
    Absent,
    /// A pid file exists but is unreadable, malformed, or carries an unsafe pid.
    Unreadable,
    /// A plausible server pid.
    Pid(i32),
}

fn read_server_pid_record(pid_path: &Path) -> ServerPidRecord {
    let content = match std::fs::read_to_string(pid_path) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return ServerPidRecord::Absent
        }
        Err(_) => return ServerPidRecord::Unreadable,
    };
    let parsed = match serde_json::from_str::<Value>(&content) {
        Ok(value) => value,
        Err(_) => return ServerPidRecord::Unreadable,
    };
    match parsed
        .get("pid")
        .and_then(Value::as_u64)
        .and_then(|pid| i32::try_from(pid).ok())
    {
        // PID 0 signals the caller's whole process group and PID 1 is init;
        // neither can be a recorded exomonad server.
        Some(pid) if pid > 1 => ServerPidRecord::Pid(pid),
        _ => ServerPidRecord::Unreadable,
    }
}

fn server_pid_is_alive(pid_path: &Path) -> bool {
    match read_server_pid_record(pid_path) {
        ServerPidRecord::Pid(pid) => !pid_is_dead(pid),
        ServerPidRecord::Absent => false,
        // Fail closed: an unusable record may still describe a live server.
        ServerPidRecord::Unreadable => true,
    }
}

fn server_socket_is_live(socket_path: &Path) -> bool {
    // A local Unix connect completes or fails immediately; a stale socket file
    // yields ECONNREFUSED, while a live listener accepts the connection.
    std::os::unix::net::UnixStream::connect(socket_path).is_ok()
}

fn process_is_zombie(pid: i32) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|stat| stat.rsplit(')').next().map(str::to_owned))
        .and_then(|rest| rest.split_whitespace().next().map(str::to_owned))
        .is_some_and(|state| state == "Z")
}

fn pid_is_dead(pid: i32) -> bool {
    match nix::sys::signal::kill(nix::unistd::Pid::from_raw(pid), None) {
        Err(nix::errno::Errno::ESRCH) => true,
        // A zombie has not been reaped yet but will not run again.
        Ok(()) => process_is_zombie(pid),
        Err(_) => false,
    }
}

/// Verify that a recorded pid still names an exomonad server process.
///
/// A stale pid file can be reused by an unrelated process, and signalling that
/// process would be destructive. The recorded pid is the `exomonad serve`
/// process, so require both an `exomonad` argv[0] and the `serve` subcommand
/// before any signal is sent. Argument text alone is forgeable, so the caller
/// must also confirm the process works in this workspace.
fn process_looks_like_exomonad_server(pid: i32) -> bool {
    let Ok(cmdline) = std::fs::read(format!("/proc/{pid}/cmdline")) else {
        return false;
    };
    let args = cmdline
        .split(|byte| *byte == 0)
        .filter(|arg| !arg.is_empty())
        .filter_map(|arg| std::str::from_utf8(arg).ok())
        .collect::<Vec<_>>();
    let names_exomonad = args.iter().any(|arg| {
        Path::new(arg)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name == "exomonad")
    });
    names_exomonad && args.contains(&"serve")
}

/// Confirm a live process works in this recreate workspace.
///
/// `/proc/<pid>/cwd` is kernel-resolved, so a server that reused the pid from
/// another project cannot masquerade as this workspace's server.
fn pid_cwd_matches_workspace(pid: i32, project_dir: &Path) -> bool {
    let Ok(cwd) = std::fs::read_link(format!("/proc/{pid}/cwd")) else {
        return false;
    };
    let Ok(expected) = project_dir.canonicalize() else {
        return false;
    };
    cwd.canonicalize()
        .is_ok_and(|resolved| resolved == expected)
}

/// Verify that the recorded pid is this workspace's exomonad server.
fn process_matches_server_record(pid: i32, project_dir: &Path) -> bool {
    process_looks_like_exomonad_server(pid) && pid_cwd_matches_workspace(pid, project_dir)
}

async fn wait_until_pid_dead(pid: i32, deadline: Instant) -> bool {
    while Instant::now() < deadline {
        if pid_is_dead(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    pid_is_dead(pid)
}

/// Stop any running server and verify termination before destructive cleanup.
///
/// `--recreate` may only proceed once the previous server is provably gone: a
/// live, unidentifiable, or unresponsive server would otherwise race the
/// cleanup and re-provision the resources being removed.
async fn stop_server_for_recreate(project_dir: &Path) -> Result<()> {
    let pid_path = project_dir.join(".exo/server.pid");
    let socket_path = project_dir.join(".exo/server.sock");
    match read_server_pid_record(&pid_path) {
        ServerPidRecord::Unreadable => {
            anyhow::bail!(
                "refusing --recreate: the server pid record at {} is unreadable or invalid; \
                 remove it manually and retry",
                pid_path.display()
            );
        }
        ServerPidRecord::Pid(pid) => {
            if pid_is_dead(pid) {
                // The recorded server is already gone; the record is stale.
            } else if process_matches_server_record(pid, project_dir) {
                info!(pid, "Stopping server before --recreate");
                let target = nix::unistd::Pid::from_raw(pid);
                let _ = nix::sys::signal::kill(target, nix::sys::signal::Signal::SIGTERM);
                if !wait_until_pid_dead(pid, Instant::now() + Duration::from_secs(2)).await {
                    let _ = nix::sys::signal::kill(target, nix::sys::signal::Signal::SIGKILL);
                    if !wait_until_pid_dead(pid, Instant::now() + Duration::from_secs(2)).await {
                        anyhow::bail!(
                            "refusing --recreate: server pid {pid} did not terminate; \
                             stop it manually and retry"
                        );
                    }
                }
            } else {
                // The pid was reused by an unrelated process or belongs to a
                // different workspace; never signal it and refuse cleanup.
                anyhow::bail!(
                    "refusing --recreate: recorded server pid {pid} does not belong to \
                     this workspace; remove {} manually and retry",
                    pid_path.display()
                );
            }
        }
        ServerPidRecord::Absent => {}
    }
    if server_socket_is_live(&socket_path) {
        anyhow::bail!(
            "refusing --recreate: a server is still listening on {}; \
             stop it manually and retry",
            socket_path.display()
        );
    }
    remove_server_artifact(&socket_path)?;
    remove_server_artifact(&pid_path)?;
    info!("Cleaned up server socket and pid");
    Ok(())
}

fn remove_server_artifact(path: &Path) -> Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| format!("failed to remove {}", path.display())),
    }
}

fn prepare_server_socket_for_start(project_dir: &Path) -> Result<()> {
    let socket_path = project_dir.join(".exo/server.sock");
    if !socket_path.exists() || server_pid_is_alive(&project_dir.join(".exo/server.pid")) {
        return Ok(());
    }

    remove_server_artifact(&socket_path)?;
    remove_server_artifact(&project_dir.join(".exo/server.pid"))?;
    Ok(())
}

#[cfg(test)]
async fn wait_for_socket_path(socket_path: &Path, timeout_dur: Duration) -> Result<()> {
    wait_for_socket_path_until(socket_path, Instant::now() + timeout_dur, timeout_dur).await
}

async fn wait_for_socket_path_until(
    socket_path: &Path,
    deadline: Instant,
    timeout_dur: Duration,
) -> Result<()> {
    while Instant::now() < deadline {
        if socket_path.exists() {
            return Ok(());
        }
        let sleep_duration =
            SERVER_HEALTH_POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now()));
        if sleep_duration.is_zero() {
            break;
        }
        tokio::time::sleep(sleep_duration).await;
    }

    anyhow::bail!(
        "Server socket not found at {} after {}s.",
        socket_path.display(),
        timeout_dur.as_secs()
    );
}

pub async fn wait_for_server_socket(project_dir: &Path) -> Result<()> {
    let socket_path = project_dir.join(".exo/server.sock");
    let deadline = Instant::now() + SERVER_STARTUP_TIMEOUT;
    wait_for_socket_path_until(&socket_path, deadline, SERVER_STARTUP_TIMEOUT).await?;

    let client = uds_client::ServerClient::new(socket_path.to_path_buf());
    while Instant::now() < deadline {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let check_timeout = remaining.min(SERVER_HEALTH_CHECK_TIMEOUT);
        if tokio::time::timeout(check_timeout, client.is_healthy())
            .await
            .unwrap_or(false)
        {
            return Ok(());
        }
        let sleep_duration =
            SERVER_HEALTH_POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now()));
        if sleep_duration.is_zero() {
            break;
        }
        tokio::time::sleep(sleep_duration).await;
    }

    anyhow::bail!(
        "Server socket exists but health check failed after {}s.",
        SERVER_STARTUP_TIMEOUT.as_secs()
    )
}

fn report_observability_health(project_dir: &Path) {
    match exomonad_core::services::read_sink_health(project_dir) {
        Ok(Some(health)) => info!(
            status = %health.measurement_status,
            accepted_events = health.accepted_event_count,
            rejected_events = health.rejected_event_count,
            write_failures = health.write_failure_count,
            last_successful_seq = ?health.last_successful_seq,
            "structured observability startup health"
        ),
        Ok(None) => warn!(
            "structured observability startup health is unknown; sink-health.json is not available yet"
        ),
        Err(error) => warn!(%error, "could not read structured observability startup health"),
    }
}

/// Parse agent type from CLI string.
fn parse_agent_type(s: &str) -> Result<AgentType> {
    let value = s.to_lowercase();
    if value == ["ge", "mini"].concat() {
        anyhow::bail!(
            "{}",
            exomonad_core::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE
        );
    }
    match value.as_str() {
        "claude" | "claude-code" => Ok(AgentType::Claude),
        "opencode" | "opencode-cli" => Ok(AgentType::OpenCode),
        "codex" => Ok(AgentType::Codex),
        "shoal" => Ok(AgentType::Shoal),
        _ => anyhow::bail!(
            "Unknown agent type: {}. Valid values: claude, opencode, codex, shoal",
            s
        ),
    }
}

fn agent_type_str(t: AgentType) -> &'static str {
    match t {
        AgentType::Claude => "claude",
        AgentType::OpenCode => "opencode",
        AgentType::Codex => "codex",
        AgentType::Shoal => "shoal",
        AgentType::Process => "process",
    }
}

fn log_ignored_effort(role: &str, agent_type: AgentType, effort: &str) {
    if matches!(agent_type, AgentType::Shoal) {
        info!(
            role,
            harness = agent_type_str(agent_type),
            effort,
            "Configured effort is ignored because this harness has no stable effort interface"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_test_support::{
        assert_fixture_git_root, init_fixture_git_repository, run_fixture_git_command,
    };
    use std::io::Write;
    use std::os::unix::fs::PermissionsExt;
    use std::process::{Command, Stdio};

    #[test]
    fn codex_protocol_delivery_is_prompt_independent() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(".exo/roles/sentinel/context");
        std::fs::create_dir_all(&path).unwrap();
        std::fs::write(path.join("root.md"), "SENTINEL ROOT PROTOCOL").unwrap();

        for initial_prompt in [None, Some("task-only initial prompt")] {
            let instructions = codex_root_instructions(tmp.path(), "sentinel");
            assert!(instructions.contains("SENTINEL ROOT PROTOCOL"));
            assert!(instructions.contains("Codex Runtime Notes"));
            if let Some(prompt) = initial_prompt {
                assert!(!instructions.contains(prompt));
            }
        }
    }

    #[test]
    fn reviewer_max_rounds_tmux_args_set_or_clear_session_override() {
        assert_eq!(
            reviewer_max_rounds_tmux_args("demo", Some(7)),
            vec![
                "set-environment",
                "-t",
                "demo",
                REVIEWER_MAX_ROUNDS_ENV,
                "7"
            ]
        );
        assert_eq!(
            reviewer_max_rounds_tmux_args("demo", None),
            vec![
                "set-environment",
                "-t",
                "demo",
                "-u",
                REVIEWER_MAX_ROUNDS_ENV
            ]
        );
    }

    #[test]
    fn watcher_dashboard_command_creates_log_file() {
        let tmp = tempfile::tempdir().unwrap();
        let command = watcher_dashboard_command(tmp.path()).unwrap();
        let log_path = tmp.path().join(".exo/logs/watcher.log");

        assert!(log_path.exists());
        assert_eq!(command, "exomonad watch");
    }

    #[test]
    fn redact_init_argv_redacts_sensitive_flag_values() {
        let redacted = redact_init_argv(vec![
            "exomonad".to_string(),
            "init".to_string(),
            "--worker".to_string(),
            "opencode".to_string(),
            "--api-key".to_string(),
            "secret-value".to_string(),
            "--token=abc123".to_string(),
        ]);

        assert_eq!(
            redacted,
            vec![
                "exomonad".to_string(),
                "init".to_string(),
                "--worker".to_string(),
                "opencode".to_string(),
                "--api-key".to_string(),
                "<redacted>".to_string(),
                "--token=<redacted>".to_string(),
            ]
        );
    }

    #[test]
    fn append_init_invocation_log_records_resolved_worker_type() {
        let tmp = tempfile::tempdir().unwrap();
        let mut config = Config {
            tmux_session: "RustDebuggerRepo".to_string(),
            root_agent_type: AgentType::Claude,
            spawn_agent_type: AgentType::OpenCode,
            model: Some("sonnet".to_string()),
            ..Config::default()
        };
        config.reviewer.agent_type = AgentType::Codex;
        config.opencode.worker_model = Some("opencode-go/deepseek-v4-pro".to_string());

        append_init_invocation_log(
            tmp.path(),
            &config,
            &[
                "exomonad".to_string(),
                "init".to_string(),
                "--worker".to_string(),
                "opencode".to_string(),
            ],
            SessionMode::Continue,
        )
        .unwrap();

        let log = std::fs::read_to_string(tmp.path().join(".exo/logs/init.jsonl")).unwrap();
        let value: serde_json::Value = serde_json::from_str(log.trim()).unwrap();

        assert_eq!(value["resolved"]["root_agent_type"], "claude");
        assert_eq!(value["resolved"]["spawn_agent_type"], "opencode");
        assert_eq!(value["resolved"]["reviewer_agent_type"], "codex");
        assert_eq!(value["session_mode"], "continue");
        assert_eq!(
            value["resolved"]["opencode_worker_model"],
            "opencode-go/deepseek-v4-pro"
        );
        assert_eq!(value["argv"][2], "--worker");
    }

    #[test]
    fn session_mode_resolves_default_and_explicit_choices() {
        assert_eq!(
            SessionMode::resolve(false, false, false).unwrap(),
            SessionMode::Continue
        );
        assert_eq!(
            SessionMode::resolve(true, false, false).unwrap(),
            SessionMode::Start
        );
        assert_eq!(
            SessionMode::resolve(false, true, false).unwrap(),
            SessionMode::Continue
        );
        assert_eq!(
            SessionMode::resolve(false, false, true).unwrap(),
            SessionMode::Recreate
        );
    }

    #[test]
    fn session_mode_rejects_conflicting_flags_by_name() {
        let error = SessionMode::resolve(true, true, false).unwrap_err();
        assert!(error.to_string().contains("--start"));
        assert!(error.to_string().contains("--continue"));
    }

    #[test]
    fn session_mode_record_is_atomic_and_readable() {
        let tmp = tempfile::tempdir().unwrap();
        record_session_mode(tmp.path(), SessionMode::Start).unwrap();
        let value: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.path().join(".exo/tl-loop/session-mode.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(value["session_mode"], "start");
        assert!(!tmp.path().join(".exo/tl-loop/session-mode.tmp").exists());
    }

    #[test]
    fn continue_cleanup_suggestion_is_advisory_and_cannot_apply() {
        let request = clean::continue_cleanup_request();
        assert_eq!(request.target, None);
        assert!(request.sweep);
        assert!(!request.apply);

        let receipt = exomonad_core::services::CleanupReceipt {
            schema_version: 1,
            operation_id: "operation".to_string(),
            plan_id: "plan".to_string(),
            started_at: 1,
            finished_at: 2,
            dry_run: true,
            operator_reason: None,
            preserve_unique_commits: false,
            entries: vec![exomonad_core::services::CleanupReceiptEntry {
                candidate_id: "candidate".to_string(),
                agent_name: "leaf".to_string(),
                agent_slug: "leaf-slug".to_string(),
                identity_snapshot: None,
                recovered_provenance: None,
                pull_request: None,
                branch: None,
                status: exomonad_core::services::CleanupReceiptStatus::WouldClean,
                actions: Vec::new(),
                reason: None,
                dirty_evidence: None,
            }],
        };
        let suggestion = clean::continue_suggestion(&receipt).unwrap();
        assert!(suggestion.contains("1 verified cleanup candidate(s)"));
        assert!(suggestion.contains("exomonad clean --sweep --apply"));
    }

    fn write_test_invocation(
        agent_dir: &Path,
        invocation_id: &str,
        runtime_agent_id: &str,
        branch: &str,
        slice_id: &str,
    ) {
        std::fs::create_dir_all(agent_dir).unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let record = runtime
            .block_on(exomonad_core::services::agent_control::start_invocation(
                agent_dir,
                AgentType::Codex,
                exomonad_core::services::agent_control::InvocationTrigger::Spawn,
                exomonad_core::domain::RoutingInfo::window(
                    exomonad_core::services::tmux_ipc::WindowId::parse("@42").unwrap(),
                ),
                None,
                None,
            ))
            .unwrap();
        let mut value = serde_json::to_value(record).unwrap();
        value["invocation_id"] = serde_json::json!(invocation_id);
        value["runtime_agent_id"] = serde_json::json!(runtime_agent_id);
        value["branch"] = serde_json::json!(branch);
        value["slice_id"] = serde_json::json!(slice_id);
        std::fs::write(
            agent_dir.join(exomonad_core::services::agent_control::INVOCATION_FILENAME),
            serde_json::to_vec_pretty(&value).unwrap(),
        )
        .unwrap();
    }

    fn test_publication(
        agent_name: &str,
        invocation_id: &str,
        branch: &str,
        slice_id: &str,
    ) -> PublishedHead {
        PublishedHead {
            pr_number: 43,
            head_branch: branch.to_owned(),
            base_branch: "main".to_owned(),
            head_sha: "head-sha".to_owned(),
            author_agent: Some(agent_name.to_owned()),
            author_role: Some("dev".to_owned()),
            provenance: exomonad_core::services::pr_registry::PublicationProvenance::LedgerOwned,
            slice_id: Some(slice_id.to_owned()),
            invocation_id: Some(invocation_id.to_owned()),
            invocation_trigger: None,
            invocation_runtime: None,
            invocation_succession: Vec::new(),
        }
    }

    #[test]
    fn continue_preserves_matching_invocation_identity_even_when_pane_is_dead() {
        let tmp = tempfile::tempdir().unwrap();
        let agent_dir = tmp.path().join("leaf-codex");
        write_test_invocation(
            &agent_dir,
            "invocation-1",
            "leaf-codex",
            "feature/leaf",
            "slice-a",
        );

        let decision = classify_agent(
            &agent_dir,
            &[test_publication(
                "leaf-codex",
                "invocation-1",
                "feature/leaf",
                "slice-a",
            )],
        );
        assert_eq!(
            decision,
            AgentContinuation::Preserve {
                invocation_id: "invocation-1".to_owned()
            }
        );
        write_continuation_decision(&agent_dir, &decision).unwrap();
        let persisted: serde_json::Value =
            serde_json::from_slice(&std::fs::read(agent_dir.join("continuation.json")).unwrap())
                .unwrap();
        assert_eq!(persisted["classification"], "preserve");
        assert_eq!(persisted["invocation_id"], "invocation-1");
    }

    #[test]
    fn continue_recreates_missing_or_unverifiable_invocation_with_reason() {
        let tmp = tempfile::tempdir().unwrap();
        let missing = tmp.path().join("missing-agent");
        std::fs::create_dir_all(&missing).unwrap();
        assert_eq!(
            classify_agent(&missing, &[]),
            AgentContinuation::Recreate {
                reason: "invocation record is missing"
            }
        );

        let malformed = tmp.path().join("malformed-agent");
        std::fs::create_dir_all(&malformed).unwrap();
        std::fs::write(malformed.join("invocation.json"), b"{not-json").unwrap();
        assert_eq!(
            classify_agent(&malformed, &[]),
            AgentContinuation::Recreate {
                reason: "invocation record is malformed"
            }
        );

        let unmatched = tmp.path().join("unmatched-agent");
        write_test_invocation(
            &unmatched,
            "invocation-2",
            "unmatched-agent",
            "feature/other",
            "slice-b",
        );
        let decision = classify_agent(&unmatched, &[]);
        assert!(matches!(
            decision,
            AgentContinuation::Recreate {
                reason: "invocation has no matching verified publication ownership"
            }
        ));
        write_continuation_decision(&unmatched, &decision).unwrap();
        let first = std::fs::read(unmatched.join("continuation.json")).unwrap();
        write_continuation_decision(&unmatched, &decision).unwrap();
        assert_eq!(
            first,
            std::fs::read(unmatched.join("continuation.json")).unwrap()
        );
    }

    #[test]
    fn continue_accepts_invocation_succession_without_rewriting_current_id() {
        let tmp = tempfile::tempdir().unwrap();
        let agent_dir = tmp.path().join("leaf-codex");
        write_test_invocation(
            &agent_dir,
            "invocation-2",
            "leaf-codex",
            "feature/leaf",
            "slice-a",
        );
        let mut publication =
            test_publication("leaf-codex", "invocation-1", "feature/leaf", "slice-a");
        publication.invocation_succession.push(
            exomonad_core::services::pr_registry::InvocationSuccession {
                from_invocation_id: "invocation-1".to_owned(),
                to_invocation_id: "invocation-2".to_owned(),
                reason: exomonad_core::services::pr_registry::SuccessionReason::SessionRecreate,
                recorded_at: 1,
            },
        );
        assert_eq!(
            classify_agent(&agent_dir, &[publication]),
            AgentContinuation::Preserve {
                invocation_id: "invocation-2".to_owned()
            }
        );
    }

    #[test]
    fn continue_refuses_modified_plan_bytes_and_keeps_snapshot_stable() {
        let tmp = tempfile::tempdir().unwrap();
        let plan = tmp.path().join(".exo/tl-loop/plan.json");
        std::fs::create_dir_all(plan.parent().unwrap()).unwrap();
        let original = serde_json::to_vec(&serde_json::json!({
            "plan": {"leaves": []}
        }))
        .unwrap();
        std::fs::write(&plan, &original).unwrap();
        validate_or_record_plan_snapshot(tmp.path(), SessionMode::Start).unwrap();
        let snapshot = std::fs::read(plan_snapshot_path(tmp.path())).unwrap();
        assert_eq!(snapshot, std::fs::read(&plan).unwrap());

        let changed = serde_json::to_vec(&serde_json::json!({
            "plan": {"leaves": [{"name": "changed"}]}
        }))
        .unwrap();
        std::fs::write(&plan, changed).unwrap();
        let error =
            validate_or_record_plan_snapshot(tmp.path(), SessionMode::Continue).unwrap_err();
        assert!(error
            .to_string()
            .contains("differs from its persisted session snapshot"));
        assert_eq!(
            snapshot,
            std::fs::read(plan_snapshot_path(tmp.path())).unwrap()
        );

        std::fs::write(&plan, snapshot).unwrap();
        validate_or_record_plan_snapshot(tmp.path(), SessionMode::Continue).unwrap();
    }

    #[test]
    fn continue_adopts_legacy_plan_then_rejects_later_drift() {
        let tmp = tempfile::tempdir().unwrap();
        let plan = tmp.path().join(".exo/tl-loop/plan.json");
        std::fs::create_dir_all(plan.parent().unwrap()).unwrap();
        let original = b"{\"plan\":{\"leaves\":[]}}\n".to_vec();
        std::fs::write(&plan, &original).unwrap();

        validate_or_record_plan_snapshot(tmp.path(), SessionMode::Continue).unwrap();
        assert_eq!(std::fs::read(&plan).unwrap(), original);
        assert_eq!(
            std::fs::read(plan_snapshot_path(tmp.path())).unwrap(),
            original
        );

        std::fs::write(&plan, b"{\"plan\":{\"leaves\":[{\"name\":\"changed\"}]}}\n").unwrap();
        let error =
            validate_or_record_plan_snapshot(tmp.path(), SessionMode::Continue).unwrap_err();
        assert!(error
            .to_string()
            .contains("differs from its persisted session snapshot"));
        assert_eq!(
            std::fs::read(plan_snapshot_path(tmp.path())).unwrap(),
            original
        );
    }

    #[test]
    fn continue_refuses_missing_plan_when_snapshot_exists() {
        let tmp = tempfile::tempdir().unwrap();
        let plan = tmp.path().join(".exo/tl-loop/plan.json");
        std::fs::create_dir_all(plan.parent().unwrap()).unwrap();
        std::fs::write(&plan, b"{\"plan\":{\"leaves\":[]}}\n").unwrap();
        validate_or_record_plan_snapshot(tmp.path(), SessionMode::Start).unwrap();
        std::fs::remove_file(&plan).unwrap();

        let error =
            validate_or_record_plan_snapshot(tmp.path(), SessionMode::Continue).unwrap_err();
        assert!(error
            .to_string()
            .contains("plan.json is missing while its persisted session snapshot exists"));
    }

    fn terminal_transition_fixture() -> (tempfile::TempDir, Vec<u8>, Vec<u8>) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#.to_vec();
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#.to_vec();
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, &requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), &original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(&original)),
        )
        .unwrap();
        (dir, original, requested)
    }

    #[test]
    fn recreate_plan_adopts_new_snapshot_and_digest_after_validation() {
        let dir = tempfile::tempdir().unwrap();
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"recreated"}]}}"#;
        std::fs::create_dir_all(plan.parent().unwrap()).unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        let digest = plan_digest(requested);
        assert_eq!(
            apply_recreate_plan_after_validation(dir.path(), Some(requested), || Ok(())).unwrap(),
            Some(digest.clone())
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{digest}\n")
        );
        assert_eq!(
            read_plan_transition(dir.path()).unwrap().unwrap(),
            (PLAN_TRANSITION_PHASE_PREPARED.to_owned(), None)
        );
        complete_recreate_plan_transition(dir.path(), None).unwrap();
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn rust_persists_waited_recreate_plan_identity() {
        let dir = tempfile::tempdir().unwrap();
        let accepted = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#;
        let digest = plan_digest(accepted);

        record_plan_snapshot_bytes(dir.path(), accepted, &digest).unwrap();

        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            accepted
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{digest}\n")
        );
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn recorder_repairs_missing_digest_for_matching_start_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let accepted = br#"{"plan":{"workers":[]}}"#;
        let digest = plan_digest(accepted);
        write_plan_snapshot(&plan_snapshot_path(dir.path()), accepted).unwrap();

        record_plan_snapshot_bytes(dir.path(), accepted, &digest).unwrap();
        record_plan_snapshot_bytes(dir.path(), accepted, &digest).unwrap();

        assert_eq!(read_plan_snapshot_digest(dir.path()).unwrap(), Some(digest));
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn recorder_rejects_conflicting_start_snapshot_and_digest() {
        let dir = tempfile::tempdir().unwrap();
        let accepted = br#"{"plan":{"workers":[]}}"#;
        let changed = br#"{"plan":{"workers":[{"name":"changed"}]}}"#;
        let digest = plan_digest(accepted);
        write_plan_snapshot(&plan_snapshot_path(dir.path()), accepted).unwrap();
        assert!(
            record_plan_snapshot_bytes(dir.path(), changed, &plan_digest(changed))
                .unwrap_err()
                .to_string()
                .contains("immutable session snapshot")
        );
        write_plan_snapshot_digest(dir.path(), "wrong").unwrap();
        assert!(record_plan_snapshot_bytes(dir.path(), accepted, &digest)
            .unwrap_err()
            .to_string()
            .contains("snapshot identity differs"));
        assert_eq!(
            read_plan_snapshot_bytes(dir.path()).unwrap(),
            Some(accepted.to_vec())
        );
        assert_eq!(
            read_plan_snapshot_digest(dir.path()).unwrap(),
            Some("wrong".into())
        );
    }

    #[test]
    fn rust_rejects_waited_recreate_plan_with_wrong_identity() {
        let dir = tempfile::tempdir().unwrap();
        let accepted = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#;

        let error = record_plan_snapshot_bytes(dir.path(), accepted, "wrong").unwrap_err();

        assert!(error
            .to_string()
            .contains("does not match expected identity"));
        assert!(!plan_snapshot_path(dir.path()).exists());
        assert!(!plan_snapshot_digest_path(dir.path()).exists());
    }

    #[test]
    fn recreate_plan_failure_before_archive_restores_prior_checkpoint_and_identity() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"recreated"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), "old checkpoint").unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        apply_recreate_plan_after_validation(dir.path(), Some(requested), || Ok(())).unwrap();

        assert_eq!(
            read_plan_transition(dir.path()).unwrap().unwrap().0,
            PLAN_TRANSITION_PHASE_PREPARED
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        recover_plan_transition(dir.path()).unwrap();

        assert_eq!(
            std::fs::read_to_string(root.join("run.json")).unwrap(),
            "old checkpoint"
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{}\n", plan_digest(original))
        );
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn recreate_plan_commit_archives_prior_checkpoint_and_clears_transition() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"recreated"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), "old checkpoint").unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        apply_recreate_plan_after_validation(dir.path(), Some(requested), || Ok(())).unwrap();
        let (_, archive_name) = read_plan_transition(dir.path()).unwrap().unwrap();
        let archive_name = archive_name.expect("recreate should reserve the old root archive");
        let archive = dir.path().join(".exo/tl-loop").join(archive_name);

        complete_recreate_plan_transition(dir.path(), None).unwrap();

        assert!(!root.exists());
        assert_eq!(
            std::fs::read_to_string(archive.join("run.json")).unwrap(),
            "old checkpoint"
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{}\n", plan_digest(requested))
        );
        assert!(!plan_transition_path(dir.path()).exists());
        assert!(!plan_transition_previous_snapshot_path(dir.path()).exists());
        assert!(!plan_transition_previous_digest_path(dir.path()).exists());
    }

    #[test]
    fn recreate_plan_archive_failure_rolls_back_adopted_identity() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"recreated"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), "old checkpoint").unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        apply_recreate_plan_after_validation(dir.path(), Some(requested), || Ok(())).unwrap();
        complete_recreate_plan_transition(dir.path(), Some("archive")).unwrap_err();

        assert!(root.is_dir());
        assert_eq!(
            std::fs::read_to_string(root.join("run.json")).unwrap(),
            "old checkpoint"
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn recreate_plan_validation_failure_preserves_prior_identity_before_teardown() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"recreated"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), "old checkpoint").unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        let error = apply_recreate_plan_after_validation(dir.path(), Some(requested), || {
            anyhow::bail!("invalid requested WorkPlan")
        })
        .unwrap_err();

        assert!(error.to_string().contains("invalid requested WorkPlan"));
        assert!(root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{}\n", plan_digest(original))
        );
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn start_with_identical_terminal_plan_uses_continue_and_preserves_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let snapshot = br#"{"plan":{"leaves":[]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, snapshot).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), snapshot).unwrap();

        assert_eq!(
            resolve_start_plan(dir.path()).unwrap(),
            StartPlanDecision::Continue {
                validated_plan: snapshot.to_vec()
            }
        );
        assert_eq!(
            apply_start_plan(dir.path(), prepare_start_plan(dir.path()).unwrap()).unwrap(),
            SessionMode::Continue
        );
        assert!(root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            snapshot
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            plan_digest(snapshot) + "\n"
        );
    }

    #[test]
    fn start_with_changed_terminal_plan_archives_run_and_replaces_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();

        assert_eq!(
            apply_start_plan(dir.path(), prepare_start_plan(dir.path()).unwrap()).unwrap(),
            SessionMode::Start
        );
        assert!(!root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            plan_digest(requested) + "\n"
        );
        let archives = std::fs::read_dir(dir.path().join(".exo/tl-loop"))
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("root.invalid-")
            })
            .collect::<Vec<_>>();
        assert_eq!(archives.len(), 1);
        assert!(archives[0].path().join("run.json").is_file());
    }

    #[test]
    fn start_with_changed_nonterminal_plan_fails_without_mutating_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_waiting"}}"#).unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();

        let error = prepare_start_plan(dir.path()).unwrap_err();
        let message = error.to_string();
        assert!(message.contains("requested plan differs"));
        assert!(message.contains("--continue"));
        assert!(message.contains("--recreate"));
        assert!(root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
    }

    #[test]
    fn start_plan_validation_failure_preserves_terminal_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, b"{not-json").unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        let error = apply_start_plan_after_validation(dir.path(), decision, || {
            anyhow::bail!("invalid requested WorkPlan")
        })
        .unwrap_err();

        assert!(error.to_string().contains("invalid requested WorkPlan"));
        assert!(root.exists());
        assert_eq!(
            std::fs::read(root.join("run.json")).unwrap(),
            br#"{"fsm":{"phase":"tl_done"}}"#
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
    }

    #[test]
    fn start_plan_apply_uses_validated_bytes_without_rereading_plan() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let validated = br#"{"plan":{"leaves":[{"name":"validated"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, validated).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        std::fs::write(&plan, b"unvalidated bytes").unwrap();
        apply_start_plan(dir.path(), decision).unwrap();

        assert!(!root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            validated
        );
    }

    #[test]
    fn start_plan_snapshot_failure_preserves_terminal_runtime() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::create_dir_all(dir.path().join(".exo/tl-loop/plan.tmp")).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        assert!(apply_start_plan(dir.path(), decision).is_err());

        assert!(root.exists());
        let archives = std::fs::read_dir(dir.path().join(".exo/tl-loop"))
            .unwrap()
            .filter_map(|entry| entry.ok())
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("root.invalid-")
            })
            .count();
        assert_eq!(archives, 0);
    }

    #[test]
    fn continue_plan_apply_rejects_drift_after_classification() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let snapshot = br#"{"plan":{"leaves":[]}}"#;
        let changed = br#"{"plan":{"leaves":[{"name":"changed"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, snapshot).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), snapshot).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        std::fs::write(&plan, changed).unwrap();
        let error = apply_start_plan(dir.path(), decision).unwrap_err();

        assert!(error.to_string().contains("changed after validation"));
        assert!(root.exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            snapshot
        );
    }

    #[test]
    fn continue_plan_apply_rejects_snapshot_drift_after_classification() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let snapshot = br#"{"plan":{"leaves":[]}}"#;
        let changed = br#"{"plan":{"leaves":[{"name":"changed"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, snapshot).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), snapshot).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), changed).unwrap();
        let error = apply_start_plan(dir.path(), decision).unwrap_err();

        assert!(error.to_string().contains("plan.snapshot changed"));
        assert!(root.exists());
    }

    #[test]
    fn start_plan_archive_failure_restores_snapshot_and_identity() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#;
        std::fs::create_dir_all(root.parent().unwrap()).unwrap();
        std::fs::write(&root, "not a checkpoint directory").unwrap();
        std::fs::write(&plan, requested).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        let decision = StartPlanDecision::NewRun {
            archive_terminal: true,
            validated_plan: Some(requested.to_vec()),
        };
        assert!(apply_start_plan(dir.path(), decision).is_err());

        assert_eq!(
            std::fs::read_to_string(&root).unwrap(),
            "not a checkpoint directory"
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{}\n", plan_digest(original))
        );
    }

    #[test]
    fn prepared_plan_transition_recovers_after_archive_before_commit() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"new"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), "old checkpoint").unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        let archive = root_archive_path_at(dir.path(), 123).unwrap().unwrap();
        begin_plan_transition(
            dir.path(),
            Some(original),
            Some(&plan_digest(original)),
            Some(&archive),
        )
        .unwrap();
        apply_plan_snapshot(dir.path(), Some(requested), Some(&plan_digest(requested))).unwrap();
        archive_root_tl_run_to(dir.path(), &archive).unwrap();

        recover_plan_transition(dir.path()).unwrap();

        assert!(root.exists());
        assert_eq!(
            std::fs::read_to_string(root.join("run.json")).unwrap(),
            "old checkpoint"
        );
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            original
        );
        assert!(!plan_transition_path(dir.path()).exists());
        assert!(!archive.exists());
    }

    #[test]
    fn transition_failure_points_preserve_committed_state() {
        for failure in ["snapshot-write", "digest-write", "archive"] {
            let (dir, original, requested) = terminal_transition_fixture();
            let decision = StartPlanDecision::NewRun {
                archive_terminal: true,
                validated_plan: Some(requested.clone()),
            };

            let error = apply_start_plan_with_failure(dir.path(), decision, failure).unwrap_err();

            assert!(error
                .to_string()
                .contains("injected plan transition failure"));
            assert!(dir.path().join(".exo/tl-loop/root").exists());
            assert_eq!(
                std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
                original
            );
            assert_eq!(
                std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
                format!("{}\n", plan_digest(&original))
            );
            assert!(!plan_transition_path(dir.path()).exists());
        }

        for failure in [
            "backup-snapshot-delete",
            "backup-digest-delete",
            "journal-delete",
        ] {
            let (dir, _original, requested) = terminal_transition_fixture();
            let decision = StartPlanDecision::NewRun {
                archive_terminal: true,
                validated_plan: Some(requested.clone()),
            };

            let error = apply_start_plan_with_failure(dir.path(), decision, failure).unwrap_err();

            assert!(error
                .to_string()
                .contains("injected plan transition failure"));
            assert!(!dir.path().join(".exo/tl-loop/root").exists());
            assert_eq!(
                std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
                requested
            );
            assert_eq!(
                std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
                format!("{}\n", plan_digest(&requested))
            );
            assert!(plan_transition_path(dir.path()).exists());

            recover_plan_transition(dir.path()).unwrap();

            assert!(!plan_transition_path(dir.path()).exists());
            assert!(!plan_transition_previous_snapshot_path(dir.path()).exists());
            assert!(!plan_transition_previous_digest_path(dir.path()).exists());
        }
    }

    #[test]
    fn start_without_plan_clears_snapshot_for_wait_for_plan() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let original = br#"{"plan":{"leaves":[]}}"#;
        let requested = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();

        let decision = prepare_start_plan(dir.path()).unwrap();
        assert_eq!(
            apply_start_plan(dir.path(), decision).unwrap(),
            SessionMode::Start
        );
        assert!(!root.exists());
        assert!(!plan_snapshot_path(dir.path()).exists());

        std::fs::write(&plan, requested).unwrap();
        validate_or_record_plan_snapshot(dir.path(), SessionMode::Continue).unwrap();
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        validate_or_record_plan_snapshot(dir.path(), SessionMode::Continue).unwrap();
    }

    #[test]
    fn start_plan_identity_treats_formatting_changes_as_drift() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        let plan = dir.path().join(".exo/tl-loop/plan.json");
        let snapshot = br#"{"plan":{"leaves":[]}}"#;
        let reformatted = br#"{
  "plan": {"leaves": []}
}
"#;
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        std::fs::write(&plan, reformatted).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), snapshot).unwrap();

        assert_eq!(
            prepare_start_plan(dir.path()).unwrap(),
            StartPlanDecision::NewRun {
                archive_terminal: true,
                validated_plan: Some(reformatted.to_vec()),
            }
        );
    }

    #[test]
    fn recreate_plan_names_dirty_worktrees_and_protected_prs() {
        let plan = RecreatePlan {
            worktrees: vec![PathBuf::from(".exo/worktrees/leaf")],
            ordered_branches: Vec::new(),
            leaf_branches: Vec::new(),
            prs_to_close: vec![43],
            prs_to_remove: vec![43],
            protected: vec![ProtectedPr {
                number: 43,
                reason: "approved and CI-green".to_owned(),
            }],
            dirty_worktrees: vec![PathBuf::from(".exo/worktrees/leaf")],
        };
        let rendered = plan.render();
        assert!(rendered.contains(".exo/worktrees/leaf [DIRTY]"));
        assert!(rendered.contains("#43 (approved and CI-green)"));
    }

    fn ordered_test_spec(branch: &str, parent_branch: &str, agent_name: &str) -> OrderedBranchSpec {
        let worktree = PathBuf::from("/project/.exo/worktrees").join(agent_name);
        OrderedBranchSpec {
            branch: branch.to_owned(),
            parent_branch: parent_branch.to_owned(),
            agent_name: agent_name.to_owned(),
            slice_id: agent_name.to_owned(),
            identity_worktree: worktree.clone(),
            worktree,
        }
    }

    fn ordered_test_observation(
        identity: OrderedIdentityState,
        unique_commits: Option<u64>,
    ) -> OrderedBranchObservation {
        OrderedBranchObservation {
            branch_exists: true,
            agent_dir_exists: false,
            identity,
            worktree_exists: false,
            worktree_branch: None,
            attached_worktree: None,
            dirty_worktree: false,
            live_invocation: false,
            unique_commits,
            publications: Vec::new(),
            protected_publications: Vec::new(),
        }
    }

    #[test]
    fn same_plan_orphan_branch_is_provably_disposable() {
        let cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            ordered_test_observation(OrderedIdentityState::Missing, Some(0)),
            &[],
        )
        .unwrap();

        assert_eq!(cleanup.action, OrderedBranchAction::Remove);
        assert!(cleanup.render().contains("main.stage -> remove"));
        assert!(cleanup.render().contains("identity=missing"));
        assert!(cleanup.render().contains("unique_commits=0"));

        let mut worktree_observation =
            ordered_test_observation(OrderedIdentityState::Missing, Some(0));
        worktree_observation.worktree_exists = true;
        worktree_observation.worktree_branch = Some("main.stage".to_owned());
        let worktree_cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            worktree_observation,
            &[],
        )
        .unwrap();
        assert!(worktree_cleanup
            .gate()
            .is_some_and(|gate| gate.contains("without a durable identity")));
    }

    #[test]
    fn nested_orphan_branch_uses_its_exact_parent() {
        let cleanup = ordered_branch_action(
            ordered_test_spec("main.parent.child", "main.parent", "child"),
            ordered_test_observation(OrderedIdentityState::Missing, Some(0)),
            &[],
        )
        .unwrap();

        assert_eq!(cleanup.spec.parent_branch, "main.parent");
        assert_eq!(cleanup.action, OrderedBranchAction::Remove);
    }

    #[test]
    fn mismatched_identity_is_a_named_recovery_gate() {
        let cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            ordered_test_observation(OrderedIdentityState::Mismatched, Some(0)),
            &[],
        )
        .unwrap();

        assert_eq!(
            cleanup.gate(),
            Some("durable identity does not match the same-plan owner")
        );
        assert!(cleanup.render().contains("preserve [GATE:"));
    }

    #[test]
    fn unique_commits_and_publications_are_preserved() {
        let mut unique = ordered_test_observation(OrderedIdentityState::Missing, Some(1));
        let unique_cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            unique.clone(),
            &[],
        )
        .unwrap();
        assert!(unique_cleanup
            .gate()
            .is_some_and(|gate| gate.contains("unique commits")));

        unique.unique_commits = Some(0);
        unique.publications = vec![43];
        unique.protected_publications = vec![43];
        let published = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            unique,
            &[],
        )
        .unwrap();
        assert!(published
            .gate()
            .is_some_and(|gate| gate.contains("publication evidence")));
        assert!(published.render().contains("#43 [PROTECTED]"));
    }

    fn write_matching_ordered_identity(project_dir: &Path, spec: &OrderedBranchSpec) {
        let dir = project_dir.join(".exo/agents").join(&spec.agent_name);
        std::fs::create_dir_all(&dir).unwrap();
        let value = serde_json::json!({
            "agent_name": spec.agent_name,
            "slug": spec.agent_name,
            "agent_type": "codex",
            "birth_branch": spec.branch,
            "parent_branch": spec.parent_branch,
            "working_dir": spec.identity_worktree,
            "display_name": format!("🤖 {}", spec.agent_name),
            "topology": "worktree_per_agent",
            "model": null,
            "effort": null,
            "ledger_owned": true,
            "slice_id": spec.slice_id,
        });
        std::fs::write(
            dir.join("identity.json"),
            serde_json::to_string(&value).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn scheduled_publication_disposal_authorizes_branch_removal() {
        let mut observation = ordered_test_observation(OrderedIdentityState::Missing, Some(0));
        observation.publications = vec![43];
        observation.protected_publications = vec![43];

        let cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            observation,
            &[43],
        )
        .unwrap();

        assert_eq!(cleanup.action, OrderedBranchAction::Remove);
    }

    fn write_published_head(project_dir: &Path, publication: &PublishedHead) {
        let dir = project_dir.join(".exo");
        std::fs::create_dir_all(&dir).unwrap();
        let document = serde_json::json!({
            "schema_version": 2,
            "heads": [publication],
        });
        std::fs::write(
            dir.join("published-heads.json"),
            serde_json::to_string_pretty(&document).unwrap(),
        )
        .unwrap();
    }

    fn ordered_repo_spec(project_dir: &Path, branch: &str) -> OrderedBranchSpec {
        let agent_name = branch.rsplit('.').next().unwrap().to_owned();
        OrderedBranchSpec {
            branch: branch.to_owned(),
            parent_branch: "main".to_owned(),
            slice_id: agent_name.clone(),
            identity_worktree: project_dir.join(".exo/worktrees").join(&agent_name),
            worktree: project_dir.join(".exo/worktrees").join(&agent_name),
            agent_name,
        }
    }

    fn init_ordered_repo() -> tempfile::TempDir {
        let temp = tempfile::tempdir().unwrap();
        let run_git = |args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(temp.path())
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run_git(&["init", "-q", "-b", "main"]);
        run_git(&["config", "user.email", "test@example.invalid"]);
        run_git(&["config", "user.name", "Test"]);
        std::fs::write(temp.path().join("seed"), "seed\n").unwrap();
        run_git(&["add", "seed"]);
        run_git(&["commit", "-q", "-m", "seed"]);
        run_git(&["branch", "main.stage"]);
        temp
    }

    fn ordered_repo_plan(spec: OrderedBranchSpec) -> RecreatePlan {
        RecreatePlan {
            worktrees: Vec::new(),
            ordered_branches: vec![OrderedBranchCleanup {
                spec,
                observation: ordered_test_observation(OrderedIdentityState::Missing, Some(0)),
                action: OrderedBranchAction::Remove,
            }],
            leaf_branches: Vec::new(),
            prs_to_close: Vec::new(),
            prs_to_remove: Vec::new(),
            protected: Vec::new(),
            dirty_worktrees: Vec::new(),
        }
    }

    #[tokio::test]
    async fn merged_publication_record_does_not_block_branch_cleanup() {
        let temp = init_ordered_repo();
        let spec = ordered_repo_spec(temp.path(), "main.stage");
        write_published_head(
            temp.path(),
            &test_publication("stage", "invocation", "main.stage", "stage"),
        );
        let mut plan = ordered_repo_plan(spec);
        // A merged or already-closed PR has nothing to close, but its record is
        // still scheduled for removal, so it must not gate branch cleanup.
        plan.prs_to_remove = vec![43];

        destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap();

        assert!(!git_branch_exists(temp.path(), "main.stage").unwrap());
    }

    #[tokio::test]
    async fn publication_closure_failure_preserves_ordered_branch() {
        let temp = init_ordered_repo();
        let spec = ordered_repo_spec(temp.path(), "main.stage");
        write_published_head(
            temp.path(),
            &test_publication("stage", "invocation", "main.stage", "stage"),
        );
        let mut plan = ordered_repo_plan(spec);
        plan.prs_to_close = vec![43];
        plan.prs_to_remove = vec![43];

        let error = destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap_err();

        // PR closure is attempted before local ownership removal, so any
        // closure failure (client, repository resolution, or the API call)
        // leaves the branch in place for a safe retry.
        let _ = error;
        assert!(git_branch_exists(temp.path(), "main.stage").unwrap());
    }

    #[test]
    fn interrupted_disposal_identity_residue_is_provably_disposable() {
        let mut observation = ordered_test_observation(OrderedIdentityState::Matching, None);
        observation.branch_exists = false;
        observation.agent_dir_exists = true;

        let cleanup = ordered_branch_action(
            ordered_test_spec("main.stage", "main", "stage"),
            observation,
            &[],
        )
        .unwrap();

        assert_eq!(cleanup.action, OrderedBranchAction::Remove);
    }

    #[tokio::test]
    async fn interrupted_disposal_is_completed_idempotently() {
        let temp = tempfile::tempdir().unwrap();
        let run_git = |args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(temp.path())
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run_git(&["init", "-q", "-b", "main"]);
        run_git(&["config", "user.email", "test@example.invalid"]);
        run_git(&["config", "user.name", "Test"]);
        std::fs::write(temp.path().join("seed"), "seed\n").unwrap();
        run_git(&["add", "seed"]);
        run_git(&["commit", "-q", "-m", "seed"]);
        run_git(&["branch", "main.stage"]);

        let spec = OrderedBranchSpec {
            branch: "main.stage".to_owned(),
            parent_branch: "main".to_owned(),
            agent_name: "stage".to_owned(),
            slice_id: "stage".to_owned(),
            identity_worktree: temp.path().join(".exo/worktrees/stage"),
            worktree: temp.path().join(".exo/worktrees/stage"),
        };
        write_matching_ordered_identity(temp.path(), &spec);
        // Simulate a crash after the branch was deleted but before the durable
        // identity was removed.
        run_git(&["branch", "-D", "main.stage"]);

        let plan = RecreatePlan {
            worktrees: Vec::new(),
            ordered_branches: vec![OrderedBranchCleanup {
                spec: spec.clone(),
                observation: ordered_test_observation(OrderedIdentityState::Matching, None),
                action: OrderedBranchAction::Remove,
            }],
            leaf_branches: Vec::new(),
            prs_to_close: Vec::new(),
            prs_to_remove: Vec::new(),
            protected: Vec::new(),
            dirty_worktrees: Vec::new(),
        };

        destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap();

        let identity = temp.path().join(".exo/agents/stage/identity.json");
        assert!(!identity.exists());
    }

    #[tokio::test]
    async fn gate_in_later_branch_preserves_earlier_branch() {
        let temp = tempfile::tempdir().unwrap();
        let run_git = |args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(temp.path())
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run_git(&["init", "-q", "-b", "main"]);
        run_git(&["config", "user.email", "test@example.invalid"]);
        run_git(&["config", "user.name", "Test"]);
        std::fs::write(temp.path().join("seed"), "seed\n").unwrap();
        run_git(&["add", "seed"]);
        run_git(&["commit", "-q", "-m", "seed"]);
        run_git(&["branch", "main.first"]);
        run_git(&["branch", "main.second"]);
        run_git(&["checkout", "-q", "main.second"]);
        std::fs::write(temp.path().join("unique"), "unique\n").unwrap();
        run_git(&["add", "unique"]);
        run_git(&["commit", "-q", "-m", "unique"]);
        run_git(&["checkout", "-q", "main"]);

        let first = OrderedBranchSpec {
            branch: "main.first".to_owned(),
            parent_branch: "main".to_owned(),
            agent_name: "first".to_owned(),
            slice_id: "first".to_owned(),
            identity_worktree: temp.path().join(".exo/worktrees/first"),
            worktree: temp.path().join(".exo/worktrees/first"),
        };
        let second = OrderedBranchSpec {
            branch: "main.second".to_owned(),
            parent_branch: "main".to_owned(),
            agent_name: "second".to_owned(),
            slice_id: "second".to_owned(),
            identity_worktree: temp.path().join(".exo/worktrees/second"),
            worktree: temp.path().join(".exo/worktrees/second"),
        };
        let plan = RecreatePlan {
            worktrees: Vec::new(),
            ordered_branches: vec![
                OrderedBranchCleanup {
                    spec: first,
                    observation: ordered_test_observation(OrderedIdentityState::Missing, Some(0)),
                    action: OrderedBranchAction::Remove,
                },
                OrderedBranchCleanup {
                    spec: second,
                    observation: ordered_test_observation(OrderedIdentityState::Missing, Some(1)),
                    action: OrderedBranchAction::Remove,
                },
            ],
            leaf_branches: Vec::new(),
            prs_to_close: Vec::new(),
            prs_to_remove: Vec::new(),
            protected: Vec::new(),
            dirty_worktrees: Vec::new(),
        };

        let error = destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("main.second"));
        // Revalidation of the later branch must complete before any mutation.
        assert!(git_branch_exists(temp.path(), "main.first").unwrap());
    }

    #[tokio::test]
    async fn ordered_branch_cleanup_is_idempotent() {
        let temp = tempfile::tempdir().unwrap();
        let run_git = |args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(temp.path())
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run_git(&["init", "-q", "-b", "main"]);
        run_git(&["config", "user.email", "test@example.invalid"]);
        run_git(&["config", "user.name", "Test"]);
        std::fs::write(temp.path().join("seed"), "seed\n").unwrap();
        run_git(&["add", "seed"]);
        run_git(&["commit", "-q", "-m", "seed"]);
        run_git(&["branch", "main.stage"]);

        let spec = OrderedBranchSpec {
            branch: "main.stage".to_owned(),
            parent_branch: "main".to_owned(),
            agent_name: "stage".to_owned(),
            slice_id: "stage".to_owned(),
            identity_worktree: temp.path().join(".exo/worktrees/stage"),
            worktree: temp.path().join(".exo/worktrees/stage"),
        };
        let plan = RecreatePlan {
            worktrees: Vec::new(),
            ordered_branches: vec![OrderedBranchCleanup {
                spec,
                observation: ordered_test_observation(OrderedIdentityState::Missing, Some(0)),
                action: OrderedBranchAction::Remove,
            }],
            leaf_branches: Vec::new(),
            prs_to_close: Vec::new(),
            prs_to_remove: Vec::new(),
            protected: Vec::new(),
            dirty_worktrees: Vec::new(),
        };

        destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap();
        destroy_recreate_resources(temp.path(), &Config::default(), &plan, false)
            .await
            .unwrap();
        assert!(!git_branch_exists(temp.path(), "main.stage").unwrap());
    }

    #[tokio::test]
    async fn recreate_plan_detects_identityless_same_plan_branch() {
        let temp = tempfile::tempdir().unwrap();
        let run_git = |args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(temp.path())
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run_git(&["init", "-q", "-b", "main"]);
        run_git(&["config", "user.email", "test@example.invalid"]);
        run_git(&["config", "user.name", "Test"]);
        std::fs::write(temp.path().join("seed"), "seed\n").unwrap();
        run_git(&["add", "seed"]);
        run_git(&["commit", "-q", "-m", "seed"]);
        run_git(&["branch", "main.stage"]);
        let plan_path = temp.path().join(".exo/tl-loop/plan.snapshot");
        std::fs::create_dir_all(plan_path.parent().unwrap()).unwrap();
        std::fs::write(
            plan_path,
            br#"{"plan":{"sub_tls":[{"name":"stage","order":1,"plan":{}}]}}"#,
        )
        .unwrap();

        let plan = build_recreate_plan(temp.path(), &Config::default(), false)
            .await
            .unwrap();

        assert_eq!(plan.ordered_branches.len(), 1);
        assert_eq!(plan.ordered_branches[0].spec.branch, "main.stage");
        assert_eq!(plan.ordered_branches[0].action, OrderedBranchAction::Remove);
        assert!(plan.render().contains("main.stage -> remove"));
    }

    fn test_leaf_publication(branch: &str, head_sha: &str) -> PublishedHead {
        PublishedHead {
            pr_number: 44,
            head_branch: branch.to_owned(),
            base_branch: "main".to_owned(),
            head_sha: head_sha.to_owned(),
            author_agent: Some("leaf".to_owned()),
            author_role: Some("dev".to_owned()),
            provenance: exomonad_core::services::pr_registry::PublicationProvenance::LedgerOwned,
            slice_id: Some("leaf".to_owned()),
            invocation_id: Some("inv".to_owned()),
            invocation_trigger: None,
            invocation_runtime: None,
            invocation_succession: Vec::new(),
        }
    }

    fn setup_leaf_fixture() -> (tempfile::TempDir, PathBuf, PathBuf, String) {
        let temp = tempfile::tempdir().unwrap();
        let project = temp.path().join("work");
        let remote = temp.path().join("remote.git");
        std::fs::create_dir_all(&project).unwrap();
        let run = |dir: &Path, args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(dir)
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run(
            temp.path(),
            &["init", "--bare", "-q", remote.to_str().unwrap()],
        );
        run(&project, &["init", "-q", "-b", "main"]);
        run(&project, &["config", "user.email", "test@example.invalid"]);
        run(&project, &["config", "user.name", "Test"]);
        std::fs::write(project.join("seed"), "seed\n").unwrap();
        run(&project, &["add", "seed"]);
        run(&project, &["commit", "-q", "-m", "seed"]);
        run(
            &project,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        run(&project, &["push", "-q", "origin", "main"]);
        run(&project, &["checkout", "-q", "-b", "main.leaf"]);
        std::fs::write(project.join("leaf"), "leaf\n").unwrap();
        run(&project, &["add", "leaf"]);
        run(&project, &["commit", "-q", "-m", "leaf"]);
        run(&project, &["push", "-q", "-u", "origin", "main.leaf"]);
        let head_sha = String::from_utf8(
            std::process::Command::new("git")
                .args(["rev-parse", "refs/heads/main.leaf"])
                .current_dir(&project)
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap()
        .trim()
        .to_owned();
        run(&project, &["checkout", "-q", "main"]);
        let worktree = project.join(".exo/worktrees/leaf");
        run(
            &project,
            &[
                "worktree",
                "add",
                "-q",
                worktree.to_str().unwrap(),
                "main.leaf",
            ],
        );
        write_leaf_identity(&project, "leaf", "main.leaf", &worktree);
        write_published_head(&project, &test_leaf_publication("main.leaf", &head_sha));
        (temp, project, remote, head_sha)
    }

    fn write_leaf_identity(project: &Path, agent: &str, branch: &str, worktree: &Path) {
        let dir = project.join(".exo/agents").join(agent);
        std::fs::create_dir_all(&dir).unwrap();
        let value = serde_json::json!({
            "agent_name": agent,
            "slug": agent,
            "birth_branch": branch,
            "working_dir": worktree,
        });
        std::fs::write(
            dir.join("identity.json"),
            serde_json::to_string(&value).unwrap(),
        )
        .unwrap();
    }

    fn leaf_plan(leaves: Vec<LeafBranchCleanup>) -> RecreatePlan {
        RecreatePlan {
            worktrees: Vec::new(),
            ordered_branches: Vec::new(),
            leaf_branches: leaves,
            prs_to_close: Vec::new(),
            prs_to_remove: vec![44],
            protected: Vec::new(),
            dirty_worktrees: Vec::new(),
        }
    }

    #[test]
    fn leaf_branch_action_requires_proven_head_ownership() {
        let project = Path::new("/repo");
        assert_eq!(
            leaf_branch_action(
                "head-a",
                Some("head-a"),
                Some("head-a"),
                None,
                false,
                false,
                false,
                project
            ),
            OrderedBranchAction::Remove
        );
        for action in [
            leaf_branch_action(
                "head-a",
                Some("other"),
                None,
                None,
                false,
                false,
                false,
                project,
            ),
            leaf_branch_action(
                "head-a",
                None,
                Some("other"),
                None,
                false,
                false,
                false,
                project,
            ),
            leaf_branch_action(
                "head-a",
                Some("head-a"),
                None,
                None,
                true,
                false,
                false,
                project,
            ),
            leaf_branch_action(
                "head-a",
                Some("head-a"),
                None,
                None,
                false,
                true,
                false,
                project,
            ),
            leaf_branch_action(
                "head-a",
                Some("head-a"),
                None,
                None,
                false,
                false,
                true,
                project,
            ),
        ] {
            assert!(matches!(action, OrderedBranchAction::Preserve(_)));
        }
    }

    #[tokio::test]
    async fn recreate_plan_render_shows_remote_lease_and_preservation() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        let rendered = leaves[0].render();
        assert!(
            rendered.contains(&format!("delete origin/main.leaf with lease {head_sha}")),
            "{rendered}"
        );
        assert!(
            rendered.contains("preserve 1 unmerged commit"),
            "{rendered}"
        );
        assert!(
            rendered.contains(&format!("remote_head={head_sha}")),
            "{rendered}"
        );
    }

    #[test]
    fn verify_pr_association_rejects_mismatched_publication() {
        use exomonad_core::domain::{BranchName, PRNumber};
        use exomonad_core::services::forgejo::ForgejoPullRequest;

        let publication = test_leaf_publication("main.leaf", "head-a");
        let pr = ForgejoPullRequest {
            number: PRNumber::new(44),
            url: String::new(),
            title: String::new(),
            body: String::new(),
            head_ref: BranchName::try_from_str("main.leaf").unwrap(),
            base_ref: BranchName::try_from_str("main").unwrap(),
            state: "open".to_owned(),
            merged: false,
            head_sha: Some("head-a".to_owned()),
            base_sha: None,
            merge_commit_sha: None,
        };
        verify_pr_association(&pr, &publication).unwrap();

        let mut mismatched_head = pr.clone();
        mismatched_head.head_sha = Some("other".to_owned());
        assert!(verify_pr_association(&mismatched_head, &publication).is_err());

        let mut mismatched_branch = pr.clone();
        mismatched_branch.head_ref = BranchName::try_from_str("main.other").unwrap();
        assert!(verify_pr_association(&mismatched_branch, &publication).is_err());

        let mut mismatched_base = pr;
        mismatched_base.base_ref = BranchName::try_from_str("release").unwrap();
        assert!(verify_pr_association(&mismatched_base, &publication).is_err());
    }

    #[tokio::test]
    async fn recreate_disposes_verified_leaf_branch_locally_and_remotely() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        assert_eq!(leaves.len(), 1);
        assert_eq!(leaves[0].action, OrderedBranchAction::Remove);
        let plan = leaf_plan(leaves);

        destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap();

        assert!(!git_branch_exists(&project, "main.leaf").unwrap());
        assert!(!project.join(".exo/worktrees/leaf").exists());
        assert!(!project.join(".exo/agents/leaf").exists());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_none());

        // Unmerged commits are preserved in a verified bundle before the refs
        // are deleted.
        let bundle = leaf_preservation_path(&project, "main.leaf", &head_sha);
        assert!(bundle.exists());
        verify_preservation_bundle(&project, &bundle, &head_sha).unwrap();

        // A durable per-step receipt records the exact order: preserve, remote
        // ref before local branch, worktree, identity.
        let receipts = read_recreate_receipts(&project).await.unwrap();
        let receipt = receipts
            .iter()
            .find(|entry| entry.branch == "main.leaf")
            .expect("leaf receipt must be persisted");
        assert_eq!(
            receipt.actions,
            vec![
                "preserve_unmerged_commits",
                "delete_remote_branch",
                "remove_worktree",
                "delete_local_branch",
                "remove_identity",
            ]
        );
        assert!(receipt.remote_deleted);
        assert!(receipt.worktree_removed);
        assert!(receipt.local_branch_deleted);
        assert!(receipt.identity_removed);

        // Idempotent retry: an interrupted disposal can be re-run safely.
        destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn recreate_leaf_cleanup_refuses_dirty_worktree_without_generic_sweep() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        let worktree = project.join(".exo/worktrees/leaf");
        let mut plan = leaf_plan(leaves);
        // The generic sweep would otherwise force-remove this worktree before
        // the leaf revalidation ran.
        plan.worktrees = vec![worktree.clone()];

        std::fs::write(worktree.join("uncommitted"), "dirty\n").unwrap();

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("dirty"), "{error}");
        // The dirty worktree and its uncommitted file survive.
        assert!(worktree.join("uncommitted").exists());
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn recreate_enumerates_remote_only_leaf_residue() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        // Simulate an interrupted cleanup: the local worktree and branch are
        // gone, but the remote ref remains.
        let worktree = project.join(".exo/worktrees/leaf");
        let run = |dir: &Path, args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(dir)
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run(
            &project,
            &["worktree", "remove", "--force", worktree.to_str().unwrap()],
        );
        run(&project, &["branch", "-D", "main.leaf"]);

        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        assert_eq!(leaves.len(), 1, "remote-only residue must be enumerated");
        assert_eq!(leaves[0].action, OrderedBranchAction::Remove);
        assert!(leaves[0].local_head.is_none());
        assert!(leaves[0].worktree.is_none());
        assert_eq!(leaves[0].remote_head.as_deref(), Some(head_sha.as_str()));
    }

    #[tokio::test]
    async fn recreate_disposes_remote_only_leaf_residue() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        let worktree = project.join(".exo/worktrees/leaf");
        let run = |dir: &Path, args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(dir)
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run(
            &project,
            &["worktree", "remove", "--force", worktree.to_str().unwrap()],
        );
        run(&project, &["branch", "-D", "main.leaf"]);

        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        let plan = leaf_plan(leaves);
        destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap();

        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_none());
        let receipts = read_recreate_receipts(&project).await.unwrap();
        let receipt = receipts
            .iter()
            .find(|entry| entry.branch == "main.leaf")
            .expect("remote-only residue receipt must be persisted");
        assert!(receipt.actions.contains(&"delete_remote_branch".to_owned()));
        assert!(receipt
            .actions
            .contains(&"local_branch_already_absent".to_owned()));
    }

    #[tokio::test]
    async fn recreate_leaf_cleanup_gates_on_missing_durable_identity() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        std::fs::remove_file(project.join(".exo/agents/leaf/identity.json")).unwrap();
        let publication = test_leaf_publication("main.leaf", &head_sha);

        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        assert_eq!(leaves.len(), 1);
        assert!(
            matches!(leaves[0].action, OrderedBranchAction::Preserve(_)),
            "a missing durable identity must gate destructive cleanup"
        );
    }

    #[tokio::test]
    async fn recreate_refuses_changed_remote_leaf_head() {
        let (temp, project, remote, head_sha) = setup_leaf_fixture();
        let publication = test_leaf_publication("main.leaf", &head_sha);
        let leaves = leaf_branch_cleanups(&project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        let plan = leaf_plan(leaves);

        // Advance the remote leaf head from a second clone.
        let other = temp.path().join("other");
        let run = |dir: &Path, args: &[&str]| {
            let status = std::process::Command::new("git")
                .args(args)
                .current_dir(dir)
                .status()
                .unwrap();
            assert!(status.success(), "git command failed: {args:?}");
        };
        run(
            temp.path(),
            &[
                "clone",
                "-q",
                remote.to_str().unwrap(),
                other.to_str().unwrap(),
            ],
        );
        run(&other, &["config", "user.email", "test@example.invalid"]);
        run(&other, &["config", "user.name", "Test"]);
        run(&other, &["checkout", "-q", "main.leaf"]);
        std::fs::write(other.join("more"), "more\n").unwrap();
        run(&other, &["add", "more"]);
        run(&other, &["commit", "-q", "-m", "more"]);
        run(&other, &["push", "-q", "origin", "main.leaf"]);

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("remote branch"), "{error}");
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
        assert!(project.join(".exo/worktrees/leaf").exists());
    }

    fn leaf_receipt(
        head_sha: &str,
        pr_number: u64,
        remote_name: &str,
        preserved_bundle: Option<PathBuf>,
    ) -> RecreateCleanupReceiptEntry {
        RecreateCleanupReceiptEntry {
            branch: "main.leaf".to_owned(),
            pr_number,
            head_sha: head_sha.to_owned(),
            remote_name: remote_name.to_owned(),
            preservation_checked: true,
            preserved_bundle: preserved_bundle.map(|path| path.display().to_string()),
            actions: Vec::new(),
            remote_deleted: false,
            worktree_removed: false,
            local_branch_deleted: false,
            identity_removed: false,
            completed_at_millis: 0,
        }
    }

    async fn leaf_fixture_plan(project: &Path, head_sha: &str) -> RecreatePlan {
        let publication = test_leaf_publication("main.leaf", head_sha);
        let leaves = leaf_branch_cleanups(project, &[publication], &[], &HashSet::new(), None)
            .await
            .unwrap();
        leaf_plan(leaves)
    }

    #[tokio::test]
    async fn recreate_refuses_missing_receipted_bundle() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        // Every ref is already gone, leaving only the receipt as evidence.
        let worktree = project.join(".exo/worktrees/leaf");
        run_git(
            &project,
            &["worktree", "remove", "--force", worktree.to_str().unwrap()],
            "remove leaf worktree",
        )
        .unwrap();
        run_git(
            &project,
            &["branch", "-D", "main.leaf"],
            "delete leaf branch",
        )
        .unwrap();
        run_git(
            &project,
            &["push", "origin", ":refs/heads/main.leaf"],
            "delete remote leaf ref",
        )
        .unwrap();
        let missing = leaf_preservation_path(&project, "main.leaf", &head_sha);
        assert!(!missing.exists());
        write_recreate_receipts(
            &project,
            &[leaf_receipt(&head_sha, 44, "origin", Some(missing))],
        )
        .await
        .unwrap();

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("missing"), "{error}");
        // The identity is retained so the cleanup can be retried.
        assert!(project.join(".exo/agents/leaf").exists());
    }

    #[tokio::test]
    async fn recreate_refuses_unverifiable_preservation_when_refs_vanish() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        // Refs vanish after planning but before preservation was ever recorded.
        let worktree = project.join(".exo/worktrees/leaf");
        run_git(
            &project,
            &["worktree", "remove", "--force", worktree.to_str().unwrap()],
            "remove leaf worktree",
        )
        .unwrap();
        run_git(
            &project,
            &["branch", "-D", "main.leaf"],
            "delete leaf branch",
        )
        .unwrap();
        run_git(
            &project,
            &["push", "origin", ":refs/heads/main.leaf"],
            "delete remote leaf ref",
        )
        .unwrap();

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(
            error
                .to_string()
                .contains("cannot verify unmerged-commit preservation"),
            "{error}"
        );
        assert!(project.join(".exo/agents/leaf").exists());
    }

    #[tokio::test]
    async fn recreate_refuses_bundle_without_recorded_head() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        // A bundle that verifies structurally but advertises a different head.
        let bundle = leaf_preservation_path(&project, "main.leaf", &head_sha);
        std::fs::create_dir_all(bundle.parent().unwrap()).unwrap();
        run_git(
            &project,
            &["bundle", "create", bundle.to_str().unwrap(), "main"],
            "create wrong-head bundle",
        )
        .unwrap();

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("does not advertise"), "{error}");
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    async fn recreate_rejects_receipt_from_another_publication() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        // Same branch and head, but a different PR number.
        write_recreate_receipts(&project, &[leaf_receipt(&head_sha, 999, "origin", None)])
            .await
            .unwrap();

        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(
            error.to_string().contains("conflicting recreate receipt"),
            "{error}"
        );
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_some());
        assert!(project.join(".exo/worktrees/leaf").exists());

        // Same branch, head, and PR, but a different remote is also foreign.
        write_recreate_receipts(&project, &[leaf_receipt(&head_sha, 44, "upstream", None)])
            .await
            .unwrap();
        let error = destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap_err();
        assert!(
            error.to_string().contains("conflicting recreate receipt"),
            "{error}"
        );
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
    }

    #[tokio::test]
    async fn recreate_reconciles_receipt_with_reappeared_resources() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        // A fully completed prior receipt, but every resource is still present.
        let mut receipt = leaf_receipt(&head_sha, 44, "origin", None);
        receipt.remote_deleted = true;
        receipt.worktree_removed = true;
        receipt.local_branch_deleted = true;
        receipt.identity_removed = true;
        write_recreate_receipts(&project, &[receipt]).await.unwrap();

        destroy_recreate_resources(&project, &Config::default(), &plan, false)
            .await
            .unwrap();

        assert!(!git_branch_exists(&project, "main.leaf").unwrap());
        assert!(!project.join(".exo/worktrees/leaf").exists());
        assert!(!project.join(".exo/agents/leaf").exists());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_none());
        // Unmerged commits were re-derived and preserved despite the stale
        // "no bundle" receipt.
        let bundle = leaf_preservation_path(&project, "main.leaf", &head_sha);
        assert!(bundle.exists());
        verify_preservation_bundle(&project, &bundle, &head_sha).unwrap();
    }

    #[tokio::test]
    async fn recreate_stops_when_forgejo_repo_lookup_fails() {
        let (_temp, project, _remote, head_sha) = setup_leaf_fixture();
        let plan = leaf_fixture_plan(&project, &head_sha).await;
        let config = Config {
            forgejo_url: Some("http://forgejo.invalid".to_owned()),
            forgejo_token: Some("token".to_owned()),
            ..Config::default()
        };

        let error = destroy_recreate_resources(&project, &config, &plan, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("Forgejo repository"), "{error}");
        // No mutation happened before verification.
        assert!(git_branch_exists(&project, "main.leaf").unwrap());
        assert!(remote_branch_sha(&project, "origin", "main.leaf")
            .unwrap()
            .is_some());
        assert!(project.join(".exo/worktrees/leaf").exists());
    }

    #[test]
    fn forgejo_pr_mismatch_verifies_closed_and_merged_prs() {
        use exomonad_core::domain::{BranchName, PRNumber};
        use exomonad_core::services::forgejo::ForgejoPullRequest;

        let publication = test_leaf_publication("main.leaf", "head-a");
        for (state, merged) in [("open", false), ("closed", false), ("closed", true)] {
            let pr = ForgejoPullRequest {
                number: PRNumber::new(44),
                url: String::new(),
                title: String::new(),
                body: String::new(),
                head_ref: BranchName::try_from_str("main.leaf").unwrap(),
                base_ref: BranchName::try_from_str("main").unwrap(),
                state: state.to_owned(),
                merged,
                head_sha: Some("head-a".to_owned()),
                base_sha: None,
                merge_commit_sha: None,
            };
            assert_eq!(
                forgejo_pr_mismatch(&pr, &publication, "main.leaf"),
                None,
                "{state}/{merged}"
            );
            let mut changed = pr.clone();
            changed.head_sha = Some("other".to_owned());
            assert!(
                forgejo_pr_mismatch(&changed, &publication, "main.leaf").is_some(),
                "{state}/{merged}"
            );
            let mut missing_head = pr;
            missing_head.head_sha = None;
            assert!(forgejo_pr_mismatch(&missing_head, &publication, "main.leaf").is_some());
        }
    }

    #[test]
    fn forgejo_client_is_unavailable_for_local_remotes() {
        let (_temp, project, _remote, _head_sha) = setup_leaf_fixture();
        assert!(!project_remote_is_forgejo(&project));
        assert!(recreate_forgejo_client(&project, &Config::default())
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    async fn recreate_requires_confirmation_and_dry_run_has_no_mutations() {
        let tmp = tempfile::tempdir().unwrap();
        let config = Config::default();
        let error = prepare_recreate(tmp.path(), &config, false, false, false, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("--confirm-recreate"));

        let dry_run = prepare_recreate(tmp.path(), &config, false, false, true, false)
            .await
            .unwrap();
        assert!(dry_run.is_none());
        assert!(!tmp.path().join(".exo/published-heads.json").exists());
    }

    #[test]
    fn agent_configuration_environment_propagates_reviewer_settings() {
        let mut config = Config::default();
        config.opencode.tl_model = Some("opencode/tl model".to_string());
        config.opencode.worker_model = Some("opencode/worker".to_string());
        config.reviewer.model = Some("openai/reviewer model".to_string());
        config.reviewer_effort_level = ResolvedEffort::from_cli(EffortLevel::XHigh);

        let environment = agent_configuration_environment(&config);

        assert!(environment.contains("EXOMONAD_TL_MODEL='opencode/tl model'"));
        assert!(environment.contains("EXOMONAD_WORKER_MODEL=opencode/worker"));
        assert!(environment.contains("EXOMONAD_REVIEWER_MODEL='openai/reviewer model'"));
        assert!(environment.contains("EXOMONAD_REVIEWER_EFFORT_LEVEL=xhigh"));

        config.reviewer.model = None;
        let default_environment = agent_configuration_environment(&config);
        assert!(!default_environment.contains(REVIEWER_MODEL_ENV));
        assert!(default_environment.contains("EXOMONAD_REVIEWER_EFFORT_LEVEL=xhigh"));
    }

    #[test]
    fn watcher_dashboard_window_detection_uses_window_name() {
        assert!(has_watcher_dashboard_window(["Server", "Watcher", "TL"]));
        assert!(!has_watcher_dashboard_window(["Server", "TL"]));
    }

    #[tokio::test]
    async fn init_reconciliation_lock_serializes_concurrent_reconcile() {
        // #903: reconcile_existing_session must hold one project-scoped lock
        // across its inspect-then-create sequence, so two concurrent
        // `exomonad init` invocations serialize instead of racing to create
        // duplicate repair windows.
        let dir = tempfile::tempdir().unwrap();
        let project_dir = dir.path().to_path_buf();

        let first = acquire_init_lifecycle_lock_async(&project_dir)
            .await
            .expect("first init should acquire the lifecycle lock");

        let lock_path = project_dir.join(".exo/tl-loop/init-reconcile.lock");
        assert!(lock_path.exists(), "lock file should be published");

        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let contender_dir = project_dir.clone();
        let contender = tokio::spawn(async move {
            started_tx.send(()).unwrap();
            let lock = acquire_init_lifecycle_lock_async(&contender_dir)
                .await
                .expect("second init should eventually acquire the lifecycle lock");
            lock
        });

        started_rx.await.unwrap();
        drop(first);

        let second_lock = contender.await.unwrap();
        assert!(lock_path.exists(), "second init should now hold the lock");
        drop(second_lock);
        assert!(!lock_path.exists(), "lock should be released on drop");
    }

    #[tokio::test]
    async fn plan_transition_recorder_completes_while_lifecycle_lock_is_held() {
        let dir = tempfile::tempdir().unwrap();
        let lifecycle_lock = acquire_init_lifecycle_lock_async(dir.path())
            .await
            .expect("init should acquire the lifecycle lock");
        let accepted = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#.to_vec();
        let expected = accepted.clone();
        let digest = plan_digest(&accepted);
        let recorder_digest = digest.clone();
        let project_dir = dir.path().to_path_buf();

        let recorder = tokio::task::spawn_blocking(move || {
            record_plan_snapshot_bytes(&project_dir, &accepted, &recorder_digest)
        });
        tokio::time::timeout(Duration::from_secs(1), recorder)
            .await
            .expect("plan transition recorder must not wait for lifecycle startup observation")
            .expect("plan transition recorder task must complete")
            .expect("plan transition recorder must persist the accepted plan");

        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            expected
        );
        assert_eq!(
            std::fs::read_to_string(plan_snapshot_digest_path(dir.path())).unwrap(),
            format!("{digest}\n")
        );
        drop(lifecycle_lock);
    }

    #[tokio::test]
    async fn recreate_journal_rejects_recorder_until_recreate_completes() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        let lifecycle_lock = acquire_init_lifecycle_lock_async(dir.path())
            .await
            .expect("init should retain the lifecycle lock during recreate");

        assert_eq!(
            apply_recreate_plan_after_validation(dir.path(), None, || Ok(())).unwrap(),
            None
        );
        assert!(!plan_snapshot_path(dir.path()).exists());
        let journal_path = plan_transition_path(dir.path());
        let prepared_journal = std::fs::read(&journal_path).unwrap();
        let prepared = read_plan_transition(dir.path()).unwrap().unwrap();
        assert_eq!(prepared.0, PLAN_TRANSITION_PHASE_PREPARED);
        let archive_name = prepared.1.expect("recreate should record an archive");

        let accepted = br#"{"plan":{"leaves":[{"name":"waited"}]}}"#;
        let error =
            record_plan_snapshot_bytes(dir.path(), accepted, &plan_digest(accepted)).unwrap_err();
        assert!(error.to_string().contains("plan transition is in progress"));
        assert_eq!(std::fs::read(&journal_path).unwrap(), prepared_journal);
        assert!(!plan_snapshot_path(dir.path()).exists());

        complete_recreate_plan_transition(dir.path(), None).unwrap();
        assert!(!root.exists());
        assert!(dir
            .path()
            .join(".exo/tl-loop")
            .join(archive_name)
            .join("run.json")
            .exists());
        assert!(!journal_path.exists());
        drop(lifecycle_lock);
    }

    #[test]
    fn concurrent_start_does_not_recover_a_live_prepared_transition() {
        let (dir, original, requested) = terminal_transition_fixture();
        let first = acquire_plan_transition_lock(dir.path()).unwrap();
        let archive = root_archive_path_at(dir.path(), 123).unwrap().unwrap();
        begin_plan_transition_locked(
            dir.path(),
            Some(&original),
            Some(&plan_digest(&original)),
            Some(&archive),
        )
        .unwrap();

        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let contender_dir = dir.path().to_path_buf();
        let contender = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            let _lock = acquire_plan_transition_lock(&contender_dir).unwrap();
            let mut failure = None;
            recover_plan_transition_locked(&contender_dir, &mut failure).unwrap();
        });
        started_rx.recv().unwrap();

        assert_eq!(
            read_plan_transition(dir.path()).unwrap().unwrap().0,
            PLAN_TRANSITION_PHASE_PREPARED
        );
        let mut failure = None;
        apply_plan_snapshot_locked(
            dir.path(),
            Some(&requested),
            Some(&plan_digest(&requested)),
            &mut failure,
        )
        .unwrap();
        archive_root_tl_run_to(dir.path(), &archive).unwrap();
        write_plan_transition(dir.path(), PLAN_TRANSITION_PHASE_ARCHIVED, Some(&archive)).unwrap();
        clear_plan_transition_locked(dir.path(), &mut failure).unwrap();
        drop(first);
        contender.join().unwrap();

        assert!(!dir.path().join(".exo/tl-loop/root").exists());
        assert_eq!(
            std::fs::read(plan_snapshot_path(dir.path())).unwrap(),
            requested
        );
        assert!(!plan_transition_path(dir.path()).exists());
    }

    #[test]
    fn forgejo_token_remote_url_rewrites_matching_ssh_origin() {
        let url = forgejo_token_remote_url(
            "git@localhost:exomonad/nemotron-port.git",
            "http://localhost:3000",
            "token-123",
        )
        .unwrap();

        assert_eq!(
            url,
            "http://forgejo_pat:token-123@localhost:3000/exomonad/nemotron-port.git"
        );
    }

    #[test]
    fn forgejo_token_remote_url_ignores_empty_token() {
        assert!(forgejo_token_remote_url(
            "git@localhost:exomonad/nemotron-port.git",
            "http://localhost:3000",
            "  ",
        )
        .is_none());
    }

    #[test]
    fn forgejo_token_remote_url_ignores_different_origin_host() {
        assert!(forgejo_token_remote_url(
            "git@github.com:nanonite/exomonad.git",
            "http://localhost:3000",
            "token-123",
        )
        .is_none());
    }

    #[test]
    fn forgejo_token_remote_url_is_idempotent_with_existing_auth() {
        assert!(forgejo_token_remote_url(
            "http://forgejo_pat:token-123@localhost:3000/exomonad/nemotron-port.git",
            "http://localhost:3000",
            "token-123",
        )
        .is_none());
    }

    fn init_temp_git_repo(remotes: &[(&str, &str)]) -> tempfile::TempDir {
        let tmp = tempfile::tempdir().unwrap();
        init_fixture_git_repository(tmp.path()).unwrap();
        for (name, url) in remotes {
            run_fixture_git_command(tmp.path(), &["remote", "add", name, url]).unwrap();
        }
        tmp
    }

    #[test]
    fn resolve_git_remote_defaults_to_origin_when_unset() {
        let tmp = init_temp_git_repo(&[("origin", "https://github.com/nanonite/repo.git")]);
        assert_eq!(resolve_git_remote(tmp.path()), "origin");
    }

    #[test]
    fn set_git_remote_override_rejects_nonexistent_remote() {
        let tmp = init_temp_git_repo(&[("origin", "https://github.com/nanonite/repo.git")]);
        let err = set_git_remote_override(tmp.path(), "forgejo")
            .expect_err("remote does not exist yet")
            .to_string();
        assert!(err.contains("no such git remote"), "{err}");
    }

    #[test]
    fn set_git_remote_override_persists_and_resolves() {
        let tmp = init_temp_git_repo(&[
            ("origin", "https://github.com/nanonite/repo.git"),
            ("forgejo", "http://localhost:3000/goya/repo.git"),
        ]);
        set_git_remote_override(tmp.path(), "forgejo").unwrap();
        assert_eq!(resolve_git_remote(tmp.path()), "forgejo");
    }

    #[test]
    fn configure_forgejo_remote_targets_configured_remote_not_origin() {
        let tmp = init_temp_git_repo(&[
            ("origin", "git@github.com:nanonite/repo.git"),
            ("forgejo", "git@localhost:goya/repo.git"),
        ]);

        configure_forgejo_remote(tmp.path(), "http://localhost:3000", "token-123", "forgejo")
            .unwrap();

        assert_fixture_git_root(tmp.path()).unwrap();
        let forgejo_url =
            run_fixture_git_command(tmp.path(), &["remote", "get-url", "forgejo"]).unwrap();
        let forgejo_url = String::from_utf8_lossy(&forgejo_url.stdout)
            .trim()
            .to_string();
        assert_eq!(
            forgejo_url,
            "http://forgejo_pat:token-123@localhost:3000/goya/repo.git"
        );

        let origin_url =
            run_fixture_git_command(tmp.path(), &["remote", "get-url", "origin"]).unwrap();
        let origin_url = String::from_utf8_lossy(&origin_url.stdout)
            .trim()
            .to_string();
        assert_eq!(
            origin_url, "git@github.com:nanonite/repo.git",
            "origin must be untouched when the configured remote is 'forgejo'"
        );
    }

    #[test]
    fn parse_remote_repo_parts_uses_last_two_path_segments() {
        let parts =
            parse_remote_repo_parts("git@forge.example:repositories/owner/exomonad.git").unwrap();

        assert_eq!(parts.host, "forge.example");
        assert_eq!(parts.owner, "owner");
        assert_eq!(parts.repo, "exomonad");
    }

    #[test]
    fn init_attaches_existing_session_without_recreate() {
        assert!(should_attach_existing_session(false, true));
    }

    #[test]
    fn init_does_not_attach_when_recreate_requested() {
        assert!(!should_attach_existing_session(true, true));
    }

    #[test]
    fn init_does_not_attach_missing_session() {
        assert!(!should_attach_existing_session(false, false));
    }

    #[test]
    fn init_restarts_tl_for_new_plan_with_live_or_dead_window() {
        assert_eq!(
            tl_window_recovery_action(false, false, true),
            TlWindowRecoveryAction::RemoveAndRelaunch
        );
        assert_eq!(
            tl_window_recovery_action(false, true, false),
            TlWindowRecoveryAction::Keep
        );
        assert_eq!(
            tl_window_recovery_action(false, false, false),
            TlWindowRecoveryAction::Keep
        );
        assert_eq!(
            tl_window_recovery_action(true, true, false),
            TlWindowRecoveryAction::RemoveAndRelaunch
        );
        assert_eq!(
            tl_window_recovery_action(true, false, false),
            TlWindowRecoveryAction::RemoveAndRelaunch
        );
    }

    #[test]
    fn root_tl_resume_requires_missing_or_nonterminal_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        assert!(root_tl_needs_resume(dir.path()).unwrap());

        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_waiting"}}"#).unwrap();
        assert!(root_tl_needs_resume(dir.path()).unwrap());

        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        assert!(!root_tl_needs_resume(dir.path()).unwrap());

        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_failed"}}"#).unwrap();
        assert!(!root_tl_needs_resume(dir.path()).unwrap());

        std::fs::write(root.join("run.json"), r#"{"phase":"done"}"#).unwrap();
        let error = root_tl_needs_resume(dir.path()).unwrap_err();
        assert!(error.to_string().contains("unsupported TL checkpoint"));

        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_waiting"}}"#).unwrap();
        assert!(root_tl_needs_resume(dir.path()).unwrap());
    }

    #[test]
    fn root_tl_resume_reports_corrupt_checkpoint() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), b"{not-json").unwrap();

        let error = root_tl_needs_resume(dir.path()).unwrap_err();
        assert!(error.to_string().contains("invalid TL checkpoint"));
    }

    #[test]
    fn startup_checkpoint_terminal_and_gate_are_operator_visible() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
              root.join("run.json"),
              r#"{"fsm":{"phase":"tl_failed"},"gates":[{"name":"tl-dispatch-failed","status":"pending"}],"slices":{"leaf-a":{"dispatch_error":"worktree is dirty"}}}"#,
          )
          .unwrap();

        let checkpoint = read_startup_checkpoint(dir.path()).unwrap();
        let message = startup_checkpoint_message(&checkpoint).unwrap();
        assert!(message.contains("phase=tl_failed"));
        assert!(message.contains("tl-dispatch-failed"));
        assert!(message.contains("python3 -m tl_loop gate --run-id root"));
        assert!(message.contains("worktree is dirty"));
        record_startup_checkpoint_classification(dir.path(), &checkpoint).unwrap();
        assert!(root.join("startup-classification.json").exists());
    }

    #[test]
    fn startup_checkpoint_nonterminal_is_not_treated_as_success() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            root.join("run.json"),
            r#"{"fsm":{"phase":"tl_waiting"},"gates":[]}"#,
        )
        .unwrap();

        assert_eq!(
            read_startup_checkpoint(dir.path()).unwrap(),
            StartupCheckpoint::Nonterminal {
                phase: "tl_waiting".to_string()
            }
        );
    }

    #[test]
    fn startup_checkpoint_pending_gates_do_not_park_active_phases() {
        let phases = [
            "tl_planning",
            "tl_dispatching",
            "tl_waiting",
            "tl_merging",
            "tl_all_merged",
            "tl_pr_filed",
        ];
        for phase in phases {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path().join(".exo/tl-loop/root");
            std::fs::create_dir_all(&root).unwrap();
            std::fs::write(
                root.join("run.json"),
                format!(
                    r#"{{"fsm":{{"phase":"{phase}"}},"gates":[{{"name":"operator-review","status":"pending"}}]}}"#
                ),
            )
            .unwrap();

            assert_eq!(
                read_startup_checkpoint(dir.path()).unwrap(),
                StartupCheckpoint::Nonterminal {
                    phase: phase.to_string()
                }
            );
        }
    }

    #[test]
    fn startup_checkpoint_missing_is_not_treated_as_success() {
        let dir = tempfile::tempdir().unwrap();

        assert_eq!(
            read_startup_checkpoint(dir.path()).unwrap(),
            StartupCheckpoint::Missing
        );
        assert!(startup_checkpoint_message(&StartupCheckpoint::Missing).is_none());
    }

    #[test]
    fn startup_checkpoint_parse_error_preserves_diagnostic() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"phase":"done"}"#).unwrap();

        let error = read_startup_checkpoint(dir.path()).unwrap_err();
        assert!(error.to_string().contains("unsupported TL checkpoint"));
        assert!(error.to_string().contains("missing string /fsm/phase"));
    }

    #[test]
    fn recreate_refuses_unanswered_gate_without_override() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(
            root.join("run.json"),
            r#"{"fsm":{"phase":"tl_failed"},"gates":[{"name":"review","status":"pending"}]}"#,
        )
        .unwrap();

        let error = ensure_recreate_allowed(dir.path(), false).unwrap_err();
        assert!(error.to_string().contains("--allow-pending-gate"));
        ensure_recreate_allowed(dir.path(), true).unwrap();
    }

    #[test]
    fn start_refuses_nonterminal_checkpoint_and_names_safe_modes() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_waiting"}}"#).unwrap();
        let error = ensure_start_allowed(dir.path()).unwrap_err();
        let message = error.to_string();
        assert!(message.contains("--continue"));
        assert!(message.contains("--recreate"));
    }

    #[test]
    fn start_allows_missing_or_terminal_checkpoint() {
        let missing = tempfile::tempdir().unwrap();
        ensure_start_allowed(missing.path()).unwrap();

        let terminal = tempfile::tempdir().unwrap();
        let root = terminal.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("run.json"), r#"{"fsm":{"phase":"tl_done"}}"#).unwrap();
        ensure_start_allowed(terminal.path()).unwrap();
    }

    #[test]
    fn archive_controller_exit_reason_preserves_prior_failure() {
        let dir = tempfile::tempdir().unwrap();
        record_controller_exit_reason(dir.path(), "controller crashed").unwrap();

        let archive = archive_controller_exit_reason(dir.path())
            .unwrap()
            .expect("exit marker should be archived");
        assert!(!controller_exit_path(dir.path()).exists());
        assert_eq!(
            serde_json::from_str::<Value>(&std::fs::read_to_string(archive).unwrap()).unwrap()
                ["reason"],
            "controller crashed"
        );
    }

    #[test]
    fn controller_exit_marker_from_previous_epoch_is_archived() {
        let dir = tempfile::tempdir().unwrap();
        let marker = controller_exit_path(dir.path());
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, r#"{"reason":"old controller crashed"}"#).unwrap();

        let current_epoch = prepare_controller_spawn(dir.path()).unwrap();

        assert!(!marker.exists());
        assert!(
            controller_exit_reason_for_attempt(dir.path(), &current_epoch)
                .unwrap()
                .is_none()
        );
        let archives = std::fs::read_dir(marker.parent().unwrap())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with("controller-exit-")
            })
            .collect::<Vec<_>>();
        assert_eq!(archives.len(), 1);
        assert_eq!(
            serde_json::from_str::<Value>(&std::fs::read_to_string(archives[0].path()).unwrap())
                .unwrap()["reason"],
            "old controller crashed"
        );
    }

    #[test]
    fn current_controller_exit_marker_is_reported_for_matching_epoch() {
        let dir = tempfile::tempdir().unwrap();
        let current_epoch = prepare_controller_spawn(dir.path()).unwrap();
        record_controller_exit_reason(dir.path(), "current controller crashed").unwrap();

        assert_eq!(
            controller_exit_reason_for_attempt(dir.path(), &current_epoch)
                .unwrap()
                .as_deref(),
            Some("current controller crashed")
        );
        let payload: Value = serde_json::from_str(
            &std::fs::read_to_string(controller_exit_path(dir.path())).unwrap(),
        )
        .unwrap();
        assert_eq!(payload["controller_epoch"], current_epoch);
    }

    #[test]
    fn server_command_preserves_session_and_agent_routing() {
        let config = Config {
            root_agent_type: AgentType::Claude,
            spawn_agent_type: AgentType::OpenCode,
            reviewer: exomonad::config::ReviewerConfig {
                agent_type: AgentType::Codex,
                ..Default::default()
            },
            ..Default::default()
        };

        let command = server_command("workspace", &config, false);
        assert!(command.contains("EXOMONAD_TMUX_SESSION=workspace"));
        assert!(command.contains("EXOMONAD_ROOT_AGENT_TYPE=claude"));
        assert!(command.contains("EXOMONAD_SPAWN_AGENT_TYPE=opencode"));
        assert!(command.contains("EXOMONAD_REVIEWER_AGENT_TYPE=codex"));
        assert!(command.ends_with("/exomonad serve"));
    }

    #[test]
    fn forgejo_env_vars_include_forgejo_and_gh_auth() {
        let vars = forgejo_env_vars("http://localhost:3000", "token-123", Some("reviewer-456"));

        assert!(vars.contains(&("FORGEJO_HOST", "localhost:3000".to_string())));
        assert!(vars.contains(&("GH_HOST", "localhost:3000".to_string())));
        assert!(vars.contains(&("FORGEJO_TOKEN", "token-123".to_string())));
        assert!(vars.contains(&("GH_TOKEN", "token-123".to_string())));
        assert!(vars.contains(&("FORGEJO_REVIEWER_TOKEN", "reviewer-456".to_string())));
        assert!(vars.contains(&("FORGEJO_URL", "http://localhost:3000".to_string())));
    }

    #[test]
    fn forgejo_env_vars_ignore_empty_tokens() {
        assert!(forgejo_env_vars("http://localhost:3000", "  ", None).is_empty());
    }

    #[test]
    fn refresh_agent_session_timestamps_skips_root_and_updates_agents() {
        let dir = tempfile::tempdir().unwrap();
        let agents = dir.path().join(".exo/agents");
        let root = agents.join("root");
        let leaf = agents.join("issue-1-leaf-codex");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&leaf).unwrap();
        std::fs::write(root.join("spawned_at"), "1").unwrap();
        std::fs::write(leaf.join("spawned_at"), "1").unwrap();

        let before = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let updated = refresh_agent_session_timestamps(dir.path()).unwrap();
        let after = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();

        assert_eq!(updated, 1);
        assert_eq!(
            std::fs::read_to_string(root.join("spawned_at")).unwrap(),
            "1"
        );
        let leaf_spawned_at = std::fs::read_to_string(leaf.join("spawned_at"))
            .unwrap()
            .parse::<u64>()
            .unwrap();
        assert!((before..=after).contains(&leaf_spawned_at));
    }

    #[test]
    fn ensure_gitignore_writes_runtime_scaffold_paths_on_fresh_repo() {
        let dir = tempfile::tempdir().unwrap();

        ensure_gitignore(dir.path()).unwrap();
        let content = std::fs::read_to_string(dir.path().join(".gitignore")).unwrap();

        for expected in [
            ".exo/*",
            "!.exo/config.toml",
            "!.exo/roles/",
            "!.exo/lib/",
            "!.exo/rules/",
            ".codex/",
            ".claude/settings.local.json",
            ".opencode/",
            "opencode.json",
            ".chainlink/issues.db",
        ] {
            assert!(
                content.lines().any(|line| line.trim() == expected),
                "missing gitignore entry: {expected}"
            );
        }
    }

    #[test]
    fn ensure_gitignore_only_appends_missing_runtime_scaffold_paths() {
        let dir = tempfile::tempdir().unwrap();
        let gitignore = dir.path().join(".gitignore");
        std::fs::write(&gitignore, "target/\n.exo/*\n.codex/\n").unwrap();

        ensure_gitignore(dir.path()).unwrap();
        let once = std::fs::read_to_string(&gitignore).unwrap();
        ensure_gitignore(dir.path()).unwrap();
        let twice = std::fs::read_to_string(&gitignore).unwrap();

        assert_eq!(once, twice);
        assert_eq!(
            once.lines().filter(|line| line.trim() == ".exo/*").count(),
            1
        );
        assert_eq!(
            once.lines().filter(|line| line.trim() == ".codex/").count(),
            1
        );
        assert!(once.lines().any(|line| line.trim() == ".opencode/"));
        assert!(once.lines().any(|line| line.trim() == "opencode.json"));
    }

    // ── validate_claude_model tests ───────────────────────────────────────
    // Aliases sourced from `claude --help`: 'sonnet' or 'opus'
    // Full IDs accepted via "claude-" prefix.

    #[test]
    fn test_validate_claude_model_aliases() {
        assert!(validate_claude_model("sonnet").is_ok());
        assert!(validate_claude_model("opus").is_ok());
    }

    #[test]
    fn test_validate_claude_model_full_ids() {
        assert!(validate_claude_model("claude-haiku-4-5-20251001").is_ok());
        assert!(validate_claude_model("claude-sonnet-4-6").is_ok());
        assert!(validate_claude_model("claude-opus-4-7").is_ok());
    }

    #[test]
    fn test_validate_claude_model_rejects_invalid() {
        assert!(validate_claude_model("gpt-4o").is_err());
        assert!(validate_claude_model("anthropic/claude-haiku").is_err());
        assert!(validate_claude_model("").is_err());
        assert!(validate_claude_model("haiku").is_err());
        assert!(validate_claude_model("haiku-model").is_err());
    }

    #[test]
    fn test_validate_codex_model_rejects_non_codex_prefixes() {
        assert!(validate_codex_model_name("gpt-5.2-codex").is_ok());
        assert!(validate_codex_model_name("opencode-go/deepseek-v4-flash").is_err());
        assert!(validate_codex_model_name("claude-sonnet-4-6").is_err());
    }

    #[tokio::test]
    async fn reviewer_validation_rejects_cross_harness_model() {
        let error = validate_reviewer_model_for_harness(
            AgentType::Codex,
            Some("opencode-go/deepseek-v4-pro"),
            Some("high"),
        )
        .await
        .unwrap_err();

        assert!(error.to_string().contains("Codex model"));
    }

    #[test]
    fn test_opencode_tl_model_requires_opencode_root_harness() {
        let error = validate_opencode_model_owner(
            AgentType::Claude,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].tl_model",
            "root_agent_type",
        )
        .unwrap_err()
        .to_string();

        assert!(error.contains("[opencode].tl_model"));
        assert!(error.contains("root_agent_type is `claude`"));
    }

    #[test]
    fn test_opencode_worker_model_requires_opencode_worker_harness() {
        let error = validate_opencode_model_owner(
            AgentType::Codex,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].worker_model",
            "spawn_agent_type",
        )
        .unwrap_err()
        .to_string();

        assert!(error.contains("[opencode].worker_model"));
        assert!(error.contains("spawn_agent_type is `codex`"));
    }

    #[test]
    fn test_opencode_model_owner_allows_matching_harness() {
        assert!(validate_opencode_model_owner(
            AgentType::OpenCode,
            Some("opencode-go/deepseek-v4-flash"),
            "[opencode].worker_model",
            "spawn_agent_type",
        )
        .is_ok());
    }

    #[test]
    fn exomonad_mcp_server_uses_resolved_binary_path() {
        let server = exomonad_mcp_server(Path::new("/tmp/bin/exomonad"), "worker", "agent-1");

        assert_eq!(
            server.get("command").and_then(Value::as_str),
            Some("/tmp/bin/exomonad")
        );
        assert_eq!(
            server.get("args"),
            Some(&serde_json::json!([
                "mcp-stdio",
                "--role",
                "worker",
                "--name",
                "agent-1"
            ]))
        );
    }

    #[test]
    fn preflight_failure_precedes_tl_window_creation() {
        let source = include_str!("init.rs");
        let preflight = source
            .find("run_tl_loop_preflight(&cwd")
            .expect("init must run TL preflight");
        let tl_window = source
            .find("ipc.new_window(\"TL\"")
            .expect("init must create the TL window");
        assert!(
            preflight < tl_window,
            "preflight must fail before creating tmux windows"
        );
    }

    #[test]
    fn server_wait_precedes_tl_window_creation() {
        let source = include_str!("init.rs");
        let server_wait = source
            .find("wait_for_server_socket(&cwd).await?")
            .expect("init must wait for the server socket");
        let tl_window = source
            .find("ipc.new_window(\"TL\"")
            .expect("init must create the TL window");
        assert!(
            server_wait < tl_window,
            "the TL window must not launch before the server socket is ready"
        );
    }

    #[tokio::test]
    async fn delayed_server_socket_creation_is_detected_before_timeout() {
        let project = tempfile::tempdir().unwrap();
        let socket_path = project.path().join("server.sock");
        let delayed_path = socket_path.clone();
        let creator = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(75)).await;
            let _listener = tokio::net::UnixListener::bind(delayed_path).unwrap();
            tokio::time::sleep(Duration::from_secs(1)).await;
        });

        wait_for_socket_path(&socket_path, Duration::from_secs(2))
            .await
            .unwrap();
        creator.abort();
    }

    #[test]
    fn stale_server_artifacts_are_removed_when_pid_is_dead() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let socket_path = exo_dir.join("server.sock");
        let pid_path = exo_dir.join("server.pid");
        std::fs::write(&socket_path, "stale socket placeholder").unwrap();
        std::fs::write(&pid_path, r#"{"pid":2147483647}"#).unwrap();

        prepare_server_socket_for_start(project.path()).unwrap();

        assert!(!socket_path.exists());
        assert!(!pid_path.exists());
    }

    #[test]
    fn live_server_artifacts_are_preserved_before_start() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let socket_path = exo_dir.join("server.sock");
        let pid_path = exo_dir.join("server.pid");
        std::fs::write(&socket_path, "live socket placeholder").unwrap();
        std::fs::write(&pid_path, format!(r#"{{"pid":{}}}"#, std::process::id())).unwrap();

        prepare_server_socket_for_start(project.path()).unwrap();

        assert!(socket_path.exists());
        assert!(pid_path.exists());
    }

    #[test]
    fn read_server_pid_record_distinguishes_absent_from_invalid() {
        let temp = tempfile::tempdir().unwrap();
        let pid_path = temp.path().join("server.pid");

        assert_eq!(read_server_pid_record(&pid_path), ServerPidRecord::Absent);
        // PID 0 would signal the caller's process group; PID 1 is init.
        for document in [
            r#"{"pid":0}"#,
            r#"{"pid":1}"#,
            r#"{"pid":4294967295}"#,
            r#"{"pid":"abc"}"#,
            r#"{"role":"server"}"#,
            "not a pid document",
        ] {
            std::fs::write(&pid_path, document).unwrap();
            assert_eq!(
                read_server_pid_record(&pid_path),
                ServerPidRecord::Unreadable,
                "document must be rejected: {document}"
            );
        }
        std::fs::write(&pid_path, format!(r#"{{"pid":{}}}"#, std::process::id())).unwrap();
        assert_eq!(
            read_server_pid_record(&pid_path),
            ServerPidRecord::Pid(std::process::id() as i32)
        );
    }

    #[tokio::test]
    async fn recreate_removes_stale_server_artifacts() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let socket_path = exo_dir.join("server.sock");
        let pid_path = exo_dir.join("server.pid");
        std::fs::write(&socket_path, "stale socket placeholder").unwrap();
        std::fs::write(&pid_path, r#"{"pid":2147483647}"#).unwrap();

        stop_server_for_recreate(project.path()).await.unwrap();

        assert!(!socket_path.exists());
        assert!(!pid_path.exists());
    }

    #[tokio::test]
    async fn recreate_stops_live_server_pid_before_cleanup() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let pid_path = exo_dir.join("server.pid");
        // argv and cwd carry the exomonad/serve/workspace identity the shutdown
        // verification requires, and the shell terminates on SIGTERM.
        let mut server = std::process::Command::new("sh")
            .current_dir(project.path())
            .arg("-c")
            .arg("while :; do sleep 1; done")
            .arg("exomonad")
            .arg("serve")
            .spawn()
            .unwrap();
        std::fs::write(&pid_path, format!(r#"{{"pid":{}}}"#, server.id())).unwrap();

        stop_server_for_recreate(project.path()).await.unwrap();

        let status = server.wait().unwrap();
        assert!(
            !status.success(),
            "the verified server process must have been signalled"
        );
        assert!(!pid_path.exists());
    }

    #[tokio::test]
    async fn recreate_refuses_cleanup_when_pid_is_reused_by_unrelated_process() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let pid_path = exo_dir.join("server.pid");
        let mut unrelated = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        std::fs::write(&pid_path, format!(r#"{{"pid":{}}}"#, unrelated.id())).unwrap();

        let error = stop_server_for_recreate(project.path()).await.unwrap_err();

        assert!(error.to_string().contains("refusing --recreate"));
        assert!(
            unrelated.try_wait().unwrap().is_none(),
            "an unrelated process that reused the pid must never be signalled"
        );
        assert!(pid_path.exists());
        let _ = unrelated.kill();
        let _ = unrelated.wait();
    }

    #[tokio::test]
    async fn recreate_refuses_cleanup_when_server_pid_is_in_another_workspace() {
        let project = tempfile::tempdir().unwrap();
        let other = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let pid_path = exo_dir.join("server.pid");
        // A real exomonad serve argv, but running in a different workspace.
        let mut other_server = std::process::Command::new("sh")
            .current_dir(other.path())
            .arg("-c")
            .arg("while :; do sleep 1; done")
            .arg("exomonad")
            .arg("serve")
            .spawn()
            .unwrap();
        std::fs::write(&pid_path, format!(r#"{{"pid":{}}}"#, other_server.id())).unwrap();

        let error = stop_server_for_recreate(project.path()).await.unwrap_err();

        assert!(error.to_string().contains("refusing --recreate"));
        assert!(
            other_server.try_wait().unwrap().is_none(),
            "a server in another workspace must never be signalled"
        );
        assert!(pid_path.exists());
        let _ = other_server.kill();
        let _ = other_server.wait();
    }

    #[tokio::test]
    async fn recreate_refuses_cleanup_when_live_socket_has_no_verifiable_pid() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let socket_path = exo_dir.join("server.sock");
        let _listener = tokio::net::UnixListener::bind(&socket_path).unwrap();

        let error = stop_server_for_recreate(project.path()).await.unwrap_err();

        assert!(error.to_string().contains("refusing --recreate"));
        assert!(
            socket_path.exists(),
            "a live server socket must not be removed when termination is unverified"
        );
        assert!(!exo_dir.join("server.pid").exists());
    }

    #[tokio::test]
    async fn recreate_refuses_cleanup_when_pid_record_is_invalid() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let pid_path = exo_dir.join("server.pid");
        // No listening socket: an invalid record must still stop cleanup.
        std::fs::write(&pid_path, "not a pid document").unwrap();

        let error = stop_server_for_recreate(project.path()).await.unwrap_err();

        assert!(error.to_string().contains("refusing --recreate"));
        assert!(pid_path.exists());
    }

    #[tokio::test]
    async fn recreate_refuses_cleanup_when_pid_record_is_zero() {
        let project = tempfile::tempdir().unwrap();
        let exo_dir = project.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        let pid_path = exo_dir.join("server.pid");
        std::fs::write(&pid_path, r#"{"pid":0}"#).unwrap();

        let error = stop_server_for_recreate(project.path()).await.unwrap_err();

        assert!(error.to_string().contains("refusing --recreate"));
        assert!(pid_path.exists());
    }

    #[test]
    fn controller_exit_reason_is_durable_and_clearable() {
        let dir = tempfile::tempdir().unwrap();

        record_controller_exit_reason(dir.path(), "plan.json is missing").unwrap();
        assert_eq!(
            controller_exit_reason(dir.path()).as_deref(),
            Some("plan.json is missing")
        );

        record_controller_exit_reason(dir.path(), "replacement reason").unwrap();
        assert_eq!(
            controller_exit_reason(dir.path()).as_deref(),
            Some("plan.json is missing")
        );

        let payload_path = controller_exit_path(dir.path());
        std::fs::write(
            &payload_path,
            r#"{"reason":"ledger tailer stopped","recent_output":"last output line"}"#,
        )
        .unwrap();
        assert_eq!(
            controller_exit_reason(dir.path()).as_deref(),
            Some("ledger tailer stopped; recent controller output: last output line")
        );

        clear_controller_exit_reason(dir.path()).unwrap();
        assert!(controller_exit_reason(dir.path()).is_none());
    }

    #[test]
    fn recreate_archives_terminal_and_live_root_checkpoints_with_evidence() {
        for phase in ["tl_done", "tl_failed", "tl_waiting"] {
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path().join(".exo/tl-loop/root");
            std::fs::create_dir_all(&root).unwrap();
            std::fs::write(
                root.join("run.json"),
                format!(r#"{{"phase":"{phase}","dispatch_error":"preserve-me"}}"#),
            )
            .unwrap();
            std::fs::write(
                root.join("controller-exit.json"),
                r#"{"reason":"specific controller failure"}"#,
            )
            .unwrap();
            std::fs::write(root.join("gates.json"), "prior gate evidence").unwrap();

            let archive = archive_root_tl_run_at(dir.path(), 123).unwrap().unwrap();

            assert!(!root.exists());
            assert_eq!(
                std::fs::read_to_string(archive.join("run.json")).unwrap(),
                format!(r#"{{"phase":"{phase}","dispatch_error":"preserve-me"}}"#)
            );
            assert_eq!(
                std::fs::read_to_string(archive.join("controller-exit.json")).unwrap(),
                r#"{"reason":"specific controller failure"}"#
            );
            assert_eq!(
                std::fs::read_to_string(archive.join("gates.json")).unwrap(),
                "prior gate evidence"
            );
        }
    }

    #[test]
    fn recreate_missing_root_is_a_noop() {
        let dir = tempfile::tempdir().unwrap();

        assert!(archive_root_tl_run_at(dir.path(), 123).unwrap().is_none());
        assert!(!dir.path().join(".exo/tl-loop").exists());
    }

    #[test]
    fn recreate_archive_collision_preserves_existing_archive() {
        let dir = tempfile::tempdir().unwrap();
        let parent = dir.path().join(".exo/tl-loop");
        let root = parent.join("root");
        let existing = parent.join("root.invalid-123");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&existing).unwrap();
        std::fs::write(existing.join("run.json"), "older archive").unwrap();
        std::fs::write(root.join("run.json"), "current checkpoint").unwrap();

        let archive = archive_root_tl_run_at(dir.path(), 123).unwrap().unwrap();

        assert_eq!(archive, parent.join("root.invalid-123-1"));
        assert_eq!(
            std::fs::read_to_string(existing.join("run.json")).unwrap(),
            "older archive"
        );
        assert_eq!(
            std::fs::read_to_string(archive.join("run.json")).unwrap(),
            "current checkpoint"
        );
    }

    #[test]
    fn recreate_rejects_non_directory_root_without_overwriting_it() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(root.parent().unwrap()).unwrap();
        std::fs::write(&root, "not a checkpoint directory").unwrap();

        let error = archive_root_tl_run_at(dir.path(), 123).unwrap_err();

        assert!(error.to_string().contains("expected"));
        assert_eq!(
            std::fs::read_to_string(root).unwrap(),
            "not a checkpoint directory"
        );
    }

    #[test]
    fn startup_failure_output_keeps_fallback_and_recent_diagnostics() {
        let output = (1..=10)
            .map(|line| format!("controller-line-{line}"))
            .collect::<Vec<_>>()
            .join("\n");

        let reason = startup_failure_with_pane_output("startup fallback".to_owned(), &output);

        assert!(reason.starts_with("startup fallback; controller output:"));
        assert!(!reason.split(" | ").any(|line| line == "controller-line-1"));
        assert!(reason.contains("controller-line-3"));
        assert!(reason.contains("controller-line-10"));
    }

    #[test]
    fn recreate_archives_root_before_creating_tl_window() {
        let source = include_str!("init.rs");
        let archive = source
            .find("complete_recreate_plan_transition_locked(&cwd")
            .expect("recreate must archive the prior root checkpoint");
        let tl_window = source
            .find("ipc.new_window(\"TL\"")
            .expect("init must create the TL window");

        assert!(archive < tl_window);
    }

    #[test]
    fn recreate_plan_validation_precedes_destructive_teardown() {
        let source = include_str!("init.rs");
        let validation = source
            .find("controller_plan_digest = apply_recreate_plan_after_validation_locked")
            .expect("recreate must validate and adopt plan identity");
        let teardown = source
            .find("destroy_recreate_resources(&cwd")
            .expect("recreate must have a destructive resource transition");

        assert!(validation < teardown);
    }

    #[test]
    fn tl_loop_command_uses_programmatic_controller() {
        let timeouts = TlLoopTimeouts {
            transport: 45.5,
            active_tail: 60.0,
            task: 90.0,
        };
        let command = tl_loop_command(
            Path::new("/tmp/repo"),
            Path::new("/tmp/exo"),
            &timeouts,
            Some("0123456789abcdef"),
        );
        assert!(command.contains("EXOMONAD_BINARY="));
        assert!(command.contains("EXOMONAD_ROLE=tl"));
        assert!(!command.contains("PYTHONPATH="));
        assert!(command.contains("python3 /tmp/exo run"));
        assert!(command.contains("--wait-for-plan"));
        assert!(command.contains("--expected-plan-digest 0123456789abcdef"));
        assert!(!command.contains("--expected-plan-hex"));
        assert!(command.contains("--transport-timeout 45.5"));
        assert!(command.contains("--active-tail-timeout 60"));
        assert!(command.contains("--task-timeout 90"));
    }

    #[test]
    fn tl_loop_command_retains_pane_and_prints_failure_diagnostics() {
        let timeouts = TlLoopTimeouts {
            transport: 1.0,
            active_tail: 2.0,
            task: 3.0,
        };
        let command = tl_loop_command(
            Path::new("/tmp/repo"),
            Path::new("/tmp/exo"),
            &timeouts,
            None,
        );

        assert!(command.contains("[ \"$status\" -ne 0 ] || [ -f"));
        assert!(command.contains("tmux set-window-option"));
        assert!(command.contains("remain-on-exit on"));
        assert!(command.contains("Controller failure marker:"));
        assert!(command.contains("controller-exit.json"));
        assert!(command.contains("Controller output log:"));
        assert!(command.contains("controller-output.log"));
        assert!(Command::new("sh")
            .args(["-n", "-c", &command])
            .status()
            .unwrap()
            .success());
    }

    #[test]
    fn unexpected_tl_exit_retains_pane_and_prints_marker_diagnostics() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("bin");
        std::fs::create_dir(&bin).unwrap();
        let tmux_call = dir.path().join("tmux-call");
        let tmux = bin.join("tmux");
        std::fs::write(
            &tmux,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > {}\n",
                shell_escape::escape(tmux_call.display().to_string().into())
            ),
        )
        .unwrap();
        std::fs::set_permissions(&tmux, std::fs::Permissions::from_mode(0o755)).unwrap();

        let marker = controller_exit_path(dir.path());
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, r#"{"reason":"unexpected test failure"}"#).unwrap();
        let wrapper =
            tl_controller_wrapper_command(dir.path(), "printf 'live output\\n'; sh -c 'exit 23'");
        let path = format!(
            "{}:{}",
            bin.display(),
            std::env::var("PATH").unwrap_or_default()
        );
        let output = Command::new("env")
            .args([
                format!("PATH={path}"),
                "TMUX_PANE=%0".to_owned(),
                "sh".to_owned(),
                "-c".to_owned(),
                wrapper,
            ])
            .output()
            .unwrap();

        assert_eq!(
            output.status.code(),
            Some(23),
            "stdout: {}\nstderr: {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        let pane = String::from_utf8_lossy(&output.stdout);
        assert!(pane.contains("unexpected test failure"));
        assert!(pane.contains("controller-output.log"));
        assert!(std::fs::read_to_string(tmux_call)
            .unwrap()
            .contains("remain-on-exit on"));
    }

    #[test]
    fn successful_tl_exit_preserves_live_output_without_retaining_pane() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("bin");
        std::fs::create_dir(&bin).unwrap();
        let tmux = bin.join("tmux");
        let tmux_call = dir.path().join("tmux-call");
        std::fs::write(
            &tmux,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" > {}\n",
                shell_escape::escape(tmux_call.display().to_string().into())
            ),
        )
        .unwrap();
        std::fs::set_permissions(&tmux, std::fs::Permissions::from_mode(0o755)).unwrap();

        let wrapper =
            tl_controller_wrapper_command(dir.path(), "printf 'live output\\n'; sh -c 'exit 0'");
        let path = format!(
            "{}:{}",
            bin.display(),
            std::env::var("PATH").unwrap_or_default()
        );
        let output = Command::new("env")
            .args([
                format!("PATH={path}"),
                "TMUX_PANE=%0".to_owned(),
                "sh".to_owned(),
                "-c".to_owned(),
                wrapper,
            ])
            .output()
            .unwrap();

        assert!(output.status.success());
        let pane = String::from_utf8_lossy(&output.stdout);
        assert!(pane.contains("live output"));
        assert!(!pane.contains("exited unexpectedly"));
        assert!(!tmux_call.exists());
    }

    #[test]
    fn successful_tl_startup_still_releases_pane_retention() {
        let source = include_str!("init.rs");
        let startup = source
            .find("let startup = wait_for_tl_controller_startup(&ipc, &cwd")
            .expect("init must wait for TL startup");
        let release = source[startup..]
            .find("if startup.is_ok() {\n        ipc.set_window_remain_on_exit(&tl_window, false)")
            .expect("successful startup must release pane retention");

        assert!(release > 0);
    }

    #[test]
    fn recovery_command_keeps_captured_digest_after_snapshot_replacement() {
        let dir = tempfile::tempdir().unwrap();
        let original = br#"{"plan":{"leaves":[]}}"#;
        let substituted = br#"{"plan":{"leaves":[{"name":"substituted"}]}}"#;
        std::fs::create_dir_all(dir.path().join(".exo/tl-loop")).unwrap();
        std::fs::write(plan_snapshot_path(dir.path()), original).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(original)),
        )
        .unwrap();

        let timeouts = TlLoopTimeouts {
            transport: 1.0,
            active_tail: 2.0,
            task: 3.0,
        };
        let expected = plan_digest(original);
        std::fs::write(plan_snapshot_path(dir.path()), substituted).unwrap();
        std::fs::write(
            plan_snapshot_digest_path(dir.path()),
            format!("{}\n", plan_digest(substituted)),
        )
        .unwrap();

        let command = tl_loop_command(
            dir.path(),
            Path::new("/tmp/exo"),
            &timeouts,
            Some(&expected),
        );

        assert!(command.contains(&format!("--expected-plan-digest {expected}")));
        assert!(!command.contains(&plan_digest(substituted)));
    }

    #[test]
    fn old_tl_loop_interpreter_error_names_found_and_required_versions() {
        let error = validate_tl_loop_python_version((3, 10, 9), (3, 11)).unwrap_err();
        let message = error.to_string();
        assert!(message.contains("Python 3.10.9"));
        assert!(message.contains("Python >= 3.11"));
    }

    #[test]
    fn runtime_build_validation_fails_closed_with_rebuild_instruction() {
        let error = validate_runtime_builds("old", "new").unwrap_err();
        let message = error.to_string();
        assert!(message.contains("server/controller build identities differ"));
        assert!(message.contains("server_build=old"));
        assert!(message.contains("just install-all-dev"));
    }

    #[test]
    fn runtime_validation_ignores_unrelated_managed_project_revision() {
        let project = tempfile::tempdir().unwrap();
        validate_runtime_compatibility(project.path()).unwrap();
    }

    #[test]
    fn runtime_build_validation_rejects_unknown_identity() {
        let error = validate_runtime_builds("unknown", "bf89654f").unwrap_err();
        let message = error.to_string();
        assert!(message.contains("unknown ExoMonad build identity"));
        assert!(message.contains("managed project revision is unrelated"));
    }

    #[test]
    fn runtime_contract_rejects_unsupported_protocol_and_schema() {
        let protocol_error =
            validate_runtime_contract(RUNTIME_PROTOCOL_VERSION + 1, 0).unwrap_err();
        assert!(protocol_error
            .to_string()
            .contains("unsupported ExoMonad runtime protocol version"));

        let schema_error = validate_runtime_contract(
            RUNTIME_PROTOCOL_VERSION,
            PUBLICATION_REGISTRY_SCHEMA_VERSION + 1,
        )
        .unwrap_err();
        assert!(schema_error
            .to_string()
            .contains("unsupported publication registry schema version"));
    }

    #[test]
    fn runtime_validation_precedes_plan_and_recreate_mutations() {
        let source = include_str!("init.rs");
        let validation = source
            .find("validate_runtime_compatibility(&cwd)?")
            .expect("init must validate runtime compatibility");
        let plan = source
            .find("write_tl_loop_plan(&cwd")
            .expect("init must locate the plan write");
        let recreate = source
            .find("complete_recreate_plan_transition_locked(&cwd")
            .expect("init must locate recreation archive");

        assert!(validation < plan);
        assert!(validation < recreate);
    }

    #[test]
    fn publication_registry_schema_accepts_legacy_and_current_versions() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join(".exo");
        std::fs::create_dir_all(&path).unwrap();
        let registry = path.join("published-heads.json");
        for document in [r#"{"heads":[]}"#, r#"{"schema_version":2,"heads":[]}"#] {
            std::fs::write(&registry, document).unwrap();
            validate_publication_registry_schema(directory.path()).unwrap();
        }
        std::fs::write(&registry, r#"{"schema_version":3,"heads":[]}"#).unwrap();
        let error = validate_publication_registry_schema(directory.path()).unwrap_err();
        assert!(error.to_string().contains("unsupported schema version 3"));
    }

    #[test]
    fn missing_controller_archive_is_rejected_without_implicit_install() {
        let directory = tempfile::tempdir().unwrap();
        let error = tl_loop_package_root_at(directory.path()).unwrap_err();
        assert!(error
            .to_string()
            .contains("installed TL controller archive is missing"));
        assert!(!directory.path().join("tl_loop.pyz").exists());
    }

    #[test]
    fn controller_interpreter_policy_is_shared_by_runtime_and_build() {
        assert!(TL_LOOP_INTERPRETER_POLICY.contains("environment = \"EXOMONAD_TL_LOOP_PYTHON\""));
        assert!(TL_LOOP_INTERPRETER_POLICY.contains("fallback = \"python3\""));
        assert!(include_str!("../../../scripts/resolve_tl_loop_python.py")
            .contains("tl_loop/interpreter_policy.toml"));
    }

    #[test]
    fn controller_build_and_runtime_resolvers_agree_for_same_environment() {
        let environment = "EXOMONAD_TL_LOOP_PYTHON";
        let policy =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tl_loop/interpreter_policy.toml");
        let resolver =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts/resolve_tl_loop_python.py");

        for selected in [Some("python3"), None] {
            let mut command = Command::new("python3");
            command.arg(&resolver).arg("--policy").arg(&policy);
            match selected {
                Some(interpreter) => {
                    command.env(environment, interpreter);
                }
                None => {
                    command.env_remove(environment);
                }
            }
            let output = command.output().expect("run build-side resolver");
            assert!(output.status.success());
            let build_side = String::from_utf8(output.stdout)
                .expect("resolver output is UTF-8")
                .trim()
                .to_owned();
            let runtime_side = tl_loop_python_with(|name| {
                (name == environment)
                    .then(|| selected.map(str::to_owned))
                    .flatten()
            });
            assert_eq!(build_side, runtime_side);
        }
    }

    #[test]
    fn structured_initial_prompt_writes_plan_and_rejects_legacy_text() {
        let tmp = tempfile::tempdir().unwrap();
        write_tl_loop_plan(tmp.path(), Some(r#"{"plan":{"leaves":[]}}"#)).unwrap();
        let plan = std::fs::read_to_string(tmp.path().join(".exo/tl-loop/plan.json")).unwrap();
        assert!(plan.contains("\"plan\""));

        let invalid = tempfile::tempdir().unwrap();
        let error = write_tl_loop_plan(invalid.path(), Some("interactive TL prompt")).unwrap_err();
        assert!(error
            .to_string()
            .contains("initial_prompt must be a JSON WorkPlan"));
    }
    #[test]
    fn parses_opencode_verbose_catalog_variants() {
        let catalog = parse_opencode_model_catalog(
            "opencode-go/deepseek-v4-pro\n{\n  \"id\": \"deepseek-v4-pro\",\n  \"providerID\": \"opencode-go\",\n  \"variants\": {\n    \"high\": {},\n    \"max\": {}\n  }\n}\n",
        );

        assert!(catalog["opencode-go/deepseek-v4-pro"].contains("high"));
        assert!(catalog["opencode-go/deepseek-v4-pro"].contains("max"));
        assert!(!catalog["opencode-go/deepseek-v4-pro"].contains("medium"));
    }

    #[test]
    fn embedded_archive_contains_expected_source() {
        let marker = std::env::var("EXOMONAD_TL_LOOP_EXPECT_MARKER").unwrap_or_default();
        let member = std::env::var("EXOMONAD_TL_LOOP_EXPECT_MEMBER")
            .unwrap_or_else(|_| "tl_loop/__init__.py".to_owned());
        let mut child = Command::new("python3")
            .arg("-c")
            .arg(
                "import io, sys, zipfile; source = zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())).read(sys.argv[2]).decode(); assert not sys.argv[1] or sys.argv[1] in source",
            )
            .arg(marker)
            .arg(member)
            .stdin(Stdio::piped())
            .spawn()
            .expect("run embedded archive validator");
        child
            .stdin
            .take()
            .expect("open archive validator stdin")
            .write_all(TL_LOOP_ARCHIVE)
            .expect("write embedded archive");
        let status = child.wait().expect("wait for embedded archive validator");
        assert!(
            status.success(),
            "embedded archive validation failed: {status}"
        );
    }
}
