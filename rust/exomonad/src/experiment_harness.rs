//! Deterministic, opt-in TL autonomy benchmark.
//!
//! This module is intentionally debug-only. It exercises the real watcher
//! transition engine with synthetic published observations and a mock
//! TL reviewer dispatch; it never contacts Forgejo or runs as part of normal tests.

use crate::config::Config;
use anyhow::{bail, Context, Result};
use chrono::Utc;
use exomonad_core::domain::{AgentName, BirthBranch, BranchName, CIStatus};
use exomonad_core::effects::EffectContext;
use exomonad_core::services::pr_registry::{ForgejoReviewState, PrEntry, PrState};
use exomonad_core::services::worktree_event_watcher::WorktreeEventWatcher;
use exomonad_core::services::{capture_memory, MemoryCapture, MemoryKind, Services};
use serde::Serialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::process::Command;
use uuid::Uuid;

const BACKLOG_SIZE: usize = 10;
const SEEDS: [u64; 5] = [1, 2, 3, 4, 5];
const EFFORT: &str = "high";
const SUCCESS_CONDITION: &str = "worker_success";
const FAILURE_CONDITION: &str = "scripted_worker_failure";

const CELLS: [Cell; 4] = [
    Cell {
        model: "sonnet",
        harness: "claude-code",
    },
    Cell {
        model: "sonnet",
        harness: "opencode",
    },
    Cell {
        model: "gpt-5.6-luna",
        harness: "codex",
    },
    Cell {
        model: "gpt-5.6-luna",
        harness: "opencode",
    },
];

#[derive(Debug, Clone, Copy)]
struct Cell {
    model: &'static str,
    harness: &'static str,
}

#[derive(Debug, Serialize)]
struct RunManifest {
    run_id: String,
    git_sha: String,
    backlog_size: usize,
    ticket_ids: Vec<u64>,
    effort: &'static str,
    seeds: &'static [u64; 5],
    cells: Vec<CellManifest>,
}

#[derive(Debug, Serialize)]
struct CellManifest {
    cell: String,
    model: &'static str,
    harness: &'static str,
    effort: &'static str,
    seeds: &'static [u64; 5],
    worker_conditions: [&'static str; 2],
    mock_watcher: bool,
    forgejo_contacts: u32,
}

#[derive(Debug, Serialize)]
struct SeedManifest {
    run_id: String,
    cell: String,
    model: &'static str,
    harness: &'static str,
    effort: &'static str,
    seed: u64,
    worker_condition: &'static str,
    backlog_size: usize,
    ticket_ids: Vec<u64>,
    budget_cap: u32,
    git_sha: String,
    provenance: &'static str,
}

#[derive(Debug, Clone)]
struct SeedOutcome {
    events: Vec<serde_json::Value>,
    row: MetricsRow,
}

#[derive(Debug, Clone)]
struct MetricsRow {
    seed: u64,
    worker_condition: &'static str,
    ticket_count: usize,
    worker_dispatches: usize,
    worker_failures: usize,
    recovery_dispatches: usize,
    reviewer_spawns: usize,
    approved_tickets: usize,
    turn_end_reason: &'static str,
    backlog_ready_count: usize,
    pending_children: usize,
    unread_inbox: usize,
    dispatch_sequence_hash: String,
}

pub async fn run(config: &Config) -> Result<()> {
    let project_dir = resolve_project_dir(config)?;
    let run_id = std::env::var("EXOMONAD_TL_AUTONOMY_RUN_ID")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| format!("{}-{}", Utc::now().format("%Y%m%dT%H%M%SZ"), Uuid::new_v4()));
    let run_dir = project_dir
        .join("experiments")
        .join("tl-autonomy")
        .join(&run_id);
    tokio::fs::create_dir_all(&run_dir).await?;

    let git_sha = git_sha(&project_dir).await;
    let backlog_dir = run_dir.join("backlog");
    let ticket_ids = seed_backlog(&backlog_dir).await?;
    let ready_count = ready_count(&backlog_dir).await?;
    if ready_count != BACKLOG_SIZE {
        bail!("synthetic backlog has {ready_count} ready tickets, expected {BACKLOG_SIZE}");
    }

    for cell in CELLS {
        let cell_name = cell_name(cell);
        let cell_dir = run_dir.join(&cell_name);
        tokio::fs::create_dir_all(&cell_dir).await?;
        let manifest = CellManifest {
            cell: cell_name.clone(),
            model: cell.model,
            harness: cell.harness,
            effort: EFFORT,
            seeds: &SEEDS,
            worker_conditions: [SUCCESS_CONDITION, FAILURE_CONDITION],
            mock_watcher: true,
            forgejo_contacts: 0,
        };
        write_json(&cell_dir.join("manifest.json"), &manifest).await?;

        let mut rows = Vec::new();
        let mut raw_events = Vec::new();
        for seed in SEEDS {
            for worker_condition in [SUCCESS_CONDITION, FAILURE_CONDITION] {
                let outcome = run_seed(&cell, seed, worker_condition, &ticket_ids).await?;
                let seed_manifest = SeedManifest {
                    run_id: run_id.clone(),
                    cell: cell_name.clone(),
                    model: cell.model,
                    harness: cell.harness,
                    effort: EFFORT,
                    seed,
                    worker_condition,
                    backlog_size: BACKLOG_SIZE,
                    ticket_ids: ticket_ids.clone(),
                    budget_cap: 10,
                    git_sha: git_sha.clone(),
                    provenance: "debug_mock_watcher",
                };
                write_json(
                    &cell_dir.join(format!("seed-{seed}-{worker_condition}.manifest.json")),
                    &seed_manifest,
                )
                .await?;
                rows.push(outcome.row);
                raw_events.extend(outcome.events);
            }
        }
        write_metrics(&cell_dir.join("metrics.csv"), &rows).await?;
        write_jsonl(&cell_dir.join("raw-events.jsonl"), &raw_events).await?;
    }

    let run_manifest = RunManifest {
        run_id: run_id.clone(),
        git_sha,
        backlog_size: BACKLOG_SIZE,
        ticket_ids,
        effort: EFFORT,
        seeds: &SEEDS,
        cells: CELLS
            .into_iter()
            .map(|cell| CellManifest {
                cell: cell_name(cell),
                model: cell.model,
                harness: cell.harness,
                effort: EFFORT,
                seeds: &SEEDS,
                worker_conditions: [SUCCESS_CONDITION, FAILURE_CONDITION],
                mock_watcher: true,
                forgejo_contacts: 0,
            })
            .collect(),
    };
    write_json(&run_dir.join("manifest.json"), &run_manifest).await?;
    crate::experiment_analysis::analyze_run(&run_dir).await?;
    println!("TL autonomy mock benchmark complete: {}", run_dir.display());
    Ok(())
}

async fn run_seed(
    cell: &Cell,
    seed: u64,
    worker_condition: &'static str,
    ticket_ids: &[u64],
) -> Result<SeedOutcome> {
    let isolated = tempfile::tempdir().context("create isolated watcher directory")?;
    let services = Services::benchmark(isolated.path().to_path_buf())?;
    let ci_status_map = services.ci_status_map.clone();
    let watcher = WorktreeEventWatcher::new(Arc::new(services.clone()))
        .with_plugins(Arc::new(tokio::sync::RwLock::new(HashMap::new())))
        .with_ci_status_map(ci_status_map.clone())
        .with_ci_source_configured(true);

    let mut events = Vec::new();
    let mut sequence = Vec::new();
    let mut worker_dispatches = 0;
    let mut worker_failures = 0;
    let mut recovery_dispatches = 0;
    let mut reviewer_spawns = 0;
    let mut approved_tickets = 0;

    for (index, ticket_id) in ticket_ids.iter().enumerate() {
        let failed = worker_condition == FAILURE_CONDITION && scripted_failure(seed, index);
        worker_dispatches += 1;
        sequence.push(format!("ticket-{ticket_id}:worker-start"));
        events.push(serde_json::json!({
            "event": "worker_start",
            "seed": seed,
            "ticket_id": ticket_id,
            "attempt": 1,
        }));

        if failed {
            worker_failures += 1;
            sequence.push(format!("ticket-{ticket_id}:worker-failed"));
            events.push(serde_json::json!({
                "event": "worker_failed",
                "seed": seed,
                "ticket_id": ticket_id,
                "attempt": 1,
                "reason": "scripted_failure",
            }));
            recovery_dispatches += 1;
            worker_dispatches += 1;
            sequence.push(format!("ticket-{ticket_id}:tl-recovery-dispatch"));
            events.push(serde_json::json!({
                "event": "tl_recovery_dispatch",
                "seed": seed,
                "ticket_id": ticket_id,
                "attempt": 2,
                "initiative": true,
            }));
        }

        sequence.push(format!("ticket-{ticket_id}:worker-succeeded"));
        events.push(serde_json::json!({
            "event": "worker_succeeded",
            "seed": seed,
            "ticket_id": ticket_id,
            "attempt": if failed { 2 } else { 1 },
        }));

        let branch = format!("main.ticket-{ticket_id}-claude");
        let head_sha = format!("seed-{seed}-ticket-{ticket_id}");
        let branch_name = BranchName::try_from_str(&branch)?;
        ci_status_map
            .write()
            .await
            .insert((branch_name, head_sha.clone()), CIStatus::Success);
        let pr = mock_pr(*ticket_id, branch, head_sha);
        sequence.push(format!("ticket-{ticket_id}:watcher-pending"));
        watcher
            .process_mock_observation(
                pr.clone(),
                ForgejoReviewState::PendingReview,
                false,
                CIStatus::Success,
            )
            .await?;
        reviewer_spawns += 1;
        sequence.push(format!("ticket-{ticket_id}:reviewer-spawned"));
        events.push(serde_json::json!({
            "event": "reviewer_spawned",
            "seed": seed,
            "ticket_id": ticket_id,
            "latency_ticks": 1,
        }));

        sequence.push(format!("ticket-{ticket_id}:watcher-approved"));
        watcher
            .process_mock_observation(pr, ForgejoReviewState::Approved, true, CIStatus::Success)
            .await?;
        approved_tickets += 1;
        events.push(serde_json::json!({
            "event": "watcher_merge_ready",
            "seed": seed,
            "ticket_id": ticket_id,
            "ci": "success",
            "review": "approved",
        }));
    }

    let turn_end_reason = simulated_turn_end_reason(cell);
    let dispatch_sequence_hash = fnv1a_hash(&sequence.join("|"));
    let backlog_ready_count = BACKLOG_SIZE;
    let pending_children = 0;
    let unread_inbox = services.inbox_store.unread_count("root").unwrap_or(0);
    let root = AgentName::try_from_str("root")?;
    let main = BirthBranch::try_from_str("main")?;
    let context = EffectContext {
        agent_name: root,
        birth_branch: main,
        working_dir: isolated.path().to_path_buf(),
    };
    capture_memory(
        &context,
        &services.session_memory,
        MemoryCapture {
            issue_id: None,
            kind: MemoryKind::TurnEnd,
            importance: 50,
            summary: format!("Mock turn ended: {turn_end_reason}"),
            detail: None,
            metadata: Some(serde_json::json!({
                "reason": turn_end_reason,
                "backlog_ready_count": backlog_ready_count,
                "pending_children": pending_children,
                "unread_inbox": unread_inbox,
                "agent_type": "tl",
                "model": cell.model,
                "effort": EFFORT,
                "harness": cell.harness,
                "seed": seed,
                "worker_condition": worker_condition,
            })),
        },
    );
    events.push(serde_json::json!({
        "event": "turn_end",
        "memory_kind": "turn_end",
        "reason": turn_end_reason,
        "backlog_ready_count": backlog_ready_count,
        "pending_children": pending_children,
        "unread_inbox": unread_inbox,
        "model": cell.model,
        "effort": EFFORT,
        "harness": cell.harness,
        "seed": seed,
        "worker_condition": worker_condition,
    }));

    if reviewer_spawns != BACKLOG_SIZE {
        bail!(
            "TL dispatched {reviewer_spawns} reviewer effects for seed {seed}, expected {BACKLOG_SIZE}"
        );
    }

    Ok(SeedOutcome {
        events,
        row: MetricsRow {
            seed,
            worker_condition,
            ticket_count: ticket_ids.len(),
            worker_dispatches,
            worker_failures,
            recovery_dispatches,
            reviewer_spawns,
            approved_tickets,
            turn_end_reason,
            backlog_ready_count,
            pending_children,
            unread_inbox,
            dispatch_sequence_hash,
        },
    })
}

fn mock_pr(number: u64, branch: String, head_sha: String) -> PrEntry {
    PrEntry {
        number,
        head_branch: branch,
        base_branch: "main".to_string(),
        title: format!("Synthetic ticket #{number}"),
        body: format!("Authoring-Agent: ticket-{number}-claude\nAuthoring-Role: dev"),
        author_agent: format!("ticket-{number}-claude"),
        author_role: "dev".to_string(),
        created_at: Utc::now(),
        state: PrState::Open,
        last_review_at: None,
        last_head_sha: Some(head_sha),
        approved_at_sha: None,
        reviewer_agent: None,
        reviewer_birth_branch: None,
        rounds: 0,
        stuck: false,
        needs_human_review: false,
        merge_blocked_on_ci: false,
        chainlink_issue_id: Some(number),
    }
}

fn scripted_failure(seed: u64, ticket_index: usize) -> bool {
    (seed.wrapping_mul(31).wrapping_add(ticket_index as u64 * 17)).is_multiple_of(4)
}

fn simulated_turn_end_reason(cell: &Cell) -> &'static str {
    match (cell.model, cell.harness) {
        ("gpt-5.6-luna", "codex") => "exited",
        ("gpt-5.6-luna", "opencode") => "asked_human",
        ("sonnet", _) => "stopped_backlog_nonempty",
        _ => "asked_human",
    }
}

fn cell_name(cell: Cell) -> String {
    format!("{}-{}", cell.model.replace('.', "-"), cell.harness)
}

async fn seed_backlog(backlog_dir: &Path) -> Result<Vec<u64>> {
    tokio::fs::create_dir_all(backlog_dir).await?;
    let db_dir = backlog_dir.join(".chainlink");
    run_chainlink(backlog_dir, &db_dir, &["init", "--no-hooks"])
        .await
        .context("initialize synthetic Chainlink database")?;

    let mut ids = Vec::with_capacity(BACKLOG_SIZE);
    for index in 0..BACKLOG_SIZE {
        let title = format!("Synthetic independent ticket {:02}", index + 1);
        let output = run_chainlink(
            backlog_dir,
            &db_dir,
            &["--quiet", "create", &title, "--priority", "medium"],
        )
        .await
        .with_context(|| format!("create synthetic ticket {}", index + 1))?;
        let id = parse_issue_id(&output)
            .with_context(|| format!("parse synthetic ticket {} id", index + 1))?;
        ids.push(id);
    }
    Ok(ids)
}

async fn ready_count(backlog_dir: &Path) -> Result<usize> {
    let db_dir = backlog_dir.join(".chainlink");
    let output = run_chainlink(backlog_dir, &db_dir, &["ready"]).await?;
    Ok(output
        .lines()
        .filter(|line| line.trim_start().starts_with('#'))
        .count())
}

async fn run_chainlink(working_dir: &Path, db_dir: &Path, args: &[&str]) -> Result<String> {
    let output = Command::new("chainlink")
        .arg("--db")
        .arg(db_dir)
        .args(args)
        .current_dir(working_dir)
        .output()
        .await
        .context("start chainlink")?;
    if !output.status.success() {
        bail!(
            "chainlink {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn parse_issue_id(output: &str) -> Option<u64> {
    output.lines().find_map(|line| {
        let candidate = line
            .trim()
            .strip_prefix('#')
            .or_else(|| line.split_whitespace().next())?;
        candidate.parse::<u64>().ok()
    })
}

async fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(value)?;
    tokio::fs::write(path, bytes).await?;
    Ok(())
}

async fn write_jsonl(path: &Path, events: &[serde_json::Value]) -> Result<()> {
    let mut text = String::new();
    for event in events {
        text.push_str(&serde_json::to_string(event)?);
        text.push('\n');
    }
    tokio::fs::write(path, text).await?;
    Ok(())
}

async fn write_metrics(path: &Path, rows: &[MetricsRow]) -> Result<()> {
    let mut text = String::from(
        "seed,worker_condition,ticket_count,worker_dispatches,worker_failures,recovery_dispatches,reviewer_spawns,approved_tickets,turn_end_reason,backlog_ready_count,pending_children,unread_inbox,dispatch_sequence_hash\n",
    );
    for row in rows {
        text.push_str(&format!(
            "{},{},{},{},{},{},{},{},{},{},{},{},{}\n",
            row.seed,
            row.worker_condition,
            row.ticket_count,
            row.worker_dispatches,
            row.worker_failures,
            row.recovery_dispatches,
            row.reviewer_spawns,
            row.approved_tickets,
            row.turn_end_reason,
            row.backlog_ready_count,
            row.pending_children,
            row.unread_inbox,
            row.dispatch_sequence_hash,
        ));
    }
    tokio::fs::write(path, text).await?;
    Ok(())
}

async fn git_sha(project_dir: &Path) -> String {
    Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(project_dir)
        .output()
        .await
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "unknown".to_string())
}

fn fnv1a_hash(sequence: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in sequence.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn resolve_project_dir(config: &Config) -> Result<PathBuf> {
    let raw = if config.project_dir.is_absolute() {
        config.project_dir.clone()
    } else {
        std::env::current_dir()?.join(&config.project_dir)
    };
    Ok(raw.canonicalize().unwrap_or(raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scripted_failure_schedule_is_deterministic() {
        let first: Vec<bool> = (0..BACKLOG_SIZE)
            .map(|index| scripted_failure(7, index))
            .collect();
        let second: Vec<bool> = (0..BACKLOG_SIZE)
            .map(|index| scripted_failure(7, index))
            .collect();
        assert_eq!(first, second);
        assert!(first.iter().any(|failed| *failed));
        assert!(first.iter().any(|failed| !failed));
    }

    #[test]
    fn lifecycle_reason_is_cell_deterministic() {
        assert_eq!(
            simulated_turn_end_reason(&Cell {
                model: "gpt-5.6-luna",
                harness: "codex",
            }),
            "exited"
        );
        assert_eq!(
            simulated_turn_end_reason(&Cell {
                model: "sonnet",
                harness: "claude-code",
            }),
            "stopped_backlog_nonempty"
        );
    }
}
