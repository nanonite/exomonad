//! Local Git native-lock inspection and explicit legacy recovery.

use anyhow::{Context, Result};
use clap::{Args, Subcommand};
use exomonad_core::services::{
    apply_legacy_git_lock, inspect_legacy_git_lock, EventLog, GitMutationSafety,
    LegacyGitLockInspection, LegacyManagedActivity,
};
use std::path::{Path, PathBuf};
use std::time::Duration;

const LEGACY_APPLY_WAIT: Duration = Duration::from_secs(2);

#[derive(Args, Debug, Clone, PartialEq, Eq)]
pub(crate) struct GitLockArgs {
    #[command(subcommand)]
    command: GitLockCommands,
}

#[derive(Subcommand, Debug, Clone, PartialEq, Eq)]
enum GitLockCommands {
    /// Inspect the active Git index lock without writing or requiring the server.
    Inspect {
        /// Git worktree root to inspect.
        #[arg(long, value_name = "PATH", default_value = ".")]
        project_root: PathBuf,
    },
    /// Quarantine the exact legacy lock approved by a previous inspect fingerprint.
    Apply {
        /// Git worktree root to modify.
        #[arg(long, value_name = "PATH", default_value = ".")]
        project_root: PathBuf,
        /// SHA-256 fingerprint printed by `exomonad git-lock inspect`.
        #[arg(long, value_name = "SHA256", required = true)]
        expected_fingerprint: String,
        /// Confirm that every writer to this repository has been stopped.
        #[arg(long, required = true)]
        writers_stopped: bool,
    },
}

pub(crate) fn run(args: GitLockArgs) -> Result<()> {
    match args.command {
        GitLockCommands::Inspect { project_root } => inspect(project_root),
        GitLockCommands::Apply {
            project_root,
            expected_fingerprint,
            writers_stopped,
        } => apply(project_root, &expected_fingerprint, writers_stopped),
    }
}

fn inspect(project_root: PathBuf) -> Result<()> {
    let safety = GitMutationSafety::new(project_root.clone());
    let paths = safety
        .resolve_paths_with_index_override(&project_root, None)
        .context("cannot resolve canonical Git lock identity")?;
    let ledger_segments = paths.worktree_root.join(".exo/ledger/segments");
    let report = inspect_legacy_git_lock(&safety, &project_root, &ledger_segments)
        .map_err(anyhow::Error::msg)
        .context("cannot inspect legacy Git lock evidence")?;
    println!("{}", render_inspection(&report)?);
    match &report.lock {
        None => println!("No native index lock is present. Retry `exomonad init --continue` when ready."),
        Some(_) if report.matching_managed_proof => {
            anyhow::bail!("managed ownership evidence exists; retry `exomonad init --continue` for proof-backed recovery")
        }
        Some(_) => match &report.managed_activity {
            LegacyManagedActivity::Clear { .. } => {
                let fingerprint = report.recovery_fingerprint.as_deref().context("inspection has no recovery fingerprint")?;
                println!("Operator action: stop all repository Git writers, then apply this exact fingerprint with --writers-stopped.");
                println!("{}", apply_command(&report.worktree_root, fingerprint));
            }
            LegacyManagedActivity::Active { .. } => anyhow::bail!("an active recorded ExoMonad process blocks recovery; wait for it to finish and inspect again"),
            LegacyManagedActivity::Uncertain { ref reason } => anyhow::bail!("relevant managed-process evidence is uncertain: {reason}"),
        },
    }
    Ok(())
}

fn apply(project_root: PathBuf, fingerprint: &str, writers_stopped: bool) -> Result<()> {
    let safety = GitMutationSafety::new(project_root.clone());
    let paths = safety
        .resolve_paths_with_index_override(&project_root, None)
        .context("cannot resolve canonical Git lock identity")?;
    let event_log = EventLog::open(paths.worktree_root.join(".exo/logs"))
        .context("cannot open the local recovery audit ledger")?;
    let result = apply_legacy_git_lock(
        &safety,
        &project_root,
        &event_log,
        fingerprint,
        writers_stopped,
        LEGACY_APPLY_WAIT,
    )
    .map_err(anyhow::Error::msg)
    .context("legacy Git lock recovery was not applied")?;
    println!(
        "Legacy Git lock recovered: {}",
        result.receipt.receipt_event_id
    );
    println!(
        "Quarantined exact lock: {}",
        result.receipt.quarantine_file.display()
    );
    println!(
        "Recovery fingerprint: {}",
        result.receipt.recovery_fingerprint
    );
    println!("Already applied: {}", result.already_applied);
    println!("Next: exomonad init --continue");
    Ok(())
}

fn render_inspection(report: &LegacyGitLockInspection) -> Result<String> {
    serde_json::to_string_pretty(report).context("cannot render Git lock inspection")
}

fn apply_command(worktree: &Path, fingerprint: &str) -> String {
    let path = shell_quote(&worktree.to_string_lossy());
    format!(
        "exomonad git-lock apply --project-root {path} --expected-fingerprint {fingerprint} --writers-stopped"
    )
}

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::Parser;

    #[derive(Parser)]
    struct TestCli {
        #[command(subcommand)]
        command: TestCommand,
    }

    #[derive(Subcommand)]
    enum TestCommand {
        GitLock(GitLockArgs),
    }

    #[test]
    fn legacy_apply_requires_fingerprint_and_explicit_writer_authorization() {
        assert!(TestCli::try_parse_from(["exomonad", "git-lock", "apply"]).is_err());
        let fingerprint = "a".repeat(64);
        assert!(TestCli::try_parse_from([
            "exomonad",
            "git-lock",
            "apply",
            "--expected-fingerprint",
            &fingerprint,
        ])
        .is_err());
        assert!(TestCli::try_parse_from([
            "exomonad",
            "git-lock",
            "apply",
            "--expected-fingerprint",
            &fingerprint,
            "--writers-stopped",
        ])
        .is_ok());
    }

    #[test]
    fn inspection_displays_a_copyable_apply_command_for_shell_sensitive_paths() {
        let command = apply_command(Path::new("/tmp/a repo/it’s\'worktree"), &"b".repeat(64));
        assert!(command.contains("--writers-stopped"));
        assert!(command.contains("'/tmp/a repo/it’s'\\''worktree'"));
    }
}
