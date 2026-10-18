//! Mutation leases and durable audit records for direct Git process effects.

use super::process_supervision::{GitProcessAudit, ProcessRunOutput};
use crate::effects::{EffectError, EffectResult};
use crate::services::{
    EventLog, GitMutationError, GitMutationLease, GitMutationLeaseSet, GitMutationResource,
    GitMutationSafety,
};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::Path;
use std::time::Duration;

const GIT_PROCESS_LEASE_WAIT: Duration = Duration::from_secs(2);

pub(crate) struct GitProcessContext {
    leases: GitMutationLeaseSet,
    event_log: EventLog,
    operation_id: String,
    agent_id: String,
    birth_branch: String,
}

impl GitProcessContext {
    pub(crate) async fn prepare(
        project_dir: &Path,
        worktree: &Path,
        operation_id: &str,
        agent_id: &str,
        birth_branch: &str,
    ) -> EffectResult<Self> {
        let leases = acquire_git_leases(project_dir, worktree, operation_id).await?;
        let event_log = open_event_log(project_dir)?;
        let context = Self {
            leases,
            event_log,
            operation_id: operation_id.to_string(),
            agent_id: agent_id.to_string(),
            birth_branch: birth_branch.to_string(),
        };
        context.record_requested()?;
        Ok(context)
    }

    fn index_lease(&self) -> EffectResult<&GitMutationLease> {
        self.leases
            .leases()
            .iter()
            .find(|lease| lease.resource == GitMutationResource::Index)
            .ok_or_else(|| {
                EffectError::custom(
                    "git_mutation_unavailable",
                    "managed Git process has no index mutation lease; no process was started",
                )
            })
    }

    pub(crate) fn audit(&self) -> EffectResult<GitProcessAudit<'_>> {
        Ok(GitProcessAudit {
            lease: self.index_lease()?,
            event_log: &self.event_log,
            operation_id: &self.operation_id,
            agent_id: &self.agent_id,
            birth_branch: &self.birth_branch,
        })
    }

    fn record_requested(&self) -> EffectResult<()> {
        let paths = &self.index_lease()?.paths;
        let data = serde_json::json!({
            "schema_version": 1,
            "operation_id_sha256": digest(&self.operation_id),
            "agent_id": self.agent_id,
            "birth_branch": self.birth_branch,
            "worktree_root": paths.worktree_root,
            "git_dir": paths.git_dir,
            "common_git_dir": paths.common_git_dir,
            "index_file": paths.index_file,
        });
        self.append_event("git.process.requested", &data, "managed Git process intent")
    }

    pub(crate) fn record_finished(
        &self,
        outcome: &str,
        process: Option<&ProcessRunOutput>,
        exit_code: Option<i32>,
    ) -> EffectResult<()> {
        let data = serde_json::json!({
            "schema_version": 1,
            "operation_id_sha256": digest(&self.operation_id),
            "outcome": outcome,
            "process_id": process.map(|result| result.pid),
            "process_start_identity": process.and_then(|result| result.start_identity),
            "process_group_id": process.and_then(|result| result.process_group_id),
            "exit_code": exit_code,
        });
        self.append_event("git.process.finished", &data, "managed Git process result")
    }

    fn append_event(
        &self,
        event_type: &str,
        data: &serde_json::Value,
        description: &str,
    ) -> EffectResult<()> {
        self.event_log
            .append(event_type, "git-process", data)
            .map_err(|_| {
                EffectError::custom(
                    "git_audit_unavailable",
                    format!("{description} could not be recorded in the durable event log"),
                )
            })?;
        Ok(())
    }
}

async fn acquire_git_leases(
    project_dir: &Path,
    worktree: &Path,
    operation_id: &str,
) -> EffectResult<GitMutationLeaseSet> {
    let safety = GitMutationSafety::new(project_dir.to_path_buf());
    let worktree = worktree.to_path_buf();
    let operation_id = operation_id.to_string();
    tokio::task::spawn_blocking(move || {
        safety.acquire_set(
            &worktree,
            &[GitMutationResource::SharedRefs, GitMutationResource::Index],
            &operation_id,
            GIT_PROCESS_LEASE_WAIT,
        )
    })
    .await
    .map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "managed Git mutation lease acquisition did not finish",
        )
    })?
    .map_err(git_mutation_error)
}

fn open_event_log(project_dir: &Path) -> EffectResult<EventLog> {
    EventLog::open(project_dir.join(".exo/logs")).map_err(|_| {
        EffectError::custom(
            "git_audit_unavailable",
            "managed Git process audit ledger is unavailable; no process was started",
        )
    })
}

pub(crate) fn is_git_executable(command: &str) -> bool {
    Path::new(command)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            name.eq_ignore_ascii_case("git") || name.eq_ignore_ascii_case("git.exe")
        })
}

pub(crate) fn validate_git_target(
    args: &[String],
    request_env: &HashMap<String, String>,
) -> EffectResult<()> {
    if command_overrides_git_target(args)
        || env_overrides_git_target(request_env)
        || ambient_git_target_override()
    {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process does not allow repository path overrides; set working_dir to the target worktree and remove Git path overrides",
        ));
    }
    Ok(())
}

pub(crate) fn validate_git_working_directory(
    worktree_root: &Path,
    command_directory: &Path,
) -> EffectResult<()> {
    let root = canonical_directory(worktree_root)?;
    let directory = canonical_directory(command_directory)?;
    if !directory.starts_with(&root) || contains_nested_git_boundary(&root, &directory)? {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process working_dir does not resolve inside its declared worktree; use the owning worktree root",
        ));
    }
    Ok(())
}

fn canonical_directory(path: &Path) -> EffectResult<std::path::PathBuf> {
    fs::canonicalize(path).map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process working_dir could not be resolved before spawn",
        )
    })
}

fn contains_nested_git_boundary(root: &Path, directory: &Path) -> EffectResult<bool> {
    let mut ancestor = directory;
    while ancestor != root {
        if has_git_marker(ancestor)? {
            return Ok(true);
        }
        ancestor = ancestor.parent().ok_or_else(|| {
            EffectError::custom(
                "git_mutation_unavailable",
                "managed Git process working_dir ancestry could not be verified",
            )
        })?;
    }
    Ok(false)
}

fn has_git_marker(directory: &Path) -> EffectResult<bool> {
    match fs::symlink_metadata(directory.join(".git")) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process repository boundary could not be inspected",
        )),
    }
}

fn command_overrides_git_target(args: &[String]) -> bool {
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if is_target_override(argument) {
            return true;
        }
        if !argument.starts_with('-') {
            return false;
        }
        index += usize::from(global_option_takes_value(argument));
        index += 1;
    }
    false
}

fn is_target_override(argument: &str) -> bool {
    argument == "-C"
        || argument.starts_with("-C")
        || argument == "--git-dir"
        || argument.starts_with("--git-dir=")
        || argument == "--work-tree"
        || argument.starts_with("--work-tree=")
}

fn global_option_takes_value(argument: &str) -> bool {
    matches!(
        argument,
        "-c" | "--config-env"
            | "--namespace"
            | "--super-prefix"
            | "--git-dir"
            | "--work-tree"
            | "-C"
    )
}

fn env_overrides_git_target(request_env: &HashMap<String, String>) -> bool {
    request_env.keys().any(|key| {
        GIT_TARGET_ENV_KEYS
            .iter()
            .any(|allowed| key.eq_ignore_ascii_case(allowed))
    })
}

fn ambient_git_target_override() -> bool {
    GIT_TARGET_ENV_KEYS
        .iter()
        .any(|key| std::env::var_os(key).is_some())
}

const GIT_TARGET_ENV_KEYS: [&str; 4] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
];

fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn git_mutation_error(error: GitMutationError) -> EffectError {
    EffectError::custom(
        "git_mutation_unavailable",
        format!("managed Git mutation readiness failed: {error}"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn managed_git_rejects_target_overrides_before_subcommand_only() {
        assert!(command_overrides_git_target(&[
            "--git-dir=/outside/secret.git".into(),
            "status".into(),
        ]));
        assert!(command_overrides_git_target(&[
            "-c".into(),
            "core.pager=cat".into(),
            "-C/outside/secret".into(),
            "status".into(),
        ]));
        assert!(!command_overrides_git_target(&[
            "cherry-pick".into(),
            "-C".into(),
            "HEAD".into(),
        ]));
    }

    #[test]
    fn managed_git_path_override_errors_do_not_echo_paths_or_environment_values() {
        let path_args = vec!["--git-dir=/outside/secret.git".into(), "status".into()];
        let path_error = validate_git_target(&path_args, &HashMap::new()).unwrap_err();
        assert!(path_error.to_string().contains("set working_dir"));
        assert!(!path_error.to_string().contains("secret"));

        let request_env =
            HashMap::from([("GIT_INDEX_FILE".into(), "/outside/secret-index".into())]);
        let env_error = validate_git_target(&[], &request_env).unwrap_err();
        assert!(env_error.to_string().contains("Git path overrides"));
        assert!(!env_error.to_string().contains("secret-index"));
    }

    #[test]
    fn managed_git_cwd_rejects_nested_repositories_and_external_symlinks() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let nested = root.join("nested");
        fs::create_dir(&nested).unwrap();
        assert!(validate_git_working_directory(root, &nested).is_ok());

        fs::write(nested.join(".git"), "gitdir: elsewhere\n").unwrap();
        assert!(validate_git_working_directory(root, &nested).is_err());
        fs::remove_file(nested.join(".git")).unwrap();

        #[cfg(unix)]
        {
            let outside = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink(outside.path(), root.join("external-link")).unwrap();
            assert!(validate_git_working_directory(root, &root.join("external-link")).is_err());
        }
    }
}
