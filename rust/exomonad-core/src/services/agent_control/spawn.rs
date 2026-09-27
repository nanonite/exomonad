use super::*;
use crate::services::forgejo::{
    ForgejoPullRequest, ForgejoPullRequestReview, ForgejoPullRequestReviewComment,
};

fn resume_spawn_lock() -> &'static tokio::sync::Mutex<()> {
    static LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
}

async fn persist_dispatch_intent(
    project_dir: &Path,
    agent_name: &AgentName,
    intent_id: Option<&str>,
) -> Result<()> {
    let Some(intent_id) = intent_id.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(());
    };
    let agent_dir = project_dir.join(".exo/agents").join(agent_name.as_str());
    fs::create_dir_all(&agent_dir).await?;
    let temporary_path = agent_dir.join("dispatch_intent.tmp");
    fs::write(&temporary_path, intent_id).await?;
    fs::rename(temporary_path, agent_dir.join("dispatch_intent")).await?;
    Ok(())
}

/// How a leaf worktree should be provisioned once branch state is known.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LeafWorktreeAction {
    /// The deterministic birth branch already exists: attach to it.
    Attach,
    /// No branch yet, but an expected head was supplied: create at that revision.
    CreateFromRevision,
    /// No branch and no expected head: create a fresh branch from the base.
    CreateFromBase,
}

/// Select attach-versus-create from verified branch state.
///
/// Branch existence decides attachment; `start_point` is expected-head evidence
/// only and must never gate whether an existing branch is reattached.
fn leaf_worktree_action(branch_exists: bool, start_point: Option<&str>) -> LeafWorktreeAction {
    if branch_exists {
        LeafWorktreeAction::Attach
    } else if start_point.is_some() {
        LeafWorktreeAction::CreateFromRevision
    } else {
        LeafWorktreeAction::CreateFromBase
    }
}

/// The durable name of one provisioning decision, as recorded in the ledger.
fn action_name(action: LeafWorktreeAction) -> &'static str {
    match action {
        LeafWorktreeAction::Attach => "attach",
        LeafWorktreeAction::CreateFromRevision => "create_from_revision",
        LeafWorktreeAction::CreateFromBase => "create_from_base",
    }
}

/// Decide whether a failed branch creation may be recovered by one attach.
///
/// Only the stable branch-exists code proves that another creator won the race.
/// Every other failure is returned unchanged, so an unrelated creation error
/// never turns into an attach.
fn creation_failure_allows_attach(error: &anyhow::Error) -> bool {
    matches!(
        error.downcast_ref::<EffectError>(),
        Some(EffectError::Custom { code, .. }) if code == "worktree.branch_exists"
    )
}

/// The branch state a leaf provisioning decision reads, in the only safe order.
///
/// `ensure_branch_fetched` can materialize the local branch from the remote
/// tracking ref when it is absent, so existence may only be read after the fetch.
/// Reading it first classifies a remote-only branch as absent, sends it down the
/// create path, and lets it succeed only through the BranchExists race recovery.
struct LeafBranchState {
    exists: bool,
    remote: crate::services::git_worktree::RemoteEvidence,
}

/// Everything the leaf provisioning decision reads.
///
/// `branch_exists` and `fresh_remote` both come from [`LeafBranchState`], taken
/// under the lifecycle lock immediately before this attempt created anything, so
/// the decision never reads state another writer has already invalidated.
struct LeafProvisioning<'a, 'b> {
    project_dir: &'a Path,
    worktree_path: &'a Path,
    branch: &'a BranchName,
    base_branch: &'a BranchName,
    /// The leaf these provisioning events are about, so an event names the
    /// agent whose spawn made the decision rather than an anonymous actor.
    agent_name: &'a AgentName,
    branch_exists: bool,
    start_point: Option<&'a str>,
    heads: LeafHeadEvidence<'b>,
    fresh_remote: crate::services::git_worktree::RemoteEvidence,
}

/// The authoritative heads a leaf must prove before its branch is attached.
///
/// Every entry is a record some part of the system already committed to, and
/// every one of them must be proven against the observed branch head. A resume
/// therefore carries up to three: the exact head its caller was authorized
/// against, the prior publication head for the branch, and the head its recovery
/// lineage recorded. Absence of all three is not a pass; it is missing
/// evidence, and the attach fails closed.
#[derive(Default, Clone, Copy)]
struct LeafHeadEvidence<'a> {
    /// The exact head a resume was authorized against, matched by equality.
    expected: Option<&'a str>,
    /// The prior ledger-owned publication head for the branch, matched by
    /// ancestry so unique unpushed commits survive the attach.
    prior_publication: Option<&'a RecordedHead>,
    /// The head the resume's recovery lineage recorded for the branch, matched
    /// by ancestry for the same reason.
    resume_lineage: Option<&'a RecordedHead>,
}

impl<'a> LeafHeadEvidence<'a> {
    fn is_empty(&self) -> bool {
        self.expected.is_none() && self.prior_publication.is_none() && self.resume_lineage.is_none()
    }
}

/// Derive the expected deterministic leaf birth branch.
///
/// Durable identity wins; otherwise deterministic naming from an explicit base
/// branch or the effective birth branch. The branch observed on disk is never an
/// input here, so a wrong-branch worktree cannot redefine the expectation.
fn expected_leaf_birth(
    identity_birth: Option<&BirthBranch>,
    base_branch: Option<&str>,
    effective_birth: &BirthBranch,
    agent_name: &str,
) -> Result<BirthBranch> {
    if let Some(birth) = identity_birth {
        return Ok(birth.clone());
    }
    if let Some(base) = base_branch {
        return BirthBranch::try_from_str(base)
            .context("replacement base branch was empty")
            .map(|branch| branch.child(agent_name));
    }
    Ok(effective_birth.child(agent_name))
}

/// Verify an existing leaf worktree path, if one is present.
///
/// A missing path is a no-op here: the remote-dependent attach checks require
/// fresh evidence and run only after `ensure_branch_fetched`. Used by the
/// preflight and the live-route return, where the path must already exist.
///
/// Reuse is proved exactly as attachment is: a registered worktree on the
/// deterministic branch, plus the whole [`LeafHeadEvidence`] set. An ordinary
/// re-spawn of a live worktree therefore fails closed too when the branch no
/// longer contains a head this owner recorded, instead of quietly continuing on
/// a rewritten branch. Presence of evidence is not required here — only its
/// proof — so a worktree with no recorded head is still reusable.
async fn verify_existing_leaf_worktree(
    git_wt: &GitWorktreeService,
    effective_project_dir: &Path,
    worktree_path: &Path,
    branch_name: &BranchName,
    heads: LeafHeadEvidence<'_>,
    require_existing_worktree: bool,
) -> Result<()> {
    if worktree_path.exists() {
        git_wt
            .verify_existing_worktree(worktree_path, branch_name)
            .map_err(|error| anyhow!(EffectError::from(error)))?;
        verify_leaf_head_evidence(git_wt, effective_project_dir, branch_name, heads).await?;
        return Ok(());
    }
    if require_existing_worktree {
        return Err(anyhow!(EffectError::from(
            crate::services::git_worktree::WorktreeError::PathUnregistered {
                path: worktree_path.display().to_string(),
            },
        )));
    }
    Ok(())
}

/// Prove every authoritative head a leaf attempt carries.
///
/// One implementation for both decisions that can admit a deterministic branch:
/// the attach of a preserved branch and the reuse of a worktree that already
/// holds it. An expected head must match by equality; each recorded head must be
/// contained in the branch, proven by ancestry so unique unpushed commits
/// survive. A recorded head the branch no longer contains, or that this
/// repository cannot resolve at all, is a rewritten or reclaimed branch and is
/// refused.
async fn verify_leaf_head_evidence(
    git_wt: &GitWorktreeService,
    effective_project_dir: &Path,
    branch_name: &BranchName,
    heads: LeafHeadEvidence<'_>,
) -> Result<()> {
    if let Some(head) = heads.expected {
        verify_branch_head(effective_project_dir, branch_name, head).await?;
    }
    if let Some(recorded) = heads.prior_publication {
        verify_recorded_head_coverage(git_wt, branch_name, recorded)?;
    }
    if let Some(recorded) = heads.resume_lineage {
        verify_recorded_head_coverage(git_wt, branch_name, recorded)?;
    }
    Ok(())
}

/// A commit the durable owner records for its deterministic branch.
///
/// This is dispatch or publication evidence, not a second owner: it proves which
/// commit the agent was last given, so a preserved branch whose remote evidence
/// is absent can still be proven to descend from its own recorded work.
#[derive(Debug)]
struct RecordedHead {
    sha: String,
    /// Durable record the SHA was read from, named in refusals.
    evidence: &'static str,
}

/// Read the recorded dispatch/publication head for a deterministic branch.
///
/// Resolution is ordered and deterministic: the latest ledger-owned publication
/// this agent owns for the branch, otherwise the owner invocation record's head
/// when that record is about the same branch.
///
/// Only a ledger-owned publication is evidence. A migrated legacy publication
/// was never verified at its filing boundary, so it cannot prove a commit head —
/// the same rule the watcher applies to publication ownership. A publication
/// filed by another agent, or a head recorded against a different branch, is
/// likewise not evidence for this branch.
///
/// An unreadable publication registry is authoritative, so it ends the search
/// instead of falling through: a possibly stale invocation head must never
/// substitute for a record that could not be read. An invocation record that
/// cannot be parsed is not evidence either.
async fn recorded_branch_head(
    project_dir: &Path,
    agent_name: &AgentName,
    branch_name: &BranchName,
) -> Option<RecordedHead> {
    use crate::services::pr_registry::{
        read_published_heads, PublicationProvenance, PUBLISHED_HEADS_FILENAME,
    };

    let publications = match read_published_heads(project_dir).await {
        Ok(publications) => publications,
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                %error,
                "Publication registry is unreadable; no recorded head can be proven for this branch"
            );
            return None;
        }
    };
    let owned = |head: &&crate::services::pr_registry::PublishedHead| {
        head.head_branch == branch_name.as_str()
            && head.author_agent.as_deref() == Some(agent_name.as_str())
    };
    let publication = publications
        .iter()
        .filter(owned)
        .rfind(|head| head.provenance == PublicationProvenance::LedgerOwned)
        .map(|head| head.head_sha.trim().to_string())
        .filter(|sha| !sha.is_empty());
    if let Some(sha) = publication {
        return Some(RecordedHead {
            sha,
            evidence: PUBLISHED_HEADS_FILENAME,
        });
    }

    let agent_dir = project_dir.join(".exo/agents").join(agent_name.as_str());
    let record = read_invocation_conservatively(&agent_dir).await?;
    if record.branch.as_deref() != Some(branch_name.as_str()) {
        return None;
    }
    let sha = record.head_sha?.trim().to_string();
    (!sha.is_empty()).then_some(RecordedHead {
        sha,
        evidence: "invocation.json",
    })
}

/// Resolve the head a resume's recovery lineage carries for its branch.
///
/// Resolution is the stale-lineage proof, so it happens before any worktree
/// decision. A resume is authorized against exactly one prior generation: if
/// the durable record is unreadable, missing, or no longer the invocation the
/// lineage names, the resume target is not the one that was approved and the
/// attach is refused with a typed ownership conflict. Nothing here inspects git,
/// so the result can be threaded into the single attach predicate rather than
/// opening a second resume path.
///
/// The head is read only from a record that names this deterministic branch, so
/// a SHA recorded against another branch is not evidence for this one. `Ok(None)`
/// means the lineage verified and simply recorded no head; only another
/// authoritative record can then prove the branch.
async fn resolve_resume_lineage_head(
    project_dir: &Path,
    agent_name: &AgentName,
    branch_name: &BranchName,
    lineage: &RecoveryInvocationLineage,
) -> Result<Option<RecordedHead>> {
    let agent_dir = project_dir.join(".exo/agents").join(agent_name.as_str());
    let Some(record) = read_invocation_conservatively(&agent_dir).await else {
        return Err(branch_ownership_conflict(format!(
            "branch {branch_name} cannot be resumed: {agent_name} has no readable invocation record to \
             prove the recovery lineage. Restore that record, or re-dispatch the leaf from its \
             deterministic branch, then retry the resume."
        )));
    };
    if record.invocation_id != lineage.prior_invocation_id {
        return Err(branch_ownership_conflict(format!(
            "branch {branch_name} cannot be resumed: the recovery lineage names prior invocation {}, \
             but {agent_name}'s durable record is now {}. Re-read the owner's current invocation and \
             retry the resume against it.",
            lineage.prior_invocation_id, record.invocation_id
        )));
    }
    if record.branch.as_deref() != Some(branch_name.as_str()) {
        return Ok(None);
    }
    let sha = record.head_sha.as_deref().unwrap_or_default().trim();
    Ok((!sha.is_empty()).then(|| RecordedHead {
        sha: sha.to_string(),
        evidence: "the resume lineage head in invocation.json",
    }))
}

/// Typed attach refusal: the branch is deterministic, so the message names the
/// branch, the conflicting or missing evidence, and the operator action.
fn branch_ownership_conflict(detail: String) -> anyhow::Error {
    anyhow!(EffectError::custom(
        "worktree.branch_ownership_conflict",
        detail
    ))
}

/// The stable code that identifies one typed resource-creation refusal.
///
/// The controller's dispatch classification reads this code and nothing else.
/// It is read from the error's typed variant, never recovered from prose, so a
/// reworded message can never change whether a failure is retried.
fn stable_error_code(error: &anyhow::Error) -> Option<&str> {
    match error.downcast_ref::<EffectError>() {
        Some(EffectError::Custom { code, .. }) => Some(code.as_str()),
        _ => None,
    }
}

/// The leaf branch holds a machine code whose retryability is a reviewed fact.
///
/// Every other code, including an untyped error, is terminal: the controller
/// fails closed rather than inferring that an untyped refusal is temporary.
pub(crate) const BRANCH_OWNERSHIP_CONFLICT_CODE: &str = "worktree.branch_ownership_conflict";

/// What the attach predicate proved about a deterministic branch.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LeafAttachability {
    /// Nothing holds the branch, so it can be attached at the leaf path.
    Attachable,
    /// The branch is already checked out at the deterministic leaf path.
    ReuseExistingWorktree,
}

/// Verify that an absent-worktree branch may be attached.
///
/// Must be called only after `ensure_branch_fetched` so `fresh_remote` reflects
/// the current remote. Requires authoritative head evidence; failures carry
/// stable machine codes through [`EffectError`].
async fn verify_attachable_branch(
    git_wt: &GitWorktreeService,
    effective_project_dir: &Path,
    worktree_path: &Path,
    branch_name: &BranchName,
    heads: LeafHeadEvidence<'_>,
    fresh_remote: crate::services::git_worktree::RemoteEvidence,
) -> Result<LeafAttachability> {
    use crate::services::git_worktree::RemoteEvidence;
    let branch_exists = git_wt
        .branch_exists(branch_name)
        .map_err(|error| anyhow!(EffectError::from(error)))?;
    if !branch_exists {
        return Ok(LeafAttachability::Attachable);
    }
    // A branch is checked out in exactly one registered worktree. Being held at
    // the deterministic leaf path is recoverable reuse; being held anywhere
    // else is a conflict that names both paths.
    let owner = match git_wt.registered_worktree_for_branch(branch_name) {
        Ok(owner) => owner,
        Err(crate::services::git_worktree::WorktreeError::BranchOwnershipConflict {
            path, ..
        }) => {
            return Err(checked_out_elsewhere(branch_name, worktree_path, &path));
        }
        Err(error) => return Err(anyhow!(EffectError::from(error))),
    };
    if let Some(owner) = owner {
        if std::fs::canonicalize(worktree_path).ok().as_deref() == Some(owner.as_path()) {
            verify_existing_leaf_worktree(
                git_wt,
                effective_project_dir,
                worktree_path,
                branch_name,
                heads,
                true,
            )
            .await?;
            return Ok(LeafAttachability::ReuseExistingWorktree);
        }
        return Err(checked_out_elsewhere(
            branch_name,
            worktree_path,
            &owner.display().to_string(),
        ));
    }
    // Authoritative head evidence is required before attaching a preserved
    // branch. Remote state is only accepted when freshly verified; otherwise an
    // expected resume head, a recorded dispatch/publication head, or a resume
    // lineage head must prove the local head. Durable identity alone proves
    // ownership, not a commit.
    match fresh_remote {
        RemoteEvidence::Unavailable => {
            if heads.is_empty() {
                return Err(branch_ownership_conflict(format!(
                    "branch {branch_name} cannot be attached: the configured remote could not be inspected, \
                     and no head is recorded for it. Restore remote access, or re-dispatch the leaf with \
                     its recorded head, then retry the spawn."
                )));
            }
        }
        RemoteEvidence::Absent => {
            if heads.is_empty() {
                return Err(branch_ownership_conflict(format!(
                    "branch {branch_name} cannot be attached: the configured remote has no head for it, \
                     and no head is recorded for it. Push the branch to the configured remote, or \
                     re-dispatch the leaf so its publication head is recorded, then retry the spawn."
                )));
            }
        }
        RemoteEvidence::AtSha(remote_sha) => {
            if !git_wt
                .local_head_is_current_or_ahead(branch_name)
                .map_err(|error| anyhow!(EffectError::from(error)))?
            {
                return Err(branch_ownership_conflict(format!(
                    "branch {branch_name} cannot be attached: it is behind or diverged from its remote \
                     head {remote_sha}. Reconcile {branch_name} with the configured remote, or \
                     re-dispatch the leaf from {remote_sha}, then retry the spawn."
                )));
            }
        }
    }
    // Every authoritative head this attempt carries is proven, not just the
    // strongest one, and by the same function that proves reuse. A resume whose
    // lineage head the branch no longer contains is a stale or rewritten target,
    // and is refused here rather than attached.
    verify_leaf_head_evidence(git_wt, effective_project_dir, branch_name, heads).await?;
    Ok(LeafAttachability::Attachable)
}

/// Prove a preserved branch's local head equals or descends from the recorded
/// head. Equal or descendant coverage is accepted so unique unpushed commits
/// survive the reattach; anything else fails closed and names both the expected
/// and the observed head.
///
/// Two refusals come out of this, and they are different facts:
///
/// | Coverage | Meaning | Why it refuses |
/// |----------|---------|---------------|
/// | `Diverged` | both commits are here, neither is an ancestor of the other | the branch was rewritten, so the recorded work is not on it |
/// | `UnknownCommit` | this repository cannot resolve the recorded commit at all | the branch was rewritten, or the recorded commit was reclaimed by garbage collection, so there is nothing to compare against |
///
/// `UnknownCommit` is intended on both the attach and the reuse path: a recorded
/// head nobody can produce again is a branch whose history is gone, and
/// continuing on it would hand the leaf work that descends from nothing this
/// owner ever published.
fn verify_recorded_head_coverage(
    git_wt: &GitWorktreeService,
    branch_name: &BranchName,
    recorded: &RecordedHead,
) -> Result<()> {
    use crate::services::git_worktree::HeadCoverage;
    let observed = git_wt
        .local_head(branch_name)
        .map_err(|error| anyhow!(EffectError::from(error)))?;
    match git_wt
        .head_coverage(branch_name, &recorded.sha)
        .map_err(|error| anyhow!(EffectError::from(error)))?
    {
        HeadCoverage::Covers => Ok(()),
        HeadCoverage::Diverged => Err(branch_ownership_conflict(format!(
            "branch {branch_name} cannot be attached or reused: its head {observed} does not contain \
             the {} head {}. Restore that commit onto {branch_name}, or re-dispatch the leaf from it, \
             then retry the spawn.",
            recorded.evidence, recorded.sha
        ))),
        HeadCoverage::UnknownCommit => Err(branch_ownership_conflict(format!(
            "branch {branch_name} cannot be attached or reused: the {} head {} is unknown to this \
             repository, so the branch was rewritten or that commit was garbage-collected (its head \
             is {observed}). Restore that commit onto {branch_name}, or re-dispatch the leaf from its \
             current head, then retry the spawn.",
            recorded.evidence, recorded.sha
        ))),
    }
}

/// The deterministic branch is checked out at a path this spawn does not own.
fn checked_out_elsewhere(
    branch_name: &BranchName,
    worktree_path: &Path,
    owner: &str,
) -> anyhow::Error {
    branch_ownership_conflict(format!(
        "branch {branch_name} is checked out at {owner}, not at the deterministic leaf path {}. \
         Stop the agent holding {branch_name} or remove that worktree, then retry the spawn.",
        worktree_path.display()
    ))
}

/// What resolving the pull request for a deterministic branch found.
///
/// `NoQualifyingPr` and `LookupFailed` are kept apart on purpose: "the forge
/// answered and no open PR carries this branch at this head" is a fact a spawn
/// may proceed on, while "the forge could not be asked" is an outage, and
/// collapsing the two would resume a leaf blind to the PR it owns.
enum PullRequestContext {
    /// An open PR carries the deterministic branch at the verified head, and
    /// this is the task text that names it.
    Resolved(String),
    /// The query was answered and nothing qualifies.
    NoQualifyingPr,
    /// The query could not be answered, with the underlying reason.
    LookupFailed(String),
}

/// Whether a pull request still carries the exact work this spawn verified.
///
/// The head SHA is what makes the match exact: a PR on the right branch whose
/// head has moved describes commits this branch does not have, and a PR the
/// forge reports no head for cannot be matched at all.
fn pr_carries_verified_head(
    pr: &ForgejoPullRequest,
    branch_name: &BranchName,
    verified_head: &str,
) -> bool {
    !verified_head.is_empty()
        && !pr.merged
        && pr.state.eq_ignore_ascii_case("open")
        && pr.head_ref == *branch_name
        && pr.head_sha.as_deref() == Some(verified_head)
}

/// The task text that tells a leaf which pull request it is continuing.
fn pr_resume_context(
    pr: &ForgejoPullRequest,
    reviews: &[ForgejoPullRequestReview],
    inline: &[ForgejoPullRequestReviewComment],
) -> String {
    let mut context = format!(
        "\n\nIMPORTANT: You are resuming work on an existing pull request, not starting fresh.\n\
         Existing PR: #{} — {}\n\
         Do NOT create a new pull request. Continue working on this branch.\n",
        pr.number.as_u64(),
        pr.title
    );
    for comment in inline {
        let file_label = comment.path.as_deref().unwrap_or("unknown file");
        context.push_str(&format!(
            "Review comment on {}: {}\n",
            file_label, comment.body
        ));
    }
    let bodies = reviews
        .iter()
        .map(|review| review.body.as_str())
        .filter(|body| !body.is_empty())
        .collect::<Vec<_>>();
    if !bodies.is_empty() {
        context.push_str("\nExisting review feedback:\n");
        for body in bodies {
            context.push_str(body);
            context.push('\n');
        }
    }
    context
}

/// Removes a worktree created by this spawn attempt if provisioning fails
/// before the agent is finalized. Dropping the guard on any `?` error path is
/// what makes cleanup reliable instead of relying on later statements.
struct WorktreeRollback {
    git_wt: Arc<GitWorktreeService>,
    path: PathBuf,
    armed: bool,
}

impl WorktreeRollback {
    fn armed(git_wt: Arc<GitWorktreeService>, path: PathBuf) -> Self {
        Self {
            git_wt,
            path,
            armed: true,
        }
    }

    fn defuse(&mut self) {
        self.armed = false;
    }
}

impl Drop for WorktreeRollback {
    fn drop(&mut self) {
        if self.armed && self.path.exists() {
            if let Err(error) = self.git_wt.remove_workspace(&self.path) {
                warn!(
                    path = %self.path.display(),
                    %error,
                    "Failed to roll back a partially created leaf worktree"
                );
            }
        }
    }
}

fn parse_git_status_paths(stdout: &[u8]) -> Vec<String> {
    let records: Vec<&[u8]> = stdout
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
        .collect();
    let mut paths = Vec::new();
    let mut index = 0;
    while index < records.len() {
        let record = records[index];
        if record.len() >= 4 {
            let status = &record[..2];
            paths.push(String::from_utf8_lossy(&record[3..]).into_owned());
            if status.iter().any(|byte| matches!(byte, b'R' | b'C')) {
                if let Some(previous_path) = records.get(index + 1) {
                    paths.push(String::from_utf8_lossy(previous_path).into_owned());
                    index += 1;
                }
            }
        }
        index += 1;
    }
    paths
}

fn is_tl_runtime_checkpoint(path: &str) -> bool {
    let normalized = path.strip_prefix("./").unwrap_or(path);
    normalized == ".exo/tl-loop" || normalized.starts_with(".exo/tl-loop/")
}

fn resolve_identity_working_dir(project_dir: &Path, working_dir: &Path) -> PathBuf {
    if working_dir.is_absolute() {
        working_dir.to_path_buf()
    } else {
        project_dir.join(working_dir)
    }
}

async fn find_existing_leaf_worktree_by_slug(
    worktree_base: &Path,
    slug: &str,
) -> Result<Option<(AgentIdentity, PathBuf)>> {
    let mut entries = match fs::read_dir(worktree_base).await {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };

    let mut candidates = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        let file_type = entry.file_type().await?;
        if !file_type.is_dir() {
            continue;
        }

        let name = entry.file_name().to_string_lossy().to_string();
        candidates.push((name, entry.path()));
    }

    candidates.sort_by(|left, right| left.0.cmp(&right.0));
    for (name, path) in candidates {
        let identity = AgentIdentity::from_internal_name(&name);
        if normalize_agent_slug(identity.slug()) == normalize_agent_slug(slug) {
            return Ok(Some((identity, path)));
        }
    }

    Ok(None)
}

fn reviewer_harness_denied_tools() -> Vec<String> {
    [
        "Edit",
        "Write",
        "NotebookEdit",
        "spawn_leaf",
        "spawn_worker",
        "merge_pr",
        "file_pr",
    ]
    .into_iter()
    .map(str::to_string)
    .collect()
}

async fn preflight_reviewer_hook_environment(project_dir: &Path) -> Result<()> {
    let binary_path = crate::util::find_exomonad_binary();
    if binary_path.components().count() > 1 && !binary_path.exists() {
        anyhow::bail!(
            "Reviewer hook preflight failed: exomonad hook binary not found at {}",
            binary_path.display()
        );
    }

    let socket_path = project_dir.join(".exo/server.sock");
    if !socket_path.exists() {
        anyhow::bail!(
            "Reviewer hook preflight failed: parent server socket missing at {}",
            socket_path.display()
        );
    }

    match tokio::time::timeout(
        std::time::Duration::from_secs(2),
        tokio::net::UnixStream::connect(&socket_path),
    )
    .await
    {
        Ok(Ok(_stream)) => Ok(()),
        Ok(Err(error)) => anyhow::bail!(
            "Reviewer hook preflight failed: parent server socket unreachable at {}: {}",
            socket_path.display(),
            error
        ),
        Err(_) => anyhow::bail!(
            "Reviewer hook preflight failed: timed out connecting to parent server socket at {}",
            socket_path.display()
        ),
    }
}

fn dirty_spawn_error(files: &[String]) -> anyhow::Error {
    let mut message = format!(
        "BLOCKED: cannot spawn agent into a dirty TL worktree. {} file(s) have uncommitted changes:",
        files.len()
    );
    for file in files {
        message.push_str("\n  ");
        message.push_str(file);
    }
    message.push_str("\nCommit the scaffold (per scaffold-fork-converge) or run `discard_worker_output` if throwaway, then retry. Workers spawn in-place and would inherit this state; dev-leaves fork from your branch HEAD and would not see your uncommitted work.");
    anyhow!(message)
}

async fn is_gitignored_path(worktree: &Path, file: &str) -> Result<bool> {
    let output = Command::new("git")
        .args(["check-ignore", "--no-index", "-q", "--", file])
        .current_dir(worktree)
        .output()
        .await
        .with_context(|| {
            format!(
                "failed to inspect gitignore rules in {}",
                worktree.display()
            )
        })?;

    match output.status.code() {
        Some(0) => Ok(true),
        Some(1) => Ok(false),
        _ => anyhow::bail!(
            "failed to inspect gitignore rules for {} in {}: {}",
            file,
            worktree.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        ),
    }
}

async fn filter_gitignored_paths(worktree: &Path, files: Vec<String>) -> Result<Vec<String>> {
    let mut visible_files = Vec::new();
    for file in files {
        if !is_gitignored_path(worktree, &file).await? {
            visible_files.push(file);
        }
    }
    Ok(visible_files)
}

async fn ensure_clean_spawn_worktree(worktree: &Path) -> Result<()> {
    let output = Command::new("git")
        .args(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        .current_dir(worktree)
        .output()
        .await
        .with_context(|| format!("failed to inspect git status in {}", worktree.display()))?;

    if !output.status.success() {
        anyhow::bail!(
            "failed to inspect git status in {}: {}",
            worktree.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let files = parse_git_status_paths(&output.stdout)
        .into_iter()
        .filter(|path| !is_tl_runtime_checkpoint(path))
        .collect();
    let files = filter_gitignored_paths(worktree, files).await?;
    if files.is_empty() {
        Ok(())
    } else {
        Err(dirty_spawn_error(&files))
    }
}

async fn verify_branch_head(
    project_dir: &Path,
    branch: &BranchName,
    expected_sha: &str,
) -> Result<()> {
    info!(branch = %branch, expected_sha, "Verifying resumed branch head");
    let revision = format!("{}^{{commit}}", branch.as_str());
    let output = Command::new("git")
        .args(["rev-parse", "--verify", revision.as_str()])
        .current_dir(project_dir)
        .output()
        .await
        .with_context(|| format!("failed to inspect head of branch {}", branch))?;
    if !output.status.success() {
        return Err(anyhow!(EffectError::custom(
            "worktree.branch_ownership_conflict",
            format!(
                "could not resolve head of resumed branch {}: {}",
                branch,
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        )));
    }
    let actual_sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if actual_sha != expected_sha {
        return Err(anyhow!(EffectError::custom(
            "worktree.branch_ownership_conflict",
            format!(
                "resumed branch {} points at {}, expected {}",
                branch, actual_sha, expected_sha
            ),
        )));
    }
    info!(branch = %branch, actual_sha, "Resumed branch head matches PR head SHA");
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveWorker {
    name: String,
    age: String,
}

fn format_worker_age(duration: std::time::Duration) -> String {
    let seconds = duration.as_secs();
    if seconds < 60 {
        format!("{}s", seconds)
    } else if seconds < 60 * 60 {
        format!("{}m", seconds / 60)
    } else {
        format!("{}h", seconds / (60 * 60))
    }
}

fn active_worker_error(worker: &ActiveWorker) -> anyhow::Error {
    anyhow!(
        "BLOCKED: workers are sequential per CLAUDE.md and docs/decisions/agent-lifecycle-invariants.md. Active worker in this TL worktree: `{}` (spawned {} ago).\n\nOptions:\n1. Wait for the active worker's handoff before spawning the next.\n2. Use `spawn_leaf` for parallel work that warrants its own PR — dev-leaves have their own worktrees and don't share state.\n\nPer-worker attribution to allow parallel workers in one worktree is explicitly out of scope (see ADR § Out of Scope).",
        worker.name,
        worker.age
    )
}

pub(crate) const ROOT_CONTEXT_RELATIVE_PATH: &str = ".exo/roles/devswarm/context/root.md";

pub const CODEX_TL_RUNTIME_NOTES: &str = "\
## Codex Runtime Notes
- ExoMonad manages Codex TLs in tmux and routes messages through Codex's supported delivery paths, not Claude Code Teams inboxes.
- Codex hooks are shell-native and configured in `.codex/hooks.json` for PreToolUse, PostToolUse, and Stop. SessionStart is Claude-specific and is not part of the Codex hook set.
- If you manually restart Codex from the TL shell, restart with `codex --dangerously-bypass-approvals-and-sandbox --cd <project-root>` so ExoMonad hooks do not enter Codex's hook review queue.
- Context inheritance for Codex children uses `codex fork <session_id>`, not ClaudeSessionRegistry.
";

pub const OPENCODE_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Handle one assignment in this process: read the task, implement it, publish the authoritative PR result, and exit cleanly. One-shot means one assignment per process, not non-interactive execution.

While this invocation is alive, continue consuming durable inbox guidance delivered through the validated tmux target for this exact invocation. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
These names are MCP tools exposed inside your agent tool interface. They are not shell commands, are not on PATH, and must not be invoked with bash commands like `which file_pr` or `file_pr ...`.

- file_pr: Create/update a PR for your branch. Call this when your implementation is ready, and again after pushing review fixes.
- notify_parent: Send a message to your parent TL when context calls for direct handoff. Use status 'success' for completed handoffs and 'failure' if you are stuck and cannot proceed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send a message by injecting it into another agent tmux pane.
- send_mailbox_message: Send a message through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_session_start: Start the Chainlink session.
- chainlink_session_status: Read Chainlink session status.
- chainlink_issue_show: Read the assigned Chainlink issue.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_subissue_create: Create a subissue when the task requires decomposition.
- chainlink_session_work: Mark the assigned Chainlink issue as active work.
- chainlink_session_end: End the Chainlink session with handoff notes.
- chainlink_subissue_close: Close a completed subissue.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify (exact commands in your spec).
4. Call file_pr to create the PR as the authoritative publication.
5. Call notify_parent with status='success' or status='failure' for the handoff, then exit. Do not wait for reviewer approval, CI, merge-ready, or merge.
6. If review guidance arrives before this invocation exits, consume it through the durable inbox and exact validated tmux target. If it arrives after exit, the TL uses `resume_pr` to start a fresh invocation in the same owner worktree, branch, and PR, with pending guidance visible at startup.
7. Use notify_parent with status='success' or status='failure' when direct handoff is
   appropriate for the context, including completion outside the normal review loop or being
   truly stuck after multiple attempts.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call spawn_leaf — you are a leaf, not a TL.
- NEVER merge PRs. Never call merge_pr, never run `gh pr merge`, never use any bash tool (ctx_execute, shell commands) to merge. Merging is exclusively the parent TL's responsibility.
- Git operations (status, commit, push) use bash. EXCEPTION: file_pr is the MCP tool for PRs — never use `gh pr create`.
- Do not create a new owner, branch, or stacked PR for review fixes.
";

pub const OPENCODE_WORKER_INSTRUCTIONS: &str = "\
# ExoMonad Worker Agent Protocol

You are an OpenCode worker in an ExoMonad agent tree. You run in a shared workspace pane and do not have your own branch.

## Your Job
Complete the narrow task assigned by your parent TL. Report completion through the provided MCP tools.

## MCP Tools Available
- chainlink_session_start: Start the Chainlink session before marking work active.
- chainlink_session_work: Mark the assigned Chainlink issue as the active work item.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_session_end: End the Chainlink session with handoff notes.
- notify_parent: Send a direct message to your parent TL when needed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send messages to other agents through tmux when explicitly instructed.
- send_mailbox_message: Send messages through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative parent direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_issue_show: Read the assigned Chainlink issue.

## Workflow
1. Read the prompt carefully and use the issue ID provided by the TL.
2. Call chainlink_session_start.
3. Call chainlink_session_work before doing the requested work.
4. Call chainlink_issue_comment for the required progress marker.
5. Call chainlink_session_end with concise handoff notes when done.
6. Call notify_parent with status='success' and include the issue ID.

## Key Rules
- Never spawn agents; workers are leaf executors.
- Never create Chainlink issues; only the parent TL creates issues.
- A `review-stuck` signal is a human-clarification handoff; never create a replacement issue or respawn work for it.
- Never initialize Chainlink agent identity; ExoMonad branch/session identity is authoritative.
- Never close Chainlink issues; your parent coordinator reviews the handoff and closes.
- Never create branches, commits, or PRs unless explicitly instructed.
";

pub const CODEX_DEV_INSTRUCTIONS: &str = "\
# ExoMonad Dev Agent Protocol

You are a Codex dev agent in an ExoMonad agent tree. You work in your own git worktree on your own branch.

## Your Job
Handle one assignment in this process: read the task, implement it, publish the authoritative PR result, and exit cleanly. One-shot means one assignment per process, not non-interactive execution.

While this invocation is alive, continue consuming durable inbox guidance delivered through the validated tmux target for this exact invocation. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
- file_pr: Create/update a PR for your branch. Call this when your implementation is ready, and again after pushing review fixes.
- notify_parent: Send a message to your parent TL when context calls for direct handoff. Use status 'success' for completed handoffs and 'failure' if you are stuck and cannot proceed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send a message by injecting it into another agent tmux pane.
- send_mailbox_message: Send a message through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_session_start: Start the Chainlink session.
- chainlink_session_status: Read Chainlink session status.
- chainlink_issue_show: Read the assigned Chainlink issue.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_subissue_create: Create a subissue when the task requires decomposition.
- chainlink_session_work: Mark the assigned Chainlink issue as active work.
- chainlink_session_end: End the Chainlink session with handoff notes.
- chainlink_subissue_close: Close a completed subissue.

## Workflow
1. Read the spec carefully. Re-read any files mentioned before editing.
2. Implement the changes on your branch.
3. Build and verify using the exact commands in your spec.
4. Call file_pr to create the PR as the authoritative publication.
5. Call notify_parent with status='success' or status='failure' for the handoff, then exit. Do not wait for reviewer approval, CI, merge-ready, or merge.
6. If review guidance arrives before this invocation exits, consume it through the durable inbox and exact validated tmux target. If it arrives after exit, the TL uses `resume_pr` to start a fresh invocation in the same owner worktree, branch, and PR, with pending guidance visible at startup.
7. Use notify_parent with status='success' or status='failure' when direct handoff is
   appropriate for the context, including completion outside the normal review loop or being
   truly stuck after multiple attempts.

## Key Rules
- Work only in your worktree. Never checkout another branch.
- Never call spawn_leaf; you are a leaf, not a TL.
- Git operations use shell commands. Use file_pr for PR creation.
- Do not create a new owner, branch, or stacked PR for review fixes.
";

pub const CODEX_WORKER_INSTRUCTIONS: &str = "\
# ExoMonad Worker Agent Protocol

You are a Codex worker in an ExoMonad agent tree. You run in a shared workspace pane and do not have your own branch.

## Your Job
Complete the narrow task assigned by your parent TL. Report completion through the provided MCP tools.

## MCP Tools Available
- chainlink_session_start: Start the Chainlink session before marking work active.
- chainlink_session_work: Mark the assigned Chainlink issue as the active work item.
- chainlink_issue_comment: Post progress on the assigned Chainlink issue.
- chainlink_session_end: End the Chainlink session with handoff notes.
- notify_parent: Send a direct message to your parent TL when needed. Never use send_message with recipient 'parent'; 'parent' is a reserved alias resolved only by notify_parent.
- send_tmux_message: Send messages to other agents through tmux when explicitly instructed.
- send_mailbox_message: Send messages through Claude Teams inbox when mailbox support is available.
- check_inbox: Drain durable inbox guidance at the start of the assignment and after each major step. Unread mail piggybacks on MCP tool results and is authoritative parent direction.
- memory_append: Append a durable session-memory record through the host ledger.
- memory_list: List durable session-memory records for the current run with optional semantic filters.
- task_list: List tasks assigned to this agent.
- task_get: Read an assigned task.
- task_update: Update an assigned task.
- chainlink_issue_show: Read the assigned Chainlink issue.

## Workflow
1. Read the prompt carefully and use the issue ID provided by the TL.
2. Call chainlink_session_start.
3. Call chainlink_session_work before doing the requested work.
4. Call chainlink_issue_comment for the required progress marker.
5. Call chainlink_session_end with concise handoff notes when done.
6. Call notify_parent with status='success' and include the issue ID.

## Key Rules
- Never spawn agents; workers are leaf executors.
- Never create Chainlink issues; only the parent TL creates issues.
- A `review-stuck` signal is a human-clarification handoff; never create a replacement issue or respawn work for it.
- Never initialize Chainlink agent identity; ExoMonad branch/session identity is authoritative.
- Never close Chainlink issues; your parent coordinator reviews the handoff and closes.
- Never create branches, commits, or PRs unless explicitly instructed.
";

fn append_reviewer_metadata(
    body: &str,
    reviewer_agent: &str,
    reviewer_birth_branch: &str,
) -> String {
    let mut lines: Vec<&str> = body
        .lines()
        .filter(|line| {
            let trimmed = line.trim_start();
            !trimmed.starts_with("Reviewer-Agent:")
                && !trimmed.starts_with("Reviewer-Birth-Branch:")
        })
        .collect();
    while lines.last().is_some_and(|line| line.trim().is_empty()) {
        lines.pop();
    }
    format!(
        "{}
Reviewer-Agent: {}
Reviewer-Birth-Branch: {}",
        lines.join(
            "
"
        ),
        reviewer_agent,
        reviewer_birth_branch
    )
}

pub const CODEX_REVIEWER_INSTRUCTIONS: &str = "\
# ExoMonad Reviewer Agent Protocol

You are a Codex reviewer agent in an ExoMonad agent tree. You review a sibling agent's PR from your own reviewer worktree.

## Your Job
Review the PR assigned in your task prompt. Approve correct changes or request specific fixes. Do not implement the fix yourself.

This reviewer process handles one exact PR/SHA assignment. Submit one authoritative verdict or comment, then exit; never wait for CI, merge-ready, or merge. While the invocation is alive, durable inbox guidance may be injected only into its validated exact tmux target. A stale target is rejected and never redirected to the root pane.

## MCP Tools Available
- approve_pr: Submit an approved Forgejo PR review.
- request_changes: Submit a request-changes Forgejo PR review.
- post_review_comment: Submit a comment-only Forgejo PR review.
- check_inbox: Drain durable inbox guidance at the start of the review and after each major step. Unread mail piggybacks on MCP tool results and is authoritative TL direction.
- list_agents: Inspect the assigned agent tree and routing state.

## Workflow
1. Read the task prompt for the PR number, PR branch, base branch, and author.
2. Run `git diff {base_branch}..HEAD` using the base branch from the prompt.
3. Review for correctness, edge cases, security issues, missing tests, and broken contracts.
4. If issues are found, call request_changes with specific, actionable feedback that references files and functions or lines.
5. If the code is correct, call approve_pr with a concise approving comment.
6. Exit after submitting. The ExoMonad watcher reads Forgejo reviews and routes the result to the dev and TL automatically. If a later review round is needed, the watcher starts a fresh SHA-scoped reviewer invocation.

## Key Rules
- Never modify code; reviewers only review.
- Never merge a PR; only the TL merges.
- Never spawn agents; reviewer is a leaf role.
- Never review your own PR. If the PR author is you, stop without submitting a verdict; stuck escalation handles reviewers that cannot proceed.
- Do not use `codex exec review`; it emits Codex-native review text and does not submit Forgejo reviews.
- This reviewer sandbox has no network access, so approve_pr/request_changes/post_review_comment (which run in the unsandboxed ExoMonad host process) are the only way to reach Forgejo — do not attempt a raw HTTP request from this session's own shell.
- Prefer 3-5 high-impact comments over exhaustive style feedback.
";

/// Render the reviewer's `Read first:` context section for the spawn task prompt.
///
/// Relative paths in `reviewer_context` are resolved against `project_dir` so they
/// remain readable from the reviewer's detached worktree (where cwd is the worktree,
/// not the project root, and the context files live outside the worktree's tracked
/// tree). Absolute paths pass through unchanged. Empty `reviewer_context` returns
/// "" — no "Read first:" header emitted in that case (production default).
pub(crate) fn render_reviewer_context_section(
    reviewer_context: &[String],
    project_dir: &std::path::Path,
) -> String {
    if reviewer_context.is_empty() {
        return String::new();
    }
    let lines = reviewer_context
        .iter()
        .map(|p| {
            let path = std::path::Path::new(p);
            let resolved = if path.is_absolute() {
                path.to_path_buf()
            } else {
                project_dir.join(path)
            };
            format!("- {}", resolved.display())
        })
        .collect::<Vec<_>>()
        .join("\n");
    format!("\n\nRead first:\n{lines}")
}

pub(crate) fn render_reviewer_acceptance_criteria(criteria: &[String]) -> String {
    if criteria.is_empty() {
        return String::new();
    }
    let bullets = criteria
        .iter()
        .map(|criterion| format!("- {}", criterion.trim()))
        .collect::<Vec<_>>()
        .join("\n");
    format!(
        "\n\n## Acceptance Criteria\nThese criteria come from TL run state for the exact reviewed head; the PR body is evidence only.\n{bullets}"
    )
}

impl<
        C: super::super::HasGitHubClient
            + super::super::HasForgejoClient
            + super::super::HasTeamRegistry
            + super::super::HasAgentResolver
            + super::super::HasProjectDir
            + super::super::HasGitWorktreeService
            + super::super::HasInboxStore
            + super::super::HasSessionMemory
            + crate::services::HasEventLog
            + 'static,
    > AgentControlService<C>
{
    /// Fetch the branch's remote evidence, then read whether the local branch
    /// exists.
    ///
    /// The order is the contract: the fetch materializes a remote-only branch
    /// locally, so existence read beforehand would report it absent and send a
    /// preserved branch down the create path.
    async fn read_leaf_branch_state(
        &self,
        effective_project_dir: &Path,
        branch: &BranchName,
    ) -> Result<LeafBranchState> {
        let remote = ensure_branch_fetched(effective_project_dir, branch).await;
        let exists = self.git_wt().branch_exists(branch)?;
        Ok(LeafBranchState { exists, remote })
    }

    /// Provision the deterministic leaf worktree for an absent path.
    ///
    /// Cleanup is armed before the first fallible creation, so any error after
    /// this point drops the guard and removes what this attempt created. The
    /// guard is returned only when this attempt created the worktree, so a later
    /// failure can never roll back a worktree it merely reused.
    async fn provision_leaf_worktree(
        &self,
        request: LeafProvisioning<'_, '_>,
    ) -> Result<Option<WorktreeRollback>> {
        let action = leaf_worktree_action(request.branch_exists, request.start_point);
        self.record_attach_decision(&request, action);
        let provisioned = self.run_leaf_provisioning(&request, action).await;
        if let Err(error) = provisioned.as_ref() {
            self.record_ownership_conflict(&request, error);
        }
        provisioned
    }

    async fn run_leaf_provisioning(
        &self,
        request: &LeafProvisioning<'_, '_>,
        action: LeafWorktreeAction,
    ) -> Result<Option<WorktreeRollback>> {
        let git_wt = self.git_wt();
        let mut rollback =
            WorktreeRollback::armed(git_wt.clone(), request.worktree_path.to_path_buf());
        git_wt.prune_worktrees()?;
        let created = match action {
            LeafWorktreeAction::Attach => {
                let fresh_remote = request.fresh_remote.clone();
                self.attach_leaf_branch(request, fresh_remote).await?
                    == LeafAttachability::Attachable
            }
            LeafWorktreeAction::CreateFromRevision => {
                let start_point = request
                    .start_point
                    .expect("CreateFromRevision requires a start point");
                self.recover_lost_creation_race(
                    request,
                    self.create_worktree_from_revision_checked(
                        request.worktree_path,
                        request.branch,
                        start_point,
                    )
                    .await,
                )
                .await?
            }
            LeafWorktreeAction::CreateFromBase => {
                self.recover_lost_creation_race(
                    request,
                    self.create_worktree_checked(
                        request.worktree_path,
                        request.branch,
                        request.base_branch,
                    )
                    .await,
                )
                .await?
            }
        };
        if created {
            self.record_attach_completed(request, action, true);
            return Ok(Some(rollback));
        }
        info!(
            worktree_path = %request.worktree_path.display(),
            branch = %request.branch,
            "Reusing the worktree that already holds the deterministic branch"
        );
        self.record_attach_completed(request, action, false);
        rollback.defuse();
        Ok(None)
    }

    /// Record the durable attach-versus-create decision for one provisioning.
    ///
    /// The decision is durable because the controller's dispatch classification
    /// and any operator reading a parked run both need to know which branch of
    /// the spawn state machine ran, not just its outcome.
    fn record_attach_decision(
        &self,
        request: &LeafProvisioning<'_, '_>,
        action: LeafWorktreeAction,
    ) {
        self.append_spawn_event(
            "agent.attach_decided",
            request.agent_name,
            &serde_json::json!({
                "branch": request.branch.as_str(),
                "worktree_path": request.worktree_path.display().to_string(),
                "action": action_name(action),
                "branch_exists": request.branch_exists,
                "start_point": request.start_point,
            }),
        );
    }

    /// Record whether provisioning created the worktree or reused an existing one.
    fn record_attach_completed(
        &self,
        request: &LeafProvisioning<'_, '_>,
        action: LeafWorktreeAction,
        created: bool,
    ) {
        self.append_spawn_event(
            "agent.attach_completed",
            request.agent_name,
            &serde_json::json!({
                "branch": request.branch.as_str(),
                "worktree_path": request.worktree_path.display().to_string(),
                "action": action_name(action),
                "created": created,
            }),
        );
    }

    /// Record one branch-ownership conflict, which is always terminal.
    ///
    /// Emitted at the boundary where the refusal leaves this attempt, so the
    /// event is recorded exactly once per refused spawn and carries no prose
    /// the controller would have to classify.
    fn record_ownership_conflict(&self, request: &LeafProvisioning<'_, '_>, error: &anyhow::Error) {
        if stable_error_code(error) != Some(BRANCH_OWNERSHIP_CONFLICT_CODE) {
            return;
        }
        self.append_spawn_event(
            "agent.branch_ownership_conflict",
            request.agent_name,
            &serde_json::json!({
                "branch": request.branch.as_str(),
                "worktree_path": request.worktree_path.display().to_string(),
                "machine_code": BRANCH_OWNERSHIP_CONFLICT_CODE,
            }),
        );
    }

    fn append_spawn_event(
        &self,
        event_type: &str,
        agent_name: &AgentName,
        payload: &serde_json::Value,
    ) {
        let Some(log) = self.ctx.event_log() else {
            return;
        };
        let _ = log.append(event_type, agent_name.as_str(), payload);
    }

    /// Attach the deterministic branch at the leaf path, or report that the
    /// leaf path already holds it. Ownership is proved before any attachment.
    async fn attach_leaf_branch(
        &self,
        request: &LeafProvisioning<'_, '_>,
        fresh_remote: crate::services::git_worktree::RemoteEvidence,
    ) -> Result<LeafAttachability> {
        let attachability = verify_attachable_branch(
            self.git_wt(),
            request.project_dir,
            request.worktree_path,
            request.branch,
            request.heads,
            fresh_remote,
        )
        .await?;
        if attachability == LeafAttachability::ReuseExistingWorktree {
            return Ok(attachability);
        }
        self.create_worktree_from_existing_branch_checked(request.worktree_path, request.branch)
            .await?;
        Ok(LeafAttachability::Attachable)
    }

    /// Recover a branch-creation race with at most one ownership-verified
    /// attach. Only the stable branch-exists code is recoverable; any other
    /// creation failure is returned unchanged.
    async fn recover_lost_creation_race(
        &self,
        request: &LeafProvisioning<'_, '_>,
        created: Result<()>,
    ) -> Result<bool> {
        let Err(error) = created else {
            return Ok(true);
        };
        if !creation_failure_allows_attach(&error) {
            return Err(error);
        }
        // The winning creator may also have pushed the branch, so the race is
        // re-evaluated against remote evidence fetched after the race.
        let fresh_remote = ensure_branch_fetched(request.project_dir, request.branch).await;
        info!(
            branch = %request.branch,
            "Lost a branch-creation race; re-verifying ownership before a single attach"
        );
        Ok(self.attach_leaf_branch(request, fresh_remote).await? == LeafAttachability::Attachable)
    }

    /// The leaf task, carrying the context of a PR that already owns the branch.
    ///
    /// One function serves the first spawn and the expected-agent resume, so a
    /// re-spawn after worktree loss and a `resume_pr` invocation restore the
    /// same context instead of the resumed leaf starting blind. The context is
    /// restored from the PR, never created here: a leaf whose branch has no
    /// qualifying open PR gets no PR context and files its own.
    ///
    /// A lookup that could not be answered is not the same fact as "no PR
    /// exists", so the two are never merged:
    ///
    /// | Outcome | Expected-agent resume | First spawn |
    /// |---------|-----------------------|-------------|
    /// | PR resolved | context appended | context appended |
    /// | no qualifying PR | no context | no context |
    /// | lookup failed | **refused**, `worktree.pr_context_unavailable` | `warn!`, then no context |
    ///
    /// A resume that cannot see its own open PR would hand the leaf a task that
    /// says nothing about the PR it owns, which is how a second PR gets filed.
    /// Failing the resume keeps the owner intact and retryable; a first spawn
    /// has no PR to be blind to, so it proceeds with the loss recorded. The
    /// caller holds the rollback guard, so a refused resume still removes
    /// whatever this spawn created.
    async fn leaf_task(
        &self,
        options: &SpawnLeafOptions,
        project_dir: &Path,
        branch: &BranchName,
        verified_head: Option<&str>,
    ) -> Result<String> {
        let mut task = options.task.clone();
        match self
            .resolve_existing_pull_request(project_dir, branch, verified_head)
            .await
        {
            PullRequestContext::Resolved(context) => task.push_str(&context),
            PullRequestContext::NoQualifyingPr => {}
            PullRequestContext::LookupFailed(error) => {
                if options.expected_agent_name.is_some() {
                    return Err(anyhow!(EffectError::custom(
                        "worktree.pr_context_unavailable",
                        format!(
                            "branch {branch} cannot be resumed: the pull request that already owns it \
                             could not be read, so the leaf would start blind. Restore access to the \
                             configured forge ({error}), then retry the resume."
                        )
                    )));
                }
                warn!(
                    branch = %branch,
                    %error,
                    "Could not read the pull request for this branch; starting without PR context"
                );
            }
        }
        if options.standalone_repo && !options.allowed_dirs.is_empty() {
            task.push_str("\n\nShared technical dependencies are available as read-only reference in `.exo/context/`. Do not modify files in this directory.");
        }
        Ok(task)
    }

    /// Resolve the pull request that already owns the deterministic branch.
    ///
    /// A branch name never identifies a PR. Only an open, unmerged PR whose head
    /// branch is the deterministic branch and whose head SHA equals the head this
    /// spawn verified describes the work being continued. A PR whose head has
    /// moved past the local branch, a closed or merged one, a PR the forge does
    /// not report a head for, and no PR at all are all the same fact — no
    /// qualifying PR — and never a guess. An unanswerable query is a different
    /// fact and is reported as `LookupFailed` with the underlying reason: an
    /// unresolvable repository, a forge error, and an unconfigured forge client
    /// are all failures to ask, not answers that nothing qualifies.
    async fn resolve_existing_pull_request(
        &self,
        project_dir: &Path,
        branch: &BranchName,
        verified_head: Option<&str>,
    ) -> PullRequestContext {
        let Some(verified_head) = verified_head.map(str::trim).filter(|sha| !sha.is_empty()) else {
            return PullRequestContext::NoQualifyingPr;
        };
        // A missing forge client is a misconfiguration, not a fact about pull
        // requests: the query was never made, so it is a failed lookup. A resume
        // refuses it, and a first spawn records it. An unverified head, by
        // contrast, really does mean there is no PR to match — a standalone repo
        // owns no host pull request.
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return PullRequestContext::LookupFailed(
                "no forge client is configured for this project".to_string(),
            );
        };
        let repo_info = match crate::services::repo::get_repo_info(project_dir).await {
            Ok(repo_info) => repo_info,
            Err(error) => {
                return PullRequestContext::LookupFailed(format!(
                    "could not resolve the repository for {}: {error}",
                    project_dir.display()
                ))
            }
        };
        let pr = match forgejo
            .find_open_pull_request(&repo_info.owner, &repo_info.repo, branch)
            .await
        {
            Ok(Some(pr)) => pr,
            Ok(None) => return PullRequestContext::NoQualifyingPr,
            Err(error) => {
                return PullRequestContext::LookupFailed(format!(
                    "the open pull request query for {}/{} failed: {error}",
                    repo_info.owner.as_str(),
                    repo_info.repo.as_str()
                ))
            }
        };
        if !pr_carries_verified_head(&pr, branch, verified_head) {
            return PullRequestContext::NoQualifyingPr;
        }
        let pr_number = pr.number.as_u64();
        // Review feedback is best effort: the PR identity is the context, so a
        // failed listing must not cost the leaf its PR. Every such failure is
        // recorded with the PR it belongs to, never dropped silently.
        let reviews = match forgejo
            .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, pr.number)
            .await
        {
            Ok(reviews) => reviews,
            Err(error) => {
                warn!(
                    pr_number,
                    %error,
                    "Could not list pull request reviews; restoring the pull request without its feedback"
                );
                Vec::new()
            }
        };
        let mut inline = Vec::new();
        for review in &reviews {
            let Some(review_id) = review.id else {
                continue;
            };
            match forgejo
                .list_pull_request_review_comments(
                    &repo_info.owner,
                    &repo_info.repo,
                    pr.number,
                    review_id,
                )
                .await
            {
                Ok(comments) => inline.extend(comments),
                Err(error) => warn!(
                    pr_number,
                    review_id,
                    %error,
                    "Could not list inline review comments; restoring the pull request without them"
                ),
            }
        }
        info!(
            pr_number,
            branch = %branch,
            head_sha = %verified_head,
            "Restoring the existing open pull request into the leaf task"
        );
        PullRequestContext::Resolved(pr_resume_context(&pr, &reviews, &inline))
    }

    /// Spawn an agent for a GitHub issue.
    ///
    /// This is the high-level semantic operation that:
    /// 1. Fetches issue from GitHub
    /// 2. Creates agent directory (.exo/agents/{agent_id}/)
    /// 3. Writes .mcp.json pointing to the Unix socket server
    /// 4. Opens tmux window with agent command (cwd = project_dir)
    #[tracing::instrument(skip(self, options), fields(issue_id = %issue_number.as_u64()))]
    pub async fn spawn_agent(
        &self,
        issue_number: IssueNumber,
        options: &SpawnOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let issue_id_log = issue_number.as_u64().to_string();
        info!(issue_id = %issue_id_log, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_agent");

        let result = timeout(SPAWN_TIMEOUT, async {
            // Validate we're in tmux
            self.resolve_tmux_session()?;

            // Resolve effective project dir.
            let effective_project_dir = self.effective_project_dir(options.subrepo.as_deref())?;

            // Get hosted issue client
            let github_client = self
                .github()
                .ok_or_else(|| anyhow!("hosted issue service not available"))?;
            let github = GitHubService::new(github_client.clone());

            // Fetch issue from hosted service
            let issue_id = issue_number.as_u64().to_string();
            info!(issue_id, "Fetching issue from hosted service");
            let repo = Repo {
                owner: options.owner.clone(),
                name: options.repo.clone(),
            };
            let issue = github.get_issue(&repo, issue_number).await?;

            // Generate slug and agent identity
            let slug = normalize_agent_slug(&issue.title);
            let identity =
                AgentIdentity::new(format!("gh-{}-{}", issue_id, slug), options.agent_type);
            let agent_name = identity.internal_name();

            // Determine base branch (use birth_branch for root detection)
            let default_base = self.birth_branch.as_parent_branch().to_string();
            let base = options
                .base_branch
                .as_ref()
                .map(|b| b.as_str().to_string())
                .unwrap_or(default_base);
            let agent_suffix = options.agent_type.suffix();
            let branch_name_raw = if self.birth_branch.depth() == 0 {
                format!("gh-{}/{}-{}", issue_id, slug, agent_suffix)
            } else {
                format!("{}/{}-{}", base, slug, agent_suffix)
            };
            let branch_name = BranchName::try_from_str(&branch_name_raw)
                .context("generated spawn branch name was empty")?;

            // Create worktree
            let worktree_path = self.worktree_base.join(agent_name.as_str());
            let base_branch = BranchName::try_from_str(base.as_str())
                .expect("validated string input is non-empty");

            // Exclusive lifecycle region: creation and a concurrent residue
            // cleanup pass must never interleave.
            let _lifecycle = self.acquire_worktree_lifecycle("create a worktree").await?;
            self.create_worktree_checked(&worktree_path, &branch_name, &base_branch)
                .await?;
            drop(_lifecycle);

            let agent_config_dir = self
                .project_dir()
                .join(".exo/agents")
                .join(agent_name.as_str());

            // Write .mcp.json for the agent
            let role = match options.agent_type {
                AgentType::Claude => crate::domain::Role::tl(),
                AgentType::Shoal => crate::domain::Role::shoal(),
                AgentType::OpenCode | AgentType::Codex => crate::domain::Role::dev(),
                AgentType::Process => unreachable!("Process agents are not spawned via effects"),
            };
            let model = self.effective_model_for(options.agent_type, role.as_str(), None);
            let effort = self.effective_effort_for(role.as_str(), None);
            self.write_agent_mcp_config(
                &effective_project_dir,
                &worktree_path,
                options.agent_type,
                &role,
            )
            .await?;

            // Build initial prompt
            let issue_url = format!(
                "https://github.com/{}/{}/issues/{}",
                options.owner, options.repo, issue_id
            );
            let original_prompt = Self::build_initial_prompt(
                &issue_id,
                &issue.title,
                &issue.body,
                &issue.labels,
                &issue_url,
            );
            let continuation_prefix = crate::services::continuation::composer::child_spawn_prefix(
                self.ctx.as_ref(),
                self,
                caller_bb,
                &agent_name,
                i64::try_from(issue_number.as_u64())
                    .context("issue number exceeds continuation ledger range")?,
            )
            .await;
            let initial_prompt = crate::services::continuation::composer::prefix_task(
                continuation_prefix.as_deref(),
                &original_prompt,
            );

            tracing::info!(
                issue_id,
                prompt_length = initial_prompt.len(),
                prefix_length = continuation_prefix.as_ref().map_or(0, String::len),
                "Built initial prompt for agent"
            );

            // tmux display name (emoji + short format)
            let display_name = options.agent_type.display_name(&issue_id, &slug);

            let parent_bb = self.effective_birth_branch(Some(caller_bb));
            let session_branch = BranchName::try_from_str(parent_bb.as_str())
                .expect("validated string input is non-empty");
            let env_vars = self.common_spawn_env(&agent_name, &session_branch, &role);

            // Open tmux window with cwd = worktree_path
            let window_id = self
                .new_tmux_window(
                    &display_name,
                    &worktree_path,
                    options.agent_type,
                    Some(&initial_prompt),
                    env_vars,
                )
                .await?;

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id.clone());
            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type: options.agent_type,
                birth_branch: BirthBranch::try_from_str(branch_name.as_str())
                    .expect("validated string input is non-empty"),
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                model: model.clone(),
                effort: effort.clone(),
                ledger_owned: false,
                slice_id: None,
            };
            self.finalize_spawn_with_mode(
                &agent_name,
                routing,
                Some(identity_record),
                InvocationMode::OneShot,
            )
            .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: agent_config_dir,
                worktree_path: worktree_path.clone(),
                branch_name: branch_name.to_string(),
                agent_name,
                issue_title: issue.title,
                agent_type: options.agent_type,
                pane_id: None,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_agent timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(issue_id = %issue_id_log, error = %msg, "spawn_agent timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(issue_id = %issue_id_log, "spawn_agent completed successfully");
        Ok(result)
    }

    /// Spawn multiple agents.
    #[tracing::instrument(skip(self, options))]
    pub async fn spawn_agents(
        &self,
        issue_ids: &[String],
        options: &SpawnOptions,
        caller_bb: &BirthBranch,
    ) -> BatchSpawnResult {
        let mut result = BatchSpawnResult {
            spawned: Vec::new(),
            failed: Vec::new(),
        };

        for issue_id_str in issue_ids {
            // Parse issue ID
            match IssueNumber::try_from(issue_id_str.clone()) {
                Ok(issue_number) => {
                    match self.spawn_agent(issue_number, options, caller_bb).await {
                        Ok(spawn_result) => result.spawned.push(spawn_result),
                        Err(e) => {
                            warn!(issue_id = issue_id_str, error = %e, "Failed to spawn agent");
                            result.failed.push((issue_id_str.clone(), e.to_string()));
                        }
                    }
                }
                Err(e) => {
                    warn!(issue_id = issue_id_str, error = %e, "Invalid issue number");
                    result.failed.push((issue_id_str.clone(), e.to_string()));
                }
            }
        }

        result
    }

    /// Generate opencode.json content for an OpenCode agent.
    ///
    /// Constructs the JSON configuration including MCP server connection, role instructions,
    /// and plugin registration pointing at `.exo/opencode-plugin`. The plugin package files
    /// must be written separately via `write_opencode_plugin_files`.
    pub fn generate_opencode_tl_settings(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
    ) -> serde_json::Value {
        Self::generate_opencode_tl_settings_with_effort(agent_name, role, extra_mcp_servers, None)
    }

    pub fn generate_opencode_tl_settings_with_effort(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(agent_name, role, extra_mcp_servers, effort, None, None)
    }

    pub fn generate_opencode_tl_settings_with_role_context(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
        role_context: &str,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(
            agent_name,
            role,
            extra_mcp_servers,
            effort,
            None,
            Some(role_context),
        )
    }

    /// Generate root OpenCode settings that load the canonical root protocol.
    pub fn generate_opencode_root_settings_with_context(
        agent_name: &str,
        context_path: &Path,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
    ) -> serde_json::Value {
        Self::generate_opencode_settings(
            agent_name,
            "root",
            extra_mcp_servers,
            effort,
            Some(context_path),
            None,
        )
    }

    fn generate_opencode_settings(
        agent_name: &str,
        role: &str,
        extra_mcp_servers: &HashMap<String, serde_json::Value>,
        effort: Option<&str>,
        context_path: Option<&Path>,
        role_context: Option<&str>,
    ) -> serde_json::Value {
        let mut mcp_servers = serde_json::Map::new();
        mcp_servers.insert(
            "exomonad".to_string(),
            serde_json::json!({
                "type": "local",
                "command": ["exomonad", "mcp-stdio", "--role", role, "--name", agent_name]
            }),
        );
        for (k, v) in extra_mcp_servers {
            mcp_servers.insert(k.clone(), v.clone());
        }

        // instructions must be an array per OpenCode's schema. Root settings
        // contain a path so the harness reads the canonical protocol itself.
        let mut instructions = match context_path {
            Some(path) => vec![path.to_string_lossy().into_owned()],
            None => match role {
                "root" | "tl" => vec![ROOT_CONTEXT_RELATIVE_PATH.to_string()],
                "worker" => vec![OPENCODE_WORKER_INSTRUCTIONS.to_string()],
                _ => vec![OPENCODE_DEV_INSTRUCTIONS.to_string()],
            },
        };
        if let Some(role_context) = role_context {
            instructions.push(role_context.to_string());
        }
        let mut settings = serde_json::json!({
            "mcp": mcp_servers,
            "instructions": instructions,
            "plugin": ["./.exo/opencode-plugin"],
        });
        if let Some(effort) = effort.filter(|value| !value.is_empty()) {
            settings["agent"] = serde_json::json!({
                format!("exomonad-{role}"): {"reasoningEffort": effort}
            });
        }
        settings
    }

    /// Write the exomonad OpenCode plugin package to `<dir>/.exo/opencode-plugin/`.
    ///
    /// Creates `index.ts` (the TypeScript bridge) and `package.json`. The plugin
    /// is referenced by `opencode.json` via `"plugin": ["./.exo/opencode-plugin"]`.
    pub async fn write_opencode_plugin_files(dir: &Path) -> Result<()> {
        use crate::opencode_plugin::{OPENCODE_PLUGIN_PKG_JSON, OPENCODE_PLUGIN_TS};
        let plugin_dir = dir.join(".exo/opencode-plugin");
        fs::create_dir_all(&plugin_dir).await?;
        fs::write(plugin_dir.join("index.ts"), OPENCODE_PLUGIN_TS).await?;
        fs::write(plugin_dir.join("package.json"), OPENCODE_PLUGIN_PKG_JSON).await?;
        info!(path = %plugin_dir.display(), "Wrote OpenCode plugin files");
        Ok(())
    }

    pub async fn write_opencode_git_stub(
        agent_config_dir: &Path,
        project_dir: &Path,
    ) -> Result<()> {
        let git_content = format!("gitdir: {}\n", project_dir.join(".git").display());
        fs::write(agent_config_dir.join(".git"), git_content).await?;
        Ok(())
    }

    async fn active_worker_for_parent_tab(
        &self,
        agents_dir: &Path,
        parent_tab: &str,
        current_agent_name: &AgentName,
    ) -> Result<Option<ActiveWorker>> {
        let Ok(mut entries) = fs::read_dir(agents_dir).await else {
            return Ok(None);
        };

        while let Some(entry) = entries.next_entry().await? {
            let file_type = entry.file_type().await?;
            if !file_type.is_dir() {
                continue;
            }

            let name = entry.file_name().to_string_lossy().to_string();
            if name == current_agent_name.as_str() {
                continue;
            }

            let agent_dir = entry.path();
            let Ok(routing) = RoutingInfo::read_from_dir(&agent_dir).await else {
                continue;
            };
            if routing.parent_tab.as_deref() != Some(parent_tab) {
                continue;
            }
            let worker_alive = if let Some(pane_id) = routing.pane_id.as_ref() {
                self.tmux()?.pane_exists(pane_id).await.unwrap_or(false)
            } else if let Some(window_id) = routing.window_id.as_ref() {
                self.tmux()?.window_exists(window_id).await.unwrap_or(false)
            } else {
                false
            };
            if !worker_alive {
                warn!(
                    worker = %name,
                    path = %agent_dir.display(),
                    "Removing stale active worker registration with no live tmux target"
                );
                if let Err(error) = fs::remove_dir_all(&agent_dir).await {
                    warn!(worker = %name, error = %error, "Failed to remove stale active worker registration");
                }
                continue;
            }

            let age = entry
                .metadata()
                .await
                .ok()
                .and_then(|metadata| metadata.modified().ok())
                .and_then(|modified| std::time::SystemTime::now().duration_since(modified).ok())
                .map(format_worker_age)
                .unwrap_or_else(|| "unknown time".to_string());
            return Ok(Some(ActiveWorker { name, age }));
        }

        Ok(None)
    }

    /// Spawn a worker agent in the current worktree (no branch/worktree).
    #[instrument(skip_all, fields(name = %options.name, agent_type = %options.agent_type.suffix()))]
    pub async fn spawn_worker(
        &self,
        options: &SpawnWorkerOptions,
        ctx: &crate::effects::EffectContext,
    ) -> Result<SpawnResult> {
        self.spawn_worker_with_intent(options, ctx, None).await
    }

    pub async fn spawn_worker_with_intent(
        &self,
        options: &SpawnWorkerOptions,
        ctx: &crate::effects::EffectContext,
        intent_id: Option<&str>,
    ) -> Result<SpawnResult> {
        let agent_type = options.agent_type;
        info!(name = %options.name, agent_type = agent_type.suffix(), timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_worker");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // Workers run in the caller's worktree and inherit any dirty state.
            let caller_tab = resolve_own_tab_name(ctx);
            let caller_worktree = ctx.working_dir.clone();
            let absolute_worktree = self.project_dir().join(&caller_worktree);
            ensure_clean_spawn_worktree(&absolute_worktree).await?;

            // Sanitize name and construct typed identity
            let identity = AgentIdentity::new(normalize_agent_slug(options.name.as_str()), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();
            let agents_dir = self.project_dir().join(".exo").join("agents");

            if let Some(active_worker) = self
                .active_worker_for_parent_tab(&agents_dir, &caller_tab, &agent_name)
                .await?
            {
                return Err(active_worker_error(&active_worker));
            }

            // Idempotency: check if agent config dir already exists (workers are panes, not tabs)
            let agent_config_dir = agents_dir.join(agent_name.as_str());
            let routing_path = agent_config_dir.join("routing.json");
            if routing_path.exists() {
                // Check tmux pane liveness — routing.json can outlive the pane
                let existing_pane_id = match RoutingInfo::read_from_dir(&agent_config_dir).await {
                    Ok(routing) => match routing.pane_id {
                        Some(ref pane_id) if self.tmux()?.pane_exists(pane_id).await.unwrap_or(false) => {
                            Some(pane_id.as_str().to_string())
                        }
                        _ => None,
                    },
                    Err(_) => None,
                };
                if let Some(pane_id) = existing_pane_id {
                    info!(name = %options.name, "Worker pane still alive, returning existing");
                    return Ok(SpawnResult {
                        agent_dir: PathBuf::new(),
                        worktree_path: PathBuf::new(),
                        branch_name: String::new(),
                        agent_name,
                        issue_title: options.name.to_string(),
                        agent_type,
                        pane_id: Some(pane_id),
                    });
                }
                // Stale: pane is dead but config dir remains. Clean up and respawn.
                info!(name = %options.name, path = %agent_config_dir.display(), "Stale worker detected (pane dead), cleaning up and respawning");
                if let Err(e) = fs::remove_dir_all(&agent_config_dir).await {
                    warn!(name = %options.name, error = %e, "Failed to clean up stale worker config dir");
                }
            }

            persist_dispatch_intent(self.project_dir(), &agent_name, intent_id).await?;

            let role = crate::domain::Role::worker();
            let model = self.effective_model_for(agent_type, role.as_str(), options.model.as_deref());
            let effort = self.effective_effort_for(role.as_str(), None);
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let session_branch = BranchName::try_from_str(parent_bb.as_str()).expect("validated string input is non-empty");
            let mut env_vars = self.common_spawn_env(&agent_name, &session_branch, &role);
              env_vars.insert(
                  "GIT_AUTHOR_NAME".to_string(),
                  format!("exomonad-{}", agent_name.as_str()),
              );
              env_vars.insert(
                  "GIT_AUTHOR_EMAIL".to_string(),
                  format!("{}@exomonad.local", agent_name.as_str()),
              );

            fs::create_dir_all(&agent_config_dir).await?;

            // Legacy .birth_branch file for serve.rs fallback resolution.
            // identity.json (written via finalize_spawn) is the canonical source,
            // but keep this for backward compatibility with older server instances.
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            fs::write(agent_config_dir.join(".birth_branch"), parent_bb.as_str()).await?;

            match agent_type {
                AgentType::OpenCode => {
                    // Write worker-specific opencode.json so the worker gets
                    // its own role/name (not the caller's root config, which
                    // lacks notify_parent and other worker tools).
                    let role_context = self.runtime_role_context(&role)?;
                    let worker_config = Self::generate_opencode_tl_settings_with_role_context(
                        agent_name.as_str(),
                        "worker",
                        &self.extra_mcp_servers,
                        None,
                        &role_context,
                    );
                    let opencode_json_path = agent_config_dir.join("opencode.json");
                    fs::write(&opencode_json_path, serde_json::to_string_pretty(&worker_config)?).await?;
                    Self::write_opencode_plugin_files(&agent_config_dir).await?;
                    Self::write_opencode_git_stub(&agent_config_dir, self.project_dir()).await?;
                    info!(path = %opencode_json_path.display(), agent_name = %agent_name, "Wrote worker opencode.json and plugin to agent config dir");
                }
                AgentType::Codex => {
                    self.write_codex_config_files(
                        &agent_config_dir,
                        &role,
                        &agent_name,
                        model.as_deref(),
                        &self.extra_mcp_servers,
                    )
                    .await?;
                    info!(path = %agent_config_dir.join(".codex/config.toml").display(), agent_name = %agent_name, "Wrote worker Codex config to agent config dir");
                }
                _ => {}
            }

            // Config-discovered runtimes run in their agent config dir so they
            // receive the worker role/name instead of inheriting the caller's
            // project config.
            let worker_cwd = match agent_type {
                AgentType::OpenCode | AgentType::Codex => agent_config_dir.clone(),
                _ => absolute_worktree.clone(),
            };

            // Workers are panes in the parent's tab — pane_id is the stable identifier.
            // Prompt goes through a temp file to avoid shell quoting issues.
            let pane_id = self.new_tmux_pane(
                &display_name,
                &worker_cwd,
                agent_type,
                Some(&options.prompt),
                env_vars,
                Some(&caller_tab),
                Some(&options.claude_flags),
                model.as_deref(),
            )
              .await?;
              let pane_id_string = pane_id.as_str().to_string();

              // Store pane_id for message delivery and cleanup
              let routing = RoutingInfo::pane(pane_id, &caller_tab);
            let parent_bb = self.effective_birth_branch(Some(&ctx.birth_branch));
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: parent_bb.clone(),
                parent_branch: parent_bb,
                working_dir: ctx.working_dir.clone(),
                display_name: display_name.clone(),
                topology: Topology::SharedDir,
                      model: model.clone(),
                      effort: effort.clone(),
                  ledger_owned: false,
                slice_id: Some(options.name.to_string()),
            };
            self.finalize_spawn(&agent_name, routing, Some(identity_record))
                .await?;

            self.emit_agent_started(&agent_name)?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: PathBuf::new(),
                worktree_path: PathBuf::new(),
                branch_name: String::new(),
                agent_name,
                issue_title: options.name.to_string(),
                agent_type,
                pane_id: Some(pane_id_string),
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_worker timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(name = %options.name, error = %msg, "spawn_worker timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(name = %options.name, "spawn_worker completed successfully");
        Ok(result)
    }

    /// Spawn a subtree agent (Claude-only) in a new git worktree.
    #[instrument(skip_all, fields(slug = %options.branch_name, agent_type = "claude"))]
    pub async fn spawn_subtree(
        &self,
        options: &SpawnSubtreeOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        info!(branch_name = %options.branch_name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_subtree");

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            let effective_birth = self.effective_birth_branch(Some(caller_bb));

            // Depth check using typed birth-branch.
            let depth = effective_birth.depth();

            if depth >= 3 {
                return Err(anyhow!("Subtree depth limit reached (max 3). Current birth-branch: {}, depth: {}", effective_birth, depth));
            }

            let effective_project_dir = self.project_dir();

            // Sanitize branch name and construct typed identity
            let agent_type = options.agent_type;
            let identity = AgentIdentity::new(normalize_agent_slug(&options.branch_name), agent_type);
            let agent_name = identity.internal_name();
            let display_name = identity.display_name();
            let child_birth = effective_birth.child(agent_name.as_str());

            // Idempotency check: if tmux window is alive, return existing info
            let tab_alive = self.is_tmux_window_alive(&display_name).await;
            if tab_alive {
                info!(slug = %identity.slug(), "Subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: self
                        .project_dir()
                        .join(".exo/agents")
                        .join(agent_name.as_str()),
                    worktree_path: self.worktree_base.join(agent_name.as_str()),
                    branch_name: child_birth.to_string(),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                    pane_id: None,
                });
            }

            // Parent branch derived from typed birth-branch.
            let current_branch = BranchName::try_from_str(effective_birth.as_parent_branch())
                .context("effective birth branch was empty")?;

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            // Branch: {current_branch}.{agent_name} (suffixed for unified namespace)
            let branch_name = child_birth.to_string();

            // Path resolution: working_dir overrides the default worktree location.
            // standalone_repo: git init (fresh .git boundary) instead of git worktree add.
            // These are orthogonal: working_dir controls WHERE, standalone_repo controls HOW.
            let (worktree_path, is_custom_dir) = if let Some(ref custom_dir) = options.working_dir {
                (custom_dir.clone(), true)
            } else {
                (self.worktree_base.join(agent_name.as_str()), false)
            };

            if options.standalone_repo {
                let _lifecycle = self
                    .acquire_worktree_lifecycle("initialize a worktree")
                    .await?;
                self.init_standalone_repo(&worktree_path).await?;
                if !options.allowed_dirs.is_empty() {
                    self.copy_allowed_dirs(&worktree_path, &options.allowed_dirs).await?;
                }
                drop(_lifecycle);
            } else if !is_custom_dir {
                // Exclusive lifecycle region: creation and a concurrent residue
                // cleanup pass must never interleave.
                let _lifecycle = self
                    .acquire_worktree_lifecycle("create a worktree")
                    .await?;
                let branch = BranchName::try_from_str(branch_name.as_str()).expect("validated string input is non-empty");
                self.create_worktree_checked(&worktree_path, &branch, &current_branch).await?;
                drop(_lifecycle);
            }

            self.create_socket_symlink(&worktree_path).await;

            let default_tl = crate::domain::Role::tl();
            let role = options.role.as_ref().unwrap_or(&default_tl);
            let model = self.effective_model_for(agent_type, role.as_str(), options.model.as_deref());
            let effort = self.effective_effort_for(role.as_str(), options.effort.as_deref());

            // Validate role context before spawning. Claude consumes a copied
            // file; OpenCode and Codex receive the same content inline in their
            // runtime instruction settings below.
            let context_src = self.resolve_role_context(role).ok_or_else(|| {
                anyhow!(
                    "Missing role context for {} at .exo/roles/{}/context/{}.md",
                    role,
                    self.wasm_name,
                    role
                )
            })?;
            let spawn_type = self.spawn_agent_type.suffix();
            match agent_type {
                AgentType::Claude => {
                    let rules_dir = worktree_path.join(".claude/rules");
                    fs::create_dir_all(&rules_dir).await?;
                    let dest = rules_dir.join("exomonad_role.md");
                    let _ = fs::remove_file(&dest).await;
                    Self::copy_role_context_with_interpolation(&context_src, &dest, spawn_type)
                        .await
                        .with_context(|| {
                            format!("Failed to copy Claude role context to {}", dest.display())
                        })?;
                    info!(role = %role, src = %context_src.display(), dest = %dest.display(), "Copied role context into worktree");
                }
                AgentType::OpenCode | AgentType::Codex | AgentType::Shoal | AgentType::Process => {}
            }

            let session_branch = BranchName::try_from_str(branch_name.as_str()).expect("validated string input is non-empty");
            let mut env_vars = self.common_spawn_env(&agent_name, &session_branch, role);

            // Write agent MCP config
            self.write_agent_mcp_config(effective_project_dir, &worktree_path, agent_type, role)
                .await?;

            match agent_type {
                AgentType::Claude => {
                    // Enable Claude Code Agent Teams for native inter-agent messaging
                    env_vars.insert(
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS".to_string(),
                        "1".to_string(),
                    );

                    // Write .claude/settings.local.json with hooks (SessionStart registers UUID for --fork-session)
                    let binary_path = crate::util::find_exomonad_binary();
                    crate::hooks::HookConfig::write_persistent(&worktree_path, &binary_path, options.permissions.as_ref(), Some(self.project_dir()))
                        .map_err(|e| anyhow!("Failed to write hook config in worktree: {}", e))?;
                    info!(worktree = %worktree_path.display(), "Wrote hook configuration for spawned Claude agent");

                    // Symlink Claude project dir so child can discover parent's sessions for --fork-session.
                    // Claude Code encodes paths via [^a-zA-Z0-9] → '-' (lossy regex replacement).
                    // Without this symlink, --resume --fork-session fails with "no conversation ID found".
                    {
                        let claude_projects_dir = dirs::home_dir()
                            .unwrap_or_default()
                            .join(".claude")
                            .join("projects");
                        let encode_path = |p: &Path| -> String {
                            p.to_string_lossy()
                                .chars()
                                .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
                                .collect()
                        };
                        let canonical_project_dir = self.project_dir().canonicalize().unwrap_or_else(|_| self.project_dir().to_path_buf());
                        let parent_encoded = encode_path(&canonical_project_dir);
                        let worktree_encoded = encode_path(&worktree_path);
                        let parent_project = claude_projects_dir.join(&parent_encoded);
                        let child_project = claude_projects_dir.join(&worktree_encoded);
                        if parent_project.exists() && !child_project.exists() {
                            match std::os::unix::fs::symlink(&parent_project, &child_project) {
                                Ok(()) => info!(
                                    parent = %parent_encoded,
                                    child = %worktree_encoded,
                                    "Symlinked Claude project dir for session inheritance"
                                ),
                                Err(e) => warn!(
                                    parent = %parent_encoded,
                                    child = %worktree_encoded,
                                    error = %e,
                                    "Failed to symlink Claude project dir (fork-session may not work)"
                                ),
                            }
                        }
                    }
                }
                AgentType::OpenCode => {
                    let role_context = self.runtime_role_context(role)?;
                    let opencode_config = Self::generate_opencode_tl_settings_with_role_context(
                        agent_name.as_str(),
                        role.as_str(),
                        &self.extra_mcp_servers,
                        None,
                        &role_context,
                    );
                    fs::write(
                        worktree_path.join("opencode.json"),
                        serde_json::to_string_pretty(&opencode_config)?,
                    ).await?;
                    Self::write_opencode_plugin_files(&worktree_path).await?;
                    info!(worktree = %worktree_path.display(), "Wrote opencode.json and plugin for OpenCode TL agent");
                }
                _ => {}
            }

            // Build task prompt with worktree context warning
            let mut task_with_context = format!(
                "You are now in worktree {} on branch {}. All file paths from your inherited context are STALE — use relative paths only and re-read files before editing.\n\n{}",
                worktree_path.display(), branch_name, options.task
            );

            if options.standalone_repo && !options.allowed_dirs.is_empty() {
                task_with_context.push_str("\n\nShared technical dependencies are available as read-only reference in `.exo/context/`. Do not modify files in this directory.");
            }

            // Inject workspace-level agent.md if present
            let agent_md_path = self.project_dir().join("agent.md");
            if agent_md_path.exists() {
                if let Ok(content) = tokio::fs::read_to_string(&agent_md_path).await {
                    task_with_context.push_str("\n\n---\n\n# Workspace Context (from agent.md)\n\n");
                    task_with_context.push_str(&content);
                }
            }

            // Open tmux window with cwd = worktree_path
            let routing = match agent_type {
                AgentType::OpenCode => {
                    // OpenCode workers run in a tmux window like Claude workers.
                    // `build_agent_command` generates `opencode run "$(cat '<prompt_file>')"`.
                    // MCP is configured via opencode.json in the worktree.
                    // Messages are delivered via tmux STDIN injection (same as all other agents).
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        None, // no fork_session for OpenCode
                        None, // no claude_flags
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                AgentType::Codex => {
                    let fork_id = options.parent_session_id.as_ref().map(|id| id.as_str());
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        fork_id,
                        None,
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                AgentType::Claude => {
                    // Determine fork mode from parent_session_id
                    let fork_id = options.parent_session_id.as_ref().map(|id| id.as_str());
                    let window_id = self.new_tmux_window_inner(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                        fork_id,
                        Some(&options.claude_flags),
                        Some(role.as_str()),
                        model.as_deref(),
                        effort.as_deref(),
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
                _ => {
                    let window_id = self.new_tmux_window(
                        &display_name,
                        &worktree_path,
                        agent_type,
                        Some(&task_with_context),
                        env_vars,
                    )
                    .await
                    .map_err(|e| {
                        warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                        e
                    })?;
                    RoutingInfo::window(window_id)
                }
            };
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                      model: model.clone(),
                      effort: effort.clone(),
                  ledger_owned: false,
                slice_id: Some(options.branch_name.clone()),
            };
            let trigger = if options
                .role
                .as_ref()
                .is_some_and(|role| role.as_str() == "reviewer")
            {
                InvocationTrigger::Review
            } else {
                InvocationTrigger::Spawn
            };
            self.finalize_spawn_with_invocation(
                &agent_name,
                routing,
                Some(identity_record),
                InvocationMetadata {
                    runtime: agent_type,
                    trigger,
                    pr_number: options.invocation_pr_number,
                    head_sha: options.invocation_head_sha.clone(),
                    model: model.clone(),
                    effort: effort.clone(),
                    recovery_lineage: None,
                    identity: None,
                    mode: InvocationMode::from_role(options.role.as_ref().map(|role| role.as_str())),
                },
            )
                .await?;

            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: self
                    .project_dir()
                    .join(".exo/agents")
                    .join(agent_name.as_str()),
                worktree_path: worktree_path.clone(),
                branch_name: branch_name.clone(),
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
                pane_id: None,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_subtree timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(branch_name = %options.branch_name, error = %msg, "spawn_subtree timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(branch_name = %options.branch_name, "spawn_subtree completed successfully");
        Ok(result)
    }

    /// Spawn a leaf agent in a new git worktree.
    #[instrument(skip_all, fields(slug = %options.branch_name))]
    pub async fn spawn_leaf_subtree(
        &self,
        options: &SpawnLeafOptions,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        self.spawn_leaf_subtree_with_intent(options, caller_bb, None)
            .await
    }

    pub async fn spawn_leaf_subtree_with_intent(
        &self,
        options: &SpawnLeafOptions,
        caller_bb: &BirthBranch,
        intent_id: Option<&str>,
    ) -> Result<SpawnResult> {
        info!(branch_name = %options.branch_name, timeout_sec = SPAWN_TIMEOUT.as_secs(), "Starting spawn_leaf_subtree");
        let _resume_spawn_guard = if options.expected_agent_name.is_some() {
            Some(resume_spawn_lock().lock().await)
        } else {
            None
        };

        let result = timeout(SPAWN_TIMEOUT, async {
            self.resolve_tmux_session()?;

            // No depth check for leaf nodes.

            let effective_birth = self.effective_birth_branch(Some(caller_bb));
            let effective_project_dir = self.project_dir();
            ensure_clean_spawn_worktree(effective_project_dir).await?;

            // Replacement leaves may start from an old PR head while their branch
            // hierarchy still targets the original PR base branch.
            let parent_branch = options
                .base_branch
                .as_deref()
                .unwrap_or(effective_birth.as_parent_branch());
            let current_branch = BranchName::try_from_str(parent_branch)
                .context("effective birth branch was empty")?;

            // Sanitize branch name and construct typed identity
            let slug = normalize_agent_slug(&options.branch_name);
            let slug_key = Slug::try_from_str(&slug).context("generated agent slug was empty")?;
            let mut agent_type = options.agent_type;
            let mut identity = AgentIdentity::new(slug.clone(), agent_type);
            let mut agent_name = identity.internal_name();
            let mut display_name = identity.display_name();
            let mut worktree_path = self.worktree_base.join(agent_name.as_str());
            let mut existing_identity_record = None;

            if let Some(expected_agent_name) = options.expected_agent_name.as_ref() {
                let expected_identity = AgentIdentity::from_internal_name(expected_agent_name.as_str());
                if normalize_agent_slug(expected_identity.slug()) != slug {
                    return Err(anyhow!(
                        "resolved resume identity {} does not match slug {}",
                        expected_agent_name,
                        slug
                    ));
                }
                identity = expected_identity;
                agent_type = identity.agent_type();
                agent_name = expected_agent_name.clone();
                display_name = identity.display_name();
                worktree_path = self.worktree_base.join(agent_name.as_str());
            } else if !options.standalone_repo && !worktree_path.exists() {
                let record = if let Some(record) =
                    self.agent_resolver().lookup_by_slug(&slug_key).await
                {
                    Some(record)
                } else {
                    self.agent_resolver()
                        .all()
                        .await
                        .into_iter()
                        .find(|record| normalize_agent_slug(record.slug.as_str()) == slug)
                };
                if let Some(record) = record {
                    if record.topology == Topology::WorktreePerAgent {
                        let record_worktree =
                            resolve_identity_working_dir(effective_project_dir, &record.working_dir);
                        if record_worktree.exists() {
                            agent_name = record.agent_name.clone();
                            agent_type = record.agent_type;
                            identity = AgentIdentity::from_internal_name(agent_name.as_str());
                            display_name = record.display_name.clone();
                            worktree_path = record_worktree;
                            existing_identity_record = Some(record);
                        }
                    }
                }

                if existing_identity_record.is_none() {
                    if let Some((found_identity, found_path)) =
                        find_existing_leaf_worktree_by_slug(&self.worktree_base, &slug).await?
                    {
                        identity = found_identity;
                        agent_type = identity.agent_type();
                        agent_name = identity.internal_name();
                        display_name = identity.display_name();
                        worktree_path = found_path;
                    }
                }
            }
            let prior_identity = self.agent_resolver().get(&agent_name).await;

            // Bounded preflight: drop sink-only residue directories left behind
            // by earlier failed spawns before deciding whether the planned path
            // is a reusable worktree. Registered, dirty, identified, or
            // ambiguous directories are never removed. A cleanup failure leaves
            // the residue in place and is reported, never silently ignored.
            if let Err(error) = super::cleanup::cleanup_unregistered_worktree_residue(
                self.project_dir(),
                self.git_wt(),
            )
            .await
            {
                warn!(
                    project = %self.project_dir().display(),
                    %error,
                    "worktree residue preflight failed; residue left in place"
                );
            }

            // Derive the expected deterministic birth branch from durable
            // identity and deterministic naming only. The branch observed on
            // disk must never redefine the expected branch.
            // Durable identity wins even when its worktree is missing or the
            // default path already exists; existing_identity_record is only
            // populated when the recorded worktree still exists.
            let durable_identity = existing_identity_record
                .as_ref()
                .or(prior_identity.as_ref());
            let expected_birth = expected_leaf_birth(
                durable_identity.map(|record| &record.birth_branch),
                options.base_branch.as_deref(),
                &effective_birth,
                agent_name.as_str(),
            )?;
            let branch_name = BranchName::try_from_str(expected_birth.to_string().as_str())
                .expect("validated string input is non-empty");

            let expected_head = if options.expected_agent_name.is_some() {
                options.start_point.as_deref()
            } else {
                None
            };

            // A resume is authorized against exactly one prior generation, so
            // its lineage is resolved before any worktree decision — including
            // the idempotent return below. A resume whose named prior invocation
            // is no longer the durable record is a stale target and is refused
            // here instead of attaching a branch nobody approved.
            let resume_lineage_head = match options.recovery_lineage.as_ref() {
                Some(lineage) => {
                    resolve_resume_lineage_head(
                        self.project_dir(),
                        &agent_name,
                        &branch_name,
                        lineage,
                    )
                    .await?
                }
                None => None,
            };
            // The prior publication head is resolved here, before any decision,
            // because reuse proves the same head set as attach: a live worktree
            // whose branch no longer contains what this owner published is a
            // rewritten branch, and an ordinary re-spawn must refuse it too.
            let recorded_head =
                recorded_branch_head(self.project_dir(), &agent_name, &branch_name).await;
            let heads = LeafHeadEvidence {
                expected: expected_head,
                prior_publication: recorded_head.as_ref(),
                resume_lineage: resume_lineage_head.as_ref(),
            };

            // Validate ownership before any idempotent return or reuse so a
            // stale routing record cannot bypass it. Existing paths must be
            // registered worktrees on the derived branch carrying every recorded
            // head; an absent path may be attached later only with proven
            // ownership.
            if !options.standalone_repo {
                verify_existing_leaf_worktree(
                    self.git_wt(),
                    effective_project_dir,
                    &worktree_path,
                    &branch_name,
                    heads,
                    false,
                )
                .await?;
            }

            let child_birth = expected_birth;

            // Idempotency check: stable routing IDs take precedence over a
            // stale display-name scan when resuming an existing owner.
            let config_dir = self
                .project_dir()
                .join(".exo/agents")
                .join(agent_name.as_str());
            let tab_alive = match self.routing_liveness(&config_dir).await {
                Some(alive) => alive,
                None => self.is_tmux_window_alive(&display_name).await,
            };
            if tab_alive {
                // A live worktree-per-agent route must still own a verified,
                // registered worktree whose branch carries every recorded head.
                // A stale route whose worktree was deleted, or whose branch was
                // rewritten away from them, must never return success.
                if !options.standalone_repo {
                    verify_existing_leaf_worktree(
                        self.git_wt(),
                        effective_project_dir,
                        &worktree_path,
                        &branch_name,
                        heads,
                        true,
                    )
                    .await?;
                }
                if options.expected_agent_name.is_some() {
                    self.refresh_agent_activity(&agent_name).await?;
                }
                info!(slug = %identity.slug(), "Leaf subtree already running, returning existing");
                return Ok(SpawnResult {
                    agent_dir: config_dir,
                    worktree_path,
                    branch_name: child_birth.to_string(),
                    agent_name,
                    issue_title: options.branch_name.clone(),
                    agent_type,
                    pane_id: None,
                });
            }

            persist_dispatch_intent(self.project_dir(), &agent_name, intent_id).await?;

            // Ensure a remote exists for local-only workflows
            ensure_remote_exists(effective_project_dir).await;

            // Push parent branch so child PRs can reference it as base
            ensure_branch_pushed(self.git_wt(), &current_branch, effective_project_dir).await;

            let actual_branch_name = branch_name.to_string();

            let mut worktree_rollback: Option<WorktreeRollback> = None;

            // Exclusive lifecycle region: the create, attach, and reuse
            // decisions below read the same registry and tree a residue cleanup
            // pass reads. Holding the lock across them means a live worktree
            // cannot be created behind a cleanup classification, and a verified
            // worktree cannot be quarantined before this decision completes.
            let _lifecycle = self
                .acquire_worktree_lifecycle("create or reuse a leaf worktree")
                .await?;

            if options.standalone_repo {
                worktree_rollback = Some(WorktreeRollback::armed(
                    self.git_wt().clone(),
                    worktree_path.clone(),
                ));
                self.init_standalone_repo(&worktree_path).await?;
                if !options.allowed_dirs.is_empty() {
                    self.copy_allowed_dirs(&worktree_path, &options.allowed_dirs).await?;
                }
            } else if worktree_path.exists() {
                if !worktree_path.is_dir() {
                    return Err(anyhow!(
                        "Existing leaf worktree path is not a directory: {}",
                        worktree_path.display()
                    ));
                }
                // Re-verify inside the lifecycle region. The preflight check ran
                // before the idempotency check, so ownership and the recorded
                // head set are proved again here while no cleanup pass can
                // quarantine the path. This is the reuse decision, and it is the
                // last point before the launch: a refusal here leaves the
                // existing worktree exactly as it was.
                verify_existing_leaf_worktree(
                    self.git_wt(),
                    effective_project_dir,
                    &worktree_path,
                    &branch_name,
                    heads,
                    true,
                )
                .await?;
                info!(
                    worktree_path = %worktree_path.display(),
                    branch_name = %branch_name,
                    "Reusing verified existing leaf worktree"
                );
            } else {
                // Fetch first, then read existence: the fetch can materialize a
                // remote-only branch locally, and reading existence beforehand
                // would send a preserved branch down the create path.
                let branch_state = self
                    .read_leaf_branch_state(effective_project_dir, &branch_name)
                    .await?;
                worktree_rollback = self
                    .provision_leaf_worktree(LeafProvisioning {
                        project_dir: effective_project_dir,
                        worktree_path: &worktree_path,
                        branch: &branch_name,
                        base_branch: &current_branch,
                        agent_name: &agent_name,
                        branch_exists: branch_state.exists,
                        start_point: options.start_point.as_deref(),
                        heads,
                        fresh_remote: branch_state.remote,
                    })
                    .await?;
            }
            // The lifecycle decision is complete; release the lock before the
            // long-running spawn work that follows.
            drop(_lifecycle);

            self.create_socket_symlink(&worktree_path).await;

            let default_dev = crate::domain::Role::dev();
            let role = options.role.as_ref().unwrap_or(&default_dev);
            let model = self.effective_model_for(agent_type, role.as_str(), options.model.as_deref());
            let effort = self.effective_effort_for(role.as_str(), None);
            let identity_model = existing_identity_record
                .as_ref()
                .and_then(|record| record.model.clone())
                .or_else(|| prior_identity.as_ref().and_then(|record| record.model.clone()))
                .or_else(|| model.clone());
            let identity_effort = existing_identity_record
                .as_ref()
                .and_then(|record| record.effort.clone())
                .or_else(|| prior_identity.as_ref().and_then(|record| record.effort.clone()))
                .or_else(|| effort.clone());
            let mut env_vars = self.common_spawn_env(&agent_name, &branch_name, role);
            self.write_agent_mcp_config(effective_project_dir, &worktree_path, agent_type, role)
                .await?;

            // The branch head this spawn settled on is what a PR is matched
            // against, so it is read from git rather than taken on trust. A
            // standalone repo owns no host PR, so it has no PR to match and
            // therefore no PR context to restore.
            let verified_head = match options.standalone_repo {
                true => None,
                false => Some(
                    self.git_wt()
                        .local_head(&branch_name)
                        .map_err(|error| anyhow!(EffectError::from(error)))?,
                ),
            };
            // Both the first spawn and the expected-agent resume compose the task
            // here, so a resume restores the PR context the new-spawn path
            // already restored instead of starting blind. A resume whose PR
            // cannot be read is refused here, before any tmux launch and while
            // the WorktreeRollback guard is still armed, so the worktree this
            // spawn created is removed rather than left behind half-provisioned.
            let task = self
                .leaf_task(
                    options,
                    effective_project_dir,
                    &branch_name,
                    verified_head.as_deref(),
                )
                .await?;

            // Open tmux window (not pane)
            // Task already includes leaf completion protocol — rendered by Haskell Prompt builder.
            let agent_config_dir = self.project_dir().join(".exo").join("agents").join(agent_name.as_str());
            let agent_config_preexisting = agent_config_dir.exists();
            fs::create_dir_all(&agent_config_dir).await?;
            env_vars.insert(
                "EXOMONAD_INVOCATION_EXIT_FILE".to_string(),
                agent_config_dir.join("exit_code").display().to_string(),
            );
            let _ = fs::remove_file(agent_config_dir.join("exit_code")).await;
            let launch_result = async {
                let prompt_file =
                    Self::write_prompt_file(self.project_dir(), &display_name, &task).await?;
                let full_command = self.leaf_launch_command(
                    agent_type,
                    role.as_str(),
                    options,
                    Some(prompt_file.as_path()),
                    &env_vars,
                    &worktree_path,
                );
                self.new_tmux_window_with_command(&display_name, &worktree_path, &full_command)
                    .await
            }
            .await;
            let window_id = match launch_result {
                Ok(wid) => wid,
                Err(e) => {
                    warn!(name = %identity.slug(), error = %e, "tmux window creation failed, rolling back");
                    if !agent_config_preexisting {
                        let _ = fs::remove_dir_all(&agent_config_dir).await;
                    }
                    // The WorktreeRollback guard removes a created worktree on
                    // this error path.
                    return Err(e);
                }
            };

            if let Err(error) = self.verify_tmux_window_startup(&window_id).await {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "tmux window exited before invocation startup readiness"
                );
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up failed leaf tmux window"
                    );
                }
                if !agent_config_preexisting {
                    let _ = fs::remove_dir_all(&agent_config_dir).await;
                }
                // The WorktreeRollback guard removes a created worktree here.
                return Err(error);
            }

            // Store window_id for message delivery and cleanup
            let routing = RoutingInfo::window(window_id.clone());
            let expected_routing = routing.clone();
            let identity_record = AgentIdentityRecord {
                agent_name: agent_name.clone(),
                slug: Slug::try_from_str(identity.slug())
                    .context("generated agent slug was empty")?,
                agent_type,
                birth_branch: child_birth,
                parent_branch: effective_birth,
                working_dir: worktree_path.clone(),
                display_name: display_name.clone(),
                topology: Topology::WorktreePerAgent,
                model: identity_model.clone(),
                effort: identity_effort.clone(),
                ledger_owned: false,
                slice_id: Some(options.branch_name.clone()),
            };
            let trigger = if options.expected_agent_name.is_some() {
                InvocationTrigger::ResumePr
            } else {
                InvocationTrigger::Spawn
            };
            if let Err(error) = self
                .finalize_spawn_with_invocation(
                &agent_name,
                routing,
                Some(identity_record),
                  InvocationMetadata {
                      runtime: agent_type,
                      trigger,
                      pr_number: options.invocation_pr_number,
                      head_sha: options.start_point.clone(),
                      model: model.clone(),
                      effort: effort.clone(),
                      recovery_lineage: options.recovery_lineage.clone(),
                      identity: None,
                      mode: InvocationMode::from_role(options.role.as_ref().map(|role| role.as_str())),
                  },
            )
                .await
            {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "Failed to persist leaf invocation metadata"
                );
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up leaf tmux window after metadata failure"
                    );
                }
                if !agent_config_preexisting {
                    let _ = fs::remove_dir_all(&agent_config_dir).await;
                }
                // The WorktreeRollback guard removes a created worktree here.
                return Err(error);
            }

            if let Err(error) = self.verify_tmux_window_startup(&window_id).await {
                warn!(
                    name = %identity.slug(),
                    window = %window_id,
                    %error,
                    "leaf invocation exited before spawn could report success"
                );
                match crate::services::agent_control::finish_invocation_and_tombstone(
                    &agent_config_dir,
                    &expected_routing,
                    InvocationStatus::Failed,
                    None,
                )
                .await
                {
                    Ok(InvocationFinishResult::IgnoredStale) => {
                        warn!(name = %identity.slug(), "Preserved newer invocation after startup failure")
                    }
                    Ok(InvocationFinishResult::Finished(_))
                    | Ok(InvocationFinishResult::Missing) => {}
                    Err(cleanup_error) => warn!(
                        name = %identity.slug(),
                        error = %cleanup_error,
                        "Failed to finish failed leaf invocation"
                    ),
                }
                if let Err(cleanup_error) = self.kill_tmux_window_id(&window_id).await {
                    warn!(
                        window = %window_id,
                        error = %cleanup_error,
                        "Failed to clean up leaf tmux window after startup failure"
                    );
                }
                return Err(error);
            }

            // Every post-creation step succeeded, including tmux launch and
            // identity finalization, so keep the worktree.
            if let Some(mut guard) = worktree_rollback.take() {
                guard.defuse();
            }
            Ok::<SpawnResult, anyhow::Error>(SpawnResult {
                agent_dir: agent_config_dir,
                worktree_path: worktree_path.clone(),
                branch_name: actual_branch_name,
                agent_name,
                issue_title: options.branch_name.clone(),
                agent_type,
                pane_id: None,
            })
        })
        .await
        .map_err(|_| {
            let msg = format!("spawn_leaf_subtree timed out after {}s", SPAWN_TIMEOUT.as_secs());
            warn!(branch_name = %options.branch_name, error = %msg, "spawn_leaf_subtree timed out");
            anyhow::Error::new(TimeoutError { message: msg })
        })??;

        info!(branch_name = %options.branch_name, "spawn_leaf_subtree completed successfully");
        Ok(result)
    }

    /// Spawn a reviewer agent for a sibling PR.
    ///
    /// Creates a tmux window with `role=reviewer` working from the project root.
    /// The reviewer examines `git diff base..{pr_branch}` and submits a
    /// Forgejo approval, request-changes review, or comment-only review.
    ///
    /// Use this when the TL receives `[PR READY]` from a child agent.
    #[instrument(skip_all, fields(pr_number = pr_entry.number))]
    pub async fn spawn_reviewer_subtree(
        &self,
        pr_entry: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let branch_name = format!("review-pr-{}", pr_entry.number);
        self.spawn_reviewer_subtree_with_criteria_named(pr_entry, caller_bb, &branch_name, &[])
            .await
    }

    pub async fn spawn_reviewer_subtree_with_criteria_named(
        &self,
        pr_entry: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        let context_section =
            render_reviewer_context_section(&self.reviewer_context, self.project_dir());
        let criteria_section = render_reviewer_acceptance_criteria(acceptance_criteria);
        let task = format!(
            "Review PR #{}: {}\n\nBranch: {}\nBase: {}\nAuthor: {}{}{}",
            pr_entry.number,
            pr_entry.title,
            pr_entry.head_branch,
            pr_entry.base_branch,
            pr_entry.author_agent,
            context_section,
            criteria_section,
        );

        // Compute the reviewer's own identity and path — same derivation spawn_subtree uses
        // internally so the MCP config agent_name matches the directory name.
        let agent_type = self.reviewer_agent_type;
        let identity = AgentIdentity::new(slugify(branch_name), agent_type);
        let reviewer_path = self.worktree_base.join(identity.internal_name().as_str());

        if agent_type == AgentType::Claude {
            preflight_reviewer_hook_environment(self.project_dir()).await?;
        }

        // Create a detached-HEAD worktree at the PR branch tip unless it already exists.
        // Detached so we don't compete with the worker's branch; the reviewer never commits.
        // This prevents clobbering the worker's opencode.json/MCP config while both run.
        let at_ref = pr_entry
            .last_head_sha
            .clone()
            .filter(|sha| !sha.trim().is_empty())
            .ok_or_else(|| {
                anyhow!(
                    "refusing reviewer spawn for PR #{} without a verified head SHA",
                    pr_entry.number
                )
            })?;
        if !reviewer_path.exists() {
            let git_wt = self.git_wt().clone();
            let path = reviewer_path.clone();
            let name = identity.internal_name().to_string();
            tokio::task::spawn_blocking(move || {
                git_wt.create_workspace_detached(&path, &at_ref, &name)
            })
            .await
            .context("tokio join error creating reviewer worktree")?
            .context("Failed to create reviewer worktree")?;
        } else {
            let current_head = Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(&reviewer_path)
                .output()
                .await
                .context("failed to inspect reviewer worktree head")?;
            let current_head = String::from_utf8_lossy(&current_head.stdout)
                .trim()
                .to_string();
            if !current_head.eq_ignore_ascii_case(&at_ref) {
                let checkout = Command::new("git")
                    .args(["checkout", "--detach", "--force", &at_ref])
                    .current_dir(&reviewer_path)
                    .output()
                    .await
                    .context("failed to align reviewer worktree with published head")?;
                if !checkout.status.success() {
                    anyhow::bail!(
                        "reviewer worktree is at {current_head}, cannot checkout published head {at_ref}: {}",
                        String::from_utf8_lossy(&checkout.stderr).trim()
                    );
                }
            }
        }

        let options = SpawnSubtreeOptions {
            task,
            branch_name: branch_name.to_string(),
            parent_session_id: None,
            role: Some(crate::domain::Role::reviewer()),
            agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            working_dir: Some(reviewer_path),
            permissions: Some(AgentPermissions {
                allow: vec![],
                deny: reviewer_harness_denied_tools(),
                default_mode: None,
            }),
            standalone_repo: false,
            allowed_dirs: vec![],
            model: self.reviewer_model.clone(),
            effort: self.reviewer_effort().map(str::to_string),
            invocation_pr_number: Some(pr_entry.number),
            invocation_head_sha: pr_entry.last_head_sha.clone(),
        };

        let result = self.spawn_subtree(&options, caller_bb).await?;

        Ok(result)
    }

    pub async fn spawn_reviewer_for_recovery(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
    ) -> Result<SpawnResult> {
        let reviewer_branch_name = format!("review-pr-{}", pr.number);
        self.spawn_reviewer_with_metadata_named(pr, caller_bb, &reviewer_branch_name, &[])
            .await
    }

    pub async fn spawn_reviewer_for_recovery_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
    ) -> Result<SpawnResult> {
        self.spawn_reviewer_with_metadata_named(pr, caller_bb, reviewer_branch_name, &[])
            .await
    }
    pub async fn spawn_reviewer_for_recovery_with_criteria_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        self.spawn_reviewer_with_metadata_named(
            pr,
            caller_bb,
            reviewer_branch_name,
            acceptance_criteria,
        )
        .await
    }

    async fn spawn_reviewer_with_metadata_named(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        caller_bb: &BirthBranch,
        reviewer_branch_name: &str,
        acceptance_criteria: &[String],
    ) -> Result<SpawnResult> {
        let reviewer_identity =
            AgentIdentity::new(slugify(reviewer_branch_name), self.reviewer_agent_type);
        let reviewer_internal_name = reviewer_identity.internal_name().to_string();
        let reviewer_birth_branch = caller_bb.child(&reviewer_internal_name).to_string();
        let result = self
            .spawn_reviewer_subtree_with_criteria_named(
                pr,
                caller_bb,
                reviewer_branch_name,
                acceptance_criteria,
            )
            .await?;
        self.persist_reviewer_assignment(pr, &reviewer_internal_name, &reviewer_birth_branch)
            .await;
        Ok(result)
    }

    async fn persist_reviewer_assignment(
        &self,
        pr: &crate::services::pr_registry::PrEntry,
        reviewer_internal_name: &str,
        reviewer_birth_branch: &str,
    ) {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return;
        };
        match crate::services::repo::get_repo_info(self.project_dir()).await {
            Ok(repo_info) => {
                let base_branch = BranchName::try_from_str(pr.base_branch.as_str())
                    .expect("validated string input is non-empty");
                let body = append_reviewer_metadata(
                    &pr.body,
                    reviewer_internal_name,
                    reviewer_birth_branch,
                );
                if let Err(err) = forgejo
                    .update_pull_request(
                        &repo_info.owner,
                        &repo_info.repo,
                        crate::domain::PRNumber::new(pr.number),
                        &pr.title,
                        &body,
                        &base_branch,
                    )
                    .await
                {
                    tracing::warn!(
                        pr_number = pr.number,
                        error = %err,
                        "Failed to persist reviewer assignment to Forgejo PR body"
                    );
                }
            }
            Err(err) => tracing::warn!(
                pr_number = pr.number,
                error = %err,
                "Failed to resolve repository while persisting reviewer assignment"
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::PRNumber;
    use exomonad_test_support::{
        assert_fixture_git_root, init_fixture_git_repository, run_fixture_git_command,
        ScrubGitRepositoryEnv,
    };
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    /// Invocation identifiers of two consecutive process generations, so a
    /// resume can name the one it continues.
    const FIRST_GENERATION: &str = "11111111-1111-1111-1111-111111111111";
    const SECOND_GENERATION: &str = "22222222-2222-2222-2222-222222222222";

    #[test]
    fn expected_leaf_birth_prefers_durable_identity() {
        let identity = BirthBranch::try_from_str("main.durable").unwrap();
        let effective = BirthBranch::try_from_str("main").unwrap();

        let expected =
            expected_leaf_birth(Some(&identity), Some("base"), &effective, "leaf").unwrap();

        assert_eq!(expected.as_str(), "main.durable");
    }

    #[test]
    fn expected_leaf_birth_is_deterministic_without_identity() {
        let effective = BirthBranch::try_from_str("main").unwrap();

        assert_eq!(
            expected_leaf_birth(None, None, &effective, "leaf")
                .unwrap()
                .as_str(),
            "main.leaf"
        );
        assert_eq!(
            expected_leaf_birth(None, Some("base"), &effective, "leaf")
                .unwrap()
                .as_str(),
            "base.leaf"
        );
    }

    #[test]
    fn leaf_worktree_action_attaches_when_branch_exists_without_start_point() {
        assert_eq!(
            leaf_worktree_action(true, None),
            LeafWorktreeAction::Attach,
            "an existing deterministic branch must attach even without a start point"
        );
    }

    #[test]
    fn leaf_worktree_action_attaches_when_branch_exists_with_start_point() {
        assert_eq!(
            leaf_worktree_action(true, Some("abc123")),
            LeafWorktreeAction::Attach
        );
    }

    #[test]
    fn leaf_worktree_action_creates_from_revision_without_branch() {
        assert_eq!(
            leaf_worktree_action(false, Some("abc123")),
            LeafWorktreeAction::CreateFromRevision
        );
    }

    #[test]
    fn leaf_worktree_action_creates_from_base_without_branch_or_start_point() {
        assert_eq!(
            leaf_worktree_action(false, None),
            LeafWorktreeAction::CreateFromBase
        );
    }

    #[test]
    fn only_the_branch_exists_code_may_be_recovered_by_an_attach() {
        let raced = anyhow::Error::from(EffectError::custom(
            "worktree.branch_exists",
            "Branch already exists: main.leaf",
        ));
        let unrelated = anyhow::Error::from(EffectError::custom(
            "worktree.base_branch_not_found",
            "Base branch not found: main.missing",
        ));
        let not_an_effect_error = anyhow!("git worktree creation panicked: boom");

        assert!(creation_failure_allows_attach(&raced));
        assert!(!creation_failure_allows_attach(&unrelated));
        assert!(!creation_failure_allows_attach(&not_an_effect_error));
    }

    /// A temporary repository with a bare remote, a leaf service, and the
    /// deterministic branch state the attach predicate reads.
    struct LeafFixture {
        /// Owns the temporary repository for the fixture's lifetime.
        _temp: tempfile::TempDir,
        repo: PathBuf,
        service: AgentControlService<crate::services::Services>,
        git_wt: Arc<GitWorktreeService>,
        leaf_agent: AgentName,
        base: BranchName,
        branch: BranchName,
        leaf_path: PathBuf,
    }

    impl LeafFixture {
        fn new(slug: &str) -> Self {
            let temp = tempfile::tempdir().expect("failed to create temp dir");
            let repo = temp.path().to_path_buf();
            init_fixture_git_repository(&repo).expect("git init failed");
            git(&repo, &["config", "user.email", "leaf@example.invalid"]);
            git(&repo, &["config", "user.name", "Leaf"]);
            git(&repo, &["commit", "--allow-empty", "-m", "base"]);
            let base_name = git_output(&repo, &["branch", "--show-current"]);
            let base = BranchName::try_from_str(base_name.as_str())
                .expect("validated string input is non-empty");
            let branch = BranchName::try_from_str(format!("{base_name}.{slug}").as_str())
                .expect("validated string input is non-empty");
            let remote = repo.join("remote.git");
            git(
                &repo,
                &["init", "--bare", remote.to_str().expect("valid UTF-8 path")],
            );
            git(
                &repo,
                &[
                    "remote",
                    "add",
                    "origin",
                    remote.to_str().expect("valid UTF-8 path"),
                ],
            );
            git(&repo, &["push", "-u", "origin", base_name.as_str()]);

            let git_wt = Arc::new(GitWorktreeService::new(repo.clone()));
            let mut services = crate::services::Services::test();
            services.project_dir = repo.clone();
            services.git_wt = git_wt.clone();
            // The provisioning events are durable, so a test that asserts one
            // must read the same ledger a run would.
            services.event_log = Some(Arc::new(
                crate::services::EventLog::open(repo.join(".exo").join("logs"))
                    .expect("event log opens"),
            ));
            Self {
                leaf_path: temp.path().join("worktrees").join(slug),
                service: AgentControlService::new(Arc::new(services)),
                leaf_agent: agent(&format!("{slug}-codex")),
                _temp: temp,
                git_wt,
                base,
                branch,
                repo,
            }
        }

        /// Every provisioning event this fixture's ledger recorded, by type.
        fn recorded(&self, event_type: &str) -> Vec<serde_json::Value> {
            let writer = crate::services::immutable_ledger::LedgerWriter::open_project(&self.repo)
                .expect("ledger opens");
            writer
                .read_resolved_events()
                .expect("ledger reads")
                .into_iter()
                .filter(|record| record.event.event_type == event_type)
                .map(|record| record.event.data.clone())
                .collect()
        }

        fn repo(&self) -> &Path {
            &self.repo
        }

        /// The local branch refs, so a test can prove an attach created none.
        fn local_branches(&self) -> Vec<String> {
            git_output(
                self.repo(),
                &["for-each-ref", "--format=%(refname:short)", "refs/heads"],
            )
            .lines()
            .map(str::to_string)
            .collect()
        }

        /// Move the released deterministic branch onto `revision`, leaving the
        /// commit it pointed at in the repository but out of the branch.
        ///
        /// The branch must not be checked out anywhere, which is why a test that
        /// rewrites it calls this before attaching the leaf worktree.
        fn move_branch_to(&self, revision: &str) {
            git(
                self.repo(),
                &["branch", "--force", self.branch.as_str(), revision],
            );
        }

        /// Create the deterministic leaf worktree and leave it there, as a
        /// preserved owner leaves it across invocations.
        fn attach_leaf_worktree(&self) {
            self.git_wt
                .create_workspace_from_existing_branch(&self.leaf_path, &self.branch)
                .expect("failed to attach the leaf worktree");
        }

        /// The reuse decision the spawn call site makes inside the exclusive
        /// lifecycle region, before any launch: a registered worktree on the
        /// deterministic branch, plus the whole head evidence set.
        async fn reuse_leaf_worktree(&self, heads: LeafHeadEvidence<'_>) -> Result<()> {
            verify_existing_leaf_worktree(
                &self.git_wt,
                self.repo(),
                &self.leaf_path,
                &self.branch,
                heads,
                true,
            )
            .await
        }

        /// The recorded publication head, resolved through the same function the
        /// spawn call site uses, for a ledger-owned publication this agent owns
        /// on the deterministic branch.
        async fn record_published_head(&self, agent: &AgentName, head_sha: &str) {
            write_publications(
                self.repo(),
                &[publication(
                    agent.as_str(),
                    self.branch.as_str(),
                    head_sha,
                    true,
                )],
            );
        }

        /// The single identity directory the owner recorded under `.exo/agents`.
        fn recorded_identities(&self) -> Vec<String> {
            let mut names: Vec<String> = std::fs::read_dir(self.repo().join(".exo/agents"))
                .map(|entries| {
                    entries
                        .filter_map(|entry| entry.ok())
                        .map(|entry| entry.file_name().to_string_lossy().into_owned())
                        .collect()
                })
                .unwrap_or_default();
            names.sort();
            names
        }

        /// Create the deterministic branch and release its worktree, so only
        /// the branch survives.
        fn seed_branch(&self) {
            let scratch = self.repo().join("scratch");
            self.git_wt
                .create_workspace(&scratch, &self.branch, &self.base)
                .expect("failed to create the leaf branch");
            self.git_wt
                .remove_workspace(&scratch)
                .expect("failed to release the leaf branch");
        }

        /// Commit on the deterministic branch through a temporary worktree and
        /// release it again, leaving the branch where the commit put it.
        fn commit_on_branch(&self, message: &str) -> String {
            let scratch = self.repo().join("scratch");
            self.git_wt
                .create_workspace_from_existing_branch(&scratch, &self.branch)
                .expect("failed to attach the leaf branch");
            git(&scratch, &["commit", "--allow-empty", "-m", message]);
            let head = git_output(&scratch, &["rev-parse", "HEAD"]);
            self.git_wt
                .remove_workspace(&scratch)
                .expect("failed to release the leaf branch");
            head
        }

        /// A preserved leaf branch carrying one commit, with no worktree on it.
        fn preserve_branch(&self, message: &str) -> String {
            self.seed_branch();
            self.commit_on_branch(message)
        }

        fn branch_head(&self) -> String {
            git_output(
                self.repo(),
                &["rev-parse", &format!("refs/heads/{}", self.branch)],
            )
        }

        /// The fresh remote evidence the production path fetches immediately
        /// before it decides.
        async fn fresh_remote(&self) -> crate::services::git_worktree::RemoteEvidence {
            ensure_branch_fetched(self.repo(), &self.branch).await
        }

        /// The same state the spawn call site reads, through the same function.
        async fn branch_state(&self) -> LeafBranchState {
            self.service
                .read_leaf_branch_state(self.repo(), &self.branch)
                .await
                .expect("branch state inspection must succeed")
        }

        async fn provisioning<'a, 'b>(
            &'a self,
            branch_exists: bool,
            recorded_head: Option<&'b RecordedHead>,
        ) -> LeafProvisioning<'a, 'b> {
            self.provisioning_with_heads(
                branch_exists,
                LeafHeadEvidence {
                    prior_publication: recorded_head,
                    ..Default::default()
                },
            )
            .await
        }

        /// The resume-shaped request: an expected resume head plus the head the
        /// resume's recovery lineage recorded for the deterministic branch.
        async fn resume_provisioning<'a, 'b>(
            &'a self,
            expected_head: Option<&'b str>,
            resume_lineage: Option<&'b RecordedHead>,
        ) -> LeafProvisioning<'a, 'b> {
            self.provisioning_with_heads(
                true,
                LeafHeadEvidence {
                    expected: expected_head,
                    resume_lineage,
                    ..Default::default()
                },
            )
            .await
        }

        async fn provisioning_with_heads<'a, 'b>(
            &'a self,
            branch_exists: bool,
            heads: LeafHeadEvidence<'b>,
        ) -> LeafProvisioning<'a, 'b> {
            self.provisioning_with_state(
                LeafBranchState {
                    exists: branch_exists,
                    remote: self.fresh_remote().await,
                },
                heads,
            )
            .await
        }

        async fn provisioning_with_state<'a, 'b>(
            &'a self,
            state: LeafBranchState,
            heads: LeafHeadEvidence<'b>,
        ) -> LeafProvisioning<'a, 'b> {
            LeafProvisioning {
                project_dir: self.repo(),
                worktree_path: &self.leaf_path,
                branch: &self.branch,
                base_branch: &self.base,
                agent_name: &self.leaf_agent,
                branch_exists: state.exists,
                start_point: None,
                heads,
                fresh_remote: state.remote,
            }
        }
    }

    /// Unwrap a provisioning result, whose rollback guard is deliberately not
    /// printable.
    fn provisioned(result: Result<Option<WorktreeRollback>>) -> Option<WorktreeRollback> {
        match result {
            Ok(guard) => guard,
            Err(error) => panic!("leaf provisioning failed: {error}"),
        }
    }

    /// Unwrap a provisioning refusal; the success value carries the same
    /// non-printable rollback guard.
    fn refusal(result: Result<Option<WorktreeRollback>>) -> anyhow::Error {
        match result {
            Err(error) => error,
            Ok(_) => panic!("leaf provisioning unexpectedly succeeded"),
        }
    }

    fn git(repo_dir: &Path, args: &[&str]) {
        run_fixture_git_command(repo_dir, args)
            .unwrap_or_else(|error| panic!("git {args:?} failed: {error}"));
    }

    fn git_output(repo_dir: &Path, args: &[&str]) -> String {
        String::from_utf8_lossy(
            &run_fixture_git_command(repo_dir, args)
                .unwrap_or_else(|error| panic!("git {args:?} failed: {error}"))
                .stdout,
        )
        .trim()
        .to_string()
    }

    fn recorded(sha: &str) -> RecordedHead {
        RecordedHead {
            sha: sha.to_string(),
            evidence: "published-heads.json",
        }
    }

    fn ownership_conflict(error: &anyhow::Error) -> String {
        let effect = error
            .downcast_ref::<EffectError>()
            .expect("an attach refusal must be a typed effect error");
        let EffectError::Custom { code, .. } = effect else {
            panic!("attach refusals must be custom coded, got {effect:?}");
        };
        assert_eq!(code, "worktree.branch_ownership_conflict");
        effect.to_string()
    }

    fn write_publications(project_dir: &Path, heads: &[serde_json::Value]) {
        std::fs::create_dir_all(project_dir.join(".exo")).unwrap();
        std::fs::write(
            project_dir.join(".exo/published-heads.json"),
            serde_json::json!({ "schema_version": 2, "heads": heads }).to_string(),
        )
        .unwrap();
    }

    fn publication(
        author: &str,
        branch: &str,
        head_sha: &str,
        ledger_owned: bool,
    ) -> serde_json::Value {
        serde_json::json!({
            "pr_number": 7,
            "head_branch": branch,
            "base_branch": "main",
            "head_sha": head_sha,
            "author_agent": author,
            "provenance": if ledger_owned { "ledger_owned" } else { "legacy" },
        })
    }

    /// Write the finished invocation record of one process generation.
    ///
    /// The generation is identified by `invocation_id`, so a test can prove a
    /// resume against a record that has since been replaced.
    fn write_invocation(
        project_dir: &Path,
        agent: &str,
        branch: &str,
        head_sha: &str,
        invocation_id: &str,
    ) {
        let agent_dir = project_dir.join(".exo/agents").join(agent);
        std::fs::create_dir_all(&agent_dir).unwrap();
        std::fs::write(
            agent_dir.join("invocation.json"),
            serde_json::json!({
                "invocation_id": invocation_id,
                "runtime": "codex",
                "trigger": "spawn",
                "routing": {"window_id": null, "pane_id": null, "parent_tab": null},
                "started_at": 1,
                "status": "exited",
                "exit_code": 0,
                "branch": branch,
                "head_sha": head_sha,
                "generation": 1,
            })
            .to_string(),
        )
        .unwrap();
    }

    /// The recovery lineage a resume is authorized against, naming the prior
    /// generation and continuing it by one.
    fn lineage(prior_invocation_id: &str) -> RecoveryInvocationLineage {
        RecoveryInvocationLineage {
            prior_invocation_id: prior_invocation_id.to_string(),
            invocation_generation: 2,
            recovery_round: 1,
            authorization_source: RecoveryAuthorization::HumanApproved,
        }
    }

    fn agent(name: &str) -> AgentName {
        AgentName::try_from_str(name).expect("validated string input is non-empty")
    }

    #[tokio::test]
    async fn preserved_branch_with_unique_commits_attaches_without_moving_its_head() {
        let fixture = LeafFixture::new("preserved");
        fixture.seed_branch();
        let published = fixture.commit_on_branch("published work");
        git(fixture.repo(), &["push", "origin", fixture.branch.as_str()]);
        // The preserved branch now carries a commit the remote has never seen.
        let head = fixture.commit_on_branch("unique unpushed commit");
        assert!(
            matches!(
                fixture.fresh_remote().await,
                crate::services::git_worktree::RemoteEvidence::AtSha(sha) if sha == published
            ),
            "the remote head must still be the published commit"
        );

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, None).await)
                .await,
        );

        assert!(
            rollback.is_some(),
            "an attach this attempt performed must stay covered by the rollback guard"
        );
        assert_eq!(
            fixture.branch_head(),
            head,
            "attaching must not move the head"
        );
        assert_eq!(
            git_output(&fixture.leaf_path, &["rev-parse", "HEAD"]),
            head,
            "the attached worktree must check out the preserved head"
        );
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed"),
            Some(std::fs::canonicalize(&fixture.leaf_path).unwrap())
        );
    }

    #[tokio::test]
    async fn a_remote_only_branch_is_attached_through_the_ordinary_path() {
        let fixture = LeafFixture::new("remote-only");
        fixture.seed_branch();
        let head = fixture.commit_on_branch("remote work");
        git(fixture.repo(), &["push", "origin", fixture.branch.as_str()]);
        git(fixture.repo(), &["branch", "-D", fixture.branch.as_str()]);
        assert!(
            !fixture
                .git_wt
                .branch_exists(&fixture.branch)
                .expect("branch inspection must succeed"),
            "the branch must live only on the remote before the decision reads it"
        );

        // The production read order must fetch first, so existence is reported
        // after the recovery fetch materialized the local branch.
        let state = fixture.branch_state().await;

        assert!(
            state.exists,
            "the recovery fetch must materialize a remote-only branch before existence is read"
        );
        assert!(
            matches!(&state.remote, crate::services::git_worktree::RemoteEvidence::AtSha(sha) if *sha == head),
            "the fetched remote evidence must be the published head, got {:?}",
            state.remote
        );
        assert_eq!(
            leaf_worktree_action(state.exists, None),
            LeafWorktreeAction::Attach,
            "a remote-only branch must take the ordinary attach path, not branch creation"
        );

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture
                        .provisioning_with_state(state, LeafHeadEvidence::default())
                        .await,
                )
                .await,
        );

        assert!(rollback.is_some());
        assert_eq!(fixture.branch_head(), head);
        assert_eq!(git_output(&fixture.leaf_path, &["rev-parse", "HEAD"]), head);
    }

    #[tokio::test]
    async fn a_created_branch_records_the_decision_and_the_completion() {
        let fixture = LeafFixture::new("decided");

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture
                        .provisioning_with_heads(false, LeafHeadEvidence::default())
                        .await,
                )
                .await,
        );

        assert!(rollback.is_some());
        let decided = fixture.recorded("agent.attach_decided");
        assert_eq!(decided.len(), 1, "exactly one decision per provisioning");
        assert_eq!(decided[0]["action"], "create_from_base");
        assert_eq!(decided[0]["branch_exists"], false);
        assert_eq!(decided[0]["branch"], fixture.branch.as_str());
        let completed = fixture.recorded("agent.attach_completed");
        assert_eq!(completed.len(), 1);
        assert_eq!(completed[0]["created"], true);
        assert_eq!(completed[0]["action"], "create_from_base");
    }

    #[tokio::test]
    async fn a_reused_worktree_records_completion_without_claiming_a_creation() {
        let fixture = LeafFixture::new("reused");
        fixture.seed_branch();
        fixture.attach_leaf_worktree();

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture
                        .provisioning_with_heads(true, LeafHeadEvidence::default())
                        .await,
                )
                .await,
        );

        assert!(
            rollback.is_none(),
            "a reused worktree must never be rolled back by this attempt"
        );
        let decided = fixture.recorded("agent.attach_decided");
        assert_eq!(decided[0]["action"], "attach");
        let completed = fixture.recorded("agent.attach_completed");
        assert_eq!(completed.len(), 1);
        assert_eq!(completed[0]["created"], false);
    }

    #[tokio::test]
    async fn an_ownership_conflict_records_its_terminal_machine_code() {
        let fixture = LeafFixture::new("conflict");
        fixture.preserve_branch("work in progress");
        git(fixture.repo(), &["push", "origin", fixture.branch.as_str()]);
        // A second worktree of this repository holds the deterministic branch,
        // which is the only shape `registered_worktree_for_branch` can see.
        let holder = fixture.repo().join("holder");
        git(
            fixture.repo(),
            &[
                "worktree",
                "add",
                holder.to_str().unwrap(),
                fixture.branch.as_str(),
            ],
        );
        let state = fixture
            .service
            .read_leaf_branch_state(fixture.repo(), &fixture.branch)
            .await
            .expect("branch state inspection must succeed");
        assert!(state.exists);
        assert!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed")
                .is_some(),
            "another worktree of this repository must hold the branch"
        );

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture
                        .provisioning_with_state(state, LeafHeadEvidence::default())
                        .await,
                )
                .await,
        );

        assert_eq!(
            stable_error_code(&error),
            Some(BRANCH_OWNERSHIP_CONFLICT_CODE),
            "the refusal must carry the terminal code the controller classifies"
        );
        let conflicts = fixture.recorded("agent.branch_ownership_conflict");
        assert_eq!(
            conflicts.len(),
            1,
            "one conflict row per refused provisioning"
        );
        assert_eq!(conflicts[0]["machine_code"], BRANCH_OWNERSHIP_CONFLICT_CODE);
        assert_eq!(conflicts[0]["branch"], fixture.branch.as_str());
        assert!(
            fixture.recorded("agent.attach_completed").is_empty(),
            "a refused provisioning must not claim a completion"
        );
    }

    #[tokio::test]
    async fn diverged_local_and_remote_heads_fail_with_an_ownership_conflict() {
        let fixture = LeafFixture::new("diverged");
        fixture.preserve_branch("local work");
        git(fixture.repo(), &["push", "origin", fixture.branch.as_str()]);
        let other = fixture.repo().join("other-clone");
        git(
            fixture.repo(),
            &[
                "clone",
                fixture.repo().join("remote.git").to_str().unwrap(),
                other.to_str().unwrap(),
            ],
        );
        git(&other, &["config", "user.email", "other@example.invalid"]);
        git(&other, &["config", "user.name", "Other"]);
        git(&other, &["checkout", fixture.branch.as_str()]);
        git(&other, &["commit", "--allow-empty", "-m", "remote ahead"]);
        git(&other, &["push", "origin", fixture.branch.as_str()]);

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, None).await)
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains("behind or diverged from its remote head"),
            "the refusal must name the divergence, got {message}"
        );
        assert!(
            !fixture.leaf_path.exists(),
            "a refused attach must not create the leaf worktree"
        );
    }

    #[tokio::test]
    async fn a_lost_branch_creation_race_attaches_once_and_keeps_the_head() {
        let fixture = LeafFixture::new("raced");
        let head = fixture.preserve_branch("raced work");
        git(fixture.repo(), &["push", "origin", fixture.branch.as_str()]);

        // The decision observed an absent branch, then a concurrent creator
        // made it: creation must fail with branch-exists and recover through a
        // single ownership-verified attach.
        assert!(
            fixture
                .git_wt
                .branch_exists(&fixture.branch)
                .expect("branch inspection must succeed"),
            "the branch must exist when creation runs, or there is no race to recover"
        );
        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(false, None).await)
                .await,
        );

        assert!(rollback.is_some());
        assert_eq!(fixture.branch_head(), head);
        assert_eq!(git_output(&fixture.leaf_path, &["rev-parse", "HEAD"]), head);
        let worktrees = git_output(fixture.repo(), &["worktree", "list"]);
        assert_eq!(
            worktrees.lines().count(),
            2,
            "exactly one leaf worktree may exist, got {worktrees}"
        );
    }

    #[tokio::test]
    async fn a_lost_branch_creation_race_reverifies_ownership_before_attaching() {
        let fixture = LeafFixture::new("raced-owner");
        fixture.preserve_branch("raced work");
        let winner = fixture.repo().join("winner");
        fixture
            .git_wt
            .create_workspace_from_existing_branch(&winner, &fixture.branch)
            .expect("the concurrent creator must hold the branch");

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(false, None).await)
                .await,
        );

        assert_both_paths_named(&error, &winner, &fixture.leaf_path);
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn a_branch_checked_out_elsewhere_fails_closed_naming_both_paths() {
        let fixture = LeafFixture::new("conflict");
        fixture.preserve_branch("held elsewhere");
        let holder = fixture.repo().join("holder");
        fixture
            .git_wt
            .create_workspace_from_existing_branch(&holder, &fixture.branch)
            .expect("the holder worktree must take the branch");

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, None).await)
                .await,
        );

        assert_both_paths_named(&error, &holder, &fixture.leaf_path);
        assert!(!fixture.leaf_path.exists());
    }

    /// A checked-out-elsewhere refusal must name the registered owner and the
    /// deterministic leaf path, both as Git reports them.
    fn assert_both_paths_named(error: &anyhow::Error, owner: &Path, expected: &Path) {
        let message = ownership_conflict(error);
        for path in [owner, expected] {
            let canonical = std::fs::canonicalize(path)
                .unwrap_or_else(|_| path.to_path_buf())
                .display()
                .to_string();
            assert!(
                message.contains(&canonical),
                "the refusal must name {canonical}, got {message}"
            );
        }
    }

    #[tokio::test]
    async fn a_branch_checked_out_at_the_expected_path_is_recovered_as_reuse() {
        let fixture = LeafFixture::new("reuse");
        fixture.preserve_branch("reused work");
        fixture
            .git_wt
            .create_workspace_from_existing_branch(&fixture.leaf_path, &fixture.branch)
            .expect("the deterministic path must take the branch");
        let head = fixture.branch_head();

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, None).await)
                .await,
        );

        assert!(
            rollback.is_none(),
            "a reused worktree must not be covered by this attempt's rollback guard"
        );
        assert_eq!(fixture.branch_head(), head);
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed"),
            Some(std::fs::canonicalize(&fixture.leaf_path).unwrap())
        );
    }

    #[tokio::test]
    async fn an_absent_remote_with_a_matching_recorded_head_attaches() {
        let fixture = LeafFixture::new("recorded");
        fixture.seed_branch();
        let published = fixture.commit_on_branch("published work");
        // The local head has moved past the recorded head, and the remote never
        // saw either commit.
        let head = fixture.commit_on_branch("unique unpushed commit");
        assert!(
            matches!(
                fixture.fresh_remote().await,
                crate::services::git_worktree::RemoteEvidence::Absent
            ),
            "the branch was never pushed, so remote evidence must be absent"
        );

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture
                        .provisioning(true, Some(&recorded(&published)))
                        .await,
                )
                .await,
        );

        assert!(
            rollback.is_some(),
            "an attach this attempt performed must stay covered by the rollback guard"
        );
        assert_eq!(fixture.branch_head(), head);
        assert_eq!(git_output(&fixture.leaf_path, &["rev-parse", "HEAD"]), head);
    }

    #[tokio::test]
    async fn an_absent_remote_with_a_mismatched_recorded_head_fails_closed() {
        let fixture = LeafFixture::new("mismatched");
        fixture.preserve_branch("recorded work");
        let unusable = "a".repeat(40);

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture.provisioning(true, Some(&recorded(&unusable))).await,
                )
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(&unusable) && message.contains("published-heads.json"),
            "the refusal must name the unusable recorded head, got {message}"
        );
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn an_absent_remote_without_a_recorded_head_fails_closed() {
        let fixture = LeafFixture::new("unproven");
        fixture.preserve_branch("unproven work");

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, None).await)
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str()) && message.contains("no head is recorded"),
            "the refusal must name the missing evidence, got {message}"
        );
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn a_failed_branch_creation_returns_without_a_worktree() {
        let fixture = LeafFixture::new("failing-create");
        let missing_base =
            BranchName::try_from_str("main.absent").expect("validated string input is non-empty");
        let mut request = fixture.provisioning(false, None).await;
        request.base_branch = &missing_base;
        let error = refusal(fixture.service.provision_leaf_worktree(request).await);

        assert!(
            !creation_failure_allows_attach(&error),
            "an unrelated creation failure must never be retried as an attach, got {error:#}"
        );
        assert!(
            format!("{error:#}").contains("Base branch not found"),
            "the failure must come from branch creation, got {error:#}"
        );

        assert!(!fixture.leaf_path.exists());
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed"),
            None
        );
    }

    #[test]
    fn the_rollback_guard_removes_the_worktree_it_created() {
        let fixture = LeafFixture::new("rollback");
        let created = fixture.repo().join("created");
        fixture
            .git_wt
            .create_workspace(&created, &fixture.branch, &fixture.base)
            .expect("failed to create the worktree under test");

        drop(WorktreeRollback::armed(
            fixture.git_wt.clone(),
            created.clone(),
        ));

        assert!(
            !created.exists(),
            "a failed spawn must not leave the worktree it created"
        );
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed"),
            None
        );
    }

    #[tokio::test]
    async fn recorded_branch_head_prefers_the_latest_ledger_owned_publication() {
        let fixture = LeafFixture::new("publication");
        let branch = fixture.branch.as_str().to_string();
        let owner = fixture.leaf_agent.as_str();
        write_publications(
            fixture.repo(),
            &[
                publication(
                    owner,
                    &branch,
                    "1111111111111111111111111111111111111111",
                    true,
                ),
                publication(
                    owner,
                    &branch,
                    "2222222222222222222222222222222222222222",
                    true,
                ),
                publication(
                    owner,
                    &branch,
                    "3333333333333333333333333333333333333333",
                    false,
                ),
            ],
        );

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert_eq!(
            recorded.map(|head| head.sha).as_deref(),
            Some("2222222222222222222222222222222222222222"),
            "the newest ledger-owned publication of this agent must win over a later legacy one"
        );
    }

    #[tokio::test]
    async fn a_legacy_publication_alone_is_not_head_evidence() {
        let fixture = LeafFixture::new("legacy");
        let branch = fixture.branch.as_str().to_string();
        let owner = fixture.leaf_agent.as_str();
        let head = fixture.preserve_branch("legacy work");
        write_publications(fixture.repo(), &[publication(owner, &branch, &head, false)]);

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert!(
            recorded.is_none(),
            "a never-verified legacy publication must not prove a commit head"
        );
    }

    #[tokio::test]
    async fn an_absent_remote_with_only_a_legacy_publication_fails_closed() {
        let fixture = LeafFixture::new("legacy-attach");
        let branch = fixture.branch.as_str().to_string();
        let head = fixture.preserve_branch("legacy work");
        write_publications(
            fixture.repo(),
            &[publication(
                fixture.leaf_agent.as_str(),
                &branch,
                &head,
                false,
            )],
        );
        assert!(matches!(
            fixture.fresh_remote().await,
            crate::services::git_worktree::RemoteEvidence::Absent
        ));
        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.provisioning(true, recorded.as_ref()).await)
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str()) && message.contains("no head is recorded"),
            "an unverified publication must leave the attach decision without evidence, got {message}"
        );
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn an_unreadable_publication_registry_never_falls_through_to_the_invocation() {
        let fixture = LeafFixture::new("unreadable-registry");
        let branch = fixture.branch.as_str().to_string();
        // Leave the registry unparseable while the invocation record carries a
        // head, so a fall-through would happily prove the wrong commit.
        write_publications(fixture.repo(), &[]);
        std::fs::write(
            fixture.repo().join(".exo/published-heads.json"),
            b"{\"heads\": [",
        )
        .unwrap();
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            &branch,
            "6666666666666666666666666666666666666666",
            FIRST_GENERATION,
        );

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert!(
            recorded.is_none(),
            "an unreadable authoritative registry must not be replaced by the invocation head"
        );
    }

    #[tokio::test]
    async fn an_unreadable_invocation_record_is_not_head_evidence() {
        let fixture = LeafFixture::new("unreadable-invocation");
        let agent_dir = fixture
            .repo()
            .join(".exo/agents")
            .join(fixture.leaf_agent.as_str());
        std::fs::create_dir_all(&agent_dir).unwrap();
        std::fs::write(agent_dir.join("invocation.json"), b"{\"invocation_id\": ").unwrap();

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert!(
            recorded.is_none(),
            "an invocation record that cannot be parsed must not prove a commit head"
        );
    }

    #[tokio::test]
    async fn recorded_branch_head_ignores_a_publication_filed_by_another_agent() {
        let fixture = LeafFixture::new("foreign");
        let branch = fixture.branch.as_str().to_string();
        write_publications(
            fixture.repo(),
            &[publication(
                "someone-else",
                &branch,
                "1111111111111111111111111111111111111111",
                true,
            )],
        );

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert!(
            recorded.is_none(),
            "another agent's publication is not head evidence for this branch"
        );
    }

    #[tokio::test]
    async fn recorded_branch_head_reads_the_owner_invocation_for_the_same_branch() {
        let fixture = LeafFixture::new("invocation");
        let branch = fixture.branch.as_str().to_string();
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            &branch,
            "4444444444444444444444444444444444444444",
            FIRST_GENERATION,
        );

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert_eq!(
            recorded.map(|head| head.sha).as_deref(),
            Some("4444444444444444444444444444444444444444")
        );
    }

    #[tokio::test]
    async fn recorded_branch_head_is_absent_without_durable_evidence() {
        let fixture = LeafFixture::new("unrecorded");
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            "main.some-other-branch",
            "5555555555555555555555555555555555555555",
            FIRST_GENERATION,
        );

        let recorded =
            recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch).await;

        assert!(
            recorded.is_none(),
            "a head recorded against another branch is not evidence for this one"
        );
    }

    /// A repository whose origin is a hosted URL, with a deterministic branch at
    /// a known head and a fake Forgejo that answers PR lookups over HTTP.
    ///
    /// The hosted origin is required because PR resolution reads owner and repo
    /// from it; the fake answers on its own port, so no forge is contacted.
    struct PullRequestFixture {
        /// Owns the temporary repository for the fixture's lifetime.
        _temp: tempfile::TempDir,
        /// Owns the scratch directory the leaf worktree is created in, kept
        /// outside the repository so a fixture leaves nothing behind in the main
        /// worktree.
        _worktrees: tempfile::TempDir,
        /// Keeps the fake Forgejo serving for the fixture's lifetime.
        forge: MockServer,
        repo: PathBuf,
        leaf_path: PathBuf,
        service: AgentControlService<crate::services::Services>,
        slug: String,
        branch: BranchName,
        /// The head the fake forge may report for the branch instead.
        other_head: String,
        head: String,
        leaf: AgentName,
    }

    /// The pull request the fake Forgejo reports for the deterministic branch.
    const PR_NUMBER: u64 = 7;
    const PR_TITLE: &str = "Restore resume lineage";
    const HOSTED_REMOTE: &str = "https://forge.example/owner/repo.git";
    const REVIEW_BODY: &str = "Tighten the refusal message.";
    const REVIEW_COMMENT: &str = "Name the observed head.";

    impl PullRequestFixture {
        async fn new(slug: &str) -> Self {
            let temp = tempfile::tempdir().expect("failed to create temp dir");
            let repo = temp.path().to_path_buf();
            init_fixture_git_repository(&repo).expect("git init failed");
            git(&repo, &["config", "user.email", "leaf@example.invalid"]);
            git(&repo, &["config", "user.name", "Leaf"]);
            git(&repo, &["commit", "--allow-empty", "-m", "base"]);
            let base_name = git_output(&repo, &["branch", "--show-current"]);
            let branch = BranchName::try_from_str(format!("{base_name}.{slug}").as_str())
                .expect("validated string input is non-empty");
            // The branch sits one commit past the base, so a test can offer the
            // forge a different head for the same branch.
            let other_head = git_output(&repo, &["rev-parse", "HEAD"]);
            git(&repo, &["commit", "--allow-empty", "-m", "branch work"]);
            git(&repo, &["branch", branch.as_str()]);
            git(&repo, &["remote", "add", "origin", HOSTED_REMOTE]);
            let head = git_output(&repo, &["rev-parse", branch.as_str()]);

            let worktrees = tempfile::tempdir().expect("failed to create the worktree dir");
            let leaf_path = worktrees.path().join(slug);
            let git_wt = Arc::new(GitWorktreeService::new(repo.clone()));
            let forge = MockServer::start().await;
            let mut services = crate::services::Services::test();
            services.project_dir = repo.clone();
            // The default `git_wt` points at the process working directory, so
            // every mutating call must name the fixture repository explicitly.
            services.git_wt = git_wt.clone();
            services.forgejo_client = Some(
                crate::services::forgejo::ForgejoClient::new(&forge.uri(), "token-123")
                    .expect("a fake Forgejo URL must construct a client"),
            );
            assert!(
                git_wt
                    .branch_exists(&branch)
                    .expect("the fixture branch must exist in the fixture repository"),
                "the git service must operate on the fixture repository, not the working directory"
            );
            Self {
                repo,
                leaf_path,
                service: AgentControlService::new(Arc::new(services)),
                slug: slug.to_string(),
                branch,
                other_head,
                head,
                leaf: agent(&format!("{slug}-codex")),
                forge,
                _worktrees: worktrees,
                _temp: temp,
            }
        }

        /// Report one open pull request for the deterministic branch, whose head
        /// is whatever the caller claims the forge has for it.
        async fn report_open_pull_request(&self, head_sha: &str) {
            Mock::given(method("GET"))
                .and(path("/api/v1/repos/owner/repo/pulls"))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                        "number": PR_NUMBER,
                        "title": PR_TITLE,
                        "state": "open",
                        "head": {"ref": self.branch.as_str(), "sha": head_sha},
                        "base": {"ref": "main"},
                    }])),
                )
                .mount(&self.forge)
                .await;
        }

        /// Report review feedback and one inline comment for the open PR.
        async fn report_review_feedback(&self) {
            let reviews = format!("/api/v1/repos/owner/repo/pulls/{PR_NUMBER}/reviews");
            Mock::given(method("GET"))
                .and(path(reviews.clone()))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                        "id": 1,
                        "state": "changes_requested",
                        "body": REVIEW_BODY,
                        "commit_id": self.head,
                    }])),
                )
                .mount(&self.forge)
                .await;
            Mock::given(method("GET"))
                .and(path(format!("{reviews}/1/comments")))
                .respond_with(
                    ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                        "body": REVIEW_COMMENT,
                        "path": "src/spawn.rs",
                    }])),
                )
                .mount(&self.forge)
                .await;
        }

        /// Make the named endpoint fail the way an unreachable forge does.
        async fn report_forge_failure(&self, endpoint: &str) {
            Mock::given(method("GET"))
                .and(path(endpoint))
                .respond_with(ResponseTemplate::new(500).set_body_string("forge is down"))
                .mount(&self.forge)
                .await;
        }

        /// Attach the deterministic branch at the leaf path, the way
        /// provisioning does before the task is composed, and return the guard
        /// that a refusal must still clean up.
        fn attach_leaf_worktree(&self) -> WorktreeRollback {
            self.service
                .git_wt()
                .create_workspace_from_existing_branch(&self.leaf_path, &self.branch)
                .expect("failed to attach the leaf worktree");
            WorktreeRollback::armed(self.service.git_wt().clone(), self.leaf_path.clone())
        }

        /// The spawn options the host builds for a `resume_pr`: it resolved the
        /// identity and the PR head, so the branch and start point are
        /// host-owned and no directory scan may replace them.
        fn resume_options(&self) -> SpawnLeafOptions {
            SpawnLeafOptions {
                expected_agent_name: Some(self.leaf.clone()),
                start_point: Some(self.head.clone()),
                invocation_pr_number: Some(PR_NUMBER),
                ..self.first_spawn_options()
            }
        }

        /// The spawn options the host builds for a first spawn: the host owns
        /// no identity yet, so it has no pull request to be blind to.
        fn first_spawn_options(&self) -> SpawnLeafOptions {
            SpawnLeafOptions {
                task: "Address the review feedback.".to_string(),
                branch_name: self.slug.clone(),
                role: Some(crate::domain::Role::dev()),
                agent_type: AgentType::Codex,
                claude_flags: ClaudeSpawnFlags::default(),
                standalone_repo: false,
                allowed_dirs: Vec::new(),
                start_point: None,
                base_branch: Some("main".to_string()),
                expected_agent_name: None,
                invocation_pr_number: None,
                recovery_lineage: None,
                model: None,
            }
        }

        /// The task the given path composes, at the head it verified.
        async fn task_for(&self, options: &SpawnLeafOptions) -> Result<String> {
            self.service
                .leaf_task(options, &self.repo, &self.branch, Some(&self.head))
                .await
        }

        /// The task the resume path composes, at the head it verified.
        async fn resume_task(&self) -> String {
            self.task_for(&self.resume_options())
                .await
                .expect("a resume whose pull request is readable must compose a task")
        }

        /// A second service over the same repository with no forge client, as a
        /// project that never configured one has.
        fn service_without_forge(&self) -> AgentControlService<crate::services::Services> {
            let mut services = crate::services::Services::test();
            services.project_dir = self.repo.clone();
            services.git_wt = Arc::new(GitWorktreeService::new(self.repo.clone()));
            AgentControlService::new(Arc::new(services))
        }

        /// The HTTP methods the fake forge saw. A resume only ever reads.
        async fn forge_methods(&self) -> Vec<String> {
            self.forge
                .received_requests()
                .await
                .unwrap_or_default()
                .iter()
                .map(|request| request.method.to_string())
                .collect()
        }
    }

    /// Unwrap a task-composition refusal, which carries a stable code.
    fn task_refusal(result: Result<String>) -> anyhow::Error {
        match result {
            Err(error) => error,
            Ok(task) => panic!("task composition unexpectedly succeeded: {task}"),
        }
    }

    /// A recorded head this repository cannot resolve is refused with the
    /// dedicated message: it must name the branch, the recorded head, its
    /// evidence source, the observed head, the two ways a head goes missing, and
    /// the operator action.
    fn assert_unknown_recorded_head(error: &anyhow::Error, unknown: &str, fixture: &LeafFixture) {
        let message = ownership_conflict(error);
        let observed = fixture.branch_head();
        for expected in [
            fixture.branch.as_str(),
            unknown,
            "published-heads.json",
            observed.as_str(),
            "unknown to this repository",
            "rewritten",
            "garbage-collected",
            "retry the spawn",
        ] {
            assert!(
                message.contains(expected),
                "the refusal must name {expected:?}, got {message}"
            );
        }
    }

    #[tokio::test]
    async fn a_resume_attaches_a_verified_branch_without_creating_one() {
        let fixture = LeafFixture::new("resume-attach");
        let head = fixture.preserve_branch("prior attempt work");
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &head,
            FIRST_GENERATION,
        );
        let branches_before = fixture.local_branches();
        let lineage_head = resolve_resume_lineage_head(
            fixture.repo(),
            &fixture.leaf_agent,
            &fixture.branch,
            &lineage(FIRST_GENERATION),
        )
        .await
        .expect("a resume against the current record must resolve")
        .expect("the finished generation recorded a head for this branch");

        let rollback = provisioned(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture.resume_provisioning(None, Some(&lineage_head)).await,
                )
                .await,
        );

        assert!(
            rollback.is_some(),
            "the resume attached a worktree, so this attempt must own its rollback"
        );
        assert_eq!(
            fixture.local_branches(),
            branches_before,
            "a resume must attach the deterministic branch, never create another one"
        );
        assert_eq!(fixture.branch_head(), head);
        assert_eq!(git_output(&fixture.leaf_path, &["rev-parse", "HEAD"]), head);
    }

    #[tokio::test]
    async fn a_resume_with_a_stale_recovery_lineage_fails_closed() {
        let fixture = LeafFixture::new("stale-lineage");
        let head = fixture.preserve_branch("prior attempt work");
        // The durable record is a newer generation than the lineage names, so
        // the authorized resume target no longer exists.
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &head,
            SECOND_GENERATION,
        );

        let error = resolve_resume_lineage_head(
            fixture.repo(),
            &fixture.leaf_agent,
            &fixture.branch,
            &lineage(FIRST_GENERATION),
        )
        .await
        .expect_err("a resume naming a superseded generation must be refused");

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains(FIRST_GENERATION)
                && message.contains(SECOND_GENERATION),
            "the refusal must name the branch, the authorized lineage head, and the observed one, got {message}"
        );
    }

    #[tokio::test]
    async fn a_resume_without_a_durable_invocation_record_fails_closed() {
        let fixture = LeafFixture::new("unrecorded-resume");
        fixture.preserve_branch("prior attempt work");

        let error = resolve_resume_lineage_head(
            fixture.repo(),
            &fixture.leaf_agent,
            &fixture.branch,
            &lineage(FIRST_GENERATION),
        )
        .await
        .expect_err("a resume with nothing to prove its lineage must be refused");

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str()) && message.contains("no readable invocation"),
            "the refusal must name the missing evidence, got {message}"
        );
    }

    #[tokio::test]
    async fn a_resume_whose_lineage_head_the_branch_rewrote_fails_closed() {
        let fixture = LeafFixture::new("rewritten-lineage");
        let abandoned = fixture.preserve_branch("work the branch no longer has");
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &abandoned,
            FIRST_GENERATION,
        );
        // The branch was rewritten back to the base, so the commit the prior
        // generation recorded is no longer in its history.
        fixture.move_branch_to(fixture.base.as_str());
        let lineage_head = resolve_resume_lineage_head(
            fixture.repo(),
            &fixture.leaf_agent,
            &fixture.branch,
            &lineage(FIRST_GENERATION),
        )
        .await
        .expect("the lineage itself is current, only the branch moved")
        .expect("the finished generation recorded a head for this branch");

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture.resume_provisioning(None, Some(&lineage_head)).await,
                )
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains(&abandoned)
                && message.contains(&fixture.branch_head()),
            "the refusal must name the branch, the expected lineage head, and the observed head, got {message}"
        );
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn a_resume_with_a_wrong_expected_head_fails_closed() {
        let fixture = LeafFixture::new("wrong-expected-head");
        fixture.preserve_branch("prior attempt work");
        let unrelated = "b".repeat(40);

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(fixture.resume_provisioning(Some(&unrelated), None).await)
                .await,
        );

        let message = ownership_conflict(&error);
        assert!(
            message.contains(&unrelated) && message.contains(&fixture.branch_head()),
            "the refusal must name the expected and the observed head, got {message}"
        );
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn a_repeated_resume_preserves_one_identity_worktree_and_branch() {
        /// Resume once: prove the lineage against the current record, then
        /// provision the deterministic branch. Reports whether this attempt had
        /// to create the worktree.
        async fn resume_once(fixture: &LeafFixture, generation: &str) -> bool {
            let lineage_head = resolve_resume_lineage_head(
                fixture.repo(),
                &fixture.leaf_agent,
                &fixture.branch,
                &lineage(generation),
            )
            .await
            .expect("a resume continues the current record")
            .expect("each finished generation recorded a head for this branch");
            let guard = provisioned(
                fixture
                    .service
                    .provision_leaf_worktree(
                        fixture.resume_provisioning(None, Some(&lineage_head)).await,
                    )
                    .await,
            );
            let created = guard.is_some();
            // A live spawn holds this guard until its agent is finalized. This
            // test stands in for that live agent, so the guard is never dropped
            // and the second resume finds the worktree the first one attached.
            std::mem::forget(guard);
            created
        }

        let fixture = LeafFixture::new("repeat-resume");
        let head = fixture.preserve_branch("first attempt work");
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &head,
            FIRST_GENERATION,
        );

        let first_resume_created_the_worktree = resume_once(&fixture, FIRST_GENERATION).await;
        // The resumed generation replaces the record it continued.
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &head,
            SECOND_GENERATION,
        );
        let second_resume_created_the_worktree = resume_once(&fixture, SECOND_GENERATION).await;

        assert!(
            first_resume_created_the_worktree,
            "the first resume must attach the branch at the deterministic leaf path"
        );
        assert!(
            !second_resume_created_the_worktree,
            "a second resume must reuse the worktree that already holds the branch"
        );
        assert_eq!(
            fixture.recorded_identities(),
            vec![fixture.leaf_agent.as_str().to_string()],
            "a repeated resume must not create a second workflow owner"
        );
        assert_eq!(
            fixture.local_branches(),
            vec![
                fixture.base.as_str().to_string(),
                fixture.branch.as_str().to_string()
            ],
            "a repeated resume must not create a second branch"
        );
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed")
                .as_deref(),
            Some(
                std::fs::canonicalize(&fixture.leaf_path)
                    .expect("the attached leaf path must resolve")
                    .as_path()
            ),
            "the branch must stay checked out at the one deterministic leaf path"
        );
        assert_eq!(fixture.branch_head(), head);
    }

    #[tokio::test]
    async fn a_resume_restores_the_existing_open_pull_request_context() {
        let fixture = PullRequestFixture::new("pr-context").await;
        fixture.report_open_pull_request(&fixture.head).await;
        fixture.report_review_feedback().await;

        let task = fixture.resume_task().await;

        assert!(
            task.starts_with("Address the review feedback."),
            "the host task must survive intact, got {task}"
        );
        assert!(
            task.contains(&format!("Existing PR: #{PR_NUMBER} — {PR_TITLE}"))
                && task.contains("Do NOT create a new pull request."),
            "a resume must be told which PR it continues, got {task}"
        );
        assert!(
            task.contains(REVIEW_BODY) && task.contains(REVIEW_COMMENT),
            "a resume must see the feedback already on that PR, got {task}"
        );
        assert!(
            fixture
                .forge_methods()
                .await
                .iter()
                .all(|method| method == "GET"),
            "restoring PR context must only read from the forge"
        );
    }

    #[tokio::test]
    async fn a_pull_request_at_a_different_head_is_not_injected() {
        let fixture = PullRequestFixture::new("pr-other-head").await;
        assert_ne!(
            fixture.other_head, fixture.head,
            "the fake must report a head this branch does not have"
        );
        fixture.report_open_pull_request(&fixture.other_head).await;
        fixture.report_review_feedback().await;

        let task = fixture.resume_task().await;

        assert_eq!(
            task, "Address the review feedback.",
            "a PR whose head has moved past the verified branch describes other work"
        );
    }

    #[tokio::test]
    async fn a_resume_without_an_open_pull_request_gets_no_pr_context() {
        let fixture = PullRequestFixture::new("pr-absent").await;
        Mock::given(method("GET"))
            .and(path("/api/v1/repos/owner/repo/pulls"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!([])))
            .mount(&fixture.forge)
            .await;

        let task = fixture.resume_task().await;

        assert_eq!(
            task, "Address the review feedback.",
            "a branch with no qualifying open PR must get no PR context"
        );
    }

    #[tokio::test]
    async fn a_repeated_resume_restores_the_same_pull_request() {
        let fixture = PullRequestFixture::new("pr-repeat").await;
        fixture.report_open_pull_request(&fixture.head).await;
        fixture.report_review_feedback().await;

        let first = fixture.resume_task().await;
        let second = fixture.resume_task().await;

        assert_eq!(first, second, "a second resume must restore the same PR");
        assert_eq!(
            first.matches(&format!("Existing PR: #{PR_NUMBER}")).count(),
            1,
            "a repeated resume must name one PR, never a second one"
        );
        assert!(
            fixture
                .forge_methods()
                .await
                .iter()
                .all(|method| method == "GET"),
            "a repeated resume must not file another pull request"
        );
    }

    #[tokio::test]
    async fn a_reused_live_worktree_must_contain_its_recorded_publication_head() {
        let fixture = LeafFixture::new("reuse-recorded");
        let head = fixture.preserve_branch("published work");
        fixture.attach_leaf_worktree();
        fixture
            .record_published_head(&fixture.leaf_agent.clone(), &head)
            .await;
        let recorded = recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch)
            .await
            .expect("a ledger-owned publication for this agent and branch is evidence");

        fixture
            .reuse_leaf_worktree(LeafHeadEvidence {
                prior_publication: Some(&recorded),
                ..Default::default()
            })
            .await
            .expect("a branch that still contains its recorded head must be reusable");

        assert!(fixture.leaf_path.exists());
        assert_eq!(fixture.branch_head(), head);
    }

    #[tokio::test]
    async fn a_rewritten_live_worktree_fails_closed_and_is_left_untouched() {
        let fixture = LeafFixture::new("reuse-rewritten");
        let published = fixture.preserve_branch("published work");
        fixture
            .record_published_head(&fixture.leaf_agent.clone(), &published)
            .await;
        let recorded = recorded_branch_head(fixture.repo(), &fixture.leaf_agent, &fixture.branch)
            .await
            .expect("the publication is evidence");
        // The branch was rewritten away from what this owner published, and the
        // worktree is then (re)created on the rewritten branch.
        fixture.move_branch_to(fixture.base.as_str());
        fixture.attach_leaf_worktree();
        let observed = fixture.branch_head();

        let error = fixture
            .reuse_leaf_worktree(LeafHeadEvidence {
                prior_publication: Some(&recorded),
                ..Default::default()
            })
            .await
            .expect_err("a rewritten branch must not be reused");

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains(&published)
                && message.contains(&observed)
                && message.contains("published-heads.json"),
            "the refusal must name the branch, the recorded head, its evidence source, and the \
             observed head, got {message}"
        );
        assert!(
            fixture.leaf_path.exists(),
            "a refused reuse must leave the existing worktree in place"
        );
        assert_eq!(
            fixture
                .git_wt
                .registered_worktree_for_branch(&fixture.branch)
                .expect("registry lookup must succeed")
                .as_deref(),
            Some(
                std::fs::canonicalize(&fixture.leaf_path)
                    .expect("the attached leaf path must resolve")
                    .as_path()
            ),
            "a refused reuse must leave the worktree registered to the branch"
        );
    }

    #[tokio::test]
    async fn a_rewritten_resume_lineage_head_fails_closed_on_a_live_worktree() {
        let fixture = LeafFixture::new("reuse-lineage");
        let prior = fixture.preserve_branch("prior attempt work");
        write_invocation(
            fixture.repo(),
            fixture.leaf_agent.as_str(),
            fixture.branch.as_str(),
            &prior,
            FIRST_GENERATION,
        );
        let lineage_head = resolve_resume_lineage_head(
            fixture.repo(),
            &fixture.leaf_agent,
            &fixture.branch,
            &lineage(FIRST_GENERATION),
        )
        .await
        .expect("the lineage is current")
        .expect("the finished generation recorded a head for this branch");
        fixture.move_branch_to(fixture.base.as_str());
        fixture.attach_leaf_worktree();
        let observed = fixture.branch_head();

        let error = fixture
            .reuse_leaf_worktree(LeafHeadEvidence {
                resume_lineage: Some(&lineage_head),
                ..Default::default()
            })
            .await
            .expect_err("a branch that dropped its lineage head must not be reused");

        let message = ownership_conflict(&error);
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains(&prior)
                && message.contains(&observed)
                && message.contains("invocation.json"),
            "the refusal must name the branch, the lineage head, its evidence source, and the \
             observed head, got {message}"
        );
        assert!(
            fixture.leaf_path.exists(),
            "a refused reuse must leave the existing worktree in place"
        );
    }

    #[tokio::test]
    async fn an_unknown_recorded_head_refuses_an_attach() {
        let fixture = LeafFixture::new("unknown-attach");
        fixture.preserve_branch("prior attempt work");
        // A head this repository has never received: the recorded commit was
        // rewritten away or reclaimed, so there is nothing to compare against.
        let unknown = "c".repeat(40);

        let error = refusal(
            fixture
                .service
                .provision_leaf_worktree(
                    fixture.provisioning(true, Some(&recorded(&unknown))).await,
                )
                .await,
        );

        assert_unknown_recorded_head(&error, &unknown, &fixture);
        assert!(!fixture.leaf_path.exists());
    }

    #[tokio::test]
    async fn an_unknown_recorded_head_refuses_a_reused_worktree() {
        let fixture = LeafFixture::new("unknown-reuse");
        fixture.preserve_branch("prior attempt work");
        fixture.attach_leaf_worktree();
        let unknown = "c".repeat(40);
        let observed = fixture.branch_head();

        let error = fixture
            .reuse_leaf_worktree(LeafHeadEvidence {
                prior_publication: Some(&recorded(&unknown)),
                ..Default::default()
            })
            .await
            .expect_err("a recorded head this repository cannot resolve must refuse reuse");

        assert_unknown_recorded_head(&error, &unknown, &fixture);
        assert!(
            fixture.leaf_path.exists(),
            "a refused reuse must leave the existing worktree in place"
        );
        assert_eq!(fixture.branch_head(), observed);
    }

    #[tokio::test]
    async fn a_resume_fails_closed_when_the_pull_request_cannot_be_read() {
        let fixture = PullRequestFixture::new("pr-outage").await;
        fixture
            .report_forge_failure("/api/v1/repos/owner/repo/pulls")
            .await;
        let guard = fixture.attach_leaf_worktree();

        let error = task_refusal(fixture.task_for(&fixture.resume_options()).await);

        let effect = error
            .downcast_ref::<EffectError>()
            .expect("a refused resume must be a typed effect error");
        let EffectError::Custom { code, .. } = effect else {
            panic!("a refused resume must be custom coded, got {effect:?}");
        };
        assert_eq!(code, "worktree.pr_context_unavailable");
        let message = effect.to_string();
        assert!(
            message.contains(fixture.branch.as_str()) && message.contains("HTTP 500"),
            "the refusal must name the branch and the underlying forge error, got {message}"
        );

        // The refusal propagates with `?` while the guard is armed, exactly as
        // the call site does, so the worktree this spawn created is removed.
        drop(guard);
        assert!(
            !fixture.leaf_path.exists(),
            "a resume refused before launch must not leave its worktree behind"
        );
    }

    #[tokio::test]
    async fn a_resume_fails_closed_when_no_forge_is_configured() {
        let fixture = PullRequestFixture::new("pr-no-forge").await;
        // No client means the query was never made, which is not the same fact
        // as "this branch has no pull request".
        let unconfigured = fixture.service_without_forge();

        let error = task_refusal(
            unconfigured
                .leaf_task(
                    &fixture.resume_options(),
                    &fixture.repo,
                    &fixture.branch,
                    Some(&fixture.head),
                )
                .await,
        );

        let effect = error
            .downcast_ref::<EffectError>()
            .expect("a refused resume must be a typed effect error");
        let EffectError::Custom { code, .. } = effect else {
            panic!("a refused resume must be custom coded, got {effect:?}");
        };
        assert_eq!(code, "worktree.pr_context_unavailable");
        let message = effect.to_string();
        assert!(
            message.contains(fixture.branch.as_str())
                && message.contains("no forge client is configured"),
            "the refusal must name the branch and the missing configuration, got {message}"
        );
        assert!(
            fixture.forge_methods().await.is_empty(),
            "with no client configured there is nothing to query"
        );
    }

    #[tokio::test]
    async fn a_first_spawn_proceeds_when_the_pull_request_cannot_be_read() {
        let fixture = PullRequestFixture::new("pr-outage-spawn").await;
        fixture
            .report_forge_failure("/api/v1/repos/owner/repo/pulls")
            .await;

        let task = fixture
            .task_for(&fixture.first_spawn_options())
            .await
            .expect("a first spawn owns no pull request, so a forge outage is logged, not fatal");

        assert_eq!(
            task, "Address the review feedback.",
            "a first spawn proceeds with the caller's task and records the loss in the log"
        );
    }

    #[tokio::test]
    async fn a_failed_review_listing_still_restores_the_pull_request_identity() {
        let fixture = PullRequestFixture::new("pr-review-outage").await;
        fixture.report_open_pull_request(&fixture.head).await;
        fixture
            .report_forge_failure(&format!(
                "/api/v1/repos/owner/repo/pulls/{PR_NUMBER}/reviews"
            ))
            .await;

        let task = fixture.resume_task().await;

        assert!(
            task.contains(&format!("Existing PR: #{PR_NUMBER} — {PR_TITLE}"))
                && task.contains("Do NOT create a new pull request."),
            "an unreadable review listing must not cost the leaf its pull request, got {task}"
        );
        assert!(
            !task.contains(REVIEW_BODY) && !task.contains(REVIEW_COMMENT),
            "feedback the forge could not return must not be invented, got {task}"
        );
    }

    #[test]
    fn pr_context_requires_an_open_pull_request_at_the_verified_head() {
        let open =
            |state: &str, merged: bool, head_sha: Option<&str>, branch: &str| ForgejoPullRequest {
                number: PRNumber::new(PR_NUMBER),
                url: String::new(),
                title: PR_TITLE.to_string(),
                body: String::new(),
                head_ref: BranchName::try_from_str(branch).expect("literal branch is non-empty"),
                base_ref: BranchName::try_from_str("main").expect("literal branch is non-empty"),
                state: state.to_string(),
                merged,
                head_sha: head_sha.map(str::to_string),
                base_sha: None,
                merge_commit_sha: None,
            };
        let branch = BranchName::try_from_str("main.leaf").expect("literal branch is non-empty");

        assert!(pr_carries_verified_head(
            &open("open", false, Some("abc"), "main.leaf"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("open", false, Some("def"), "main.leaf"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("open", false, None, "main.leaf"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("closed", false, Some("abc"), "main.leaf"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("open", true, Some("abc"), "main.leaf"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("open", false, Some("abc"), "main.other"),
            &branch,
            "abc"
        ));
        assert!(!pr_carries_verified_head(
            &open("open", false, Some("abc"), "main.leaf"),
            &branch,
            ""
        ));
    }

    #[test]
    fn test_opencode_dev_instructions_clarify_mcp_tools_are_not_shell_commands() {
        assert!(OPENCODE_DEV_INSTRUCTIONS
            .contains("MCP tools exposed inside your agent tool interface"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("not shell commands"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("not on PATH"));
        assert!(OPENCODE_DEV_INSTRUCTIONS.contains("which file_pr"));
    }

    #[tokio::test]
    async fn test_find_existing_leaf_worktree_by_slug_uses_recorded_agent_type_suffix() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree_base = temp_dir.path().join("worktrees");
        fs::create_dir_all(worktree_base.join("resume-leaf-opencode"))
            .await
            .unwrap();

        let (identity, path) = find_existing_leaf_worktree_by_slug(&worktree_base, "resume-leaf")
            .await
            .unwrap()
            .unwrap();

        assert_eq!(identity.slug(), "resume-leaf");
        assert_eq!(identity.agent_type(), AgentType::OpenCode);
        assert_eq!(path, worktree_base.join("resume-leaf-opencode"));
    }

    #[tokio::test]
    async fn test_find_existing_leaf_worktree_by_slug_rejects_prefix_collision() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree_base = temp_dir.path().join("worktrees");
        fs::create_dir_all(worktree_base.join("resume-leaf-extra-opencode"))
            .await
            .unwrap();

        let found = find_existing_leaf_worktree_by_slug(&worktree_base, "resume-leaf")
            .await
            .unwrap();

        assert!(found.is_none());
    }

    #[test]
    fn test_parse_git_status_paths_lists_status_entries() {
        let paths = parse_git_status_paths(
            b" M src/lib.rs\0A  docs/new.md\0R  new.rs\0old.rs\0?? scratch.txt\0",
        );
        assert_eq!(
            paths,
            vec![
                "src/lib.rs".to_string(),
                "docs/new.md".to_string(),
                "new.rs".to_string(),
                "old.rs".to_string(),
                "scratch.txt".to_string(),
            ]
        );
    }

    #[test]
    fn test_parse_git_status_paths_preserves_spaces_and_quotes() {
        let paths = parse_git_status_paths(b"?? user file.txt\0?? \"quoted file.txt\"\0");

        assert_eq!(paths, vec!["user file.txt", "\"quoted file.txt\""]);
    }

    #[test]
    fn test_tl_runtime_checkpoint_is_the_only_exempt_subtree() {
        assert!(is_tl_runtime_checkpoint(".exo/tl-loop/root/run.json"));
        assert!(is_tl_runtime_checkpoint("./.exo/tl-loop"));
        assert!(!is_tl_runtime_checkpoint(".exo/config.toml"));
        assert!(!is_tl_runtime_checkpoint("src/.exo/tl-loop/file"));
    }

    async fn run_git_test_command(worktree: &Path, args: &[&str]) {
        assert_fixture_git_root(worktree).unwrap();
        let output = Command::new("git")
            .args(args)
            .current_dir(worktree)
            .scrub_git_repository_env()
            .output()
            .await
            .unwrap_or_else(|error| panic!("failed to run git {args:?}: {error}"));
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    async fn init_test_repo(worktree: &Path) {
        init_fixture_git_repository(worktree).unwrap();
        run_git_test_command(worktree, &["config", "user.email", "test@example.com"]).await;
        run_git_test_command(worktree, &["config", "user.name", "Test User"]).await;
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_ignores_gitignored_tracked_files() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::write(worktree.join(".gitignore"), "*.db\n")
            .await
            .unwrap();
        fs::create_dir_all(worktree.join(".chainlink"))
            .await
            .unwrap();
        fs::write(worktree.join(".chainlink/issues.db"), "before")
            .await
            .unwrap();
        run_git_test_command(worktree, &["add", ".gitignore"]).await;
        run_git_test_command(worktree, &["add", "-f", ".chainlink/issues.db"]).await;
        run_git_test_command(worktree, &["commit", "-q", "-m", "init"]).await;

        fs::write(worktree.join(".chainlink/issues.db"), "after")
            .await
            .unwrap();

        ensure_clean_spawn_worktree(worktree).await.unwrap();
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_blocks_nonignored_dirty_files() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::create_dir_all(worktree.join("src")).await.unwrap();
        fs::write(worktree.join("src/lib.rs"), "before")
            .await
            .unwrap();
        run_git_test_command(worktree, &["add", "src/lib.rs"]).await;
        run_git_test_command(worktree, &["commit", "-q", "-m", "init"]).await;

        fs::write(worktree.join("src/lib.rs"), "after")
            .await
            .unwrap();

        let message = ensure_clean_spawn_worktree(worktree)
            .await
            .unwrap_err()
            .to_string();
        assert!(message.contains("src/lib.rs"));
        assert!(message.contains("dirty TL worktree"));
    }

    #[tokio::test]
    async fn test_ensure_clean_spawn_worktree_ignores_runtime_checkpoints_only() {
        let temp_dir = tempfile::tempdir().unwrap();
        let worktree = temp_dir.path();
        init_test_repo(worktree).await;

        fs::create_dir_all(worktree.join(".exo/tl-loop/root"))
            .await
            .unwrap();
        fs::write(worktree.join(".exo/tl-loop/root/run.json"), "checkpoint")
            .await
            .unwrap();
        ensure_clean_spawn_worktree(worktree).await.unwrap();

        fs::write(
            worktree.join(".exo/config.toml"),
            "spawn_agent_type = \"codex\"\n",
        )
        .await
        .unwrap();
        let message = ensure_clean_spawn_worktree(worktree)
            .await
            .unwrap_err()
            .to_string();
        assert!(message.contains(".exo/config.toml"));
    }

    #[test]
    fn test_dirty_spawn_error_includes_count_files_and_recovery() {
        let files = vec!["src/lib.rs".to_string(), "docs/new.md".to_string()];
        let message = dirty_spawn_error(&files).to_string();
        assert!(message.contains(
            "BLOCKED: cannot spawn agent into a dirty TL worktree. 2 file(s) have uncommitted changes:"
        ));
        assert!(message.contains("  src/lib.rs"));
        assert!(message.contains("  docs/new.md"));
        assert!(message.contains("Commit the scaffold"));
        assert!(message.contains("discard_worker_output"));
        assert!(message.contains("dev-leaves fork from your branch HEAD"));
    }

    #[test]
    fn test_format_worker_age_uses_readable_units() {
        assert_eq!(format_worker_age(std::time::Duration::from_secs(42)), "42s");
        assert_eq!(format_worker_age(std::time::Duration::from_secs(125)), "2m");
        assert_eq!(
            format_worker_age(std::time::Duration::from_secs(7_400)),
            "2h"
        );
    }

    #[test]
    fn test_active_worker_error_includes_name_options_and_scope() {
        let worker = ActiveWorker {
            name: "alpha-codex".to_string(),
            age: "2m".to_string(),
        };
        let message = active_worker_error(&worker).to_string();
        assert!(message.contains("Active worker in this TL worktree: `alpha-codex`"));
        assert!(message.contains("spawned 2m ago"));
        assert!(message.contains("Wait for the active worker's handoff"));
        assert!(message.contains("Use `spawn_leaf` for parallel work"));
        assert!(message.contains("Per-worker attribution"));
    }

    #[tokio::test]
    async fn test_reviewer_hook_preflight_reports_missing_socket() {
        let dir = tempfile::tempdir().unwrap();

        let error = preflight_reviewer_hook_environment(dir.path())
            .await
            .unwrap_err();
        let message = error.to_string();

        assert!(
            message.contains("Reviewer hook preflight failed: parent server socket missing"),
            "unexpected error: {message}"
        );
        assert!(message.contains(".exo/server.sock"));
    }

    #[tokio::test]
    async fn test_write_opencode_git_stub_points_to_project_git_dir() {
        let dir = tempfile::tempdir().unwrap();
        let project_dir = dir.path().join("project");
        let agent_config_dir = dir.path().join("agent-config");
        fs::create_dir_all(&agent_config_dir).await.unwrap();

        AgentControlService::<crate::services::Services>::write_opencode_git_stub(
            &agent_config_dir,
            &project_dir,
        )
        .await
        .unwrap();

        let content = fs::read_to_string(agent_config_dir.join(".git"))
            .await
            .unwrap();
        assert_eq!(
            content,
            format!("gitdir: {}\n", project_dir.join(".git").display())
        );
    }

    #[test]
    fn test_reviewer_harness_denies_builtin_edit_tools() {
        let denied = reviewer_harness_denied_tools();

        for tool in [
            "Edit",
            "Write",
            "NotebookEdit",
            "spawn_leaf",
            "spawn_worker",
            "merge_pr",
            "file_pr",
        ] {
            assert!(
                denied.contains(&tool.to_string()),
                "reviewer harness permissions must deny {tool}"
            );
        }
    }

    #[test]
    fn test_coding_profiles_use_one_shot_assignment_handoff() {
        for instructions in [OPENCODE_DEV_INSTRUCTIONS, CODEX_DEV_INSTRUCTIONS] {
            assert!(instructions.contains("one assignment"));
            assert!(instructions.contains("exact invocation"));
            assert!(instructions.contains("resume_pr"));
            assert!(instructions.contains("exit"));
            assert!(!instructions.contains("Stay active"));
            assert!(!instructions.contains("Do not exit or consider yourself done"));
        }

        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("one exact PR/SHA assignment"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("Exit after submitting"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("fresh SHA-scoped reviewer invocation"));
        assert!(CODEX_REVIEWER_INSTRUCTIONS.contains("never wait for CI, merge-ready"));
    }

    /// Regression test for a reviewer sandbox/instructions mismatch: `codex_config.rs`
    /// hardcodes `network_access = false` for the Codex reviewer profile, so any
    /// developer instructions that tell the reviewer to hit Forgejo over the network
    /// from its own shell (curl, fj, wget) can never actually run. The verdict must
    /// route through the MCP tools instead, which execute in the unsandboxed
    /// ExoMonad host process. See docs/decisions/agent-sandbox-profiles.md.
    #[test]
    fn test_codex_reviewer_instructions_do_not_require_sandboxed_network_access() {
        let lower = CODEX_REVIEWER_INSTRUCTIONS.to_lowercase();
        // Checks for actual shell invocations, not just the word "curl"/"fj" — the
        // instructions are allowed to mention them by name when explaining why the
        // reviewer must NOT invoke them directly (network_access = false).
        assert!(
            !lower.contains("curl -") && !lower.contains("curl http"),
            "reviewer instructions must not tell the sandboxed shell to curl Forgejo directly: {CODEX_REVIEWER_INSTRUCTIONS}"
        );
        assert!(
            !lower.contains("fj pr review")
                && !lower.contains("fj pr view")
                && !lower.contains("fj pr files"),
            "reviewer instructions must not tell the sandboxed shell to run fj against Forgejo: {CODEX_REVIEWER_INSTRUCTIONS}"
        );
        assert!(
            CODEX_REVIEWER_INSTRUCTIONS.contains("approve_pr")
                && CODEX_REVIEWER_INSTRUCTIONS.contains("request_changes"),
            "reviewer instructions must submit verdicts through the approve_pr/request_changes MCP tools"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_resolves_relative_paths_against_project_dir() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let ctx = vec![
            ".exo/context/reviewer-checklist.md".to_string(),
            "AGENTS.md".to_string(),
        ];
        let section = render_reviewer_context_section(&ctx, &project_dir);
        assert!(
            section.contains("/tmp/exo-project/.exo/context/reviewer-checklist.md"),
            "relative paths must be joined with project_dir; got: {section}"
        );
        assert!(
            section.contains("/tmp/exo-project/AGENTS.md"),
            "second relative path must also be resolved; got: {section}"
        );
        assert!(
            section.starts_with("\n\nRead first:\n"),
            "header line must precede the bullet list; got: {section}"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_passes_absolute_paths_through() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let ctx = vec!["/etc/some/absolute.md".to_string()];
        let section = render_reviewer_context_section(&ctx, &project_dir);
        assert!(
            section.contains("/etc/some/absolute.md"),
            "absolute paths must pass through; got: {section}"
        );
        assert!(
            !section.contains("/tmp/exo-project/etc"),
            "absolute paths must NOT be joined with project_dir; got: {section}"
        );
    }

    #[test]
    fn test_render_reviewer_context_section_empty_emits_nothing() {
        let project_dir = std::path::PathBuf::from("/tmp/exo-project");
        let section = render_reviewer_context_section(&[], &project_dir);
        assert!(
            section.is_empty(),
            "empty ctx must emit no header; got: {section}"
        );
    }

    #[tokio::test]
    async fn test_copy_allowed_dirs_validation() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();

        // Setup source dirs
        let shared_context = project_dir.join("shared-context");
        fs::create_dir_all(&shared_context).await.unwrap();
        fs::write(shared_context.join("ref.txt"), "context data")
            .await
            .unwrap();

        let agent_wt = project_dir.join("agent-wt");
        fs::create_dir_all(&agent_wt).await.unwrap();

        let git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.clone(),
        ));
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.clone();
        services.git_wt = git_wt;
        let service = AgentControlService::new(Arc::new(services));

        // Test valid copy
        service
            .copy_allowed_dirs(&agent_wt, &["shared-context".to_string()])
            .await
            .unwrap();
        assert!(agent_wt
            .join(".exo/context/shared-context/ref.txt")
            .exists());

        // Test invalid paths (should skip but not fail)
        service
            .copy_allowed_dirs(
                &agent_wt,
                &["/absolute".to_string(), "../outside".to_string()],
            )
            .await
            .unwrap();
        assert!(!agent_wt.join(".exo/context/absolute").exists());
        assert!(!agent_wt.join(".exo/context/outside").exists());
    }

    #[tokio::test]
    async fn dispatch_intent_is_atomic_and_precedes_spawn_resources() {
        let temp_dir = tempfile::tempdir().unwrap();
        let agent = AgentIdentity::new("leaf-a".to_string(), AgentType::Codex).internal_name();

        persist_dispatch_intent(temp_dir.path(), &agent, Some("intent-a"))
            .await
            .unwrap();

        let agent_dir = temp_dir.path().join(".exo/agents").join(agent.as_str());
        assert_eq!(
            fs::read_to_string(agent_dir.join("dispatch_intent"))
                .await
                .unwrap(),
            "intent-a"
        );
        assert!(!agent_dir.join("dispatch_intent.tmp").exists());
    }

    #[test]
    fn test_claude_project_path_encoding() {
        // Claude Code encodes paths via [^a-zA-Z0-9] → '-'
        // Verified against actual ~/.claude/projects/ directory names.
        let encode = |s: &str| -> String {
            s.chars()
                .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
                .collect()
        };

        // Basic path
        assert_eq!(
            encode("/home/inanna/dev/exomonad"),
            "-home-inanna-dev-exomonad"
        );
        // Worktree path (dots and hyphens in segments)
        assert_eq!(
            encode("/home/inanna/dev/exomonad/.exo/worktrees/fork-session"),
            "-home-inanna-dev-exomonad--exo-worktrees-fork-session"
        );
        // Hidden dir (leading dot → double dash after parent separator)
        assert_eq!(
            encode("/home/inanna/.config/home-manager"),
            "-home-inanna--config-home-manager"
        );
        // Deep nested path with hyphens
        assert_eq!(
            encode("/home/inanna/dev/aegis-binder-diagnostic-framework"),
            "-home-inanna-dev-aegis-binder-diagnostic-framework"
        );
        // Path with spaces
        assert_eq!(
            encode("/home/user/My Projects/app"),
            "-home-user-My-Projects-app"
        );
    }
}
