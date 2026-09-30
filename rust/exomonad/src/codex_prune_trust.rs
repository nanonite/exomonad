//! `exomonad codex-prune-trust`: inspect and reclaim historical ExoMonad Codex
//! hook-trust residue.
//!
//! This is an operator maintenance command, not part of any lifecycle path.
//! `init` and `spawn` never call it, so a running ExoMonad never removes trust
//! on its own initiative; an operator asks for the plan, reads it, and then
//! confirms the removal with `--apply`.
//!
//! It is deliberately not part of `exomonad clean`. `clean` is scoped to one
//! project's managed agent resources and runs through the authenticated control
//! API, while the residue this command reclaims lives in a Codex *user* config
//! that no project server owns — the operator's own `~/.codex/config.toml`, or a
//! `CODEX_HOME` left behind by an e2e run.

use anyhow::{Context, Result};
use clap::Args;
use exomonad_core::codex_config::{codex_user_config_path, HookTrustRemoval};
use exomonad_core::codex_trust_maintenance::{
    apply_codex_trust_prune, plan_codex_trust_prune, CodexTrustPrunePlan,
};
use exomonad_core::util::find_exomonad_binary;
use std::path::PathBuf;

#[derive(Args, Debug, Clone, PartialEq, Eq)]
pub(crate) struct CodexPruneTrustArgs {
    /// Codex user config to inspect. Defaults to $CODEX_HOME/config.toml, or
    /// ~/.codex/config.toml when CODEX_HOME is unset.
    #[arg(long, value_name = "PATH")]
    pub(crate) user_config: Option<PathBuf>,
    /// An ExoMonad binary whose canonical hook digests prove ownership of
    /// historical entries, spelled exactly as it was when it generated them.
    /// Repeat for every installation that may have written residue; the running
    /// ExoMonad binary is always included.
    #[arg(long = "expect-binary", value_name = "PATH")]
    pub(crate) expect_binary: Vec<PathBuf>,
    /// Remove the entries the plan proved. Without this flag nothing is written.
    #[arg(long)]
    pub(crate) apply: bool,
}

impl CodexPruneTrustArgs {
    /// The Codex user config to operate on.
    fn user_config_path(&self) -> Result<PathBuf> {
        match self.user_config.clone() {
            Some(path) => Ok(path),
            None => codex_user_config_path().context(
                "cannot resolve a Codex user config: CODEX_HOME is unset and no home directory \
                 is available. Pass --user-config with the path to inspect.",
            ),
        }
    }

    /// The ExoMonad binaries whose recomputed digests may prove ownership.
    ///
    /// The running ExoMonad comes first because it is the installation most
    /// likely to have written the residue; every explicit `--expect-binary`
    /// follows, deduplicated so the report never lists one binary twice.
    fn attested_binaries(&self) -> Vec<PathBuf> {
        let running = find_exomonad_binary();
        let mut attested = vec![running.clone()];
        for binary in &self.expect_binary {
            if *binary != running && !attested.contains(binary) {
                attested.push(binary.clone());
            }
        }
        attested
    }
}

pub(crate) fn run(args: CodexPruneTrustArgs) -> Result<()> {
    let user_config_path = args.user_config_path()?;
    let plan = plan_codex_trust_prune(&user_config_path, &args.attested_binaries()).with_context(
        || {
            format!(
                "cannot inspect ExoMonad Codex hook trust in {}",
                user_config_path.display()
            )
        },
    )?;
    println!("{}", render_plan(&plan, args.apply));
    if !args.apply {
        return Ok(());
    }
    let removal = apply_codex_trust_prune(&plan).with_context(|| {
        format!(
            "cannot remove ExoMonad Codex hook trust from {}",
            user_config_path.display()
        )
    })?;
    println!();
    println!("{}", render_removal(&removal));
    Ok(())
}

/// The plan an operator reads before anything is written: every exact candidate
/// key with the evidence that proved it, and every preserved key with the
/// reason it was gated.
pub(crate) fn render_plan(plan: &CodexTrustPrunePlan, apply: bool) -> String {
    let mut lines = vec![
        format!("Codex user config: {}", plan.user_config_path.display()),
        format!(
            "Mode: {}",
            if apply {
                "apply (removing proven entries)"
            } else {
                "dry-run (no entries were removed)"
            }
        ),
        format!(
            "Attested ExoMonad binaries ({}):",
            plan.attested_exomonad_binaries.len()
        ),
    ];
    lines.extend(
        plan.attested_exomonad_binaries
            .iter()
            .map(|binary| format!("  - {}", binary.display())),
    );
    lines.push(String::new());
    lines.push(format!(
        "Provable historical ExoMonad hook trust ({} entry/entries):",
        plan.proven.len()
    ));
    if plan.proven.is_empty() {
        lines.push("  none".to_string());
    }
    lines.extend(plan.proven.iter().map(|proven| format!("  {proven}")));
    lines.push(String::new());
    lines.push(format!("Preserved ({} entry/entries):", plan.gated.len()));
    if plan.gated.is_empty() {
        lines.push("  none".to_string());
    }
    lines.extend(plan.gated.iter().map(|gated| format!("  {gated}")));
    lines.push(summary(plan));
    if !apply && !plan.is_empty() {
        lines.push(format!(
            "Next: re-run with --apply to remove {} provable entry/entries.",
            plan.proven.len()
        ));
    }
    lines.join("\n")
}

fn summary(plan: &CodexTrustPrunePlan) -> String {
    format!(
        "Summary: {} [hooks.state] entry/entries scanned; {} provable; {} preserved; {} not in \
         ExoMonad's key shape and never inspected.",
        plan.scanned,
        plan.proven.len(),
        plan.gated.len(),
        plan.foreign
    )
}

/// What the confirmed apply actually did, including anything it re-checked under
/// the config lock and refused.
pub(crate) fn render_removal(removal: &HookTrustRemoval) -> String {
    let mut lines = vec![format!("Applied: {removal}")];
    lines.extend(
        removal
            .preserved
            .iter()
            .map(|preserved| format!("  preserved: {preserved}")),
    );
    if removal.removed.is_empty() {
        lines.push(
            "Nothing was removed: no provable historical ExoMonad hook trust remains, so running \
             this again is a no-op."
                .to_string(),
        );
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::{Parser, Subcommand};
    use exomonad_core::codex_trust_maintenance::{
        GatedHistoricalTrust, ProvenHistoricalTrust, TrustGateReason,
    };

    #[derive(Parser)]
    #[command(name = "exomonad")]
    struct TestCli {
        #[command(subcommand)]
        command: TestCommand,
    }

    #[derive(Subcommand)]
    enum TestCommand {
        CodexPruneTrust(CodexPruneTrustArgs),
    }

    fn parse(args: &[&str]) -> Result<CodexPruneTrustArgs, clap::error::Error> {
        TestCli::try_parse_from(args).map(|cli| match cli.command {
            TestCommand::CodexPruneTrust(args) => args,
        })
    }

    const PROVEN_KEY: &str = "/gone/repo/.exo/worktrees/w/.codex/config.toml:stop:0:0";
    const GATED_KEY: &str = "/gone/repo/.exo/worktrees/other/.codex/config.toml:stop:0:0";

    fn plan() -> CodexTrustPrunePlan {
        CodexTrustPrunePlan {
            user_config_path: PathBuf::from("/home/operator/.codex/config.toml"),
            attested_exomonad_binaries: vec![PathBuf::from("/usr/local/bin/exomonad")],
            scanned: 5,
            proven: vec![ProvenHistoricalTrust {
                key: PROVEN_KEY.to_string(),
                config_path: PathBuf::from("/gone/repo/.exo/worktrees/w/.codex/config.toml"),
                event_label: "stop".to_string(),
                exomonad_binary: PathBuf::from("/usr/local/bin/exomonad"),
                trusted_hash: "sha256:abc".to_string(),
            }],
            gated: vec![GatedHistoricalTrust {
                key: GATED_KEY.to_string(),
                reason: TrustGateReason::NoAttestedBinary {
                    recorded: "sha256:def".to_string(),
                },
            }],
            foreign: 3,
        }
    }

    #[test]
    fn defaults_to_a_dry_run() {
        let args = parse(&["exomonad", "codex-prune-trust"]).unwrap();
        assert!(!args.apply, "maintenance never writes without --apply");
        assert!(args.user_config.is_none());
        assert!(args.expect_binary.is_empty());
    }

    #[test]
    fn accepts_an_explicit_user_config_repeatable_binaries_and_apply() {
        let args = parse(&[
            "exomonad",
            "codex-prune-trust",
            "--user-config",
            "/tmp/codex/config.toml",
            "--expect-binary",
            "/opt/old/exomonad",
            "--expect-binary",
            "/opt/older/exomonad",
            "--apply",
        ])
        .unwrap();
        assert_eq!(
            args.user_config,
            Some(PathBuf::from("/tmp/codex/config.toml"))
        );
        assert_eq!(
            args.expect_binary,
            vec![
                PathBuf::from("/opt/old/exomonad"),
                PathBuf::from("/opt/older/exomonad")
            ]
        );
        assert!(args.apply);
    }

    #[test]
    fn attested_binaries_include_the_running_exomonad_exactly_once() {
        let running = find_exomonad_binary();
        let args = CodexPruneTrustArgs {
            user_config: None,
            expect_binary: vec![running.clone(), PathBuf::from("/opt/old/exomonad")],
            apply: false,
        };
        let attested = args.attested_binaries();
        assert_eq!(attested.first(), Some(&running));
        assert_eq!(
            attested.iter().filter(|binary| **binary == running).count(),
            1
        );
        assert_eq!(attested.last(), Some(&PathBuf::from("/opt/old/exomonad")));
    }

    #[test]
    fn dry_run_plan_names_every_candidate_key_and_reason() {
        let rendered = render_plan(&plan(), false);
        assert!(rendered.contains("Mode: dry-run (no entries were removed)"));
        assert!(rendered.contains("Codex user config: /home/operator/.codex/config.toml"));
        assert!(rendered.contains("Attested ExoMonad binaries (1):\n  - /usr/local/bin/exomonad"));
        assert!(rendered.contains(&format!(
            "{PROVEN_KEY}: config /gone/repo/.exo/worktrees/w/.codex/config.toml is gone and its \
             trusted_hash is the stop digest ExoMonad generates for /usr/local/bin/exomonad"
        )));
        assert!(rendered.contains(&format!(
            "{GATED_KEY}: no attested ExoMonad binary generates the recorded digest sha256:def"
        )));
        assert!(rendered.contains(
            "Summary: 5 [hooks.state] entry/entries scanned; 1 provable; 1 preserved; 3 not in \
             ExoMonad's key shape and never inspected."
        ));
        assert!(rendered.contains("Next: re-run with --apply to remove 1 provable entry/entries."));
    }

    #[test]
    fn apply_mode_does_not_ask_for_a_second_confirmation() {
        let rendered = render_plan(&plan(), true);
        assert!(rendered.contains("Mode: apply (removing proven entries)"));
        assert!(!rendered.contains("--apply"));
        assert!(rendered.contains("Provable historical ExoMonad hook trust (1 entry/entries):"));
    }

    #[test]
    fn an_empty_plan_asks_for_no_confirmation() {
        let mut empty = plan();
        empty.proven.clear();
        let rendered = render_plan(&empty, false);
        assert!(
            rendered.contains("Provable historical ExoMonad hook trust (0 entry/entries):\n  none")
        );
        assert!(rendered.contains("Preserved (1 entry/entries):"));
        assert!(!rendered.contains("--apply"));
    }

    #[test]
    fn a_removal_that_removed_nothing_reports_idempotence() {
        let rendered = render_removal(&HookTrustRemoval::default());
        assert!(rendered.contains("Applied: checked 0 ExoMonad Codex hook trust entries"));
        assert!(rendered.contains("running this again is a no-op"));
    }
}
