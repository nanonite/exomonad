//! Process execution effect handler for the `process.*` namespace.
//!
//! Runs arbitrary host commands with args, env, working dir, and optional timeout.
//! Working dir is validated to stay within the agent's worktree (no absolute paths,
//! no `..` traversal).

use crate::effects::{
    dispatch_process_effect, EffectContext, EffectError, EffectResult, ProcessEffects,
};
use async_trait::async_trait;
use exomonad_proto::effects::process::*;
use std::path::Component;
use std::time::Duration;

const CHAINLINK_TRACE_ENV: &str = "EXOMONAD_CHAINLINK_TRACE";

/// Process execution effect handler.
///
/// Executes commands within the agent's worktree. No command allowlist —
/// trusts WASM author. Working dir is confined to the agent's worktree root.
pub struct ProcessHandler;

impl Default for ProcessHandler {
    fn default() -> Self {
        Self
    }
}

impl ProcessHandler {
    pub fn new() -> Self {
        Self
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

    fn format_command_line(command: &str, args: &[String]) -> String {
        std::iter::once(command.to_string())
            .chain(args.iter().map(|arg| Self::quote_command_arg(arg)))
            .collect::<Vec<_>>()
            .join(" ")
    }

    fn quote_command_arg(arg: &str) -> String {
        if !arg.is_empty()
            && arg.chars().all(|c| {
                c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '/' | ':' | '=')
            })
        {
            return arg.to_string();
        }

        format!("'{}'", arg.replace('\'', "'\\''"))
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

        tracing::info!(
            command = %req.command,
            args = ?req.args,
            dir = %resolved_dir.display(),
            "[Process] run starting"
        );
        let trace_chainlink = Self::should_trace_chainlink(&req);
        if trace_chainlink {
            tracing::info!(
                chainlink_command = %Self::format_command_line(&req.command, &req.args),
                args = ?req.args,
                dir = %resolved_dir.display(),
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

        let output_result = if req.timeout_ms > 0 {
            let timeout_dur = Duration::from_millis(req.timeout_ms);
            match tokio::time::timeout(timeout_dur, cmd.output()).await {
                Ok(res) => res,
                Err(_) => {
                    return Err(EffectError::Timeout {
                        message: format!(
                            "process.run timed out after {}ms: {} {}",
                            req.timeout_ms,
                            req.command,
                            req.args.join(" ")
                        ),
                    });
                }
            }
        } else {
            cmd.output().await
        };

        match output_result {
            Ok(output) => {
                let exit_code = output.status.code().unwrap_or(-1);
                let stdout = String::from_utf8_lossy(&output.stdout).into_owned();
                let stderr = String::from_utf8_lossy(&output.stderr).into_owned();

                tracing::info!(exit_code, "[Process] run completed");
                if trace_chainlink {
                    tracing::info!(
                        exit_code,
                        chainlink_command = %Self::format_command_line(&req.command, &req.args),
                        "[Chainlink] command completed"
                    );
                }

                Ok(RunResponse {
                    exit_code,
                    stdout,
                    stderr,
                })
            }
            Err(e) => Err(EffectError::custom(
                "process_error",
                format!("Failed to execute: {}", e),
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;

    fn test_ctx() -> EffectContext {
        EffectContext {
            agent_name: AgentName::from("test"),
            birth_branch: BirthBranch::from("main"),
            working_dir: std::path::PathBuf::from("."),
        }
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
            command: "sleep".into(),
            args: vec!["60".into()],
            working_dir: ".".into(),
            env: std::collections::HashMap::new(),
            timeout_ms: 100,
        };

        let err = handler.run(req, &ctx).await.unwrap_err();
        assert!(matches!(err, EffectError::Timeout { .. }));
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
    fn test_format_command_line_quotes_spaced_args() {
        let args = vec![
            "timer".to_string(),
            "start".to_string(),
            "Issue with spaces".to_string(),
        ];

        assert_eq!(
            ProcessHandler::format_command_line("chainlink", &args),
            "chainlink timer start 'Issue with spaces'"
        );
    }
}
