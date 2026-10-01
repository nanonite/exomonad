//! Which Codex model the shipped scaffold provisions, and refusing an unusable one.
//!
//! The harness policy key is `codex/<model>`. The controller splits it
//! (`parse_harness_identifier`, `tl_loop/select/harness.py`) and the model half
//! becomes `model = ...` in the generated child config, so the name written
//! into `.exo/harness_policy.toml` is the name the worker asks for.
//!
//! Scaffolding used to hard-code `gpt-luna`. A ChatGPT-account Codex login
//! rejects it before the worker's first inference:
//!
//! ```text
//! 400 invalid_request_error: The 'gpt-luna' model is not supported when using
//! Codex with a ChatGPT account.
//! ```
//!
//! so every scaffolded project provisioned a worker that could not take a turn
//! — and the failure surfaced as a dispatch timeout rather than as a model
//! name. The scaffold therefore resolves the model from an explicit source
//! instead of guessing one, and fails fast when it cannot. Chainlink #1149.

use anyhow::{anyhow, Context, Result};
use std::path::PathBuf;

/// The model earlier revisions of the scaffold hard-coded, and which a
/// ChatGPT-account Codex login rejects. Naming it here is what makes the
/// refusal a regression guard rather than a silent regression.
pub(crate) const REJECTED_SCAFFOLD_MODEL: &str = "gpt-luna";

/// The model the harness policy names, as a `codex/<model>` harness key.
pub(crate) const HARNESS_AGENT_TYPE: &str = "codex";

/// Explicit override for the model the scaffold provisions, taking precedence
/// over the host Codex config. Same shape as `EXOMONAD_TL_MODEL` /
/// `EXOMONAD_WORKER_MODEL`; this one is read before any file is written.
pub(crate) const CODEX_MODEL_ENV: &str = "EXOMONAD_CODEX_MODEL";

/// `CODEX_HOME` is the variable Codex itself honors, so the scaffold reads the
/// same config file the operator's own `codex` invocation reads.
pub(crate) const CODEX_HOME_ENV: &str = "CODEX_HOME";

/// Look up an environment variable, or `None` when it is unset or blank.
///
/// Injected rather than read from the process environment so the resolution
/// order is testable without mutating the environment the tests run in.
pub(crate) type EnvLookup<'a> = &'a dyn Fn(&str) -> Option<String>;

/// Resolve the harness key the scaffold writes into the harness policy.
///
/// Precedence: the explicit `EXOMONAD_CODEX_MODEL` override, then the host
/// Codex config's top-level `model`. There is no built-in default — a scaffold
/// that guesses a model name guesses wrong against whatever account it runs
/// on, which is the failure this module exists to remove.
pub(crate) fn resolve_scaffold_harness(env: EnvLookup<'_>) -> Result<String> {
    let model = match non_blank(env(CODEX_MODEL_ENV)) {
        Some(model) => model,
        None => host_config_model(&host_config_path(env))?,
    };
    reject_unrunnable_model(&model)?;
    Ok(format!("{HARNESS_AGENT_TYPE}/{model}"))
}

/// Refuse a model the account is known to reject.
///
/// This is a guard against re-introducing the rejected scaffold default, not a
/// capability oracle: every model probed against a ChatGPT-account login is
/// rejected the same way, so only the account's own config says which models
/// run. A name that passes here can still be refused by the account, and the
/// failure stays visible as the account's own 400 rather than as a park.
fn reject_unrunnable_model(model: &str) -> Result<()> {
    if model == REJECTED_SCAFFOLD_MODEL {
        anyhow::bail!(
            "refusing to provision Codex model '{model}': a ChatGPT-account Codex login rejects it with \
             \"400 invalid_request_error: The '{model}' model is not supported when using Codex with a \
             ChatGPT account.\" Name a model the account can run — the top-level `model` in your Codex \
             config is read automatically, or set {CODEX_MODEL_ENV}. See Chainlink #1149."
        );
    }
    Ok(())
}

/// The Codex config the scaffold reads the model from.
fn host_config_path(env: EnvLookup<'_>) -> PathBuf {
    if let Some(home) = non_blank(env(CODEX_HOME_ENV)) {
        return PathBuf::from(home).join("config.toml");
    }
    let home = env("HOME").unwrap_or_default();
    PathBuf::from(home).join(".codex").join("config.toml")
}

/// The top-level `model` from a host Codex config.
///
/// A missing file, an unparseable file, and a file that names no model are all
/// the same operator-facing situation — there is no model to scaffold — so all
/// three name the override that supplies one.
fn host_config_model(path: &std::path::Path) -> Result<String> {
    let text = std::fs::read_to_string(path).map_err(|error| {
        anyhow!(
            "cannot resolve a Codex model to scaffold: no host Codex config at {}. \
             Set {CODEX_MODEL_ENV} to a model your Codex account can run. ({error})",
            path.display()
        )
    })?;
    let document = toml::from_str::<toml::Value>(&text)
        .with_context(|| format!("cannot parse the host Codex config at {}", path.display()))?;
    document
        .get("model")
        .and_then(toml::Value::as_str)
        .map(str::trim)
        .filter(|model| !model.is_empty())
        .map(str::to_string)
        .ok_or_else(|| {
            anyhow!(
                "cannot resolve a Codex model to scaffold: {} sets no top-level `model`. \
                 Set {CODEX_MODEL_ENV} to a model your Codex account can run.",
                path.display()
            )
        })
}

/// An owned, trimmed string, or `None` when the value is absent or blank.
fn non_blank(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    /// A model the account can actually run. Not a default — only ever the
    /// value a test supplies through one of the sources.
    const SUPPORTED: &str = "gpt-5.6-luna";

    /// The host a resolution reads: an environment plus the Codex home it points
    /// at, so a test describes both halves of the resolution in one place.
    struct Host {
        env: HashMap<String, String>,
        /// Held, never read: dropping it would delete the config the resolver
        /// is about to look for.
        _home: tempfile::TempDir,
    }

    impl Host {
        /// A host whose Codex config holds `body`, or no config file at all.
        fn with_config(body: Option<&str>) -> Self {
            let home = tempfile::tempdir().unwrap();
            if let Some(body) = body {
                std::fs::write(home.path().join("config.toml"), body).unwrap();
            }
            let home_path = home.path().display().to_string();
            let mut host = Self {
                env: HashMap::new(),
                _home: home,
            };
            host.set(CODEX_HOME_ENV, &home_path);
            host
        }

        fn set(&mut self, name: &str, value: &str) -> &mut Self {
            self.env.insert(name.to_string(), value.to_string());
            self
        }

        fn env(&self) -> impl Fn(&str) -> Option<String> + '_ {
            move |name: &str| self.env.get(name).cloned()
        }
    }

    #[test]
    fn resolves_the_explicit_override() {
        let mut host = Host::with_config(Some("model = \"from-config\"\n"));
        host.set(CODEX_MODEL_ENV, SUPPORTED);
        assert_eq!(
            resolve_scaffold_harness(&host.env()).unwrap(),
            format!("codex/{SUPPORTED}")
        );
    }

    #[test]
    fn falls_back_to_the_host_config() {
        let host = Host::with_config(Some(&format!("model = \"{SUPPORTED}\"\n")));
        assert_eq!(
            resolve_scaffold_harness(&host.env()).unwrap(),
            format!("codex/{SUPPORTED}")
        );
    }

    #[test]
    fn reads_the_model_a_bare_codex_invocation_uses() {
        // A bare `codex` run with no --model flag takes the config's model, so
        // that is the one already known to work on this account.
        let host = Host::with_config(Some(&format!(
            "model_reasoning_effort = \"xhigh\"\nmodel = \"{SUPPORTED}\"\n"
        )));
        assert_eq!(
            resolve_scaffold_harness(&host.env()).unwrap(),
            format!("codex/{SUPPORTED}")
        );
    }

    #[test]
    fn refuses_the_rejected_scaffold_model_from_the_override() {
        let mut host = Host::with_config(None);
        host.set(CODEX_MODEL_ENV, REJECTED_SCAFFOLD_MODEL);
        assert_refused(&host);
    }

    #[test]
    fn refuses_the_rejected_scaffold_model_from_the_host_config() {
        let host = Host::with_config(Some(&format!("model = \"{REJECTED_SCAFFOLD_MODEL}\"\n")));
        assert_refused(&host);
    }

    #[test]
    fn fails_rather_than_guessing_when_there_is_no_model() {
        let host = Host::with_config(None);
        let error = resolve_scaffold_harness(&host.env())
            .unwrap_err()
            .to_string();
        assert!(error.contains(CODEX_MODEL_ENV), "{error}");
        assert!(error.contains("config.toml"), "{error}");
    }

    #[test]
    fn fails_when_the_host_config_names_no_model() {
        let host = Host::with_config(Some("# nothing but a comment\n"));
        let error = resolve_scaffold_harness(&host.env())
            .unwrap_err()
            .to_string();
        assert!(error.contains("no top-level `model`"), "{error}");
        assert!(error.contains(CODEX_MODEL_ENV), "{error}");
    }

    #[test]
    fn fails_when_the_host_config_does_not_parse() {
        let host = Host::with_config(Some("model = \n"));
        let error = resolve_scaffold_harness(&host.env())
            .unwrap_err()
            .to_string();
        assert!(error.contains("cannot parse"), "{error}");
    }

    #[test]
    fn treats_a_blank_override_as_unset() {
        let mut host = Host::with_config(Some(&format!("model = \"{SUPPORTED}\"\n")));
        host.set(CODEX_MODEL_ENV, "   ");
        assert_eq!(
            resolve_scaffold_harness(&host.env()).unwrap(),
            format!("codex/{SUPPORTED}")
        );
    }

    #[test]
    fn reads_home_when_codex_home_is_unset() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(home.path().join(".codex")).unwrap();
        std::fs::write(
            home.path().join(".codex/config.toml"),
            format!("model = \"{SUPPORTED}\"\n"),
        )
        .unwrap();
        let env: HashMap<String, String> =
            [("HOME".to_string(), home.path().display().to_string())]
                .into_iter()
                .collect();
        let lookup = |name: &str| env.get(name).cloned();
        assert_eq!(
            resolve_scaffold_harness(&lookup).unwrap(),
            format!("codex/{SUPPORTED}")
        );
    }

    /// The refusal has to name the model and the account's own reason: a
    /// message that only said "unsupported" sends the operator hunting.
    fn assert_refused(host: &Host) {
        let error = resolve_scaffold_harness(&host.env())
            .unwrap_err()
            .to_string();
        assert!(error.contains(REJECTED_SCAFFOLD_MODEL), "{error}");
        assert!(error.contains("not supported when using Codex"), "{error}");
        assert!(error.contains(CODEX_MODEL_ENV), "{error}");
        assert!(error.contains("1149"), "{error}");
    }
}
