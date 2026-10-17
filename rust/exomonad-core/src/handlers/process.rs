//! Process execution effect handler for the `process.*` namespace.
//!
//! Runs arbitrary host commands with args, env, working dir, and optional timeout.
//! Working dir is validated to stay within the agent's worktree (no absolute paths,
//! no `..` traversal).

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

        let timeout = (req.timeout_ms > 0).then(|| Duration::from_millis(req.timeout_ms));
        let output = run_supervised(&mut cmd, timeout, &operation_id)
            .await
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
        ProcessRunError::Cleanup(reason) => EffectError::custom(
            "process_cleanup_unresolved",
            format!("{reason}; operation_id={operation_id}"),
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;

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
