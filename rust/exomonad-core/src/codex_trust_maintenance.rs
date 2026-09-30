//! Recognition and safe pruning of *historical* ExoMonad Codex hook-trust residue.
//!
//! [`crate::codex_config::uninstall_codex_hook_trust`] removes trust by reading
//! the generated `.codex/config.toml` that proves ExoMonad wrote an entry. That
//! proof is gone once the worktree is deleted: the residue of a deleted ExoMonad
//! worktree, of an e2e run whose cache directory was thrown away, or of an
//! ExoMonad version that no longer renders the same hook, is a `[hooks.state]`
//! key whose config no longer exists. This module is how an operator inspects
//! and reclaims that residue *without* a global heuristic sweep.
//!
//! # Ownership evidence
//!
//! An entry is claimed only when every one of these holds. Each is exact
//! evidence, not a pattern that merely looks familiar:
//!
//! 1. **ExoMonad's key shape.** The key is exactly
//!    `<absolute generated config path>:<event label>:0:0`, where the event
//!    label is one of the three [`CODEX_HOOKS`] labels, the handler indexes are
//!    the `0:0` ExoMonad always writes, and the path is exactly
//!    `<agent dir>/.codex/config.toml`. A `.codex/config.toml` suffix on its own
//!    proves nothing; the suffix only narrows what is even considered.
//! 2. **The config is gone.** Nothing exists at that path — file, directory, or
//!    symlink. A config that is still there is *live* state whose trust belongs
//!    to the live removal path, which can still recompute its hash from the
//!    config itself.
//! 3. **A recomputed digest match.** The recorded `trusted_hash` equals the
//!    digest [`canonical_hook_trust_hashes`] produces today for one of the
//!    explicitly attested ExoMonad binaries, for the event the key names. It is
//!    a SHA-256 preimage match against ExoMonad's own canonical serialization
//!    of `<binary> hook <event> --runtime codex`, so it can only have been
//!    written by ExoMonad rendering exactly that binary.
//!
//! Anything that fails a gate is preserved and reported with the reason it was
//! gated. Keys that are not in ExoMonad's shape at all are counted, not
//! inspected, so unrelated projects and hand-written Codex state are never even
//! candidates.
//!
//! # Deliberately not here
//!
//! Nothing in this module runs from `init`, `spawn`, or any other ordinary
//! lifecycle path. Reclaiming historical residue is an operator decision, so it
//! is only reachable through the explicit `exomonad codex-prune-trust` command,
//! which always prints its plan before anything is written and only writes when
//! the operator passes `--apply`.

use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use crate::codex_config::{
    canonical_hook_trust_hashes, hooks_state_table_for_removal, parse_user_config,
    recorded_hook_trust, uninstall_captured_codex_hook_trust, HookTrustRemoval, OwnedHookTrustKey,
    RecordedHookTrust, CODEX_HOOKS,
};

/// The digest algorithm Codex and ExoMonad record hook trust with.
const TRUSTED_HASH_PREFIX: &str = "sha256:";
/// A hex SHA-256 digest is 64 characters; anything else is not a record ExoMonad wrote.
const TRUSTED_HASH_HEX_LEN: usize = 64;
/// The handler indexes ExoMonad writes: first matcher group, first hook in it.
const EXOMONAD_HANDLER_INDEXES: &str = ":0:0";
/// The only generated config location ExoMonad ever installs trust for.
const GENERATED_CONFIG_DIR: &str = ".codex";
const GENERATED_CONFIG_FILE: &str = "config.toml";

/// A `[hooks.state]` key whose ownership ExoMonad re-proved, with the evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProvenHistoricalTrust {
    /// The exact `[hooks.state]` key.
    pub key: String,
    /// The generated config path the key names. Provably absent from disk.
    pub config_path: PathBuf,
    /// The event label the key names, and the attested binary that reproduces
    /// the recorded digest for it.
    pub event_label: String,
    pub exomonad_binary: PathBuf,
    /// The `trusted_hash` ExoMonad recomputed and matched.
    pub trusted_hash: String,
}

impl ProvenHistoricalTrust {
    fn owned_key(&self) -> OwnedHookTrustKey {
        OwnedHookTrustKey {
            key: self.key.clone(),
            trusted_hash: self.trusted_hash.clone(),
        }
    }
}

impl std::fmt::Display for ProvenHistoricalTrust {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "{}: config {} is gone and its trusted_hash is the {} digest ExoMonad generates \
             for {}",
            self.key,
            self.config_path.display(),
            self.event_label,
            self.exomonad_binary.display()
        )
    }
}

/// Why ExoMonad refused to claim a `[hooks.state]` key that has its own shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TrustGateReason {
    /// The entry is not shaped like the `trusted_hash` record ExoMonad writes.
    UnreadableRecord { detail: String },
    /// The record carries fields beyond `trusted_hash`, so it is not a record
    /// ExoMonad wrote and something else may own the extra state.
    ExtraRecordFields { fields: Vec<String> },
    /// The recorded digest is not a lowercase `sha256:` hex digest, so it cannot
    /// be compared against a recomputed ExoMonad hash.
    ForeignDigest { recorded: String },
    /// Something still exists at the generated config path, so this is live
    /// trust whose owner can still be recomputed from the config itself.
    ConfigStillPresent { config_path: PathBuf },
    /// No attested ExoMonad binary reproduces the recorded digest, so ExoMonad
    /// cannot prove which installation wrote it.
    NoAttestedBinary { recorded: String },
}

impl std::fmt::Display for TrustGateReason {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TrustGateReason::UnreadableRecord { detail } => write!(
                formatter,
                "{detail}, so it is not a record ExoMonad can compare against a recomputed \
                 digest. ExoMonad left it untouched."
            ),
            TrustGateReason::ExtraRecordFields { fields } => write!(
                formatter,
                "the record also carries {} that ExoMonad never writes, so it is not purely \
                 ExoMonad residue. ExoMonad left it untouched.",
                fields.join(", ")
            ),
            TrustGateReason::ForeignDigest { recorded } => write!(
                formatter,
                "the recorded digest {recorded} is not a lowercase {TRUSTED_HASH_PREFIX} hex \
                 digest, so no ExoMonad-generated hook can have produced it. ExoMonad left it \
                 untouched."
            ),
            TrustGateReason::ConfigStillPresent { config_path } => write!(
                formatter,
                "the generated config {} still exists, so this is live trust, not residue. \
                 Remove it with the live removal path, which recomputes the hash from that \
                 config, or delete the config first if the agent is really gone.",
                config_path.display()
            ),
            TrustGateReason::NoAttestedBinary { recorded } => write!(
                formatter,
                "no attested ExoMonad binary generates the recorded digest {recorded}, so \
                 ExoMonad cannot prove which installation wrote this entry. Pass \
                 --expect-binary with the exomonad path that generated it, or delete this entry \
                 by hand."
            ),
        }
    }
}

/// A `[hooks.state]` key ExoMonad deliberately refused to claim.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatedHistoricalTrust {
    /// The exact `[hooks.state]` key that was preserved.
    pub key: String,
    pub reason: TrustGateReason,
}

impl std::fmt::Display for GatedHistoricalTrust {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: {}", self.key, self.reason)
    }
}

/// What a read-only scan of a Codex user config found, and what — if the
/// operator confirms — would be removed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexTrustPrunePlan {
    /// The Codex user config that was scanned.
    pub user_config_path: PathBuf,
    /// The ExoMonad binaries whose recomputed digests prove ownership.
    pub attested_exomonad_binaries: Vec<PathBuf>,
    /// How many `[hooks.state]` keys the scan inspected.
    pub scanned: usize,
    /// Keys ExoMonad re-proved as its own residue.
    pub proven: Vec<ProvenHistoricalTrust>,
    /// Keys in ExoMonad's shape that were preserved, each with its reason.
    pub gated: Vec<GatedHistoricalTrust>,
    /// Keys that are not in ExoMonad's key shape, so they were never inspected.
    pub foreign: usize,
}

impl CodexTrustPrunePlan {
    /// True when the plan would change nothing.
    pub fn is_empty(&self) -> bool {
        self.proven.is_empty()
    }

    /// The plan's proven entries as the exact key/hash pairs the removal path
    /// re-verifies under the config lock.
    pub fn owned_keys(&self) -> Vec<OwnedHookTrustKey> {
        self.proven
            .iter()
            .map(ProvenHistoricalTrust::owned_key)
            .collect()
    }
}

/// Scans a Codex user config and reports the historical ExoMonad hook trust it
/// can re-prove, plus everything it refused to claim and why.
///
/// Read-only: it never writes, never locks, and never creates the config. The
/// atomic writer the install and removal paths use means a reader always sees
/// one complete config, so the plan cannot observe a half-written file.
///
/// Fails closed on a user config that is not parseable TOML, or whose
/// `[hooks.state]` is not a table: ExoMonad will not plan a mutation of
/// configuration it cannot interpret. A user config that does not exist is not
/// an error — there is no trust to prune.
pub fn plan_codex_trust_prune(
    user_config_path: &Path,
    attested_exomonad_binaries: &[PathBuf],
) -> std::io::Result<CodexTrustPrunePlan> {
    let attested = AttestedHookHashes::for_binaries(attested_exomonad_binaries)?;
    let existing = std::fs::read_to_string(user_config_path).unwrap_or_default();
    let mut root = parse_user_config(&existing)?;

    let mut plan = CodexTrustPrunePlan {
        user_config_path: user_config_path.to_path_buf(),
        attested_exomonad_binaries: attested.binaries.clone(),
        scanned: 0,
        proven: Vec::new(),
        gated: Vec::new(),
        foreign: 0,
    };
    if let Some(state) = hooks_state_table_for_removal(user_config_path, &mut root)? {
        for (key, entry) in state.iter() {
            plan.scanned += 1;
            match classify_state_entry(key, entry, &attested) {
                EntryVerdict::Foreign => plan.foreign += 1,
                EntryVerdict::Gated(reason) => plan.gated.push(GatedHistoricalTrust {
                    key: key.to_string(),
                    reason,
                }),
                EntryVerdict::Proven {
                    config_path,
                    event_label,
                    exomonad_binary,
                    trusted_hash,
                } => plan.proven.push(ProvenHistoricalTrust {
                    key: key.to_string(),
                    config_path,
                    event_label,
                    exomonad_binary,
                    trusted_hash,
                }),
            }
        }
    }
    plan.proven.sort_by(|left, right| left.key.cmp(&right.key));
    plan.gated.sort_by(|left, right| left.key.cmp(&right.key));
    Ok(plan)
}

/// Removes exactly the entries [`plan_codex_trust_prune`] proved, through the
/// same `.exomonad-config.lock` sidecar flock and atomic writer installation
/// and live removal use.
///
/// Removal re-checks each recorded `trusted_hash` against the proven value
/// under that lock, so a key edited between the plan and the apply is preserved
/// and reported instead of deleted. An empty plan is a successful no-op that
/// leaves the config byte-for-byte untouched, which is what makes repeated
/// maintenance idempotent.
pub fn apply_codex_trust_prune(plan: &CodexTrustPrunePlan) -> std::io::Result<HookTrustRemoval> {
    uninstall_captured_codex_hook_trust(&plan.user_config_path, &plan.owned_keys())
}

/// A `[hooks.state]` key in exactly the shape ExoMonad installs.
struct ExomonadStateKey {
    config_path: PathBuf,
    event_label: &'static str,
}

/// The digest ExoMonad recomputes for each event, per attested binary.
struct AttestedHookHashes {
    binaries: Vec<PathBuf>,
    by_event: BTreeMap<String, BTreeMap<String, PathBuf>>,
}

impl AttestedHookHashes {
    /// Recomputes the hooks ExoMonad generates for every attested binary.
    ///
    /// Fails closed on an empty set: a scan that can prove nothing would report
    /// every key as unproven for want of evidence rather than because the
    /// evidence is missing, which is the one answer an operator must be able to
    /// trust.
    fn for_binaries(binaries: &[PathBuf]) -> std::io::Result<Self> {
        if binaries.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "cannot plan Codex trust pruning without at least one attested ExoMonad binary: \
                 ExoMonad proves historical trust by recomputing the digest it generated around \
                 that binary, so an empty set can never prove anything.",
            ));
        }
        let mut attested = AttestedHookHashes {
            binaries: Vec::new(),
            by_event: BTreeMap::new(),
        };
        for binary in binaries {
            attested.add(binary)?;
        }
        Ok(attested)
    }

    fn add(&mut self, binary: &Path) -> std::io::Result<()> {
        if self.binaries.iter().any(|seen| seen == binary) {
            return Ok(());
        }
        for hash in canonical_hook_trust_hashes(binary)? {
            self.by_event
                .entry(hash.event_label)
                .or_default()
                .entry(hash.trusted_hash)
                .or_insert_with(|| binary.to_path_buf());
        }
        self.binaries.push(binary.to_path_buf());
        Ok(())
    }

    /// The attested binary that generates `recorded` for `event_label`, if any.
    fn binary_for(&self, event_label: &str, recorded: &str) -> Option<&Path> {
        self.by_event
            .get(event_label)
            .and_then(|hashes| hashes.get(recorded))
            .map(PathBuf::as_path)
    }
}

/// What one `[hooks.state]` key turned out to be.
enum EntryVerdict {
    /// Not in ExoMonad's key shape, so not a candidate at all.
    Foreign,
    /// ExoMonad's shape, but not provable, so preserved with a reason.
    Gated(TrustGateReason),
    /// ExoMonad's own residue, re-proved against an attested binary.
    Proven {
        config_path: PathBuf,
        event_label: String,
        exomonad_binary: PathBuf,
        trusted_hash: String,
    },
}

/// Classifies one `[hooks.state]` key.
///
/// The gates are checked in evidence order: the structural facts about the
/// record first, then whether the config still exists, and the digest match
/// last because it is the only check that can prove ownership at all.
fn classify_state_entry(
    key: &str,
    entry: &toml::Value,
    attested: &AttestedHookHashes,
) -> EntryVerdict {
    let Some(state_key) = parse_exomonad_state_key(key) else {
        return EntryVerdict::Foreign;
    };
    let recorded = match recorded_hook_trust(entry) {
        RecordedHookTrust::Hash(hash) => hash,
        RecordedHookTrust::Unreadable(detail) => {
            return EntryVerdict::Gated(TrustGateReason::UnreadableRecord { detail })
        }
    };
    if let Some(fields) = record_fields_beyond_trusted_hash(entry) {
        return EntryVerdict::Gated(TrustGateReason::ExtraRecordFields { fields });
    }
    if !is_exomonad_trusted_hash(&recorded) {
        return EntryVerdict::Gated(TrustGateReason::ForeignDigest { recorded });
    }
    if path_exists(&state_key.config_path) {
        return EntryVerdict::Gated(TrustGateReason::ConfigStillPresent {
            config_path: state_key.config_path,
        });
    }
    match attested.binary_for(state_key.event_label, &recorded) {
        Some(binary) => EntryVerdict::Proven {
            config_path: state_key.config_path,
            event_label: state_key.event_label.to_string(),
            exomonad_binary: binary.to_path_buf(),
            trusted_hash: recorded,
        },
        None => EntryVerdict::Gated(TrustGateReason::NoAttestedBinary { recorded }),
    }
}

/// Splits a `[hooks.state]` key into the generated config it names, when the
/// key is exactly the key ExoMonad writes and nothing else.
///
/// Rejects, as foreign rather than as a candidate:
///
/// - a key without one of ExoMonad's three event labels,
/// - a key whose handler indexes are not the `0:0` ExoMonad writes, so a
///   hand-written config with a second matcher group is never a candidate,
/// - a config path that is not absolute or is not `<agent dir>/.codex/config.toml`.
fn parse_exomonad_state_key(key: &str) -> Option<ExomonadStateKey> {
    let event = CODEX_HOOKS
        .iter()
        .find(|event| key.ends_with(&state_key_suffix(event.event_label)))?;
    let config_path = PathBuf::from(key.strip_suffix(&state_key_suffix(event.event_label))?);
    is_exomonad_generated_config_path(&config_path).then_some(ExomonadStateKey {
        config_path,
        event_label: event.event_label,
    })
}

fn state_key_suffix(event_label: &str) -> String {
    format!(":{event_label}{EXOMONAD_HANDLER_INDEXES}")
}

fn is_exomonad_generated_config_path(config_path: &Path) -> bool {
    config_path.is_absolute()
        && config_path.file_name() == Some(OsStr::new(GENERATED_CONFIG_FILE))
        && config_path.parent().and_then(Path::file_name) == Some(OsStr::new(GENERATED_CONFIG_DIR))
}

/// Whether anything is still at `path`.
///
/// `symlink_metadata` rather than `Path::exists` so a dangling symlink counts as
/// present: something owns the name, and ExoMonad does not get to prune trust
/// for a path whose name is still taken.
fn path_exists(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok()
}

fn is_exomonad_trusted_hash(recorded: &str) -> bool {
    recorded
        .strip_prefix(TRUSTED_HASH_PREFIX)
        .is_some_and(|hex| {
            hex.len() == TRUSTED_HASH_HEX_LEN
                && hex
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        })
}

/// The fields of a hook-trust record beyond the single `trusted_hash` ExoMonad
/// writes, sorted so the report is stable.
fn record_fields_beyond_trusted_hash(entry: &toml::Value) -> Option<Vec<String>> {
    let table = entry.as_table()?;
    let mut extra: Vec<String> = table
        .keys()
        .filter(|field| field.as_str() != "trusted_hash")
        .cloned()
        .collect();
    extra.sort();
    (!extra.is_empty()).then_some(extra)
}

#[cfg(test)]
#[path = "codex_trust_maintenance/tests.rs"]
mod tests;
