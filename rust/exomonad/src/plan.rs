//! Operator commands for validating and applying TL plan revisions.

use anyhow::{Context, Result};
use clap::{Args, Subcommand};
use std::path::Path;
use std::process::Stdio;
use tokio::process::Command;

#[derive(Args)]
pub(crate) struct PlanArgs {
    #[command(subcommand)]
    command: PlanCommands,
}

#[derive(Subcommand)]
enum PlanCommands {
    /// Inspect or apply an audited revision to the active TL plan.
    Revise {
        /// Validate and report the candidate without writing state.
        #[arg(long)]
        dry_run: bool,
        /// Apply the candidate plan as a recoverable, audited transition.
        #[arg(long)]
        apply: bool,
        /// Required operator reason recorded in the durable audit log.
        #[arg(long)]
        reason: Option<String>,
    },
}

pub(crate) async fn run(args: PlanArgs) -> Result<()> {
    let PlanCommands::Revise {
        dry_run,
        apply,
        reason,
    } = args.command;
    validate_mode(dry_run, apply, reason.as_deref())?;
    let project_root = std::env::current_dir().context("failed to resolve project directory")?;
    let _lifecycle = if apply {
        Some(crate::init::acquire_init_lifecycle_lock(&project_root)?)
    } else {
        None
    };
    let _transition = if apply {
        Some(crate::init::acquire_plan_transition_lock(&project_root)?)
    } else {
        None
    };
    let output = run_python_revision(&project_root, dry_run, reason).await?;
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        anyhow::bail!(
            "plan revision failed{}",
            if diagnostic.is_empty() {
                String::new()
            } else {
                format!(": {diagnostic}")
            }
        );
    }
    print!("{}", String::from_utf8_lossy(&output.stdout));
    Ok(())
}

fn validate_mode(dry_run: bool, apply: bool, reason: Option<&str>) -> Result<()> {
    if dry_run == apply {
        anyhow::bail!("choose exactly one of --dry-run or --apply");
    }
    match (
        apply,
        reason.map(str::trim).filter(|value| !value.is_empty()),
    ) {
        (true, None) => anyhow::bail!("--apply requires a non-empty --reason"),
        (false, Some(_)) => anyhow::bail!("--reason is only valid with --apply"),
        _ => Ok(()),
    }
}

async fn run_python_revision(
    project_root: &Path,
    dry_run: bool,
    reason: Option<String>,
) -> Result<std::process::Output> {
    let interpreter = crate::init::check_tl_loop_python(project_root)?;
    let package_root = crate::init::tl_loop_package_root()?;
    let mut command = Command::new(interpreter);
    command
        .arg(package_root)
        .arg("plan-revision")
        .arg("--project-root")
        .arg(project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if dry_run {
        command.arg("--dry-run");
    } else {
        let reason = reason.context("--apply requires a non-empty --reason")?;
        command.arg("--apply").arg("--reason").arg(reason);
    }
    command
        .output()
        .await
        .context("failed to run TL plan revision")
}

#[cfg(test)]
mod tests {
    use clap::Parser;

    use crate::Cli;

    use super::validate_mode;

    #[test]
    fn public_plan_revise_forms_parse() {
        assert!(Cli::try_parse_from(["exomonad", "plan", "revise", "--dry-run"]).is_ok());
        assert!(Cli::try_parse_from([
            "exomonad",
            "plan",
            "revise",
            "--apply",
            "--reason",
            "Bind issue IDs",
        ])
        .is_ok());
    }

    #[test]
    fn revision_mode_requires_one_operation() {
        assert!(validate_mode(false, false, None).is_err());
        assert!(validate_mode(true, true, None).is_err());
        assert!(validate_mode(true, false, None).is_ok());
        assert!(validate_mode(false, true, Some("operator approved")).is_ok());
    }

    #[test]
    fn apply_requires_a_reason_and_dry_run_rejects_one() {
        assert!(validate_mode(false, true, None).is_err());
        assert!(validate_mode(false, true, Some("  ")).is_err());
        assert!(validate_mode(true, false, Some("not applicable")).is_err());
    }
}
