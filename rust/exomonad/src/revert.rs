use anyhow::{Context, Result};
use exomonad::config::Config;
use exomonad_core::services::{
    agent_control::{codex_lifecycle, CapturedCodexTrust},
    tmux_ipc::TmuxIpc,
    AgentType,
};
use std::path::{Path, PathBuf};
use tokio::net::UnixStream;
use tokio::process::Command;

#[derive(Debug, Default)]
struct RevertReport {
    removed: Vec<PathBuf>,
    warnings: Vec<String>,
}

impl RevertReport {
    fn removed(&mut self, path: impl Into<PathBuf>) {
        self.removed.push(path.into());
    }

    fn warn(&mut self, message: impl Into<String>) {
        self.warnings.push(message.into());
    }

    fn print(&self) {
        if self.removed.is_empty() {
            println!("No exomonad init artifacts found.");
        } else {
            println!("Removed exomonad init artifacts:");
            for path in &self.removed {
                println!("  {}", path.display());
            }
        }

        for warning in &self.warnings {
            eprintln!("Warning: {warning}");
        }
    }
}

pub async fn run(config: &Config, kill_session: bool) -> Result<()> {
    let mut report = RevertReport::default();
    run_with_report(config, kill_session, &mut report).await?;
    report.print();
    Ok(())
}

async fn run_with_report(
    config: &Config,
    kill_session: bool,
    report: &mut RevertReport,
) -> Result<()> {
    let project_dir = &config.project_dir;

    remove_root_artifacts(project_dir, report).await;
    remove_companion_artifacts(config, report).await;
    remove_stale_sockets(project_dir, report).await;

    if kill_session {
        TmuxIpc::kill_session(&config.tmux_session)
            .await
            .with_context(|| format!("failed to kill tmux session {}", config.tmux_session))?;
        println!("Killed tmux session {}", config.tmux_session);
    }

    Ok(())
}

/// Claims the ExoMonad Codex hook trust an about-to-be-reverted directory holds.
///
/// Must run *before* the revert removes the generated `.codex/config.toml`:
/// that config is the only proof of which `[hooks.state]` keys ExoMonad owns, so
/// a capture after the removal would have nothing left to prove ownership with.
fn claim_reverted_codex_trust(
    report: &mut RevertReport,
    dirs: &[PathBuf],
    label: &str,
) -> Vec<CapturedCodexTrust> {
    let refs: Vec<&Path> = dirs.iter().map(PathBuf::as_path).collect();
    match codex_lifecycle::capture_codex_trust_for_disposal(&refs) {
        Ok(claimed) => claimed,
        Err(error) => {
            report.warn(format!(
                "failed to claim the ExoMonad Codex trust for {label}: {error}; the Codex user \
                 config is left untouched"
            ));
            Vec::new()
        }
    }
}

/// Releases a claim taken by [`claim_reverted_codex_trust`].
///
/// A release that cannot finish is a warning rather than a silent success: the
/// init artifacts are gone, so hook trust would otherwise outlive the agent with
/// nothing reporting it.
fn release_reverted_codex_trust(
    report: &mut RevertReport,
    claimed: &[CapturedCodexTrust],
    label: &str,
) {
    if claimed.is_empty() {
        return;
    }
    let batch = codex_lifecycle::release_captured_codex_trusts(claimed);
    if !batch.is_complete() {
        report.warn(format!(
            "{label} was reverted but its ExoMonad Codex trust could not be released: {}",
            batch.failures.join("; ")
        ));
    }
}

async fn remove_root_artifacts(project_dir: &Path, report: &mut RevertReport) {
    let claimed =
        claim_reverted_codex_trust(report, &[project_dir.to_path_buf()], "the project root");
    for path in [
        ".mcp.json",
        ".claude/settings.local.json",
        ".claude/rules/exomonad_role.md",
        "opencode.json",
        ".codex/config.toml",
        ".exo/agents/root/opencode.json",
        ".exo/agents/root/.birth_branch",
    ] {
        remove_file_if_exists(&project_dir.join(path), report).await;
    }
    release_reverted_codex_trust(report, &claimed, "the project root");
}

async fn remove_companion_artifacts(config: &Config, report: &mut RevertReport) {
    for companion in &config.companions {
        let agent_dir = config.project_dir.join(".exo/agents").join(&companion.name);
        let label = format!("companion {}", companion.name);
        let claimed = claim_reverted_codex_trust(report, std::slice::from_ref(&agent_dir), &label);
        let agent_type = companion.agent_type.unwrap_or(AgentType::Claude);
        if agent_type == AgentType::Claude {
            remove_companion_worktree(&config.project_dir, &companion.name, report).await;
        }

        for file_name in [
            "routing.json",
            "settings.json",
            "opencode.json",
            ".birth_branch",
            // A Codex companion's generated config is an init artifact like any
            // other: leaving it behind would keep Codex re-reading a config for a
            // companion this revert just removed.
            ".codex/config.toml",
        ] {
            remove_file_if_exists(&agent_dir.join(file_name), report).await;
        }
        release_reverted_codex_trust(report, &claimed, &label);
    }
}

async fn remove_companion_worktree(project_dir: &Path, name: &str, report: &mut RevertReport) {
    let worktree_path = project_dir.join(".exo/companions").join(name);
    if !worktree_path.exists() {
        return;
    }

    let output = Command::new("git")
        .args(["worktree", "remove", "--force"])
        .arg(&worktree_path)
        .current_dir(project_dir)
        .output()
        .await;

    match output {
        Ok(output) if output.status.success() => {
            report.removed(worktree_path);
        }
        Ok(output) => {
            report.warn(format!(
                "git worktree remove failed for {}: {}",
                worktree_path.display(),
                String::from_utf8_lossy(&output.stderr).trim()
            ));
            remove_dir_if_exists(&worktree_path, report).await;
        }
        Err(error) => {
            report.warn(format!(
                "failed to run git worktree remove for {}: {}",
                worktree_path.display(),
                error
            ));
            remove_dir_if_exists(&worktree_path, report).await;
        }
    }
}

async fn remove_stale_sockets(project_dir: &Path, report: &mut RevertReport) {
    let server_socket = project_dir.join(".exo/server.sock");
    if socket_alive(&server_socket).await {
        report.warn(format!(
            "server socket is live; leaving {} and .exo/sockets/ intact",
            server_socket.display()
        ));
        return;
    }

    remove_file_if_exists(&server_socket, report).await;
    remove_dir_if_exists(&project_dir.join(".exo/sockets"), report).await;
}

async fn socket_alive(path: &Path) -> bool {
    if !path.exists() {
        return false;
    }
    UnixStream::connect(path).await.is_ok()
}

async fn remove_file_if_exists(path: &Path, report: &mut RevertReport) {
    if !path.exists() {
        return;
    }

    match tokio::fs::remove_file(path).await {
        Ok(()) => report.removed(path.to_path_buf()),
        Err(error) => report.warn(format!("failed to remove {}: {}", path.display(), error)),
    }
}

async fn remove_dir_if_exists(path: &Path, report: &mut RevertReport) {
    if !path.exists() {
        return;
    }

    match tokio::fs::remove_dir_all(path).await {
        Ok(()) => report.removed(path.to_path_buf()),
        Err(error) => report.warn(format!("failed to remove {}: {}", path.display(), error)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad::config::{
        CompanionConfig, ReviewerConfig, DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS,
        DEFAULT_TL_DISPATCH_RETRY_BASE_DELAY_SECONDS, DEFAULT_TL_DISPATCH_RETRY_LIMIT,
        DEFAULT_TL_DISPATCH_RETRY_MAX_DELAY_SECONDS, DEFAULT_TL_TASK_TIMEOUT_SECONDS,
        DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS,
    };
    use exomonad_core::{services::AgentType, Role};
    use std::collections::HashMap;

    fn test_config(project_dir: PathBuf) -> Config {
        Config {
            project_dir: project_dir.clone(),
            role: Role::tl(),
            tmux_session: "exo-test".to_string(),
            port: 0,
            worktree_base: project_dir.join(".exo/worktrees"),
            shell_command: None,
            wasm_dir: project_dir.join(".exo/wasm"),
            root_agent_type: AgentType::Claude,
            tl_effort_level: exomonad::config::ResolvedEffort::MEDIUM_DEFAULT,
            spawn_agent_type: AgentType::Codex,
            worker_effort_level: exomonad::config::ResolvedEffort::MEDIUM_DEFAULT,
            reviewer_effort_level: exomonad::config::ResolvedEffort::MEDIUM_DEFAULT,
            flake_ref: None,
            wasm_name: "devswarm".to_string(),
            extra_mcp_servers: HashMap::new(),
            initial_prompt: None,
            yolo: false,
            companions: vec![CompanionConfig {
                name: "buddy".to_string(),
                role: "worker".to_string(),
                agent_type: Some(AgentType::Codex),
                command: "codex".to_string(),
                task: None,
                model: None,
            }],
            root_command: None,
            otlp_endpoint: None,
            model: None,
            poll_interval: None,
            inbox_poke_interval: None,
            orphan_reconciler_interval_secs: None,
            tl_transport_timeout_seconds: DEFAULT_TL_TRANSPORT_TIMEOUT_SECONDS,
            tl_active_tail_timeout_seconds: DEFAULT_TL_ACTIVE_TAIL_TIMEOUT_SECONDS,
            tl_task_timeout_seconds: DEFAULT_TL_TASK_TIMEOUT_SECONDS,
            tl_dispatch_retry_limit: DEFAULT_TL_DISPATCH_RETRY_LIMIT,
            tl_dispatch_retry_base_delay_seconds: DEFAULT_TL_DISPATCH_RETRY_BASE_DELAY_SECONDS,
            tl_dispatch_retry_max_delay_seconds: DEFAULT_TL_DISPATCH_RETRY_MAX_DELAY_SECONDS,
            tl_preflight_runtime_paths: Vec::new(),
            openrouter: Default::default(),
            opencode: Default::default(),
            opencode_as_tl: false,
            forgejo_url: None,
            forgejo_token: None,
            forgejo_reviewer_token: None,
            forgejo_webhook_secret: None,
            forgejo_ssh_port: None,
            reviewer: ReviewerConfig::default(),
        }
    }

    /// The ExoMonad Codex hook trust recorded in an isolated Codex home.
    fn hook_trust_entries(codex_home: &Path) -> usize {
        let raw = std::fs::read_to_string(codex_home.join("config.toml")).unwrap_or_default();
        toml::from_str::<toml::Value>(&raw)
            .ok()
            .and_then(|config| {
                config
                    .get("hooks")
                    .and_then(|hooks| hooks.get("state"))
                    .and_then(toml::Value::as_table)
                    .map(|state| state.len())
            })
            .unwrap_or(0)
    }

    /// Provisions Codex trust for one agent directory through the same lifecycle
    /// every ExoMonad Codex agent uses.
    fn provision_codex(codex_home: &Path, agent_dir: &Path, agent_name: &str, role: &str) {
        std::env::set_var("CODEX_HOME", codex_home);
        let extra_mcp_servers: HashMap<String, serde_json::Value> = HashMap::new();
        exomonad_core::services::agent_control::provision_codex_agent(
            &exomonad_core::services::agent_control::CodexAgentSpec {
                agent_dir,
                agent_name,
                role,
                role_context: Some("SENTINEL ROLE CONTEXT"),
                model: None,
                effort: None,
                extra_mcp_servers: &extra_mcp_servers,
                exomonad_binary: Path::new("/usr/local/bin/exomonad"),
            },
        )
        .expect("the Codex agent is provisioned");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn revert_removes_the_codex_config_and_the_trust_it_justified() {
        let codex_home = tempfile::tempdir().unwrap();
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let config = test_config(project_dir.clone());
        let root_config = project_dir.join(".codex/config.toml");
        let companion_dir = project_dir.join(".exo/agents/buddy");
        for path in [
            root_config.clone(),
            companion_dir.join(".codex/config.toml"),
        ] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        }
        std::fs::write(companion_dir.join("routing.json"), "init artifact").unwrap();
        provision_codex(codex_home.path(), &project_dir, "root", "dev");
        provision_codex(codex_home.path(), &companion_dir, "buddy", "worker");
        assert_eq!(
            hook_trust_entries(codex_home.path()),
            6,
            "the root config and the companion each carry three trust records"
        );

        let mut report = RevertReport::default();
        run_with_report(&config, false, &mut report).await.unwrap();

        assert!(!root_config.exists(), "revert removes the generated config");
        assert!(!companion_dir.join(".codex/config.toml").exists());
        assert_eq!(
            hook_trust_entries(codex_home.path()),
            0,
            "a reverted agent's hook trust must not outlive it"
        );
        assert!(
            report.warnings.is_empty(),
            "reverting a trusted Codex agent is not a partial failure: {:?}",
            report.warnings
        );
        std::env::remove_var("CODEX_HOME");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn revert_keeps_the_trust_of_agents_it_did_not_remove() {
        let codex_home = tempfile::tempdir().unwrap();
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let config = test_config(project_dir.clone());
        // A dormant owner's agent directory: revert has no reason to touch it, so
        // it must stay resumable with its trust intact.
        let dormant = project_dir.join(".exo/agents/dormant-codex");
        std::fs::create_dir_all(&dormant).unwrap();
        provision_codex(codex_home.path(), &dormant, "dormant-codex", "dev");
        assert_eq!(hook_trust_entries(codex_home.path()), 3);

        let mut report = RevertReport::default();
        run_with_report(&config, false, &mut report).await.unwrap();

        assert!(dormant.exists());
        assert!(dormant.join(".codex/config.toml").exists());
        assert_eq!(
            hook_trust_entries(codex_home.path()),
            3,
            "an owner revert did not dispose must keep its trust"
        );
        std::env::remove_var("CODEX_HOME");
    }

    #[tokio::test]
    async fn revert_removes_init_artifacts_and_keeps_project_data() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path().to_path_buf();
        let config = test_config(project_dir.clone());

        for path in [
            ".mcp.json",
            ".claude/settings.local.json",
            ".claude/rules/exomonad_role.md",
            ".codex/config.toml",
            ".exo/agents/root/opencode.json",
            ".exo/agents/root/.birth_branch",
            ".exo/agents/buddy/routing.json",
            ".exo/agents/buddy/settings.json",
            ".exo/agents/buddy/.birth_branch",
            ".exo/server.sock",
            ".exo/sockets/control.sock",
        ] {
            let full_path = project_dir.join(path);
            tokio::fs::create_dir_all(full_path.parent().unwrap())
                .await
                .unwrap();
            tokio::fs::write(full_path, "init artifact").await.unwrap();
        }
        tokio::fs::create_dir_all(project_dir.join(".chainlink"))
            .await
            .unwrap();
        tokio::fs::write(project_dir.join(".exo/config.toml"), "")
            .await
            .unwrap();
        tokio::fs::write(project_dir.join(".chainlink/issues.db"), "keep")
            .await
            .unwrap();

        let mut report = RevertReport::default();
        run_with_report(&config, false, &mut report).await.unwrap();

        assert!(!project_dir.join(".mcp.json").exists());
        assert!(!project_dir.join(".exo/agents/buddy/routing.json").exists());
        assert!(!project_dir.join(".exo/server.sock").exists());
        assert!(!project_dir.join(".exo/sockets").exists());
        assert!(project_dir.join(".exo/config.toml").exists());
        assert!(project_dir.join(".chainlink/issues.db").exists());
        assert!(report.removed.iter().any(|p| p.ends_with(".mcp.json")));
    }
}
