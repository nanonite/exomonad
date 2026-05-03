use crate::services::review_policy::ReviewPolicy;
use std::path::Path;

/// Result of analyzing a PR diff for complexity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComplexityReport {
    /// Total lines changed (additions + deletions).
    pub total_lines_changed: u64,
    /// Files that match `external_review_paths` patterns.
    pub external_review_path_matches: Vec<String>,
    /// Recommendation for second-reviewer routing.
    pub recommendation: SecondReviewerRecommendation,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SecondReviewerRecommendation {
    /// No second reviewer needed.
    Pass,
    /// Path-based match: at least one changed file matches `external_review_paths`.
    PathMatch {
        matching_files: Vec<String>,
    },
    /// Line-count threshold exceeded.
    Threshold {
        lines_changed: u64,
        threshold: u64,
    },
    /// Both path match and threshold exceeded.
    MultipleTriggers {
        matching_files: Vec<String>,
        lines_changed: u64,
    },
}

impl ComplexityReport {
    /// Whether a second reviewer is recommended.
    pub fn needs_second_reviewer(&self) -> bool {
        !matches!(self.recommendation, SecondReviewerRecommendation::Pass)
    }

    /// Generate a human-readable summary.
    pub fn summary(&self) -> String {
        let mut parts = vec![format!("{} lines changed", self.total_lines_changed)];
        if !self.external_review_path_matches.is_empty() {
            parts.push(format!(
                "{} sensitive path(s) matched: {}",
                self.external_review_path_matches.len(),
                self.external_review_path_matches.join(", ")
            ));
        }
        match &self.recommendation {
            SecondReviewerRecommendation::Pass => {
                parts.push("No second reviewer needed".to_string())
            }
            SecondReviewerRecommendation::PathMatch { .. } => {
                parts.push("Second reviewer required (path policy)".to_string())
            }
            SecondReviewerRecommendation::Threshold { lines_changed, threshold } => {
                parts.push(format!(
                    "Second reviewer required ({} lines > {} threshold)",
                    lines_changed, threshold
                ))
            }
            SecondReviewerRecommendation::MultipleTriggers { .. } => {
                parts.push("Second reviewer required (multiple triggers)".to_string())
            }
        }
        parts.join("; ")
    }
}

/// Analyze a PR diff and classify complexity against the review policy.
///
/// Returns an error if `git diff` fails. Otherwise always returns a report.
pub async fn classify_complexity(
    project_dir: &Path,
    base_branch: &str,
    head_branch: &str,
    policy: &ReviewPolicy,
) -> anyhow::Result<ComplexityReport> {
    let diff_output = run_git_diff_stat(project_dir, base_branch, head_branch).await?;
    let (total_lines, changed_files) = parse_numstat_output(&diff_output);

    let external_matches: Vec<String> = changed_files
        .iter()
        .filter(|f| policy.path_triggers_external_review(f))
        .cloned()
        .collect();

    let recommendation = if !external_matches.is_empty()
        && policy.lines_trigger_external_review(total_lines)
    {
        SecondReviewerRecommendation::MultipleTriggers {
            matching_files: external_matches.clone(),
            lines_changed: total_lines,
        }
    } else if !external_matches.is_empty() {
        SecondReviewerRecommendation::PathMatch {
            matching_files: external_matches.clone(),
        }
    } else if policy.lines_trigger_external_review(total_lines) {
        SecondReviewerRecommendation::Threshold {
            lines_changed: total_lines,
            threshold: policy.external_review_threshold,
        }
    } else {
        SecondReviewerRecommendation::Pass
    };

    Ok(ComplexityReport {
        total_lines_changed: total_lines,
        external_review_path_matches: external_matches,
        recommendation,
    })
}

/// Count total lines changed between two branches.
///
/// Returns (total_lines, file_list). Faster than full `classify` when only
/// line counts are needed.
pub async fn count_changed_lines(
    project_dir: &Path,
    base_branch: &str,
    head_branch: &str,
) -> anyhow::Result<u64> {
    let diff_output = run_git_diff_stat(project_dir, base_branch, head_branch).await?;
    let (total_lines, _) = parse_numstat_output(&diff_output);
    Ok(total_lines)
}

async fn run_git_diff_stat(
    project_dir: &Path,
    base_branch: &str,
    head_branch: &str,
) -> anyhow::Result<String> {
    let output = tokio::process::Command::new("git")
        .args([
            "diff",
            &format!("{}..{}", base_branch, head_branch),
            "--numstat",
        ])
        .current_dir(project_dir)
        .output()
        .await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!("git diff --numstat failed: {}", stderr.trim());
    }

    Ok(String::from_utf8_lossy(&output.stdout).to_string())
}

/// Parse `git diff --numstat` output: each line is `additions\tsubtractions\tfilename`.
fn parse_numstat_output(output: &str) -> (u64, Vec<String>) {
    let mut total_added: u64 = 0;
    let mut total_removed: u64 = 0;
    let mut files = Vec::new();

    for line in output.lines() {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() < 3 {
            continue;
        }
        // Binary files show up as "-\t-\tfilename"
        let added: u64 = parts[0].parse().unwrap_or(0);
        let removed: u64 = parts[1].parse().unwrap_or(0);
        total_added += added;
        total_removed += removed;
        files.push(parts[2].to_string());
    }

    (total_added + total_removed, files)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_numstat_empty() {
        let (lines, files) = parse_numstat_output("");
        assert_eq!(lines, 0);
        assert!(files.is_empty());
    }

    #[test]
    fn test_parse_numstat_simple() {
        let input = "10\t5\tsrc/main.rs\n3\t0\tREADME.md\n";
        let (lines, files) = parse_numstat_output(input);
        assert_eq!(lines, 18);
        assert_eq!(files.len(), 2);
        assert_eq!(files[0], "src/main.rs");
        assert_eq!(files[1], "README.md");
    }

    #[test]
    fn test_parse_numstat_binary() {
        let input = "-\t-\tbin/app\n";
        let (lines, files) = parse_numstat_output(input);
        assert_eq!(lines, 0);
        assert_eq!(files.len(), 1);
    }

    #[test]
    fn test_parse_numstat_mixed() {
        let input = "100\t50\tlib.rs\n-\t-\tasset.png\n5\t0\tmod.rs\n";
        let (lines, files) = parse_numstat_output(input);
        assert_eq!(lines, 155);
        assert_eq!(files.len(), 3);
    }

    #[test]
    fn test_report_pass() {
        let report = ComplexityReport {
            total_lines_changed: 50,
            external_review_path_matches: vec![],
            recommendation: SecondReviewerRecommendation::Pass,
        };
        assert!(!report.needs_second_reviewer());
        assert!(report.summary().contains("No second reviewer"));
    }

    #[test]
    fn test_report_path_match() {
        let report = ComplexityReport {
            total_lines_changed: 50,
            external_review_path_matches: vec!["proto/foo.proto".to_string()],
            recommendation: SecondReviewerRecommendation::PathMatch {
                matching_files: vec!["proto/foo.proto".to_string()],
            },
        };
        assert!(report.needs_second_reviewer());
        assert!(report.summary().contains("path policy"));
    }

    #[test]
    fn test_report_threshold() {
        let report = ComplexityReport {
            total_lines_changed: 400,
            external_review_path_matches: vec![],
            recommendation: SecondReviewerRecommendation::Threshold {
                lines_changed: 400,
                threshold: 300,
            },
        };
        assert!(report.needs_second_reviewer());
        assert!(report.summary().contains("400 lines > 300"));
    }

    #[test]
    fn test_report_multiple_triggers() {
        let report = ComplexityReport {
            total_lines_changed: 400,
            external_review_path_matches: vec!["proto/foo.proto".to_string()],
            recommendation: SecondReviewerRecommendation::MultipleTriggers {
                matching_files: vec!["proto/foo.proto".to_string()],
                lines_changed: 400,
            },
        };
        assert!(report.needs_second_reviewer());
        assert!(report.summary().contains("multiple triggers"));
    }
}
