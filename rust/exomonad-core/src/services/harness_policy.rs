//! Read the operator-authored harness allowlist for the spawn guard and init.
//!
//! `tl_loop` owns the selection policy in `tl_loop/select/policy.py` and
//! `tl_loop/select/harness.py`. The Rust spawn guard needs the same notion of
//! "this harness is configured for this role" so a policy-selected route is a
//! routing transition, not an arbitrary explicit cross-harness request. This
//! module reads only the `roles.<role>.allow` identifiers with the same
//! grammar: `harness` or `harness/model`, a supported harness name
//! (case-insensitive), and a non-empty model after the first separator. The
//! model is everything after that separator, exactly as the Python parser
//! reads it.
//!
//! The two implementations are pinned to one definition by
//! `tests/fixtures/harness_policy_agreement.{toml,json}`: the Rust test and
//! `tl_loop/tests/test_harness_policy_agreement.py` both load that file and
//! assert the same entries, grammar, and route decisions.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use super::agent_control::AgentType;

const POLICY_ROLES: [&str; 3] = ["tl", "worker", "reviewer"];

#[derive(Debug, thiserror::Error)]
pub enum HarnessPolicyError {
    #[error("harness policy {path}: {message}")]
    Invalid { path: PathBuf, message: String },
}

/// One `harness` or `harness/model` entry from a role allow list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HarnessPolicyEntry {
    pub agent_type: AgentType,
    pub model: Option<String>,
}

impl HarnessPolicyEntry {
    /// Parse one allow-list identifier with the Python grammar.
    ///
    /// The whole identifier is trimmed first, the harness is trimmed and
    /// lowercased, and the model is everything after the first `/`, trimmed
    /// but otherwise preserved (including any later `/`).
    pub fn parse(identifier: &str) -> Result<Self, String> {
        let trimmed = identifier.trim();
        if trimmed.is_empty() {
            return Err("must be a non-empty string".to_string());
        }
        let (harness, model) = match trimmed.split_once('/') {
            Some((harness, model)) => (harness, Some(model)),
            None => (trimmed, None),
        };
        let model = match model {
            Some(model) => {
                let model = model.trim();
                if model.is_empty() {
                    return Err(
                        "model-qualified harness must include a non-empty model".to_string()
                    );
                }
                Some(model.to_string())
            }
            None => None,
        };
        let agent_type = match harness.trim().to_ascii_lowercase().as_str() {
            "claude" => AgentType::Claude,
            "codex" => AgentType::Codex,
            "opencode" => AgentType::OpenCode,
            "shoal" => AgentType::Shoal,
            other => return Err(format!("unsupported agent type {other:?}")),
        };
        Ok(Self { agent_type, model })
    }

    /// The canonical `harness` or `harness/model` spelling of this entry.
    pub fn identifier(&self) -> String {
        match &self.model {
            Some(model) => format!("{}/{}", self.agent_type.suffix(), model),
            None => self.agent_type.suffix().to_string(),
        }
    }
}

/// Why a configured role route is outside the operator's allow list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RoutePolicyMismatch {
    /// The harness itself does not appear in the role allow list.
    HarnessNotAllowed,
    /// Every allow entry for the harness is model-qualified and none names the
    /// configured model.
    ModelNotAllowed { allowed_models: Vec<String> },
}

/// Canonical entries the operator allows for each controller role.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HarnessPolicyAllowlist {
    roles: BTreeMap<String, Vec<HarnessPolicyEntry>>,
    /// Derived harness index for the spawn guard's harness-only question.
    agent_types: BTreeMap<String, Vec<AgentType>>,
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
        let mut agent_types = BTreeMap::new();
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
            let mut types = Vec::new();
            for (index, identifier) in identifiers.iter().enumerate() {
                let identifier = identifier
                    .as_str()
                    .ok_or_else(|| format!("roles.{role}.allow[{index}]: must be a string"))?;
                let entry = HarnessPolicyEntry::parse(identifier)
                    .map_err(|error| format!("roles.{role}.allow[{index}]: {error}"))?;
                if !allowed.contains(&entry) {
                    if !types.contains(&entry.agent_type) {
                        types.push(entry.agent_type);
                    }
                    allowed.push(entry);
                }
            }
            parsed.insert(role.to_string(), allowed);
            agent_types.insert(role.to_string(), types);
        }
        Ok(Self {
            roles: parsed,
            agent_types,
        })
    }

    /// Return whether the operator allows this harness for the role.
    ///
    /// This is the spawn guard's harness-only question. A bare or
    /// model-qualified entry both authorize the harness.
    pub fn allows(&self, role: &str, agent_type: AgentType) -> bool {
        self.agent_types
            .get(role)
            .is_some_and(|allowed| allowed.contains(&agent_type))
    }

    /// Canonical agent types allowed for one role, in policy order.
    pub fn allowed(&self, role: &str) -> &[AgentType] {
        self.agent_types.get(role).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Every canonical entry allowed for one role, in policy order.
    pub fn entries(&self, role: &str) -> &[HarnessPolicyEntry] {
        self.roles.get(role).map(Vec::as_slice).unwrap_or(&[])
    }

    /// Return why a configured route is outside the allow list, or `None`.
    ///
    /// A harness must appear in the role's allow list. When the configured
    /// route names a model and every entry for that harness is
    /// model-qualified, the model must equal one of those entries. A bare
    /// harness entry authorizes any model of that harness.
    pub fn route_mismatch(
        &self,
        role: &str,
        agent_type: AgentType,
        model: Option<&str>,
    ) -> Option<RoutePolicyMismatch> {
        let matching: Vec<&HarnessPolicyEntry> = self
            .entries(role)
            .iter()
            .filter(|entry| entry.agent_type == agent_type)
            .collect();
        if matching.is_empty() {
            return Some(RoutePolicyMismatch::HarnessNotAllowed);
        }
        if matching.iter().any(|entry| entry.model.is_none()) {
            return None;
        }
        let model = model?.trim();
        let allowed_models: Vec<String> = matching
            .iter()
            .filter_map(|entry| entry.model.clone())
            .collect();
        if allowed_models.iter().any(|allowed| allowed == model) {
            None
        } else {
            Some(RoutePolicyMismatch::ModelNotAllowed { allowed_models })
        }
    }
}

/// Map the controller's role spellings onto the three policy roles.
pub fn policy_role(role: &str) -> Option<&'static str> {
    match role.trim().to_ascii_lowercase().as_str() {
        "reviewer" => Some("reviewer"),
        "tl" | "root" => Some("tl"),
        "worker" | "dev" | "dev_leaf" | "dev-leaf" | "leaf" | "teammate" => Some("worker"),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    fn fixture_directory() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
    }

    #[derive(Debug, Deserialize)]
    struct AgreementFixture {
        canonical: BTreeMap<String, Vec<String>>,
        entries: BTreeMap<String, Vec<EntryExpectation>>,
        grammar: Vec<GrammarExpectation>,
        invalid_grammar: Vec<String>,
        route_cases: Vec<RouteCase>,
    }

    #[derive(Debug, Deserialize)]
    struct EntryExpectation {
        identifier: String,
        harness: String,
        model: Option<String>,
    }

    #[derive(Debug, Deserialize)]
    struct GrammarExpectation {
        identifier: String,
        harness: String,
        model: Option<String>,
    }

    #[derive(Debug, Deserialize)]
    struct RouteCase {
        role: String,
        harness: String,
        model: Option<String>,
        allowed: bool,
    }

    fn agreement_fixture() -> AgreementFixture {
        let directory = fixture_directory();
        serde_json::from_str(
            &std::fs::read_to_string(directory.join("harness_policy_agreement.json"))
                .expect("read shared expectation"),
        )
        .expect("shared expectation is JSON")
    }

    fn shared_policy() -> HarnessPolicyAllowlist {
        let directory = fixture_directory();
        HarnessPolicyAllowlist::load(&directory.join("harness_policy_agreement.toml"))
            .expect("shared policy fixture parses")
            .expect("shared policy fixture exists")
    }

    #[test]
    fn allowlist_matches_the_shared_cross_language_fixture() {
        let policy = shared_policy();
        let fixture = agreement_fixture();

        for role in POLICY_ROLES {
            let expected_types: Vec<AgentType> = fixture.canonical[role]
                .iter()
                .map(|value| {
                    HarnessPolicyEntry::parse(value)
                        .expect("expected harness parses")
                        .agent_type
                })
                .collect();
            assert_eq!(
                policy.allowed(role),
                expected_types.as_slice(),
                "role {role} canonical allowlist disagrees with the shared fixture"
            );

            let expected_entries: Vec<HarnessPolicyEntry> = fixture.entries[role]
                .iter()
                .map(|entry| {
                    let parsed =
                        HarnessPolicyEntry::parse(&entry.identifier).expect("entry parses");
                    assert_eq!(parsed.agent_type.suffix(), entry.harness);
                    assert_eq!(parsed.model, entry.model);
                    parsed
                })
                .collect();
            assert_eq!(
                policy.entries(role),
                expected_entries.as_slice(),
                "role {role} entries disagree with the shared fixture"
            );
        }
    }

    #[test]
    fn allowlist_grammar_matches_the_shared_python_fixture() {
        let fixture = agreement_fixture();
        for row in fixture.grammar {
            let parsed = HarnessPolicyEntry::parse(&row.identifier)
                .unwrap_or_else(|error| panic!("{}: {error}", row.identifier));
            assert_eq!(
                parsed.agent_type.suffix(),
                row.harness,
                "{}",
                row.identifier
            );
            assert_eq!(parsed.model, row.model, "{}", row.identifier);
        }
        for identifier in fixture.invalid_grammar {
            assert!(
                HarnessPolicyEntry::parse(&identifier).is_err(),
                "the shared fixture expects {identifier:?} to be invalid"
            );
        }
    }

    #[test]
    fn route_cases_match_the_shared_cross_language_fixture() {
        let policy = shared_policy();
        let fixture = agreement_fixture();
        for case in fixture.route_cases {
            let agent_type = HarnessPolicyEntry::parse(&case.harness)
                .expect("route case harness parses")
                .agent_type;
            let mismatch = policy.route_mismatch(&case.role, agent_type, case.model.as_deref());
            assert_eq!(
                mismatch.is_none(),
                case.allowed,
                "role {} route {:?}/{:?} disagrees with the shared fixture: {mismatch:?}",
                case.role,
                case.harness,
                case.model
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

    #[test]
    fn model_qualified_entries_keep_every_model_for_one_harness() {
        let policy = HarnessPolicyAllowlist::parse(
            "[roles.tl]\nallow = [\"codex/a\"]\n[roles.worker]\nallow = [\"codex/a\", \"codex/b\"]\n[roles.reviewer]\nallow = [\"claude/sonnet\"]\n",
        )
        .expect("policy parses");

        assert_eq!(
            policy
                .entries("worker")
                .iter()
                .map(HarnessPolicyEntry::identifier)
                .collect::<Vec<_>>(),
            vec!["codex/a".to_string(), "codex/b".to_string()]
        );
        assert!(policy
            .route_mismatch("worker", AgentType::Codex, Some("b"))
            .is_none());
        assert_eq!(
            policy.route_mismatch("worker", AgentType::Codex, Some("c")),
            Some(RoutePolicyMismatch::ModelNotAllowed {
                allowed_models: vec!["a".to_string(), "b".to_string()]
            })
        );
    }

    #[test]
    fn bare_harness_entry_allows_any_model() {
        let policy = HarnessPolicyAllowlist::parse(
            "[roles.tl]\nallow = [\"codex/a\"]\n[roles.worker]\nallow = [\"opencode\"]\n[roles.reviewer]\nallow = [\"claude/sonnet\"]\n",
        )
        .expect("policy parses");

        assert!(policy
            .route_mismatch("worker", AgentType::OpenCode, Some("anything"))
            .is_none());
        assert!(policy
            .route_mismatch("worker", AgentType::OpenCode, None)
            .is_none());
    }

    #[test]
    fn harness_and_model_disagreements_are_distinct() {
        let policy = HarnessPolicyAllowlist::parse(
            "[roles.tl]\nallow = [\"codex/a\"]\n[roles.worker]\nallow = [\"codex/a\"]\n[roles.reviewer]\nallow = [\"claude/sonnet\"]\n",
        )
        .expect("policy parses");

        assert_eq!(
            policy.route_mismatch("worker", AgentType::OpenCode, None),
            Some(RoutePolicyMismatch::HarnessNotAllowed)
        );
        assert_eq!(
            policy.route_mismatch("worker", AgentType::Codex, Some("b")),
            Some(RoutePolicyMismatch::ModelNotAllowed {
                allowed_models: vec!["a".to_string()]
            })
        );
    }
}
