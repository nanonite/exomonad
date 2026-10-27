//! Explicit workspace control-plane shutdown and its durable handoff record.
//!
//! `exomonad shutdown` remains a server-only operation. This module is used by
//! the opt-in workspace shutdown endpoint and only acts on a root controller
//! pane whose exact owner was recorded by init.

use super::AgentControlService;
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
use uuid::Uuid;

pub const ROOT_CONTROLLER_OWNERSHIP_RELATIVE_PATH: &str =
    ".exo/tl-loop/root/controller-target.json";
pub const WORKSPACE_SHUTDOWN_RELATIVE_PATH: &str =
    ".exo/controller-lifecycle/workspace-shutdown.json";
const WORKSPACE_SHUTDOWN_GRACE: Duration = Duration::from_secs(3);
const WORKSPACE_SHUTDOWN_POLL: Duration = Duration::from_millis(50);

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
        let mut intent = load_or_create_intent(project_dir).await?;
        if intent.phase == WorkspaceShutdownPhase::Complete {
            return Ok(result_for(&intent, "already_complete"));
        }
        persist_intent(&path, &mut intent, None).await?;

        let tmux = self.tmux()?;
        verify_controller_epoch(project_dir, &intent.target).await?;
        let target_state = observe_controller_pane(&tmux, &intent.target).await?;
        if target_state == ControllerPaneState::Live {
            tmux.send_interrupt_to_pane(&intent.target.pane_id).await?;
            let stopped =
                wait_for_controller_pane(&tmux, &intent.target, WORKSPACE_SHUTDOWN_GRACE).await?;
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

    /// A dispatch permit shares the same cross-process mutation lease used by
    /// workspace shutdown. Holding it across tmux creation prevents a process
    /// from starting after shutdown has persisted its fence.
    pub(crate) async fn acquire_dispatch_permit(
        &self,
        decision: &str,
    ) -> Result<crate::services::worktree_lifecycle::LifecycleGuard> {
        let guard = self.acquire_worktree_lifecycle(decision).await?;
        if workspace_shutdown_path(self.project_dir()).exists() {
            anyhow::bail!("workspace control-plane shutdown is pending; run init --continue to reconcile before dispatch");
        }
        Ok(guard)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ControllerPaneState {
    Live,
    Stopped,
}

async fn load_or_create_intent(project_dir: &Path) -> Result<WorkspaceShutdownIntent> {
    let path = workspace_shutdown_path(project_dir);
    match tokio::fs::read(&path).await {
        Ok(bytes) => {
            let intent: WorkspaceShutdownIntent = serde_json::from_slice(&bytes)
                .with_context(|| format!("parse shutdown intent {}", path.display()))?;
            if intent.schema_version != 1 || intent.operation_id.trim().is_empty() {
                anyhow::bail!(
                    "workspace shutdown intent is malformed; startup reconciliation is required"
                );
            }
            Ok(intent)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let target = read_controller_ownership(project_dir).await?;
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
        if !invocation.is_live()
            || invocation.routing.window_id.as_ref().map(WindowId::as_str)
                != Some(target.window_id.as_str())
            || invocation.routing.pane_id.is_some()
        {
            continue;
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
        if invocation.is_live()
            && invocation.routing.window_id.as_ref().map(WindowId::as_str)
                == Some(target.window_id.as_str())
            && invocation.routing.pane_id.is_some()
        {
            count = count.saturating_add(1);
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
    if current.routing.window_id.as_ref() != Some(&target.window_id)
        || current.routing.pane_id.is_some()
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
    use crate::services::agent_control::{
        AgentIdentityRecord, AgentType, InvocationRecord, InvocationStatus, InvocationTrigger,
        Topology,
    };
    use crate::services::agent_resolver::AgentResolver;
    use std::ffi::OsString;
    use std::process::{Command, Stdio};
    use std::sync::Arc;

    struct IsolatedTmux {
        socket: String,
        session: String,
        window: WindowId,
        pane: PaneId,
        previous_socket: Option<OsString>,
    }

    impl IsolatedTmux {
        fn new() -> Self {
            let socket = format!("exo-controller-lifecycle-{}", Uuid::new_v4());
            let session = format!("exo-controller-lifecycle-{}", Uuid::new_v4());
            let previous_socket = std::env::var_os("EXOMONAD_TMUX_SOCKET");
            std::env::set_var("EXOMONAD_TMUX_SOCKET", &socket);
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
                previous_socket,
            }
        }

        fn command(socket: &str) -> Command {
            let mut command = Command::new("tmux");
            command.args(["-L", socket]).stdout(Stdio::piped());
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
    }

    impl Drop for IsolatedTmux {
        fn drop(&mut self) {
            let _ = Self::command(&self.socket).arg("kill-server").status();
            if let Some(previous) = self.previous_socket.take() {
                std::env::set_var("EXOMONAD_TMUX_SOCKET", previous);
            } else {
                std::env::remove_var("EXOMONAD_TMUX_SOCKET");
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
    async fn workspace_shutdown_stops_only_verified_controller_and_is_idempotent() {
        let project = tempfile::tempdir().unwrap();
        let tmux = IsolatedTmux::new();
        let worker_pane = tmux.add_worker_pane();
        tmux.retain_exited_panes();
        let target = seed_root_target(project.path(), &tmux).await;
        let mut root_route = RoutingInfo::window(tmux.window.clone());
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
        root_route.parent_tab = None;

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
        assert_eq!(intent.phase, WorkspaceShutdownPhase::Requested);
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
}
