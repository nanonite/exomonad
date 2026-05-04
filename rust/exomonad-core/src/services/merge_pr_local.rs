// Local PR merge — replaces GitHub merge API with git merge + registry update.
//
// Reads PR from .exo/prs.json, applies review policy gates, performs
// local git merge into the parent (base) branch, pushes to origin,
// and updates the registry state to Merged.

use crate::domain::{AgentName, BranchName, MergeStrategy, PRNumber};
use crate::services::file_pr_local::{read_pr_registry, write_pr_registry, PrState};
use crate::services::git_worktree::GitWorktreeService;
use crate::services::merge_pr::MergePROutput;
pub use crate::services::review_policy::ReviewPolicy;
use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tracing::{info, warn};

// ============================================================================
// Merge Gate Errors
// ============================================================================

/// Errors from merge gate checks. Each variant maps to a specific policy violation.
#[derive(Debug, Clone, thiserror::Error)]
pub enum MergeGateError {
    /// PR is in the Stuck terminal state — must surface to human.
    #[error("PR #{pr_number} is STUCK (rounds={rounds}) — requires human intervention")]
    Stuck {
        pr_number: u64,
        rounds: u32,
    },
    /// PR has the `needs_human_review` flag set.
    #[error("PR #{pr_number} requires human review")]
    NeedsHumanReview {
        pr_number: u64,
    },
    /// PR review state is not Approved.
    #[error("PR #{pr_number} not approved (review_state={review_state})")]
    NotApproved {
        pr_number: u64,
        review_state: String,
    },
    /// Author and merger are the same agent — identity separation violated.
    #[error("PR #{pr_number}: author {author} cannot self-merge")]
    SelfMerge {
        pr_number: u64,
        author: String,
    },
    /// Not enough review rounds completed.
    #[error("PR #{pr_number}: {rounds} review round(s) completed, {required} required")]
    InsufficientRounds {
        pr_number: u64,
        rounds: u32,
        required: u32,
    },
    /// Complex PR requires a second reviewer but only one has reviewed.
    #[error("PR #{pr_number}: requires second reviewer (complexity threshold exceeded)")]
    SecondReviewerRequired {
        pr_number: u64,
    },
    /// PR not found in the registry.
    #[error("PR #{pr_number} not found in local registry")]
    NotFound {
        pr_number: u64,
    },
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
pub async fn merge_pr_local(
    pr_number: PRNumber,
    strategy: &MergeStrategy,
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    merger_agent: &AgentName,
    policy: &ReviewPolicy,
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

    // Gate checks (fail early)
    if let Err(e) = check_merge_gates(&pr, merger_agent, policy, None) {
        warn!("Merge gate failed: {}", e);
        return Ok(MergePROutput {
            success: false,
            message: e.to_string(),
            git_fetched: false,
            branch_name: BranchName::from(head_branch.as_str()),
        });
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

    // Step 4: Push base branch to origin (local knot)
    let base = BranchName::from(base_branch.as_str());
    let dir = PathBuf::from(project_dir);
    let wt = git_wt.clone();
    tokio::task::spawn_blocking(move || wt.push_bookmark(&dir, &base))
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

    info!(
        pr_number = pr_number.as_u64(),
        "PR merged and registry updated"
    );

    Ok(MergePROutput {
        success: true,
        message: format!("PR #{} merged via {}", pr_number, strategy),
        git_fetched: true,
        branch_name: BranchName::from(head_branch.as_str()),
    })
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
            rounds: 0,
            stuck: false,
            needs_human_review: false,
        }
    }

    fn reviewer_agent() -> AgentName {
        AgentName::from("reviewer-gemini")
    }

    fn author_agent() -> AgentName {
        AgentName::from("feat-gemini")
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
        assert!(result.is_ok(), "Expected all gates to pass, got: {:?}", result);
    }

    #[test]
    fn test_all_gates_pass_with_high_rounds() {
        let mut pr = test_entry(2, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 3;
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
        assert!(
            matches!(result, Err(MergeGateError::NeedsHumanReview { pr_number: 1 })),
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
        assert!(
            matches!(result, Err(MergeGateError::NotApproved { pr_number: 1, .. })),
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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
        let result = check_merge_gates(&pr, &author_agent(), &standard_policy(), None);
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
        let merger = AgentName::from("tl-claude");
        let result = check_merge_gates(&pr, &merger, &standard_policy(), None);
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
        let result = check_merge_gates(&pr, &author_agent(), &standard_policy(), None);
        assert!(result.is_err(), "Author should not be able to merge own PR");
    }

    // ── Gate: Minimum Review Rounds ────────────────────────────

    #[test]
    fn test_insufficient_rounds_blocks_merge() {
        let mut pr = test_entry(1, "main.feat-gemini", "main");
        pr.review_state = LocalReviewState::Approved;
        pr.rounds = 0; // 0 rounds, policy requires 1
        pr.reviewer_agent = Some("reviewer-gemini".into());

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &relaxed, None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &strict, None);
        assert!(
            matches!(result, Err(MergeGateError::InsufficientRounds { rounds: 2, required: 3, .. })),
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
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, Some(200));
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
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, Some(50));
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
        let result = check_merge_gates(&pr, &reviewer_agent(), &policy, None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

        let result = check_merge_gates(&pr, &reviewer_agent(), &standard_policy(), None);
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

    // ── Registry integration test (no git) ─────────────────────

    #[tokio::test]
    async fn test_merge_pr_not_found_errors() {
        let tmp = tempfile::tempdir().unwrap();
        let git_wt = Arc::new(GitWorktreeService::new(tmp.path().to_path_buf()));
        let pr_number = PRNumber::new(999);

        let result = merge_pr_local(
            pr_number,
            &MergeStrategy::Squash,
            tmp.path(),
            git_wt,
            &reviewer_agent(),
            &standard_policy(),
        )
        .await;

        assert!(result.is_err());
        let msg = format!("{}", result.err().unwrap());
        assert!(msg.contains("999"));
    }
}
