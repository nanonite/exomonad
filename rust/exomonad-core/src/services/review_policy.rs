use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::Path;

/// Match a path against a glob pattern prefix.
fn pattern_matches_path(prefix: &str, path: &str) -> bool {
    path.starts_with(prefix)
}

/// Review policy configuration loaded from `.exo/review-policy.toml`.
#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct ReviewPolicy {
    /// Minimum review rounds before merge is allowed.
    pub min_review_rounds: u32,

    /// Maximum review rounds before Stuck terminal state.
    pub reviewer_max_rounds: u32,

    /// Review must be submitted within this recency window (seconds).
    pub review_freshness_window_secs: u64,

    /// Lines changed threshold to trigger second-reviewer requirement.
    pub external_review_threshold: u64,

    /// Paths that always trigger an external/second review.
    #[serde(default)]
    pub external_review_paths: Vec<String>,

    /// Maximum wait time for a reviewer to respond (seconds).
    pub reviewer_max_wait_seconds: u64,

    /// Maximum rate-limit retries for reviewer agents.
    pub reviewer_max_rate_limit_retries: u32,

    /// Require a second reviewer for complex PRs.
    pub require_second_reviewer_complexity: bool,

    /// Lines changed threshold to trigger second-reviewer requirement.
    pub complexity_line_threshold: u64,
}

impl Default for ReviewPolicy {
    fn default() -> Self {
        Self {
            min_review_rounds: 1,
            reviewer_max_rounds: 5,
            review_freshness_window_secs: 1200,
            external_review_threshold: 300,
            external_review_paths: vec![
                "proto/**".to_string(),
                "rust/exomonad-core/src/handlers/**".to_string(),
            ],
            reviewer_max_wait_seconds: 1200,
            reviewer_max_rate_limit_retries: 2,
            require_second_reviewer_complexity: false,
            complexity_line_threshold: 500,
        }
    }
}

impl ReviewPolicy {
    /// Standard development policy: require 1 round, 20 min window.
    pub fn standard() -> Self {
        Self::default()
    }

    /// Load policy from `.exo/review-policy.toml` or return defaults.
    pub async fn load(project_dir: &Path) -> Result<Self> {
        let path = project_dir.join(".exo/review-policy.toml");
        if !path.exists() {
            return Ok(Self::default());
        }
        let data = tokio::fs::read_to_string(&path)
            .await
            .with_context(|| format!("Failed to read {}", path.display()))?;
        let policy: ReviewPolicy =
            toml::from_str(&data).with_context(|| "Failed to parse review-policy.toml")?;
        Ok(policy)
    }

    /// Check whether a changed path triggers external review.
    pub fn path_triggers_external_review(&self, path: &str) -> bool {
        if self.external_review_paths.is_empty() {
            return false;
        }
        self.external_review_paths.iter().any(|pattern| {
            if let Some(prefix) = pattern.strip_suffix("**") {
                pattern_matches_path(prefix, path)
            } else {
                path.contains(pattern.as_str())
            }
        })
    }

    /// Check whether line count exceeds the external review threshold.
    pub fn lines_trigger_external_review(&self, lines_changed: u64) -> bool {
        lines_changed > self.external_review_threshold
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_policy_values() {
        let p = ReviewPolicy::default();
        assert_eq!(p.min_review_rounds, 1);
        assert_eq!(p.reviewer_max_rounds, 5);
        assert_eq!(p.review_freshness_window_secs, 1200);
        assert_eq!(p.external_review_threshold, 300);
        assert_eq!(p.reviewer_max_wait_seconds, 1200);
        assert_eq!(p.reviewer_max_rate_limit_retries, 2);
    }

    #[test]
    fn test_path_triggers_external_proto() {
        let mut p = ReviewPolicy::default();
        p.external_review_paths = vec!["proto/**".to_string()];
        assert!(p.path_triggers_external_review("proto/exomonad.proto"));
        assert!(!p.path_triggers_external_review("rust/main.rs"));
    }

    #[test]
    fn test_path_triggers_empty_when_no_patterns() {
        let mut p = ReviewPolicy::default();
        p.external_review_paths = vec![];
        assert!(!p.path_triggers_external_review("proto/exomonad.proto"));
    }

    #[test]
    fn test_lines_trigger_at_threshold() {
        let p = ReviewPolicy::default();
        assert!(p.lines_trigger_external_review(301));
        assert!(!p.lines_trigger_external_review(300));
    }

    #[test]
    fn test_deserialize_toml_minimal() {
        let toml_str = r#"
            min_review_rounds = 2
        "#;
        let policy: ReviewPolicy = toml::from_str(toml_str).unwrap();
        assert_eq!(policy.min_review_rounds, 2);
        assert_eq!(policy.reviewer_max_rounds, 5); // default
    }

    #[test]
    fn test_deserialize_toml_full() {
        let toml_str = r#"
            min_review_rounds = 2
            reviewer_max_rounds = 3
            review_freshness_window_secs = 600
            external_review_threshold = 500
            external_review_paths = ["proto/**", "haskell/**"]
            reviewer_max_wait_seconds = 900
            reviewer_max_rate_limit_retries = 3
            require_second_reviewer_complexity = true
            complexity_line_threshold = 1000
        "#;
        let policy: ReviewPolicy = toml::from_str(toml_str).unwrap();
        assert_eq!(policy.min_review_rounds, 2);
        assert_eq!(policy.reviewer_max_rounds, 3);
        assert_eq!(policy.review_freshness_window_secs, 600);
        assert_eq!(policy.external_review_threshold, 500);
        assert_eq!(policy.external_review_paths.len(), 2);
        assert_eq!(policy.reviewer_max_wait_seconds, 900);
        assert_eq!(policy.reviewer_max_rate_limit_retries, 3);
        assert!(policy.require_second_reviewer_complexity);
        assert_eq!(policy.complexity_line_threshold, 1000);
    }
}
