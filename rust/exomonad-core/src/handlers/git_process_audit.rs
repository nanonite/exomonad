//! Mutation leases and durable audit records for direct Git process effects.

use super::git_process_policy::{
    is_git_commit, prepare_git_command, validate_gitlink_index_identity,
};
use super::process_supervision::{GitProcessAudit, ProcessRunOutput};
use crate::effects::{EffectError, EffectResult};
use crate::services::{
    EventLog, GitMutationError, GitMutationLease, GitMutationLeaseSet, GitMutationResource,
    GitMutationSafety,
};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

const GIT_PROCESS_LEASE_WAIT: Duration = Duration::from_secs(2);

pub(crate) struct GitProcessRequest<'a> {
    pub args: &'a [String],
    pub environment: &'a HashMap<String, String>,
    pub operation_id: &'a str,
    pub agent_id: &'a str,
    pub birth_branch: &'a str,
}

pub(crate) struct GitProcessContext {
    leases: GitMutationLeaseSet,
    command_args: Vec<String>,
    commit_index: Option<tempfile::NamedTempFile>,
    event_log: EventLog,
    operation_id: String,
    agent_id: String,
    birth_branch: String,
}

impl GitProcessContext {
    pub(crate) async fn prepare(
        project_dir: &Path,
        worktree: &Path,
        command_directory: &Path,
        request: GitProcessRequest<'_>,
    ) -> EffectResult<Self> {
        let leases = acquire_git_leases(project_dir, worktree, request.operation_id).await?;
        validate_git_working_directory(worktree, command_directory, request.environment)?;
        let command_args =
            prepare_git_command(request.args, command_directory, request.environment)?;
        let commit_index = if is_git_commit(request.args) {
            let index = isolated_commit_index(&leases)?;
            validate_gitlink_index_identity(
                command_directory,
                request.environment,
                Some(index.path()),
            )?;
            Some(index)
        } else {
            None
        };
        let event_log = open_event_log(project_dir)?;
        let context = Self {
            leases,
            command_args,
            commit_index,
            event_log,
            operation_id: request.operation_id.to_string(),
            agent_id: request.agent_id.to_string(),
            birth_branch: request.birth_branch.to_string(),
        };
        context.record_requested()?;
        Ok(context)
    }

    pub(crate) fn command_args(&self) -> &[String] {
        &self.command_args
    }

    pub(crate) fn commit_index_path(&self) -> Option<&Path> {
        self.commit_index
            .as_ref()
            .map(tempfile::NamedTempFile::path)
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
            "isolated_commit_index_file": self.commit_index_path(),
        });
        self.append_event("git.process.requested", &data, "managed Git process intent")
    }

    pub(crate) fn record_finished(
        &self,
        outcome: &str,
        process: Option<&ProcessRunOutput>,
        exit_code: Option<i32>,
    ) -> EffectResult<()> {
        self.record_finished_with_identity(
            outcome,
            process.map(|result| result.pid),
            process.and_then(|result| result.start_identity),
            process.and_then(|result| result.process_group_id),
            exit_code,
            None,
        )
    }

    pub(crate) fn record_finished_with_identity(
        &self,
        outcome: &str,
        process_id: Option<u32>,
        process_start_identity: Option<u64>,
        process_group_id: Option<u32>,
        exit_code: Option<i32>,
        cleanup_diagnostic: Option<&str>,
    ) -> EffectResult<()> {
        let data = serde_json::json!({
            "schema_version": 1,
            "operation_id_sha256": digest(&self.operation_id),
            "outcome": outcome,
            "process_id": process_id,
            "process_start_identity": process_start_identity,
            "process_group_id": process_group_id,
            "exit_code": exit_code,
            "cleanup_diagnostic": cleanup_diagnostic,
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

fn isolated_commit_index(leases: &GitMutationLeaseSet) -> EffectResult<tempfile::NamedTempFile> {
    let lease = leases
        .leases()
        .iter()
        .find(|lease| lease.resource == GitMutationResource::Index)
        .ok_or_else(|| {
            EffectError::custom(
                "git_mutation_unavailable",
                "managed Git commit has no index mutation lease; no process was started",
            )
        })?;
    let metadata = fs::symlink_metadata(&lease.paths.index_file).map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "managed Git commit index could not be inspected; stage changes before committing",
        )
    })?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git commit index is not a regular file; no process was started",
        ));
    }
    let bytes = fs::read(&lease.paths.index_file).map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "managed Git commit index could not be read safely; no process was started",
        )
    })?;
    let parent = lease.paths.index_file.parent().ok_or_else(|| {
        EffectError::custom(
            "git_mutation_unavailable",
            "managed Git commit index has no safe parent directory",
        )
    })?;
    let mut index = tempfile::Builder::new()
        .prefix("exo-process-commit-index-")
        .tempfile_in(parent)
        .map_err(|_| {
            EffectError::custom(
                "git_mutation_unavailable",
                "isolated Git commit index could not be created",
            )
        })?;
    use std::io::Write;
    index.write_all(&bytes).map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "isolated Git commit index could not be initialized",
        )
    })?;
    index.flush().map_err(|_| {
        EffectError::custom(
            "git_mutation_unavailable",
            "isolated Git commit index could not be synchronized",
        )
    })?;
    Ok(index)
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
            "managed Git process does not allow Git path overrides; set working_dir to the target worktree and remove Git path overrides",
        ));
    }
    Ok(())
}

pub(crate) fn validate_git_working_directory(
    worktree_root: &Path,
    command_directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<()> {
    let root = canonical_directory(worktree_root)?;
    let directory = canonical_directory(command_directory)?;
    if !directory.starts_with(&root) {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process working_dir does not resolve inside its declared worktree; use the owning worktree root",
        ));
    }

    let declared_git_dir = effective_git_dir(&root, request_env)?;
    let command_git_dir = effective_git_dir(&directory, request_env)?;
    if declared_git_dir != command_git_dir {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process working_dir resolves to a different repository; use the owning worktree root",
        ));
    }
    let declared_top_level = effective_git_top_level(&root, request_env)?;
    let command_top_level = effective_git_top_level(&directory, request_env)?;
    if declared_top_level != root || command_top_level != root {
        return Err(EffectError::custom(
            "git_mutation_unavailable",
            "managed Git process resolves outside the declared worktree; use the owning worktree root",
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

fn effective_git_dir(
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<std::path::PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--absolute-git-dir"])
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_OBJECT_DIRECTORY_RELATIVE")
        .env_remove("GIT_QUARANTINE_PATH")
        .env_remove("GIT_SHALLOW_FILE")
        .output()
        .map_err(|_| git_identity_unavailable())?;
    if !output.status.success() {
        return Err(git_identity_unavailable());
    }
    let git_dir = std::str::from_utf8(&output.stdout)
        .map_err(|_| git_identity_unavailable())?
        .trim();
    if git_dir.is_empty() {
        return Err(git_identity_unavailable());
    }
    canonical_directory(Path::new(git_dir))
}

fn effective_git_top_level(
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<std::path::PathBuf> {
    let output = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_OBJECT_DIRECTORY_RELATIVE")
        .env_remove("GIT_QUARANTINE_PATH")
        .env_remove("GIT_SHALLOW_FILE")
        .output()
        .map_err(|_| git_identity_unavailable())?;
    if !output.status.success() {
        return Err(git_identity_unavailable());
    }
    let top_level = std::str::from_utf8(&output.stdout)
        .map_err(|_| git_identity_unavailable())?
        .trim();
    if top_level.is_empty() {
        return Err(git_identity_unavailable());
    }
    canonical_directory(Path::new(top_level))
}

fn git_identity_unavailable() -> EffectError {
    EffectError::custom(
        "git_mutation_unavailable",
        "managed Git process repository identity could not be verified before spawn",
    )
}

fn command_overrides_git_target(args: &[String]) -> bool {
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if is_target_override(argument) || config_option_overrides_worktree(argument) {
            return true;
        }
        if (argument == "-c" || argument == "--config-env")
            && args
                .get(index + 1)
                .is_some_and(|value| is_worktree_config_assignment(value))
        {
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

fn config_option_overrides_worktree(argument: &str) -> bool {
    if let Some(value) = argument.strip_prefix("--config-env=") {
        return is_worktree_config_assignment(value);
    }
    argument
        .strip_prefix("-c")
        .filter(|value| !value.is_empty())
        .is_some_and(is_worktree_config_assignment)
}

fn is_worktree_config_assignment(value: &str) -> bool {
    value
        .split_once('=')
        .is_some_and(|(key, _)| key.trim().eq_ignore_ascii_case("core.worktree"))
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
    }) || env_sets_worktree_config(request_env)
}

fn ambient_git_target_override() -> bool {
    let ambient = std::env::vars().collect::<HashMap<_, _>>();
    env_overrides_git_target(&ambient)
}

fn env_sets_worktree_config(environment: &HashMap<String, String>) -> bool {
    if environment
        .keys()
        .any(|key| key.eq_ignore_ascii_case("GIT_CONFIG_PARAMETERS"))
    {
        return true;
    }
    let Some(count) = environment
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("GIT_CONFIG_COUNT"))
        .map(|(_, value)| value)
    else {
        return false;
    };
    let Ok(count) = count.parse::<usize>() else {
        return true;
    };
    if count > MAX_GIT_CONFIG_ENV_ITEMS {
        return true;
    }
    (0..count).any(|index| {
        environment
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(&format!("GIT_CONFIG_KEY_{index}")))
            .is_some_and(|(_, value)| value.eq_ignore_ascii_case("core.worktree"))
    })
}

const MAX_GIT_CONFIG_ENV_ITEMS: usize = 128;

const GIT_TARGET_ENV_KEYS: [&str; 8] = [
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_OBJECT_DIRECTORY_RELATIVE",
    "GIT_QUARANTINE_PATH",
    "GIT_SHALLOW_FILE",
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
    fn managed_git_rejects_external_object_storage_and_worktree_config_overrides() {
        let object_directory =
            HashMap::from([("GIT_OBJECT_DIRECTORY".into(), "/outside/objects".into())]);
        assert!(validate_git_target(&[], &object_directory).is_err());

        let worktree_config = vec![
            "-c".into(),
            "core.worktree=/outside/assigned-worktree".into(),
            "clean".into(),
        ];
        assert!(validate_git_target(&worktree_config, &HashMap::new()).is_err());

        let config_environment = vec![
            "--config-env=core.worktree=EXTERNAL_WORKTREE".into(),
            "status".into(),
        ];
        assert!(validate_git_target(&config_environment, &HashMap::new()).is_err());

        let injected_config = HashMap::from([
            ("GIT_CONFIG_COUNT".into(), "1".into()),
            ("GIT_CONFIG_KEY_0".into(), "core.worktree".into()),
            (
                "GIT_CONFIG_VALUE_0".into(),
                "/outside/assigned-worktree".into(),
            ),
        ]);
        assert!(validate_git_target(&[], &injected_config).is_err());
        assert!(!command_overrides_git_target(&[
            "-c".into(),
            "user.name=per-worktree".into(),
            "status".into(),
        ]));
    }

    #[test]
    fn managed_git_cwd_rejects_nested_repositories_and_external_symlinks() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let init = Command::new("git")
            .args(["init", "-q", "-b", "main"])
            .current_dir(root)
            .output()
            .unwrap();
        assert!(init.status.success());
        let nested = root.join("nested");
        fs::create_dir(&nested).unwrap();
        assert!(validate_git_working_directory(root, &nested, &HashMap::new()).is_ok());

        fs::write(nested.join(".git"), "gitdir: elsewhere\n").unwrap();
        assert!(validate_git_working_directory(root, &nested, &HashMap::new()).is_err());
        fs::remove_file(nested.join(".git")).unwrap();

        #[cfg(unix)]
        {
            let outside = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink(outside.path(), root.join("external-link")).unwrap();
            assert!(validate_git_working_directory(
                root,
                &root.join("external-link"),
                &HashMap::new(),
            )
            .is_err());
        }
    }
}
