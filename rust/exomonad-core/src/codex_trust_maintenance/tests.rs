//! Tests for historical ExoMonad Codex hook-trust recognition and pruning.
//!
//! The properties under test are the ones the maintenance contract promises: a
//! dry run never writes, a confirmed apply removes only re-proven keys, every
//! ambiguous entry is preserved with a reason, a config that still exists is
//! never a candidate, a concurrent install never loses entries, and repeating
//! the whole thing changes nothing the second time.

use super::*;
use crate::codex_config::{
    install_codex_hook_trust, read_owned_hook_trust_keys, render_codex_config,
};
use std::collections::HashMap;

/// An ExoMonad installation whose canonical digests the tests attest.
const ATTESTED: &str = "/usr/local/bin/exomonad";
/// A second installation, for residue a newer build did not write.
const ATTESTED_OLDER: &str = "/opt/older/bin/exomonad";

fn attested() -> Vec<PathBuf> {
    vec![PathBuf::from(ATTESTED)]
}

/// The digest ExoMonad generates for `event_label` around `binary`.
fn digest(binary: &str, event_label: &str) -> String {
    canonical_hook_trust_hashes(Path::new(binary))
        .expect("canonical digests recompute")
        .into_iter()
        .find(|hash| hash.event_label == event_label)
        .expect("ExoMonad seeds every event")
        .trusted_hash
}

fn string(value: &str) -> toml::Value {
    toml::Value::String(value.to_string())
}

fn record(trusted_hash: &str) -> toml::Value {
    table([("trusted_hash", string(trusted_hash))])
}

fn field(name: &str, value: toml::Value) -> toml::Value {
    table([(name, value)])
}

fn table<const N: usize>(fields: [(&str, toml::Value); N]) -> toml::Value {
    toml::Value::Table(
        fields
            .into_iter()
            .map(|(name, value)| (name.to_string(), value))
            .collect(),
    )
}

fn state_key(config_path: &Path, event_label: &str) -> String {
    format!("{}:{event_label}:0:0", config_path.display())
}

/// A config path for an agent directory that is provably not on disk.
fn gone_config_path(root: &Path, agent: &str) -> PathBuf {
    root.join(format!("repo/.exo/worktrees/{agent}/.codex/config.toml"))
}

fn gone_key(root: &Path, agent: &str, event_label: &str) -> String {
    state_key(&gone_config_path(root, agent), event_label)
}

/// A Codex user config holding a `model` line, a `[[hooks.Stop]]` group that
/// belongs to the operator rather than to ExoMonad, and the given
/// `[hooks.state]` entries.
fn seed_user_config(user_config_path: &Path, entries: &[(String, toml::Value)]) {
    std::fs::create_dir_all(user_config_path.parent().expect("a config parent")).unwrap();
    let config = operator_config(entries);
    std::fs::write(user_config_path, config).expect("the user config is seeded");
}

/// The operator's own Codex config with `entries` under `[hooks.state]`.
///
/// It carries a `model` line and a `[[hooks.Stop]]` hook group that belongs to
/// the operator rather than to ExoMonad, so the preservation tests can assert
/// that a maintenance apply disturbs neither.
fn operator_config(entries: &[(String, toml::Value)]) -> String {
    let mut root = toml::map::Map::new();
    root.insert("model".to_string(), string("gpt-5.6-luna"));
    let mut hooks = toml::map::Map::new();
    hooks.insert(
        "Stop".to_string(),
        toml::Value::Array(vec![operator_stop_group()]),
    );
    hooks.insert("state".to_string(), state_table(entries));
    root.insert("hooks".to_string(), toml::Value::Table(hooks));
    toml::to_string_pretty(&toml::Value::Table(root)).expect("the seeded config serializes")
}

fn state_table(entries: &[(String, toml::Value)]) -> toml::Value {
    let mut state = toml::map::Map::new();
    for (key, entry) in entries {
        state.insert(key.clone(), entry.clone());
    }
    toml::Value::Table(state)
}

/// A `[[hooks.Stop]]` group whose command is the operator's own notify script.
fn operator_stop_group() -> toml::Value {
    let handler = toml::map::Map::from_iter([
        ("type".to_string(), string("command")),
        ("command".to_string(), string("notify-send done")),
    ]);
    toml::Value::Table(toml::map::Map::from_iter([
        ("matcher".to_string(), string("*")),
        (
            "hooks".to_string(),
            toml::Value::Array(vec![toml::Value::Table(handler)]),
        ),
    ]))
}

fn read(user_config_path: &Path) -> String {
    std::fs::read_to_string(user_config_path).expect("the user config is readable")
}

/// The `[hooks.state]` keys still present, or an empty list once ExoMonad has
/// pruned the scaffolding it emptied.
fn state_keys(user_config_path: &Path) -> Vec<String> {
    let raw = read(user_config_path);
    let parsed: toml::Value = toml::from_str(&raw).expect("the user config is valid TOML");
    parsed
        .get("hooks")
        .and_then(|hooks| hooks.get("state"))
        .and_then(toml::Value::as_table)
        .map(|state| state.keys().cloned().collect())
        .unwrap_or_default()
}

fn plan_for(user_config_path: &Path, binaries: &[PathBuf]) -> CodexTrustPrunePlan {
    plan_codex_trust_prune(user_config_path, binaries).expect("the scan succeeds")
}

fn gate_reason(plan: &CodexTrustPrunePlan, key: &str) -> TrustGateReason {
    plan.gated
        .iter()
        .find(|gated| gated.key == key)
        .unwrap_or_else(|| panic!("{key} is gated: {:?}", gated_keys(plan)))
        .reason
        .clone()
}

fn gated_keys(plan: &CodexTrustPrunePlan) -> Vec<&str> {
    plan.gated.iter().map(|gated| gated.key.as_str()).collect()
}

#[test]
fn dry_run_proves_historical_keys_without_writing_anything() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let entries = ["pre_tool_use", "post_tool_use", "stop"]
        .into_iter()
        .map(|event| {
            (
                gone_key(dir.path(), "worker-1-codex", event),
                record(&digest(ATTESTED, event)),
            )
        })
        .collect::<Vec<_>>();
    seed_user_config(&user_config_path, &entries);
    let before = read(&user_config_path);

    let plan = plan_for(&user_config_path, &attested());

    assert_eq!(plan.scanned, 3);
    assert_eq!(plan.proven.len(), 3);
    assert!(
        plan.gated.is_empty(),
        "three re-proven entries and nothing to gate"
    );
    assert_eq!(plan.foreign, 0);
    for proven in &plan.proven {
        assert_eq!(proven.exomonad_binary, PathBuf::from(ATTESTED));
        assert_eq!(proven.trusted_hash, digest(ATTESTED, &proven.event_label));
    }
    assert_eq!(
        read(&user_config_path),
        before,
        "a dry run must not write, not even a normalized rewrite"
    );
}

#[test]
fn apply_removes_the_proven_keys_and_preserves_everything_else() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let proven_key = gone_key(dir.path(), "worker-1-codex", "stop");
    let ambiguous_key = gone_key(dir.path(), "worker-2-codex", "stop");
    let foreign_key = "/elsewhere/.codex/other.toml:stop:0:0".to_string();
    seed_user_config(
        &user_config_path,
        &[
            (proven_key.clone(), record(&digest(ATTESTED, "stop"))),
            (ambiguous_key.clone(), record("sha256:not-exomonad")),
            (foreign_key.clone(), record(&digest(ATTESTED, "stop"))),
        ],
    );

    let plan = plan_for(&user_config_path, &attested());
    assert_eq!(plan.proven.len(), 1);
    assert_eq!(plan.gated.len(), 1);
    assert_eq!(plan.foreign, 1);
    let removal = apply_codex_trust_prune(&plan).expect("the apply succeeds");

    assert_eq!(removal.removed, vec![proven_key.clone()]);
    assert!(!state_keys(&user_config_path).contains(&proven_key));
    let after = read(&user_config_path);
    assert!(after.contains(&ambiguous_key), "ambiguous entry preserved");
    assert!(
        after.contains(&foreign_key),
        "a key outside ExoMonad's shape survives untouched even when its digest matches: the \
         digest proves which hook was hashed, never which config path ExoMonad owned"
    );
}

#[test]
fn residue_outside_any_exomonad_directory_is_still_provable() {
    // The digest is a preimage over ExoMonad's own canonical serialization of
    // `<binary> hook <event> --runtime codex`, and only ExoMonad's installer
    // writes these keys. A deleted e2e cache path is therefore reclaimable even
    // though the directory it named was never part of a live ExoMonad project.
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let cache_key = format!(
        "{}/repo/.exo/worktrees/one-shot-codex/.codex/config.toml:stop:0:0",
        dir.path()
            .join(".cache/exomonad-e2e/one-shot-lifecycle.1qhAfMhC")
            .display()
    );
    assert!(!gone_config_path(dir.path(), "unused").exists());
    seed_user_config(
        &user_config_path,
        &[(cache_key.clone(), record(&digest(ATTESTED, "stop")))],
    );

    let plan = plan_for(&user_config_path, &attested());
    let removal = apply_codex_trust_prune(&plan).expect("the apply succeeds");

    assert_eq!(removal.removed, vec![cache_key]);
    assert!(state_keys(&user_config_path).is_empty());
}

#[test]
fn unrelated_config_and_hooks_survive_verbatim_apart_from_writer_normalization() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let unrelated_key = "/elsewhere/.codex/config.toml:stop:0:0".to_string();
    seed_user_config(
        &user_config_path,
        &[
            (
                gone_key(dir.path(), "worker-1-codex", "stop"),
                record(&digest(ATTESTED, "stop")),
            ),
            (unrelated_key.clone(), record("sha256:elsewhere")),
        ],
    );
    let before = read(&user_config_path);

    let plan = plan_for(&user_config_path, &attested());
    apply_codex_trust_prune(&plan).expect("the apply succeeds");

    let after = read(&user_config_path);
    assert!(
        !after.contains("worker-1-codex"),
        "the proven residue is gone"
    );
    for preserved in [
        "model = \"gpt-5.6-luna\"",
        "[[hooks.Stop]]",
        "command = \"notify-send done\"",
        &unrelated_key,
    ] {
        assert!(
            after.contains(preserved),
            "{preserved} must survive the removal byte-for-byte"
        );
    }
    assert_eq!(
        before.matches("notify-send done").count(),
        after.matches("notify-send done").count(),
        "no hook is added or dropped"
    );
    assert_ne!(
        before, after,
        "the removal did change the file, so the preservation assertions are meaningful"
    );
}

#[test]
fn ambiguous_entries_are_preserved_with_the_reason_they_were_gated() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let unreadable = gone_key(dir.path(), "no-hash", "stop");
    let wrong_type = gone_key(dir.path(), "wrong-type", "stop");
    let extra_fields = gone_key(dir.path(), "extra-fields", "stop");
    let foreign_digest = gone_key(dir.path(), "foreign-digest", "stop");
    let other_binary = gone_key(dir.path(), "other-binary", "stop");
    let live_config = dir.path().join("live/.codex/config.toml");
    let live = state_key(&live_config, "stop");
    seed_user_config(
        &user_config_path,
        &[
            (
                unreadable.clone(),
                field("approved", toml::Value::Boolean(true)),
            ),
            (
                wrong_type.clone(),
                field("trusted_hash", toml::Value::Integer(7)),
            ),
            (
                extra_fields.clone(),
                table([
                    ("trusted_hash", string(&digest(ATTESTED, "stop"))),
                    ("note", string("mine")),
                ]),
            ),
            (foreign_digest.clone(), record("not-a-digest")),
            (
                other_binary.clone(),
                record(&digest(ATTESTED_OLDER, "stop")),
            ),
            (live.clone(), record(&digest(ATTESTED, "stop"))),
        ],
    );
    std::fs::create_dir_all(live_config.parent().unwrap()).unwrap();
    std::fs::write(
        &live_config,
        render_codex_config(
            "live",
            "dev",
            "ctx",
            None,
            &HashMap::new(),
            Path::new(ATTESTED),
            dir.path(),
        ),
    )
    .unwrap();

    let plan = plan_for(&user_config_path, &attested());

    assert!(
        plan.proven.is_empty(),
        "nothing here is provable, so a maintenance run may not remove anything"
    );
    assert_eq!(plan.gated.len(), 6);
    assert!(matches!(
        gate_reason(&plan, &unreadable),
        TrustGateReason::UnreadableRecord { .. }
    ));
    assert!(matches!(
        gate_reason(&plan, &wrong_type),
        TrustGateReason::UnreadableRecord { .. }
    ));
    assert_eq!(
        gate_reason(&plan, &extra_fields),
        TrustGateReason::ExtraRecordFields {
            fields: vec!["note".to_string()]
        }
    );
    assert_eq!(
        gate_reason(&plan, &foreign_digest),
        TrustGateReason::ForeignDigest {
            recorded: "not-a-digest".to_string()
        }
    );
    assert_eq!(
        gate_reason(&plan, &other_binary),
        TrustGateReason::NoAttestedBinary {
            recorded: digest(ATTESTED_OLDER, "stop")
        },
        "a digest from an installation the operator did not attest is unproven, not deletable"
    );
    assert!(matches!(
        gate_reason(&plan, &live),
        TrustGateReason::ConfigStillPresent { .. }
    ));
}

#[test]
fn a_config_that_still_exists_is_gated_even_with_a_matching_digest() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let config_path = dir.path().join("repo/.exo/agents/live/.codex/config.toml");
    std::fs::create_dir_all(config_path.parent().unwrap()).unwrap();
    std::fs::write(
        &config_path,
        render_codex_config(
            "live",
            "dev",
            "ctx",
            None,
            &HashMap::new(),
            Path::new(ATTESTED),
            dir.path(),
        ),
    )
    .unwrap();
    let key = state_key(&config_path, "stop");
    seed_user_config(
        &user_config_path,
        &[(key.clone(), record(&digest(ATTESTED, "stop")))],
    );

    let plan = plan_for(&user_config_path, &attested());

    assert!(plan.proven.is_empty());
    assert_eq!(plan.gated.len(), 1);
    assert_eq!(
        gate_reason(&plan, &key),
        TrustGateReason::ConfigStillPresent {
            config_path: config_path.clone()
        },
        "live trust belongs to the live removal path, which can recompute the digest"
    );
}

#[test]
fn a_dangling_symlink_counts_as_a_config_that_still_exists() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let config_path = dir.path().join("repo/.exo/agents/held/.codex/config.toml");
    std::fs::create_dir_all(config_path.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(dir.path().join("moved-away"), &config_path).unwrap();
    let key = state_key(&config_path, "stop");
    seed_user_config(
        &user_config_path,
        &[(key.clone(), record(&digest(ATTESTED, "stop")))],
    );

    let plan = plan_for(&user_config_path, &attested());

    assert!(plan.proven.is_empty());
    assert_eq!(
        gate_reason(&plan, &key),
        TrustGateReason::ConfigStillPresent { config_path }
    );
}

#[test]
fn keys_outside_exomonads_shape_are_never_candidates() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let config_path = gone_config_path(dir.path(), "worker-1-codex");
    let digest = digest(ATTESTED, "stop");
    let foreign = [
        // A different event label.
        format!("{}:session_start:0:0", config_path.display()),
        // A different handler index, i.e. a second matcher group in a hand-written config.
        format!("{}:stop:1:0", config_path.display()),
        // A different generated filename.
        format!(
            "{}:stop:0:0",
            dir.path().join("repo/.codex/other.toml").display()
        ),
        // Not a generated Codex config location at all.
        format!("{}:stop:0:0", dir.path().join("repo/notes.md").display()),
        // A relative key source.
        ".codex/config.toml:stop:0:0".to_string(),
    ];
    let entries = foreign
        .iter()
        .map(|key| (key.clone(), record(&digest)))
        .collect::<Vec<_>>();
    seed_user_config(&user_config_path, &entries);

    let plan = plan_for(&user_config_path, &attested());

    assert!(plan.proven.is_empty(), "a matching digest is not enough");
    assert!(plan.gated.is_empty());
    assert_eq!(plan.foreign, foreign.len());
    assert_eq!(plan.scanned, foreign.len());
    assert_eq!(state_keys(&user_config_path).len(), foreign.len());
}

#[test]
fn attested_binaries_are_deduplicated_and_all_of_them_prove() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let from_current = gone_key(dir.path(), "new", "stop");
    let from_older = gone_key(dir.path(), "old", "stop");
    seed_user_config(
        &user_config_path,
        &[
            (from_current.clone(), record(&digest(ATTESTED, "stop"))),
            (from_older.clone(), record(&digest(ATTESTED_OLDER, "stop"))),
        ],
    );

    let plan = plan_codex_trust_prune(
        &user_config_path,
        &[
            PathBuf::from(ATTESTED),
            PathBuf::from(ATTESTED_OLDER),
            PathBuf::from(ATTESTED),
        ],
    )
    .unwrap();

    assert_eq!(plan.attested_exomonad_binaries.len(), 2);
    assert_eq!(plan.proven.len(), 2);
    let removal = apply_codex_trust_prune(&plan).unwrap();
    assert_eq!(removal.removed.len(), 2);
}

#[test]
fn attesting_no_binary_is_refused_rather_than_reporting_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    seed_user_config(
        &user_config_path,
        &[(
            gone_key(dir.path(), "worker-1-codex", "stop"),
            record(&digest(ATTESTED, "stop")),
        )],
    );

    let error = plan_codex_trust_prune(&user_config_path, &[]).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidInput);
    assert!(
        error
            .to_string()
            .contains("at least one attested ExoMonad binary"),
        "an empty evidence set is a caller error, not an answer: {error}"
    );
}

#[test]
fn a_missing_user_config_reports_nothing_to_prune_and_creates_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");

    let plan = plan_for(&user_config_path, &attested());

    assert_eq!(plan.scanned, 0);
    assert!(plan.is_empty());
    assert!(
        !user_config_path.exists(),
        "a scan never creates the config"
    );
}

#[test]
fn a_user_config_without_hook_state_reports_nothing_to_prune() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
    std::fs::write(&user_config_path, "model = \"gpt-5.6-luna\"\n").unwrap();

    let plan = plan_for(&user_config_path, &attested());

    assert_eq!(plan.scanned, 0);
    assert!(plan.is_empty());
}

#[test]
fn a_state_table_that_is_not_a_table_fails_closed_without_writing() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
    let malformed = "model = \"gpt-5.6-luna\"\n\n[hooks]\nstate = \"off\"\n";
    std::fs::write(&user_config_path, malformed).unwrap();

    let error = plan_codex_trust_prune(&user_config_path, &attested()).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert_eq!(
        read(&user_config_path),
        malformed,
        "a failed scan leaves the config byte-for-byte unchanged"
    );
}

#[test]
fn an_unparseable_user_config_fails_closed_without_writing() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
    let malformed = "[hooks.state\nbroken = ";
    std::fs::write(&user_config_path, malformed).unwrap();

    let error = plan_codex_trust_prune(&user_config_path, &attested()).unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert_eq!(read(&user_config_path), malformed);
}

#[test]
fn applying_twice_is_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let unrelated_key = "/elsewhere/.codex/config.toml:stop:0:0".to_string();
    seed_user_config(
        &user_config_path,
        &[
            (
                gone_key(dir.path(), "worker-1-codex", "stop"),
                record(&digest(ATTESTED, "stop")),
            ),
            (unrelated_key.clone(), record("sha256:elsewhere")),
        ],
    );

    let first = plan_for(&user_config_path, &attested());
    let first_removal = apply_codex_trust_prune(&first).unwrap();
    let after_first = read(&user_config_path);

    let second = plan_for(&user_config_path, &attested());
    let second_removal = apply_codex_trust_prune(&second).unwrap();

    assert_eq!(first_removal.removed.len(), 1);
    assert!(second.is_empty(), "a second plan has nothing left to prove");
    assert!(second_removal.removed.is_empty());
    assert_eq!(second_removal.checked, 0);
    assert_eq!(
        read(&user_config_path),
        after_first,
        "the second apply does not even rewrite the file"
    );
    assert_eq!(state_keys(&user_config_path), vec![unrelated_key]);
}

#[test]
fn a_concurrent_install_and_prune_never_lose_either_side() {
    const PRUNERS: usize = 4;
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    let unrelated_key = "/elsewhere/.codex/config.toml:stop:0:0".to_string();
    let residue: Vec<(String, toml::Value)> = ["pre_tool_use", "post_tool_use", "stop"]
        .into_iter()
        .map(|event| {
            (
                gone_key(dir.path(), &format!("residue-{event}"), event),
                record(&digest(ATTESTED, event)),
            )
        })
        .collect();
    let mut entries = residue.clone();
    entries.push((unrelated_key.clone(), record("sha256:elsewhere")));
    seed_user_config(&user_config_path, &entries);

    // A live agent whose config exists: its trust is gated, so a concurrent
    // install and prune must both leave it alone.
    let live_config = dir.path().join("repo/.exo/agents/live/.codex/config.toml");
    std::fs::create_dir_all(live_config.parent().unwrap()).unwrap();
    std::fs::write(
        &live_config,
        render_codex_config(
            "live",
            "dev",
            "ctx",
            None,
            &HashMap::new(),
            Path::new(ATTESTED),
            dir.path(),
        ),
    )
    .unwrap();

    let installer = {
        let user_config_path = user_config_path.clone();
        let live_config = live_config.clone();
        std::thread::spawn(move || {
            install_codex_hook_trust(&user_config_path, &live_config).expect("install succeeds");
        })
    };
    let pruners: Vec<_> = (0..PRUNERS)
        .map(|_| {
            let user_config_path = user_config_path.clone();
            std::thread::spawn(move || {
                let plan = plan_codex_trust_prune(&user_config_path, &attested())
                    .expect("each scan succeeds");
                apply_codex_trust_prune(&plan).expect("each apply succeeds");
            })
        })
        .collect();
    installer.join().unwrap();
    for pruner in pruners {
        pruner.join().unwrap();
    }

    let keys = state_keys(&user_config_path);
    for (key, _) in &residue {
        assert!(!keys.contains(key), "{key} was proven and removed");
    }
    assert!(
        keys.contains(&unrelated_key),
        "an unrelated key is never lost"
    );
    let owned = read_owned_hook_trust_keys(&live_config).expect("live trust is readable");
    for owned_key in owned {
        assert!(
            keys.contains(&owned_key.key),
            "the concurrently installed trust for a live agent survived every prune"
        );
    }
}

#[test]
fn an_empty_state_table_prunes_the_scaffolding_it_emptied() {
    let dir = tempfile::tempdir().unwrap();
    let user_config_path = dir.path().join("codex-home/config.toml");
    seed_user_config(
        &user_config_path,
        &[(
            gone_key(dir.path(), "worker-1-codex", "stop"),
            record(&digest(ATTESTED, "stop")),
        )],
    );

    let plan = plan_for(&user_config_path, &attested());
    let removal = apply_codex_trust_prune(&plan).unwrap();

    assert!(removal.pruned_empty_hooks_state);
    let after = read(&user_config_path);
    assert!(
        !after.contains("[hooks.state]"),
        "the emptied state is gone"
    );
    assert!(
        after.contains("[[hooks.Stop]]"),
        "the operator's own hook group is not ExoMonad scaffolding and stays"
    );
    assert!(after.contains("model = \"gpt-5.6-luna\""));
}
