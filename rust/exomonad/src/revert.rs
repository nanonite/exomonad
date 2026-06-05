use anyhow::{Context, Result};
use exomonad::config::Config;
use exomonad_core::services::{tmux_ipc::TmuxIpc, AgentType};
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

async fn remove_root_artifacts(project_dir: &Path, report: &mut RevertReport) {
    for path in [
        ".mcp.json",
        ".claude/settings.local.json",
        ".claude/rules/exomonad_role.md",
        "opencode.json",
        ".codex/config.toml",
        ".gemini/settings.json",
        ".exo/agents/root/opencode.json",
        ".exo/agents/root/.birth_branch",
    ] {
        remove_file_if_exists(&project_dir.join(path), report).await;
    }
}

async fn remove_companion_artifacts(config: &Config, report: &mut RevertReport) {
    for companion in &config.companions {
        let agent_type = companion.agent_type.unwrap_or(AgentType::Claude);
        if agent_type == AgentType::Claude {
            remove_companion_worktree(&config.project_dir, &companion.name, report).await;
        }

        let agent_dir = config.project_dir.join(".exo/agents").join(&companion.name);
        for file_name in [
            "routing.json",
            "settings.json",
            "opencode.json",
            ".birth_branch",
        ] {
            remove_file_if_exists(&agent_dir.join(file_name), report).await;
        }
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
    use exomonad::config::{CompanionConfig, ReviewerConfig};
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
            spawn_agent_type: AgentType::Codex,
            flake_ref: None,
            wasm_name: "devswarm".to_string(),
            extra_mcp_servers: HashMap::new(),
            initial_prompt: None,
            yolo: false,
            companions: vec![CompanionConfig {
                name: "buddy".to_string(),
                role: "worker".to_string(),
                agent_type: Some(AgentType::Gemini),
                command: "gemini".to_string(),
                task: None,
                model: None,
            }],
            root_command: None,
            otlp_endpoint: None,
            model: None,
            poll_interval: None,
            inbox_poke_interval: None,
            orphan_reconciler_interval_secs: None,
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
            ".gemini/settings.json",
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
