//! Audited commit-all preparation for an ordered TL scope handoff.
//!
//! The caller worktree is resolved by the authenticated effect context. Every
//! Git subprocess runs under the existing managed-process supervisor while one
//! refs/index lease set remains held across inspection, staging, and commit.

use super::process_supervision::{run_supervised, GitProcessAudit, ProcessRunError};
use crate::effects::{EffectError, EffectResult};
use crate::services::{
    EventLog, GitMutationLease, GitMutationLeaseSet, GitMutationResource, GitMutationSafety,
};
use exomonad_proto::effects::agent::{PrepareScopeHandoffRequest, PrepareScopeHandoffResponse};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitStatus;
use std::time::Duration;
use tokio::process::Command;
use tokio::sync::oneshot;

const LEASE_WAIT: Duration = Duration::from_secs(2);
const GIT_TIMEOUT: Duration = Duration::from_secs(120);
const COMMIT_SUBJECT: &str = "Prepare parent worktree for ordered subTL handoff";

pub(crate) async fn prepare_scope_handoff(
    project_dir: &Path,
    source_worktree: &Path,
    authenticated_controller: &str,
    birth_branch: &str,
    request: PrepareScopeHandoffRequest,
) -> EffectResult<PrepareScopeHandoffResponse> {
    validate_request(&request, authenticated_controller)?;
    let source_worktree = canonical_source_worktree(project_dir, source_worktree)
        .map_err(EffectError::invalid_input)?;
    let operation_identity = operation_identity(&request, &source_worktree);
    let context = match HandoffContext::prepare(
        project_dir,
        &source_worktree,
        &operation_identity,
        authenticated_controller,
        birth_branch,
    )
    .await
    {
        Ok(context) => context,
        Err(error) => return Ok(blocked_response(&request, error)),
    };

    let (cancel_sender, mut cancellation) = oneshot::channel();
    let supervisor = tokio::spawn(async move {
        let result = execute_handoff(context, request.clone(), &mut cancellation).await;
        (request, result)
    });
    let joined = supervisor.await.map_err(|_| {
        EffectError::custom(
            "scope_handoff_supervisor_failed",
            "scope handoff supervisor ended unexpectedly; retry with the same handoff identity to reconcile Git evidence",
        )
    });
    drop(cancel_sender);
    match joined? {
        (_request, Ok(response)) => Ok(response),
        (request, Err(error)) => Ok(blocked_response(&request, error)),
    }
}

struct HandoffContext {
    worktree: PathBuf,
    branch: String,
    head: String,
    operation_identity: String,
    agent_id: String,
    birth_branch: String,
    leases: GitMutationLeaseSet,
    event_log: EventLog,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum GitFailureOutcome {
    Refused,
    Unknown,
}

#[derive(Debug)]
struct GitFailure {
    outcome: GitFailureOutcome,
    message: String,
}

impl GitFailure {
    fn refused(message: impl Into<String>) -> Self {
        Self {
            outcome: GitFailureOutcome::Refused,
            message: message.into(),
        }
    }

    fn unknown(message: impl Into<String>) -> Self {
        Self {
            outcome: GitFailureOutcome::Unknown,
            message: message.into(),
        }
    }
}

impl HandoffContext {
    async fn prepare(
        project_dir: &Path,
        source_worktree: &Path,
        operation_identity: &str,
        agent_id: &str,
        birth_branch: &str,
    ) -> Result<Self, String> {
        let safety = GitMutationSafety::new(project_dir.to_path_buf());
        let target = if source_worktree.is_absolute() {
            source_worktree.to_path_buf()
        } else {
            project_dir.join(source_worktree)
        };
        let operation_identity = operation_identity.to_string();
        let lease_operation_identity = operation_identity.clone();
        let leases = tokio::task::spawn_blocking(move || {
            safety.acquire_set_with_index_override(
                &target,
                &[GitMutationResource::SharedRefs, GitMutationResource::Index],
                &lease_operation_identity,
                LEASE_WAIT,
                None,
            )
        })
        .await
        .map_err(|_| "Git mutation lease acquisition did not finish".to_string())?
        .map_err(|error| format!("Git mutation readiness is unavailable: {error}"))?;
        let paths = index_lease(&leases)
            .ok_or_else(|| "Git mutation lease does not cover the worktree index".to_string())?
            .paths
            .clone();
        let event_log = EventLog::open(project_dir.join(".exo/logs"))
            .map_err(|_| "durable Git operation audit is unavailable".to_string())?;
        let mut context = Self {
            worktree: paths.worktree_root,
            branch: String::new(),
            head: String::new(),
            operation_identity: operation_identity.to_string(),
            agent_id: agent_id.to_string(),
            birth_branch: birth_branch.to_string(),
            leases,
            event_log,
        };
        let (cancel_sender, mut cancellation) = oneshot::channel();
        context.branch = context
            .git_text(&["branch", "--show-current"], &mut cancellation)
            .await?;
        context.head = context
            .git_text(&["rev-parse", "--verify", "HEAD"], &mut cancellation)
            .await?;
        drop(cancel_sender);
        Ok(context)
    }

    async fn git_text(
        &self,
        arguments: &[&str],
        cancellation: &mut oneshot::Receiver<()>,
    ) -> Result<String, String> {
        let output = self
            .git(arguments.iter().map(OsString::from).collect(), cancellation)
            .await
            .map_err(|failure| failure.message)?;
        if !output.status.success() {
            return Err(git_refusal(output.status, "Git inspection failed"));
        }
        String::from_utf8(output.stdout)
            .map(|value| value.trim().to_string())
            .map_err(|_| "Git returned malformed repository evidence".to_string())
    }

    async fn git_bytes(
        &self,
        arguments: Vec<OsString>,
        cancellation: &mut oneshot::Receiver<()>,
    ) -> Result<Vec<u8>, String> {
        let output = self
            .git(arguments, cancellation)
            .await
            .map_err(|failure| failure.message)?;
        if !output.status.success() {
            return Err(git_refusal(output.status, "Git inspection failed"));
        }
        Ok(output.stdout)
    }

    async fn git(
        &self,
        arguments: Vec<OsString>,
        cancellation: &mut oneshot::Receiver<()>,
    ) -> Result<super::process_supervision::ProcessRunOutput, GitFailure> {
        let command_id = format!("{}-{}", self.operation_identity, uuid::Uuid::new_v4());
        let paths = &index_lease(&self.leases)
            .ok_or_else(|| {
                GitFailure::refused("Git mutation lease does not cover the worktree index")
            })?
            .paths;
        append_audit(
            &self.event_log,
            "git.process.requested",
            &self.agent_id,
            serde_json::json!({
                "schema_version": 1,
                "operation_id_sha256": digest(&command_id),
                "agent_id": self.agent_id,
                "birth_branch": self.birth_branch,
                "worktree_root": paths.worktree_root,
                "git_dir": paths.git_dir,
                "common_git_dir": paths.common_git_dir,
                "index_file": paths.index_file,
                "operation_kind": "scope_handoff_git"
            }),
        )
        .map_err(GitFailure::refused)?;
        let lease = index_lease(&self.leases).ok_or_else(|| {
            GitFailure::refused("Git mutation lease does not cover the worktree index")
        })?;
        let audit = GitProcessAudit {
            lease,
            event_log: &self.event_log,
            operation_id: &command_id,
            agent_id: &self.agent_id,
            birth_branch: &self.birth_branch,
        };
        let mut command = Command::new("git");
        command
            .args(arguments)
            .current_dir(&self.worktree)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .env_remove("GIT_COMMON_DIR")
            .env_remove("GIT_INDEX_FILE")
            .env_remove("GIT_OBJECT_DIRECTORY")
            .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES")
            .kill_on_drop(true);
        let result = run_supervised(
            &mut command,
            Some(GIT_TIMEOUT),
            &command_id,
            Some(audit),
            cancellation,
        )
        .await;
        match result {
            Ok(output) => {
                append_audit(
                    &self.event_log,
                    "git.process.finished",
                    &self.agent_id,
                    serde_json::json!({
                        "schema_version": 1,
                        "operation_id_sha256": digest(&command_id),
                        "outcome": "exited",
                        "process_id": output.pid,
                        "process_start_identity": output.start_identity,
                        "process_group_id": output.process_group_id,
                        "exit_code": output.status.code(),
                    }),
                )
                .map_err(|error| {
                    if output.status.success() {
                        GitFailure::unknown(error)
                    } else {
                        GitFailure::refused(error)
                    }
                })?;
                Ok(output)
            }
            Err(error) => {
                let outcome = process_outcome(&error);
                append_audit(
                    &self.event_log,
                    "git.process.finished",
                    &self.agent_id,
                    serde_json::json!({
                        "schema_version": 1,
                        "operation_id_sha256": digest(&command_id),
                        "outcome": outcome,
                        "exit_code": serde_json::Value::Null,
                    }),
                )
                .map_err(GitFailure::unknown)?;
                let failure_outcome = process_failure_outcome(&error);
                let failure = process_error(error);
                match failure_outcome {
                    GitFailureOutcome::Refused => Err(GitFailure::refused(failure)),
                    GitFailureOutcome::Unknown => Err(GitFailure::unknown(failure)),
                }
            }
        }
    }
}

async fn execute_handoff(
    mut context: HandoffContext,
    request: PrepareScopeHandoffRequest,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<PrepareScopeHandoffResponse, String> {
    match execute_handoff_body(&mut context, &request, cancellation).await {
        Ok(response) => Ok(response),
        Err(error) => {
            if let Ok(branch) = context
                .git_text(&["branch", "--show-current"], cancellation)
                .await
            {
                context.branch = branch;
            }
            if let Ok(head) = context
                .git_text(&["rev-parse", "--verify", "HEAD"], cancellation)
                .await
            {
                context.head = head;
            }
            let snapshot = worktree_fingerprint(&context, cancellation)
                .await
                .unwrap_or_default();
            let changed_paths = changed_path_hex(&context, cancellation)
                .await
                .unwrap_or_default();
            let mut response = response(
                &request,
                &context,
                "blocked",
                "",
                &snapshot,
                changed_paths,
                false,
            );
            response.error = error;
            Ok(response)
        }
    }
}

async fn execute_handoff_body(
    context: &mut HandoffContext,
    request: &PrepareScopeHandoffRequest,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<PrepareScopeHandoffResponse, String> {
    if context.branch != request.expected_branch {
        return Err(
            "scope handoff is blocked because the caller worktree is on a different branch"
                .to_string(),
        );
    }
    validate_branch(context, &request.expected_branch, cancellation).await?;
    reject_unfinished_git_operation(context, cancellation).await?;
    let unmerged = context
        .git_bytes(
            vec!["ls-files".into(), "--unmerged".into(), "-z".into()],
            cancellation,
        )
        .await?;
    if !unmerged.is_empty() {
        return Err("scope handoff is blocked by unresolved Git conflicts".to_string());
    }

    let snapshot_paths = current_worktree_paths(context, cancellation).await?;
    let snapshot = fingerprint_paths(context, &snapshot_paths)?;
    let changed_paths = changed_path_hex(context, cancellation).await?;
    if !request.apply
        && !request.expected_head_sha.is_empty()
        && request.expected_head_sha != context.head
    {
        return Err("scope handoff source HEAD changed after inspection".to_string());
    }
    if request.apply && request.expected_source_fingerprint.is_empty() {
        return Err("apply requires the fingerprint returned by a prior inspection".to_string());
    }
    if request.apply && request.expected_head_sha.is_empty() {
        return Err("apply requires the HEAD returned by a prior inspection".to_string());
    }

    let identity_head = if request.expected_head_sha.is_empty() {
        context.head.as_str()
    } else {
        request.expected_head_sha.as_str()
    };
    let identity_fingerprint = if request.expected_source_fingerprint.is_empty() {
        snapshot.as_str()
    } else {
        request.expected_source_fingerprint.as_str()
    };
    let handoff_identity = handoff_identity(
        request,
        &context.worktree,
        &context.branch,
        identity_head,
        identity_fingerprint,
    );
    if request.apply {
        if let Some(commit) = find_handoff_commit(context, &handoff_identity, cancellation).await? {
            return adopt_existing_commit(context, request, changed_paths, commit);
        }
        if request.expected_head_sha != context.head {
            return Err("scope handoff source HEAD changed and no matching committed handoff can be recovered".to_string());
        }
    }
    if !request.expected_source_fingerprint.is_empty()
        && request.expected_source_fingerprint != snapshot
    {
        return Err("scope handoff worktree changed after inspection".to_string());
    }

    let clean = changed_paths.is_empty();
    if !request.apply || clean {
        if request.apply {
            append_handoff_event(
                context,
                "tl.scope_handoff.no_changes",
                request,
                &handoff_identity,
                &snapshot,
                serde_json::json!({"changed_paths": changed_paths}),
            )?;
        }
        return Ok(response(
            request,
            context,
            if clean { "no_changes" } else { "ready" },
            "",
            &snapshot,
            changed_paths,
            true,
        ));
    }

    require_commit_identity(context, cancellation).await?;

    append_handoff_event(
        context,
        "tl.scope_handoff.intent",
        request,
        &handoff_identity,
        &snapshot,
        serde_json::json!({"outcome": "intent", "changed_paths": changed_paths}),
    )?;
    run_required_git(
        context,
        vec!["add".into(), "-A".into()],
        cancellation,
        "Git staging was refused; worktree content was preserved",
    )
    .await?;
    let after_staging =
        fingerprint_with_current_paths(context, &snapshot_paths, cancellation).await?;
    if after_staging != snapshot {
        append_handoff_event(
            context,
            "tl.scope_handoff.blocked",
            request,
            &handoff_identity,
            &snapshot,
            serde_json::json!({"outcome": "staging_snapshot_changed", "observed_fingerprint": after_staging}),
        )?;
        return Err(format!(
            "worktree content changed while staging (expected {snapshot}, observed {after_staging}); inspect and retry with a new handoff snapshot"
        ));
    }
    let staged = context
        .git(
            vec![
                "diff".into(),
                "--cached".into(),
                "--quiet".into(),
                "HEAD".into(),
            ],
            cancellation,
        )
        .await
        .map_err(|failure| failure.message)?;
    if staged.status.success() {
        append_handoff_event(
            context,
            "tl.scope_handoff.no_changes",
            request,
            &handoff_identity,
            &snapshot,
            serde_json::json!({"outcome": "no_changes_after_staging"}),
        )?;
        return Ok(response(
            request,
            context,
            "no_changes",
            "",
            &snapshot,
            Vec::new(),
            true,
        ));
    }
    if staged.status.code() != Some(1) {
        return Err(git_refusal(
            staged.status,
            "staged worktree could not be verified",
        ));
    }

    let head_before_commit = context
        .git_text(&["rev-parse", "--verify", "HEAD"], cancellation)
        .await?;
    let branch_before_commit = context
        .git_text(&["branch", "--show-current"], cancellation)
        .await?;
    if head_before_commit != request.expected_head_sha
        || branch_before_commit != request.expected_branch
    {
        return Err(
            "source branch or HEAD changed during staging; no handoff commit was created"
                .to_string(),
        );
    }
    let expected_tree = context.git_text(&["write-tree"], cancellation).await?;

    let message = commit_message(
        &handoff_identity,
        &snapshot,
        &context.head,
        &context.branch,
        &expected_tree,
    );
    let commit_result = match context.git(message, cancellation).await {
        Ok(output) => output,
        Err(failure) => {
            let outcome = match failure.outcome {
                GitFailureOutcome::Refused => "commit_refused",
                GitFailureOutcome::Unknown => "unknown",
            };
            append_handoff_event(
                context,
                if outcome == "unknown" {
                    "tl.scope_handoff.unknown"
                } else {
                    "tl.scope_handoff.commit_refused"
                },
                request,
                &handoff_identity,
                &snapshot,
                serde_json::json!({"outcome": outcome}),
            )?;
            if outcome == "unknown" {
                if let Ok(branch) = context
                    .git_text(&["branch", "--show-current"], cancellation)
                    .await
                {
                    context.branch = branch;
                }
                if let Ok(head) = context
                    .git_text(&["rev-parse", "--verify", "HEAD"], cancellation)
                    .await
                {
                    context.head = head;
                }
            }
            let mut response = response(
                request,
                context,
                outcome,
                "",
                &snapshot,
                changed_paths,
                false,
            );
            response.error = failure.message;
            return Ok(response);
        }
    };
    if !commit_result.status.success() {
        append_handoff_event(
            context,
            "tl.scope_handoff.commit_refused",
            request,
            &handoff_identity,
            &snapshot,
            serde_json::json!({"outcome": "commit_refused"}),
        )?;
        let mut response = response(
            request,
            context,
            "commit_refused",
            "",
            &snapshot,
            changed_paths,
            false,
        );
        response.error = git_refusal(
            commit_result.status,
            "Git commit was refused; staged and working files remain available",
        );
        return Ok(response);
    }
    let head_after = context
        .git_text(&["rev-parse", "--verify", "HEAD"], cancellation)
        .await?;
    let branch_after = context
        .git_text(&["branch", "--show-current"], cancellation)
        .await?;
    context.head = head_after.clone();
    context.branch = branch_after.clone();
    if branch_after != request.expected_branch
        || !verify_handoff_commit(
            context,
            &head_after,
            &request.expected_head_sha,
            &handoff_identity,
            &snapshot,
            &expected_tree,
            cancellation,
        )
        .await?
    {
        append_handoff_event(
            context,
            "tl.scope_handoff.unknown",
            request,
            &handoff_identity,
            &snapshot,
            serde_json::json!({"outcome": "commit_evidence_unverified", "head_observed": head_after, "tree_expected": expected_tree}),
        )?;
        let mut response = response(
            request,
            context,
            "unknown",
            "",
            &snapshot,
            changed_path_hex(context, cancellation)
                .await
                .unwrap_or_default(),
            false,
        );
        response.error = "Git commit may have completed, but its handoff evidence could not be verified; retry the same handoff snapshot".to_string();
        return Ok(response);
    }
    let changed_after_commit = changed_path_hex(context, cancellation).await?;
    if !changed_after_commit.is_empty() {
        append_handoff_event(
            context,
            "tl.scope_handoff.committed_with_new_changes",
            request,
            &handoff_identity,
            &snapshot,
            serde_json::json!({"outcome": "committed_with_new_changes", "commit_sha": head_after, "head_after": head_after, "tree_sha": expected_tree, "changed_paths": changed_after_commit}),
        )?;
        return Ok(response(
            request,
            context,
            "committed_with_new_changes",
            &head_after,
            &snapshot,
            changed_after_commit,
            false,
        ));
    }
    append_handoff_event(
        context,
        "tl.scope_handoff.committed",
        request,
        &handoff_identity,
        &snapshot,
        serde_json::json!({"outcome": "committed", "commit_sha": head_after, "head_after": head_after, "tree_sha": expected_tree, "changed_paths": changed_paths}),
    )?;
    Ok(response(
        request,
        context,
        "committed",
        &head_after,
        &snapshot,
        Vec::new(),
        true,
    ))
}

fn adopt_existing_commit(
    context: &mut HandoffContext,
    request: &PrepareScopeHandoffRequest,
    changed_paths: Vec<String>,
    commit: ExistingHandoffCommit,
) -> Result<PrepareScopeHandoffResponse, String> {
    if commit.parent != request.expected_head_sha
        || commit.fingerprint != request.expected_source_fingerprint
    {
        return Err(
            "handoff identity conflicts with retained commit ancestry or snapshot evidence"
                .to_string(),
        );
    }
    if context.head != commit.sha || !changed_paths.is_empty() {
        return Err(
            "matching handoff commit exists but the source worktree has advanced or changed"
                .to_string(),
        );
    }
    append_handoff_event(
        context,
        "tl.scope_handoff.adopted",
        request,
        &commit.identity,
        &commit.fingerprint,
        serde_json::json!({"outcome": "adopted", "commit_sha": commit.sha, "head_after": commit.sha, "changed_paths": changed_paths}),
    )?;
    context.head = commit.sha.clone();
    Ok(response(
        request,
        context,
        "recovered_commit",
        &commit.sha,
        &commit.fingerprint,
        Vec::new(),
        true,
    ))
}

async fn validate_branch(
    context: &HandoffContext,
    expected_branch: &str,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<(), String> {
    let checked = context
        .git_text(
            &["check-ref-format", "--branch", expected_branch],
            cancellation,
        )
        .await?;
    if checked != expected_branch {
        return Err("requested branch is not a canonical Git branch name".to_string());
    }
    Ok(())
}

async fn reject_unfinished_git_operation(
    context: &HandoffContext,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<(), String> {
    for marker in [
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "BISECT_LOG",
    ] {
        let path = context
            .git_text(&["rev-parse", "--git-path", marker], cancellation)
            .await?;
        let path = PathBuf::from(path);
        let path = if path.is_absolute() {
            path
        } else {
            context.worktree.join(path)
        };
        if fs::symlink_metadata(path).is_ok() {
            return Err("scope handoff is blocked by an unfinished Git operation".to_string());
        }
    }
    Ok(())
}

async fn worktree_fingerprint(
    context: &HandoffContext,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<String, String> {
    let paths = current_worktree_paths(context, cancellation).await?;
    fingerprint_paths(context, &paths)
}

async fn current_worktree_paths(
    context: &HandoffContext,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<BTreeSet<Vec<u8>>, String> {
    let tracked = context
        .git_bytes(vec!["ls-files".into(), "-z".into()], cancellation)
        .await?;
    let committed = context
        .git_bytes(
            vec![
                "ls-tree".into(),
                "-r".into(),
                "-z".into(),
                "--name-only".into(),
                "HEAD".into(),
            ],
            cancellation,
        )
        .await?;
    let untracked = context
        .git_bytes(
            vec![
                "ls-files".into(),
                "--others".into(),
                "--exclude-standard".into(),
                "-z".into(),
            ],
            cancellation,
        )
        .await?;
    let mut paths = path_set(&tracked)?;
    paths.extend(path_set(&committed)?);
    paths.extend(path_set(&untracked)?);
    Ok(paths)
}

async fn fingerprint_with_current_paths(
    context: &HandoffContext,
    original_paths: &BTreeSet<Vec<u8>>,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<String, String> {
    let mut paths = current_worktree_paths(context, cancellation).await?;
    paths.extend(original_paths.iter().cloned());
    fingerprint_paths(context, &paths)
}

fn fingerprint_paths(
    context: &HandoffContext,
    paths: &BTreeSet<Vec<u8>>,
) -> Result<String, String> {
    let mut digest = Sha256::new();
    for path in paths {
        let relative = PathBuf::from(path_to_os(path)?);
        if relative.is_absolute()
            || relative
                .components()
                .any(|part| part == std::path::Component::ParentDir)
        {
            return Err("Git reported an unsafe worktree path".to_string());
        }
        hash_worktree_path(
            &mut digest,
            &context.worktree,
            &context.worktree.join(relative),
            path,
        )?;
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn hash_worktree_path(
    digest: &mut Sha256,
    root: &Path,
    full_path: &Path,
    relative: &[u8],
) -> Result<(), String> {
    digest.update((relative.len() as u64).to_be_bytes());
    digest.update(relative);
    reject_symlinked_parent(root, relative)?;
    match fs::symlink_metadata(full_path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => digest.update([0]),
        Err(_) => return Err("worktree path could not be inspected safely".to_string()),
        Ok(metadata) if metadata.file_type().is_symlink() => {
            digest.update([1]);
            let target = fs::read_link(full_path)
                .map_err(|_| "worktree symlink could not be read".to_string())?;
            digest.update(os_bytes(target.as_os_str()));
        }
        Ok(metadata) if metadata.is_file() => {
            digest.update([2]);
            digest.update([u8::from(is_executable(&metadata))]);
            let mut file = File::open(full_path)
                .map_err(|_| "worktree file could not be read safely".to_string())?;
            let mut buffer = [0u8; 16 * 1024];
            loop {
                let read = file
                    .read(&mut buffer)
                    .map_err(|_| "worktree file could not be fingerprinted".to_string())?;
                if read == 0 {
                    break;
                }
                digest.update(&buffer[..read]);
            }
        }
        Ok(_) => {
            return Err(
                "nested repositories and special files must be resolved before handoff".to_string(),
            )
        }
    }
    Ok(())
}

fn reject_symlinked_parent(root: &Path, relative: &[u8]) -> Result<(), String> {
    let relative = PathBuf::from(path_to_os(relative)?);
    let mut current = root.to_path_buf();
    let mut components = relative.components().peekable();
    while let Some(component) = components.next() {
        let std::path::Component::Normal(name) = component else {
            return Err("Git reported an unsafe worktree path".to_string());
        };
        if components.peek().is_none() {
            break;
        }
        current.push(name);
        match fs::symlink_metadata(&current) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err("worktree parent path could not be inspected safely".to_string()),
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err("worktree path has a symlinked parent".to_string());
            }
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => return Err("worktree parent is not a directory".to_string()),
        }
    }
    Ok(())
}

async fn changed_path_hex(
    context: &HandoffContext,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<Vec<String>, String> {
    let changed = context
        .git_bytes(
            vec![
                "diff".into(),
                "--name-only".into(),
                "--no-renames".into(),
                "-z".into(),
                "HEAD".into(),
            ],
            cancellation,
        )
        .await?;
    let untracked = context
        .git_bytes(
            vec![
                "ls-files".into(),
                "--others".into(),
                "--exclude-standard".into(),
                "-z".into(),
            ],
            cancellation,
        )
        .await?;
    let mut paths = path_set(&changed)?;
    paths.extend(path_set(&untracked)?);
    Ok(paths.into_iter().map(|path| hex(&path)).collect())
}

fn path_set(bytes: &[u8]) -> Result<BTreeSet<Vec<u8>>, String> {
    if bytes.last().is_some_and(|last| *last != 0) {
        return Err("Git returned malformed path evidence".to_string());
    }
    Ok(bytes
        .split(|byte| *byte == 0)
        .filter(|path| !path.is_empty())
        .map(Vec::from)
        .collect())
}

fn path_to_os(bytes: &[u8]) -> Result<OsString, String> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        Ok(OsString::from_vec(bytes.to_vec()))
    }
    #[cfg(not(unix))]
    {
        String::from_utf8(bytes.to_vec())
            .map(OsString::from)
            .map_err(|_| "Git returned a path not representable on this platform".to_string())
    }
}

#[derive(Debug)]
struct ExistingHandoffCommit {
    sha: String,
    parent: String,
    identity: String,
    fingerprint: String,
}

async fn find_handoff_commit(
    context: &HandoffContext,
    identity: &str,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<Option<ExistingHandoffCommit>, String> {
    let output = context
        .git_bytes(
            vec![
                "log".into(),
                "--format=%H%x00%P%x00%B%x00".into(),
                "HEAD".into(),
            ],
            cancellation,
        )
        .await?;
    let fields: Vec<&[u8]> = output.split(|byte| *byte == 0).collect();
    for record in fields.chunks_exact(3) {
        let sha = String::from_utf8_lossy(record[0]).trim().to_string();
        let parent = String::from_utf8_lossy(record[1]).trim().to_string();
        let body = String::from_utf8_lossy(record[2]);
        let saved_identity = trailer(&body, "Exomonad-Handoff-Identity");
        if saved_identity.as_deref() != Some(identity) {
            continue;
        }
        let tree = context
            .git_text(
                &["rev-parse", "--verify", &format!("{}^{{tree}}", sha)],
                cancellation,
            )
            .await?;
        let saved_tree = trailer(&body, "Exomonad-Handoff-Tree").unwrap_or_default();
        if saved_tree != tree {
            return Err(
                "retained handoff commit tree does not match its audit trailer".to_string(),
            );
        }
        return Ok(Some(ExistingHandoffCommit {
            sha,
            parent,
            identity: identity.to_string(),
            fingerprint: trailer(&body, "Exomonad-Handoff-Fingerprint").unwrap_or_default(),
        }));
    }
    Ok(None)
}

async fn verify_handoff_commit(
    context: &HandoffContext,
    commit: &str,
    expected_parent: &str,
    identity: &str,
    fingerprint: &str,
    expected_tree: &str,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<bool, String> {
    let parent = context
        .git_text(
            &["rev-parse", "--verify", &format!("{commit}^")],
            cancellation,
        )
        .await?;
    let body = context
        .git_text(&["show", "-s", "--format=%B", commit], cancellation)
        .await?;
    let tree = context
        .git_text(
            &["rev-parse", "--verify", &format!("{commit}^{{tree}}")],
            cancellation,
        )
        .await?;
    Ok(parent == expected_parent
        && tree == expected_tree
        && trailer(&body, "Exomonad-Handoff-Identity").as_deref() == Some(identity)
        && trailer(&body, "Exomonad-Handoff-Fingerprint").as_deref() == Some(fingerprint)
        && trailer(&body, "Exomonad-Handoff-Tree").as_deref() == Some(expected_tree))
}

fn commit_message(
    identity: &str,
    fingerprint: &str,
    parent: &str,
    branch: &str,
    tree: &str,
) -> Vec<OsString> {
    let branch_digest = digest(branch);
    [
        OsString::from("commit"),
        OsString::from("-m"),
        OsString::from(COMMIT_SUBJECT),
        OsString::from("-m"),
        OsString::from(format!("Exomonad-Handoff-Identity: {identity}")),
        OsString::from("-m"),
        OsString::from(format!("Exomonad-Handoff-Fingerprint: {fingerprint}")),
        OsString::from("-m"),
        OsString::from(format!("Exomonad-Handoff-Parent: {parent}")),
        OsString::from("-m"),
        OsString::from(format!("Exomonad-Handoff-Branch-SHA256: {branch_digest}")),
        OsString::from("-m"),
        OsString::from(format!("Exomonad-Handoff-Tree: {tree}")),
    ]
    .into()
}

fn append_handoff_event(
    context: &HandoffContext,
    event_type: &str,
    request: &PrepareScopeHandoffRequest,
    identity: &str,
    fingerprint: &str,
    details: serde_json::Value,
) -> Result<(), String> {
    let mut event = serde_json::json!({
        "schema_version": 1,
        "handoff_identity": identity,
        "handoff_id": request.handoff_id,
        "controller_id": request.controller_id,
        "parent_run_id": request.parent_run_id,
        "parent_scope_id": request.parent_scope_id,
        "child_scope_id": request.child_scope_id,
        "source_worktree": context.worktree,
        "branch": context.branch,
        "head_before": request.expected_head_sha,
        "source_fingerprint": fingerprint,
    });
    if let (Some(event), Some(details)) = (event.as_object_mut(), details.as_object()) {
        event.extend(
            details
                .iter()
                .map(|(key, value)| (key.clone(), value.clone())),
        );
    }
    append_audit(&context.event_log, event_type, &context.agent_id, event)
}

fn append_audit(
    log: &EventLog,
    event_type: &str,
    actor: &str,
    data: serde_json::Value,
) -> Result<(), String> {
    log.append(event_type, actor, &data)
        .map(|_| ())
        .map_err(|_| "scope handoff audit could not be persisted".to_string())
}

fn response(
    request: &PrepareScopeHandoffRequest,
    context: &HandoffContext,
    outcome: &str,
    commit_sha: &str,
    fingerprint: &str,
    changed_paths: Vec<String>,
    ready: bool,
) -> PrepareScopeHandoffResponse {
    PrepareScopeHandoffResponse {
        success: ready,
        error: String::new(),
        outcome: outcome.to_string(),
        handoff_id: request.handoff_id.clone(),
        controller_id: request.controller_id.clone(),
        parent_run_id: request.parent_run_id.clone(),
        parent_scope_id: request.parent_scope_id.clone(),
        child_scope_id: request.child_scope_id.clone(),
        source_worktree: context.worktree.to_string_lossy().to_string(),
        branch: context.branch.clone(),
        head_before: if request.expected_head_sha.is_empty() {
            context.head.clone()
        } else {
            request.expected_head_sha.clone()
        },
        head_after: if commit_sha.is_empty() {
            context.head.clone()
        } else {
            commit_sha.to_string()
        },
        commit_sha: commit_sha.to_string(),
        source_fingerprint: fingerprint.to_string(),
        changed_paths,
        ready,
    }
}

fn blocked_response(
    request: &PrepareScopeHandoffRequest,
    error: String,
) -> PrepareScopeHandoffResponse {
    PrepareScopeHandoffResponse {
        success: false,
        error,
        outcome: "blocked".to_string(),
        handoff_id: request.handoff_id.clone(),
        controller_id: request.controller_id.clone(),
        parent_run_id: request.parent_run_id.clone(),
        parent_scope_id: request.parent_scope_id.clone(),
        child_scope_id: request.child_scope_id.clone(),
        source_worktree: String::new(),
        branch: String::new(),
        head_before: request.expected_head_sha.clone(),
        head_after: String::new(),
        commit_sha: String::new(),
        source_fingerprint: String::new(),
        changed_paths: Vec::new(),
        ready: false,
    }
}

fn validate_request(
    request: &PrepareScopeHandoffRequest,
    authenticated_controller: &str,
) -> EffectResult<()> {
    if request.controller_id != authenticated_controller {
        return Err(EffectError::permission_denied(
            "scope handoff controller does not match the authenticated caller",
        ));
    }
    for (label, value) in [
        ("handoff_id", request.handoff_id.as_str()),
        ("parent_run_id", request.parent_run_id.as_str()),
        ("parent_scope_id", request.parent_scope_id.as_str()),
        ("child_scope_id", request.child_scope_id.as_str()),
        ("expected_branch", request.expected_branch.as_str()),
    ] {
        if !valid_identity(value) {
            return Err(EffectError::invalid_input(format!(
                "{label} is missing or malformed"
            )));
        }
    }
    if !request.expected_head_sha.is_empty() && !valid_object_id(&request.expected_head_sha) {
        return Err(EffectError::invalid_input("expected_head_sha is malformed"));
    }
    if !request.expected_source_fingerprint.is_empty()
        && !is_sha256(&request.expected_source_fingerprint)
    {
        return Err(EffectError::invalid_input(
            "expected_source_fingerprint is malformed",
        ));
    }
    Ok(())
}

fn valid_identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && !value.starts_with('-')
        && value.bytes().all(|byte| byte.is_ascii_graphic())
}

fn valid_object_id(value: &str) -> bool {
    matches!(value.len(), 40 | 64) && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn operation_identity(request: &PrepareScopeHandoffRequest, source: &Path) -> String {
    let source = source.to_string_lossy();
    let mut hasher = Sha256::new();
    for value in [
        request.handoff_id.as_bytes(),
        request.controller_id.as_bytes(),
        request.parent_run_id.as_bytes(),
        request.parent_scope_id.as_bytes(),
        request.child_scope_id.as_bytes(),
        source.as_bytes(),
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    format!("scope-handoff-{:x}", hasher.finalize())
}

fn canonical_source_worktree(
    project_dir: &Path,
    source_worktree: &Path,
) -> Result<PathBuf, String> {
    let project_dir = fs::canonicalize(project_dir)
        .map_err(|_| "configured repository root could not be resolved".to_string())?;
    let requested = if source_worktree.is_absolute() {
        source_worktree.to_path_buf()
    } else {
        project_dir.join(source_worktree)
    };
    let canonical = fs::canonicalize(requested)
        .map_err(|_| "authenticated caller worktree could not be resolved".to_string())?;
    if canonical.to_str().is_none() {
        return Err(
            "authenticated caller worktree path is not representable in the handoff receipt"
                .to_string(),
        );
    }
    Ok(canonical)
}

fn handoff_identity(
    request: &PrepareScopeHandoffRequest,
    worktree: &Path,
    branch: &str,
    head: &str,
    fingerprint: &str,
) -> String {
    let mut hasher = Sha256::new();
    for value in [
        request.handoff_id.as_bytes(),
        request.controller_id.as_bytes(),
        request.parent_run_id.as_bytes(),
        request.parent_scope_id.as_bytes(),
        request.child_scope_id.as_bytes(),
        worktree.to_string_lossy().as_bytes(),
        branch.as_bytes(),
        head.as_bytes(),
        fingerprint.as_bytes(),
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    format!("{:x}", hasher.finalize())
}

fn trailer(body: &str, name: &str) -> Option<String> {
    body.lines()
        .find_map(|line| line.strip_prefix(&format!("{name}: ")).map(str::to_string))
}

fn git_refusal(status: ExitStatus, message: &str) -> String {
    match status.code() {
        Some(code) => format!("{message} (exit code {code})"),
        None => format!("{message} (process status unavailable)"),
    }
}

fn process_error(error: ProcessRunError) -> String {
    match error {
        ProcessRunError::Timeout => "managed Git command timed out; inspect the worktree and retry the same handoff snapshot".to_string(),
        ProcessRunError::Cancelled { .. } => "managed Git command was cancelled and its process group was reaped".to_string(),
        ProcessRunError::Cleanup(_) => "managed Git process cleanup is unresolved; no handoff readiness was recorded".to_string(),
        ProcessRunError::Audit => "managed Git process audit could not be persisted".to_string(),
        ProcessRunError::Spawn(_) | ProcessRunError::Wait(_) | ProcessRunError::Output(_) => "managed Git command could not be completed safely".to_string(),
    }
}

fn process_outcome(error: &ProcessRunError) -> &'static str {
    match error {
        ProcessRunError::Timeout => "timeout",
        ProcessRunError::Cancelled { .. } => "cancelled",
        ProcessRunError::Cleanup(_) => "cleanup_unresolved",
        ProcessRunError::Audit => "audit_unavailable",
        ProcessRunError::Spawn(_) => "spawn_failed",
        ProcessRunError::Wait(_) => "wait_failed",
        ProcessRunError::Output(_) => "output_failed",
    }
}

fn process_failure_outcome(error: &ProcessRunError) -> GitFailureOutcome {
    match error {
        ProcessRunError::Spawn(_) => GitFailureOutcome::Refused,
        ProcessRunError::Wait(_)
        | ProcessRunError::Output(_)
        | ProcessRunError::Timeout
        | ProcessRunError::Cancelled { .. }
        | ProcessRunError::Cleanup(_)
        | ProcessRunError::Audit => GitFailureOutcome::Unknown,
    }
}

async fn run_required_git(
    context: &HandoffContext,
    arguments: Vec<OsString>,
    cancellation: &mut oneshot::Receiver<()>,
    refusal: &str,
) -> Result<(), String> {
    let output = context
        .git(arguments, cancellation)
        .await
        .map_err(|failure| failure.message)?;
    if output.status.success() {
        Ok(())
    } else {
        Err(git_refusal(output.status, refusal))
    }
}

async fn require_commit_identity(
    context: &HandoffContext,
    cancellation: &mut oneshot::Receiver<()>,
) -> Result<(), String> {
    for variable in ["GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"] {
        let output = context
            .git(vec!["var".into(), variable.into()], cancellation)
            .await
            .map_err(|failure| failure.message)?;
        if !output.status.success() {
            return Err(
                "Git author or committer identity is unavailable; configure user.name and user.email before handoff"
                    .to_string(),
            );
        }
    }
    Ok(())
}

fn index_lease(set: &GitMutationLeaseSet) -> Option<&GitMutationLease> {
    set.leases()
        .iter()
        .find(|lease| lease.resource == GitMutationResource::Index)
}

fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn os_bytes(value: &OsStr) -> Vec<u8> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        value.as_bytes().to_vec()
    }
    #[cfg(not(unix))]
    {
        value.to_string_lossy().as_bytes().to_vec()
    }
}

#[cfg(unix)]
fn is_executable(metadata: &fs::Metadata) -> bool {
    use std::os::unix::fs::PermissionsExt;
    metadata.permissions().mode() & 0o111 != 0
}

#[cfg(not(unix))]
fn is_executable(_metadata: &fs::Metadata) -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use tempfile::TempDir;

    #[tokio::test]
    async fn commit_all_captures_worktree_changes_and_replay_adopts_same_commit() {
        let repository = TestRepository::new();
        let base = repository.head();
        repository.write(".gitignore", ".exo/\n*.secret\n");
        repository.write("kept.secret", "tracked but now ignored\n");
        repository.write("tracked.txt", "pre-staged\n");
        repository.git(&["add", "tracked.txt"]);
        repository.write("tracked.txt", "final tracked content\n");
        repository.remove("delete.txt");
        repository.write("new.txt", "new content\n");
        repository.write("private.secret", "must stay ignored\n");

        let inspected = run(&repository, request(false, "", "")).await;
        assert!(inspected.ready, "{}", inspected.error);
        assert!(!inspected.changed_paths.is_empty());
        let handoff = request(true, &inspected.head_after, &inspected.source_fingerprint);
        let committed = run(&repository, handoff.clone()).await;
        assert!(committed.ready, "{}", committed.error);
        assert_eq!(committed.outcome, "committed");
        assert_ne!(committed.commit_sha, base);
        assert_eq!(repository.head(), committed.commit_sha);
        let paths = repository.git(&["show", "--pretty=", "--name-only", "HEAD"]);
        assert!(paths.contains("tracked.txt"));
        assert!(paths.contains("delete.txt"));
        assert!(paths.contains("new.txt"));
        assert!(paths.contains("kept.secret"));
        assert!(!paths.contains("private.secret"));
        assert_eq!(
            repository.git(&["show", "HEAD:tracked.txt"]),
            "final tracked content"
        );

        let replayed = run(&repository, handoff).await;
        assert!(replayed.ready, "{}", replayed.error);
        assert_eq!(replayed.outcome, "recovered_commit");
        assert_eq!(replayed.commit_sha, committed.commit_sha);
        assert_eq!(repository.commit_count(), 2);
    }

    #[tokio::test]
    async fn clean_source_is_a_noop_and_scope_is_limited_to_callers_worktree() {
        let repository = TestRepository::new();
        let sibling_directory = tempfile::tempdir().unwrap();
        let sibling = sibling_directory.path().join("sibling");
        repository.git(&[
            "worktree",
            "add",
            "-b",
            "sibling",
            sibling.to_str().expect("UTF-8 test path"),
        ]);
        fs::write(sibling.join("sibling.txt"), "independent work\n").unwrap();
        let clean = run(&repository, request(false, "", "")).await;
        assert!(clean.ready, "{}", clean.error);
        assert_eq!(clean.outcome, "no_changes");
        assert_eq!(repository.commit_count(), 1);

        repository.write("root.txt", "root work\n");
        let inspected = run(&repository, request(false, "", "")).await;
        let committed = run(
            &repository,
            request(true, &inspected.head_after, &inspected.source_fingerprint),
        )
        .await;
        assert!(committed.ready, "{}", committed.error);
        assert_eq!(repository.head(), committed.commit_sha);
        assert_eq!(
            repository
                .git(&["-C", sibling.to_str().unwrap(), "status", "--porcelain"])
                .trim(),
            "?? sibling.txt"
        );
        assert_eq!(repository.commit_count(), 2);
    }

    #[tokio::test]
    async fn conflict_wrong_branch_and_commit_hook_refusal_never_report_ready() {
        let repository = TestRepository::new();
        repository.git(&["checkout", "-b", "other"]);
        let wrong_branch = run(&repository, request(false, "", "")).await;
        assert!(!wrong_branch.ready);
        assert!(wrong_branch.error.contains("different branch"));

        repository.write("tracked.txt", "dirty\n");
        let mut inspection_request = request(false, "", "");
        inspection_request.expected_branch = "other".into();
        let inspected = run(&repository, inspection_request.clone()).await;
        let hook = repository.path().join(".git/hooks/pre-commit");
        fs::write(&hook, "#!/bin/sh\nexit 1\n").unwrap();
        set_executable(&hook);
        let refused = run(
            &repository,
            request_with_branch(
                "other",
                true,
                &inspected.head_after,
                &inspected.source_fingerprint,
            ),
        )
        .await;
        assert!(!refused.ready);
        assert_eq!(refused.outcome, "commit_refused");
        assert!(refused.error.contains("refused"));
        assert_eq!(repository.head(), inspected.head_after);
        assert_eq!(
            fs::read_to_string(repository.path().join("tracked.txt")).unwrap(),
            "dirty\n"
        );
        assert_eq!(
            repository
                .git(&["diff", "--cached", "--name-only", "HEAD"])
                .trim(),
            "tracked.txt"
        );
        fs::remove_file(hook).unwrap();
        let retry = run(
            &repository,
            request_with_branch(
                "other",
                true,
                &inspected.head_after,
                &inspected.source_fingerprint,
            ),
        )
        .await;
        assert!(retry.ready, "{}", retry.error);
        assert_eq!(retry.outcome, "committed");
    }

    #[tokio::test]
    async fn missing_commit_identity_blocks_before_staging_and_preserves_the_index() {
        let repository = TestRepository::new();
        repository.write("tracked.txt", "handoff change\n");
        repository.git(&["config", "user.name", ""]);
        repository.git(&["config", "user.email", ""]);
        let inspected = run(&repository, request(false, "", "")).await;
        let result = run(
            &repository,
            request(true, &inspected.head_after, &inspected.source_fingerprint),
        )
        .await;

        assert!(!result.ready);
        assert!(result.error.contains("identity is unavailable"));
        assert_eq!(repository.commit_count(), 1);
        assert!(repository
            .git(&["diff", "--cached", "--name-only"])
            .is_empty());
        assert_eq!(
            repository.git(&["diff", "--name-only"]).trim(),
            "tracked.txt"
        );
    }

    #[tokio::test]
    async fn unfinished_merge_and_detached_head_are_held() {
        let repository = TestRepository::new();
        repository.git(&["checkout", "-b", "feature"]);
        repository.write("tracked.txt", "feature side\n");
        repository.git(&["add", "tracked.txt"]);
        repository.git(&["commit", "-m", "feature change"]);
        repository.git(&["checkout", "main"]);
        repository.write("tracked.txt", "main side\n");
        repository.git(&["add", "tracked.txt"]);
        repository.git(&["commit", "-m", "main change"]);
        let merge = Command::new("git")
            .args(["merge", "feature"])
            .current_dir(repository.path())
            .output()
            .unwrap();
        assert!(!merge.status.success());
        let before = repository.head();
        let inspected = run(&repository, request(false, "", "")).await;
        let held = run(
            &repository,
            request(true, &before, &inspected.source_fingerprint),
        )
        .await;
        assert!(!held.ready);
        assert!(
            held.error.contains("unfinished Git operation") || held.error.contains("conflicts")
        );
        assert_eq!(repository.head(), before);

        repository.git(&["merge", "--abort"]);
        repository.git(&["checkout", "--detach"]);
        let detached = run(&repository, request(false, "", "")).await;
        assert!(!detached.ready);
        assert!(detached.error.contains("different branch"));
    }

    #[test]
    fn authenticated_controller_is_required() {
        let request = request(true, &"a".repeat(40), &"b".repeat(64));
        assert!(validate_request(&request, "other-controller").is_err());
        assert!(validate_request(&request, "controller").is_ok());
    }

    #[tokio::test]
    async fn interrupted_commit_recovery_requires_matching_head_and_snapshot() {
        let repository = TestRepository::new();
        repository.write("tracked.txt", "handoff change\n");
        let inspected = run(&repository, request(false, "", "")).await;
        let original = request(true, &inspected.head_after, &inspected.source_fingerprint);
        let committed = run(&repository, original.clone()).await;
        assert!(committed.ready, "{}", committed.error);

        repository.write("tracked.txt", "later unsaved edit\n");
        let stale = run(&repository, original).await;
        assert!(!stale.ready);
        assert!(stale.error.contains("changed") || stale.error.contains("advanced"));
        assert_eq!(repository.commit_count(), 2);
    }

    #[tokio::test]
    async fn concurrent_replay_of_one_handoff_creates_only_one_commit() {
        let repository = TestRepository::new();
        repository.write("tracked.txt", "handoff change\n");
        let inspected = run(&repository, request(false, "", "")).await;
        let handoff = request(true, &inspected.head_after, &inspected.source_fingerprint);

        let (first, second) =
            tokio::join!(run(&repository, handoff.clone()), run(&repository, handoff));

        assert!(first.ready, "{}", first.error);
        assert!(second.ready, "{}", second.error);
        assert_eq!(first.commit_sha, second.commit_sha);
        assert_eq!(repository.commit_count(), 2);
        assert!(matches!(
            first.outcome.as_str(),
            "committed" | "recovered_commit"
        ));
        assert!(matches!(
            second.outcome.as_str(),
            "committed" | "recovered_commit"
        ));
    }

    #[test]
    fn interrupted_git_commit_processes_are_not_treated_as_known_refusals() {
        assert_eq!(
            process_failure_outcome(&ProcessRunError::Spawn(std::io::ErrorKind::NotFound)),
            GitFailureOutcome::Refused
        );
        assert_eq!(
            process_failure_outcome(&ProcessRunError::Timeout),
            GitFailureOutcome::Unknown
        );
        assert_eq!(
            process_failure_outcome(&ProcessRunError::Audit),
            GitFailureOutcome::Unknown
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn symlinked_parent_is_refused_without_reading_outside_the_worktree() {
        use std::os::unix::fs::symlink;

        let repository = TestRepository::new();
        repository.write("nested/tracked.txt", "inside repository\n");
        repository.git(&["add", "nested/tracked.txt"]);
        repository.git(&["commit", "-m", "add nested path"]);
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("tracked.txt"), "outside content\n").unwrap();
        fs::remove_dir_all(repository.path().join("nested")).unwrap();
        symlink(outside.path(), repository.path().join("nested")).unwrap();

        let inspected = run(&repository, request(false, "", "")).await;

        assert!(!inspected.ready);
        assert!(inspected.error.contains("symlinked parent"));
        assert_eq!(
            fs::read_to_string(outside.path().join("tracked.txt")).unwrap(),
            "outside content\n"
        );
        assert_eq!(repository.commit_count(), 2);
    }

    async fn run(
        repository: &TestRepository,
        request: PrepareScopeHandoffRequest,
    ) -> PrepareScopeHandoffResponse {
        prepare_scope_handoff(
            repository.path(),
            Path::new("."),
            "controller",
            "main",
            request,
        )
        .await
        .unwrap()
    }

    fn request(apply: bool, head: &str, fingerprint: &str) -> PrepareScopeHandoffRequest {
        request_with_branch("main", apply, head, fingerprint)
    }

    fn request_with_branch(
        branch: &str,
        apply: bool,
        head: &str,
        fingerprint: &str,
    ) -> PrepareScopeHandoffRequest {
        PrepareScopeHandoffRequest {
            apply,
            handoff_id: "ordered-handoff-1".into(),
            controller_id: "controller".into(),
            parent_run_id: "root-run".into(),
            parent_scope_id: "root".into(),
            child_scope_id: "architecture".into(),
            expected_branch: branch.into(),
            expected_head_sha: head.into(),
            expected_source_fingerprint: fingerprint.into(),
        }
    }

    struct TestRepository(TempDir);

    impl TestRepository {
        fn new() -> Self {
            let directory = tempfile::tempdir().unwrap();
            let repository = Self(directory);
            repository.git(&["init", "-b", "main"]);
            repository.git(&["config", "user.name", "Scope Handoff Test"]);
            repository.git(&["config", "user.email", "scope-handoff@example.invalid"]);
            repository.write(".gitignore", ".exo/\n");
            repository.write("tracked.txt", "initial\n");
            repository.write("delete.txt", "remove me\n");
            repository.write("kept.secret", "initial tracked secret\n");
            repository.git(&["add", "-A"]);
            repository.git(&["commit", "-m", "initial"]);
            repository
        }

        fn path(&self) -> &Path {
            self.0.path()
        }

        fn write(&self, relative: &str, content: &str) {
            let path = self.path().join(relative);
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent).unwrap();
            }
            fs::write(path, content).unwrap();
        }

        fn remove(&self, relative: &str) {
            fs::remove_file(self.path().join(relative)).unwrap();
        }

        fn git(&self, arguments: &[&str]) -> String {
            let output = Command::new("git")
                .args(arguments)
                .current_dir(self.path())
                .env_remove("GIT_DIR")
                .env_remove("GIT_WORK_TREE")
                .env_remove("GIT_COMMON_DIR")
                .env_remove("GIT_INDEX_FILE")
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "git {:?}: {}",
                arguments,
                String::from_utf8_lossy(&output.stderr)
            );
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        }

        fn head(&self) -> String {
            self.git(&["rev-parse", "HEAD"])
        }

        fn commit_count(&self) -> usize {
            self.git(&["rev-list", "--count", "HEAD"]).parse().unwrap()
        }
    }

    #[cfg(unix)]
    fn set_executable(path: &Path) {
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(path, permissions).unwrap();
    }

    #[cfg(not(unix))]
    fn set_executable(_path: &Path) {}
}
