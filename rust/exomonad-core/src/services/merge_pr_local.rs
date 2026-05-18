// Local PR merge — replaces GitHub merge API with git merge + registry update.
//
// Reads PR from .exo/prs.json, applies review policy gates, performs
// local git merge into the parent (base) branch, pushes to the tangled
// remote (or origin as fallback), and updates the registry state to Merged.

use crate::domain::{AgentName, BranchName, CIStatus, MergeStrategy, PRNumber};
use crate::services::file_pr_local::{
    read_pr_registry, resolve_push_remote, write_pr_registry, PrRegistry, PrState,
};
use crate::services::git_worktree::GitWorktreeService;
use crate::services::merge_pr::MergePROutput;
pub use crate::services::review_policy::ReviewPolicy;
use anyhow::{bail, Context, Result};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{info, warn};

// ============================================================================
// Merge Gate Errors
// ============================================================================

/// Errors from merge gate checks. Each variant maps to a specific policy violation.
#[derive(Debug, Clone, thiserror::Error)]
pub enum MergeGateError {
    /// PR is in the Stuck terminal state — must surface to human.
    #[error("PR #{pr_number} is STUCK (rounds={rounds}) — requires human intervention")]
    Stuck { pr_number: u64, rounds: u32 },
    /// PR has the `needs_human_review` flag set.
    #[error("PR #{pr_number} requires human review")]
    NeedsHumanReview { pr_number: u64 },
    /// PR review state is not Approved.
    #[error("PR #{pr_number} not approved (review_state={review_state})")]
    NotApproved {
        pr_number: u64,
        review_state: String,
    },
    /// Author and merger are the same agent — identity separation violated.
    #[error("PR #{pr_number}: author {author} cannot self-merge")]
    SelfMerge { pr_number: u64, author: String },
    /// Not enough review rounds completed.
    #[error("PR #{pr_number}: {rounds} review round(s) completed, {required} required")]
    InsufficientRounds {
        pr_number: u64,
        rounds: u32,
        required: u32,
    },
    /// Complex PR requires a second reviewer but only one has reviewed.
    #[error("PR #{pr_number}: requires second reviewer (complexity threshold exceeded)")]
    SecondReviewerRequired { pr_number: u64 },
    /// PR not found in the registry.
    #[error("PR #{pr_number} not found in local registry")]
    NotFound { pr_number: u64 },
    /// CI has not passed (spindle reports non-success status).
    #[error("PR #{pr_number}: CI not passing (status={status})")]
    CiNotPassed { pr_number: u64, status: String },
}

// ============================================================================
// Gate Checks
// ============================================================================

/// Run all merge gates against a PR entry.
///
/// Returns `Ok(())` if the PR passes all gates, or the first failing gate error.
pub fn check_merge_gates(
    pr: &crate::services::file_pr_local::PrEntry,
    merger: &AgentName,
    policy: &ReviewPolicy,
    line_count: Option<u64>,
    ci_status: Option<CIStatus>,
) -> std::result::Result<(), MergeGateError> {
    // Gate 1: Not stuck
    if pr.stuck {
        return Err(MergeGateError::Stuck {
            pr_number: pr.number,
            rounds: pr.rounds,
        });
    }

    // Gate 2: Not needing human review
    if pr.needs_human_review {
        return Err(MergeGateError::NeedsHumanReview {
            pr_number: pr.number,
        });
    }

    // Gate 3: Review must be approved
    use crate::services::file_pr_local::LocalReviewState;
    if pr.review_state != LocalReviewState::Approved {
        let state_str = match pr.review_state {
            LocalReviewState::PendingReview => "pending_review",
            LocalReviewState::ChangesRequested => "changes_requested",
            LocalReviewState::Approved => "approved",
        };
        return Err(MergeGateError::NotApproved {
            pr_number: pr.number,
            review_state: state_str.to_string(),
        });
    }

    // Gate 4: Author/reviewer identity separation
    // The merger must not be the PR author (unless a human override is in place).
    let merger_str = merger.as_str();
    if pr.author_agent == merger_str {
        return Err(MergeGateError::SelfMerge {
            pr_number: pr.number,
            author: pr.author_agent.clone(),
        });
    }

    // Gate 5: Minimum review rounds
    if policy.min_review_rounds > 0 && pr.rounds < policy.min_review_rounds {
        return Err(MergeGateError::InsufficientRounds {
            pr_number: pr.number,
            rounds: pr.rounds,
            required: policy.min_review_rounds,
        });
    }

    // Gate 6: Complexity-based second-reviewer requirement
    if policy.require_second_reviewer_complexity {
        if let Some(lines) = line_count {
            if lines > policy.complexity_line_threshold {
                return Err(MergeGateError::SecondReviewerRequired {
                    pr_number: pr.number,
                });
            }
        }
    }

    // Gate 7: CI must pass when spindle status is available
    if let Some(ci) = ci_status {
        if ci != CIStatus::Success && ci != CIStatus::Neutral {
            return Err(MergeGateError::CiNotPassed {
                pr_number: pr.number,
                status: ci.as_str().to_string(),
            });
        }
    }

    Ok(())
}

// ============================================================================
// Git Operations
// ============================================================================

async fn run_git(dir: &Path, args: &[&str]) -> Result<()> {
    let dir = dir.to_path_buf();
    let args: Vec<String> = args.iter().map(|s| s.to_string()).collect();
    tokio::task::spawn_blocking(move || {
        let output = std::process::Command::new("git")
            .args(&args)
            .current_dir(&dir)
            .output()
            .context("Failed to run git")?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            bail!("git {} failed: {}", args.join(" "), stderr.trim());
        }
        Ok(())
    })
    .await
    .context("spawn_blocking failed")??;
    Ok(())
}

// ============================================================================
// Main Implementation
// ============================================================================

/// Merge a PR locally: read from `.exo/prs.json`, gate, git merge, push, update registry.
///
/// `project_dir` is the root of the exomonad project (where `.exo/` lives).
/// `merger_agent` is the agent requesting the merge (enforces identity separation).
/// `spindle_url` and `ci_status_map` wire the CI gate: when spindle is configured the branch
/// must have a `Success` or `Neutral` CI status or the merge is blocked.
pub async fn merge_pr_local(
    pr_number: PRNumber,
    strategy: &MergeStrategy,
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    merger_agent: &AgentName,
    policy: &ReviewPolicy,
    spindle_url: Option<&str>,
    ci_status_map: &Arc<RwLock<HashMap<BranchName, CIStatus>>>,
) -> Result<MergePROutput> {
    let prs_path = project_dir.join(".exo/prs.json");

    // Read registry and find the PR
    let registry = read_pr_registry(&prs_path).await?;
    let pr = registry
        .prs
        .get(&pr_number.as_u64())
        .ok_or_else(|| anyhow::anyhow!("PR #{} not found in local registry", pr_number))?
        .clone();

    let head_branch = pr.head_branch.clone();
    let base_branch = pr.base_branch.clone();

    info!(
        pr_number = pr_number.as_u64(),
        head = %head_branch,
        base = %base_branch,
        merger = %merger_agent,
        "Merging local PR"
    );

    // Resolve CI status for Gate 7: when spindle is configured, missing status blocks the merge.
    let ci_status = if spindle_url.is_some() {
        let branch = BranchName::try_from_str(head_branch.as_str())
            .expect("validated string input is non-empty");
        let status = ci_status_map
            .read()
            .await
            .get(&branch)
            .copied()
            .unwrap_or(CIStatus::Unknown);
        info!(branch = %head_branch, status = ?status, "CI gate check (spindle configured)");
        Some(status)
    } else {
        None
    };

    // Gate checks (fail early)
    if let Err(e) = check_merge_gates(&pr, merger_agent, policy, None, ci_status) {
        let blocked_on_ci = matches!(e, MergeGateError::CiNotPassed { .. });
        if blocked_on_ci {
            set_merge_blocked_on_ci(&prs_path, pr_number.as_u64(), true).await?;
        }
        warn!(blocked_on_ci, "Merge gate failed: {}", e);
        return Ok(MergePROutput {
            success: false,
            message: e.to_string(),
            git_fetched: false,
            branch_name: BranchName::try_from_str(head_branch.as_str())
                .expect("validated string input is non-empty"),
        });
    }
    if pr.merge_blocked_on_ci {
        set_merge_blocked_on_ci(&prs_path, pr_number.as_u64(), false).await?;
    }

    // --- Local git merge ---

    // Step 1: Get current branch so we can restore it
    let dir = PathBuf::from(project_dir);
    let wt = git_wt.clone();
    let current_branch = tokio::task::spawn_blocking(move || wt.get_workspace_bookmark(&dir))
        .await
        .context("spawn_blocking failed")?
        .context("Failed to get current bookmark")?;

    // Step 2: Checkout base branch if not already on it
    if current_branch.as_deref() != Some(&base_branch) {
        info!("Checking out base branch: {}", base_branch);
        run_git(project_dir, &["checkout", &base_branch]).await?;
    }

    // Step 3: Merge head branch using the requested strategy
    let commit_msg = format!("Merge PR #{} ({})", pr_number, strategy);
    match strategy {
        MergeStrategy::Squash => {
            run_git(project_dir, &["merge", "--squash", &head_branch]).await?;
            run_git(project_dir, &["commit", "-m", &commit_msg]).await?;
        }
        MergeStrategy::Merge | MergeStrategy::Rebase => {
            run_git(project_dir, &["merge", &head_branch, "-m", &commit_msg]).await?;
        }
    }

    // Step 4: Push base branch to tangled remote if configured, otherwise origin.
    let base = BranchName::try_from_str(base_branch.as_str())
        .expect("validated string input is non-empty");
    let dir = PathBuf::from(project_dir);
    let remote = resolve_push_remote(&dir).to_string();
    info!(remote = %remote, base = %base_branch, "Pushing merged base branch");
    let wt = git_wt.clone();
    tokio::task::spawn_blocking(move || wt.push_to_remote(&dir, &base, &remote))
        .await
        .context("spawn_blocking failed")?
        .context("push base branch")?;
    info!("Pushed base branch: {}", base_branch);

    // Step 5: Update registry — mark PR as Merged
    let mut registry = read_pr_registry(&prs_path).await?;
    if let Some(entry) = registry.prs.get_mut(&pr_number.as_u64()) {
        entry.state = PrState::Merged;
    }
    write_pr_registry(&prs_path, &registry).await?;
    if let Some(issue_id) = pr.chainlink_issue_id {
        close_chainlink_issue(project_dir, issue_id, pr_number.as_u64()).await;
    }

    info!(
        pr_number = pr_number.as_u64(),
        "PR merged and registry updated"
    );

    // Step 6: Clean up the agent's worktree and identity dir now that the branch is merged.
    // The worktree path follows the naming convention: .exo/worktrees/{last-dot-segment}/
    // Non-fatal — log warnings but don't fail the merge.
    let agent_slug = head_branch
        .rsplit('.')
        .next()
        .unwrap_or(&head_branch)
        .to_string();
    let worktree_path = project_dir.join(".exo/worktrees").join(&agent_slug);
    if worktree_path.exists() {
        let wt = git_wt.clone();
        let wt_path = worktree_path.clone();
        match tokio::task::spawn_blocking(move || wt.remove_workspace(&wt_path)).await {
            Ok(Ok(())) => info!(path = %worktree_path.display(), "Removed merged worktree"),
            Ok(Err(e)) => {
                warn!(error = %e, path = %worktree_path.display(), "Failed to remove worktree (non-fatal)")
            }
            Err(e) => warn!(error = %e, "spawn_blocking failed for worktree removal"),
        }
    }
    let agent_dir = project_dir.join(".exo/agents").join(&agent_slug);
    if agent_dir.exists() {
        if let Err(e) = std::fs::remove_dir_all(&agent_dir) {
            warn!(error = %e, path = %agent_dir.display(), "Failed to remove agent dir (non-fatal)");
        } else {
            info!(path = %agent_dir.display(), "Removed merged agent dir");
        }
    }

    Ok(MergePROutput {
        success: true,
        message: format!("PR #{} merged via {}", pr_number, strategy),
        git_fetched: true,
        branch_name: BranchName::try_from_str(head_branch.as_str())
            .expect("validated string input is non-empty"),
    })
}

async fn close_chainlink_issue(project_dir: &Path, issue_id: u64, pr_number: u64) {
    let project_dir = project_dir.to_path_buf();
    let issue_id_arg = issue_id.to_string();
    let result = tokio::task::spawn_blocking(move || {
        std::process::Command::new("chainlink")
            .args(["issue", "close", &issue_id_arg])
            .current_dir(project_dir)
            .output()
    })
    .await;

    match result {
        Ok(Ok(output)) if output.status.success() => {
            info!(issue_id, pr_number, "Closed Chainlink issue for merged PR");
        }
        Ok(Ok(output)) => {
            warn!(
                issue_id,
                pr_number,
                stderr = %String::from_utf8_lossy(&output.stderr).trim(),
                "Failed to close Chainlink issue for merged PR"
            );
        }
        Ok(Err(err)) => {
            warn!(issue_id, pr_number, error = %err, "Failed to run chainlink close");
        }
        Err(err) => {
            warn!(issue_id, pr_number, error = %err, "chainlink close task failed");
        }
    }
}

async fn set_merge_blocked_on_ci(prs_path: &Path, pr_number: u64, blocked: bool) -> Result<()> {
    let mut registry: PrRegistry = read_pr_registry(prs_path).await?;
    if let Some(pr) = registry.prs.get_mut(&pr_number) {
        pr.merge_blocked_on_ci = blocked;
        write_pr_registry(prs_path, &registry).await?;
        info!(
            pr_number,
            merge_blocked_on_ci = blocked,
            "Updated local PR CI merge-blocked state"
        );
    }
    Ok(())
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AgentName;
    use crate::services::file_pr_local::{LocalReviewState, PrEntry, PrState};
    use chrono::Utc;

    fn test_entry(number: u64, head: &str, base: &str) -> PrEntry {
        PrEntry {
            number,
            head_branch: head.into(),
            base_branch: base.into(),
            title: "Test PR".into(),
            body: "Test body".into(),
            author_agent: "feat-gemini".into(),
            author_role: "dev".into(),
            created_at: Utc::now(),
            state: PrState::Open,
            review_state: LocalReviewState::PendingReview,
            last_review_at: None,
            last_head_sha: None,
            reviewer_agent: None,
            reviewer_birth_branch: None,
            rounds: 0,
            stuck: false,
            needs_human_review: false,
            merge_blocked_on_ci: false,
            chainlink_issue_id: None,
        }
    }

    fn reviewer_agent() -> AgentName {
        AgentName::try_from_str("reviewer-gemini").expect("literal validated string is non-empty")
    }

    fn author_agent() -> AgentName {
        AgentName::try_from_str("feat-gemini").expect("literal validated string is non-empty")
    }

    fn standard_policy() -> ReviewPolicy {
        ReviewPolicy::standard()
    }

    // ── Happy path ──────────────────────────────────────────────

    #[test]
    fn test_all_gates_pass_for_approved_pr() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            result.is_ok(),
            "Expected all gates to pass, got: {:?}",
            result
        );
    }

    #[test]
    fn test_all_gates_pass_with_high_rounds() {
        let mut pr = test_entry(2, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 3;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(result.is_ok());
    }

    // ── Gate: Stuck ────────────────────────────────────────────

    #[test]
    fn test_stuck_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.stuck = true;
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 6;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(result, Err(MergeGateError::Stuck { pr_number: 1, .. })),
            "Expected Stuck, got: {:?}",
            result
        );
    }

    #[test]
    fn test_stuck_message_contains_round_count() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.stuck = true;
        pr.rounds = 7;
        pr.review_state = LocalReviewState::Approved;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        let msg = result.unwrap_err().to_string();
        assert!(msg.contains("STUCK"));
        assert!(msg.contains("rounds=7"));
    }

    // ── Gate: NeedsHumanReview ─────────────────────────────────

    #[test]
    fn test_needs_human_review_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.needs_human_review = true;
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 2;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::NeedsHumanReview { pr_number: 1 })
            ),
            "Expected NeedsHumanReview, got: {:?}",
            result
        );
    }

    #[test]
    fn test_needs_human_review_overrides_approved_state() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.needs_human_review = true;
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 3;
        pr.reviewer_agent = Some("other-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(result.is_err());
        // needs_human_review is checked before review_state, so it should hit that gate
        assert!(
            matches!(result, Err(MergeGateError::NeedsHumanReview { .. })),
            "Expected NeedsHumanReview to take priority, got: {:?}",
            result
        );
    }

    // ── Gate: NotApproved ──────────────────────────────────────

    #[test]
    fn test_pending_review_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::PendingReview;
        pr.rounds = 0;

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::NotApproved { pr_number: 1, .. })
            ),
            "Expected NotApproved, got: {:?}",
            result
        );
    }

    #[test]
    fn test_changes_requested_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::ChangesRequested;
        pr.rounds = 1;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(result, Err(MergeGateError::NotApproved { .. })),
            "Expected NotApproved, got: {:?}",
            result
        );
    }

    #[test]
    fn test_not_approved_message_includes_state() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::ChangesRequested;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        let msg = result.unwrap_err().to_string();
        assert!(msg.contains("changes_requested"));
    }

    // ── Gate: Identity Separation ──────────────────────────────

    #[test]
    fn test_author_cannot_self_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        // Author == merger
        let result = check_merge_gates(&pr, &author_agent(), &standard_policy(), None, None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::SelfMerge {
                    pr_number: 1,
                    author: _
                })
            ),
            "Expected SelfMerge, got: {:?}",
            result
        );
    }

    #[test]
    fn test_author_reviewer_separation() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("third-agent-gemini".into());

        // Third agent merges (not author, not reviewer) — OK
        let merger =
            AgentName::try_from_str("tl-claude").expect("literal validated string is non-empty");
        let result = check_merge_gates(&pr, &merger, &standard_policy(), None, None);
        assert!(result.is_ok());
    }

    #[test]
    fn test_agent_cannot_self_approve_then_merge() {
        // When reviewer == author, the PR shouldn't have Approved state,
        // but if it did, the self-merge gate catches it anyway.
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("feat-gemini".into()); // Same as author

        // If the author tries to merge after self-approving
        let result = check_merge_gates(&pr, &author_agent(), &standard_policy(), None, None);
        assert!(result.is_err(), "Author should not be able to merge own PR");
    }

    // ── Gate: Minimum Review Rounds ────────────────────────────

    #[test]
    fn test_insufficient_rounds_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 0; // 0 rounds, policy requires 1
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::InsufficientRounds {
                    pr_number: 1,
                    rounds: 0,
                    required: 1
                })
            ),
            "Expected InsufficientRounds, got: {:?}",
            result
        );
    }

    #[test]
    fn test_min_rounds_policy_can_be_relaxed() {
        let mut relaxed = ReviewPolicy::standard();
        relaxed.min_review_rounds = 0;

        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 0;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &relaxed, None, None);
        assert!(result.is_ok(), "Relaxed policy should allow 0 rounds");
    }

    #[test]
    fn test_min_rounds_policy_can_be_strict() {
        let mut strict = ReviewPolicy::standard();
        strict.min_review_rounds = 3;

        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 2;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &strict, None, None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::InsufficientRounds {
                    rounds: 2,
                    required: 3,
                    ..
                })
            ),
            "Strict policy should require 3 rounds"
        );
    }

    // ── Gate: Complexity Second Reviewer ───────────────────────

    #[test]
    fn test_complex_pr_requires_second_reviewer() {
        let mut policy = ReviewPolicy::standard();
        policy.require_second_reviewer_complexity = true;
        policy.complexity_line_threshold = 100;

        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 2;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        // 200 lines changed > 100 threshold
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, Some(200), None);
        assert!(
            matches!(
                result,
                Err(MergeGateError::SecondReviewerRequired { pr_number: 1 })
            ),
            "Expected SecondReviewerRequired, got: {:?}",
            result
        );
    }

    #[test]
    fn test_small_pr_passes_complexity_gate() {
        let mut policy = ReviewPolicy::standard();
        policy.require_second_reviewer_complexity = true;
        policy.complexity_line_threshold = 500;

        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        // 50 lines changed < 500 threshold
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, Some(50), None);
        assert!(result.is_ok(), "Small PR should pass complexity gate");
    }

    #[test]
    fn test_complexity_gate_ignored_when_no_line_count() {
        let mut policy = ReviewPolicy::standard();
        policy.require_second_reviewer_complexity = true;

        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        // No line count provided
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, None, None);
        assert!(result.is_ok(), "Should skip complexity when no line count");
    }

    // ── Gate: Multiple gates fail, first error wins ────────────

    #[test]
    fn test_first_gate_error_returned() {
        // Stuck flag should be caught before NotApproved or InsufficientRounds
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.stuck = true;
        pr.needs_human_review = true;
        pr.review_state = LocalReviewState::PendingReview;
        pr.rounds = 0;

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(result, Err(MergeGateError::Stuck { .. })),
            "Stuck should be caught first, got: {:?}",
            result
        );
    }

    #[test]
    fn test_gate_order_is_deterministic() {
        // Verify that NeedsHumanReview beats NotApproved
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.needs_human_review = true;
        pr.review_state = LocalReviewState::PendingReview;

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(
            matches!(result, Err(MergeGateError::NeedsHumanReview { .. })),
            "NeedsHumanReview should beat NotApproved, got: {:?}",
            result
        );
    }

    // ── Default policy ─────────────────────────────────────────

    #[test]
    fn test_default_policy_requires_one_round() {
        let policy = ReviewPolicy::default();
        assert_eq!(policy.min_review_rounds, 1);
        assert!(!policy.require_second_reviewer_complexity);
    }

    #[test]
    fn test_standard_policy_requires_one_round() {
        let policy = ReviewPolicy::standard();
        assert_eq!(policy.min_review_rounds, 1);
        assert_eq!(policy.reviewer_max_rounds, 2);
    }

    // ── MergeGateError Display ─────────────────────────────────

    #[test]
    fn test_stuck_error_display() {
        let e = MergeGateError::Stuck {
            pr_number: 42,
            rounds: 7,
        };
        assert!(e.to_string().contains("42"));
        assert!(e.to_string().contains("STUCK"));
    }

    #[test]
    fn test_self_merge_error_display_includes_author() {
        let e = MergeGateError::SelfMerge {
            pr_number: 5,
            author: "alice-gemini".into(),
        };
        assert!(e.to_string().contains("alice-gemini"));
        assert!(e.to_string().contains("self-merge"));
    }

    // ── Gate 7: CI status ──────────────────────────────────────

    fn approved_pr_with_rounds() -> PrEntry {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        pr
    }

    #[test]
    fn test_ci_gate_passes_when_spindle_not_configured() {
        let pr = approved_pr_with_rounds();
        // None ci_status = no spindle, gate skipped
        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None, None);
        assert!(result.is_ok());
    }

    #[test]
    fn test_ci_gate_passes_on_success() {
        let pr = approved_pr_with_rounds();
        let result = check_merge_gates(
            &pr,
            &reviewer_agent(),
            &standard_policy(),
            None,
            Some(CIStatus::Success),
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_ci_gate_passes_on_neutral() {
        let pr = approved_pr_with_rounds();
        let result = check_merge_gates(
            &pr,
            &reviewer_agent(),
            &standard_policy(),
            None,
            Some(CIStatus::Neutral),
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_ci_gate_blocks_on_failure() {
        let pr = approved_pr_with_rounds();
        let result = check_merge_gates(
            &pr,
            &reviewer_agent(),
            &standard_policy(),
            None,
            Some(CIStatus::Failure),
        );
        assert!(matches!(result, Err(MergeGateError::CiNotPassed { .. })));
    }

    #[test]
    fn test_ci_gate_blocks_on_pending() {
        let pr = approved_pr_with_rounds();
        let result = check_merge_gates(
            &pr,
            &reviewer_agent(),
            &standard_policy(),
            None,
            Some(CIStatus::Pending),
        );
        assert!(matches!(result, Err(MergeGateError::CiNotPassed { .. })));
    }

    #[test]
    fn test_ci_gate_blocks_on_unknown_when_spindle_configured() {
        let pr = approved_pr_with_rounds();
        let result = check_merge_gates(
            &pr,
            &reviewer_agent(),
            &standard_policy(),
            None,
            Some(CIStatus::Unknown),
        );
        assert!(matches!(result, Err(MergeGateError::CiNotPassed { .. })));
    }

    #[tokio::test]
    async fn test_merge_pr_local_ci_gate_blocks_when_spindle_configured_no_status() {
        let tmp = tempfile::tempdir().unwrap();
        // Write a prs.json with an approved PR
        let prs_path = tmp.path().join(".exo/prs.json");
        std::fs::create_dir_all(tmp.path().join(".exo")).unwrap();
        let mut registry = crate::services::file_pr_local::PrRegistry::default();
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        registry.prs.insert(1, pr);
        crate::services::file_pr_local::write_pr_registry(&prs_path, &registry)
            .await
            .unwrap();

        let git_wt = Arc::new(GitWorktreeService::new(tmp.path().to_path_buf()));
        let ci_map: Arc<RwLock<HashMap<BranchName, CIStatus>>> =
            Arc::new(RwLock::new(HashMap::new()));

        let result = merge_pr_local(
            PRNumber::new(1),
            &MergeStrategy::Squash,
            tmp.path(),
            git_wt,
            &reviewer_agent(),
            &standard_policy(),
            Some("ws://localhost:6555"),
            &ci_map,
        )
        .await
        .unwrap();

        assert!(
            !result.success,
            "CI gate should block merge when spindle is configured but no status"
        );
        assert!(
            result.message.contains("CI"),
            "Error message should mention CI: {}",
            result.message
        );
    }

    #[tokio::test]
    async fn test_merge_pr_local_ci_gate_passes_with_success_status() {
        let tmp = tempfile::tempdir().unwrap();
        let prs_path = tmp.path().join(".exo/prs.json");
        std::fs::create_dir_all(tmp.path().join(".exo")).unwrap();
        let mut registry = crate::services::file_pr_local::PrRegistry::default();
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 1;
        registry.prs.insert(1, pr);
        crate::services::file_pr_local::write_pr_registry(&prs_path, &registry)
            .await
            .unwrap();

        let git_wt = Arc::new(GitWorktreeService::new(tmp.path().to_path_buf()));
        let mut map = HashMap::new();
        map.insert(
            BranchName::try_from_str("main.feat-gemini")
                .expect("literal validated string is non-empty"),
            CIStatus::Success,
        );
        let ci_map = Arc::new(RwLock::new(map));

        // merge will fail at the git step (no real repo), but gate 7 must pass
        let result = merge_pr_local(
            PRNumber::new(1),
            &MergeStrategy::Squash,
            tmp.path(),
            git_wt,
            &reviewer_agent(),
            &standard_policy(),
            Some("ws://localhost:6555"),
            &ci_map,
        )
        .await;

        // Error is expected (git checkout fails), but NOT a gate failure
        match result {
            Ok(output) => assert!(
                output.success || !output.message.contains("CI"),
                "Unexpected CI gate failure: {}",
                output.message
            ),
            Err(_) => {} // git ops fail without a real repo — that's expected
        }
    }

    // ── Registry integration test (no git) ─────────────────────

    #[tokio::test]
    async fn test_merge_pr_not_found_errors() {
        let tmp = tempfile::tempdir().unwrap();
        let git_wt = Arc::new(GitWorktreeService::new(tmp.path().to_path_buf()));
        let pr_number = PRNumber::new(999);

        let ci_map = Arc::new(RwLock::new(HashMap::new()));
        let result = merge_pr_local(
            pr_number,
            &MergeStrategy::Squash,
            tmp.path(),
            git_wt,
            &reviewer_agent(),
            &standard_policy(),
            None,
            &ci_map,
        )
        .await;

        assert!(result.is_err());
        let msg = format!("{}", result.err().unwrap());
        assert!(msg.contains("999"));
    }
}
