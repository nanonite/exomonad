//! Process execution effect handler for the `process.*` namespace.
//!
//! Runs arbitrary host commands with args, env, working dir, and optional timeout.
//! Working dir is validated to stay within the agent's worktree (no absolute paths,
//! no `..` traversal).

use super::git_process_audit::{
    is_git_executable, validate_git_target, validate_git_working_directory, GitProcessContext,
};
use super::process_supervision::{run_supervised, ProcessRunError};
use crate::effects::{
    dispatch_process_effect, EffectContext, EffectError, EffectResult, ProcessEffects,
};
use async_trait::async_trait;
use exomonad_proto::effects::process::*;
use std::path::{Component, PathBuf};
use std::time::Duration;

const CHAINLINK_DB_ENV: &str = "CHAINLINK_DB";
const CHAINLINK_TRACE_ENV: &str = "EXOMONAD_CHAINLINK_TRACE";

/// Process execution effect handler.
///
/// Executes commands within the agent's worktree. No command allowlist —
/// trusts WASM author. Working dir is confined to the agent's worktree root.
pub struct ProcessHandler {
    project_dir: PathBuf,
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

        let mut cmd = tokio::process::Command::new(&req.command);
        cmd.args(&req.args)
            .current_dir(&resolved_dir)
            .kill_on_drop(true);

        if !req.env.is_empty() {
            cmd.envs(&req.env);
        }
        if let Some(chainlink_db) = self.chainlink_db_env(&req) {
            cmd.env(CHAINLINK_DB_ENV, chainlink_db);
        }

        let git_process = if is_git_executable(&req.command) {
            validate_git_target(&req.args, &req.env)?;
            validate_git_working_directory(&ctx.working_dir, &resolved_dir, &req.env)?;
            Some(
                GitProcessContext::prepare(
                    &self.project_dir,
                    &ctx.working_dir,
                    &resolved_dir,
                    &req.env,
                    &operation_id,
                    ctx.agent_name.as_ref(),
                    ctx.birth_branch.as_str(),
                )
                .await?,
            )
        } else {
            None
        };
        let timeout = (req.timeout_ms > 0).then(|| Duration::from_millis(req.timeout_ms));
        let audit = git_process
            .as_ref()
            .map(GitProcessContext::audit)
            .transpose()?;
        let output = match run_supervised(&mut cmd, timeout, &operation_id, audit).await {
            Ok(output) => {
                if let Some(context) = &git_process {
                    context.record_finished("exited", Some(&output), output.status.code())?;
                }
                output
            }
            Err(error) => {
                if let Some(context) = &git_process {
                    context.record_finished(process_outcome(&error), None, None)?;
                }
                return Err(process_run_error(error, req.timeout_ms, &operation_id));
            }
        };
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
