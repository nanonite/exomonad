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

    /// CI merge gate behavior.
    pub ci: CiPolicy,

    /// Maximum active session duration for leaf/worker agents in seconds. 0 = disabled.
    pub max_leaf_session_seconds: u64,

    /// Maximum active session duration for reviewer agents in seconds. 0 = disabled.
    pub max_reviewer_session_seconds: u64,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(default)]
pub struct CiPolicy {
    /// `auto` requires CI only when a CI source is configured, `on` always requires it,
    /// and `off` treats CI as neutral.
    pub gate: CiGate,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CiGate {
    Auto,
    On,
    Off,
}

impl Default for CiPolicy {
    fn default() -> Self {
        Self { gate: CiGate::Auto }
    }
}

impl Default for CiGate {
    fn default() -> Self {
        Self::Auto
    }
}

impl CiGate {
    pub fn enabled(self, ci_source_configured: bool) -> bool {
        match self {
            Self::Auto => ci_source_configured,
            Self::On => true,
            Self::Off => false,
        }
    }
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
            ci: CiPolicy::default(),
            max_leaf_session_seconds: 3600,
            max_reviewer_session_seconds: 600,
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

    /// Load the file policy and apply an explicit init/session override.
    pub async fn load_with_reviewer_max_rounds(
        project_dir: &Path,
        reviewer_max_rounds: Option<u32>,
    ) -> Result<Self> {
        let policy = Self::load(project_dir).await?;
        policy.with_reviewer_max_rounds(reviewer_max_rounds)
    }

    /// Apply an explicit reviewer cap, preserving the file/default policy when omitted.
    pub fn with_reviewer_max_rounds(mut self, reviewer_max_rounds: Option<u32>) -> Result<Self> {
        if let Some(rounds) = reviewer_max_rounds {
            if rounds == 0 {
                anyhow::bail!("reviewer_max_rounds must be at least 1, got 0");
            }
            self.reviewer_max_rounds = rounds;
        }
        Ok(self)
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
        assert_eq!(p.ci.gate, CiGate::Auto);
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

            [ci]
            gate = "off"
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
        assert_eq!(policy.ci.gate, CiGate::Off);
    }

    #[tokio::test]
    async fn test_file_policy_and_init_override_resolve_in_precedence_order() {
        let temp_dir = tempfile::tempdir().unwrap();
        let exo_dir = temp_dir.path().join(".exo");
        std::fs::create_dir_all(&exo_dir).unwrap();
        std::fs::write(
            exo_dir.join("review-policy.toml"),
            "reviewer_max_rounds = 2\n",
        )
        .unwrap();

        let overridden = ReviewPolicy::load_with_reviewer_max_rounds(temp_dir.path(), Some(5))
            .await
            .unwrap();
        let file_value = ReviewPolicy::load_with_reviewer_max_rounds(temp_dir.path(), None)
            .await
            .unwrap();

        assert_eq!(overridden.reviewer_max_rounds, 5);
        assert_eq!(file_value.reviewer_max_rounds, 2);
    }

    #[test]
    fn test_explicit_reviewer_max_rounds_override_wins_over_policy_file_value() {
        let policy = ReviewPolicy {
            reviewer_max_rounds: 2,
            ..ReviewPolicy::default()
        };
        let resolved = policy.with_reviewer_max_rounds(Some(5)).unwrap();
        assert_eq!(resolved.reviewer_max_rounds, 5);
    }

    #[test]
    fn test_omitted_reviewer_max_rounds_preserves_policy_file_value() {
        let policy = ReviewPolicy {
            reviewer_max_rounds: 2,
            ..ReviewPolicy::default()
        };
        let resolved = policy.with_reviewer_max_rounds(None).unwrap();
        assert_eq!(resolved.reviewer_max_rounds, 2);
    }

    #[test]
    fn test_reviewer_max_rounds_override_rejects_zero() {
        let error = ReviewPolicy::default()
            .with_reviewer_max_rounds(Some(0))
            .expect_err("zero must be rejected");
        assert!(error.to_string().contains("at least 1"));
    }

    #[test]
    fn test_ci_gate_auto_requires_configured_source_only() {
        assert!(!CiGate::Auto.enabled(false));
        assert!(CiGate::Auto.enabled(true));
        assert!(CiGate::On.enabled(false));
        assert!(!CiGate::Off.enabled(true));
    }
}
