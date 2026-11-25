//! Explicit workspace control-plane shutdown and its durable handoff record.
//!
//! `exomonad shutdown` remains a server-only operation. This module is used by
//! the opt-in workspace shutdown endpoint and only acts on a root controller
//! pane whose exact owner was recorded by init.

use super::AgentControlService;
use crate::domain::RoutingInfo;
use crate::services::agent_control::invocation;
use crate::services::tmux_ipc::{PaneId, WindowId};
use crate::services::{
    HasAgentResolver, HasGitHubClient, HasGitWorktreeService, HasProjectDir, HasTeamRegistry,
};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::time::Instant;
use tracing::{debug, warn};
use uuid::Uuid;

pub const ROOT_CONTROLLER_OWNERSHIP_RELATIVE_PATH: &str =
    ".exo/tl-loop/root/controller-target.json";
pub const ROOT_CONTROLLER_PENDING_OWNERSHIP_RELATIVE_PATH: &str =
    ".exo/tl-loop/root/controller-target.pending.json";
pub const CONTROLLER_STARTUP_INTENT_RELATIVE_PATH: &str =
    ".exo/tl-loop/root/controller-startup.json";
pub const WORKSPACE_SHUTDOWN_RELATIVE_PATH: &str =
    ".exo/controller-lifecycle/workspace-shutdown.json";
const WORKSPACE_SHUTDOWN_GRACE: Duration = Duration::from_secs(3);
const WORKSPACE_SHUTDOWN_POLL: Duration = Duration::from_millis(50);
const ORDERED_CONTROLLER_REGISTRATION_DECISION: &str =
    "register an ordered sub-TL controller invocation";
/// The exact transient refusals a dispatch sees while `init --continue` is
/// still reconciling the control plane. A blocked-resume journal re-drive that
/// hits one of these must stay pending and retry after init commits; treating
/// it as a terminal launch failure would strand a resume the same init is
/// about to release.
pub const CONTROLLER_STARTUP_HANDOFF_INCOMPLETE: &str =
    "controller startup handoff is incomplete; run init --continue to reconcile before dispatch";
pub const WORKSPACE_SHUTDOWN_PENDING: &str =
    "workspace control-plane shutdown is pending; run init --continue to reconcile before dispatch";

/// Whether a dispatch refusal is the transient control-plane reconciliation
/// precondition rather than a durable launch failure. Matched by the exact
/// messages this module emits, so a new refusal is never retried by accident.
pub fn dispatch_precondition_is_transient(message: &str) -> bool {
    message.contains(CONTROLLER_STARTUP_HANDOFF_INCOMPLETE)
        || message.contains(WORKSPACE_SHUTDOWN_PENDING)
}

/// Exact root controller pane ownership captured by `init` after startup.
/// Numeric tmux IDs are accepted only with the server, process, run and epoch
/// identities that were observed together when this record was written.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ControllerPaneOwnership {
    pub schema_version: u32,
    pub controller_epoch: String,
    pub run_id: String,
    pub tmux_session: String,
    pub window_id: WindowId,
    pub pane_id: PaneId,
    pub pane_pid: u32,
    pub pane_start_ticks: u64,
    pub tmux_server_pid: u32,
    pub tmux_server_start_ticks: u64,
}

/// Exact tmux and process identity before a controller has written its root
/// RunStore. This is a startup handoff record, not an active shutdown owner.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ControllerPaneTarget {
    pub schema_version: u32,
    pub controller_epoch: String,
    pub tmux_session: String,
    pub window_id: WindowId,
    pub pane_id: PaneId,
    pub pane_pid: u32,
    pub pane_start_ticks: u64,
    pub tmux_server_pid: u32,
    pub tmux_server_start_ticks: u64,
}

impl ControllerPaneTarget {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1
            || self.controller_epoch.trim().is_empty()
            || self.tmux_session.trim().is_empty()
            || self.pane_pid == 0
            || self.pane_start_ticks == 0
            || self.tmux_server_pid == 0
            || self.tmux_server_start_ticks == 0
        {
            anyhow::bail!("root controller startup target is incomplete or unsupported");
        }
        Ok(())
    }

    pub fn with_run_id(&self, run_id: impl Into<String>) -> ControllerPaneOwnership {
        ControllerPaneOwnership {
            schema_version: self.schema_version,
            controller_epoch: self.controller_epoch.clone(),
            run_id: run_id.into(),
            tmux_session: self.tmux_session.clone(),
            window_id: self.window_id.clone(),
            pane_id: self.pane_id.clone(),
            pane_pid: self.pane_pid,
            pane_start_ticks: self.pane_start_ticks,
            tmux_server_pid: self.tmux_server_pid,
            tmux_server_start_ticks: self.tmux_server_start_ticks,
        }
    }
}

impl ControllerPaneOwnership {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1
            || self.controller_epoch.trim().is_empty()
            || self.run_id.trim().is_empty()
            || self.tmux_session.trim().is_empty()
            || self.pane_pid == 0
            || self.pane_start_ticks == 0
            || self.tmux_server_pid == 0
            || self.tmux_server_start_ticks == 0
        {
            anyhow::bail!("root controller pane ownership record is incomplete or unsupported");
        }
        Ok(())
    }
}

/// Persisted phase of one explicit workspace shutdown operation.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum WorkspaceShutdownPhase {
    Requested,
    ControllerStopped,
    Complete,
    Blocked,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceControllerBinding {
    pub agent_id: String,
    pub invocation_id: String,
    pub generation: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceShutdownIntent {
    pub schema_version: u32,
    pub operation_id: String,
    pub phase: WorkspaceShutdownPhase,
    pub requested_at: u64,
    pub completed_at: Option<u64>,
    pub target: ControllerPaneOwnership,
    pub controllers: Vec<WorkspaceControllerBinding>,
    pub preserved_worker_panes: u32,
    pub blocker: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct WorkspaceShutdownResult {
    pub status: String,
    pub operation_id: String,
    pub controllers_interrupted: usize,
    pub worker_panes_preserved: u32,
}

pub fn controller_ownership_path(project_dir: &Path) -> PathBuf {
    project_dir.join(ROOT_CONTROLLER_OWNERSHIP_RELATIVE_PATH)
}

pub fn pending_controller_ownership_path(project_dir: &Path) -> PathBuf {
    project_dir.join(ROOT_CONTROLLER_PENDING_OWNERSHIP_RELATIVE_PATH)
}

pub fn controller_startup_intent_path(project_dir: &Path) -> PathBuf {
    project_dir.join(CONTROLLER_STARTUP_INTENT_RELATIVE_PATH)
}

pub fn workspace_shutdown_path(project_dir: &Path) -> PathBuf {
    project_dir.join(WORKSPACE_SHUTDOWN_RELATIVE_PATH)
}

pub async fn read_controller_ownership(project_dir: &Path) -> Result<ControllerPaneOwnership> {
    let path = controller_ownership_path(project_dir);
    let bytes = tokio::fs::read(&path)
        .await
        .with_context(|| format!("read root controller ownership {}", path.display()))?;
    let ownership: ControllerPaneOwnership = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse root controller ownership {}", path.display()))?;
    ownership.validate()?;
    Ok(ownership)
}

/// Read the exact target init is preparing, preferring a durable pending
/// handoff over the previous completed controller target.
pub async fn read_controller_target_for_routing(
    project_dir: &Path,
) -> Result<ControllerPaneOwnership> {
    let pending_path = pending_controller_ownership_path(project_dir);
    match tokio::fs::read(&pending_path).await {
        Ok(bytes) => {
            let target: ControllerPaneOwnership =
                serde_json::from_slice(&bytes).with_context(|| {
                    format!(
                        "parse pending root controller target {}",
                        pending_path.display()
                    )
                })?;
            target.validate()?;
            Ok(target)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            read_controller_ownership(project_dir).await
        }
        Err(error) => Err(error).with_context(|| {
            format!(
                "read pending root controller target {}",
                pending_path.display()
            )
        }),
    }
}

/// Capture target coordinates only from the window/pane IDs returned by the
/// creating tmux operation. A title or pane position is never an identity.
pub async fn capture_controller_ownership(
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    controller_epoch: &str,
    run_id: &str,
    window_id: WindowId,
    pane_id: PaneId,
) -> Result<ControllerPaneOwnership> {
    let target = capture_controller_pane_target(tmux, controller_epoch, window_id, pane_id).await?;
    let ownership = target.with_run_id(run_id);
    ownership.validate()?;
    Ok(ownership)
}

pub async fn capture_controller_pane_target(
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    controller_epoch: &str,
    window_id: WindowId,
    pane_id: PaneId,
) -> Result<ControllerPaneTarget> {
    let server = tmux.server_identity().await?;
    let pane = tmux
        .pane_snapshot(tmux.session_name(), &window_id, &pane_id)
        .await?
        .context("new root controller pane disappeared before ownership capture")?;
    anyhow::ensure!(
        !pane.dead,
        "new root controller pane exited before ownership capture"
    );
    let target = ControllerPaneTarget {
        schema_version: 1,
        controller_epoch: controller_epoch.to_owned(),
        tmux_session: pane.session_name,
        window_id,
        pane_id,
        pane_pid: pane.pid,
        pane_start_ticks: process_start_ticks(pane.pid).await?,
        tmux_server_pid: server.pid,
        tmux_server_start_ticks: server.start_ticks,
    };
    target.validate()?;
    Ok(target)
}

pub async fn controller_pane_target_is_live(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneTarget,
) -> Result<bool> {
    target.validate()?;
    let epoch_path = project_dir.join(".exo/tl-loop/root.controller-epoch");
    let epoch = tokio::fs::read_to_string(&epoch_path)
        .await
        .with_context(|| format!("read root controller epoch {}", epoch_path.display()))?;
    if epoch.trim() != target.controller_epoch {
        anyhow::bail!("root controller epoch changed; startup target is stale");
    }
    let server = tmux.server_identity().await?;
    if server.pid != target.tmux_server_pid || server.start_ticks != target.tmux_server_start_ticks
    {
        anyhow::bail!("tmux server identity changed; root controller startup target is stale");
    }
    let owner = target.with_run_id("startup-pending");
    match observe_controller_pane(tmux, &owner).await? {
        ControllerPaneState::Live => Ok(true),
        ControllerPaneState::Stopped => Ok(false),
    }
}

/// Verify the current epoch, RunStore, tmux server, pane ID and process-start
/// identity. `false` means the exact recorded pane is gone or dead; conflicts
/// and unavailable inspection remain errors.
pub async fn controller_ownership_is_live(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
) -> Result<bool> {
    controller_ownership_liveness(project_dir, tmux, target, false).await
}

/// Startup liveness for a committed root controller. A recorded tmux server
/// lifetime that has ended proves the historical pane is gone even when a fresh
/// server recycles the same numeric ids, so replacement may proceed. Other
/// callers keep the strict conflict error from [`controller_ownership_is_live`].
pub async fn controller_ownership_is_live_for_startup(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
) -> Result<bool> {
    controller_ownership_liveness(project_dir, tmux, target, true).await
}

async fn controller_ownership_liveness(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
    ended_server_is_stopped: bool,
) -> Result<bool> {
    verify_controller_epoch(project_dir, target).await?;
    let server = tmux.server_identity().await?;
    if server.pid != target.tmux_server_pid || server.start_ticks != target.tmux_server_start_ticks
    {
        if ended_server_is_stopped {
            // The recorded tmux server lifetime has ended. Its numeric
            // @window/%pane IDs may be recycled by the fresh server for a
            // different process, so the historical pane cannot be live.
            // Same-server identity checks below remain strict.
            warn!(
                recorded_server_pid = target.tmux_server_pid,
                recorded_server_start_ticks = target.tmux_server_start_ticks,
                current_server_pid = server.pid,
                current_server_start_ticks = server.start_ticks,
                "Recorded tmux server lifetime ended; controller ownership is not live"
            );
            return Ok(false);
        }
        anyhow::bail!("tmux server identity changed; root controller ownership is stale");
    }
    match observe_controller_pane(tmux, target).await? {
        ControllerPaneState::Live => Ok(true),
        ControllerPaneState::Stopped => Ok(false),
    }
}

/// Classification of a historical controller binding against the exact target
/// that the current startup transaction recorded for routing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ControllerBindingReconciliation {
    /// The prior binding is the same exact live controller target.
    Current,
    /// The prior controller lifetime is proven ended. The numeric target was
    /// recycled by the current startup transaction, not reused by the same
    /// process.
    Superseded { classification: &'static str },
    /// The prior binding cannot be classified safely; hold without replacement.
    Unverified { diagnostic: &'static str },
}

/// Distinguish a historical ordered-controller binding from the exact pending
/// startup target captured by init.
///
/// A committed `controller-target.json` record is the historical lifetime.
/// `controller-target.pending.json` is the current startup transaction. The two
/// may carry the same numeric `@window`/`%pane` IDs after a fresh tmux server or
/// a window recreation, so only the saved server and process identities and the
/// controller epoch decide whether the old lifetime ended.
pub async fn reconcile_controller_binding(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    prior_routing: &RoutingInfo,
    prior_controller_epoch: Option<&str>,
) -> Result<ControllerBindingReconciliation> {
    let committed = match read_controller_ownership(project_dir).await {
        Ok(ownership) => ownership,
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                target = %format!("{prior_routing:?}"),
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Controller reconciliation refused: the prior controller has no readable committed ownership record"
            );
            return Ok(ControllerBindingReconciliation::Unverified {
                diagnostic: "the prior controller has no committed ownership record",
            });
        }
    };
    if !routing_matches_ownership(prior_routing, &committed) {
        warn!(
            project = %project_dir.display(),
            prior_window = ?prior_routing.window_id,
            prior_pane = ?prior_routing.pane_id,
            committed_window = %committed.window_id,
            committed_pane = %committed.pane_id,
            "Controller reconciliation refused: the prior routing does not match the committed ownership record"
        );
        return Ok(ControllerBindingReconciliation::Unverified {
            diagnostic:
                "the prior controller routing does not match its committed ownership record",
        });
    }
    if !pending_controller_ownership_path(project_dir).exists() {
        // No current startup transaction exists. The prior binding may only be
        // reused when its exact committed lifetime is still observable; a
        // recycled numeric @window/%pane alone must never revive a stale
        // invocation.
        return match controller_ownership_is_live(project_dir, tmux, &committed).await {
            Ok(true) => Ok(ControllerBindingReconciliation::Current),
            Ok(false) => {
                warn!(
                    project = %project_dir.display(),
                    target = %format!("{prior_routing:?}"),
                    "Controller reconciliation refused: the prior controller target is absent and no current startup target was recorded"
                );
                Ok(ControllerBindingReconciliation::Unverified {
                    diagnostic:
                        "the prior controller target is absent and no current startup target was recorded",
                })
            }
            Err(error) => {
                warn!(
                    project = %project_dir.display(),
                    target = %format!("{prior_routing:?}"),
                    error = %error,
                    error_chain = ?error.chain().collect::<Vec<_>>(),
                    "Controller reconciliation refused: the prior controller target identity could not be inspected and no current startup target was recorded"
                );
                Ok(ControllerBindingReconciliation::Unverified {
                    diagnostic:
                        "the prior controller target identity could not be inspected and no current startup target was recorded",
                })
            }
        };
    }
    let pending = match read_controller_target_for_routing(project_dir).await {
        Ok(target) => target,
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                path = %pending_controller_ownership_path(project_dir).display(),
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Controller reconciliation refused: the pending startup target is unreadable or invalid"
            );
            return Ok(ControllerBindingReconciliation::Unverified {
                diagnostic: "the pending startup target is unreadable or invalid",
            });
        }
    };
    if pending.run_id != committed.run_id || pending.tmux_session != committed.tmux_session {
        warn!(
            project = %project_dir.display(),
            pending_run = %pending.run_id,
            committed_run = %committed.run_id,
            pending_session = %pending.tmux_session,
            committed_session = %committed.tmux_session,
            "Controller reconciliation refused: the pending startup target belongs to another root scope"
        );
        return Ok(ControllerBindingReconciliation::Unverified {
            diagnostic: "the pending startup target belongs to another root scope",
        });
    }
    if prior_controller_epoch.is_some_and(|epoch| epoch == pending.controller_epoch) {
        return Ok(ControllerBindingReconciliation::Current);
    }
    if let Err(error) = validate_startup_handoff_evidence(project_dir, &pending).await {
        warn!(
            project = %project_dir.display(),
            path = %controller_startup_intent_path(project_dir).display(),
            error = %error,
            error_chain = ?error.chain().collect::<Vec<_>>(),
            "Controller reconciliation refused: the pending startup target does not match its startup fence"
        );
        return Ok(ControllerBindingReconciliation::Unverified {
            diagnostic: "the pending startup target does not match its startup transaction",
        });
    }
    match controller_ownership_is_live(project_dir, tmux, &pending).await {
        Ok(true) => {}
        Ok(false) => {
            warn!(
                project = %project_dir.display(),
                target = %format!("{pending:?}"),
                "Controller reconciliation refused: the pending startup target is stopped"
            );
            return Ok(ControllerBindingReconciliation::Unverified {
                diagnostic: "the pending startup target is stopped",
            });
        }
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                target = %format!("{pending:?}"),
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Controller reconciliation refused: the pending startup target identity could not be inspected"
            );
            return Ok(ControllerBindingReconciliation::Unverified {
                diagnostic: "the pending startup target identity could not be inspected",
            });
        }
    }
    if same_controller_lifetime(&committed, &pending) {
        return Ok(ControllerBindingReconciliation::Current);
    }
    match proven_superseded_lifetime(&committed, &pending).await {
        Ok(Some(classification)) => {
            debug!(
                project = %project_dir.display(),
                classification,
                committed_epoch = %committed.controller_epoch,
                pending_epoch = %pending.controller_epoch,
                "Classified the prior controller lifetime as superseded"
            );
            Ok(ControllerBindingReconciliation::Superseded { classification })
        }
        Ok(None) => {
            warn!(
                project = %project_dir.display(),
                committed_epoch = %committed.controller_epoch,
                pending_epoch = %pending.controller_epoch,
                "Controller reconciliation refused: the recorded controller process may still own the delivery target"
            );
            Ok(ControllerBindingReconciliation::Unverified {
                diagnostic: "the recorded controller process may still own the delivery target",
            })
        }
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                committed_epoch = %committed.controller_epoch,
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Controller reconciliation refused: the prior controller process identity could not be inspected"
            );
            Err(error.context(
                "the prior controller process identity could not be inspected; replacement is blocked",
            ))
        }
    }
}

fn routing_matches_ownership(routing: &RoutingInfo, ownership: &ControllerPaneOwnership) -> bool {
    routing.window_id.as_ref() == Some(&ownership.window_id)
        && routing.pane_id.as_ref() == Some(&ownership.pane_id)
}

fn same_controller_lifetime(
    left: &ControllerPaneOwnership,
    right: &ControllerPaneOwnership,
) -> bool {
    left.controller_epoch == right.controller_epoch
        && left.tmux_server_pid == right.tmux_server_pid
        && left.tmux_server_start_ticks == right.tmux_server_start_ticks
        && left.pane_pid == right.pane_pid
        && left.pane_start_ticks == right.pane_start_ticks
}

async fn proven_superseded_lifetime(
    committed: &ControllerPaneOwnership,
    current: &ControllerPaneOwnership,
) -> Result<Option<&'static str>> {
    if committed.tmux_server_pid != current.tmux_server_pid
        || committed.tmux_server_start_ticks != current.tmux_server_start_ticks
    {
        return Ok(Some(
            "the tmux server lifetime that owned the prior controller ended",
        ));
    }
    if committed.pane_pid == current.pane_pid
        && committed.pane_start_ticks == current.pane_start_ticks
    {
        return Ok(None);
    }
    match process_start_ticks(committed.pane_pid).await {
        Ok(start_ticks) if start_ticks == committed.pane_start_ticks => Ok(None),
        Ok(_) => Ok(Some("the recorded controller pane process was replaced")),
        Err(error) if process_missing(&error) => {
            Ok(Some("the recorded controller pane process ended"))
        }
        Err(error) => Err(error),
    }
}

fn process_missing(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<std::io::Error>()
            .is_some_and(|error| error.kind() == std::io::ErrorKind::NotFound)
    })
}

#[derive(Debug, Deserialize)]
struct ControllerStartupHandoffEvidence {
    schema_version: u32,
    controller_epoch: String,
    tmux_session: String,
    tmux_server_pid: u32,
    tmux_server_start_ticks: u64,
    #[serde(default)]
    target: Option<ControllerPaneTarget>,
}

/// Validate the durable startup fence against the pending target.
///
/// `init` always persists the fence before it persists the pending target (see
/// `launch_tl_recovery` and `reconcile_interrupted_controller_startup`), so a
/// pending record without a fence is a partial or tampered handoff. Fail
/// closed rather than letting an unexplained pending file select a shutdown or
/// restart target.
async fn validate_startup_handoff_evidence(
    project_dir: &Path,
    pending: &ControllerPaneOwnership,
) -> Result<()> {
    let path = controller_startup_intent_path(project_dir);
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            anyhow::bail!(
                "controller startup fence {} is missing for a persisted pending target",
                path.display()
            )
        }
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let evidence: ControllerStartupHandoffEvidence = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse controller startup fence {}", path.display()))?;
    anyhow::ensure!(
        evidence.schema_version == 1
            && evidence.controller_epoch == pending.controller_epoch
            && evidence.tmux_session == pending.tmux_session
            && evidence.tmux_server_pid == pending.tmux_server_pid
            && evidence.tmux_server_start_ticks == pending.tmux_server_start_ticks,
        "controller startup fence conflicts with the pending target identity"
    );
    if let Some(target) = evidence.target.as_ref() {
        anyhow::ensure!(
            target.controller_epoch == pending.controller_epoch
                && target.tmux_session == pending.tmux_session
                && target.window_id == pending.window_id
                && target.pane_id == pending.pane_id
                && target.pane_pid == pending.pane_pid
                && target.pane_start_ticks == pending.pane_start_ticks
                && target.tmux_server_pid == pending.tmux_server_pid
                && target.tmux_server_start_ticks == pending.tmux_server_start_ticks,
            "controller startup fence target conflicts with the pending target identity"
        );
    }
    Ok(())
}

/// Return the exact pending startup target when it still matches the current
/// controller epoch, tmux server, and recorded pane process. A stopped pending
/// pane is still returned so shutdown can finish its interruption records; a
/// stale or unreadable pending record is not selected.
pub async fn pending_startup_target_if_current(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
) -> Result<Option<ControllerPaneOwnership>> {
    let pending_path = pending_controller_ownership_path(project_dir);
    if !pending_path.exists() {
        return Ok(None);
    }
    let pending = match read_controller_target_for_routing(project_dir).await {
        Ok(target) => target,
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                path = %pending_path.display(),
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Ignoring pending startup target: the record is unreadable or invalid"
            );
            return Ok(None);
        }
    };
    if let Err(error) = validate_startup_handoff_evidence(project_dir, &pending).await {
        warn!(
            project = %project_dir.display(),
            path = %controller_startup_intent_path(project_dir).display(),
            error = %error,
            error_chain = ?error.chain().collect::<Vec<_>>(),
            "Ignoring pending startup target: the startup fence is missing or conflicts with the pending identity"
        );
        return Ok(None);
    }
    match controller_ownership_is_live(project_dir, tmux, &pending).await {
        Ok(_) => Ok(Some(pending)),
        Err(error) => {
            warn!(
                project = %project_dir.display(),
                target = %format!("{pending:?}"),
                error = %error,
                error_chain = ?error.chain().collect::<Vec<_>>(),
                "Ignoring pending startup target: its identity could not be inspected"
            );
            Ok(None)
        }
    }
}

/// Select the target a workspace shutdown must quiesce. A valid pending
/// startup target is preferred over the last committed controller because init
/// advances the controller epoch before child repair and commit. Anything that
/// cannot be validated falls back to the committed record, which still fails
/// closed when its epoch is stale.
pub async fn select_controller_shutdown_target(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
) -> Result<ControllerPaneOwnership> {
    if let Some(pending) = pending_startup_target_if_current(project_dir, tmux).await? {
        match read_controller_ownership(project_dir).await {
            Ok(committed)
                if pending.run_id == committed.run_id
                    && pending.tmux_session == committed.tmux_session =>
            {
                return Ok(pending);
            }
            Ok(committed) => {
                warn!(
                    project = %project_dir.display(),
                    pending_run = %pending.run_id,
                    committed_run = %committed.run_id,
                    pending_session = %pending.tmux_session,
                    committed_session = %committed.tmux_session,
                    "Not selecting the pending startup target: it belongs to another root scope"
                );
            }
            Err(error) => {
                debug!(
                    project = %project_dir.display(),
                    error = %error,
                    error_chain = ?error.chain().collect::<Vec<_>>(),
                    "Selecting the validated pending startup target because no committed controller ownership is readable"
                );
                return Ok(pending);
            }
        }
    }
    let committed = read_controller_ownership(project_dir).await?;
    verify_controller_epoch(project_dir, &committed).await?;
    Ok(committed)
}

pub async fn persist_pending_controller_ownership(
    project_dir: &Path,
    ownership: &ControllerPaneOwnership,
) -> Result<()> {
    ownership.validate()?;
    write_atomic(&pending_controller_ownership_path(project_dir), ownership).await
}

pub async fn clear_pending_controller_ownership(
    project_dir: &Path,
    expected_epoch: &str,
) -> Result<()> {
    let path = pending_controller_ownership_path(project_dir);
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let pending: ControllerPaneOwnership = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse pending root controller target {}", path.display()))?;
    pending.validate()?;
    if pending.controller_epoch != expected_epoch {
        anyhow::bail!(
            "pending root controller target belongs to another startup epoch; preserving it"
        );
    }
    match tokio::fs::remove_file(&path).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error)
            .with_context(|| format!("remove pending controller target {}", path.display())),
    }
}

pub async fn persist_controller_ownership(
    project_dir: &Path,
    ownership: &ControllerPaneOwnership,
) -> Result<()> {
    ownership.validate()?;
    write_atomic(&controller_ownership_path(project_dir), ownership).await
}

impl<C> AgentControlService<C>
where
    C: HasAgentResolver
        + HasGitHubClient
        + HasProjectDir
        + HasTeamRegistry
        + HasGitWorktreeService
        + 'static,
{
    /// Fence dispatch and quiesce only the root control pane captured by init.
    /// The marker remains until init completes a supported startup handoff.
    pub async fn shutdown_workspace_control_plane(&self) -> Result<WorkspaceShutdownResult> {
        let _ordered = self.ordered_sub_tl_provision_lock.lock().await;
        let _lifecycle = self
            .acquire_worktree_lifecycle("shut down the workspace control plane")
            .await?;
        let project_dir = self.project_dir();
        let path = workspace_shutdown_path(project_dir);
        let tmux = self.tmux()?;
        let mut intent = load_or_create_intent(project_dir, &tmux).await?;
        if intent.phase == WorkspaceShutdownPhase::Complete {
            return Ok(result_for(&intent, "already_complete"));
        }
        persist_intent(&path, &mut intent, None).await?;

        let outcome: Result<WorkspaceShutdownResult> = async {
            verify_controller_epoch(project_dir, &intent.target).await?;
            let target_state = observe_controller_pane(&tmux, &intent.target).await?;
            if target_state == ControllerPaneState::Live {
                tmux.send_interrupt_to_pane(&intent.target.pane_id).await?;
                let stopped = wait_for_controller_pane(
                    &tmux,
                    &intent.target,
                    WORKSPACE_SHUTDOWN_GRACE,
                )
                .await?;
                if !stopped {
                    verify_controller_epoch(project_dir, &intent.target).await?;
                    let current = observe_controller_pane(&tmux, &intent.target).await?;
                    if current != ControllerPaneState::Live {
                        anyhow::bail!("controller pane changed during bounded shutdown; refusing forced retirement");
                    }
                    tmux.kill_pane(&intent.target.pane_id).await?;
                }
            }
            wait_for_pane_absence(&tmux, &intent.target, WORKSPACE_SHUTDOWN_GRACE).await?;
            intent.phase = WorkspaceShutdownPhase::ControllerStopped;
            persist_intent(&path, &mut intent, None).await?;

            for binding in intent.controllers.clone() {
                finish_controller_binding(project_dir, &tmux, &intent.target, &binding).await?;
            }
            intent.phase = WorkspaceShutdownPhase::Complete;
            intent.completed_at = Some(unix_timestamp());
            persist_intent(&path, &mut intent, None).await?;
            Ok(result_for(&intent, "complete"))
        }
        .await;

        match outcome {
            Ok(result) => Ok(result),
            Err(error) => {
                intent.phase = WorkspaceShutdownPhase::Blocked;
                let _ = persist_intent(&path, &mut intent, Some(format!("{error:#}"))).await;
                Err(error)
            }
        }
    }

    /// A dispatch permit shares the same cross-process mutation lease used by
    /// workspace shutdown. Holding it across tmux creation prevents a process
    /// from starting after shutdown has persisted its fence.
    pub(crate) async fn acquire_dispatch_permit(
        &self,
        decision: &str,
    ) -> Result<crate::services::worktree_lifecycle::LifecycleGuard> {
        let guard = self.acquire_worktree_lifecycle(decision).await?;
        ensure_dispatch_preconditions(self.project_dir(), decision).await?;
        Ok(guard)
    }
}

/// Read-only check for the transient control-plane preconditions that fence
/// ordinary dispatch while `init --continue` reconciles. Callers that need to
/// avoid a pre-launch side effect (the blocked-resume replay) use this before
/// rendering runtime config or touching the owner worktree.
pub async fn dispatch_preconditions_ready(project_dir: &Path) -> Result<()> {
    ensure_dispatch_preconditions(project_dir, "blocked resume readiness").await
}

async fn ensure_dispatch_preconditions(project_dir: &Path, decision: &str) -> Result<()> {
    let path = workspace_shutdown_path(project_dir);
    if path.exists() && !completed_shutdown_allows_controller_registration(&path, decision).await? {
        anyhow::bail!("{WORKSPACE_SHUTDOWN_PENDING}");
    }
    if controller_startup_handoff_pending(project_dir).await?
        && decision != ORDERED_CONTROLLER_REGISTRATION_DECISION
    {
        anyhow::bail!("{CONTROLLER_STARTUP_HANDOFF_INCOMPLETE}");
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ControllerStartupFencePhase {
    Requested,
    TargetCreated,
    StateReconciled,
    Released,
}

#[derive(Debug, Deserialize)]
struct ControllerStartupFenceIntent {
    schema_version: u32,
    controller_epoch: String,
    tmux_session: String,
    tmux_server_pid: u32,
    tmux_server_start_ticks: u64,
    continue_mode: bool,
    #[serde(default)]
    root_run_id: Option<String>,
    #[serde(default)]
    target: Option<ControllerPaneTarget>,
    phase: ControllerStartupFencePhase,
}

impl ControllerStartupFenceIntent {
    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            self.schema_version == 1
                && !self.controller_epoch.trim().is_empty()
                && !self.tmux_session.trim().is_empty()
                && self.tmux_server_pid > 0
                && self.tmux_server_start_ticks > 0,
            "controller startup handoff intent is incomplete or unsupported"
        );
        if self.continue_mode {
            anyhow::ensure!(
                self.root_run_id
                    .as_deref()
                    .is_some_and(|run_id| !run_id.trim().is_empty()),
                "continued controller startup handoff lacks its accepted root identity"
            );
        }
        match (&self.target, &self.phase) {
            (None, ControllerStartupFencePhase::Requested) => {}
            (Some(target), ControllerStartupFencePhase::TargetCreated)
            | (Some(target), ControllerStartupFencePhase::StateReconciled)
            | (Some(target), ControllerStartupFencePhase::Released) => {
                target.validate()?;
                anyhow::ensure!(
                    target.controller_epoch == self.controller_epoch
                        && target.tmux_session == self.tmux_session
                        && target.tmux_server_pid == self.tmux_server_pid
                        && target.tmux_server_start_ticks == self.tmux_server_start_ticks,
                    "controller startup handoff target conflicts with its durable identity"
                );
            }
            _ => anyhow::bail!("controller startup handoff phase and target disagree"),
        }
        Ok(())
    }
}

async fn controller_startup_handoff_pending(project_dir: &Path) -> Result<bool> {
    let path = controller_startup_intent_path(project_dir);
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let intent: ControllerStartupFenceIntent = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse controller startup handoff fence {}", path.display()))?;
    intent.validate()?;
    Ok(true)
}

async fn completed_shutdown_allows_controller_registration(
    path: &Path,
    decision: &str,
) -> Result<bool> {
    if decision != ORDERED_CONTROLLER_REGISTRATION_DECISION {
        return Ok(false);
    }
    let bytes = tokio::fs::read(path)
        .await
        .with_context(|| format!("read workspace shutdown fence {}", path.display()))?;
    let intent: WorkspaceShutdownIntent = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse workspace shutdown fence {}", path.display()))?;
    if intent.schema_version != 1 || intent.operation_id.trim().is_empty() {
        anyhow::bail!("workspace shutdown fence is malformed; startup reconciliation is required");
    }
    Ok(intent.phase == WorkspaceShutdownPhase::Complete)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ControllerPaneState {
    Live,
    Stopped,
}

async fn load_or_create_intent(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
) -> Result<WorkspaceShutdownIntent> {
    let path = workspace_shutdown_path(project_dir);
    match tokio::fs::read(&path).await {
        Ok(bytes) => {
            let mut intent: WorkspaceShutdownIntent = serde_json::from_slice(&bytes)
                .with_context(|| format!("parse shutdown intent {}", path.display()))?;
            if intent.schema_version != 1 || intent.operation_id.trim().is_empty() {
                anyhow::bail!(
                    "workspace shutdown intent is malformed; startup reconciliation is required"
                );
            }
            // A completed intent normally proves the prior controller is gone.
            // It can still predate an interrupted startup that has not yet
            // archived it. A newer live pending target in the same root scope is
            // a different controller lifetime and must be quiesced by this
            // supported command instead of reporting the old operation complete.
            retarget_interrupted_shutdown(project_dir, tmux, &mut intent).await?;
            Ok(intent)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let target = select_controller_shutdown_target(project_dir, tmux).await?;
            verify_controller_epoch(project_dir, &target).await?;
            let controllers = collect_shared_controller_bindings(project_dir, &target).await?;
            let preserved_worker_panes = count_worker_panes(project_dir, &target).await?;
            Ok(WorkspaceShutdownIntent {
                schema_version: 1,
                operation_id: Uuid::new_v4().to_string(),
                phase: WorkspaceShutdownPhase::Requested,
                requested_at: unix_timestamp(),
                completed_at: None,
                target,
                controllers,
                preserved_worker_panes,
                blocker: None,
            })
        }
        Err(error) => {
            Err(error).with_context(|| format!("read shutdown intent {}", path.display()))
        }
    }
}

/// A shutdown intent can survive a crash while init advances the controller
/// epoch and persists a new pending startup target. Retarget the unfinished
/// operation to that exact pending target only when it still belongs to the same
/// root run and tmux session scope.
async fn retarget_interrupted_shutdown(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    intent: &mut WorkspaceShutdownIntent,
) -> Result<()> {
    let Some(pending) = pending_startup_target_if_current(project_dir, tmux).await? else {
        return Ok(());
    };
    let same_scope = pending.run_id == intent.target.run_id
        && pending.tmux_session == intent.target.tmux_session;
    if !same_scope || pending.controller_epoch == intent.target.controller_epoch {
        return Ok(());
    }
    intent.target = pending;
    intent.controllers = collect_shared_controller_bindings(project_dir, &intent.target).await?;
    intent.preserved_worker_panes = count_worker_panes(project_dir, &intent.target).await?;
    intent.phase = WorkspaceShutdownPhase::Requested;
    intent.completed_at = None;
    Ok(())
}

async fn collect_shared_controller_bindings(
    project_dir: &Path,
    target: &ControllerPaneOwnership,
) -> Result<Vec<WorkspaceControllerBinding>> {
    let agents = project_dir.join(".exo/agents");
    let mut entries = tokio::fs::read_dir(&agents)
        .await
        .with_context(|| format!("read controller registry {}", agents.display()))?;
    let mut bindings = Vec::new();
    let mut identities = HashMap::new();
    let mut shared_scope_agents = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let agent_id = entry.file_name().to_string_lossy().into_owned();
        let agent_dir = entry.path();
        let identity_path = agent_dir.join("identity.json");
        let identity = match tokio::fs::read(&identity_path).await {
            Ok(bytes) => Some(
                serde_json::from_slice::<crate::services::agent_resolver::AgentIdentityRecord>(
                    &bytes,
                )
                .with_context(|| {
                    format!("parse controller identity {}", identity_path.display())
                })?,
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| {
                    format!("read controller identity {}", identity_path.display())
                })
            }
        };
        if let Some(identity) = identity {
            if identity.agent_name.as_str() != agent_id {
                anyhow::bail!("controller identity name conflicts with its registry directory");
            }
            identities.insert(agent_id.clone(), identity);
        }
        let Some(invocation) = invocation::read_invocation(&agent_dir).await? else {
            continue;
        };
        if !invocation.is_live() || invocation.routing.window_id.as_ref() != Some(&target.window_id)
        {
            continue;
        }
        match invocation.routing.pane_id.as_ref() {
            Some(pane_id) if pane_id == &target.pane_id => {}
            Some(_) => continue,
            None if agent_id == "root" => {}
            None => anyhow::bail!(
                "live invocation {agent_id} shares the controller window without an exact pane binding"
            ),
        }
        if agent_id != "root" {
            let identity = identities.get(&agent_id).context(
                "a live controller on the shared TL pane has no durable identity record",
            )?;
            if !identity.ledger_owned || identity.slice_id.is_none() {
                anyhow::bail!("a live non-controller owner shares the root TL route; refusing workspace shutdown");
            }
            shared_scope_agents.push(agent_id.clone());
        }
        bindings.push(WorkspaceControllerBinding {
            agent_id,
            invocation_id: invocation.invocation_id,
            generation: invocation.generation,
        });
    }
    let root_branch = identities
        .get("root")
        .map(|identity| identity.birth_branch.to_string())
        .or_else(|| {
            std::fs::read_to_string(project_dir.join(".exo/agents/root/.birth_branch"))
                .ok()
                .map(|branch| branch.trim().to_owned())
                .filter(|branch| !branch.is_empty())
        })
        .context("root branch identity is unavailable for shared-controller validation")?;
    for agent_id in shared_scope_agents {
        let mut visited = Vec::new();
        if !controller_descends_from_root(&agent_id, &root_branch, &identities, &mut visited) {
            anyhow::bail!(
                "shared TL controller {agent_id} is not bound to the accepted root declaration"
            );
        }
    }
    bindings.sort_by(|left, right| left.agent_id.cmp(&right.agent_id));
    Ok(bindings)
}

fn controller_descends_from_root(
    agent_id: &str,
    root_branch: &str,
    identities: &HashMap<String, crate::services::agent_resolver::AgentIdentityRecord>,
    visited: &mut Vec<String>,
) -> bool {
    if visited.iter().any(|seen| seen == agent_id) {
        return false;
    }
    visited.push(agent_id.to_owned());
    let Some(identity) = identities.get(agent_id) else {
        return false;
    };
    if identity.parent_branch.as_str() == root_branch {
        return true;
    }
    let parents = identities
        .iter()
        .filter(|(name, parent)| {
            name.as_str() != agent_id
                && parent.birth_branch.as_str() == identity.parent_branch.as_str()
                && parent.ledger_owned
                && parent.slice_id.is_some()
        })
        .map(|(name, _)| name.as_str())
        .collect::<Vec<_>>();
    if parents.len() != 1 {
        return false;
    }
    controller_descends_from_root(parents[0], root_branch, identities, visited)
}

async fn count_worker_panes(project_dir: &Path, target: &ControllerPaneOwnership) -> Result<u32> {
    let agents = project_dir.join(".exo/agents");
    let mut entries = tokio::fs::read_dir(&agents).await?;
    let mut count = 0_u32;
    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let agent_dir = entry.path();
        let Some(invocation) = invocation::read_invocation(&agent_dir).await? else {
            continue;
        };
        if invocation.is_live() && invocation.routing.window_id.as_ref() == Some(&target.window_id)
        {
            match invocation.routing.pane_id.as_ref() {
                Some(pane_id) if pane_id == &target.pane_id => {}
                Some(_) => count = count.saturating_add(1),
                None if entry.file_name() == "root" => {}
                None => anyhow::bail!(
                    "live invocation {} shares the controller window without an exact pane binding",
                    entry.file_name().to_string_lossy()
                ),
            }
        }
    }
    Ok(count)
}

async fn verify_controller_epoch(
    project_dir: &Path,
    target: &ControllerPaneOwnership,
) -> Result<()> {
    target.validate()?;
    let epoch_path = project_dir.join(".exo/tl-loop/root.controller-epoch");
    let epoch = tokio::fs::read_to_string(&epoch_path)
        .await
        .with_context(|| format!("read root controller epoch {}", epoch_path.display()))?;
    if epoch.trim() != target.controller_epoch {
        anyhow::bail!("root controller epoch changed; workspace shutdown evidence is stale");
    }
    let run_path = project_dir.join(".exo/tl-loop/root/run.json");
    let run_bytes = tokio::fs::read(&run_path)
        .await
        .with_context(|| format!("read root RunStore {}", run_path.display()))?;
    let run: serde_json::Value = serde_json::from_slice(&run_bytes)
        .with_context(|| format!("parse root RunStore {}", run_path.display()))?;
    if run.get("run_id").and_then(serde_json::Value::as_str) != Some(&target.run_id) {
        anyhow::bail!("root controller run identity changed; workspace shutdown evidence is stale");
    }
    Ok(())
}

async fn observe_controller_pane(
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
) -> Result<ControllerPaneState> {
    let server = tmux.server_identity().await?;
    if server.pid != target.tmux_server_pid || server.start_ticks != target.tmux_server_start_ticks
    {
        anyhow::bail!("tmux server identity changed; refusing to act on a reused pane ID");
    }
    let Some(pane) = tmux
        .pane_snapshot(&target.tmux_session, &target.window_id, &target.pane_id)
        .await?
    else {
        return Ok(ControllerPaneState::Stopped);
    };
    if pane.pid != target.pane_pid {
        anyhow::bail!("root controller pane PID changed; refusing to stop a replacement process");
    }
    if pane.dead {
        return Ok(ControllerPaneState::Stopped);
    }
    let start_ticks = process_start_ticks(pane.pid).await?;
    if start_ticks != target.pane_start_ticks {
        anyhow::bail!(
            "root controller pane process identity changed; refusing to stop a replacement process"
        );
    }
    Ok(ControllerPaneState::Live)
}

async fn wait_for_controller_pane(
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
    timeout: Duration,
) -> Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        if observe_controller_pane(tmux, target).await? == ControllerPaneState::Stopped {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        tokio::time::sleep(WORKSPACE_SHUTDOWN_POLL).await;
    }
}

async fn wait_for_pane_absence(
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
    timeout: Duration,
) -> Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        let Some(pane) = tmux
            .pane_snapshot(&target.tmux_session, &target.window_id, &target.pane_id)
            .await?
        else {
            return Ok(());
        };
        if pane.pid != target.pane_pid {
            anyhow::bail!("controller pane changed before shutdown completed");
        }
        if pane.dead {
            let server = tmux.server_identity().await?;
            if server.pid != target.tmux_server_pid
                || server.start_ticks != target.tmux_server_start_ticks
            {
                anyhow::bail!("tmux server identity changed before dead controller pane cleanup");
            }
            tmux.kill_pane(&target.pane_id).await?;
            continue;
        }
        if Instant::now() >= deadline {
            anyhow::bail!("root controller pane did not retire within the bounded shutdown period");
        }
        tokio::time::sleep(WORKSPACE_SHUTDOWN_POLL).await;
    }
}

async fn finish_controller_binding(
    project_dir: &Path,
    tmux: &crate::services::tmux_ipc::TmuxIpc,
    target: &ControllerPaneOwnership,
    binding: &WorkspaceControllerBinding,
) -> Result<()> {
    let agent_dir = project_dir.join(".exo/agents").join(&binding.agent_id);
    let current = invocation::read_invocation(&agent_dir)
        .await?
        .context("controller invocation disappeared during workspace shutdown")?;
    if current.invocation_id != binding.invocation_id || current.generation != binding.generation {
        anyhow::bail!(
            "controller invocation changed during workspace shutdown; preserving newer owner"
        );
    }
    if !current.is_live() {
        return Ok(());
    }
    let legacy_root_window_route = binding.agent_id == "root" && current.routing.pane_id.is_none();
    if current.routing.window_id.as_ref() != Some(&target.window_id)
        || (current.routing.pane_id.as_ref() != Some(&target.pane_id) && !legacy_root_window_route)
    {
        anyhow::bail!(
            "controller route changed during workspace shutdown; preserving current owner"
        );
    }
    let pane = tmux
        .pane_snapshot(&target.tmux_session, &target.window_id, &target.pane_id)
        .await?;
    if pane.is_some() {
        anyhow::bail!("controller pane reappeared before interruption checkpointing");
    }
    invocation::finish_invocation_after_controller_pane_stopped(&agent_dir, &current, target, tmux)
        .await?;
    Ok(())
}

async fn persist_intent(
    path: &Path,
    intent: &mut WorkspaceShutdownIntent,
    blocker: Option<String>,
) -> Result<()> {
    intent.blocker = blocker;
    write_atomic(path, intent).await
}

async fn write_atomic<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let parent = path
        .parent()
        .context("shutdown record path has no parent")?;
    tokio::fs::create_dir_all(parent).await?;
    let temporary = parent.join(format!(".workspace-shutdown-{}.tmp", Uuid::new_v4()));
    let bytes = serde_json::to_vec_pretty(value)?;
    if let Err(error) = tokio::fs::write(&temporary, bytes).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error)
            .with_context(|| format!("write shutdown record {}", temporary.display()));
    }
    if let Err(error) = tokio::fs::rename(&temporary, path).await {
        let _ = tokio::fs::remove_file(&temporary).await;
        return Err(error).with_context(|| format!("replace shutdown record {}", path.display()));
    }
    Ok(())
}

fn result_for(intent: &WorkspaceShutdownIntent, status: &str) -> WorkspaceShutdownResult {
    WorkspaceShutdownResult {
        status: status.to_owned(),
        operation_id: intent.operation_id.clone(),
        controllers_interrupted: intent.controllers.len(),
        worker_panes_preserved: intent.preserved_worker_panes,
    }
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

async fn process_start_ticks(pid: u32) -> Result<u64> {
    let path = PathBuf::from(format!("/proc/{pid}/stat"));
    let stat = tokio::fs::read_to_string(&path)
        .await
        .with_context(|| format!("inspect owned controller process {pid}"))?;
    let fields = stat
        .rsplit_once(')')
        .context("process stat is malformed")?
        .1
        .split_whitespace()
        .collect::<Vec<_>>();
    fields
        .get(19)
        .context("process stat lacks start-time field")?
        .parse()
        .context("process start-time field is malformed")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, RoutingInfo, Slug};
    use crate::services::agent_control::test_tracing::global_test_event_sink;
    use crate::services::agent_control::{
        AgentIdentityRecord, AgentType, InvocationRecord, InvocationStatus, InvocationTrigger,
        Topology,
    };
    use crate::services::agent_resolver::AgentResolver;
    use exomonad_test_support::TmuxTestServer;
    use std::process::{Command, Stdio};
    use std::sync::Arc;

    #[test]
    fn transient_control_plane_preconditions_are_classified_exactly() {
        assert!(dispatch_precondition_is_transient(
            CONTROLLER_STARTUP_HANDOFF_INCOMPLETE
        ));
        assert!(dispatch_precondition_is_transient(
            WORKSPACE_SHUTDOWN_PENDING
        ));
        assert!(dispatch_precondition_is_transient(&format!(
            "[agent_error] {}",
            CONTROLLER_STARTUP_HANDOFF_INCOMPLETE
        )));
        assert!(!dispatch_precondition_is_transient(
            "worktree check failed: uncommitted or untracked files are present"
        ));
        assert!(!dispatch_precondition_is_transient(
            "blocked owner invocation is still live; refusing duplicate resume"
        ));
    }

    /// A predecessor controller epoch must never authorize the current
    /// controller's run, pane, or process identity.
    #[tokio::test]
    async fn controller_epoch_mismatch_still_rejects_ownership_evidence() {
        let project = tempfile::tempdir().unwrap();
        tokio::fs::create_dir_all(project.path().join(".exo/tl-loop/root"))
            .await
            .unwrap();
        tokio::fs::write(
            project.path().join(".exo/tl-loop/root.controller-epoch"),
            "epoch-current\n",
        )
        .await
        .unwrap();
        tokio::fs::write(
            project.path().join(".exo/tl-loop/root/run.json"),
            r#"{"run_id":"run-1"}"#,
        )
        .await
        .unwrap();
        let target = ControllerPaneOwnership {
            schema_version: 1,
            controller_epoch: "epoch-predecessor".to_string(),
            run_id: "run-1".to_string(),
            tmux_session: "session".to_string(),
            window_id: WindowId::parse("@1").unwrap(),
            pane_id: PaneId::parse("%1").unwrap(),
            pane_pid: 1,
            pane_start_ticks: 1,
            tmux_server_pid: 1,
            tmux_server_start_ticks: 1,
        };

        let error = verify_controller_epoch(project.path(), &target)
            .await
            .unwrap_err()
            .to_string();

        assert!(
            error.contains("root controller epoch changed"),
            "unexpected error: {error}"
        );
    }

    struct IsolatedTmux {
        socket: String,
        session: String,
        window: WindowId,
        pane: PaneId,
        _server: TmuxTestServer,
    }

    impl IsolatedTmux {
        fn new() -> Self {
            let server = TmuxTestServer::new();
            let socket = server.socket().to_owned();
            let session = format!("exo-controller-lifecycle-{}", Uuid::new_v4());
            let output = Self::command(&socket)
                .args([
                    "new-session",
                    "-d",
                    "-s",
                    &session,
                    "-n",
                    "TL",
                    "-P",
                    "-F",
                    "#{window_id}",
                    "sh",
                    "-c",
                    "sleep 300",
                ])
                .output()
                .expect("start isolated tmux server");
            assert!(output.status.success(), "isolated tmux server starts");
            let window = WindowId::parse(String::from_utf8_lossy(&output.stdout).trim())
                .expect("tmux returns the controller window ID");
            let pane_output = Self::command(&socket)
                .args([
                    "list-panes",
                    "-t",
                    &format!("{session}:{}", window.as_str()),
                    "-F",
                    "#{pane_id}",
                ])
                .output()
                .expect("list controller pane");
            assert!(pane_output.status.success());
            let pane = PaneId::parse(String::from_utf8_lossy(&pane_output.stdout).trim())
                .expect("tmux returns the controller pane ID");
            Self {
                socket,
                session,
                window,
                pane,
                _server: server,
            }
        }

        fn command(socket: &str) -> Command {
            let mut command = Command::new("tmux");
            command
                .args(["-S", socket])
                .env_remove("TMUX")
                .env_remove("TMUX_PANE")
                .stdout(Stdio::piped());
            command
        }

        fn add_worker_pane(&self) -> PaneId {
            let output = Self::command(&self.socket)
                .args([
                    "split-window",
                    "-d",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    &format!("{}:{}", self.session, self.window.as_str()),
                    "sleep",
                    "300",
                ])
                .output()
                .expect("create independent worker pane");
            assert!(output.status.success(), "worker pane starts");
            PaneId::parse(String::from_utf8_lossy(&output.stdout).trim())
                .expect("tmux returns worker pane ID")
        }

        fn retain_exited_panes(&self) {
            let status = Self::command(&self.socket)
                .args([
                    "set-window-option",
                    "-t",
                    &format!("{}:{}", self.session, self.window.as_str()),
                    "remain-on-exit",
                    "on",
                ])
                .status()
                .expect("configure retained dead panes");
            assert!(status.success(), "tmux accepts remain-on-exit");
        }

        fn ignore_controller_interrupt(&self) {
            let status = Self::command(&self.socket)
                .args([
                    "respawn-pane",
                    "-k",
                    "-t",
                    self.pane.as_str(),
                    "sh",
                    "-c",
                    "trap '' INT; exec sleep 300",
                ])
                .status()
                .expect("restart controller pane with SIGINT ignored");
            assert!(status.success(), "tmux restarts the controller pane");
            // `respawn-pane` returns before the shell installs its trap. Wait
            // for the ignored-SIGINT mask so the bounded fallback test cannot
            // race the interrupt against shell startup.
            let output = Self::command(&self.socket)
                .args(["list-panes", "-t", self.pane.as_str(), "-F", "#{pane_pid}"])
                .output()
                .expect("read respawned controller pane pid");
            assert!(output.status.success(), "respawned pane is inspectable");
            let pid: u32 = String::from_utf8_lossy(&output.stdout)
                .lines()
                .next()
                .unwrap_or_default()
                .trim()
                .parse()
                .expect("respawned pane has a numeric pid");
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            loop {
                if let Ok(contents) = std::fs::read_to_string(format!("/proc/{pid}/status")) {
                    if let Some(mask) = contents.lines().find_map(|line| {
                        line.strip_prefix("SigIgn:").and_then(|value| {
                            u64::from_str_radix(value.trim().trim_start_matches("0x"), 16).ok()
                        })
                    }) {
                        if mask & 0x2 != 0 {
                            return;
                        }
                    }
                }
                assert!(
                    std::time::Instant::now() < deadline,
                    "controller pane {pid} did not install its SIGINT trap"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }

    fn identity(
        name: &str,
        birth: &str,
        parent: &str,
        ledger_owned: bool,
        slice_id: Option<&str>,
    ) -> AgentIdentityRecord {
        AgentIdentityRecord {
            agent_name: AgentName::try_from_str(name).unwrap(),
            slug: Slug::try_from_str(name).unwrap(),
            agent_type: AgentType::Codex,
            birth_branch: BirthBranch::try_from_str(birth).unwrap(),
            parent_branch: BirthBranch::try_from_str(parent).unwrap(),
            working_dir: PathBuf::from(if name == "root" {
                ".".to_owned()
            } else {
                format!(".exo/worktrees/{name}")
            }),
            display_name: name.to_owned(),
            topology: Topology::WorktreePerAgent,
            model: None,
            effort: None,
            role_route: None,
            ledger_owned,
            slice_id: slice_id.map(ToOwned::to_owned),
        }
    }

    async fn service_for(
        project_dir: &Path,
        session: &str,
    ) -> AgentControlService<crate::services::Services> {
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.to_path_buf();
        services.agent_resolver = Arc::new(AgentResolver::load(project_dir.to_path_buf()).await);
        AgentControlService::new(Arc::new(services)).with_tmux_session(session.to_owned())
    }

    async fn seed_running_invocation(
        project_dir: &Path,
        agent_id: &str,
        route: RoutingInfo,
        identity: AgentIdentityRecord,
    ) -> InvocationRecord {
        let agent_dir = project_dir.join(".exo/agents").join(agent_id);
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        tokio::fs::write(
            agent_dir.join("identity.json"),
            serde_json::to_vec_pretty(&identity).unwrap(),
        )
        .await
        .unwrap();
        super::super::start_invocation(
            &agent_dir,
            AgentType::Codex,
            InvocationTrigger::Spawn,
            route,
            None,
            None,
        )
        .await
        .unwrap()
    }

    async fn seed_root_target(project_dir: &Path, tmux: &IsolatedTmux) -> ControllerPaneOwnership {
        tokio::fs::create_dir_all(project_dir.join(".exo/tl-loop/root"))
            .await
            .unwrap();
        tokio::fs::create_dir_all(project_dir.join(".exo/agents/root"))
            .await
            .unwrap();
        tokio::fs::write(
            project_dir.join(".exo/tl-loop/root.controller-epoch"),
            "epoch-1\n",
        )
        .await
        .unwrap();
        tokio::fs::write(
            project_dir.join(".exo/tl-loop/root/run.json"),
            r#"{"run_id":"run-1"}"#,
        )
        .await
        .unwrap();
        let identity = identity("root", "main", "main", true, Some("root"));
        tokio::fs::write(
            project_dir.join(".exo/agents/root/identity.json"),
            serde_json::to_vec_pretty(&identity).unwrap(),
        )
        .await
        .unwrap();
        tokio::fs::write(project_dir.join(".exo/agents/root/.birth_branch"), "main\n")
            .await
            .unwrap();
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        let server = tmux_ipc.server_identity().await.unwrap();
        let pane = tmux_ipc
            .pane_snapshot(&tmux.session, &tmux.window, &tmux.pane)
            .await
            .unwrap()
            .unwrap();
        let target = ControllerPaneOwnership {
            schema_version: 1,
            controller_epoch: "epoch-1".to_owned(),
            run_id: "run-1".to_owned(),
            tmux_session: tmux.session.clone(),
            window_id: tmux.window.clone(),
            pane_id: tmux.pane.clone(),
            pane_pid: pane.pid,
            pane_start_ticks: process_start_ticks(pane.pid).await.unwrap(),
            tmux_server_pid: server.pid,
            tmux_server_start_ticks: server.start_ticks,
        };
        persist_controller_ownership(project_dir, &target)
            .await
            .unwrap();
        target
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn ended_tmux_server_lifetime_makes_root_ownership_not_live() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let target = seed_root_target(project.path(), &tmux).await;
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(
            controller_ownership_is_live(project.path(), &tmux_ipc, &target)
                .await
                .unwrap()
        );

        // A fresh server may hand out the same @window/%pane ids. The saved
        // server identity proves the recorded lifetime ended, so the historical
        // pane must not be reported live even though those ids exist again.
        let mut replaced_server_pid = target.clone();
        replaced_server_pid.tmux_server_pid = target.tmux_server_pid.saturating_add(1);
        assert!(!controller_ownership_is_live_for_startup(
            project.path(),
            &tmux_ipc,
            &replaced_server_pid
        )
        .await
        .unwrap());
        assert!(
            controller_ownership_is_live(project.path(), &tmux_ipc, &replaced_server_pid)
                .await
                .is_err(),
            "non-startup callers keep the strict server conflict"
        );

        let mut replaced_server_ticks = target.clone();
        replaced_server_ticks.tmux_server_start_ticks =
            target.tmux_server_start_ticks.saturating_add(1);
        assert!(!controller_ownership_is_live_for_startup(
            project.path(),
            &tmux_ipc,
            &replaced_server_ticks
        )
        .await
        .unwrap());

        // Within the same server lifetime a changed pane process is still an
        // identity conflict, not an absence.
        let mut replaced_pane = target;
        replaced_pane.pane_pid = replaced_pane.pane_pid.saturating_add(1);
        assert!(
            controller_ownership_is_live(project.path(), &tmux_ipc, &replaced_pane)
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn pending_target_cleanup_requires_its_exact_startup_epoch() {
        let project = tempfile::tempdir().unwrap();
        let target = ControllerPaneOwnership {
            schema_version: 1,
            controller_epoch: "epoch-current".to_owned(),
            run_id: "run-1".to_owned(),
            tmux_session: "workspace".to_owned(),
            window_id: WindowId::parse("@7").unwrap(),
            pane_id: PaneId::parse("%9").unwrap(),
            pane_pid: 123,
            pane_start_ticks: 456,
            tmux_server_pid: 321,
            tmux_server_start_ticks: 654,
        };
        persist_pending_controller_ownership(project.path(), &target)
            .await
            .unwrap();

        let error = clear_pending_controller_ownership(project.path(), "other-epoch")
            .await
            .unwrap_err();
        assert!(error.to_string().contains("another startup epoch"));
        assert!(pending_controller_ownership_path(project.path()).exists());

        clear_pending_controller_ownership(project.path(), "epoch-current")
            .await
            .unwrap();
        assert!(!pending_controller_ownership_path(project.path()).exists());
    }

    #[tokio::test]
    async fn dispatch_permit_stays_fenced_until_startup_handoff_is_removed() {
        let project = tempfile::tempdir().unwrap();
        let service = service_for(project.path(), "workspace").await;
        let startup_path = project
            .path()
            .join(".exo/tl-loop/root/controller-startup.json");
        tokio::fs::create_dir_all(startup_path.parent().unwrap())
            .await
            .unwrap();
        // This test has no workspace-shutdown marker: the startup intent itself
        // must continue fencing ordinary dispatch after init archives shutdown.
        for continue_mode in [false, true] {
            for phase in [
                "requested",
                "target_created",
                "state_reconciled",
                "released",
            ] {
                let target = ControllerPaneTarget {
                    schema_version: 1,
                    controller_epoch: "epoch-1".to_owned(),
                    tmux_session: "workspace".to_owned(),
                    window_id: WindowId::parse("@7").unwrap(),
                    pane_id: PaneId::parse("%9").unwrap(),
                    pane_pid: 123,
                    pane_start_ticks: 456,
                    tmux_server_pid: 321,
                    tmux_server_start_ticks: 654,
                };
                let target_value = if phase == "requested" {
                    serde_json::Value::Null
                } else {
                    let ownership = target.with_run_id("run-1");
                    tokio::fs::write(
                        pending_controller_ownership_path(project.path()),
                        serde_json::to_vec(&ownership).unwrap(),
                    )
                    .await
                    .unwrap();
                    if phase == "released" {
                        tokio::fs::write(
                            controller_ownership_path(project.path()),
                            serde_json::to_vec(&ownership).unwrap(),
                        )
                        .await
                        .unwrap();
                    }
                    serde_json::to_value(target).unwrap()
                };
                tokio::fs::write(
                    &startup_path,
                    serde_json::to_vec(&serde_json::json!({
                        "schema_version": 1,
                        "controller_epoch": "epoch-1",
                        "previous_controller_epoch": null,
                        "tmux_session": "workspace",
                        "tmux_server_pid": 321,
                        "tmux_server_start_ticks": 654,
                        "continue_mode": continue_mode,
                        "root_run_id": if continue_mode { Some("run-1") } else { None },
                        "target": target_value,
                        "phase": phase
                    }))
                    .unwrap(),
                )
                .await
                .unwrap();

                let error = service
                    .acquire_dispatch_permit("start another worker")
                    .await
                    .expect_err("ordinary dispatch remains fenced until startup commits");
                assert!(error.to_string().contains("controller startup handoff"));
                drop(
                    service
                        .acquire_dispatch_permit("register an ordered sub-TL controller invocation")
                        .await
                        .expect("ordered controller registration can reconcile startup"),
                );
            }
        }

        tokio::fs::remove_file(startup_path).await.unwrap();
        drop(
            service
                .acquire_dispatch_permit("start another worker")
                .await
                .expect("dispatch resumes after startup handoff is committed"),
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn workspace_shutdown_stops_only_verified_controller_and_is_idempotent() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        tmux.retain_exited_panes();
        let target = seed_root_target(project.path(), &tmux).await;
        let root_route = RoutingInfo::window_pane(tmux.window.clone(), target.pane_id.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            root_route.clone(),
            identity("root", "main", "main", true, Some("root")),
        )
        .await;
        let _child_invocation = seed_running_invocation(
            project.path(),
            "architecture",
            root_route.clone(),
            identity(
                "architecture",
                "main.architecture",
                "main",
                true,
                Some("architecture"),
            ),
        )
        .await;
        let _nested_invocation = seed_running_invocation(
            project.path(),
            "kani",
            root_route.clone(),
            identity(
                "kani",
                "main.architecture.kani",
                "main.architecture",
                true,
                Some("kani"),
            ),
        )
        .await;
        let mut worker_route = RoutingInfo::window(tmux.window.clone());
        worker_route.pane_id = Some(worker_pane.clone());
        let _worker_invocation = seed_running_invocation(
            project.path(),
            "worker-codex",
            worker_route,
            identity("worker-codex", "main", "main", false, None),
        )
        .await;
        let service = service_for(project.path(), &tmux.session).await;
        let result = service
            .shutdown_workspace_control_plane()
            .await
            .expect("verified workspace shutdown succeeds");
        assert_eq!(result.status, "complete");
        assert_eq!(result.controllers_interrupted, 3);
        assert_eq!(result.worker_panes_preserved, 1);
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &target.window_id, &target.pane_id)
            .await
            .unwrap()
            .is_none());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
        for agent in ["root", "architecture", "kani"] {
            let current =
                invocation::read_invocation(&project.path().join(".exo/agents").join(agent))
                    .await
                    .unwrap()
                    .unwrap();
            assert_eq!(current.status, InvocationStatus::Killed);
            assert_eq!(
                current.exit_classification.as_deref(),
                Some("workspace_control_plane_shutdown")
            );
        }
        let intent_path = workspace_shutdown_path(project.path());
        let first: WorkspaceShutdownIntent =
            serde_json::from_slice(&tokio::fs::read(&intent_path).await.unwrap()).unwrap();
        assert_eq!(first.phase, WorkspaceShutdownPhase::Complete);
        let repeated = service.shutdown_workspace_control_plane().await.unwrap();
        assert_eq!(repeated.status, "already_complete");
        assert_eq!(repeated.operation_id, result.operation_id);
        let second: WorkspaceShutdownIntent =
            serde_json::from_slice(&tokio::fs::read(&intent_path).await.unwrap()).unwrap();
        assert_eq!(first, second);
    }

    /// Persist the same startup fence that `init` writes before the pending
    /// target. Tests must produce the production ordering, otherwise the
    /// fail-closed fence validation is what they are exercising.
    fn write_startup_fence(project_dir: &Path, pending: &ControllerPaneOwnership) {
        let path = controller_startup_intent_path(project_dir);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let target = ControllerPaneTarget {
            schema_version: pending.schema_version,
            controller_epoch: pending.controller_epoch.clone(),
            tmux_session: pending.tmux_session.clone(),
            window_id: pending.window_id.clone(),
            pane_id: pending.pane_id.clone(),
            pane_pid: pending.pane_pid,
            pane_start_ticks: pending.pane_start_ticks,
            tmux_server_pid: pending.tmux_server_pid,
            tmux_server_start_ticks: pending.tmux_server_start_ticks,
        };
        let intent = serde_json::json!({
            "schema_version": 1,
            "controller_epoch": pending.controller_epoch,
            "previous_controller_epoch": serde_json::Value::Null,
            "tmux_session": pending.tmux_session,
            "tmux_server_pid": pending.tmux_server_pid,
            "tmux_server_start_ticks": pending.tmux_server_start_ticks,
            "continue_mode": true,
            "root_run_id": pending.run_id,
            "target": target,
            "phase": "target_created",
        });
        std::fs::write(path, serde_json::to_vec_pretty(&intent).unwrap()).unwrap();
    }

    async fn seed_pending_startup_target(
        project_dir: &Path,
        committed: &ControllerPaneOwnership,
        epoch: &str,
        run_id: &str,
    ) -> ControllerPaneOwnership {
        tokio::fs::write(
            project_dir.join(".exo/tl-loop/root.controller-epoch"),
            format!("{epoch}\n"),
        )
        .await
        .unwrap();
        let mut pending = committed.clone();
        pending.controller_epoch = epoch.to_owned();
        pending.run_id = run_id.to_owned();
        write_startup_fence(project_dir, &pending);
        persist_pending_controller_ownership(project_dir, &pending)
            .await
            .unwrap();
        pending
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn workspace_shutdown_quiesces_a_valid_pending_startup_target() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        tmux.retain_exited_panes();
        let committed = seed_root_target(project.path(), &tmux).await;
        let root_route = RoutingInfo::window_pane(tmux.window.clone(), committed.pane_id.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            root_route.clone(),
            identity("root", "main", "main", true, Some("root")),
        )
        .await;
        let _child_invocation = seed_running_invocation(
            project.path(),
            "architecture",
            root_route,
            identity(
                "architecture",
                "main.architecture",
                "main",
                true,
                Some("architecture"),
            ),
        )
        .await;
        let mut worker_route = RoutingInfo::window(tmux.window.clone());
        worker_route.pane_id = Some(worker_pane.clone());
        let _worker_invocation = seed_running_invocation(
            project.path(),
            "worker-codex",
            worker_route,
            identity("worker-codex", "main", "main", false, None),
        )
        .await;

        // Init advanced the epoch and persisted the exact pending startup
        // target before child repair. The committed target still names the
        // previous epoch, so selecting it would be stale.
        let pending =
            seed_pending_startup_target(project.path(), &committed, "epoch-2", "run-1").await;
        assert_ne!(pending.controller_epoch, committed.controller_epoch);

        let service = service_for(project.path(), &tmux.session).await;
        let result = service
            .shutdown_workspace_control_plane()
            .await
            .expect("a valid pending startup target can be quiesced");
        assert_eq!(result.status, "complete");
        assert_eq!(result.controllers_interrupted, 2);
        assert_eq!(result.worker_panes_preserved, 1);

        let intent: WorkspaceShutdownIntent = serde_json::from_slice(
            &tokio::fs::read(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(intent.target.controller_epoch, "epoch-2");
        assert_eq!(intent.target.pane_id, pending.pane_id);
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &pending.window_id, &pending.pane_id)
            .await
            .unwrap()
            .is_none());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
        for agent in ["root", "architecture"] {
            let current =
                invocation::read_invocation(&project.path().join(".exo/agents").join(agent))
                    .await
                    .unwrap()
                    .unwrap();
            assert_eq!(current.status, InvocationStatus::Killed);
            assert_eq!(
                current.exit_classification.as_deref(),
                Some("workspace_control_plane_shutdown")
            );
        }
        assert_eq!(
            service
                .shutdown_workspace_control_plane()
                .await
                .unwrap()
                .status,
            "already_complete"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn workspace_shutdown_does_not_prefer_a_pending_target_from_another_scope() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let committed = seed_root_target(project.path(), &tmux).await;
        let root_route = RoutingInfo::window_pane(tmux.window.clone(), committed.pane_id.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            root_route,
            identity("root", "main", "main", true, Some("root")),
        )
        .await;
        // The pending record is live and epoch-current but belongs to another
        // root run, so it cannot authorize stopping this controller.
        seed_pending_startup_target(project.path(), &committed, "epoch-2", "foreign-run").await;

        let service = service_for(project.path(), &tmux.session).await;
        let error = service
            .shutdown_workspace_control_plane()
            .await
            .expect_err("a foreign pending target is not a shutdown owner");
        assert!(
            format!("{error:#}").contains("root controller epoch changed"),
            "unexpected diagnostic: {error:#}"
        );
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &committed.window_id, &committed.pane_id)
            .await
            .unwrap()
            .is_some());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
        assert!(
            !tokio::fs::try_exists(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
            "a refused selection persists no shutdown intent"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn interrupted_shutdown_intent_retargets_to_the_pending_startup() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let _worker_pane = tmux.add_worker_pane();
        tmux.retain_exited_panes();
        let committed = seed_root_target(project.path(), &tmux).await;
        let root_route = RoutingInfo::window_pane(tmux.window.clone(), committed.pane_id.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            root_route,
            identity("root", "main", "main", true, Some("root")),
        )
        .await;
        let mut intent = WorkspaceShutdownIntent {
            schema_version: 1,
            operation_id: "interrupted-operation".to_owned(),
            phase: WorkspaceShutdownPhase::Requested,
            requested_at: unix_timestamp(),
            completed_at: None,
            target: committed.clone(),
            controllers: Vec::new(),
            preserved_worker_panes: 0,
            blocker: None,
        };
        persist_intent(&workspace_shutdown_path(project.path()), &mut intent, None)
            .await
            .unwrap();
        let pending =
            seed_pending_startup_target(project.path(), &committed, "epoch-2", "run-1").await;
        let service = service_for(project.path(), &tmux.session).await;
        let result = service
            .shutdown_workspace_control_plane()
            .await
            .expect("an unfinished shutdown retargets to the exact pending startup");
        assert_eq!(result.status, "complete");
        let final_intent: WorkspaceShutdownIntent = serde_json::from_slice(
            &tokio::fs::read(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(final_intent.operation_id, "interrupted-operation");
        assert_eq!(final_intent.target.controller_epoch, "epoch-2");
        assert_eq!(final_intent.target.pane_id, pending.pane_id);
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &pending.window_id, &pending.pane_id)
            .await
            .unwrap()
            .is_none());
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn completed_shutdown_intent_quiesces_a_later_interrupted_startup() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let _worker_pane = tmux.add_worker_pane();
        let committed = seed_root_target(project.path(), &tmux).await;
        let mut completed = WorkspaceShutdownIntent {
            schema_version: 1,
            operation_id: "completed-old-operation".to_owned(),
            phase: WorkspaceShutdownPhase::Complete,
            requested_at: unix_timestamp(),
            completed_at: Some(unix_timestamp()),
            target: committed.clone(),
            controllers: Vec::new(),
            preserved_worker_panes: 0,
            blocker: None,
        };
        persist_intent(
            &workspace_shutdown_path(project.path()),
            &mut completed,
            None,
        )
        .await
        .unwrap();
        let pending =
            seed_pending_startup_target(project.path(), &committed, "epoch-2", "run-1").await;
        let service = service_for(project.path(), &tmux.session).await;
        let result = service
            .shutdown_workspace_control_plane()
            .await
            .expect("a completed intent still quiesces a newer interrupted startup");
        assert_eq!(result.status, "complete");
        assert_eq!(result.operation_id, "completed-old-operation");
        let final_intent: WorkspaceShutdownIntent = serde_json::from_slice(
            &tokio::fs::read(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(final_intent.target.controller_epoch, "epoch-2");
        assert_eq!(final_intent.target.pane_id, pending.pane_id);
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &pending.window_id, &pending.pane_id)
            .await
            .unwrap()
            .is_none());

        // Without another startup epoch this is now a genuine repeat and must
        // not manufacture a second shutdown operation.
        let repeated = service.shutdown_workspace_control_plane().await.unwrap();
        assert_eq!(repeated.status, "already_complete");
        assert_eq!(repeated.operation_id, "completed-old-operation");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn pending_startup_target_without_a_startup_fence_fails_closed() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let committed = seed_root_target(project.path(), &tmux).await;
        let routing = RoutingInfo::window_pane(tmux.window.clone(), committed.pane_id.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            routing.clone(),
            identity("root", "main", "main", true, Some("root")),
        )
        .await;

        // Simulate a partial/tampered handoff: the pending target exists, but
        // the startup fence that init always writes first does not.
        tokio::fs::write(
            project.path().join(".exo/tl-loop/root.controller-epoch"),
            "epoch-2\n",
        )
        .await
        .unwrap();
        let mut pending = committed.clone();
        pending.controller_epoch = "epoch-2".to_owned();
        persist_pending_controller_ownership(project.path(), &pending)
            .await
            .unwrap();
        assert!(!controller_startup_intent_path(project.path()).exists());

        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(
            pending_startup_target_if_current(project.path(), &tmux_ipc)
                .await
                .unwrap()
                .is_none(),
            "a pending target without its startup fence is not current"
        );
        assert!(
            matches!(
                reconcile_controller_binding(
                    project.path(),
                    &tmux_ipc,
                    &routing,
                    Some(&committed.controller_epoch),
                )
                .await
                .unwrap(),
                ControllerBindingReconciliation::Unverified { .. }
            ),
            "missing fence evidence must fail closed"
        );

        let service = service_for(project.path(), &tmux.session).await;
        let error = service
            .shutdown_workspace_control_plane()
            .await
            .expect_err("shutdown falls back to the stale committed target");
        assert!(
            format!("{error:#}").contains("root controller epoch changed"),
            "unexpected diagnostic: {error:#}"
        );
        assert!(
            !tokio::fs::try_exists(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
            "a refused selection persists no shutdown intent"
        );
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &committed.window_id, &committed.pane_id)
            .await
            .unwrap()
            .is_some());
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn changed_root_target_fails_closed_and_keeps_workers_untouched() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let mut target = seed_root_target(project.path(), &tmux).await;
        target.pane_pid = target.pane_pid.saturating_add(100_000);
        persist_controller_ownership(project.path(), &target)
            .await
            .unwrap();
        let service = service_for(project.path(), &tmux.session).await;
        let error = service
            .shutdown_workspace_control_plane()
            .await
            .expect_err("replacement PID is not the owned controller");
        assert!(error.to_string().contains("pane PID changed"));
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &tmux.window, &tmux.pane)
            .await
            .unwrap()
            .is_some());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
        let intent: WorkspaceShutdownIntent = serde_json::from_slice(
            &tokio::fs::read(workspace_shutdown_path(project.path()))
                .await
                .unwrap(),
        )
        .unwrap();
        assert_eq!(intent.phase, WorkspaceShutdownPhase::Blocked);
        assert!(
            intent
                .blocker
                .as_deref()
                .is_some_and(|blocker| blocker.contains("pane PID changed")),
            "unexpected shutdown blocker: {:?}",
            intent.blocker
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn missing_root_ownership_refuses_without_stopping_any_pane() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        seed_root_target(project.path(), &tmux).await;
        tokio::fs::remove_file(controller_ownership_path(project.path()))
            .await
            .unwrap();
        let service = service_for(project.path(), &tmux.session).await;

        let error = service
            .shutdown_workspace_control_plane()
            .await
            .expect_err("missing root target cannot authorize workspace shutdown");
        assert!(error.to_string().contains("read root controller ownership"));
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &tmux.window, &tmux.pane)
            .await
            .unwrap()
            .is_some());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
        assert!(
            !tokio::fs::try_exists(workspace_shutdown_path(project.path()))
                .await
                .unwrap()
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn stubborn_controller_uses_bounded_exact_pane_fallback() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        tmux.ignore_controller_interrupt();
        let target = seed_root_target(project.path(), &tmux).await;
        let route = RoutingInfo::window(tmux.window.clone());
        let _root_invocation = seed_running_invocation(
            project.path(),
            "root",
            route,
            identity("root", "main", "main", true, Some("root")),
        )
        .await;
        let service = service_for(project.path(), &tmux.session).await;

        let started = Instant::now();
        let result = service
            .shutdown_workspace_control_plane()
            .await
            .expect("bounded exact-pane fallback retires the unresponsive controller");
        let elapsed = started.elapsed();

        assert!(elapsed >= Duration::from_secs(2), "{elapsed:?}");
        assert!(elapsed < Duration::from_secs(5), "{elapsed:?}");
        assert_eq!(result.status, "complete");
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        assert!(tmux_ipc
            .pane_snapshot(&tmux.session, &target.window_id, &target.pane_id)
            .await
            .unwrap()
            .is_none());
        assert!(tmux_ipc.pane_exists(&worker_pane).await.unwrap());
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn dispatch_racing_workspace_shutdown_observes_the_durable_fence() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let _independent_worker_pane = tmux.add_worker_pane();
        seed_root_target(project.path(), &tmux).await;
        let dispatch_service = service_for(project.path(), &tmux.session).await;
        let shutdown_service = service_for(project.path(), &tmux.session).await;
        let permit = dispatch_service
            .acquire_dispatch_permit("hold a competing dispatch lease")
            .await
            .expect("dispatch obtains the shared lifecycle lease first");

        let shutdown =
            tokio::spawn(async move { shutdown_service.shutdown_workspace_control_plane().await });
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(
            !shutdown.is_finished(),
            "shutdown waits behind active dispatch"
        );
        assert!(
            !tokio::fs::try_exists(workspace_shutdown_path(project.path()))
                .await
                .unwrap()
        );
        drop(permit);

        let result = tokio::time::timeout(Duration::from_secs(5), shutdown)
            .await
            .expect("shutdown finishes after the dispatch lease releases")
            .expect("shutdown task joins")
            .expect("workspace shutdown succeeds");
        assert_eq!(result.status, "complete");

        let next_dispatch = service_for(project.path(), &tmux.session).await;
        let error = next_dispatch
            .acquire_dispatch_permit("start another worker")
            .await
            .expect_err("completed workspace shutdown keeps dispatch fenced");
        assert!(error.to_string().contains("shutdown is pending"));
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn server_restart_reattaches_retained_live_worker_monitor() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let route = RoutingInfo::window_pane(tmux.window.clone(), worker_pane.clone());
        let worker = seed_running_invocation(
            project.path(),
            "worker-codex",
            route,
            identity("worker-codex", "main", "main", false, None),
        )
        .await;
        let agent_dir = project.path().join(".exo/agents/worker-codex");
        let service = service_for(project.path(), &tmux.session).await;
        service.resume_invocation_monitors().await;
        assert!(
            crate::services::agent_control::invocation_monitor_registered(
                &agent_dir,
                &worker.invocation_id
            ),
            "a retained live invocation is monitored after a server restart"
        );

        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        tmux_ipc.kill_pane(&worker_pane).await.unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            let current = invocation::read_invocation(&agent_dir)
                .await
                .unwrap()
                .unwrap();
            if !current.is_live() {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "reattached monitor did not retire the exited worker"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while crate::services::agent_control::invocation_monitor_registered(
            &agent_dir,
            &worker.invocation_id,
        ) {
            assert!(
                std::time::Instant::now() < deadline,
                "finished invocation monitor did not deregister"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        service.resume_invocation_monitors().await;
        assert!(
            !crate::services::agent_control::invocation_monitor_registered(
                &agent_dir,
                &worker.invocation_id
            ),
            "a terminal invocation is not reattached"
        );
    }

    async fn read_invocation_bytes(agent_dir: &Path) -> Vec<u8> {
        tokio::fs::read(agent_dir.join("invocation.json"))
            .await
            .unwrap()
    }

    fn captured_lines_containing(
        sink: &Arc<std::sync::Mutex<Vec<String>>>,
        needle: &str,
    ) -> String {
        sink.lock()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .filter(|line| line.contains(needle))
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn server_restart_leaves_a_wrong_owner_invocation_unmonitored() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let route = RoutingInfo::window_pane(tmux.window.clone(), worker_pane.clone());
        let worker = seed_running_invocation(
            project.path(),
            "worker-wrong-owner",
            route.clone(),
            identity("worker-wrong-owner", "main", "main", false, None),
        )
        .await;
        let agent_dir = project.path().join(".exo/agents/worker-wrong-owner");
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        tmux_ipc
            .set_routing_owner(&route, "another-agent", "another-invocation", 7)
            .await
            .unwrap();
        let before = read_invocation_bytes(&agent_dir).await;
        let sink = global_test_event_sink().clone();
        let service = service_for(project.path(), &tmux.session).await;

        service.resume_invocation_monitors().await;

        assert!(
            !crate::services::agent_control::invocation_monitor_registered(
                &agent_dir,
                &worker.invocation_id
            ),
            "an invocation whose pane is owned by another agent stays unmonitored"
        );
        assert_eq!(
            read_invocation_bytes(&agent_dir).await,
            before,
            "an unrevalidatable invocation record is left untouched"
        );
        let logs = captured_lines_containing(&sink, &worker.invocation_id);
        assert!(logs.contains("LEVEL_Level(Warn)"), "{logs}");
        assert!(
            logs.contains(
                "Retained invocation routing could not be revalidated; leaving it unmonitored"
            ),
            "{logs}"
        );
        assert!(logs.contains("ownership conflicts"), "{logs}");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn reconcile_live_invocation_startup_refuses_a_missing_delivery_target() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let route = RoutingInfo::window_pane(tmux.window.clone(), worker_pane.clone());
        let worker = seed_running_invocation(
            project.path(),
            "worker-missing-target",
            route,
            identity("worker-missing-target", "main", "main", false, None),
        )
        .await;
        let agent_dir = project.path().join(".exo/agents/worker-missing-target");
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        tmux_ipc.kill_pane(&worker_pane).await.unwrap();
        let before = read_invocation_bytes(&agent_dir).await;

        let error = invocation::reconcile_live_invocation_startup(
            &agent_dir,
            &worker,
            &worker.routing,
            Some(&tmux_ipc),
            invocation::DeliveryTargetRequirement::MustExist,
        )
        .await
        .expect_err("a recorded delivery target that no longer exists cannot be revalidated");

        assert!(
            format!("{error:#}").contains("no longer exists"),
            "unexpected diagnostic: {error:#}"
        );
        assert_eq!(
            read_invocation_bytes(&agent_dir).await,
            before,
            "a refused reconciliation leaves the invocation record untouched"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn server_restart_leaves_a_missing_target_invocation_unmonitored() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        let route = RoutingInfo::window_pane(tmux.window.clone(), worker_pane.clone());
        let worker = seed_running_invocation(
            project.path(),
            "worker-missing-pane",
            route,
            identity("worker-missing-pane", "main", "main", false, None),
        )
        .await;
        let agent_dir = project.path().join(".exo/agents/worker-missing-pane");
        let tmux_ipc = crate::services::tmux_ipc::TmuxIpc::new(&tmux.session);
        tmux_ipc.kill_pane(&worker_pane).await.unwrap();
        let before = read_invocation_bytes(&agent_dir).await;
        let sink = global_test_event_sink().clone();
        let service = service_for(project.path(), &tmux.session).await;

        service.resume_invocation_monitors().await;

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        while crate::services::agent_control::invocation_monitor_registered(
            &agent_dir,
            &worker.invocation_id,
        ) {
            assert!(
                std::time::Instant::now() < deadline,
                "an invocation whose recorded pane no longer exists stays unmonitored"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(
            invocation::read_invocation(&agent_dir)
                .await
                .unwrap()
                .unwrap()
                .is_live(),
            "a refused reconciliation does not terminalize the retained invocation"
        );
        assert_eq!(
            read_invocation_bytes(&agent_dir).await,
            before,
            "a missing delivery target leaves the invocation record untouched"
        );
        let logs = captured_lines_containing(&sink, &worker.invocation_id);
        assert!(logs.contains("LEVEL_Level(Warn)"), "{logs}");
        assert!(
            logs.contains(
                "Retained invocation routing could not be revalidated; leaving it unmonitored"
            ),
            "{logs}"
        );
        assert!(logs.contains("no longer exists"), "{logs}");
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn server_restart_skips_an_unreadable_invocation_record() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let agent_dir = project.path().join(".exo/agents/worker-malformed");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        let contents = b"{\"invocation_id\":\"malformed-invocation\",\"status\":".to_vec();
        tokio::fs::write(agent_dir.join("invocation.json"), &contents)
            .await
            .unwrap();
        let sink = global_test_event_sink().clone();
        let service = service_for(project.path(), &tmux.session).await;

        service.resume_invocation_monitors().await;

        assert_eq!(
            crate::services::agent_control::invocation_monitor_count(&agent_dir),
            0,
            "an unreadable invocation record is not monitored"
        );
        assert_eq!(
            read_invocation_bytes(&agent_dir).await,
            contents,
            "an unreadable invocation record is left untouched"
        );
        let logs = captured_lines_containing(&sink, "worker-malformed");
        assert!(logs.contains("LEVEL_Level(Warn)"), "{logs}");
        assert!(
            logs.contains("Could not read a retained invocation for monitor recovery"),
            "{logs}"
        );
    }

    #[tokio::test]
    #[serial_test::serial]
    async fn server_restart_skips_monitor_recovery_without_tmux() {
        let project = tempfile::tempdir().unwrap();
        let route = RoutingInfo::window(WindowId::parse("@1").unwrap());
        let _worker = seed_running_invocation(
            project.path(),
            "worker-no-tmux",
            route,
            identity("worker-no-tmux", "main", "main", false, None),
        )
        .await;
        let agent_dir = project.path().join(".exo/agents/worker-no-tmux");
        let before = read_invocation_bytes(&agent_dir).await;
        let sink = global_test_event_sink().clone();
        let mut services = crate::services::Services::test();
        services.project_dir = project.path().to_path_buf();
        services.agent_resolver = Arc::new(AgentResolver::load(project.path().to_path_buf()).await);
        let service = AgentControlService::new(Arc::new(services));

        service.resume_invocation_monitors().await;

        assert_eq!(
            crate::services::agent_control::invocation_monitor_count(&agent_dir),
            0,
            "monitor recovery without a tmux session registers nothing"
        );
        assert_eq!(
            read_invocation_bytes(&agent_dir).await,
            before,
            "monitor recovery without a tmux session leaves the record untouched"
        );
        let logs = captured_lines_containing(
            &sink,
            "Skipping invocation monitor recovery because tmux is not configured",
        );
        assert!(logs.contains("LEVEL_Level(Warn)"), "{logs}");
        assert!(!logs.is_empty(), "the skip must be reported: {logs}");
    }
}
