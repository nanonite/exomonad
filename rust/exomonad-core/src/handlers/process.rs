//! Process execution effect handler for the `process.*` namespace.
//!
//! Runs arbitrary host commands with args, env, working dir, and optional timeout.
//! Working dir is validated to stay within the agent's worktree (no absolute paths,
//! no `..` traversal).

use super::git_process_audit::{
    is_git_executable, validate_git_target, validate_git_working_directory, GitProcessContext,
    GitProcessRequest,
};
use super::process_supervision::{run_supervised, ProcessRunError};
use crate::effects::{
    dispatch_process_effect, EffectContext, EffectError, EffectResult, ProcessEffects,
};
use async_trait::async_trait;
use exomonad_proto::effects::process::*;
use std::path::{Component, PathBuf};
use std::time::Duration;
use tokio::sync::oneshot;

const CHAINLINK_DB_ENV: &str = "CHAINLINK_DB";
const CHAINLINK_TRACE_ENV: &str = "EXOMONAD_CHAINLINK_TRACE";

/// Process execution effect handler.
///
/// Executes commands within the agent's worktree. No command allowlist —
/// trusts WASM author. Working dir is confined to the agent's worktree root.
pub struct ProcessHandler {
    project_dir: PathBuf,
}

struct ProcessCancellationSignal(Option<oneshot::Sender<()>>);

impl Drop for ProcessCancellationSignal {
    fn drop(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}

impl Default for ProcessHandler {
    fn default() -> Self {
        Self::new()
    }
}

impl ProcessHandler {
    pub fn new() -> Self {
        Self {
            project_dir: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
        }
    }

    pub fn with_project_dir(project_dir: PathBuf) -> Self {
        Self { project_dir }
    }

    /// Validate that working_dir contains no absolute paths or parent traversal.
    fn validate_working_dir(working_dir: &str) -> EffectResult<()> {
        let path = std::path::Path::new(working_dir);

        if path.is_absolute() {
            return Err(EffectError::invalid_input(
                "working_dir must be relative, not absolute",
            ));
        }

        for component in path.components() {
            if matches!(component, Component::ParentDir) {
                return Err(EffectError::invalid_input(
                    "working_dir must not contain '..' (parent directory traversal)",
                ));
            }
        }

        Ok(())
    }

    fn should_trace_chainlink(req: &RunRequest) -> bool {
        req.command == "chainlink" && std::env::var_os(CHAINLINK_TRACE_ENV).is_some()
    }

    fn chainlink_db_env(&self, req: &RunRequest) -> Option<PathBuf> {
        if req.command != "chainlink" || req.env.contains_key(CHAINLINK_DB_ENV) {
            return None;
        }

        Some(self.project_dir.join(".chainlink").join("issues.db"))
    }
}

crate::impl_pass_through_handler!(ProcessHandler, "process", dispatch_process_effect);

#[async_trait]
impl ProcessEffects for ProcessHandler {
    async fn run(&self, req: RunRequest, ctx: &EffectContext) -> EffectResult<RunResponse> {
        if req.working_dir.is_empty() {
            return Err(EffectError::invalid_input("working_dir is required"));
        }

        Self::validate_working_dir(&req.working_dir)?;

        let agent_root = ctx.working_dir.clone();
        let resolved_dir = agent_root.join(&req.working_dir);

        let operation_id = uuid::Uuid::new_v4().to_string();
        tracing::info!(
            process_operation_id = %operation_id,
            "[Process] run starting"
        );
        let trace_chainlink = Self::should_trace_chainlink(&req);
        if trace_chainlink {
            tracing::info!(
                process_operation_id = %operation_id,
                "[Chainlink] command starting"
            );
        }

        let git_process = if is_git_executable(&req.command) {
            validate_git_target(&req.args, &req.env)?;
            validate_git_working_directory(&ctx.working_dir, &resolved_dir, &req.env)?;
            Some(
                GitProcessContext::prepare(
                    &self.project_dir,
                    &ctx.working_dir,
                    &resolved_dir,
                    GitProcessRequest {
                        args: &req.args,
                        environment: &req.env,
                        operation_id: &operation_id,
                        agent_id: ctx.agent_name.as_ref(),
                        birth_branch: ctx.birth_branch.as_str(),
                    },
                )
                .await?,
            )
        } else {
            None
        };
        let command_args = git_process
            .as_ref()
            .map(GitProcessContext::command_args)
            .unwrap_or(&req.args);
        let mut cmd = tokio::process::Command::new(&req.command);
        cmd.args(command_args)
            .current_dir(&resolved_dir)
            .kill_on_drop(true);

        if !req.env.is_empty() {
            cmd.envs(&req.env);
        }
        if let Some(index_file) = git_process
            .as_ref()
            .and_then(GitProcessContext::commit_index_path)
        {
            cmd.env("GIT_INDEX_FILE", index_file);
        }
        if let Some(chainlink_db) = self.chainlink_db_env(&req) {
            cmd.env(CHAINLINK_DB_ENV, chainlink_db);
        }
        let timeout = (req.timeout_ms > 0).then(|| Duration::from_millis(req.timeout_ms));
        let (cancel_sender, mut cancel_receiver) = oneshot::channel();
        let _cancellation_signal = ProcessCancellationSignal(Some(cancel_sender));
        let supervisor_operation_id = operation_id.clone();
        let supervisor = tokio::spawn(async move {
            let audit = git_process
                .as_ref()
                .map(GitProcessContext::audit)
                .transpose()
                .map_err(|_| ProcessRunError::Audit)?;
            let result = run_supervised(
                &mut cmd,
                timeout,
                &supervisor_operation_id,
                audit,
                &mut cancel_receiver,
            )
            .await;
            if let Some(context) = git_process {
                let recorded = match &result {
                    Ok(output) => {
                        context.record_finished("exited", Some(output), output.status.code())
                    }
                    Err(ProcessRunError::Cancelled {
                        pid,
                        start_identity,
                        process_group_id,
                        cleanup_diagnostic,
                    }) => context.record_finished_with_identity(
                        if cleanup_diagnostic.is_some() {
                            "cleanup_unresolved"
                        } else {
                            "cancelled"
                        },
                        Some(*pid),
                        *start_identity,
                        *process_group_id,
                        None,
                        *cleanup_diagnostic,
                    ),
                    Err(error) => context.record_finished(process_outcome(error), None, None),
                };
                if recorded.is_err() {
                    return Err(ProcessRunError::Audit);
                }
            }
            result
        });
        let output = supervisor
            .await
            .map_err(|_| {
                EffectError::custom(
                    "process_cleanup_unresolved",
                    format!("managed process supervisor exited unexpectedly; operation_id={operation_id}"),
                )
            })?
            .map_err(|error| process_run_error(error, req.timeout_ms, &operation_id))?;
        let exit_code = output.status.code().unwrap_or(-1);
        let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
        let stderr = String::from_utf8_lossy(&output.stderr).into_owned();

        tracing::info!(
            process_operation_id = %operation_id,
            child_pid = output.pid,
            child_start_identity = ?output.start_identity,
            process_group_id = ?output.process_group_id,
            exit_code,
            "[Process] run completed"
        );
        if trace_chainlink {
            tracing::info!(
                process_operation_id = %operation_id,
                exit_code,
                "[Chainlink] command completed"
            );
        }

        Ok(RunResponse {
            exit_code,
            stdout,
            stderr,
        })
    }
}

fn process_run_error(error: ProcessRunError, timeout_ms: u64, operation_id: &str) -> EffectError {
    match error {
        ProcessRunError::Timeout => EffectError::Timeout {
            message: format!(
                "process.run timed out after {timeout_ms}ms; managed process group stopped; operation_id={operation_id}"
            ),
        },
        ProcessRunError::Spawn(kind) => EffectError::custom(
            "process_error",
            format!("managed process could not start ({kind:?}); operation_id={operation_id}"),
        ),
        ProcessRunError::Wait(kind) | ProcessRunError::Output(kind) => EffectError::custom(
            "process_error",
            format!("managed process result is unavailable ({kind:?}); operation_id={operation_id}"),
        ),
        ProcessRunError::Cancelled {
            cleanup_diagnostic: Some(reason),
            ..
        } => EffectError::custom(
            "process_cleanup_unresolved",
            format!("{reason}; operation_id={operation_id}"),
        ),
        ProcessRunError::Cancelled { .. } => EffectError::custom(
            "process_cancelled",
            format!("managed process was cancelled; operation_id={operation_id}"),
        ),
        ProcessRunError::Cleanup(reason) => EffectError::custom(
            "process_cleanup_unresolved",
            format!("{reason}; operation_id={operation_id}"),
        ),
        ProcessRunError::Audit => EffectError::custom(
            "git_audit_unavailable",
            format!(
                "managed Git process could not record its lifecycle; operation_id={operation_id}"
            ),
        ),
    }
}

fn process_outcome(error: &ProcessRunError) -> &'static str {
    match error {
        ProcessRunError::Timeout => "timed_out",
        ProcessRunError::Cancelled {
            cleanup_diagnostic: Some(_),
            ..
        } => "cleanup_unresolved",
        ProcessRunError::Cancelled { .. } => "cancelled",
        ProcessRunError::Spawn(_) => "spawn_failed",
        ProcessRunError::Wait(_) | ProcessRunError::Output(_) => "unresolved",
        ProcessRunError::Cleanup(_) | ProcessRunError::Audit => "cleanup_unresolved",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;
    use crate::services::EventLog;

    fn test_ctx() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("test")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    fn test_ctx_at(working_dir: &std::path::Path) -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("test")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: working_dir.to_path_buf(),
        }
    }

    fn run_git_fixture_output(path: &std::path::Path, args: &[&str]) -> std::process::Output {
        std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .env_remove("GIT_COMMON_DIR")
            .env_remove("GIT_INDEX_FILE")
            .output()
            .expect("test environment has Git on PATH")
    }

    fn assert_git_fixture_succeeded(output: std::process::Output, args: &[&str]) {
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[cfg(target_os = "linux")]
    fn process_is_running(pid: u32) -> bool {
        let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else {
            return false;
        };
        stat.rsplit_once(')')
            .and_then(|(_, fields)| fields.split_whitespace().next())
            .is_some_and(|state| state != "Z")
    }

    #[cfg(target_os = "linux")]
    async fn wait_until_process_stops(pid: u32) -> bool {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while tokio::time::Instant::now() < deadline {
            if !process_is_running(pid) {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        !process_is_running(pid)
    }

    #[cfg(target_os = "linux")]
    async fn assert_stopped_and_cleanup(pid: u32) -> bool {
        let stopped = wait_until_process_stops(pid).await;
        if !stopped {
            let _ = nix::sys::signal::kill(
                nix::unistd::Pid::from_raw(pid as i32),
                nix::sys::signal::Signal::SIGKILL,
            );
            let _ = wait_until_process_stops(pid).await;
        }
        stopped
    }

    #[tokio::test]
    async fn test_run_echo() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec!["hello".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 5000,
        };

        let resp = handler.run(req, &ctx).await.unwrap();
        assert_eq!(resp.exit_code, 0);
        assert!(resp.stdout.contains("hello"));
    }

    #[tokio::test]
    async fn test_run_empty_working_dir_rejected() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec![],
            working_dir: "".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::InvalidInput { .. }));
    }

    #[tokio::test]
    async fn test_run_absolute_path_rejected() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec![],
            working_dir: "/tmp".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::InvalidInput { .. }));
    }

    #[tokio::test]
    async fn test_run_parent_traversal_rejected() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec![],
            working_dir: "../../../etc".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::InvalidInput { .. }));
    }

    #[tokio::test]
    async fn test_run_nested_parent_traversal_rejected() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec![],
            working_dir: "subdir/../../escape".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::InvalidInput { .. }));
    }

    #[tokio::test]
    async fn process_handler_rejects_nested_bare_git_repository_before_ref_mutation() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let initialize_outer = ["init", "-q", "-b", "main"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(root, &initialize_outer),
            &initialize_outer,
        );

        let nested_bare = root.join("nested-bare.git");
        std::fs::create_dir(&nested_bare).unwrap();
        let initialize_nested = ["init", "-q", "--bare", "."];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&nested_bare, &initialize_nested),
            &initialize_nested,
        );
        let head_before = std::fs::read(nested_bare.join("HEAD")).unwrap();

        let handler = ProcessHandler::with_project_dir(root.to_path_buf());
        let context = test_ctx_at(root);
        let request = RunRequest {
            command: "git".into(),
            args: vec![
                "symbolic-ref".into(),
                "HEAD".into(),
                "refs/heads/escaped".into(),
            ],
            working_dir: "nested-bare.git".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 5_000,
        };

        let error = handler.run(request, &context).await.unwrap_err();

        assert!(error.to_string().contains("different repository"));
        assert_eq!(
            std::fs::read(nested_bare.join("HEAD")).unwrap(),
            head_before
        );
        assert!(!nested_bare.join("refs/heads/escaped").exists());
        assert!(!root.join(".exo/logs").exists());
    }

    #[tokio::test]
    async fn process_handler_rejects_git_object_writes_outside_the_assigned_repository() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("assigned");
        std::fs::create_dir(&root).unwrap();
        let initialize = ["init", "-q", "-b", "main"];
        assert_git_fixture_succeeded(run_git_fixture_output(&root, &initialize), &initialize);
        std::fs::write(root.join("source.txt"), "assigned source\n").unwrap();

        let outside_objects = temporary.path().join("external-objects");
        std::fs::create_dir(&outside_objects).unwrap();
        let handler = ProcessHandler::with_project_dir(root.clone());
        let request = RunRequest {
            command: "git".into(),
            args: vec!["hash-object".into(), "-w".into(), "source.txt".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::from([(
                "GIT_OBJECT_DIRECTORY".into(),
                outside_objects.to_string_lossy().into_owned(),
            )]),
            timeout_ms: 5_000,
        };

        let error = handler
            .run(request, &test_ctx_at(&root))
            .await
            .expect_err("managed Git must reject an external object directory");

        assert!(error.to_string().contains("Git path overrides"));
        assert!(std::fs::read_dir(outside_objects).unwrap().next().is_none());
        assert!(!root.join(".exo/logs").exists());
    }

    #[tokio::test]
    async fn process_handler_blocks_nested_git_mutators_before_starting_a_process() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let initialize = ["init", "-q", "-b", "main"];
        assert_git_fixture_succeeded(run_git_fixture_output(root, &initialize), &initialize);
        let handler = ProcessHandler::with_project_dir(root.to_path_buf());
        let commands = [
            vec!["submodule", "update", "--init", "--recursive"],
            vec!["submodule", "init"],
            vec!["submodule", "deinit", "--", "vendor/reference"],
            vec!["submodule", "foreach", "git", "fetch"],
            vec!["submodule", "sync", "--recursive"],
            vec!["submodule", "absorbgitdirs"],
            vec!["fetch", "--recurse-submodules=on-demand"],
            vec!["push", "--recurse-submodules=on-demand"],
            vec!["checkout", "--recurse-submodules", "HEAD"],
            vec!["worktree", "add", ".exo/worktrees/bypass", "HEAD"],
            vec!["clone", "https://example.invalid/reference.git", "clone"],
            vec!["init", "nested"],
            vec!["clean", "-ffdx"],
        ];

        for args in commands {
            let request = RunRequest {
                command: "git".into(),
                args: args.iter().map(|arg| (*arg).to_string()).collect(),
                working_dir: ".".into(),
                env: std::collections::HashMap::new(),
                timeout_ms: 5_000,
            };
            let error = handler
                .run(request, &test_ctx_at(root))
                .await
                .expect_err("nested Git mutation must be refused before process start");
            assert!(error.to_string().contains("git_mutation_unavailable"));
        }

        assert!(!root.join("nested/.git").exists());
        assert!(!root.join("clone/.git").exists());
        assert!(!root.join(".exo/worktrees/bypass").exists());
        assert!(!root.join(".exo/logs").exists());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn process_handler_commit_all_excludes_reference_gitlinks_and_holds_staged_pointers() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("workspace");
        std::fs::create_dir(&root).unwrap();
        for arguments in [
            vec!["init", "-q", "-b", "main"],
            vec!["config", "user.name", "process test"],
            vec!["config", "user.email", "process-test@example.invalid"],
        ] {
            assert_git_fixture_succeeded(run_git_fixture_output(&root, &arguments), &arguments);
        }
        std::fs::create_dir_all(root.join("tools")).unwrap();
        let reference_source = temporary.path().join("reference-source");
        std::fs::create_dir(&reference_source).unwrap();
        for arguments in [
            vec!["init", "-q", "-b", "main"],
            vec!["config", "user.name", "reference test"],
            vec!["config", "user.email", "reference-test@example.invalid"],
        ] {
            assert_git_fixture_succeeded(
                run_git_fixture_output(&reference_source, &arguments),
                &arguments,
            );
        }
        std::fs::write(reference_source.join("ref.txt"), "reference base\n").unwrap();
        let add_reference = ["add", "ref.txt"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&reference_source, &add_reference),
            &add_reference,
        );
        let commit_reference = ["commit", "-q", "-m", "reference base"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&reference_source, &commit_reference),
            &commit_reference,
        );
        let reference_oid = String::from_utf8(
            run_git_fixture_output(&reference_source, &["rev-parse", "HEAD"]).stdout,
        )
        .unwrap()
        .trim()
        .to_string();
        let reference_path = root.join("tools/reference");
        let clone = std::process::Command::new("git")
            .args(["clone", "-q", "--local"])
            .arg(&reference_source)
            .arg(&reference_path)
            .output()
            .unwrap();
        assert!(
            clone.status.success(),
            "{}",
            String::from_utf8_lossy(&clone.stderr)
        );
        std::fs::write(
            root.join(".gitmodules"),
            "[submodule \"reference\"]\n\tpath = tools/reference\n\turl = https://example.invalid/reference.git\n",
        )
        .unwrap();
        let add_modules = ["add", ".gitmodules"];
        assert_git_fixture_succeeded(run_git_fixture_output(&root, &add_modules), &add_modules);
        let gitlink = format!("160000,{reference_oid},tools/reference");
        assert_git_fixture_succeeded(
            run_git_fixture_output(&root, &["update-index", "--add", "--cacheinfo", &gitlink]),
            &["update-index", "--add", "--cacheinfo", &gitlink],
        );
        let initial_commit = ["commit", "-q", "-m", "workspace with reference"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&root, &initial_commit),
            &initial_commit,
        );

        std::fs::write(reference_path.join("ref.txt"), "reference advanced\n").unwrap();
        let nested_add = ["add", "ref.txt"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&reference_path, &nested_add),
            &nested_add,
        );
        let nested_commit = ["commit", "-q", "-m", "reference advanced"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&reference_path, &nested_commit),
            &nested_commit,
        );
        std::fs::write(root.join("parent.txt"), "ordinary parent edit\n").unwrap();

        let handler = ProcessHandler::with_project_dir(root.clone());
        let add_response = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["add".into(), "-A".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("commit-all staging should run for ordinary workspace files");
        assert_eq!(add_response.exit_code, 0, "{}", add_response.stderr);
        let staged = String::from_utf8(
            run_git_fixture_output(&root, &["diff", "--cached", "--name-only"]).stdout,
        )
        .unwrap();
        assert!(staged.lines().any(|path| path == "parent.txt"));
        assert!(!staged.lines().any(|path| path == "tools/reference"));

        let commit_response = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["commit".into(), "-m".into(), "parent change".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("ordinary staged workspace changes should commit");
        assert_eq!(commit_response.exit_code, 0, "{}", commit_response.stderr);
        assert_eq!(
            String::from_utf8(
                run_git_fixture_output(&root, &["rev-parse", "HEAD:tools/reference"]).stdout
            )
            .unwrap()
            .trim(),
            reference_oid
        );

        let advanced_reference_oid = String::from_utf8(
            run_git_fixture_output(&reference_path, &["rev-parse", "HEAD"]).stdout,
        )
        .unwrap()
        .trim()
        .to_string();
        let head_before_pathspec =
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let pathspec_commit = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec![
                        "commit".into(),
                        "-m".into(),
                        "unauthorized reference pathspec".into(),
                        "tools/reference".into(),
                    ],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("commit pathspec must not restage a reference from its worktree");
        assert!(
            pathspec_commit
                .to_string()
                .contains("commit options that stage worktree paths are refused"),
            "unexpected pathspec refusal: {pathspec_commit}"
        );
        assert_eq!(
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim(),
            head_before_pathspec,
            "refusing the pathspec must leave the parent commit unchanged"
        );
        let update_refusal = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec![
                        "update-index".into(),
                        "--cacheinfo".into(),
                        format!("160000,{advanced_reference_oid},tools/reference"),
                    ],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("direct update-index must not change a parent gitlink");
        assert!(update_refusal
            .to_string()
            .contains("direct update-index is refused"));

        std::fs::write(
            root.join("race.txt"),
            "commit before external index writer\n",
        )
        .unwrap();
        let stage_race = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["add".into(), "-A".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("ordinary workspace edits remain stageable");
        assert_eq!(stage_race.exit_code, 0, "{}", stage_race.stderr);
        let real_git = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|directory| directory.join("git"))
            .find(|candidate| candidate.is_file())
            .unwrap();
        let actual_index = root.join(".git/index");
        let wrapper = root.join(".git/git");
        std::fs::write(
            &wrapper,
            format!(
                "#!/bin/sh\nif [ \"$1\" = \"commit\" ]; then GIT_INDEX_FILE='{}' '{}' update-index --cacheinfo '{}'; fi\nexec '{}' \"$@\"\n",
                actual_index.display(),
                real_git.display(),
                format!("160000,{advanced_reference_oid},tools/reference"),
                real_git.display(),
            ),
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        let mut wrapper_permissions = std::fs::metadata(&wrapper).unwrap().permissions();
        wrapper_permissions.set_mode(0o755);
        std::fs::set_permissions(&wrapper, wrapper_permissions).unwrap();
        let head_before_race =
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let racing_commit = handler
            .run(
                RunRequest {
                    command: wrapper.to_string_lossy().into_owned(),
                    args: vec!["commit".into(), "-m".into(), "concurrent commit".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("commit should use its immutable index snapshot");
        assert_eq!(racing_commit.exit_code, 0, "{}", racing_commit.stderr);
        assert_eq!(
            String::from_utf8(
                run_git_fixture_output(&root, &["rev-parse", "HEAD:tools/reference"]).stdout
            )
            .unwrap()
            .trim(),
            reference_oid,
            "external index change immediately before commit must not enter its tree"
        );
        let staged_reference = String::from_utf8(
            run_git_fixture_output(&root, &["ls-files", "--stage", "tools/reference"]).stdout,
        )
        .unwrap();
        assert!(staged_reference.contains(&advanced_reference_oid));

        let parent_head =
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let staged_refusal = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["commit".into(), "-m".into(), "unauthorized pointer".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("pre-staged unauthorized reference pointer must block commit");
        assert!(staged_refusal
            .to_string()
            .contains("reference Git mutation refused"));
        assert_eq!(
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim(),
            parent_head
        );
        assert_ne!(head_before_race, parent_head);

        let alternate = temporary.path().join("alternate-reference");
        std::fs::create_dir(&alternate).unwrap();
        for arguments in [
            vec!["init", "-q", "-b", "main"],
            vec!["config", "user.name", "assigned reference"],
            vec!["config", "user.email", "assigned-reference@example.invalid"],
        ] {
            assert_git_fixture_succeeded(
                run_git_fixture_output(&alternate, &arguments),
                &arguments,
            );
        }
        std::fs::write(alternate.join("file.txt"), "assigned work\n").unwrap();
        let alternate_add = ["add", "-A"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&alternate, &alternate_add),
            &alternate_add,
        );
        let assigned_handler = ProcessHandler::with_project_dir(alternate.clone());
        let assigned_ctx = test_ctx_at(&alternate);
        let assigned = assigned_handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["commit".into(), "-m".into(), "assigned work".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &assigned_ctx,
            )
            .await
            .expect("a separately assigned repository keeps ordinary Git behavior");
        assert_eq!(assigned.exit_code, 0, "{}", assigned.stderr);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn process_handler_resolves_git_aliases_before_reference_mutation_guards() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path().join("workspace");
        std::fs::create_dir(&root).unwrap();
        for arguments in [
            vec!["init", "-q", "-b", "main"],
            vec!["config", "user.name", "process alias test"],
            vec!["config", "user.email", "process-alias@example.invalid"],
        ] {
            assert_git_fixture_succeeded(run_git_fixture_output(&root, &arguments), &arguments);
        }
        let source = temporary.path().join("reference-source");
        std::fs::create_dir(&source).unwrap();
        for arguments in [
            vec!["init", "-q", "-b", "main"],
            vec!["config", "user.name", "reference alias test"],
            vec!["config", "user.email", "reference-alias@example.invalid"],
        ] {
            assert_git_fixture_succeeded(run_git_fixture_output(&source, &arguments), &arguments);
        }
        std::fs::write(source.join("reference.txt"), "base\n").unwrap();
        let add = ["add", "reference.txt"];
        assert_git_fixture_succeeded(run_git_fixture_output(&source, &add), &add);
        let commit = ["commit", "-q", "-m", "base"];
        assert_git_fixture_succeeded(run_git_fixture_output(&source, &commit), &commit);
        let original_oid =
            String::from_utf8(run_git_fixture_output(&source, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let nested = root.join("tools/reference");
        std::fs::create_dir_all(nested.parent().unwrap()).unwrap();
        let clone = std::process::Command::new("git")
            .args(["clone", "-q", "--local"])
            .arg(&source)
            .arg(&nested)
            .output()
            .unwrap();
        assert!(
            clone.status.success(),
            "{}",
            String::from_utf8_lossy(&clone.stderr)
        );
        std::fs::write(
            root.join(".gitmodules"),
            "[submodule \"reference\"]\n\tpath = tools/reference\n\turl = https://example.invalid/reference.git\n",
        )
        .unwrap();
        let add_modules = ["add", ".gitmodules"];
        assert_git_fixture_succeeded(run_git_fixture_output(&root, &add_modules), &add_modules);
        let gitlink = format!("160000,{original_oid},tools/reference");
        assert_git_fixture_succeeded(
            run_git_fixture_output(&root, &["update-index", "--add", "--cacheinfo", &gitlink]),
            &["update-index", "--add", "--cacheinfo", &gitlink],
        );
        let initial_commit = ["commit", "-q", "-m", "reference"];
        assert_git_fixture_succeeded(
            run_git_fixture_output(&root, &initial_commit),
            &initial_commit,
        );

        std::fs::write(nested.join("reference.txt"), "advanced\n").unwrap();
        assert_git_fixture_succeeded(run_git_fixture_output(&nested, &add), &add);
        assert_git_fixture_succeeded(run_git_fixture_output(&nested, &commit), &commit);
        let advanced_oid =
            String::from_utf8(run_git_fixture_output(&nested, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        std::fs::write(root.join("ordinary.txt"), "workspace edit\n").unwrap();
        let handler = ProcessHandler::with_project_dir(root.clone());

        let staged = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec![
                        "-c".into(),
                        "alias.stage-all=add -A".into(),
                        "stage-all".into(),
                    ],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("command-line staging aliases must receive reference exclusions");
        assert_eq!(staged.exit_code, 0, "{}", staged.stderr);
        let staged_paths = String::from_utf8(
            run_git_fixture_output(&root, &["diff", "--cached", "--name-only"]).stdout,
        )
        .unwrap();
        assert!(staged_paths.lines().any(|path| path == "ordinary.txt"));
        assert!(!staged_paths.lines().any(|path| path == "tools/reference"));

        for (key, value) in [
            ("alias.stage-chain", "stage-final"),
            ("alias.stage-final", "add -A"),
            ("alias.finish", "commit -m tidy"),
        ] {
            assert_git_fixture_succeeded(
                run_git_fixture_output(&root, &["config", "--local", key, value]),
                &["config", "--local", key, value],
            );
        }
        std::fs::write(root.join("second.txt"), "second workspace edit\n").unwrap();
        let chained_stage = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["stage-chain".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("repository-configured chained aliases must be resolved");
        assert_eq!(chained_stage.exit_code, 0, "{}", chained_stage.stderr);
        let committed = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec![
                        "-c".into(),
                        "alias.finish-cli=finish".into(),
                        "finish-cli".into(),
                    ],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect("commit aliases must use the isolated reference-validated index");
        assert_eq!(committed.exit_code, 0, "{}", committed.stderr);
        assert_eq!(
            String::from_utf8(
                run_git_fixture_output(&root, &["rev-parse", "HEAD:tools/reference"]).stdout,
            )
            .unwrap()
            .trim(),
            original_oid
        );

        std::fs::write(nested.join("alias-target.txt"), "must remain untracked\n").unwrap();
        let parent_index_before_alias_refusals = std::fs::read(root.join(".git/index")).unwrap();
        let reference_index_before_alias_refusals =
            std::fs::read(nested.join(".git/index")).unwrap();
        let reference_head_before_alias_refusals =
            String::from_utf8(run_git_fixture_output(&nested, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let reference_refs_before_alias_refusals =
            run_git_fixture_output(&nested, &["show-ref"]).stdout;

        for (key, value) in [
            ("alias.redirect-chain", "redirect-final"),
            ("alias.redirect-final", "-C tools/reference add -A"),
            (
                "alias.redirect-git-dir",
                "--git-dir tools/reference/.git add -A",
            ),
            (
                "alias.redirect-work-tree",
                "--work-tree tools/reference add -A",
            ),
            (
                "alias.redirect-core-worktree",
                "-c core.worktree=tools/reference add -A",
            ),
            (
                "alias.redirect-config-env",
                "--config-env=core.worktree=REFERENCE_WORKTREE add -A",
            ),
        ] {
            assert_git_fixture_succeeded(
                run_git_fixture_output(&root, &["config", "--local", key, value]),
                &["config", "--local", key, value],
            );
        }
        let redirections = [
            (
                vec![
                    "-c".into(),
                    "alias.redirect-c=-C tools/reference add -A".into(),
                    "redirect-c".into(),
                ],
                std::collections::HashMap::new(),
            ),
            (
                vec!["redirect-git-dir".into()],
                std::collections::HashMap::new(),
            ),
            (
                vec!["redirect-work-tree".into()],
                std::collections::HashMap::new(),
            ),
            (
                vec!["redirect-core-worktree".into()],
                std::collections::HashMap::new(),
            ),
            (
                vec!["redirect-config-env".into()],
                std::collections::HashMap::from([(
                    "REFERENCE_WORKTREE".into(),
                    "tools/reference".into(),
                )]),
            ),
            (
                vec!["redirect-chain".into()],
                std::collections::HashMap::new(),
            ),
        ];
        for (args, env) in redirections {
            let error = handler
                .run(
                    RunRequest {
                        command: "git".into(),
                        args,
                        working_dir: ".".into(),
                        env,
                        timeout_ms: 5_000,
                    },
                    &test_ctx_at(&root),
                )
                .await
                .expect_err("an alias must not redirect Git outside its assigned worktree");
            assert!(error.to_string().contains("Git path overrides"));
            assert_eq!(
                std::fs::read(root.join(".git/index")).unwrap(),
                parent_index_before_alias_refusals
            );
            assert_eq!(
                std::fs::read(nested.join(".git/index")).unwrap(),
                reference_index_before_alias_refusals
            );
        }
        assert_eq!(
            String::from_utf8(run_git_fixture_output(&nested, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim(),
            reference_head_before_alias_refusals
        );
        assert_eq!(
            run_git_fixture_output(&nested, &["show-ref"]).stdout,
            reference_refs_before_alias_refusals
        );
        assert_eq!(
            std::fs::read(nested.join("alias-target.txt")).unwrap(),
            b"must remain untracked\n"
        );

        assert_git_fixture_succeeded(
            run_git_fixture_output(
                &root,
                &[
                    "update-index",
                    "--cacheinfo",
                    &format!("160000,{advanced_oid},tools/reference"),
                ],
            ),
            &[
                "update-index",
                "--cacheinfo",
                &format!("160000,{advanced_oid},tools/reference"),
            ],
        );
        let head_before_refusal =
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim()
                .to_string();
        let refused = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["finish".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("pre-staged reference pointer changes must block commit aliases");
        assert!(refused
            .to_string()
            .contains("reference Git mutation refused"));
        assert_eq!(
            String::from_utf8(run_git_fixture_output(&root, &["rev-parse", "HEAD"]).stdout)
                .unwrap()
                .trim(),
            head_before_refusal
        );
        assert!(String::from_utf8(
            run_git_fixture_output(&root, &["ls-files", "--stage", "tools/reference"]).stdout,
        )
        .unwrap()
        .contains(&advanced_oid));

        let shell_marker = temporary.path().join("shell-alias-ran");
        let shell_alias = format!("alias.shell-write=!touch {}", shell_marker.display());
        let shell_refused = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["-c".into(), shell_alias, "shell-write".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("shell aliases must never run through managed Git policy");
        assert!(shell_refused
            .to_string()
            .contains("shell Git aliases are not allowed"));
        assert!(!shell_marker.exists());

        for (key, value) in [
            ("alias.cycle-one", "cycle-two"),
            ("alias.cycle-two", "cycle-one"),
        ] {
            assert_git_fixture_succeeded(
                run_git_fixture_output(&root, &["config", "--local", key, value]),
                &["config", "--local", key, value],
            );
        }
        let cycle_refused = handler
            .run(
                RunRequest {
                    command: "git".into(),
                    args: vec!["cycle-one".into()],
                    working_dir: ".".into(),
                    env: std::collections::HashMap::new(),
                    timeout_ms: 5_000,
                },
                &test_ctx_at(&root),
            )
            .await
            .expect_err("cyclic aliases must be rejected before Git starts");
        assert!(cycle_refused
            .to_string()
            .contains("alias expansion contains a cycle"));
    }

    #[tokio::test]
    async fn process_handler_keeps_read_only_submodule_and_worktree_inspection_available() {
        let temporary = tempfile::tempdir().unwrap();
        let root = temporary.path();
        let initialize = ["init", "-q", "-b", "main"];
        assert_git_fixture_succeeded(run_git_fixture_output(root, &initialize), &initialize);
        let handler = ProcessHandler::with_project_dir(root.to_path_buf());

        for args in [
            vec!["submodule", "status"],
            vec!["submodule", "summary"],
            vec!["worktree", "list"],
        ] {
            let request = RunRequest {
                command: "git".into(),
                args: args.iter().map(|arg| (*arg).to_string()).collect(),
                working_dir: ".".into(),
                env: std::collections::HashMap::new(),
                timeout_ms: 5_000,
            };
            let response = handler
                .run(request, &test_ctx_at(root))
                .await
                .unwrap_or_else(|error| panic!("git {args:?} should remain available: {error}"));
            assert_eq!(response.exit_code, 0, "git {args:?}");
        }
    }

    #[tokio::test]
    async fn test_run_subdirectory_allowed() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "echo".into(),
            args: vec!["subdir-test".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 5000,
        };

        let resp = handler.run(req, &ctx).await.unwrap();
        assert_eq!(resp.exit_code, 0);
    }

    #[tokio::test]
    async fn test_run_timeout_kills_process() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();

        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec!["-c".into(), "sleep 60".into(), "credential-canary".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 100,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::Timeout { .. }));
        assert!(!err.to_string().contains("credential-canary"));
    }

    #[tokio::test]
    async fn nonzero_exit_preserves_command_result() {
        let handler = ProcessHandler::new();
        let ctx = test_ctx();
        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec!["-c".into(), "exit 7".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 1000,
        };

        let response = handler.run(req, &ctx).await.unwrap();
        assert_eq!(response.exit_code, 7);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn timeout_does_not_leave_a_helper_process_running() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("helper.pid");
        let handler = ProcessHandler::new();
        let ctx = test_ctx_at(temp.path());
        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                format!("sleep 60 & echo $! > {}; wait", pid_file.display()),
            ],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 100,
        };

        let error = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(error, EffectError::Timeout { .. }));
        let pid = wait_for_pid_file(&pid_file).await;
        let stopped = assert_stopped_and_cleanup(pid).await;
        assert!(stopped, "timed-out process left helper PID {pid} running");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn timeout_forces_termination_when_group_ignores_sigterm() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("helper.pid");
        let handler = ProcessHandler::new();
        let ctx = test_ctx_at(temp.path());
        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                format!(
                    "trap '' TERM; (trap '' TERM; exec sleep 60) & echo $! > {}; wait",
                    pid_file.display()
                ),
            ],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 100,
        };

        let error = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(error, EffectError::Timeout { .. }));
        let pid = wait_for_pid_file(&pid_file).await;
        assert!(assert_stopped_and_cleanup(pid).await);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn timeout_preserves_unrelated_processes() {
        let temp = tempfile::tempdir().unwrap();
        let mut unrelated = tokio::process::Command::new("sleep")
            .arg("60")
            .spawn()
            .unwrap();
        let unrelated_pid = unrelated.id().unwrap();
        let handler = ProcessHandler::new();
        let ctx = test_ctx_at(temp.path());
        let req = RunRequest {
            command: "sleep".into(),
            args: vec!["60".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 100,
        };

        let error = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(error, EffectError::Timeout { .. }));
        let unrelated_survived = process_is_running(unrelated_pid);
        unrelated.start_kill().unwrap();
        unrelated.wait().await.unwrap();
        assert!(unrelated_survived);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn aborting_the_runner_kills_its_process_group() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("helper.pid");
        let handler = ProcessHandler::new();
        let ctx = test_ctx_at(temp.path());
        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                format!("sleep 60 & echo $! > {}; wait", pid_file.display()),
            ],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };
        let runner = tokio::spawn(async move { handler.run(req, &ctx).await });
        let pid = match tokio::time::timeout(Duration::from_secs(2), wait_for_pid_file(&pid_file))
            .await
        {
            Ok(pid) => pid,
            Err(_) => {
                runner.abort();
                let _ = runner.await;
                panic!("helper did not start");
            }
        };

        runner.abort();
        assert!(runner.await.unwrap_err().is_cancelled());
        assert!(assert_stopped_and_cleanup(pid).await);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn aborting_a_sigterm_ignoring_group_forces_kill_and_reaps_helpers() {
        let temp = tempfile::tempdir().unwrap();
        let parent_pid_file = temp.path().join("parent.pid");
        let helper_pid_file = temp.path().join("helper.pid");
        let handler = ProcessHandler::new();
        let context = test_ctx_at(temp.path());
        let request = RunRequest {
            command: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                format!(
                    "trap '' TERM; echo $$ > {}; (trap '' TERM; exec sleep 60) & echo $! > {}; wait",
                    parent_pid_file.display(),
                    helper_pid_file.display()
                ),
            ],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };
        let runner = tokio::spawn(async move { handler.run(request, &context).await });
        let helper_pid = wait_for_pid_file(&helper_pid_file).await;
        let parent_pid = wait_for_pid_file(&parent_pid_file).await;

        runner.abort();
        assert!(runner.await.unwrap_err().is_cancelled());
        assert!(assert_stopped_and_cleanup(parent_pid).await);
        assert!(assert_stopped_and_cleanup(helper_pid).await);
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn aborting_a_git_process_records_one_finished_event_after_group_cleanup() {
        use crate::services::EventLog;

        let temp = tempfile::tempdir().unwrap();
        let (git_shim, _fifo_guard) = init_git_index_lock_fixture(temp.path());
        let worktree = temp.path().to_path_buf();
        let command_directory = worktree.join("nested");
        let saved_index = std::fs::read(worktree.join(".git/index")).unwrap();
        let handler = ProcessHandler::with_project_dir(temp.path().to_path_buf());
        let context = test_ctx_at(temp.path());
        let lock = temp.path().join(".git/index.lock");
        let event_log = EventLog::open(temp.path().join(".exo/logs")).unwrap();
        let request = RunRequest {
            command: git_shim.to_string_lossy().into_owned(),
            args: vec!["credential-canary-argument".into()],
            working_dir: "nested".into(),
            env: std::collections::HashMap::from([(
                "CREDENTIAL_CANARY_ENV".into(),
                "credential-canary-value".into(),
            )]),
            timeout_ms: 0,
        };
        let mut runner = tokio::spawn(async move { handler.run(request, &context).await });
        let process_id = tokio::select! {
            pid = wait_for_lock_proof(&event_log, &lock) => pid,
            result = &mut runner => panic!("Git process returned before lock proof: {result:?}"),
        };
        let before_abort = event_log.ledger().read_events().unwrap();
        let started = before_abort
            .iter()
            .find(|record| record.event.event_type == "git.process.started")
            .unwrap();
        let operation_hash = started.event.data["operation_id_sha256"]
            .as_str()
            .unwrap()
            .to_string();

        runner.abort();
        assert!(runner.await.unwrap_err().is_cancelled());
        assert!(assert_stopped_and_cleanup(process_id).await);

        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let records = loop {
            let records = event_log.ledger().read_events().unwrap();
            let finished = records.iter().filter(|record| {
                record.event.event_type == "git.process.finished"
                    && record.event.data["operation_id_sha256"] == operation_hash
            });
            if finished.count() == 1 {
                break records;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "cancelled Git process did not record one finished event"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        let finished: Vec<_> = records
            .iter()
            .filter(|record| {
                record.event.event_type == "git.process.finished"
                    && record.event.data["operation_id_sha256"] == operation_hash
            })
            .collect();
        assert_eq!(finished.len(), 1);
        assert_eq!(finished[0].event.data["outcome"], "cancelled");
        assert_eq!(finished[0].event.data["process_id"], process_id);
        assert_eq!(
            std::fs::read(worktree.join(".git/index")).unwrap(),
            saved_index,
            "cancellation must not commit a partial index update"
        );
        assert!(records.iter().any(|record| {
            record.event.event_type == "git.native_lock.held"
                && record.event.data["owner"]["pid"] == process_id
        }));
        assert!(
            !lock.exists(),
            "the reaped Git process should release its own lock"
        );
        assert!(command_directory.is_dir());
        let audit = records
            .iter()
            .map(|record| record.event.data.to_string())
            .collect::<String>();
        assert!(!audit.contains("credential-canary-argument"));
        assert!(!audit.contains("credential-canary-value"));
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn aborting_as_git_process_exits_records_one_terminal_event() {
        use crate::services::EventLog;
        use std::os::unix::fs::PermissionsExt;

        let temp = tempfile::tempdir().unwrap();
        let (git_shim, _fifo_guard) = init_git_index_lock_fixture(temp.path());
        let marker = temp.path().join("git-exit.marker");
        std::fs::write(
            &git_shim,
            format!(
                "#!/bin/sh\n: > {}\nexit 0\n",
                shell_quote(marker.to_str().unwrap())
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&git_shim).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&git_shim, permissions).unwrap();

        let handler = ProcessHandler::with_project_dir(temp.path().to_path_buf());
        let context = test_ctx_at(temp.path());
        let request = RunRequest {
            command: git_shim.to_string_lossy().into_owned(),
            args: Vec::new(),
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        };
        let runner = tokio::spawn(async move { handler.run(request, &context).await });
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while !marker.exists() {
            assert!(
                tokio::time::Instant::now() < deadline,
                "Git exit marker was not written"
            );
            tokio::task::yield_now().await;
        }
        runner.abort();
        match runner.await {
            Err(error) => assert!(error.is_cancelled()),
            Ok(result) => assert!(result.is_ok()),
        }

        let event_log = EventLog::open(temp.path().join(".exo/logs")).unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let records = loop {
            let records = event_log.ledger().read_events().unwrap();
            let terminal = records
                .iter()
                .filter(|record| record.event.event_type == "git.process.finished")
                .count();
            if terminal == 1 {
                break records;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "exiting Git process did not record its terminal event"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        };
        let finished: Vec<_> = records
            .iter()
            .filter(|record| record.event.event_type == "git.process.finished")
            .collect();
        assert_eq!(finished.len(), 1);
        assert!(matches!(
            finished[0].event.data["outcome"].as_str(),
            Some("exited" | "cancelled")
        ));
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(
            event_log
                .ledger()
                .read_events()
                .unwrap()
                .iter()
                .filter(|record| record.event.event_type == "git.process.finished")
                .count(),
            1,
            "cancellation/exit race must not duplicate the finish audit"
        );
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn normal_nonzero_exit_cleans_up_helpers_and_preserves_exit_code() {
        let temp = tempfile::tempdir().unwrap();
        let pid_file = temp.path().join("helper.pid");
        let handler = ProcessHandler::new();
        let ctx = test_ctx_at(temp.path());
        let req = RunRequest {
            command: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                format!("sleep 60 & echo $! > {}; exit 7", pid_file.display()),
            ],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 2000,
        };

        let response = handler.run(req, &ctx).await.unwrap();
        let pid = wait_for_pid_file(&pid_file).await;
        let stopped = assert_stopped_and_cleanup(pid).await;
        assert_eq!(response.exit_code, 7);
        assert!(stopped, "normal process exit left helper PID {pid} running");
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn timed_out_git_index_write_records_owner_and_recovers_without_changing_index() {
        use crate::services::{
            GitLockRecoveryReceipt, GitMutationResource, GitMutationSafety,
            NativeLockRecoveryVerdict,
        };

        let temp = tempfile::tempdir().unwrap();
        let (git_shim, _fifo_guard) = init_git_index_lock_fixture(temp.path());
        let worktree = temp.path().to_path_buf();
        let command_directory = worktree.join("nested");
        let handler = ProcessHandler::with_project_dir(temp.path().to_path_buf());
        let ctx = test_ctx_at(temp.path());
        let index = worktree.join(".git/index");
        let head = run_git_output(&worktree, &["rev-parse", "HEAD"]);
        let saved_index = std::fs::read(&index).unwrap();
        let lock = temp.path().join(".git/index.lock");
        let event_log = EventLog::open(temp.path().join(".exo/logs")).unwrap();
        let request = RunRequest {
            command: git_shim.to_string_lossy().into_owned(),
            args: vec!["credential-canary-argument".into()],
            working_dir: "nested".into(),
            env: std::collections::HashMap::from([(
                "CREDENTIAL_CANARY_ENV".into(),
                "credential-canary-value".into(),
            )]),
            timeout_ms: 1200,
        };
        let mut runner = tokio::spawn(async move { handler.run(request, &ctx).await });
        let started_pid = tokio::select! {
            pid = wait_for_lock_proof(&event_log, &lock) => pid,
            result = &mut runner => panic!(
                "Git process returned before lock proof was observed: {result:?}"
            ),
        };
        nix::sys::signal::kill(
            nix::unistd::Pid::from_raw(started_pid as i32),
            nix::sys::signal::Signal::SIGSTOP,
        )
        .unwrap();
        let error = runner.await.unwrap().unwrap_err();
        assert!(matches!(error, EffectError::Timeout { .. }));
        assert!(
            lock.exists(),
            "killed Git write must retain its native lock"
        );

        let records = event_log.ledger().read_events().unwrap();
        let started = records
            .iter()
            .find(|record| record.event.event_type == "git.process.started")
            .unwrap();
        let requested = records
            .iter()
            .find(|record| record.event.event_type == "git.process.requested")
            .unwrap();
        let finished = records
            .iter()
            .find(|record| record.event.event_type == "git.process.finished")
            .unwrap();
        let operation_hash = started.event.data["operation_id_sha256"].as_str().unwrap();
        assert_eq!(
            requested.event.data["operation_id_sha256"].as_str(),
            Some(operation_hash)
        );
        assert_eq!(
            finished.event.data["operation_id_sha256"].as_str(),
            Some(operation_hash)
        );
        assert_eq!(
            started.event.data["process_id"].as_u64(),
            Some(started_pid as u64)
        );
        assert!(started.event.data["process_group_id"].is_number());
        let owner = records
            .iter()
            .find(|record| record.event.event_type == "git.native_lock.held")
            .unwrap();
        assert_eq!(
            owner.event.data["owner"]["start_time_ticks"],
            started.event.data["process_start_identity"]
        );
        assert_eq!(
            owner.event.data["operation_id_sha256"],
            started.event.data["operation_id_sha256"]
        );
        assert_eq!(
            started.event.data["index_lock_file"].as_str(),
            Some(lock.to_string_lossy().as_ref())
        );
        assert_eq!(
            started.event.data["worktree_root"].as_str(),
            Some(worktree.canonicalize().unwrap().to_string_lossy().as_ref())
        );
        assert!(started.event.data.get("args").is_none());
        let audit = records
            .iter()
            .map(|record| record.event.data.to_string())
            .collect::<String>();
        assert!(!audit.contains("credential-canary-argument"));
        assert!(!audit.contains("credential-canary-value"));
        assert!(!audit.contains("CREDENTIAL_CANARY_ENV"));
        assert_eq!(
            owner.event.data["owner"]["pid"].as_u64(),
            Some(started_pid as u64)
        );
        assert!(records.iter().any(|record| {
            record.event.event_type == "git.process.finished"
                && record.event.data["outcome"] == "timed_out"
        }));

        let safety = GitMutationSafety::new(temp.path().to_path_buf());
        let leases = safety
            .acquire_set(
                temp.path(),
                &[GitMutationResource::Index],
                "test-index-recovery",
                Duration::ZERO,
            )
            .unwrap();
        let index_lease = leases
            .leases()
            .iter()
            .find(|lease| lease.resource == GitMutationResource::Index)
            .unwrap();
        let recovered = safety.reconcile_native_index_lock(index_lease, &event_log, Duration::ZERO);
        assert!(
            matches!(
                &recovered,
                NativeLockRecoveryVerdict::Recovered(GitLockRecoveryReceipt { .. })
            ),
            "unexpected native lock recovery verdict: {recovered:?}"
        );
        assert!(!lock.exists());
        assert_eq!(std::fs::read(&index).unwrap(), saved_index);
        assert_eq!(run_git_output(&worktree, &["rev-parse", "HEAD"]), head);
        assert!(command_directory.is_dir());
    }

    #[cfg(target_os = "linux")]
    async fn wait_for_lock_proof(event_log: &EventLog, lock: &std::path::Path) -> u32 {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        loop {
            if lock.exists() {
                let records = event_log.ledger().read_events().unwrap();
                if let Some(owner) = records
                    .iter()
                    .find(|record| record.event.event_type == "git.native_lock.held")
                {
                    return owner.event.data["owner"]["pid"].as_u64().unwrap() as u32;
                }
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "Git did not acquire and durably prove its native index lock"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    #[cfg(target_os = "linux")]
    async fn wait_for_pid_file(path: &std::path::Path) -> u32 {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(value) = std::fs::read_to_string(path) {
                if let Ok(pid) = value.trim().parse() {
                    return pid;
                }
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "helper did not start"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    #[cfg(target_os = "linux")]
    fn run_git_fixture(path: &std::path::Path, args: &[&str]) {
        let output = std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    #[cfg(target_os = "linux")]
    fn run_git_output(path: &std::path::Path, args: &[&str]) -> String {
        let output = std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .env_remove("GIT_DIR")
            .env_remove("GIT_WORK_TREE")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    }

    #[cfg(target_os = "linux")]
    fn init_git_index_lock_fixture(path: &std::path::Path) -> (PathBuf, std::fs::File) {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

        run_git_fixture(path, &["init", "-q", "-b", "main"]);
        run_git_fixture(path, &["config", "user.name", "process test"]);
        run_git_fixture(
            path,
            &["config", "user.email", "process-test@example.invalid"],
        );
        std::fs::write(path.join("tracked.txt"), "committed\n").unwrap();
        run_git_fixture(path, &["add", "tracked.txt"]);
        run_git_fixture(path, &["commit", "-q", "-m", "base"]);
        let worktree_subdir = path.join("nested");
        std::fs::create_dir(&worktree_subdir).unwrap();
        let fifo = worktree_subdir.join("index-info.fifo");
        nix::unistd::mkfifo(
            &fifo,
            nix::sys::stat::Mode::S_IRUSR | nix::sys::stat::Mode::S_IWUSR,
        )
        .unwrap();
        let fifo_guard = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(&fifo)
            .unwrap();
        let git = std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|directory| directory.join("git"))
            .find(|candidate| candidate.is_file())
            .expect("test environment has Git on PATH");
        let shim_directory = path.join("shim");
        std::fs::create_dir(&shim_directory).unwrap();
        let shim = shim_directory.join("git");
        std::fs::write(
            &shim,
            format!(
                "#!/bin/sh\nexec {} update-index --index-info < {}\n",
                shell_quote(git.to_string_lossy().as_ref()),
                shell_quote(fifo.to_string_lossy().as_ref()),
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&shim).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(&shim, permissions).unwrap();
        (shim, fifo_guard)
    }

    #[cfg(target_os = "linux")]
    fn shell_quote(value: &str) -> String {
        format!("'{}'", value.replace('\'', "'\\''"))
    }

    #[test]
    fn test_validate_working_dir_valid() {
        assert!(ProcessHandler::validate_working_dir(".").is_ok());
        assert!(ProcessHandler::validate_working_dir("src").is_ok());
        assert!(ProcessHandler::validate_working_dir("src/handlers").is_ok());
        assert!(ProcessHandler::validate_working_dir("./src").is_ok());
    }

    #[test]
    fn test_validate_working_dir_invalid() {
        assert!(ProcessHandler::validate_working_dir("/tmp").is_err());
        assert!(ProcessHandler::validate_working_dir("/").is_err());
        assert!(ProcessHandler::validate_working_dir("..").is_err());
        assert!(ProcessHandler::validate_working_dir("../foo").is_err());
        assert!(ProcessHandler::validate_working_dir("foo/../../bar").is_err());
    }

    #[test]
    fn direct_git_executable_names_are_recognized() {
        assert!(is_git_executable("git"));
        assert!(is_git_executable("/usr/bin/git"));
        assert!(is_git_executable("/Git/bin/Git.EXE"));
        assert!(is_git_executable("GIT.EXE"));
        assert!(!is_git_executable("/bin/sh"));
        assert!(!is_git_executable("my-git-wrapper"));
    }

    fn process_request(command: &str) -> RunRequest {
        RunRequest {
            command: command.into(),
            args: vec![],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 0,
        }
    }

    #[test]
    fn test_chainlink_db_env_points_to_project_db() {
        let project_dir = std::path::PathBuf::from("/repo");
        let handler = ProcessHandler::with_project_dir(project_dir.clone());
        let req = process_request("chainlink");

        assert_eq!(
            handler.chainlink_db_env(&req),
            Some(project_dir.join(".chainlink").join("issues.db"))
        );
    }

    #[test]
    fn test_chainlink_db_env_ignores_non_chainlink_commands() {
        let handler = ProcessHandler::with_project_dir(std::path::PathBuf::from("/repo"));
        let req = process_request("echo");

        assert_eq!(handler.chainlink_db_env(&req), None);
    }

    #[test]
    fn test_chainlink_db_env_preserves_explicit_env() {
        let handler = ProcessHandler::with_project_dir(std::path::PathBuf::from("/repo"));
        let mut req = process_request("chainlink");
        req.env.insert(
            CHAINLINK_DB_ENV.to_string(),
            "/custom/issues.db".to_string(),
        );

        assert_eq!(handler.chainlink_db_env(&req), None);
    }
}
