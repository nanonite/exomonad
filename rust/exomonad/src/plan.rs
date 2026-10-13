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
    /// Diagnose or restore RunStore plan bindings from the accepted snapshot.
    Reconcile {
        /// Validate and report drift and the required evidence fingerprint.
        #[arg(long)]
        dry_run: bool,
        /// Apply a verified restoration as a recoverable, audited transition.
        #[arg(long)]
        apply: bool,
        /// Required operator reason recorded in the reconciliation audit.
        #[arg(long)]
        reason: Option<String>,
        /// Fingerprint returned by the dry-run whose evidence is being approved.
        #[arg(long)]
        expected_fingerprint: Option<String>,
    },
}

pub(crate) async fn run(args: PlanArgs) -> Result<()> {
    let project_root = std::env::current_dir().context("failed to resolve project directory")?;
    match args.command {
        PlanCommands::Revise {
            dry_run,
            apply,
            reason,
        } => {
            validate_mode(dry_run, apply, reason.as_deref())?;
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
            print_output(output, "plan revision")?;
        }
        PlanCommands::Reconcile {
            dry_run,
            apply,
            reason,
            expected_fingerprint,
        } => {
            validate_reconcile_mode(
                dry_run,
                apply,
                reason.as_deref(),
                expected_fingerprint.as_deref(),
            )?;
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
            let output =
                run_python_reconciliation(&project_root, dry_run, reason, expected_fingerprint)
                    .await?;
            print_output(output, "plan reconciliation")?;
        }
    }
    Ok(())
}

fn print_output(output: std::process::Output, operation: &str) -> Result<()> {
    if !output.status.success() {
        let diagnostic = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        anyhow::bail!(
            "{operation} failed{}",
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

fn validate_reconcile_mode(
    dry_run: bool,
    apply: bool,
    reason: Option<&str>,
    fingerprint: Option<&str>,
) -> Result<()> {
    if dry_run == apply {
        anyhow::bail!("choose exactly one of --dry-run or --apply");
    }
    if apply {
        if reason
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .is_none()
        {
            anyhow::bail!("--apply requires a non-empty --reason");
        }
        if fingerprint.is_none() {
            anyhow::bail!("--apply requires --expected-fingerprint from a dry-run");
        }
    } else if reason.is_some() || fingerprint.is_some() {
        anyhow::bail!("--reason and --expected-fingerprint are only valid with --apply");
    }
    Ok(())
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

async fn run_python_reconciliation(
    project_root: &Path,
    dry_run: bool,
    reason: Option<String>,
    fingerprint: Option<String>,
) -> Result<std::process::Output> {
    let interpreter = crate::init::check_tl_loop_python(project_root)?;
    let package_root = crate::init::tl_loop_package_root()?;
    let mut command = Command::new(interpreter);
    command
        .arg(package_root)
        .arg("plan-reconciliation")
        .arg("--project-root")
        .arg(project_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if dry_run {
        command.arg("--dry-run");
    } else {
        let reason = reason.context("--apply requires a non-empty --reason")?;
        let fingerprint = fingerprint.context("--apply requires a dry-run fingerprint")?;
        command
            .arg("--apply")
            .arg("--reason")
            .arg(reason)
            .arg("--expected-fingerprint")
            .arg(fingerprint);
    }
    command
        .output()
        .await
        .context("failed to run TL plan reconciliation")
}

#[cfg(test)]
mod tests {
    use clap::Parser;

    use crate::Cli;

    use super::{validate_mode, validate_reconcile_mode};

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

    #[test]
    fn public_plan_reconcile_forms_parse() {
        assert!(Cli::try_parse_from(["exomonad", "plan", "reconcile", "--dry-run"]).is_ok());
        assert!(Cli::try_parse_from([
            "exomonad",
            "plan",
            "reconcile",
            "--apply",
            "--reason",
            "Restore accepted plan bindings",
            "--expected-fingerprint",
            &"a".repeat(64),
        ])
        .is_ok());
    }

    #[test]
    fn reconciliation_apply_requires_a_fingerprint() {
        assert!(validate_reconcile_mode(true, false, None, None).is_ok());
        assert!(
            validate_reconcile_mode(false, true, Some("restore"), Some(&"a".repeat(64))).is_ok()
        );
        assert!(validate_reconcile_mode(false, true, Some("restore"), None).is_err());
        assert!(validate_reconcile_mode(true, false, None, Some(&"a".repeat(64))).is_err());
    }
}
