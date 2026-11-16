//! Read the operator-authored harness allowlist for the spawn guard.
//!
//! `tl_loop` owns the selection policy in `tl_loop/select/policy.py` and
//! `tl_loop/select/harness.py`. The Rust spawn guard needs the same notion of
//! "this harness is configured for this role" so a policy-selected route is a
//! routing transition, not an arbitrary explicit cross-harness request. This
//! module reads only the `roles.<role>.allow` identifiers with the same
//! grammar: `harness` or `harness/model`, a lowercase supported harness, and a
//! non-empty model after the separator.
//!
//! The two implementations are pinned to one definition by
//! `tests/fixtures/harness_policy_agreement.{toml,json}`: the Rust test and
//! `tl_loop/tests/test_harness_policy_agreement.py` both load that file and
//! assert the same canonical agent-type sets.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use super::agent_control::AgentType;

const POLICY_ROLES: [&str; 3] = ["tl", "worker", "reviewer"];

#[derive(Debug, thiserror::Error)]
pub enum HarnessPolicyError {
    #[error("harness policy {path}: {message}")]
    Invalid { path: PathBuf, message: String },
}

/// Canonical agent types the operator allows for each controller role.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HarnessPolicyAllowlist {
    roles: BTreeMap<String, Vec<AgentType>>,
}

impl HarnessPolicyAllowlist {
    /// Load the allowlist. A missing file is not an error: the configured
    /// `config.toml` route remains the only authorized route.
    pub fn load(path: &Path) -> Result<Option<Self>, HarnessPolicyError> {
        let contents = match std::fs::read_to_string(path) {
            Ok(contents) => contents,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(HarnessPolicyError::Invalid {
                    path: path.to_path_buf(),
                    message: error.to_string(),
                });
            }
        };
        Self::parse(&contents)
            .map(Some)
            .map_err(|message| HarnessPolicyError::Invalid {
                path: path.to_path_buf(),
                message,
            })
    }

    /// Parse one already-read policy document.
    pub fn parse(contents: &str) -> Result<Self, String> {
        let document: toml::Value = contents
            .parse()
            .map_err(|error| format!("invalid TOML: {error}"))?;
        let roles = document
            .get("roles")
            .and_then(toml::Value::as_table)
            .ok_or_else(|| "roles: must be a table".to_string())?;
        let mut parsed = BTreeMap::new();
        for role in POLICY_ROLES {
            let table = roles
                .get(role)
                .and_then(toml::Value::as_table)
                .ok_or_else(|| format!("roles.{role}: must be a table"))?;
            let identifiers = table
                .get("allow")
                .and_then(toml::Value::as_array)
                .ok_or_else(|| format!("roles.{role}.allow: must be an array"))?;
            if identifiers.is_empty() {
                return Err(format!("roles.{role}.allow: must not be empty"));
            }
            let mut allowed = Vec::new();
            for (index, identifier) in identifiers.iter().enumerate() {
                let identifier = identifier
                    .as_str()
                    .ok_or_else(|| format!("roles.{role}.allow[{index}]: must be a string"))?;
                let agent_type = parse_allowlist_identifier(identifier)
                    .map_err(|error| format!("roles.{role}.allow[{index}]: {error}"))?;
                if !allowed.contains(&agent_type) {
                    allowed.push(agent_type);
                }
            }
            parsed.insert(role.to_string(), allowed);
        }
        Ok(Self { roles: parsed })
    }

    /// Return whether the operator allows this harness for the role.
    pub fn allows(&self, role: &str, agent_type: AgentType) -> bool {
        self.roles
            .get(role)
            .is_some_and(|allowed| allowed.contains(&agent_type))
    }

    /// Canonical agent types allowed for one role, in policy order.
    pub fn allowed(&self, role: &str) -> &[AgentType] {
        self.roles.get(role).map(Vec::as_slice).unwrap_or(&[])
    }
}

/// Map the controller's role spellings onto the three policy roles.
pub(crate) fn policy_role(role: &str) -> Option<&'static str> {
    match role.trim().to_ascii_lowercase().as_str() {
        "reviewer" => Some("reviewer"),
        "tl" | "root" => Some("tl"),
        "worker" | "dev" | "dev_leaf" | "dev-leaf" | "leaf" | "teammate" => Some("worker"),
        _ => None,
    }
}

fn parse_allowlist_identifier(identifier: &str) -> Result<AgentType, String> {
    let trimmed = identifier.trim();
    if trimmed.is_empty() {
        return Err("must be a non-empty string".to_string());
    }
    let (harness, model) = match trimmed.split_once('/') {
        Some((harness, model)) => (harness, Some(model)),
        None => (trimmed, None),
    };
    if model.is_some_and(|model| model.trim().is_empty()) {
        return Err("model-qualified harness must include a non-empty model".to_string());
    }
    match harness.trim().to_ascii_lowercase().as_str() {
        "claude" => Ok(AgentType::Claude),
        "codex" => Ok(AgentType::Codex),
        "opencode" => Ok(AgentType::OpenCode),
        "shoal" => Ok(AgentType::Shoal),
        other => Err(format!("unsupported agent type {other:?}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture_directory() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
    }

    #[test]
    fn allowlist_matches_the_shared_cross_language_fixture() {
        let directory = fixture_directory();
        let policy = HarnessPolicyAllowlist::load(&directory.join("harness_policy_agreement.toml"))
            .expect("shared policy fixture parses")
            .expect("shared policy fixture exists");
        let expected: BTreeMap<String, Vec<String>> = serde_json::from_str(
            &std::fs::read_to_string(directory.join("harness_policy_agreement.json"))
                .expect("read shared expectation"),
        )
        .expect("shared expectation is JSON");

        for role in POLICY_ROLES {
            let expected_types: Vec<AgentType> = expected[role]
                .iter()
                .map(|value| parse_allowlist_identifier(value).expect("expected harness parses"))
                .collect();
            assert_eq!(
                policy.allowed(role),
                expected_types.as_slice(),
                "role {role} canonical allowlist disagrees with the shared fixture"
            );
        }
    }

    #[test]
    fn missing_policy_is_not_an_error_and_invalid_policy_is_rejected() {
        let temp = tempfile::tempdir().expect("temporary policy directory");
        let missing = temp.path().join("missing.toml");
        assert_eq!(
            HarnessPolicyAllowlist::load(&missing).expect("missing is optional"),
            None
        );

        let invalid = temp.path().join("invalid.toml");
        std::fs::write(
            &invalid,
            "[roles.tl]\nallow = [\"retired/gemini\"]\n[roles.worker]\nallow = [\"codex\"]\n[roles.reviewer]\nallow = [\"codex\"]\n",
        )
        .expect("write invalid policy");
        assert!(HarnessPolicyAllowlist::load(&invalid).is_err());
    }

    #[test]
    fn allowlist_role_spellings_match_the_python_policy_roles() {
        for spelling in ["worker", "dev", "dev_leaf", "dev-leaf", "leaf", "teammate"] {
            assert_eq!(policy_role(spelling), Some("worker"));
        }
        assert_eq!(policy_role("tl"), Some("tl"));
        assert_eq!(policy_role("root"), Some("tl"));
        assert_eq!(policy_role("reviewer"), Some("reviewer"));
        assert_eq!(policy_role("companion"), None);
    }
}
