use crate::domain::Address;
use crate::services::agent_inbox::{InboxMessage, GLOBAL_AGENT_INBOX};
use crate::services::tmux_events;
use agent_client_protocol::{Agent, PromptRequest};
use claude_teams_bridge as teams_mailbox;
use claude_teams_bridge::TeamRegistry;
use exomonad_proto::effects::events::{event, AgentMessage, Event};
use tokio::process::Command;
use tracing::{debug, info, instrument, warn};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryResult {
    Teams,
    Acp,
    Uds,
    Tmux,
    Failed,
}

fn pinned_tmux_target(target: &str) -> String {
    if target.starts_with('%') {
        target.to_string()
    } else {
        format!("{}.0", target)
    }
}

fn routing_tmux_target(routing: &serde_json::Value) -> Option<String> {
    routing["pane_id"]
        .as_str()
        .or_else(|| routing["window_id"].as_str())
        .or_else(|| routing["parent_tab"].as_str())
        .map(|s| s.to_string())
}

fn agent_type_from_key(agent_key: &str) -> crate::services::AgentType {
    let slug = agent_key
        .rsplit_once('.')
        .map(|(_, s)| s)
        .unwrap_or(agent_key);
    crate::services::AgentType::from_dir_name(slug)
}

fn supports_teams_inbox(agent_type: crate::services::AgentType) -> bool {
    matches!(agent_type, crate::services::AgentType::Claude)
}

fn should_try_acp(agent_type: crate::services::AgentType) -> bool {
    !matches!(agent_type, crate::services::AgentType::OpenCode)
}

fn tmux_injection_options(agent_type: crate::services::AgentType) -> tmux_events::InjectionOptions {
    if matches!(agent_type, crate::services::AgentType::Claude) {
        tmux_events::InjectionOptions::claude_default()
    } else {
        tmux_events::InjectionOptions::inline_submit()
    }
}

fn worker_gone_detail(agent_key: &str, target: &str) -> String {
    format!("[WORKER GONE: {agent_key}] routing target {target} is not alive")
}

async fn mark_agent_exited(agent_dir: &std::path::Path) {
    let exited_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
        .to_string();
    if let Err(error) = tokio::fs::write(agent_dir.join("exited_at"), exited_at).await {
        warn!(path = %agent_dir.display(), %error, "failed to write agent exited_at tombstone");
    }
    match tokio::fs::remove_file(agent_dir.join("routing.json")).await {
        Ok(()) => info!(path = %agent_dir.display(), "removed stale agent routing"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            warn!(path = %agent_dir.display(), %error, "failed to remove stale agent routing")
        }
    }
}

async fn tmux_target_alive(target: &str) -> Result<bool, String> {
    let session = std::env::var("EXOMONAD_TMUX_SESSION")
        .map_err(|_| "EXOMONAD_TMUX_SESSION is not set".to_string())?;
    if session.trim().is_empty() {
        return Err("EXOMONAD_TMUX_SESSION is empty".to_string());
    }
    let qualified_target = crate::services::tmux_ipc::qualify_tmux_target(&session, target);
    let output = Command::new("tmux")
        .args([
            "display-message",
            "-p",
            "-t",
            &qualified_target,
            "#{pane_id}",
        ])
        .output()
        .await
        .map_err(|error| error.to_string())?;
    Ok(output.status.success() && !String::from_utf8_lossy(&output.stdout).trim().is_empty())
}

async fn routing_target_alive_or_cleanup(
    project_dir: &std::path::Path,
    agent_dir_name: &str,
    target: &str,
    agent_key: &str,
    from: &crate::domain::AgentName,
) -> bool {
    match tmux_target_alive(target).await {
        Ok(true) => true,
        Ok(false) => {
            let agent_dir = project_dir.join(".exo/agents").join(agent_dir_name);
            mark_agent_exited(&agent_dir).await;
            let detail = worker_gone_detail(agent_key, target);
            tracing::info!(
                otel.name = "message.delivery",
                agent_id = %from,
                recipient = %agent_key,
                method = "tmux_routing",
                outcome = "failed",
                detail = %detail,
                "[event] message.delivery"
            );
            false
        }
        Err(error) => {
            warn!(agent = %agent_key, target, %error, "could not verify tmux routing target liveness");
            false
        }
    }
}

/// Notification status for parent-facing messages.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotifyStatus {
    Success,
    Failure,
    Stuck,
}

impl NotifyStatus {
    /// Parse from proto/wire string ("failure" → Failure, "stuck" → Stuck, anything else → Success).
    pub fn parse(s: &str) -> Self {
        match s {
            "failure" => NotifyStatus::Failure,
            "stuck" => NotifyStatus::Stuck,
            _ => NotifyStatus::Success,
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            NotifyStatus::Success => "success",
            NotifyStatus::Failure => "failure",
            NotifyStatus::Stuck => "stuck",
        }
    }
}

impl std::fmt::Display for NotifyStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Format a parent-facing notification message.
/// Failure → `[FAILED: {id}] {msg}`, Stuck → `[STUCK: {id}] {msg}`, otherwise → `[from: {id}] {msg}`.
pub fn format_parent_notification(
    agent_id: &crate::domain::AgentName,
    status: NotifyStatus,
    message: &str,
) -> String {
    let default_msg = match status {
        NotifyStatus::Failure => "Task failed.",
        NotifyStatus::Stuck => "Review did not converge. Human intervention required.",
        NotifyStatus::Success => "Status update.",
    };
    let msg = if message.is_empty() {
        default_msg
    } else {
        message
    };
    match status {
        NotifyStatus::Failure => format!("[FAILED: {}] {}", agent_id, msg),
        NotifyStatus::Stuck => format!("[STUCK: {}] {}", agent_id, msg),
        NotifyStatus::Success => format!("[from: {}] {}", agent_id, msg),
    }
}

/// Delivery method used for message routing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryMethod {
    TeamsInbox,
    Acp,
    Uds,
    Tmux,
}

/// Outcome of a routed message delivery.
#[derive(Debug)]
pub enum DeliveryOutcome {
    /// Successfully delivered to the resolved recipient.
    Delivered {
        method: DeliveryMethod,
        recipient: crate::domain::AgentName,
    },
    /// Original target could not be resolved; fell back to team lead.
    FallbackToLead {
        method: DeliveryMethod,
        original: String,
        lead: crate::domain::AgentName,
    },
    /// Delivery failed entirely.
    Failed { original: String, reason: String },
}

impl DeliveryOutcome {
    fn from_result(result: DeliveryResult, recipient: &str) -> Self {
        let agent = crate::domain::AgentName::try_from_str(recipient)
            .expect("validated string input is non-empty");
        match result {
            DeliveryResult::Failed => DeliveryOutcome::Failed {
                original: recipient.to_string(),
                reason: "all delivery methods failed".to_string(),
            },
            DeliveryResult::Teams => DeliveryOutcome::Delivered {
                method: DeliveryMethod::TeamsInbox,
                recipient: agent,
            },
            DeliveryResult::Acp => DeliveryOutcome::Delivered {
                method: DeliveryMethod::Acp,
                recipient: agent,
            },
            DeliveryResult::Uds => DeliveryOutcome::Delivered {
                method: DeliveryMethod::Uds,
                recipient: agent,
            },
            DeliveryResult::Tmux => DeliveryOutcome::Delivered {
                method: DeliveryMethod::Tmux,
                recipient: agent,
            },
        }
    }

    /// Whether delivery succeeded (including fallback).
    pub fn is_success(&self) -> bool {
        matches!(
            self,
            DeliveryOutcome::Delivered { .. } | DeliveryOutcome::FallbackToLead { .. }
        )
    }

    /// The delivery method string for proto response.
    pub fn method_string(&self) -> &str {
        match self {
            DeliveryOutcome::Delivered { method, .. }
            | DeliveryOutcome::FallbackToLead { method, .. } => match method {
                DeliveryMethod::TeamsInbox => "teams_inbox",
                DeliveryMethod::Acp => "acp",
                DeliveryMethod::Uds => "unix_socket",
                DeliveryMethod::Tmux => "tmux_stdin",
            },
            DeliveryOutcome::Failed { .. } => "failed",
        }
    }
}

const MAILBOX_PROTOCOL_AVAILABLE_ENV: &str = "EXOMONAD_MAILBOX_PROTOCOL_AVAILABLE";

pub const MAILBOX_PROTOCOL_UNAVAILABLE_MESSAGE: &str = "Mailbox protocol not available in this session: Teams inbox is not configured or has not passed e2e validation for this role/runtime combination.";

#[derive(Clone, Copy)]
enum MessageDeliveryPath {
    Smart,
    TmuxOnly,
    MailboxOnly,
}

pub fn mailbox_protocol_available() -> bool {
    matches!(
        std::env::var(MAILBOX_PROTOCOL_AVAILABLE_ENV).as_deref(),
        Ok("1") | Ok("true") | Ok("yes")
    )
}

/// Route a message to a typed Address using the default delivery fallback chain.
///
/// Resolves the Address to a concrete agent key and tab name, then delegates
/// to `deliver_to_agent()`. For `Address::Team` with no member, resolves the
/// team lead from the TeamRegistry.
#[instrument(skip_all, fields(address = %address, from = %from))]
pub async fn route_message(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
) -> DeliveryOutcome {
    route_message_with(
        ctx,
        address,
        from,
        content,
        summary,
        MessageDeliveryPath::Smart,
    )
    .await
}

/// Route a message only through tmux STDIN injection.
#[instrument(skip_all, fields(address = %address, from = %from))]
pub async fn route_tmux_message(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
) -> DeliveryOutcome {
    route_message_with(
        ctx,
        address,
        from,
        content,
        summary,
        MessageDeliveryPath::TmuxOnly,
    )
    .await
}

/// Route a message only through the Claude Teams inbox mailbox protocol.
#[instrument(skip_all, fields(address = %address, from = %from))]
pub async fn route_mailbox_message(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
) -> DeliveryOutcome {
    route_message_with(
        ctx,
        address,
        from,
        content,
        summary,
        MessageDeliveryPath::MailboxOnly,
    )
    .await
}

async fn route_message_with(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
    path: MessageDeliveryPath,
) -> DeliveryOutcome {
    match address {
        Address::Agent(name) => {
            let tab_name = resolve_tab_name_for_agent(name, Some(ctx.agent_resolver()));
            let agent_key = name.as_str();
            let result =
                deliver_to_agent_for(ctx, agent_key, &tab_name, from, content, summary, path).await;
            DeliveryOutcome::from_result(result, agent_key)
        }
        Address::Team { team, member } => {
            if let Some(member_name) = member {
                let tab_name = resolve_tab_name_for_agent(member_name, Some(ctx.agent_resolver()));
                let agent_key = member_name.as_str();
                let result =
                    deliver_to_agent_for(ctx, agent_key, &tab_name, from, content, summary, path)
                        .await;
                DeliveryOutcome::from_result(result, agent_key)
            } else {
                resolve_and_deliver_to_lead(ctx, team.as_str(), from, content, summary, path).await
            }
        }
        Address::Supervisor => {
            let result =
                deliver_to_agent_for(ctx, "root", "TL", from, content, summary, path).await;
            DeliveryOutcome::from_result(result, "root")
        }
    }
}

async fn deliver_to_agent_for(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
    path: MessageDeliveryPath,
) -> DeliveryResult {
    match path {
        MessageDeliveryPath::Smart => {
            deliver_to_agent(ctx, agent_key, tmux_target, from, message, summary).await
        }
        MessageDeliveryPath::TmuxOnly => {
            deliver_via_tmux(ctx.project_dir(), agent_key, tmux_target, from, message).await
        }
        MessageDeliveryPath::MailboxOnly => {
            deliver_to_agent_mailbox(ctx, agent_key, from, message, summary).await
        }
    }
}

/// Resolve team lead and deliver. Uses `config.json`'s `leadAgentId` to find
/// the lead, falls back to first in-memory entry, then to "root".
async fn resolve_and_deliver_to_lead(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    team_name: &str,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
    path: MessageDeliveryPath,
) -> DeliveryOutcome {
    let original = format!("team:{}:lead", team_name);

    let lead_key = ctx
        .team_registry()
        .resolve_lead(team_name)
        .await
        .unwrap_or_else(|| "root".to_string());

    info!(
        team = %team_name,
        lead = %lead_key,
        "Resolved team lead for delivery"
    );

    let lead_agent = crate::domain::AgentName::try_from_str(lead_key.as_str())
        .expect("validated string input is non-empty");
    let tab_name = resolve_tab_name_for_agent(&lead_agent, Some(ctx.agent_resolver()));
    let result =
        deliver_to_agent_for(ctx, &lead_key, &tab_name, from, content, summary, path).await;

    match result {
        DeliveryResult::Failed => DeliveryOutcome::Failed {
            original,
            reason: format!("delivery to resolved lead '{}' failed", lead_key),
        },
        _ => DeliveryOutcome::FallbackToLead {
            method: delivery_method_from_result(result),
            original,
            lead: crate::domain::AgentName::try_from_str(lead_key.as_str())
                .expect("validated string input is non-empty"),
        },
    }
}

fn delivery_method_from_result(result: DeliveryResult) -> DeliveryMethod {
    match result {
        DeliveryResult::Teams => DeliveryMethod::TeamsInbox,
        DeliveryResult::Acp => DeliveryMethod::Acp,
        DeliveryResult::Uds => DeliveryMethod::Uds,
        DeliveryResult::Tmux | DeliveryResult::Failed => DeliveryMethod::Tmux,
    }
}

/// Resolve the tmux window/display name for an agent.
///
/// Primary path: `AgentResolver` lookup (pre-computed `display_name`).
/// Derivation fallback: for agents not in the resolver (CC-native teammates
/// that were never spawned via exomonad and thus never registered).
pub fn resolve_tab_name_for_agent(
    agent_key: &crate::domain::AgentName,
    resolver: Option<&super::agent_resolver::AgentResolver>,
) -> String {
    if agent_key.as_str() == "root" {
        return "TL".to_string();
    }

    if let Some(resolver) = resolver {
        if let Ok(records) = resolver.records_ref().try_read() {
            if let Some(record) = records.get(agent_key) {
                return record.display_name.clone();
            }
        }
    }

    // A bare birth-branch like "main" has no recognized agent type suffix.
    // from_internal_name defaults to Gemini when no suffix matches, so
    // cross-checking distinguishes a bare branch from an actual Gemini agent.
    // Bare branches are always the root TL's birth-branch → window is "TL".
    let derived_type = crate::services::agent_control::AgentType::from_dir_name(agent_key.as_str());
    if matches!(
        derived_type,
        crate::services::agent_control::AgentType::Gemini
    ) && !agent_key.as_str().ends_with("-gemini")
    {
        return "TL".to_string();
    }

    let identity =
        crate::services::agent_control::AgentIdentity::from_internal_name(agent_key.as_str());
    identity.display_name()
}

/// Notify a parent agent. Single codepath for all parent notifications.
///
/// Pipeline: event log → EventQueue → format `[from: id]`/`[FAILED: id]` → deliver_to_agent.
/// Used by both `EventHandler::notify_parent` (agent-initiated) and the poller's
/// `NotifyParentAction` (system-initiated via event handlers).
///
/// All messages are prefixed with `[from: id]` (or `[FAILED: id]` for failures).
/// Event handler messages include their own structural tags (e.g. `[PR READY]`)
/// inside the message body, so the TL sees: `[from: leaf-id] [PR READY] PR #5...`
///
/// For peer-to-peer messaging, use `deliver_to_agent()` directly instead.
#[allow(clippy::too_many_arguments)]
#[instrument(skip_all, fields(agent_id = %agent_id, parent_session_id = %parent_session_id, status = %status))]
pub async fn notify_parent_delivery(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasEventLog
          + super::HasEventQueue
          + super::HasProjectDir),
    agent_id: &crate::domain::AgentName,
    parent_session_id: &str,
    parent_tab_name: &str,
    status: NotifyStatus,
    message: &str,
    summary: Option<&str>,
    source: &str,
) -> DeliveryResult {
    // 1. Log OTel event + JSONL
    tracing::info!(
        otel.name = "agent.notify_parent",
        parent = %parent_session_id,
        status = %status,
        source = %source,
        "[event] agent.notify_parent"
    );
    if let Some(log) = ctx.event_log() {
        let _ = log.append(
            "agent.notify_parent",
            agent_id.as_str(),
            &serde_json::json!({
                "parent": parent_session_id,
                "status": status.as_str(),
                "message": message,
                "source": source,
            }),
        );
    }

    // 2. Publish to event queue
    let event = Event {
        event_id: 0,
        event_type: Some(event::EventType::AgentMessage(AgentMessage {
            agent_id: agent_id.to_string(),
            status: status.to_string(),
            message: message.to_string(),
            changes: Vec::new(),
        })),
    };
    ctx.event_queue()
        .notify_event(parent_session_id, event)
        .await;

    // 3. Format and deliver
    let notification = format_parent_notification(agent_id, status, message);
    let default_summary = format!("Agent update: {}", agent_id);
    let summary = summary.unwrap_or(&default_summary);

    let delivery_result = deliver_to_agent(
        ctx,
        parent_session_id,
        parent_tab_name,
        agent_id,
        &notification,
        summary,
    )
    .await;

    delivery_result
}

/// Deliver a notification via HTTP POST over a Unix domain socket.
/// Fire-and-forget with 5s timeout.
async fn spawn_inbox_consumer(agent: String) {
    tokio::spawn(async move {
        loop {
            let Some(message) = GLOBAL_AGENT_INBOX.begin_delivery(&agent).await else {
                return;
            };

            let result = tmux_events::inject_input_with_options(
                &message.target,
                &message.body,
                &message.project_dir,
                message.injection_options,
            )
            .await;
            let success = result.is_ok();
            if let Err(e) = result {
                warn!(
                    target = %message.target,
                    recipient = %message.recipient,
                    error = %e,
                    "agent inbox delivery failed; message remains queued for retry"
                );
            }

            tracing::info!(
                otel.name = "message.delivery",
                agent_id = %message.from,
                recipient = %message.recipient,
                method = "agent_inbox_tmux",
                outcome = if success { "success" } else { "failed" },
                detail = %message.detail,
                "[event] message.delivery"
            );

            GLOBAL_AGENT_INBOX
                .complete_delivery(&agent, message.id, success)
                .await;

            if !success {
                return;
            }
        }
    });
}

async fn enqueue_tmux_delivery(
    agent_key: &str,
    target: &str,
    effective_pd: std::path::PathBuf,
    from: &crate::domain::AgentName,
    message: &str,
    detail: &str,
) -> DeliveryResult {
    let inbox_message = InboxMessage::new(
        target.to_string(),
        effective_pd,
        from.as_str().to_string(),
        agent_key.to_string(),
        message.to_string(),
        detail.to_string(),
    )
    .with_injection_options(tmux_injection_options(agent_type_from_key(agent_key)));

    match GLOBAL_AGENT_INBOX.enqueue(agent_key, inbox_message).await {
        Ok(outcome) => {
            if outcome.warning_emitted {
                warn!(
                    agent = %agent_key,
                    depth = outcome.depth,
                    "agent inbox queue depth warning"
                );
            }
            if outcome.should_start_consumer {
                spawn_inbox_consumer(agent_key.to_string()).await;
            }
            DeliveryResult::Tmux
        }
        Err(e) => {
            warn!(agent = %agent_key, error = %e, "agent inbox enqueue failed");
            DeliveryResult::Failed
        }
    }
}

async fn deliver_via_uds(
    socket_path: &std::path::Path,
    from: &str,
    message: &str,
    summary: &str,
) -> Result<(), String> {
    use http_body_util::{BodyExt, Full};
    use hyper::Request;
    use hyper_util::rt::TokioIo;
    use std::time::Duration;
    use tokio::net::UnixStream;

    let body = serde_json::json!({
        "from": from,
        "message": message,
        "summary": summary,
    });
    let body_bytes = serde_json::to_vec(&body).map_err(|e| e.to_string())?;

    let result = tokio::time::timeout(Duration::from_secs(5), async {
        let stream = UnixStream::connect(socket_path)
            .await
            .map_err(|e| e.to_string())?;
        let io = TokioIo::new(stream);

        let (mut sender, conn) = hyper::client::conn::http1::handshake(io)
            .await
            .map_err(|e| e.to_string())?;

        tokio::spawn(async move {
            let _ = conn.await;
        });

        let req = Request::post("/notify")
            .header("host", "localhost")
            .header("content-type", "application/json")
            .body(Full::new(hyper::body::Bytes::from(body_bytes)))
            .map_err(|e| e.to_string())?;

        let resp = sender.send_request(req).await.map_err(|e| e.to_string())?;

        let status = resp.status();
        if status.is_success() {
            Ok(())
        } else {
            let body_bytes = resp
                .into_body()
                .collect()
                .await
                .map_err(|e| e.to_string())?
                .to_bytes();
            Err(format!(
                "UDS server responded: {} - {}",
                status,
                String::from_utf8_lossy(&body_bytes)
                    .lines()
                    .next()
                    .unwrap_or("empty")
            ))
        }
    })
    .await;

    match result {
        Ok(inner) => inner,
        Err(_) => Err("UDS delivery timed out after 5s".to_string()),
    }
}

/// Deliver via tmux STDIN injection (routing.json lookup + fallback to tmux_target).
/// Used as primary path for OpenCode agents and as fallback for others.
async fn deliver_via_tmux(
    project_dir: &std::path::Path,
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
) -> DeliveryResult {
    let slug = agent_key
        .rsplit_once('.')
        .map(|(_, s)| s)
        .unwrap_or(agent_key);
    let agents_dir = project_dir.join(".exo/agents");
    // Try the bare slug directly (handles birth-branch keys like "main.root-tl-opencode"
    // where the directory is "root-tl-opencode"), then with type suffixes.
    let routing_candidates = std::iter::once(agent_key.to_string())
        .chain(std::iter::once(slug.to_string()))
        .chain(
            ["gemini", "claude", "shoal", "opencode", "codex"]
                .iter()
                .flat_map(|suffix| {
                    [
                        format!("{}-{}", slug, suffix),
                        format!("{}-{}", agent_key, suffix),
                    ]
                }),
        );

    let mut routing_target = None;
    let mut routing_parent_tab = None;
    let mut matched_dir_name = None;
    for dir_name in routing_candidates {
        let path = agents_dir.join(&dir_name).join("routing.json");
        if let Ok(content) = tokio::fs::read_to_string(&path).await {
            if let Ok(routing) = serde_json::from_str::<serde_json::Value>(&content) {
                let target = routing_tmux_target(&routing);

                if let Some(t) = target {
                    routing_target = Some(t);
                    routing_parent_tab = routing["parent_tab"].as_str().map(|s| s.to_string());
                    matched_dir_name = Some(dir_name.clone());
                    break;
                }
            }
        }
    }

    if let Some(target) = routing_target {
        let Some(ref dir_name) = matched_dir_name else {
            return DeliveryResult::Failed;
        };
        if !routing_target_alive_or_cleanup(project_dir, dir_name, &target, agent_key, from).await {
            return DeliveryResult::Failed;
        }

        tracing::Span::current().record("delivery_method", "tmux");
        info!(
            agent = %agent_key,
            target = %target,
            chars = message.len(),
            "Injecting message via routing.json"
        );
        let worktree = if let Some(ref parent_tab) = routing_parent_tab {
            crate::services::resolve_worktree_from_tab(parent_tab)
        } else if let Some(ref dir_name) = matched_dir_name {
            let wt_path = project_dir.join(".exo/worktrees").join(dir_name);
            if wt_path.exists() {
                std::path::PathBuf::from(format!(".exo/worktrees/{}/", dir_name))
            } else {
                crate::services::resolve_working_dir(agent_key)
            }
        } else {
            crate::services::resolve_working_dir(agent_key)
        };
        let effective_pd = project_dir.join(worktree);
        // Pin to pane 0 when target is a window name/ID (not a %N pane ID) so
        // injection reaches the TL's main pane rather than an active worker pane.
        let pinned_target = pinned_tmux_target(&target);
        return enqueue_tmux_delivery(
            agent_key,
            &pinned_target,
            effective_pd,
            from,
            message,
            &target,
        )
        .await;
    }

    tracing::Span::current().record("delivery_method", "tmux");
    debug!(
        target = %tmux_target,
        agent = %agent_key,
        chars = message.len(),
        "Injecting message into agent pane via tmux"
    );
    let worktree = if tmux_target == "TL" {
        std::path::PathBuf::from(".")
    } else {
        crate::services::resolve_worktree_from_tab(tmux_target)
    };
    let effective_pd = project_dir.join(worktree);
    // When the target is a window name (not a %N pane ID), append ".0" to pin
    // injection to the first pane. Without this, tmux sends to the active pane,
    // which may be a worker pane rather than the TL's main pane.
    let pinned_target = pinned_tmux_target(tmux_target);
    enqueue_tmux_delivery(
        agent_key,
        &pinned_target,
        effective_pd,
        from,
        message,
        tmux_target,
    )
    .await
}

async fn deliver_to_agent_mailbox(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    agent_key: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
) -> DeliveryResult {
    let agent_type = agent_type_from_key(agent_key);
    if !supports_teams_inbox(agent_type) {
        tracing::info!(
            otel.name = "message.delivery",
            agent_id = %from,
            recipient = %agent_key,
            method = "teams_inbox",
            outcome = "failed",
            detail = "recipient runtime does not support Teams inbox",
            "[event] message.delivery"
        );
        return DeliveryResult::Failed;
    }

    let team_registry = ctx.team_registry();
    let (sender_info, recipient_info) = team_registry.get_pair(from.as_str(), agent_key).await;
    let sender_team = sender_info.map(|info| info.team_name);
    let resolved = recipient_info.or_else(|| {
        sender_team
            .as_deref()
            .and_then(|team| TeamRegistry::resolve_from_config(team, agent_key))
    });
    let Some(team_info) = resolved else {
        tracing::info!(
            otel.name = "message.delivery",
            agent_id = %from,
            recipient = %agent_key,
            method = "teams_inbox",
            outcome = "failed",
            detail = "recipient is not registered in Teams inbox",
            "[event] message.delivery"
        );
        return DeliveryResult::Failed;
    };

    let team_name_ref = &team_info.team_name;
    let inbox_name_ref = &team_info.inbox_name;
    let inbox_policy = super::resilience::RetryPolicy::new(
        3,
        super::resilience::Backoff::Fixed(std::time::Duration::from_millis(100)),
    );
    let result = super::resilience::retry(&inbox_policy, || async {
        teams_mailbox::write_to_inbox(
            team_name_ref,
            inbox_name_ref,
            from.as_str(),
            message,
            summary,
        )
        .map_err(|e| anyhow::anyhow!("{}", e))
    })
    .await;

    match result {
        Ok(timestamp) => {
            tracing::Span::current().record("delivery_method", "teams");
            info!(
                agent = %agent_key,
                team = %team_info.team_name,
                inbox = %team_info.inbox_name,
                timestamp = %timestamp,
                "Wrote message to Teams inbox without fallback"
            );
            tracing::info!(
                otel.name = "message.delivery",
                agent_id = %from,
                recipient = %agent_key,
                method = "teams_inbox",
                outcome = "success",
                detail = format!("{}/{}", team_info.team_name, team_info.inbox_name),
                "[event] message.delivery"
            );
            DeliveryResult::Teams
        }
        Err(e) => {
            warn!(
                agent = %agent_key,
                error = %e,
                "Teams inbox write failed after 3 attempts; mailbox-only delivery has no fallback"
            );
            tracing::info!(
                otel.name = "message.delivery",
                agent_id = %from,
                recipient = %agent_key,
                method = "teams_inbox",
                outcome = "failed",
                detail = %e,
                "[event] message.delivery"
            );
            DeliveryResult::Failed
        }
    }
}

/// Deliver a message to an agent.
///
/// For OpenCode agents with ACP: HTTP POST to ACP server.
/// Tries Teams inbox delivery if a registry and agent key are provided.
/// Attempts ACP prompt delivery if a registry is provided and agent is registered.
/// Attempts HTTP-over-UDS delivery for custom binary agents (e.g., shoal-agent).
/// Falls back to tmux input injection if other delivery methods fail or are not available.
#[instrument(skip_all, fields(agent_key = %agent_key, from = %from, delivery_method = tracing::field::Empty))]
pub async fn deliver_to_agent(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAcpRegistry
          + super::HasAgentResolver
          + super::HasProjectDir),
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
) -> DeliveryResult {
    let team_registry = ctx.team_registry();
    let acp_registry = ctx.acp_registry();
    let _agent_resolver = ctx.agent_resolver();
    let project_dir = ctx.project_dir();
    let agent_type = agent_type_from_key(agent_key);

    // Batch lookup: sender's team (for Tier 2 scoping) + recipient in-memory check.
    // Single lock acquisition instead of two separate get() calls.
    let (sender_info, recipient_info) = team_registry.get_pair(from.as_str(), agent_key).await;
    let sender_team = sender_info.map(|info| info.team_name);
    // Track whether this is a Tier 1 (in-memory) resolution — CC-native agents
    // (Tier 2, config.json) don't have worktrees or routing.json, so the
    // verifier's tmux fallback should be skipped for them.
    let is_in_memory = recipient_info.is_some();
    // Use in-memory result directly, or fall back to Tier 2 (config.json scan)
    let resolved = recipient_info.or_else(|| {
        sender_team
            .as_deref()
            .and_then(|team| TeamRegistry::resolve_from_config(team, agent_key))
    });
    if supports_teams_inbox(agent_type) {
        if let Some(team_info) = resolved {
            // Retry inbox writes up to 3 times before falling back
            let team_name_ref = &team_info.team_name;
            let inbox_name_ref = &team_info.inbox_name;
            let inbox_policy = super::resilience::RetryPolicy::new(
                3,
                super::resilience::Backoff::Fixed(std::time::Duration::from_millis(100)),
            );
            let teams_result = super::resilience::retry(&inbox_policy, || async {
                teams_mailbox::write_to_inbox(
                    team_name_ref,
                    inbox_name_ref,
                    from.as_str(),
                    message,
                    summary,
                )
                .map_err(|e| anyhow::anyhow!("{}", e))
            })
            .await;
            let teams_result = match teams_result {
                Ok(timestamp) => Some(timestamp),
                Err(e) => {
                    warn!(
                        agent = %agent_key,
                        error = %e,
                        "Teams inbox write failed after 3 attempts, falling back to ACP/tmux"
                    );
                    tracing::info!(
                        otel.name = "message.delivery",
                        agent_id = %from,
                        recipient = %agent_key,
                        method = "teams_inbox",
                        outcome = "failed",
                        detail = %e,
                        "[event] message.delivery"
                    );
                    None
                }
            };

            if let Some(timestamp) = teams_result {
                tracing::Span::current().record("delivery_method", "teams");
                info!(
                    agent = %agent_key,
                    team = %team_info.team_name,
                    inbox = %team_info.inbox_name,
                    timestamp = %timestamp,
                    "Wrote message to Teams inbox, spawning delivery verifier (30s)"
                );

                tracing::info!(
                    otel.name = "message.delivery",
                    agent_id = %from,
                    recipient = %agent_key,
                    method = "teams_inbox",
                    outcome = "success",
                    detail = format!("{}/{}", team_info.team_name, team_info.inbox_name),
                    "[event] message.delivery"
                );

                // Spawn background task to verify CC's InboxPoller read the message.
                // If not read within 30s, fall back to tmux STDIN injection.
                // For Tier 2 (CC-native) recipients, skip tmux fallback — they don't
                // have exomonad worktrees or routing.json. CC's InboxPoller owns delivery.
                let team_name = team_info.team_name.clone();
                let inbox_name = team_info.inbox_name.clone();
                let agent = agent_key.to_string();
                let target = tmux_target.to_string();
                let msg = message.to_string();
                let has_tmux_fallback = is_in_memory;
                let worktree = if agent_key.contains('.') {
                    crate::services::resolve_working_dir(agent_key)
                } else if tmux_target == "TL" {
                    std::path::PathBuf::from(".")
                } else {
                    crate::services::resolve_worktree_from_tab(tmux_target)
                };
                let pd = project_dir.join(worktree);
                tokio::spawn(async move {
                    let verify_policy = crate::services::resilience::RetryPolicy::new(
                        3,
                        crate::services::resilience::Backoff::Fixed(
                            std::time::Duration::from_secs(10),
                        ),
                    );
                    let verified = crate::services::resilience::retry(&verify_policy, || {
                        let is_read =
                            teams_mailbox::is_message_read(&team_name, &inbox_name, &timestamp);
                        info!(
                            agent = %agent,
                            team = %team_name,
                            inbox = %inbox_name,
                            timestamp = %timestamp,
                            is_read,
                            "Delivery verifier poll"
                        );
                        async move {
                            if is_read {
                                Ok(())
                            } else {
                                anyhow::bail!("message not yet read")
                            }
                        }
                    })
                    .await;
                    if verified.is_ok() {
                        return;
                    }
                    if !has_tmux_fallback {
                        warn!(
                            agent = %agent,
                            team = %team_name,
                            "Teams inbox message not read after 30s (Tier 2 recipient, no tmux fallback)"
                        );
                        return;
                    }
                    warn!(
                        agent = %agent,
                        team = %team_name,
                        target = %target,
                        "Teams inbox message not read after 30s, falling back to agent inbox"
                    );
                    let fallback_sender = crate::domain::AgentName::try_from_str("teams-fallback")
                        .expect("literal validated string is non-empty");
                    let _ =
                        enqueue_tmux_delivery(&agent, &target, pd, &fallback_sender, &msg, &target)
                            .await;
                });
                return DeliveryResult::Teams;
            }
        }
    } else if resolved.is_some() {
        debug!(
            agent = %agent_key,
            runtime = ?agent_type,
            "Skipping Teams inbox for non-Claude runtime; falling back gracefully"
        );
    }

    if should_try_acp(agent_type) {
        if let Some(conn) = acp_registry.get(agent_key).await {
            match conn
                .conn
                .prompt(PromptRequest::new(
                    conn.session_id.clone(),
                    // ACP prompt content can be multiple messages, but we deliver one-at-a-time here.
                    vec![message.into()],
                ))
                .await
            {
                Ok(_) => {
                    tracing::Span::current().record("delivery_method", "acp");
                    info!(agent = %agent_key, "Delivered message via ACP prompt");
                    tracing::info!(
                        otel.name = "message.delivery",
                        agent_id = %from,
                        recipient = %agent_key,
                        method = "acp",
                        outcome = "success",
                        detail = %conn.session_id,
                        "[event] message.delivery"
                    );
                    return DeliveryResult::Acp;
                }
                Err(e) => {
                    warn!(
                        agent = %agent_key,
                        error = ?e,
                        "ACP prompt failed, falling back to tmux"
                    );
                    tracing::info!(
                        otel.name = "message.delivery",
                        agent_id = %from,
                        recipient = %agent_key,
                        method = "acp",
                        outcome = "failed",
                        detail = ?e,
                        "[event] message.delivery"
                    );
                }
            }
        }
    } else {
        debug!(
            agent = %agent_key,
            runtime = ?agent_type,
            "Skipping ACP for runtime with unsupported prompt integration; falling back to inbox-backed tmux"
        );
    }

    // Try HTTP-over-UDS delivery (for custom binary agents like shoal-agent)
    let socket_path = project_dir.join(format!(".exo/agents/{}/notify.sock", agent_key));
    if socket_path.exists() {
        match deliver_via_uds(&socket_path, from.as_str(), message, summary).await {
            Ok(()) => {
                tracing::Span::current().record("delivery_method", "uds");
                info!(agent = %agent_key, socket = %socket_path.display(), "Delivered message via Unix socket");
                tracing::info!(
                    otel.name = "message.delivery",
                    agent_id = %from,
                    recipient = %agent_key,
                    method = "unix_socket",
                    outcome = "success",
                    detail = %socket_path.to_string_lossy(),
                    "[event] message.delivery"
                );
                return DeliveryResult::Uds;
            }
            Err(e) => {
                warn!(agent = %agent_key, error = %e, "UDS delivery failed, falling back to tmux");
                tracing::info!(
                    otel.name = "message.delivery",
                    agent_id = %from,
                    recipient = %agent_key,
                    method = "unix_socket",
                    outcome = "failed",
                    detail = %e,
                    "[event] message.delivery"
                );
            }
        }
    }

    // Fall back to tmux STDIN injection
    deliver_via_tmux(project_dir, agent_key, tmux_target, from, message).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AgentName;

    #[test]
    fn deliver_to_agent_reports_tmux_fallback_as_tmux_stdin() {
        let outcome = DeliveryOutcome::from_result(DeliveryResult::Tmux, "worker-codex");
        assert_eq!(outcome.method_string(), "tmux_stdin");
    }

    #[test]
    fn non_claude_tmux_delivery_uses_inline_submit() {
        assert_eq!(
            tmux_injection_options(crate::services::AgentType::Codex),
            tmux_events::InjectionOptions::inline_submit()
        );
        assert_eq!(
            tmux_injection_options(crate::services::AgentType::OpenCode),
            tmux_events::InjectionOptions::inline_submit()
        );
        assert_eq!(
            tmux_injection_options(crate::services::AgentType::Claude),
            tmux_events::InjectionOptions::claude_default()
        );
    }

    #[test]
    fn claude_teams_path_bypasses_agent_inbox_while_codex_falls_back_gracefully() {
        assert!(supports_teams_inbox(crate::services::AgentType::Claude));
        assert!(!supports_teams_inbox(crate::services::AgentType::Codex));
        assert!(!supports_teams_inbox(crate::services::AgentType::Gemini));
        assert!(!supports_teams_inbox(crate::services::AgentType::OpenCode));
    }

    #[test]
    fn test_format_parent_notification_success() {
        let id = crate::domain::AgentName::try_from_str("agent-1")
            .expect("literal validated string is non-empty");
        let msg = format_parent_notification(&id, NotifyStatus::Success, "All done");
        assert_eq!(msg, "[from: agent-1] All done");
    }

    #[test]
    fn test_format_parent_notification_success_empty() {
        let id = crate::domain::AgentName::try_from_str("agent-1")
            .expect("literal validated string is non-empty");
        let msg = format_parent_notification(&id, NotifyStatus::Success, "");
        assert_eq!(msg, "[from: agent-1] Status update.");
    }

    #[test]
    fn test_format_parent_notification_failure() {
        let id = crate::domain::AgentName::try_from_str("agent-2")
            .expect("literal validated string is non-empty");
        let msg = format_parent_notification(&id, NotifyStatus::Failure, "Something went wrong");
        assert_eq!(msg, "[FAILED: agent-2] Something went wrong");
    }

    #[test]
    fn test_format_parent_notification_failure_empty() {
        let id = crate::domain::AgentName::try_from_str("agent-2")
            .expect("literal validated string is non-empty");
        let msg = format_parent_notification(&id, NotifyStatus::Failure, "");
        assert_eq!(msg, "[FAILED: agent-2] Task failed.");
    }

    #[test]
    fn test_format_parent_notification_other_status() {
        let id = crate::domain::AgentName::try_from_str("agent-3")
            .expect("literal validated string is non-empty");
        let msg = format_parent_notification(&id, NotifyStatus::parse("running"), "Working...");
        assert_eq!(msg, "[from: agent-3] Working...");
    }

    #[test]
    fn test_delivery_result_variants_distinct() {
        assert_ne!(DeliveryResult::Teams, DeliveryResult::Tmux);
        assert_ne!(DeliveryResult::Teams, DeliveryResult::Failed);
        assert_ne!(DeliveryResult::Tmux, DeliveryResult::Failed);
    }

    #[test]
    fn test_worker_gone_detail_is_tl_visible() {
        assert_eq!(
            worker_gone_detail("worker-opencode", "%42"),
            "[WORKER GONE: worker-opencode] routing target %42 is not alive"
        );
    }

    #[test]
    fn test_routing_tmux_target_prefers_worker_pane_id() {
        let routing = serde_json::json!({
            "pane_id": "%42",
            "window_id": "@7",
            "parent_tab": "TL"
        });

        assert_eq!(routing_tmux_target(&routing), Some("%42".to_string()));
    }

    #[test]
    fn test_routing_tmux_target_falls_back_to_window() {
        let routing = serde_json::json!({
            "window_id": "@7",
            "parent_tab": "TL"
        });

        assert_eq!(routing_tmux_target(&routing), Some("@7".to_string()));
    }

    #[test]
    fn test_pinned_tmux_target_leaves_pane_id_unmodified() {
        assert_eq!(pinned_tmux_target("%42"), "%42");
    }

    #[test]
    fn test_pinned_tmux_target_pins_window_name_to_first_pane() {
        assert_eq!(pinned_tmux_target("TL"), "TL.0");
    }

    #[tokio::test]
    async fn test_deliver_no_registry_returns_tmux() {
        let services = crate::services::Services::test();
        let result = deliver_to_agent(
            &services,
            "agent-1",
            "tab-1",
            &AgentName::try_from_str("test").expect("literal validated string is non-empty"),
            "hello",
            "summary",
        )
        .await;
        assert_eq!(result, DeliveryResult::Tmux);
    }
}
