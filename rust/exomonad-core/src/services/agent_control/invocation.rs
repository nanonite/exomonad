//! Generation-scoped process-attempt metadata for issue-owned agents.
//!
//! An invocation belongs to the existing agent identity and worktree. It is
//! replaced when a new process attempt starts, but it never owns or removes
//! the identity, worktree, branch, PR, or inbox. The mutation lock plus
//! atomic rename keep a stale process exit from overwriting a newer attempt.

use super::{read_controller_target_for_routing, AgentType, RoutingInfo};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio::sync::Mutex;
use tracing::{debug, info, warn};
use uuid::Uuid;

pub const INVOCATION_FILENAME: &str = "invocation.json";

/// Harness-written usage source for the invocation that owns this agent dir.
///
/// A one-shot Claude invocation writes its `claude -p --output-format json`
/// envelope here. The finish path consumes the file, so a later invocation in
/// the same directory cannot inherit the earlier invocation's numbers.
pub const INVOCATION_USAGE_FILENAME: &str = "usage.json";

/// The read is local and bounded; a stalled or oversized source is no usage.
const MAX_INVOCATION_USAGE_BYTES: u64 = 1024 * 1024;
const INVOCATION_USAGE_READ_TIMEOUT: Duration = Duration::from_millis(500);

/// Compile-time identity of the runtime that wrote this invocation record.
///
/// The workspace build script stamps `EXOMONAD_BUILD_GIT_COMMIT` for the
/// `exomonad` package. `exomonad-core` does not receive that package-private
/// rustc env, so a missing stamp is the explicit string `unknown`; it is never
/// an invented crate-version label. The `exomonad` server injects the real
/// commit with [`AgentControlService::with_build_identity`] so every durable
/// record names the build that wrote it.
///
/// [`AgentControlService::with_build_identity`]: super::AgentControlService::with_build_identity
pub const BUILD_IDENTITY: &str = match option_env!("EXOMONAD_BUILD_GIT_COMMIT") {
    Some(commit) => commit,
    None => "unknown",
};

/// The literal persisted beside a launch command: was it the exact command or
/// a reconstruction from the resolver?
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LaunchCommandSource {
    /// Captured from the command that actually launched the process.
    Actual,
    /// Rebuilt from the resolved route when no actual command was available.
    Reconstructed,
}

/// Source of each field of the resolved route.
///
/// `harness`, `model`, and `effort` each cite their own `RouteValueSource`
/// (`cli`, `local_config`, `project_config`, `environment`, `policy`,
/// `default`, ...). One field's provenance never stands in for another's.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct RouteConfigSources {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub harness: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
}

/// Names whose values are credentials and must never reach a persisted record.
const SENSITIVE_NAME_MARKERS: &[&str] = &[
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
];

/// Flags whose following argv entry is a credential value.
const SENSITIVE_VALUE_FLAGS: &[&str] = &[
    "--api-key",
    "--token",
    "--access-token",
    "--auth-token",
    "--password",
    "--secret",
    "--credential",
    "--key",
];

fn looks_like_credential_name(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    SENSITIVE_NAME_MARKERS
        .iter()
        .any(|marker| upper.contains(marker))
}

/// Render a launch command with every environment value and credential flag
/// value redacted.
///
/// Environment values may carry API keys, tokens, or forge credentials; argv
/// flags may carry the same secret directly. Only the keys, flag names, and
/// non-secret arguments remain in the durable record.
pub(crate) fn redacted_launch_command(
    full_command: &str,
    env_vars: &std::collections::HashMap<String, String>,
) -> String {
    let mut redacted = full_command.to_string();
    for value in env_vars.values().filter(|value| !value.is_empty()) {
        let escaped = shell_escape::escape(value.as_str().into()).to_string();
        if escaped != "''" {
            redacted = redacted.replace(&escaped, "<redacted>");
        }
    }
    let mut redact_next = false;
    let tokens = redacted
        .split(' ')
        .map(|token| {
            if redact_next {
                redact_next = false;
                return "<redacted>".to_string();
            }
            if let Some((name, _)) = token.split_once('=') {
                if looks_like_credential_name(name) {
                    return format!("{name}=<redacted>");
                }
            }
            if SENSITIVE_VALUE_FLAGS.iter().any(|flag| token == *flag) {
                redact_next = true;
            }
            token.to_string()
        })
        .collect::<Vec<_>>();
    tokens.join(" ")
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InvocationTrigger {
    Spawn,
    ResumePr,
    ResumeBlocked,
    ControllerRestart,
    Review,
}

impl InvocationTrigger {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Spawn => "spawn",
            Self::ResumePr => "resume_pr",
            Self::ResumeBlocked => "resume_blocked",
            Self::ControllerRestart => "controller_restart",
            Self::Review => "review",
        }
    }
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InvocationMode {
    /// Runs one assignment to completion and then exits.
    OneShot,
    #[default]
    /// Remains resident so its caller can deliver live guidance.
    Interactive,
}

impl InvocationMode {
    pub fn from_role(role: Option<&str>) -> Self {
        match role {
            Some("dev") | Some("dev_leaf") | Some("dev-leaf") | Some("reviewer")
            | Some("worker") => Self::OneShot,
            _ => Self::Interactive,
        }
    }
}

#[cfg(test)]
mod mode_tests {
    use super::InvocationMode;

    #[test]
    fn mode_is_role_scoped() {
        for role in ["dev", "dev_leaf", "dev-leaf", "reviewer", "worker"] {
            assert_eq!(
                InvocationMode::from_role(Some(role)),
                InvocationMode::OneShot
            );
        }
        for role in [None, Some("tl"), Some("companion")] {
            assert_eq!(InvocationMode::from_role(role), InvocationMode::Interactive);
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryAuthorization {
    Controller,
    HumanApproved,
    Reconciler,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecoveryInvocationLineage {
    pub prior_invocation_id: String,
    pub invocation_generation: u64,
    pub recovery_round: u32,
    pub authorization_source: RecoveryAuthorization,
}

#[derive(Debug, Clone)]
pub struct InvocationMetadata {
    pub runtime: AgentType,
    pub trigger: InvocationTrigger,
    pub pr_number: Option<u64>,
    pub head_sha: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub identity: Option<InvocationIdentityContext>,
    pub recovery_lineage: Option<RecoveryInvocationLineage>,
    pub expected_live_invocation_id: Option<String>,
    pub mode: InvocationMode,
    pub role_route: Option<RoleRouteProvenance>,
    pub controller_epoch: Option<String>,
    /// Redacted launch command/argv/flags for this process attempt.
    pub launch_command: Option<String>,
    /// Whether `launch_command` is the actual command or a reconstruction.
    pub launch_command_source: Option<LaunchCommandSource>,
    /// Per-field provenance of the resolved route.
    pub config_sources: Option<RouteConfigSources>,
    /// Build identity injected by the server process that wrote the record.
    pub build: Option<String>,
}

/// Accepted controller route persisted with the invocation that used it.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RoleRouteProvenance {
    pub role: String,
    pub harness: String,
    pub agent_type: AgentType,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub configuration_fingerprint: String,
    pub policy_fingerprint: Option<String>,
    pub manifest_digest: Option<String>,
    pub route_fingerprint: String,
    pub dispatch_intent_id: String,
    pub scope_id: String,
}

#[derive(Debug, Clone, Default)]
pub struct InvocationIdentityContext {
    pub runtime_agent_id: Option<String>,
    pub slice_id: Option<String>,
    pub branch: Option<String>,
    pub worktree: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct InvocationExitContext {
    pub reason: Option<String>,
    pub classification: Option<String>,
    pub stderr_tail: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InvocationStatus {
    Running,
    Exited,
    Failed,
    Killed,
    TimedOut,
}

fn deserialize_optional_unix_seconds<'de, D>(deserializer: D) -> Result<Option<u64>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Timestamp {
        Integer(u64),
        Fractional(f64),
    }

    match Option::<Timestamp>::deserialize(deserializer)? {
        None => Ok(None),
        Some(Timestamp::Integer(seconds)) => Ok(Some(seconds)),
        Some(Timestamp::Fractional(seconds))
            if seconds.is_finite() && seconds >= 0.0 && seconds < u64::MAX as f64 =>
        {
            Ok(Some(seconds.trunc() as u64))
        }
        Some(Timestamp::Fractional(_)) => Err(serde::de::Error::custom(
            "ended_at must be a non-negative Unix timestamp",
        )),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InvocationUsage {
    /// Prompt tokens including cache creation and cache reads, matching the
    /// harness envelope's own input accounting.
    pub input_tokens: u64,
    pub output_tokens: u64,
    /// `input_tokens + output_tokens`, carried explicitly for the controller.
    pub total_tokens: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct InvocationRecord {
    pub invocation_id: String,
    pub runtime: AgentType,
    pub trigger: InvocationTrigger,
    #[serde(default)]
    pub mode: InvocationMode,
    pub routing: RoutingInfo,
    pub started_at: u64,
    #[serde(
        default,
        deserialize_with = "deserialize_optional_unix_seconds",
        skip_serializing_if = "Option::is_none"
    )]
    pub ended_at: Option<u64>,
    pub status: InvocationStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exit_code: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pr_number: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head_sha: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prior_invocation_id: Option<String>,
    #[serde(default)]
    pub recovery_round: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authorization_source: Option<RecoveryAuthorization>,
    #[serde(default)]
    pub generation: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller_epoch: Option<String>,
    #[serde(default)]
    pub runtime_agent_id: Option<String>,
    #[serde(default)]
    pub slice_id: Option<String>,
    #[serde(default)]
    pub branch: Option<String>,
    #[serde(default)]
    pub worktree: Option<String>,
    #[serde(default)]
    pub exit_reason: Option<String>,
    #[serde(default)]
    pub exit_classification: Option<String>,
    #[serde(default)]
    pub stderr_tail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role_route: Option<RoleRouteProvenance>,
    /// Compile-time runtime build that wrote this generation.
    #[serde(default)]
    pub build: String,
    /// Launch command, argv, and flags with environment values redacted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub launch_command: Option<String>,
    /// Whether the launch command was captured or reconstructed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub launch_command_source: Option<LaunchCommandSource>,
    /// Provenance of each resolved route field for this attempt.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub config_sources: Option<RouteConfigSources>,
    /// Measured usage for this invocation, when its harness produced an
    /// authoritative per-invocation source.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<InvocationUsage>,
}

impl InvocationRecord {
    pub fn is_live(&self) -> bool {
        self.ended_at.is_none() && self.status == InvocationStatus::Running
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(clippy::large_enum_variant)]
pub enum InvocationFinishResult {
    Finished(InvocationRecord),
    IgnoredStale,
    Missing,
}

fn mutation_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

#[cfg(test)]
fn invocation_save_failpoints(
) -> &'static std::sync::Mutex<std::collections::HashSet<std::path::PathBuf>> {
    static FAILPOINTS: OnceLock<std::sync::Mutex<std::collections::HashSet<std::path::PathBuf>>> =
        OnceLock::new();
    FAILPOINTS.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}

#[cfg(test)]
pub(crate) fn fail_after_next_invocation_save(agent_dir: &Path) {
    invocation_save_failpoints()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .insert(agent_dir.to_path_buf());
}

#[cfg(test)]
fn take_invocation_save_failpoint(agent_dir: &Path) -> bool {
    invocation_save_failpoints()
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .remove(agent_dir)
}

fn invocation_path(agent_dir: &Path) -> std::path::PathBuf {
    agent_dir.join(INVOCATION_FILENAME)
}

fn unix_timestamp() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

async fn read_locked(agent_dir: &Path) -> Result<Option<InvocationRecord>> {
    let path = invocation_path(agent_dir);
    match tokio::fs::read_to_string(&path).await {
        Ok(contents) => Ok(Some(serde_json::from_str(&contents).with_context(
            || format!("failed to parse invocation record {}", path.display()),
        )?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

async fn next_generation(agent_dir: &Path) -> Result<u64> {
    Ok(read_locked(agent_dir)
        .await?
        .map(|record| record.generation)
        .unwrap_or(0)
        .saturating_add(1))
}

/// Parse the authoritative `claude -p --output-format json` envelope.
///
/// The envelope shape and the cache-inclusive input sum are the ones the
/// repository's tool-free RLM backend already reads (`_claude_reply` /
/// `_usage_tokens`). A missing, partial, malformed, or zero report is not a
/// measurement and returns an explicit reason instead of a number.
pub fn parse_claude_usage_envelope(raw: &str) -> Result<InvocationUsage, String> {
    let value: serde_json::Value = serde_json::from_str(raw)
        .map_err(|error| format!("usage envelope is not exactly one JSON object: {error}"))?;
    let envelope = value
        .as_object()
        .ok_or_else(|| "usage envelope is not a JSON object".to_string())?;
    let usage = envelope
        .get("usage")
        .and_then(serde_json::Value::as_object)
        .ok_or_else(|| "usage envelope has no usage object".to_string())?;

    let cache_creation_tokens = optional_usage_counter(usage, "cache_creation_input_tokens")?;
    let cache_read_tokens = optional_usage_counter(usage, "cache_read_input_tokens")?;
    let input_tokens = required_usage_counter(usage, "input_tokens")?
        .checked_add(cache_creation_tokens)
        .and_then(|value| value.checked_add(cache_read_tokens))
        .ok_or_else(|| "input token counters overflow".to_string())?;
    let output_tokens = required_usage_counter(usage, "output_tokens")?;
    let total_tokens = input_tokens
        .checked_add(output_tokens)
        .ok_or_else(|| "token counters overflow".to_string())?;
    if total_tokens == 0 {
        return Err("usage envelope reports zero tokens".to_string());
    }
    Ok(InvocationUsage {
        input_tokens,
        output_tokens,
        total_tokens,
    })
}

fn required_usage_counter(
    usage: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<u64, String> {
    usage
        .get(key)
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| format!("usage {key} must be a non-negative integer"))
}

fn optional_usage_counter(
    usage: &serde_json::Map<String, serde_json::Value>,
    key: &str,
) -> Result<u64, String> {
    match usage.get(key) {
        None => Ok(0),
        Some(value) => value
            .as_u64()
            .ok_or_else(|| format!("usage {key} must be a non-negative integer")),
    }
}

/// Read and consume the invocation-owned usage source.
///
/// The read is local, bounded, and time-limited so finish never waits on a
/// slow or hostile file. Absent is normal (Codex, OpenCode, Shoal, and
/// interactive Claude have no source); a file that exists but cannot be used
/// logs why and contributes no usage.
async fn read_invocation_usage(agent_dir: &Path) -> Option<InvocationUsage> {
    let path = agent_dir.join(INVOCATION_USAGE_FILENAME);
    match tokio::fs::metadata(&path).await {
        Ok(metadata) if metadata.is_file() => {}
        Ok(_) => {
            debug!(path = %path.display(), "Ignoring non-file invocation usage source");
            return None;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            debug!(path = %path.display(), "No per-invocation usage source");
            return None;
        }
        Err(error) => {
            warn!(path = %path.display(), %error, "Ignoring unreadable invocation usage source");
            return None;
        }
    }

    let outcome = tokio::time::timeout(
        INVOCATION_USAGE_READ_TIMEOUT,
        read_bounded_usage_source(&path),
    )
    .await;
    let _ = tokio::fs::remove_file(&path).await;

    match outcome {
        Err(_) => {
            warn!(path = %path.display(), "Invocation usage source read timed out");
            None
        }
        Ok(Err(reason)) => {
            warn!(path = %path.display(), reason = %reason, "Ignoring invocation usage source");
            None
        }
        Ok(Ok(raw)) => match parse_claude_usage_envelope(&raw) {
            Ok(usage) => Some(usage),
            Err(reason) => {
                warn!(path = %path.display(), reason = %reason, "Ignoring invocation usage source");
                None
            }
        },
    }
}

async fn read_bounded_usage_source(path: &Path) -> Result<String, String> {
    let metadata = tokio::fs::metadata(path)
        .await
        .map_err(|error| format!("cannot stat usage source: {error}"))?;
    if metadata.len() > MAX_INVOCATION_USAGE_BYTES {
        return Err(format!(
            "usage source is {} bytes, over the {MAX_INVOCATION_USAGE_BYTES}-byte bound",
            metadata.len()
        ));
    }
    let file = tokio::fs::File::open(path)
        .await
        .map_err(|error| format!("cannot open usage source: {error}"))?;
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_INVOCATION_USAGE_BYTES + 1)
        .read_to_end(&mut bytes)
        .await
        .map_err(|error| format!("cannot read usage source: {error}"))?;
    if bytes.len() as u64 > MAX_INVOCATION_USAGE_BYTES {
        return Err(format!(
            "usage source grew past the {MAX_INVOCATION_USAGE_BYTES}-byte bound"
        ));
    }
    String::from_utf8(bytes).map_err(|error| format!("usage source is not UTF-8: {error}"))
}

async fn write_atomic(agent_dir: &Path, record: &InvocationRecord) -> Result<()> {
    let path = invocation_path(agent_dir);
    let temporary = agent_dir.join(format!(".invocation-{}.tmp", Uuid::new_v4()));
    let json = serde_json::to_vec_pretty(record)?;
    if let Err(error) = tokio::fs::write(&temporary, json).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to write {}", temporary.display()));
    }
    if let Err(error) = tokio::fs::rename(&temporary, &path).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to replace {}", path.display()));
    }
    Ok(())
}

async fn clear_terminal_invocation_markers(
    agent_dir: &Path,
    previous: Option<&InvocationRecord>,
) -> Result<()> {
    // Exit markers have no embedded generation ID. Their owner is established
    // by the current terminal checkpoint, so retire them before its successor
    // is saved rather than during a later live reuse.
    let Some(previous) = previous.filter(|record| {
        record.ended_at.is_some()
            && matches!(
                record.status,
                InvocationStatus::Exited
                    | InvocationStatus::Failed
                    | InvocationStatus::Killed
                    | InvocationStatus::TimedOut
            )
    }) else {
        return Ok(());
    };

    for marker in ["exited_at", "exit_code"] {
        match tokio::fs::remove_file(agent_dir.join(marker)).await {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "failed to retire {marker} from terminal invocation {}",
                        previous.invocation_id
                    )
                })
            }
        }
    }
    Ok(())
}

async fn persist_routing_ownership(
    agent_dir: &Path,
    routing: &RoutingInfo,
    invocation_id: &str,
    generation: u64,
) -> Result<()> {
    let agent_id = agent_dir
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .context("agent directory has no usable identity for routing ownership")?;
    routing
        .with_ownership(agent_id, invocation_id, generation)
        .write_to_dir(agent_dir)
        .await
}

/// Whether startup reconciliation requires the recorded delivery target to
/// still exist.
///
/// A live reuse may race an exit monitor that has already observed its target
/// disappear and will record the terminal outcome. Startup recovery has no such
/// monitor, so a vanished target cannot be revalidated and must not be
/// reattached to an empty monitor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DeliveryTargetRequirement {
    MayBeGone,
    MustExist,
}

pub(crate) async fn reconcile_live_invocation_startup(
    agent_dir: &Path,
    expected: &InvocationRecord,
    requested_routing: &RoutingInfo,
    tmux: Option<&crate::services::tmux_ipc::TmuxIpc>,
    delivery_target: DeliveryTargetRequirement,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let current = require_expected_live_invocation(agent_dir, expected, requested_routing).await?;
    let persisted = RoutingInfo::read_from_dir(agent_dir).await?;
    if !persisted.same_delivery_target(&current.routing) {
        return Err(anyhow::anyhow!(
            "persisted routing target conflicts with the live invocation"
        ));
    }
    let route_owned = routing_has_owner_fields(&persisted);
    if route_owned {
        verify_current_routing_owner(agent_dir, &persisted, &current)?;
    }
    if current.routing.window_id.is_none() && current.routing.pane_id.is_none() {
        anyhow::bail!("live invocation has no tmux target for startup recovery");
    }

    let agent_id = agent_id_from_dir(agent_dir)?;
    if let Some(tmux) = tmux {
        let target_alive = crate::services::tmux_ipc::routing_target_alive(&current.routing, tmux)
            .await
            .context("failed to validate the live invocation delivery target")?;
        if !target_alive {
            if delivery_target == DeliveryTargetRequirement::MustExist {
                anyhow::bail!(
                    "live invocation delivery target no longer exists; refusing to reattach its monitor"
                );
            }
        } else {
            match tmux.routing_owner_state(&current.routing).await? {
                crate::services::tmux_ipc::RoutingOwnerState::Unowned => {
                    tmux.set_routing_owner(
                        &current.routing,
                        agent_id,
                        &current.invocation_id,
                        current.generation,
                    )
                    .await?;
                }
                crate::services::tmux_ipc::RoutingOwnerState::Owned(owner)
                    if owner.agent_id == agent_id
                        && owner.invocation_id == current.invocation_id
                        && owner.generation == current.generation => {}
                crate::services::tmux_ipc::RoutingOwnerState::Owned(owner) => {
                    if !routing_owner_matches_invocation_or_parent(
                        agent_dir,
                        &current,
                        &owner,
                        &current.routing,
                    )
                    .await?
                    {
                        anyhow::bail!("tmux target ownership conflicts with the live invocation or its declared parent");
                    }
                }
                crate::services::tmux_ipc::RoutingOwnerState::Partial => {
                    anyhow::bail!("tmux target has partial ownership evidence");
                }
            }
        }
    }
    if !route_owned {
        persist_routing_ownership(
            agent_dir,
            &current.routing,
            &current.invocation_id,
            current.generation,
        )
        .await?;
    }
    crate::services::lifecycle::ensure_invocation_started(agent_dir, &current).map(|()| current)
}

fn agent_id_from_dir(agent_dir: &Path) -> Result<&str> {
    agent_dir
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .context("agent directory has no usable identity for routing ownership")
}

pub(crate) async fn routing_owner_matches_invocation_or_parent(
    agent_dir: &Path,
    invocation: &InvocationRecord,
    owner: &crate::services::tmux_ipc::TmuxOwnership,
    delivery_target: &RoutingInfo,
) -> Result<bool> {
    let child_id = agent_id_from_dir(agent_dir)?;
    if owner.agent_id == child_id {
        return Ok(owner.invocation_id == invocation.invocation_id
            && owner.generation == invocation.generation
            && invocation.routing.same_delivery_target(delivery_target));
    }
    let child_identity_path = agent_dir.join("identity.json");
    let child_identity: crate::services::agent_resolver::AgentIdentityRecord =
        serde_json::from_slice(&tokio::fs::read(&child_identity_path).await?)?;
    if child_identity.agent_name.as_str() != child_id
        || child_identity.birth_branch.as_str() != invocation.branch.as_deref().unwrap_or_default()
    {
        return Ok(false);
    }
    if shared_sibling_ordered_owner_matches(agent_dir, &child_identity, owner, delivery_target)
        .await?
    {
        return Ok(true);
    }
    let Ok(parent_name) = crate::domain::AgentName::try_from_str(&owner.agent_id) else {
        return Ok(false);
    };
    let parent_dir = agent_dir
        .parent()
        .context("agent directory has no registry parent")?
        .join(parent_name.as_str());
    let parent_identity_path = parent_dir.join("identity.json");
    let parent_identity_bytes = match tokio::fs::read(&parent_identity_path).await {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    let parent_identity: crate::services::agent_resolver::AgentIdentityRecord =
        serde_json::from_slice(&parent_identity_bytes)?;
    if parent_identity.agent_name != parent_name
        || !parent_identity.ledger_owned
        || parent_identity.birth_branch != child_identity.parent_branch
    {
        return Ok(false);
    }
    let Some(parent_invocation) = read_locked(&parent_dir).await? else {
        return Ok(false);
    };
    if !parent_invocation.is_live()
        || parent_invocation.invocation_id != owner.invocation_id
        || parent_invocation.generation != owner.generation
    {
        return Ok(false);
    }
    if parent_invocation
        .routing
        .same_delivery_target(delivery_target)
    {
        return Ok(true);
    }
    parent_window_route_matches_exact_controller_pane(
        agent_dir,
        &parent_invocation.routing,
        delivery_target,
    )
    .await
}

/// A shared TL pane can carry only one window-level ownership tag, but several
/// ledger-owned sibling controllers may legitimately share that exact pane.
/// Accept a sibling tag only when both identities are accepted declarations
/// with the same parent branch, the sibling invocation is live, and the
/// delivery target is exactly the recorded controller target.
async fn shared_sibling_ordered_owner_matches(
    agent_dir: &Path,
    child_identity: &crate::services::agent_resolver::AgentIdentityRecord,
    owner: &crate::services::tmux_ipc::TmuxOwnership,
    delivery_target: &RoutingInfo,
) -> Result<bool> {
    let Some(registry) = agent_dir.parent() else {
        return Ok(false);
    };
    if owner.agent_id == "root" {
        // The root controller is the shared parent, never a sibling.
        return Ok(false);
    }
    let owner_dir = registry.join(&owner.agent_id);
    let owner_identity_path = owner_dir.join("identity.json");
    let owner_identity = match tokio::fs::read(&owner_identity_path).await {
        Ok(bytes) => {
            serde_json::from_slice::<crate::services::agent_resolver::AgentIdentityRecord>(&bytes)?
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    if owner_identity.agent_name.as_str() != owner.agent_id
        || !owner_identity.ledger_owned
        || owner_identity.slice_id.is_none()
        || owner_identity.parent_branch != child_identity.parent_branch
        || owner_identity.birth_branch.parent().as_ref() != Some(&child_identity.parent_branch)
    {
        return Ok(false);
    }
    let Some(owner_invocation) = read_locked(&owner_dir).await? else {
        return Ok(false);
    };
    if !owner_invocation.is_live()
        || owner_invocation.invocation_id != owner.invocation_id
        || owner_invocation.generation != owner.generation
    {
        return Ok(false);
    }
    let Some(project_dir) = registry.parent().and_then(Path::parent) else {
        return Ok(false);
    };
    let target = read_controller_target_for_routing(project_dir).await?;
    Ok(
        Some(&target.window_id) == delivery_target.window_id.as_ref()
            && Some(&target.pane_id) == delivery_target.pane_id.as_ref(),
    )
}

async fn parent_window_route_matches_exact_controller_pane(
    agent_dir: &Path,
    parent_routing: &RoutingInfo,
    delivery_target: &RoutingInfo,
) -> Result<bool> {
    if parent_routing.pane_id.is_some()
        || delivery_target.pane_id.is_none()
        || parent_routing.window_id != delivery_target.window_id
    {
        return Ok(false);
    }
    let project_dir = agent_dir
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("agent directory is outside the project registry")?;
    let target = read_controller_target_for_routing(project_dir).await?;
    Ok(
        Some(&target.window_id) == delivery_target.window_id.as_ref()
            && Some(&target.pane_id) == delivery_target.pane_id.as_ref(),
    )
}

async fn require_expected_live_invocation(
    agent_dir: &Path,
    expected: &InvocationRecord,
    requested_routing: &RoutingInfo,
) -> Result<InvocationRecord> {
    let current = read_locked(agent_dir)
        .await?
        .context("live invocation checkpoint is unavailable during route recovery")?;
    if &current == expected
        && current.is_live()
        && current.routing.same_delivery_target(requested_routing)
    {
        return Ok(current);
    }
    Err(anyhow::anyhow!(
        "live invocation changed before its routing ownership could be recovered"
    ))
}

fn routing_has_owner_fields(routing: &RoutingInfo) -> bool {
    routing.owner_agent_id.is_some()
        || routing.owner_invocation_id.is_some()
        || routing.owner_generation.is_some()
}

fn verify_current_routing_owner(
    agent_dir: &Path,
    routing: &RoutingInfo,
    invocation: &InvocationRecord,
) -> Result<()> {
    let agent_id = agent_dir
        .file_name()
        .and_then(|name| name.to_str())
        .context("agent directory has no usable identity for routing recovery")?;
    let is_current_owner = routing.has_ownership_proof()
        && routing.owner_agent_id.as_deref() == Some(agent_id)
        && routing.owner_invocation_id.as_deref() == Some(invocation.invocation_id.as_str())
        && routing.owner_generation == Some(invocation.generation);
    if is_current_owner {
        return Ok(());
    }
    Err(anyhow::anyhow!(
        "persisted routing ownership conflicts with the live invocation"
    ))
}

pub const ADOPTION_RECEIPT_FILENAME: &str = "adoption.json";

/// Durable receipt for a response-loss adoption of an already-launched
/// successor. It is generation-scoped and idempotent per invocation and intent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AdoptionReceipt {
    pub invocation_id: String,
    pub intent_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prior_invocation_id: Option<String>,
    pub adopted_at: u64,
}

fn adoption_receipt_path(agent_dir: &Path) -> std::path::PathBuf {
    agent_dir.join(ADOPTION_RECEIPT_FILENAME)
}

/// The adoption receipt describes exactly one generation, so a successor
/// generation retires it as it replaces the record the receipt belongs to.
async fn retire_adoption_receipt(agent_dir: &Path) -> Result<()> {
    match tokio::fs::remove_file(adoption_receipt_path(agent_dir)).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| {
            format!(
                "failed to retire the prior adoption receipt in {}",
                agent_dir.display()
            )
        }),
    }
}

async fn read_adoption_receipt(agent_dir: &Path) -> Result<Option<AdoptionReceipt>> {
    let path = adoption_receipt_path(agent_dir);
    match tokio::fs::read_to_string(&path).await {
        Ok(contents) => Ok(Some(serde_json::from_str(&contents).with_context(
            || format!("failed to parse adoption receipt {}", path.display()),
        )?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("failed to read {}", path.display())),
    }
}

/// Record that a response-loss resume adopted the already-launched successor.
///
/// This is the durable evidence a restart (and the compiled interruption
/// acceptance) can observe before the adoption response is returned. Repeated
/// adoption of the same invocation and intent is idempotent and keeps the first
/// receipt, so a restart cannot move the receipt past the interruption window.
pub async fn record_blocked_resume_adoption(
    agent_dir: &Path,
    invocation_id: &str,
    intent_id: &str,
) -> Result<AdoptionReceipt> {
    let _guard = mutation_lock().lock().await;
    if let Some(existing) = read_adoption_receipt(agent_dir).await? {
        if existing.invocation_id == invocation_id && existing.intent_id == intent_id {
            return Ok(existing);
        }
        return Err(anyhow::anyhow!(
            "adoption receipt belongs to another invocation or dispatch intent"
        ));
    }
    let record = read_locked(agent_dir)
        .await?
        .context("adoption receipt requires the owned invocation record")?;
    if record.invocation_id != invocation_id {
        return Err(anyhow::anyhow!(
            "the owned invocation changed before its adoption receipt was recorded"
        ));
    }
    let receipt = AdoptionReceipt {
        invocation_id: record.invocation_id.clone(),
        intent_id: intent_id.to_string(),
        prior_invocation_id: record.prior_invocation_id.clone(),
        adopted_at: unix_timestamp(),
    };
    let path = adoption_receipt_path(agent_dir);
    let temporary = agent_dir.join(format!(".adoption-{}.tmp", Uuid::new_v4()));
    let json = serde_json::to_vec_pretty(&receipt)?;
    if let Err(error) = tokio::fs::write(&temporary, json).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to write {}", temporary.display()));
    }
    if let Err(error) = tokio::fs::rename(&temporary, &path).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("failed to replace {}", path.display()));
    }
    info!(
        path = %path.display(),
        invocation_id,
        intent_id,
        "Recorded blocked-resume adoption receipt"
    );
    Ok(receipt)
}

pub async fn read_invocation(agent_dir: &Path) -> Result<Option<InvocationRecord>> {
    read_locked(agent_dir).await
}

/// Read legacy metadata without allowing a malformed optional record to make
/// an agent look dead or trigger destructive cleanup.
pub async fn read_invocation_conservatively(agent_dir: &Path) -> Option<InvocationRecord> {
    match read_invocation(agent_dir).await {
        Ok(Some(record)) => Some(record),
        Ok(None) => {
            debug!(path = %invocation_path(agent_dir).display(), "No invocation record; treating agent as legacy-owned");
            None
        }
        Err(error) => {
            warn!(path = %invocation_path(agent_dir).display(), %error, "Ignoring malformed invocation record conservatively");
            None
        }
    }
}

/// Start a new process attempt for the existing agent identity.
pub async fn start_invocation(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
) -> Result<InvocationRecord> {
    start_invocation_with_context(
        agent_dir, runtime, trigger, routing, pr_number, head_sha, None,
    )
    .await
}

async fn start_invocation_with_context(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    identity: Option<InvocationIdentityContext>,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let generation = next_generation(agent_dir).await?;
    let identity = identity.unwrap_or_default();
    let record = InvocationRecord {
        invocation_id: Uuid::new_v4().to_string(),
        runtime,
        trigger,
        mode: InvocationMode::Interactive,
        routing,
        started_at: unix_timestamp(),
        ended_at: None,
        status: InvocationStatus::Running,
        exit_code: None,
        pr_number,
        head_sha,
        model: None,
        effort: None,
        generation,
        controller_epoch: None,
        runtime_agent_id: identity.runtime_agent_id,
        slice_id: identity.slice_id,
        branch: identity.branch,
        worktree: identity.worktree,
        exit_reason: None,
        exit_classification: None,
        stderr_tail: None,
        role_route: None,
        build: BUILD_IDENTITY.to_string(),
        launch_command: None,
        launch_command_source: None,
        config_sources: None,
        prior_invocation_id: None,
        recovery_round: 0,
        authorization_source: None,
        usage: None,
    };
    retire_adoption_receipt(agent_dir).await?;
    write_atomic(agent_dir, &record).await?;
    persist_routing_ownership(
        agent_dir,
        &record.routing,
        &record.invocation_id,
        record.generation,
    )
    .await?;
    crate::services::lifecycle::ensure_invocation_started(agent_dir, &record)?;
    info!(
        path = %invocation_path(agent_dir).display(),
        invocation_id = %record.invocation_id,
        trigger = ?record.trigger,
        "Started agent invocation"
    );
    Ok(record)
}

/// Start a process attempt with the effective model and effort selected for it.
#[allow(clippy::too_many_arguments)]
pub async fn start_invocation_with_provenance(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    model: Option<String>,
    effort: Option<String>,
) -> Result<InvocationRecord> {
    start_invocation_with_provenance_and_context(
        agent_dir, runtime, trigger, routing, pr_number, head_sha, model, effort, None, None,
    )
    .await
}

// The explicit provenance fields mirror the invocation record and keep the
// compatibility wrapper above source-compatible for existing callers.
#[allow(clippy::too_many_arguments)]
pub async fn start_invocation_with_provenance_and_context(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    identity: Option<InvocationIdentityContext>,
    recovery_lineage: Option<RecoveryInvocationLineage>,
) -> Result<InvocationRecord> {
    start_invocation_with_provenance_and_context_and_mode(
        agent_dir,
        runtime,
        trigger,
        routing,
        pr_number,
        head_sha,
        model,
        effort,
        identity,
        recovery_lineage,
        InvocationMode::Interactive,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
pub async fn start_invocation_with_provenance_and_context_and_mode(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    identity: Option<InvocationIdentityContext>,
    recovery_lineage: Option<RecoveryInvocationLineage>,
    mode: InvocationMode,
) -> Result<InvocationRecord> {
    start_invocation_with_provenance_and_context_and_mode_with_id(
        agent_dir,
        runtime,
        trigger,
        routing,
        pr_number,
        head_sha,
        model,
        effort,
        identity,
        recovery_lineage,
        mode,
        Uuid::new_v4().to_string(),
    )
    .await
}

#[allow(clippy::too_many_arguments)]
/// Start an invocation using an ID registered in durable publication lineage.
pub async fn start_invocation_with_provenance_and_context_and_mode_with_id(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    identity: Option<InvocationIdentityContext>,
    recovery_lineage: Option<RecoveryInvocationLineage>,
    mode: InvocationMode,
    invocation_id: String,
) -> Result<InvocationRecord> {
    start_invocation_with_provenance_and_context_and_mode_with_id_guarded(
        agent_dir,
        runtime,
        trigger,
        routing,
        pr_number,
        head_sha,
        model,
        effort,
        identity,
        recovery_lineage,
        mode,
        invocation_id,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
pub async fn start_invocation_with_provenance_and_context_and_mode_with_id_guarded(
    agent_dir: &Path,
    runtime: AgentType,
    trigger: InvocationTrigger,
    routing: RoutingInfo,
    pr_number: Option<u64>,
    head_sha: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    identity: Option<InvocationIdentityContext>,
    recovery_lineage: Option<RecoveryInvocationLineage>,
    mode: InvocationMode,
    invocation_id: String,
    expected_live_invocation_id: Option<&str>,
    role_route: Option<RoleRouteProvenance>,
    controller_epoch: Option<String>,
    launch_command: Option<String>,
    launch_command_source: Option<LaunchCommandSource>,
    config_sources: Option<RouteConfigSources>,
    build: Option<String>,
) -> Result<InvocationRecord> {
    if invocation_id.trim().is_empty() {
        return Err(anyhow::anyhow!("invocation ID must not be empty"));
    }
    if trigger == InvocationTrigger::ControllerRestart
        && (recovery_lineage.is_none() || pr_number.is_some() || head_sha.is_some())
    {
        return Err(anyhow::anyhow!(
            "controller restart requires terminal invocation lineage and cannot carry PR coordinates"
        ));
    }
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir).await?;
    if let Some(expected_id) = expected_live_invocation_id {
        if current
            .as_ref()
            .is_none_or(|record| record.invocation_id != expected_id || !record.is_live())
        {
            return Err(anyhow::anyhow!(
                "the expected live controller invocation changed before provisioning completed"
            ));
        }
        return Err(anyhow::anyhow!(
            "a matching live controller invocation must be reused, not replaced"
        ));
    }
    let generation = current
        .as_ref()
        .map(|record| record.generation.checked_add(1))
        .unwrap_or(Some(1))
        .ok_or_else(|| anyhow::anyhow!("invocation generation is exhausted"))?;
    if let Some(lineage) = recovery_lineage.as_ref() {
        if lineage.invocation_generation != generation {
            return Err(anyhow::anyhow!(
                "recovery invocation generation mismatch: expected {}, allocated {}",
                lineage.invocation_generation,
                generation
            ));
        }
        let Some(previous) = current.as_ref() else {
            return Err(anyhow::anyhow!(
                "recovery lineage references a prior invocation, but the current invocation checkpoint is missing"
            ));
        };
        let previous_is_terminal = matches!(
            previous.status,
            InvocationStatus::Exited
                | InvocationStatus::Failed
                | InvocationStatus::Killed
                | InvocationStatus::TimedOut
        ) && previous.ended_at.is_some();
        if previous.invocation_id != lineage.prior_invocation_id || !previous_is_terminal {
            return Err(anyhow::anyhow!(
                "recovery lineage no longer matches the current terminal invocation"
            ));
        }
    }
    clear_terminal_invocation_markers(agent_dir, current.as_ref()).await?;
    // Revalidate lineage before changing the route. A concurrent service may
    // have registered a replacement after this caller read its prior record.
    // Keep route and invocation ownership updates inside the same mutation lock.
    routing.write_to_dir(agent_dir).await?;
    let identity = identity.unwrap_or_default();
    let record = InvocationRecord {
        invocation_id,
        runtime,
        trigger,
        mode,
        routing,
        started_at: unix_timestamp(),
        ended_at: None,
        status: InvocationStatus::Running,
        exit_code: None,
        pr_number,
        head_sha,
        model,
        effort,
        prior_invocation_id: recovery_lineage
            .as_ref()
            .map(|lineage| lineage.prior_invocation_id.clone()),
        recovery_round: recovery_lineage
            .as_ref()
            .map(|lineage| lineage.recovery_round)
            .unwrap_or(0),
        authorization_source: recovery_lineage
            .as_ref()
            .map(|lineage| lineage.authorization_source),
        generation,
        controller_epoch,
        runtime_agent_id: identity.runtime_agent_id,
        slice_id: identity.slice_id,
        branch: identity.branch,
        worktree: identity.worktree,
        exit_reason: None,
        exit_classification: None,
        stderr_tail: None,
        role_route,
        build: build.unwrap_or_else(|| BUILD_IDENTITY.to_string()),
        launch_command,
        launch_command_source,
        config_sources,
        usage: None,
    };
    retire_adoption_receipt(agent_dir).await?;
    write_atomic(agent_dir, &record).await?;
    #[cfg(test)]
    if take_invocation_save_failpoint(agent_dir) {
        anyhow::bail!("injected interruption after invocation checkpoint save");
    }
    persist_routing_ownership(
        agent_dir,
        &record.routing,
        &record.invocation_id,
        record.generation,
    )
    .await?;
    crate::services::lifecycle::ensure_invocation_started(agent_dir, &record)?;
    info!(
        path = %invocation_path(agent_dir).display(),
        invocation_id = %record.invocation_id,
        trigger = ?record.trigger,
        model = ?record.model,
        effort = ?record.effort,
        "Started agent invocation"
    );
    Ok(record)
}

async fn finish_locked(
    agent_dir: &Path,
    invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
    exit_context: InvocationExitContext,
) -> Result<InvocationFinishResult> {
    let Some(mut record) = read_locked(agent_dir).await? else {
        return Ok(InvocationFinishResult::Missing);
    };
    if record.invocation_id != invocation_id {
        return Ok(InvocationFinishResult::IgnoredStale);
    }
    if record.ended_at.is_some() {
        return Ok(InvocationFinishResult::Finished(record));
    }
    record.ended_at = Some(unix_timestamp());
    record.status = status;
    record.exit_code = exit_code;
    record.exit_reason = exit_context.reason;
    record.exit_classification = exit_context.classification;
    record.stderr_tail = exit_context.stderr_tail;
    record.usage = read_invocation_usage(agent_dir).await;
    write_atomic(agent_dir, &record).await?;
    crate::services::lifecycle::record_invocation_finished(agent_dir, &record);
    info!(
        path = %invocation_path(agent_dir).display(),
        invocation_id,
        status = ?record.status,
        exit_code = ?record.exit_code,
        "Finished agent invocation"
    );
    Ok(InvocationFinishResult::Finished(record))
}

/// Finish only the current invocation generation identified by its ID.
pub async fn finish_invocation(
    agent_dir: &Path,
    invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
) -> Result<InvocationFinishResult> {
    finish_invocation_with_context(
        agent_dir,
        invocation_id,
        status,
        exit_code,
        InvocationExitContext::default(),
    )
    .await
}

async fn finish_invocation_with_context(
    agent_dir: &Path,
    invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
    exit_context: InvocationExitContext,
) -> Result<InvocationFinishResult> {
    let _guard = mutation_lock().lock().await;
    finish_locked(agent_dir, invocation_id, status, exit_code, exit_context).await
}

/// Finish and tombstone a routing target only when the current invocation
/// still owns the exact routing record supplied by the exiting process.
/// Missing records retain legacy cleanup behavior; malformed records are an
/// error and therefore leave routing untouched. The routing snapshot is
/// retained as last-known metadata; `exited_at` and the terminal invocation
/// status are the retirement markers used by liveness checks.
pub async fn finish_invocation_and_tombstone(
    agent_dir: &Path,
    expected_routing: &RoutingInfo,
    status: InvocationStatus,
    exit_code: Option<i32>,
) -> Result<InvocationFinishResult> {
    finish_invocation_and_tombstone_with_context(
        agent_dir,
        expected_routing,
        status,
        exit_code,
        InvocationExitContext::default(),
    )
    .await
}

pub async fn finish_invocation_and_tombstone_with_context(
    agent_dir: &Path,
    expected_routing: &RoutingInfo,
    status: InvocationStatus,
    exit_code: Option<i32>,
    exit_context: InvocationExitContext,
) -> Result<InvocationFinishResult> {
    finish_invocation_and_tombstone_checked(
        agent_dir,
        expected_routing,
        None,
        status,
        exit_code,
        exit_context,
    )
    .await
}

/// Finish and tombstone only the exact invocation captured by a monitor.
///
/// A monitor started for a superseded predecessor may observe a recycled
/// `@window`/`%pane` after a fresh tmux server and report an exit. The
/// invocation ID is the durable generation marker, so a delayed monitor must
/// never retire the successor that now owns the same numeric target.
pub(crate) async fn finish_invocation_and_tombstone_for_invocation(
    agent_dir: &Path,
    expected_routing: &RoutingInfo,
    expected_invocation_id: &str,
    status: InvocationStatus,
    exit_code: Option<i32>,
    exit_context: InvocationExitContext,
) -> Result<InvocationFinishResult> {
    finish_invocation_and_tombstone_checked(
        agent_dir,
        expected_routing,
        Some(expected_invocation_id),
        status,
        exit_code,
        exit_context,
    )
    .await
}

async fn finish_invocation_and_tombstone_checked(
    agent_dir: &Path,
    expected_routing: &RoutingInfo,
    expected_invocation_id: Option<&str>,
    status: InvocationStatus,
    exit_code: Option<i32>,
    exit_context: InvocationExitContext,
) -> Result<InvocationFinishResult> {
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir).await?;
    if let Some(record) = current.as_ref() {
        if expected_invocation_id.is_some_and(|expected| expected != record.invocation_id) {
            warn!(
                path = %invocation_path(agent_dir).display(),
                current_invocation_id = %record.invocation_id,
                expected_invocation_id = ?expected_invocation_id,
                "Ignoring stale invocation exit for a superseded controller generation"
            );
            return Ok(InvocationFinishResult::IgnoredStale);
        }
        if !record.routing.same_delivery_target(expected_routing) {
            warn!(
                path = %invocation_path(agent_dir).display(),
                invocation_id = %record.invocation_id,
                "Ignoring stale invocation exit for newer routing generation"
            );
            return Ok(InvocationFinishResult::IgnoredStale);
        }
        let routing_has_owner_fields = record.routing.owner_invocation_id.is_some()
            || record.routing.owner_generation.is_some();
        let expected_has_owner_fields = expected_routing.owner_invocation_id.is_some()
            || expected_routing.owner_generation.is_some();
        if routing_has_owner_fields
            && expected_has_owner_fields
            && record.routing.owner_invocation_id != expected_routing.owner_invocation_id
        {
            warn!(
                path = %invocation_path(agent_dir).display(),
                current_invocation_id = %record.invocation_id,
                current_owner = ?record.routing.owner_invocation_id,
                expected_owner = ?expected_routing.owner_invocation_id,
                "Ignoring stale invocation exit for a different routing owner"
            );
            return Ok(InvocationFinishResult::IgnoredStale);
        }
    }

    let result = match current {
        Some(record) => {
            finish_locked(
                agent_dir,
                &record.invocation_id,
                status,
                exit_code,
                exit_context,
            )
            .await?
        }
        None => InvocationFinishResult::Missing,
    };
    let exited_at = unix_timestamp().to_string();
    tokio::fs::write(agent_dir.join("exited_at"), exited_at).await?;
    if let Ok(routing) = RoutingInfo::read_from_dir(agent_dir).await {
        routing.without_ownership().write_to_dir(agent_dir).await?;
    }
    Ok(result)
}

/// Finish an invocation only after its recorded delivery target was
/// positively observed absent. The exact checkpoint and routing owner are
/// revalidated under the invocation mutation lock so a concurrent successor
/// cannot be mistaken for the interrupted invocation.
pub(crate) async fn finish_invocation_for_missing_target(
    agent_dir: &Path,
    expected: &InvocationRecord,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    status: InvocationStatus,
    reason: &str,
    classification: &str,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir)
        .await?
        .context("controller invocation disappeared during liveness reconciliation")?;
    if current != *expected || !current.is_live() {
        anyhow::bail!("controller invocation changed during liveness reconciliation");
    }
    if current.routing.window_id.is_none() && current.routing.pane_id.is_none() {
        anyhow::bail!("live controller has no stable delivery target to reconcile");
    }
    let persisted_routing = RoutingInfo::read_from_dir(agent_dir).await?;
    if !persisted_routing.same_delivery_target(&current.routing) {
        anyhow::bail!("controller routing changed during liveness reconciliation");
    }
    if crate::services::tmux_ipc::routing_target_alive(&current.routing, tmux).await? {
        anyhow::bail!("recorded delivery target became live during liveness reconciliation");
    }
    let owner_fields = persisted_routing.owner_agent_id.is_some()
        || persisted_routing.owner_invocation_id.is_some()
        || persisted_routing.owner_generation.is_some();
    if owner_fields {
        let owner_matches = persisted_routing.has_ownership_proof()
            && persisted_routing.owner_agent_id.as_deref() == Some(agent_id_from_dir(agent_dir)?)
            && persisted_routing.owner_invocation_id.as_deref()
                == Some(current.invocation_id.as_str())
            && persisted_routing.owner_generation == Some(current.generation);
        if !owner_matches {
            anyhow::bail!("controller routing ownership conflicts with the interrupted invocation");
        }
    }
    let finished = finish_locked(
        agent_dir,
        &current.invocation_id,
        status,
        None,
        InvocationExitContext {
            reason: Some(reason.to_owned()),
            classification: Some(classification.to_owned()),
            stderr_tail: None,
        },
    )
    .await?;
    let InvocationFinishResult::Finished(finished) = finished else {
        anyhow::bail!("controller invocation could not be retired after target loss");
    };
    tokio::fs::write(agent_dir.join("exited_at"), unix_timestamp().to_string()).await?;
    persisted_routing
        .without_ownership()
        .write_to_dir(agent_dir)
        .await?;
    Ok(finished)
}

/// Retire a controller invocation whose exact historical target lifetime is
/// proven ended even though a fresh startup recycled the same numeric
/// `@window`/`%pane` IDs. The caller must already have classified the binding
/// as superseded; this function revalidates that proof under the invocation
/// mutation lock so a concurrent successor cannot be retired instead.
pub(crate) async fn finish_invocation_after_superseded_target(
    agent_dir: &Path,
    expected: &InvocationRecord,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    project_dir: &Path,
    reason: &str,
    classification: &str,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir)
        .await?
        .context("controller invocation disappeared during liveness reconciliation")?;
    if current.invocation_id == expected.invocation_id
        && current.generation == expected.generation
        && !current.is_live()
    {
        // A concurrent exit monitor can retire the same controller generation
        // after it observed the target lifetime end. The crash boundary is then
        // already durable, so converge on that record instead of rejecting the
        // stale observation and blocking successor creation.
        return Ok(current);
    }
    if current != *expected || !current.is_live() {
        anyhow::bail!("controller invocation changed during liveness reconciliation");
    }
    if current.routing.window_id.is_none() && current.routing.pane_id.is_none() {
        anyhow::bail!("live controller has no stable delivery target to reconcile");
    }
    let persisted_routing = RoutingInfo::read_from_dir(agent_dir).await?;
    if !persisted_routing.same_delivery_target(&current.routing) {
        anyhow::bail!("controller routing changed during liveness reconciliation");
    }
    let owner_fields = persisted_routing.owner_agent_id.is_some()
        || persisted_routing.owner_invocation_id.is_some()
        || persisted_routing.owner_generation.is_some();
    if owner_fields {
        let owner_matches = persisted_routing.has_ownership_proof()
            && persisted_routing.owner_agent_id.as_deref() == Some(agent_id_from_dir(agent_dir)?)
            && persisted_routing.owner_invocation_id.as_deref()
                == Some(current.invocation_id.as_str())
            && persisted_routing.owner_generation == Some(current.generation);
        if !owner_matches {
            anyhow::bail!("controller routing ownership conflicts with the interrupted invocation");
        }
    }
    match super::controller_lifecycle::reconcile_controller_binding(
        project_dir,
        tmux,
        &current.routing,
        current.controller_epoch.as_deref(),
    )
    .await?
    {
        super::ControllerBindingReconciliation::Superseded { .. } => {}
        super::ControllerBindingReconciliation::Current => {
            anyhow::bail!("controller target is still the current lifetime")
        }
        super::ControllerBindingReconciliation::Unverified { diagnostic } => {
            anyhow::bail!("controller target lifetime is unverified: {diagnostic}")
        }
    }
    let finished = finish_locked(
        agent_dir,
        &current.invocation_id,
        InvocationStatus::Killed,
        None,
        InvocationExitContext {
            reason: Some(reason.to_owned()),
            classification: Some(classification.to_owned()),
            stderr_tail: None,
        },
    )
    .await?;
    let InvocationFinishResult::Finished(finished) = finished else {
        anyhow::bail!("controller invocation could not be retired after target supersession");
    };
    tokio::fs::write(agent_dir.join("exited_at"), unix_timestamp().to_string()).await?;
    persisted_routing
        .without_ownership()
        .write_to_dir(agent_dir)
        .await?;
    Ok(finished)
}

/// Retire one controller invocation after its exact shared control pane has
/// stopped. Invocation routes may name the shared window while worker panes
/// remain in it, so window-level liveness alone is insufficient evidence.
pub(crate) async fn finish_invocation_after_controller_pane_stopped(
    agent_dir: &Path,
    expected: &InvocationRecord,
    target: &super::ControllerPaneOwnership,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
) -> Result<InvocationRecord> {
    let _guard = mutation_lock().lock().await;
    let current = read_locked(agent_dir)
        .await?
        .context("controller invocation disappeared during workspace shutdown")?;
    if current.invocation_id != expected.invocation_id
        || current.generation != expected.generation
        || !current.routing.same_delivery_target(&expected.routing)
    {
        anyhow::bail!("controller invocation changed during workspace shutdown");
    }
    if !current.is_live() {
        return Ok(current);
    }
    let legacy_root_window_route =
        agent_id_from_dir(agent_dir)? == "root" && current.routing.pane_id.is_none();
    if current.routing.window_id.as_ref() != Some(&target.window_id)
        || (current.routing.pane_id.as_ref() != Some(&target.pane_id) && !legacy_root_window_route)
    {
        anyhow::bail!("controller invocation no longer uses the accepted shared TL pane");
    }
    let server = tmux.server_identity().await?;
    if server.pid != target.tmux_server_pid || server.start_ticks != target.tmux_server_start_ticks
    {
        anyhow::bail!("tmux server identity changed during workspace shutdown");
    }
    if let Some(pane) = tmux
        .pane_snapshot(&target.tmux_session, &target.window_id, &target.pane_id)
        .await?
    {
        if !pane.dead {
            anyhow::bail!(
                "shared controller pane is still active during interruption checkpointing"
            );
        }
    }
    let finished = finish_locked(
        agent_dir,
        &current.invocation_id,
        InvocationStatus::Killed,
        None,
        InvocationExitContext {
            reason: Some("workspace control-plane shutdown".to_string()),
            classification: Some("workspace_control_plane_shutdown".to_string()),
            stderr_tail: None,
        },
    )
    .await?;
    let InvocationFinishResult::Finished(finished) = finished else {
        anyhow::bail!("controller invocation was not durably retired after pane shutdown");
    };
    tokio::fs::write(agent_dir.join("exited_at"), unix_timestamp().to_string()).await?;
    if let Ok(routing) = RoutingInfo::read_from_dir(agent_dir).await {
        routing.without_ownership().write_to_dir(agent_dir).await?;
    }
    Ok(finished)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::tmux_ipc::WindowId;
    use tempfile::tempdir;

    fn routing() -> RoutingInfo {
        RoutingInfo::window(WindowId::parse("@42").expect("valid window id"))
    }

    #[test]
    fn invocation_reader_accepts_fractional_timeout_timestamp_from_python() {
        let value = serde_json::json!({
            "invocation_id": "inv-timeout",
            "runtime": "opencode",
            "trigger": "resume_pr",
            "mode": "one_shot",
            "routing": {"window_id": "@42"},
            "started_at": 1791330626,
            "ended_at": 1791330627.2185607,
            "status": "timed_out",
            "exit_code": null,
            "pr_number": 45,
            "head_sha": "25ec8d08ca984b56497518dd05a572a116d1f883",
            "generation": 2,
            "runtime_agent_id": "issue-811-substitution-model-architecture-opencode",
            "slice_id": "issue-811-substitution-model-architecture",
            "branch": "main.substitution-model-architecture-stage.issue-811-substitution-model-architecture-opencode",
            "worktree": ".exo/worktrees/issue-811-substitution-model-architecture-opencode",
            "exit_reason": "declared_task_budget_exceeded",
            "exit_classification": "task_budget_exceeded"
        });
        let record: InvocationRecord =
            serde_json::from_value(value).expect("fractional ended_at remains readable");
        assert_eq!(record.ended_at, Some(1791330627));
    }

    #[tokio::test]
    async fn invocation_record_persists_required_fields() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            routing(),
            Some(580),
            Some("abc123".to_string()),
        )
        .await
        .expect("start invocation");

        let persisted = read_invocation(dir.path())
            .await
            .expect("read invocation")
            .expect("record exists");
        assert_eq!(persisted, record);
        assert!(!persisted.invocation_id.is_empty());
        assert!(persisted.is_live());
        assert!(dir.path().join(INVOCATION_FILENAME).exists());
    }

    #[tokio::test]
    async fn adoption_receipt_is_durable_idempotent_and_generation_scoped() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation(
            dir.path(),
            AgentType::OpenCode,
            InvocationTrigger::ResumeBlocked,
            routing(),
            None,
            None,
        )
        .await
        .expect("start invocation");

        let receipt = record_blocked_resume_adoption(
            dir.path(),
            &record.invocation_id,
            "blocked-resume:test",
        )
        .await
        .expect("record adoption receipt");
        assert_eq!(receipt.invocation_id, record.invocation_id);
        assert_eq!(receipt.intent_id, "blocked-resume:test");
        assert_eq!(receipt.prior_invocation_id, record.prior_invocation_id);
        assert!(dir.path().join(ADOPTION_RECEIPT_FILENAME).exists());
        assert_eq!(
            read_adoption_receipt(dir.path())
                .await
                .expect("read receipt")
                .expect("receipt exists"),
            receipt
        );

        let repeated = record_blocked_resume_adoption(
            dir.path(),
            &record.invocation_id,
            "blocked-resume:test",
        )
        .await
        .expect("repeat is idempotent");
        assert_eq!(repeated, receipt, "a repeat must keep the first receipt");

        assert!(
            record_blocked_resume_adoption(
                dir.path(),
                &record.invocation_id,
                "blocked-resume:another-intent",
            )
            .await
            .is_err(),
            "a receipt cannot be reused for another dispatch intent"
        );

        let successor = start_invocation(
            dir.path(),
            AgentType::OpenCode,
            InvocationTrigger::ResumeBlocked,
            routing(),
            None,
            None,
        )
        .await
        .expect("start successor invocation");
        assert!(successor.generation > record.generation);
        assert!(
            !dir.path().join(ADOPTION_RECEIPT_FILENAME).exists(),
            "a new generation retires the prior adoption receipt"
        );
    }

    #[tokio::test]
    async fn invocation_exit_context_is_bounded_and_persisted() {
        let dir = tempdir().expect("tempdir");
        let _record = start_invocation_with_provenance_and_context(
            dir.path(),
            AgentType::OpenCode,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
            None,
            None,
            Some(InvocationIdentityContext {
                runtime_agent_id: Some("tunable-operator-body-opencode".to_string()),
                slice_id: Some("tunable-operator-body".to_string()),
                branch: Some("main.tunable-operator-body".to_string()),
                worktree: Some(".exo/worktrees/tunable-operator-body-opencode".to_string()),
            }),
            None,
        )
        .await
        .expect("start invocation");
        let result = finish_invocation_and_tombstone_with_context(
            dir.path(),
            &routing(),
            InvocationStatus::Killed,
            None,
            InvocationExitContext {
                reason: Some("tmux_target_exited_without_exit_marker".to_string()),
                classification: Some("missing_exit_marker".to_string()),
                stderr_tail: Some("stderr tail".to_string()),
            },
        )
        .await
        .expect("finish invocation");
        let InvocationFinishResult::Finished(finished) = result else {
            panic!("expected finished invocation");
        };

        assert_eq!(
            finished.runtime_agent_id.as_deref(),
            Some("tunable-operator-body-opencode")
        );
        assert_eq!(finished.slice_id.as_deref(), Some("tunable-operator-body"));
        assert_eq!(
            finished.exit_classification.as_deref(),
            Some("missing_exit_marker")
        );
        assert_eq!(finished.stderr_tail.as_deref(), Some("stderr tail"));
        let persisted = tokio::fs::read_to_string(dir.path().join(INVOCATION_FILENAME))
            .await
            .expect("persisted invocation");
        assert!(persisted.contains("missing_exit_marker"));
        assert!(persisted.contains("tunable-operator-body-opencode"));
    }

    #[tokio::test]
    async fn recovery_lineage_persists_and_requires_next_generation() {
        let dir = tempdir().expect("tempdir");
        let prior = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
        )
        .await
        .expect("prior invocation");
        finish_invocation(
            dir.path(),
            &prior.invocation_id,
            InvocationStatus::Killed,
            Some(137),
        )
        .await
        .expect("finish prior invocation");
        let lineage = RecoveryInvocationLineage {
            prior_invocation_id: prior.invocation_id.clone(),
            invocation_generation: 2,
            recovery_round: 2,
            authorization_source: RecoveryAuthorization::HumanApproved,
        };
        let record = start_invocation_with_provenance_and_context(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ControllerRestart,
            routing(),
            None,
            None,
            None,
            None,
            None,
            Some(lineage.clone()),
        )
        .await
        .expect("lineage invocation");

        assert_eq!(record.generation, 2);
        assert_eq!(
            record.prior_invocation_id.as_deref(),
            Some(prior.invocation_id.as_str())
        );
        assert_eq!(record.recovery_round, 2);
        assert_eq!(
            record.authorization_source,
            Some(RecoveryAuthorization::HumanApproved)
        );
        assert_eq!(read_invocation(dir.path()).await.unwrap(), Some(record));
    }

    #[tokio::test]
    async fn one_shot_mode_is_persisted_and_survives_reload() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation_with_provenance_and_context_and_mode(
            dir.path(),
            AgentType::Claude,
            InvocationTrigger::Review,
            routing(),
            Some(43),
            Some("head-sha".to_string()),
            None,
            None,
            None,
            None,
            InvocationMode::OneShot,
        )
        .await
        .expect("one-shot invocation");

        assert_eq!(record.mode, InvocationMode::OneShot);
        assert_eq!(read_invocation(dir.path()).await.unwrap(), Some(record));
    }

    #[tokio::test]
    async fn preallocated_invocation_id_is_persisted_exactly() {
        let dir = tempdir().expect("tempdir");
        let invocation_id = "resume-invocation-id".to_string();
        let record = start_invocation_with_provenance_and_context_and_mode_with_id(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            routing(),
            Some(43),
            Some("head-sha".to_string()),
            None,
            None,
            None,
            None,
            InvocationMode::OneShot,
            invocation_id.clone(),
        )
        .await
        .expect("preallocated resume invocation");

        assert_eq!(record.invocation_id, invocation_id);
        assert_eq!(read_invocation(dir.path()).await.unwrap(), Some(record));
    }

    #[tokio::test]
    async fn invocation_record_persists_model_and_effort_provenance() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation_with_provenance(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            routing(),
            Some(580),
            Some("abc123".to_string()),
            Some("gpt-5.6-luna".to_string()),
            Some("xhigh".to_string()),
        )
        .await
        .expect("start invocation");

        let persisted = read_invocation(dir.path())
            .await
            .expect("read invocation")
            .expect("record exists");
        assert_eq!(persisted, record);
        assert_eq!(persisted.model.as_deref(), Some("gpt-5.6-luna"));
        assert_eq!(persisted.effort.as_deref(), Some("xhigh"));
    }

    #[tokio::test]
    async fn newer_invocation_survives_stale_finish_with_routing_intact() {
        let dir = tempdir().expect("tempdir");
        let first_routing = routing();
        first_routing.write_to_dir(dir.path()).await.unwrap();
        let first = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            first_routing.clone(),
            None,
            None,
        )
        .await
        .expect("first invocation");
        let second_routing = RoutingInfo::window(WindowId::parse("@43").expect("window id"));
        let second = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::ResumePr,
            second_routing.clone(),
            Some(580),
            Some("def456".to_string()),
        )
        .await
        .expect("second invocation");
        second_routing.write_to_dir(dir.path()).await.unwrap();

        assert_eq!(
            finish_invocation(
                dir.path(),
                &first.invocation_id,
                InvocationStatus::Exited,
                Some(0),
            )
            .await
            .expect("finish stale invocation"),
            InvocationFinishResult::IgnoredStale
        );
        assert_eq!(read_invocation(dir.path()).await.unwrap(), Some(second));
        assert_eq!(
            finish_invocation_and_tombstone(
                dir.path(),
                &first_routing,
                InvocationStatus::Exited,
                Some(0),
            )
            .await
            .expect("stale tombstone"),
            InvocationFinishResult::IgnoredStale
        );
        assert_eq!(
            RoutingInfo::read_from_dir(dir.path()).await.unwrap(),
            second_routing
        );
        assert!(!dir.path().join("exited_at").exists());
    }

    #[tokio::test]
    async fn current_invocation_completion_records_outcome_code_and_end_time() {
        let dir = tempdir().expect("tempdir");
        let started = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
        )
        .await
        .expect("start invocation");
        let result = finish_invocation(
            dir.path(),
            &started.invocation_id,
            InvocationStatus::Failed,
            Some(17),
        )
        .await
        .expect("finish invocation");
        let InvocationFinishResult::Finished(finished) = result else {
            panic!("expected finished invocation");
        };
        assert_eq!(finished.status, InvocationStatus::Failed);
        assert_eq!(finished.exit_code, Some(17));
        assert!(finished.ended_at.is_some());
        assert!(!finished.is_live());
    }

    #[tokio::test]
    async fn one_shot_exit_retires_routing_and_preserves_exit_code() {
        let dir = tempdir().expect("tempdir");
        let expected_routing = routing();
        expected_routing.write_to_dir(dir.path()).await.unwrap();
        let started = start_invocation(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            expected_routing.clone(),
            None,
            None,
        )
        .await
        .expect("start invocation");

        let result = finish_invocation_and_tombstone(
            dir.path(),
            &expected_routing,
            InvocationStatus::Exited,
            Some(0),
        )
        .await
        .expect("finish one-shot invocation");
        let InvocationFinishResult::Finished(finished) = result else {
            panic!("expected finished invocation");
        };
        assert_eq!(finished.invocation_id, started.invocation_id);
        assert_eq!(finished.status, InvocationStatus::Exited);
        assert_eq!(finished.exit_code, Some(0));
        assert!(!finished.is_live());
        assert!(dir.path().join("exited_at").exists());
        assert_eq!(
            RoutingInfo::read_from_dir(dir.path()).await.unwrap(),
            expected_routing
        );
    }

    #[tokio::test]
    async fn rejected_startup_does_not_create_running_invocation_record() {
        let dir = tempdir().expect("tempdir");
        assert!(!dir.path().join(INVOCATION_FILENAME).exists());
        assert!(matches!(
            crate::services::tmux_ipc::classify_window_startup(false),
            crate::services::tmux_ipc::WindowStartupStatus::ExitedBeforeReady
        ));
        assert!(read_invocation(dir.path()).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn missing_and_malformed_records_are_conservative() {
        let dir = tempdir().expect("tempdir");
        assert!(read_invocation_conservatively(dir.path()).await.is_none());
        tokio::fs::write(dir.path().join(INVOCATION_FILENAME), "not-json")
            .await
            .expect("write malformed record");
        assert!(read_invocation_conservatively(dir.path()).await.is_none());
        assert!(
            finish_invocation(dir.path(), "old", InvocationStatus::Exited, None)
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn invocation_record_persists_build_launch_command_and_config_source() {
        let dir = tempdir().expect("tempdir");
        let launch = "OPENROUTER_API_KEY=<redacted> opencode run --standalone --model opencode-go/deepseek-v4-flash#max";
        let record = start_invocation_with_provenance_and_context_and_mode_with_id_guarded(
            dir.path(),
            AgentType::OpenCode,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
            Some("opencode-go/deepseek-v4-flash".to_string()),
            Some("max".to_string()),
            None,
            None,
            InvocationMode::OneShot,
            "invocation-1352".to_string(),
            None,
            None,
            None,
            Some(launch.to_string()),
            Some(LaunchCommandSource::Actual),
            Some(RouteConfigSources {
                harness: Some("cli".to_string()),
                model: Some("local_config".to_string()),
                effort: Some("default".to_string()),
            }),
            Some("commit-injected-by-server".to_string()),
        )
        .await
        .expect("start invocation");

        assert_eq!(record.build, "commit-injected-by-server");
        assert_eq!(record.launch_command.as_deref(), Some(launch));
        assert_eq!(
            record.launch_command_source,
            Some(LaunchCommandSource::Actual)
        );
        let sources = record.config_sources.as_ref().expect("route sources");
        assert_eq!(sources.harness.as_deref(), Some("cli"));
        assert_eq!(sources.model.as_deref(), Some("local_config"));
        assert_eq!(sources.effort.as_deref(), Some("default"));
        assert_eq!(
            record.model.as_deref(),
            Some("opencode-go/deepseek-v4-flash")
        );
        assert_eq!(record.effort.as_deref(), Some("max"));

        let persisted = read_invocation(dir.path())
            .await
            .expect("read invocation")
            .expect("record exists");
        assert_eq!(persisted, record);
        let document: serde_json::Value = serde_json::from_slice(
            &tokio::fs::read(dir.path().join(INVOCATION_FILENAME))
                .await
                .unwrap(),
        )
        .expect("invocation JSON");
        assert_eq!(document["build"], "commit-injected-by-server");
        assert_eq!(document["launch_command_source"], "actual");
        assert_eq!(document["config_sources"]["harness"], "cli");
        assert_eq!(document["config_sources"]["model"], "local_config");
        assert_eq!(document["config_sources"]["effort"], "default");
        assert!(document["launch_command"]
            .as_str()
            .is_some_and(|command| command.contains("--model")));
    }

    #[test]
    fn redacted_launch_command_removes_credential_flag_values_without_env() {
        let command = "codex --api-key canary-secret-1352-do-not-persist --cd /work";
        let redacted = redacted_launch_command(command, &std::collections::HashMap::new());
        assert!(!redacted.contains("canary-secret-1352-do-not-persist"));
        assert!(redacted.contains("--api-key <redacted>"));
        assert!(redacted.contains("--cd /work"));
    }

    #[test]
    fn redacted_launch_command_removes_environment_secret_values() {
        let mut env = std::collections::HashMap::new();
        env.insert(
            "OPENROUTER_API_KEY".to_string(),
            "canary-secret-1352-do-not-persist".to_string(),
        );
        env.insert("EXOMONAD_PARENT_AGENT".to_string(), "root".to_string());
        let command = "OPENROUTER_API_KEY=canary-secret-1352-do-not-persist EXOMONAD_PARENT_AGENT=root opencode run --standalone --model opencode-go/deepseek-v4-flash";

        let redacted = redacted_launch_command(command, &env);

        assert!(!redacted.contains("canary-secret-1352-do-not-persist"));
        assert!(redacted.contains("OPENROUTER_API_KEY=<redacted>"));
        assert!(redacted.contains("opencode run --standalone"));
        assert!(redacted.contains("--model"));
    }

    #[tokio::test]
    async fn persisted_invocation_record_never_contains_a_canary_secret() {
        let dir = tempdir().expect("tempdir");
        let secret = "canary-secret-1352-do-not-persist";
        let mut env = std::collections::HashMap::new();
        env.insert("OPENROUTER_API_KEY".to_string(), secret.to_string());
        let full_command = format!(
            "OPENROUTER_API_KEY={secret} codex --dangerously-bypass-approvals-and-sandbox --cd /work"
        );
        let launch = redacted_launch_command(&full_command, &env);
        start_invocation_with_provenance_and_context_and_mode_with_id_guarded(
            dir.path(),
            AgentType::Codex,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
            Some("gpt-6-luna".to_string()),
            Some("high".to_string()),
            None,
            None,
            InvocationMode::OneShot,
            "invocation-canary".to_string(),
            None,
            None,
            None,
            Some(launch),
            Some(LaunchCommandSource::Actual),
            None,
            None,
        )
        .await
        .expect("start invocation");

        let bytes = tokio::fs::read(dir.path().join(INVOCATION_FILENAME))
            .await
            .unwrap();
        let text = String::from_utf8(bytes).unwrap();
        assert!(!text.contains(secret));
        assert!(text.contains("<redacted>"));
        assert!(text.contains("gpt-6-luna"));
    }

    #[tokio::test]
    async fn persisted_record_and_ledger_never_contain_a_flag_value_canary_secret() {
        let root = tempdir().expect("tempdir");
        let agent_dir = root.path().join(".exo/agents/canary");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        let secret = "canary-secret-1352-flag-value";
        let full_command =
            format!("codex --api-key {secret} --dangerously-bypass-approvals-and-sandbox");
        let launch = redacted_launch_command(&full_command, &std::collections::HashMap::new());
        start_invocation_with_provenance_and_context_and_mode_with_id_guarded(
            &agent_dir,
            AgentType::Codex,
            InvocationTrigger::Spawn,
            routing(),
            None,
            None,
            Some("gpt-6-luna".to_string()),
            Some("high".to_string()),
            None,
            None,
            InvocationMode::OneShot,
            "invocation-flag-canary".to_string(),
            None,
            None,
            None,
            Some(launch),
            Some(LaunchCommandSource::Actual),
            None,
            Some("commit-canary".to_string()),
        )
        .await
        .expect("start invocation");

        for entry in walk_files(root.path()) {
            let bytes = std::fs::read(&entry).expect("read persisted file");
            let text = String::from_utf8_lossy(&bytes);
            assert!(
                !text.contains(secret),
                "secret reached persisted file {}",
                entry.display()
            );
        }
        let record = read_invocation(&agent_dir)
            .await
            .expect("read invocation")
            .expect("record exists");
        assert!(record
            .launch_command
            .as_deref()
            .is_some_and(|command| command.contains("--api-key <redacted>")));
    }

    fn walk_files(root: &Path) -> Vec<std::path::PathBuf> {
        let mut files = Vec::new();
        let mut pending = vec![root.to_path_buf()];
        while let Some(directory) = pending.pop() {
            for entry in std::fs::read_dir(&directory).expect("read directory") {
                let entry = entry.expect("directory entry");
                let path = entry.path();
                if entry.file_type().expect("file type").is_dir() {
                    pending.push(path);
                } else {
                    files.push(path);
                }
            }
        }
        files
    }

    #[tokio::test]
    async fn legacy_invocation_without_launch_provenance_still_decodes() {
        let dir = tempdir().expect("tempdir");
        tokio::fs::write(
            dir.path().join(INVOCATION_FILENAME),
            serde_json::json!({
                "invocation_id": "legacy-1352",
                "runtime": "codex",
                "trigger": "spawn",
                "routing": {"window_id": "@42"},
                "started_at": 1,
                "status": "running"
            })
            .to_string(),
        )
        .await
        .unwrap();

        let record = read_invocation(dir.path())
            .await
            .expect("read invocation")
            .expect("record exists");
        assert!(record.build.is_empty());
        assert!(record.launch_command.is_none());
        assert!(record.launch_command_source.is_none());
        assert!(record.config_sources.is_none());
        assert!(record.usage.is_none());
    }

    #[test]
    fn claude_usage_envelope_sums_cache_inclusive_input_and_output() {
        let usage = parse_claude_usage_envelope(
            &serde_json::json!({
                "type": "result",
                "is_error": false,
                "result": "done",
                "usage": {
                    "input_tokens": 120,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 50,
                    "output_tokens": 25
                }
            })
            .to_string(),
        )
        .expect("authoritative envelope");
        assert_eq!(
            usage,
            InvocationUsage {
                input_tokens: 200,
                output_tokens: 25,
                total_tokens: 225,
            }
        );
    }

    #[test]
    fn partial_zero_and_ambiguous_usage_envelopes_are_not_measurements() {
        for raw in [
            r#"{"usage":{"input_tokens":10}}"#,
            r#"{"usage":{"input_tokens":10,"output_tokens":-1}}"#,
            r#"{"usage":{"input_tokens":0,"output_tokens":0}}"#,
            r#"{"result":"done"}"#,
            "not json",
            concat!(
                r#"{"usage":{"input_tokens":10,"output_tokens":2}}"#,
                "\n",
                r#"{"usage":{"input_tokens":99,"output_tokens":1}}"#
            ),
        ] {
            assert!(
                parse_claude_usage_envelope(raw).is_err(),
                "must not invent usage from {raw:?}"
            );
        }
    }

    #[tokio::test]
    async fn finish_reads_and_consumes_the_invocation_owned_usage_source() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation(
            dir.path(),
            AgentType::Claude,
            InvocationTrigger::Spawn,
            RoutingInfo::window(WindowId::parse("@42").expect("window")),
            None,
            None,
        )
        .await
        .expect("start");
        tokio::fs::write(
            dir.path().join(INVOCATION_USAGE_FILENAME),
            serde_json::json!({
                "usage": {
                    "input_tokens": 7,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 3
                }
            })
            .to_string(),
        )
        .await
        .expect("usage source");
        let InvocationFinishResult::Finished(finished) = finish_invocation(
            dir.path(),
            &record.invocation_id,
            InvocationStatus::Exited,
            Some(0),
        )
        .await
        .expect("finish") else {
            panic!("expected the current invocation to finish");
        };
        assert_eq!(
            finished.usage,
            Some(InvocationUsage {
                input_tokens: 7,
                output_tokens: 3,
                total_tokens: 10,
            })
        );
        assert!(!dir.path().join(INVOCATION_USAGE_FILENAME).exists());
    }

    #[tokio::test]
    async fn a_missing_usage_source_leaves_the_finished_invocation_conserved() {
        let dir = tempdir().expect("tempdir");
        let record = start_invocation(
            dir.path(),
            AgentType::Claude,
            InvocationTrigger::Spawn,
            RoutingInfo::window(WindowId::parse("@42").expect("window")),
            None,
            None,
        )
        .await
        .expect("start");
        let InvocationFinishResult::Finished(finished) = finish_invocation(
            dir.path(),
            &record.invocation_id,
            InvocationStatus::Exited,
            Some(0),
        )
        .await
        .expect("finish") else {
            panic!("expected the current invocation to finish");
        };
        assert!(finished.usage.is_none());
    }
}
