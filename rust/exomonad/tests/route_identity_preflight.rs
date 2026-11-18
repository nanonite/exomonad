//! Public-API pin for the accepted role-routing identity (#1433).
//!
//! `serve` loads project configuration; `init` repeats the same effective
//! values as explicit flags and marks their provenance `Cli`. Both must agree
//! on one accepted configuration identity before a controller is released.

use exomonad::config::{Config, EffortLevel, ResolvedEffort, RouteValueSource};

const PROJECT_CONFIG: &str = "root_agent_type = 'opencode'\nmodel = 'opencode-go/glm-5.3-flash'\ntl_effort_level = 'high'\nspawn_agent_type = 'opencode'\nworker_model = 'opencode-go/deepseek-v4.1-flash'\nworker_effort_level = 'high'\n[reviewer]\nagent_type = 'opencode'\nmodel = 'opencode-go/glm-5.3-flash'\neffort_level = 'high'\n";

fn project_config() -> (tempfile::TempDir, Config) {
    let project = tempfile::tempdir().unwrap();
    let exo = project.path().join(".exo");
    std::fs::create_dir_all(&exo).unwrap();
    std::fs::write(exo.join("config.toml"), PROJECT_CONFIG).unwrap();
    let config = Config::discover_from(project.path(), &|_| None).unwrap();
    (project, config)
}

fn as_init_cli_repetition(config: &mut Config) {
    config.spawn_agent_type_source = RouteValueSource::Cli;
    config.worker_model_source = RouteValueSource::Cli;
    config.opencode_worker_model_source = RouteValueSource::Cli;
    config.worker_effort_level = ResolvedEffort::from_cli(EffortLevel::High);
    config.reviewer_agent_type_source = RouteValueSource::Cli;
    config.reviewer_model_source = RouteValueSource::Cli;
    config.reviewer_effort_level = ResolvedEffort::from_cli(EffortLevel::High);
}

#[test]
fn repeating_project_values_as_cli_flags_keeps_one_effective_identity() {
    let (_project, serve) = project_config();
    let mut init = serve.clone();
    as_init_cli_repetition(&mut init);

    assert_eq!(
        serve.routing_configuration_fingerprint(),
        init.routing_configuration_fingerprint(),
        "source provenance must not change the effective role configuration identity"
    );
    assert!(serve
        .routing_configuration_fingerprint()
        .starts_with("sha256:v2:"));
}

#[test]
fn a_changed_effective_value_changes_the_identity_across_public_api() {
    let (_project, serve) = project_config();
    let mut changed = serve.clone();
    changed.worker_model = Some("opencode-go/another-model".to_string());

    assert_ne!(
        serve.routing_configuration_fingerprint(),
        changed.routing_configuration_fingerprint()
    );
}
