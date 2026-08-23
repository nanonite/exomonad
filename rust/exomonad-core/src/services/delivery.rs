use crate::domain::{Address, AgentName, BirthBranch, RoutingInfo, Slug};
use crate::services::agent_control::{
    finish_invocation_and_tombstone, InvocationFinishResult, InvocationStatus,
};
use crate::services::agent_inbox::{idempotency_key_for_message, InboxMessage, GLOBAL_AGENT_INBOX};
use crate::services::tmux_events;
use crate::services::{
    GuidanceBatch, GuidanceBatchRequest, GuidanceIdentity, GuidanceItemInput, QueueClass,
};
use claude_teams_bridge as teams_mailbox;
use claude_teams_bridge::TeamRegistry;
use exomonad_proto::effects::events::{event, AgentMessage, Event};
use tokio::process::Command;
use tracing::{debug, info, instrument, warn};

/// Delivery attempts for a single queued message before it is abandoned.
const MAX_DELIVERY_ATTEMPTS: u32 = 8;
/// First retry delay; doubles per attempt up to `MAX_DELIVERY_BACKOFF`.
const INITIAL_DELIVERY_BACKOFF: std::time::Duration = std::time::Duration::from_secs(1);
const MAX_DELIVERY_BACKOFF: std::time::Duration = std::time::Duration::from_secs(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryResult {
    Teams,
    Uds,
    Tmux,
    Durable,
    Failed,
}

fn routing_tmux_target(routing: &serde_json::Value) -> Option<String> {
    routing["pane_id"]
        .as_str()
        .or_else(|| routing["window_id"].as_str())
        .map(ToOwned::to_owned)
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

async fn mark_agent_exited(agent_dir: &std::path::Path, expected_target: &str) {
    let Ok(routing) = RoutingInfo::read_from_dir(agent_dir).await else {
        warn!(
            path = %agent_dir.display(),
            "preserving agent routing because exit reconciliation could not parse routing"
        );
        return;
    };
    if !routing_matches_tmux_target(&routing, expected_target) {
        warn!(
            path = %agent_dir.display(),
            expected_target,
            "ignoring stale exit for a replaced routing target"
        );
        return;
    }
    match finish_invocation_and_tombstone(agent_dir, &routing, InvocationStatus::Exited, None).await
    {
        Ok(InvocationFinishResult::IgnoredStale) => {
            info!(path = %agent_dir.display(), "ignored stale agent exit")
        }
        Ok(InvocationFinishResult::Finished(_)) | Ok(InvocationFinishResult::Missing) => {
            info!(path = %agent_dir.display(), "retired stale agent routing")
        }
        Err(error) => warn!(
            path = %agent_dir.display(),
            %error,
            "failed to finish invocation; preserving stale agent routing"
        ),
    }
}

fn routing_matches_tmux_target(routing: &RoutingInfo, expected_target: &str) -> bool {
    routing
        .pane_id
        .as_ref()
        .is_some_and(|pane_id| pane_id.as_str() == expected_target)
        || routing
            .window_id
            .as_ref()
            .is_some_and(|window_id| window_id.as_str() == expected_target)
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

async fn current_tmux_pane_target(target: &str) -> Option<String> {
    if target.starts_with('%') {
        return Some(target.to_string());
    }

    let session = std::env::var("EXOMONAD_TMUX_SESSION").ok()?;
    if session.trim().is_empty() {
        return None;
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
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let pane_id = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!pane_id.is_empty()).then_some(pane_id)
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
            mark_agent_exited(&agent_dir, target).await;
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
    Blocked,
}

impl NotifyStatus {
    /// Parse from proto/wire string ("failure" → Failure, "stuck" → Stuck, anything else → Success).
    pub fn parse(s: &str) -> Self {
        match s {
            "failure" => NotifyStatus::Failure,
            "stuck" => NotifyStatus::Stuck,
            "blocked" => NotifyStatus::Blocked,
            _ => NotifyStatus::Success,
        }
    }

    /// Parse a wire status fail-closed at the MCP boundary.
    pub fn parse_wire(s: &str) -> Result<Self, &'static str> {
        match s {
            "success" => Ok(NotifyStatus::Success),
            "failure" => Ok(NotifyStatus::Failure),
            "blocked" => Ok(NotifyStatus::Blocked),
            _ => Err("status must be one of success, failure, or blocked"),
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            NotifyStatus::Success => "success",
            NotifyStatus::Failure => "failure",
            NotifyStatus::Stuck => "stuck",
            NotifyStatus::Blocked => "blocked",
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
        NotifyStatus::Blocked => "Task is externally blocked. Human intervention required.",
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
        NotifyStatus::Blocked => format!("[BLOCKED: {}] {}", agent_id, msg),
        NotifyStatus::Success => format!("[from: {}] {}", agent_id, msg),
    }
}

/// Delivery method used for message routing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeliveryMethod {
    TeamsInbox,
    Uds,
    Tmux,
    DurableInbox,
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
            DeliveryResult::Uds => DeliveryOutcome::Delivered {
                method: DeliveryMethod::Uds,
                recipient: agent,
            },
            DeliveryResult::Tmux => DeliveryOutcome::Delivered {
                method: DeliveryMethod::Tmux,
                recipient: agent,
            },
            DeliveryResult::Durable => DeliveryOutcome::Delivered {
                method: DeliveryMethod::DurableInbox,
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
                DeliveryMethod::Uds => "unix_socket",
                DeliveryMethod::Tmux => "tmux_stdin",
                DeliveryMethod::DurableInbox => "durable_inbox",
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
          + super::HasAgentResolver
          + super::HasInboxStore
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
          + super::HasAgentResolver
          + super::HasInboxStore
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

/// Route an operational notification through tmux without creating a durable inbox row.
#[instrument(skip_all, fields(address = %address, from = %from))]
pub async fn route_tmux_notification(
    ctx: &(impl super::HasTeamRegistry + super::HasAgentResolver + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    _summary: &str,
) -> DeliveryOutcome {
    match address {
        Address::Agent(name) => {
            let tab_name = resolve_tab_name_for_agent(name, Some(ctx.agent_resolver()));
            let agent_key = name.as_str();
            let result =
                deliver_via_tmux(ctx.project_dir(), agent_key, &tab_name, from, content, None)
                    .await;
            DeliveryOutcome::from_result(result, agent_key)
        }
        Address::Team { team, member } => {
            let agent_key = match member {
                Some(member_name) => member_name.as_str().to_string(),
                None => ctx
                    .team_registry()
                    .resolve_lead(team.as_str())
                    .await
                    .unwrap_or_else(|| "root".to_string()),
            };
            let agent_name = crate::domain::AgentName::try_from_str(agent_key.as_str())
                .expect("validated agent key is non-empty");
            let tab_name = resolve_tab_name_for_agent(&agent_name, Some(ctx.agent_resolver()));
            let result = deliver_via_tmux(
                ctx.project_dir(),
                &agent_key,
                &tab_name,
                from,
                content,
                None,
            )
            .await;
            DeliveryOutcome::from_result(result, &agent_key)
        }
        Address::Supervisor => {
            let result =
                deliver_via_tmux(ctx.project_dir(), "root", "TL", from, content, None).await;
            DeliveryOutcome::from_result(result, "root")
        }
    }
}

/// Route a message only through the Claude Teams inbox mailbox protocol.
#[instrument(skip_all, fields(address = %address, from = %from))]
pub async fn route_mailbox_message(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
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
          + super::HasAgentResolver
          + super::HasInboxStore
          + super::HasProjectDir),
    address: &Address,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
    path: MessageDeliveryPath,
) -> DeliveryOutcome {
    if matches!(address, Address::Agent(name) if name.as_str() == "parent") {
        warn!(
            from = %from,
            "Refusing to route message to reserved agent alias 'parent'"
        );
        return DeliveryOutcome::Failed {
            original: "agent:parent".to_string(),
            reason: "reserved agent alias 'parent' must be resolved by notify_parent".to_string(),
        };
    }

    match address {
        Address::Agent(name) => {
            let tab_name = resolve_tab_name_for_agent(name, Some(ctx.agent_resolver()));
            let agent_key = name.as_str();
            let result = deliver_to_agent_for(
                ctx,
                agent_key,
                &tab_name,
                from,
                content,
                summary,
                QueueClass::Steering,
                path,
            )
            .await;
            DeliveryOutcome::from_result(result, agent_key)
        }
        Address::Team { team, member } => {
            if let Some(member_name) = member {
                let tab_name = resolve_tab_name_for_agent(member_name, Some(ctx.agent_resolver()));
                let agent_key = member_name.as_str();
                let result = deliver_to_agent_for(
                    ctx,
                    agent_key,
                    &tab_name,
                    from,
                    content,
                    summary,
                    QueueClass::Steering,
                    path,
                )
                .await;
                DeliveryOutcome::from_result(result, agent_key)
            } else {
                resolve_and_deliver_to_lead(
                    ctx,
                    team.as_str(),
                    from,
                    content,
                    summary,
                    QueueClass::Steering,
                    path,
                )
                .await
            }
        }
        Address::Supervisor => {
            let result = deliver_to_agent_for(
                ctx,
                "root",
                "TL",
                from,
                content,
                summary,
                QueueClass::Steering,
                path,
            )
            .await;
            DeliveryOutcome::from_result(result, "root")
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn deliver_to_agent_for(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
          + super::HasProjectDir),
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
    queue_class: QueueClass,
    path: MessageDeliveryPath,
) -> DeliveryResult {
    match path {
        MessageDeliveryPath::Smart => {
            deliver_to_agent_with_class(
                ctx,
                agent_key,
                tmux_target,
                from,
                message,
                summary,
                queue_class,
            )
            .await
        }
        MessageDeliveryPath::TmuxOnly => {
            let Some(batch) =
                record_inbox_delivery(ctx, agent_key, from, message, summary, queue_class).await
            else {
                return DeliveryResult::Failed;
            };
            match deliver_via_tmux(
                ctx.project_dir(),
                agent_key,
                tmux_target,
                from,
                message,
                Some(&batch.batch_id),
            )
            .await
            {
                DeliveryResult::Failed => DeliveryResult::Durable,
                result => result,
            }
        }
        MessageDeliveryPath::MailboxOnly => {
            if record_inbox_delivery(ctx, agent_key, from, message, summary, queue_class)
                .await
                .is_none()
            {
                return DeliveryResult::Failed;
            }
            match deliver_to_agent_mailbox(ctx, agent_key, from, message, summary).await {
                DeliveryResult::Failed => DeliveryResult::Durable,
                result => result,
            }
        }
    }
}

/// Resolve team lead and deliver. Uses `config.json`'s `leadAgentId` to find
/// the lead, falls back to first in-memory entry, then to "root".
async fn resolve_and_deliver_to_lead(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
          + super::HasProjectDir),
    team_name: &str,
    from: &crate::domain::AgentName,
    content: &str,
    summary: &str,
    queue_class: QueueClass,
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
    let result = deliver_to_agent_for(
        ctx,
        &lead_key,
        &tab_name,
        from,
        content,
        summary,
        queue_class,
        path,
    )
    .await;

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
        DeliveryResult::Uds => DeliveryMethod::Uds,
        DeliveryResult::Tmux => DeliveryMethod::Tmux,
        DeliveryResult::Durable | DeliveryResult::Failed => DeliveryMethod::DurableInbox,
    }
}

/// Canonicalize a computed parent branch into the recipient's AgentName.
///
/// A spawned agent's branch is always dotted (`{parent}.{slug}-{type}`); the
/// last dot-segment is that agent's suffixed AgentName, which is the key it
/// drains its durable inbox under. A top-level branch (no dot, e.g. `main` or
/// `master`) hosts no spawned agent of its own — it belongs to the root TL,
/// which is addressed and drains as the AgentName `root`.
///
/// Dotted branches are returned unchanged; the inbox keys them by their last
/// dot-segment on write/drain. Collapsing top-level branches here keeps the
/// recipient key equal to the agent's own AgentName at the notify_parent
/// source, so the durable inbox never records an undeliverable key like `main`.
pub(crate) fn canonical_parent_recipient(parent_branch: &str) -> String {
    if parent_branch.contains('.') {
        parent_branch.to_string()
    } else {
        "root".to_string()
    }
}

async fn canonical_recipient_key(
    resolver: &crate::services::AgentResolver,
    agent_key: &str,
) -> String {
    if let Ok(name) = AgentName::try_from_str(agent_key) {
        if resolver.get(&name).await.is_some() {
            return agent_key.to_string();
        }
    }

    if let Ok(branch) = BirthBranch::try_from_str(agent_key) {
        if let Some(record) = resolver
            .all()
            .await
            .into_iter()
            .find(|record| record.birth_branch == branch)
        {
            return record.agent_name.to_string();
        }
    }

    if agent_key.contains('.') {
        return agent_key.to_string();
    }

    if let Ok(slug) = Slug::try_from_str(agent_key) {
        if let Some(record) = resolver.lookup_by_slug(&slug).await {
            return record.agent_name.to_string();
        }
    }

    let candidates = resolver
        .all()
        .await
        .into_iter()
        .map(|record| format!("{}:{}", record.slug, record.agent_name))
        .collect::<Vec<_>>()
        .join(",");
    warn!(
        unresolved_recipient = %agent_key,
        resolver_candidates = %candidates,
        "Unable to resolve inbox recipient key; recording message under unresolved key"
    );
    tracing::info!(
        otel.name = "message.delivery",
        recipient = %agent_key,
        method = "agent_inbox",
        outcome = "unresolved_recipient",
        resolver_candidates = %candidates,
        "[event] message.delivery"
    );

    agent_key.to_string()
}

/// Rebuild transport caches from durable guidance at process startup.
///
/// The durable store recovers expired leases and determines which agents have
/// available pending batches. `AgentInbox` only receives those rows; it never
/// decides which batches exist or whether a batch is terminal.
pub async fn rebuild_durable_inbox_caches(
    store: &crate::services::InboxStore,
    resolver: &crate::services::AgentResolver,
    project_dir: &std::path::Path,
) -> anyhow::Result<usize> {
    store.recover_expired_leases(crate::services::inbox_store::now_epoch_secs())?;
    let agent_ids = store.pending_agent_ids(crate::services::inbox_store::now_epoch_secs())?;
    let mut restored = 0;
    for agent_id in agent_ids {
        let (target, agent_type) = if agent_id == "root" {
            ("TL".to_string(), crate::services::AgentType::Claude)
        } else if let Ok(agent_name) = AgentName::try_from_str(&agent_id) {
            let record = resolver.get(&agent_name).await;
            (
                resolve_tab_name_for_agent(&agent_name, Some(resolver)),
                record
                    .map(|record| record.agent_type)
                    .unwrap_or_else(|| agent_type_from_key(&agent_id)),
            )
        } else {
            (agent_id.clone(), agent_type_from_key(&agent_id))
        };
        restored += GLOBAL_AGENT_INBOX
            .rebuild_from_durable(
                store,
                &agent_id,
                &target,
                project_dir.to_path_buf(),
                tmux_injection_options(agent_type),
            )
            .await?;
    }
    Ok(restored)
}

async fn record_inbox_delivery(
    ctx: &(impl super::HasInboxStore + super::HasAgentResolver),
    agent_key: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
    queue_class: QueueClass,
) -> Option<GuidanceBatch> {
    let to_agent = canonical_recipient_key(ctx.agent_resolver(), agent_key).await;
    let idempotency_key = idempotency_key_for_message(from.as_str(), &to_agent, message);
    let request = GuidanceBatchRequest {
        agent_id: to_agent.clone(),
        queue_class,
        items: vec![GuidanceItemInput {
            from_agent: from.as_str().to_string(),
            content: message.to_string(),
            summary: Some(summary.to_string()),
            injection_options: serde_json::Value::Null,
        }],
        identity: GuidanceIdentity::default(),
        idempotency_key: Some(idempotency_key),
        source_message_id: None,
    };
    match ctx.inbox_store().enqueue_batch_with_compatibility(
        request,
        from.as_str(),
        message,
        Some(summary),
    ) {
        Ok(result) => {
            debug!(
                batch_id = %result.batch.batch_id,
                source_message_id = ?result.batch.source_message_id,
                queue_class = ?result.batch.queue_class,
                from = %from,
                to = %to_agent,
                "Committed durable guidance batch and compatibility message before transport"
            );
            Some(result.batch)
        }
        Err(error) => {
            warn!(
                from = %from,
                to = %to_agent,
                error = %error,
                "Failed to commit durable guidance batch before transport"
            );
            None
        }
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
            if let Some(record) = records
                .values()
                .find(|record| record.birth_branch.as_str() == agent_key.as_str())
            {
                return record.display_name.clone();
            }
        }
    }

    // A bare birth-branch like "main" has no recognized agent type suffix.
    // from_internal_name defaults to Codex when no suffix matches, so
    // cross-checking distinguishes a bare branch from an actual Codex agent.
    // Bare branches are always the root TL's birth-branch → window is "TL".
    let derived_type = crate::services::agent_control::AgentType::from_dir_name(agent_key.as_str());
    if matches!(
        derived_type,
        crate::services::agent_control::AgentType::Codex
    ) && !agent_key.as_str().ends_with("-codex")
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
          + super::HasAgentResolver
          + super::HasEventLog
          + super::HasEventQueue
          + super::HasInboxStore
          + super::HasProjectDir),
    agent_id: &crate::domain::AgentName,
    parent_session_id: &str,
    parent_tab_name: &str,
    status: NotifyStatus,
    message: &str,
    summary: Option<&str>,
    source: &str,
    head_sha: Option<&str>,
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
                "head_sha": head_sha,
                "head_sha_finding": head_sha.map(|_| serde_json::Value::Null).unwrap_or_else(|| {
                    serde_json::Value::String(
                        "not_available_without_verified_pr_context".to_string(),
                    )
                }),
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

    if let Some(log) = ctx.event_log() {
        let _ = log.append(
            "message.delivery",
            agent_id.as_str(),
            &serde_json::json!({
                "from": agent_id,
                "recipient": parent_session_id,
                "method": format!("{:?}", delivery_method_from_result(delivery_result)),
                "outcome": if delivery_result == DeliveryResult::Failed { "failed" } else { "success" },
                "source": source,
            }),
        );
    }

    delivery_result
}

/// Drain `agent`'s inbox queue, injecting each message into its tmux target.
/// Exits only when the queue is empty; a failing delivery is retried with
/// backoff and then abandoned, never left stranded without a consumer.
async fn spawn_inbox_consumer(agent: String) {
    tokio::spawn(run_inbox_consumer(
        agent,
        |message: InboxMessage| async move {
            tmux_events::inject_input_with_options(
                &message.target,
                &message.body,
                &message.project_dir,
                message.injection_options,
            )
            .await
        },
    ));
}

/// Consumer loop, generic over the injector so tests can drive failure paths.
async fn run_inbox_consumer<F, Fut>(agent: String, inject: F)
where
    F: Fn(InboxMessage) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = anyhow::Result<()>> + Send + 'static,
{
    loop {
        let Some(message) = GLOBAL_AGENT_INBOX.begin_delivery(&agent).await else {
            return;
        };

        let mut attempt: u32 = 0;
        loop {
            attempt += 1;
            let result = deliver_once(&inject, message.clone()).await;
            let success = result.is_ok();

            let outcome = if success { "success" } else { "failed" };
            let attempt_data = serde_json::json!({
                "message_id": message.id,
                "recipient": message.recipient,
                "from": message.from,
                "method": "agent_inbox_tmux",
                "attempt": attempt,
                "outcome": outcome,
                "detail": message.detail,
            });
            if let Ok(log) = crate::services::EventLog::open(message.project_dir.join(".exo/logs"))
            {
                let _ = log.append("message.delivery", &message.from, &attempt_data);
            }

            crate::services::lifecycle::record_guidance_delivery(
                &message.project_dir,
                &message.recipient,
                &message.from,
                "tmux_injection",
                if success { "success" } else { "failed" },
            )
            .await;

            tracing::info!(
                otel.name = "message.delivery",
                agent_id = %message.from,
                recipient = %message.recipient,
                method = "agent_inbox_tmux",
                outcome = if success { "success" } else { "failed" },
                attempt,
                detail = %message.detail,
                "[event] message.delivery"
            );

            if success {
                GLOBAL_AGENT_INBOX
                    .complete_delivery(&agent, message.id, true)
                    .await;
                break;
            }

            let error = result.unwrap_err();

            if attempt >= MAX_DELIVERY_ATTEMPTS {
                tracing::error!(
                    target = %message.target,
                    recipient = %message.recipient,
                    attempts = attempt,
                    error = %error,
                    "agent inbox delivery abandoned after exhausting retries; message dropped"
                );
                tracing::info!(
                    otel.name = "agent_inbox.messages_abandoned",
                    recipient = %message.recipient,
                    attempts = attempt,
                    "[metric] agent_inbox.messages_abandoned"
                );
                if let Ok(log) =
                    crate::services::EventLog::open(message.project_dir.join(".exo/logs"))
                {
                    let _ = log.append(
                        "agent_inbox.messages_abandoned",
                        &message.recipient,
                        &serde_json::json!({
                            "message_id": message.id,
                            "recipient": message.recipient,
                            "attempts": attempt,
                            "outcome": "abandoned"
                        }),
                    );
                }
                GLOBAL_AGENT_INBOX
                    .abandon_delivery(&agent, message.id)
                    .await;
                break;
            }

            warn!(
                target = %message.target,
                recipient = %message.recipient,
                attempt,
                error = %error,
                "agent inbox delivery failed; retrying after backoff"
            );
            tokio::time::sleep(delivery_backoff(attempt)).await;
        }
    }
}

/// Run one injection attempt in its own task so a panic inside tmux injection
/// becomes an ordinary failed attempt instead of killing the consumer and
/// stranding the queue.
async fn deliver_once<F, Fut>(inject: &F, message: InboxMessage) -> anyhow::Result<()>
where
    F: Fn(InboxMessage) -> Fut + Send + Sync + 'static,
    Fut: std::future::Future<Output = anyhow::Result<()>> + Send + 'static,
{
    match tokio::spawn(inject(message)).await {
        Ok(result) => result,
        Err(join_error) if join_error.is_panic() => {
            Err(anyhow::anyhow!("delivery task panicked: {join_error}"))
        }
        Err(join_error) => Err(anyhow::anyhow!("delivery task failed: {join_error}")),
    }
}

fn delivery_backoff(attempt: u32) -> std::time::Duration {
    INITIAL_DELIVERY_BACKOFF
        .saturating_mul(2u32.saturating_pow(attempt.saturating_sub(1)))
        .min(MAX_DELIVERY_BACKOFF)
}

async fn enqueue_tmux_delivery(
    agent_key: &str,
    target: &str,
    effective_pd: std::path::PathBuf,
    from: &crate::domain::AgentName,
    message: &str,
    detail: &str,
    cache_key: Option<&str>,
) -> DeliveryResult {
    let mut inbox_message = InboxMessage::new(
        target.to_string(),
        effective_pd,
        from.as_str().to_string(),
        agent_key.to_string(),
        message.to_string(),
        detail.to_string(),
    )
    .with_injection_options(tmux_injection_options(agent_type_from_key(agent_key)));
    if let Some(cache_key) = cache_key {
        inbox_message = inbox_message.with_cache_key(cache_key);
    }

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
    cache_key: Option<&str>,
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
            ["claude", "shoal", "opencode", "codex"]
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
        return enqueue_tmux_delivery(
            agent_key,
            &target,
            effective_pd,
            from,
            message,
            &target,
            cache_key,
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
    // Resolve the current pane from the display name. Window and pane indexes
    // are session-local and can become stale after a restart; the display name
    // is the stable identity supplied by AgentResolver.
    let Some(current_target) = current_tmux_pane_target(tmux_target).await else {
        warn!(
            agent = %agent_key,
            target = %tmux_target,
            "refusing tmux delivery because the exact current pane cannot be resolved"
        );
        return DeliveryResult::Failed;
    };
    match tmux_target_alive(&current_target).await {
        Ok(true) => {}
        Ok(false) => {
            warn!(
                agent = %agent_key,
                target = %current_target,
                "refusing tmux delivery because the exact current pane is stale"
            );
            return DeliveryResult::Failed;
        }
        Err(error) => {
            warn!(
                agent = %agent_key,
                target = %current_target,
                %error,
                "refusing tmux delivery because pane liveness could not be verified"
            );
            return DeliveryResult::Failed;
        }
    }
    enqueue_tmux_delivery(
        agent_key,
        &current_target,
        effective_pd,
        from,
        message,
        tmux_target,
        cache_key,
    )
    .await
}

async fn deliver_to_agent_mailbox(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
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
/// Tries Teams inbox delivery if a registry and agent key are provided.
/// Attempts HTTP-over-UDS delivery for custom binary agents (e.g., shoal-agent).
/// Falls back to tmux input injection if other delivery methods fail or are not available.
#[instrument(skip_all, fields(agent_key = %agent_key, from = %from, delivery_method = tracing::field::Empty))]
pub async fn deliver_to_agent(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
          + super::HasProjectDir),
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
) -> DeliveryResult {
    deliver_to_agent_with_class(
        ctx,
        agent_key,
        tmux_target,
        from,
        message,
        summary,
        QueueClass::FollowUp,
    )
    .await
}

async fn deliver_to_agent_with_class(
    ctx: &(impl super::HasTeamRegistry
          + super::HasAgentResolver
          + super::HasInboxStore
          + super::HasProjectDir),
    agent_key: &str,
    tmux_target: &str,
    from: &crate::domain::AgentName,
    message: &str,
    summary: &str,
    queue_class: QueueClass,
) -> DeliveryResult {
    let team_registry = ctx.team_registry();
    let _agent_resolver = ctx.agent_resolver();
    let project_dir = ctx.project_dir();
    let agent_type = agent_type_from_key(agent_key);
    if let Err(error) = GLOBAL_AGENT_INBOX
        .rebuild_from_durable(
            ctx.inbox_store(),
            agent_key,
            tmux_target,
            project_dir.to_path_buf(),
            tmux_injection_options(agent_type),
        )
        .await
    {
        warn!(agent = %agent_key, %error, "Failed to rebuild durable guidance transport cache");
    }
    let Some(batch) =
        record_inbox_delivery(ctx, agent_key, from, message, summary, queue_class).await
    else {
        crate::services::lifecycle::record_guidance_delivery(
            project_dir,
            agent_key,
            from.as_str(),
            "durable_inbox",
            "failed",
        )
        .await;
        return DeliveryResult::Failed;
    };
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
                        "Teams inbox write failed after 3 attempts, falling back to tmux"
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
                crate::services::lifecycle::record_guidance_delivery_with_batch(
                    project_dir,
                    agent_key,
                    from.as_str(),
                    "teams_inbox",
                    "success",
                    Some(&batch),
                )
                .await;

                // Spawn background task to verify CC's InboxPoller read the message.
                // If not read within 30s, fall back to tmux STDIN injection.
                // For Tier 2 (CC-native) recipients, skip tmux fallback — they don't
                // have exomonad worktrees or routing.json. CC's InboxPoller owns delivery.
                let team_name = team_info.team_name.clone();
                let inbox_name = team_info.inbox_name.clone();
                let agent = agent_key.to_string();
                let target = tmux_target.to_string();
                let msg = message.to_string();
                let batch_id = batch.batch_id.clone();
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
                    let _ = enqueue_tmux_delivery(
                        &agent,
                        &target,
                        pd,
                        &fallback_sender,
                        &msg,
                        &target,
                        Some(batch_id.as_str()),
                    )
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
                crate::services::lifecycle::record_guidance_delivery_with_batch(
                    project_dir,
                    agent_key,
                    from.as_str(),
                    "uds",
                    "success",
                    Some(&batch),
                )
                .await;
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
                crate::services::lifecycle::record_guidance_delivery_with_batch(
                    project_dir,
                    agent_key,
                    from.as_str(),
                    "uds",
                    "failed",
                    Some(&batch),
                )
                .await;
            }
        }
    }

    // Fall back to tmux STDIN injection
    let result = deliver_via_tmux(
        project_dir,
        agent_key,
        tmux_target,
        from,
        message,
        Some(&batch.batch_id),
    )
    .await;
    crate::services::lifecycle::record_guidance_delivery_with_batch(
        project_dir,
        agent_key,
        from.as_str(),
        "exact_tmux",
        if matches!(result, DeliveryResult::Failed) {
            "failed"
        } else {
            "queued"
        },
        Some(&batch),
    )
    .await;
    match result {
        DeliveryResult::Failed => DeliveryResult::Durable,
        result => result,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::{AgentIdentityRecord, AgentResolver, HasAgentResolver};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU32, Ordering as AtomicOrdering};
    use std::sync::Arc;

    fn agent_name(value: &str) -> AgentName {
        AgentName::try_from_str(value).expect("literal validated string is non-empty")
    }

    fn birth_branch(value: &str) -> BirthBranch {
        BirthBranch::try_from_str(value).expect("literal validated string is non-empty")
    }

    fn slug(value: &str) -> Slug {
        Slug::try_from_str(value).expect("literal validated string is non-empty")
    }

    fn tmux_message(body: &str) -> crate::services::agent_inbox::InboxMessage {
        crate::services::agent_inbox::InboxMessage::new(
            "%1".to_string(),
            std::path::PathBuf::from("."),
            "sender".to_string(),
            "recipient".to_string(),
            body.to_string(),
            "%1".to_string(),
        )
    }

    /// Injector that fails its first `fail_times` calls, then succeeds.
    #[allow(clippy::type_complexity)]
    fn flaky_injector(
        fail_times: u32,
    ) -> (
        Arc<AtomicU32>,
        impl Fn(
                crate::services::agent_inbox::InboxMessage,
            )
                -> std::pin::Pin<Box<dyn std::future::Future<Output = anyhow::Result<()>> + Send>>
            + Send
            + Sync
            + 'static,
    ) {
        let calls = Arc::new(AtomicU32::new(0));
        let counter = calls.clone();
        let inject = move |_message| {
            let seen = counter.fetch_add(1, AtomicOrdering::SeqCst) + 1;
            Box::pin(async move {
                if seen <= fail_times {
                    Err(anyhow::anyhow!("simulated tmux failure"))
                } else {
                    Ok(())
                }
            })
                as std::pin::Pin<Box<dyn std::future::Future<Output = anyhow::Result<()>> + Send>>
        };
        (calls, inject)
    }

    async fn services_with_agent(slug_value: &str, agent_value: &str) -> crate::services::Services {
        let temp_dir = tempfile::tempdir().expect("tempdir should be created");
        let resolver = AgentResolver::load(temp_dir.path().to_path_buf()).await;
        resolver
            .register(AgentIdentityRecord {
                agent_name: agent_name(agent_value),
                slug: slug(slug_value),
                agent_type: AgentType::OpenCode,
                birth_branch: birth_branch(&format!("main.{agent_value}")),
                parent_branch: birth_branch("main"),
                working_dir: PathBuf::from(format!(".exo/worktrees/{agent_value}")),
                display_name: agent_value.to_string(),
                topology: Topology::WorktreePerAgent,
                model: None,
                effort: None,
                ledger_owned: false,
                slice_id: None,
            })
            .await
            .expect("identity registration should succeed");

        let mut services = crate::services::Services::test();
        services.agent_resolver = Arc::new(resolver);
        services
    }

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
    fn delivery_backoff_doubles_and_saturates_at_cap() {
        assert_eq!(delivery_backoff(1), std::time::Duration::from_secs(1));
        assert_eq!(delivery_backoff(2), std::time::Duration::from_secs(2));
        assert_eq!(delivery_backoff(3), std::time::Duration::from_secs(4));
        assert_eq!(delivery_backoff(6), std::time::Duration::from_secs(30));
        assert_eq!(delivery_backoff(64), MAX_DELIVERY_BACKOFF);
    }

    #[tokio::test]
    async fn record_inbox_delivery_resolves_slug_to_agent_name() {
        let services = services_with_agent("patch-step-over", "patch-step-over-opencode").await;
        let from = agent_name("root");

        assert!(record_inbox_delivery(
            &services,
            "patch-step-over",
            &from,
            "hello",
            "summary",
            QueueClass::FollowUp,
        )
        .await
        .is_some());

        let (queue_class, state, content, source_message_id): (String, String, String, i64) =
            services
                .inbox_store
                .connection()
                .expect("guidance connection should open")
                .query_row(
                    "SELECT b.queue_class, b.state, i.content, b.source_message_id
                 FROM guidance_batches b
                 JOIN guidance_items i ON i.batch_id = b.batch_id
                 WHERE b.agent_id = ?1",
                    ["patch-step-over-opencode"],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .expect("guidance batch should be committed");
        assert_eq!(queue_class, "follow_up");
        assert_eq!(state, "pending");
        assert_eq!(content, "hello");
        let compatibility_message_id: i64 = services
            .inbox_store
            .connection()
            .expect("guidance connection should open")
            .query_row(
                "SELECT id FROM messages WHERE to_agent = ?1 AND content = ?2",
                ["patch-step-over-opencode", "hello"],
                |row| row.get(0),
            )
            .expect("compatibility message should be retained");
        assert_eq!(source_message_id, compatibility_message_id);

        let messages = services
            .inbox_store
            .drain_unread("patch-step-over-opencode")
            .expect("inbox drain should succeed");
        assert_eq!(messages.len(), 1);
        assert_eq!(messages[0].to_agent, "patch-step-over-opencode");
        assert_eq!(messages[0].content, "hello");
    }

    #[tokio::test]
    async fn canonical_recipient_key_keeps_registered_agent_name() {
        let services = services_with_agent("patch-step-over", "patch-step-over-opencode").await;

        let recipient =
            canonical_recipient_key(services.agent_resolver(), "patch-step-over-opencode").await;

        assert_eq!(recipient, "patch-step-over-opencode");
    }

    #[tokio::test]
    async fn canonical_recipient_key_resolves_parent_birth_branch() {
        let services = services_with_agent("patch-step-over", "patch-step-over-opencode").await;

        let recipient =
            canonical_recipient_key(services.agent_resolver(), "main.patch-step-over-opencode")
                .await;

        assert_eq!(recipient, "patch-step-over-opencode");
    }

    #[tokio::test]
    async fn resolve_tab_name_uses_current_identity_for_birth_branch() {
        let services = services_with_agent("patch-step-over", "patch-step-over-opencode").await;
        let branch = agent_name("main.patch-step-over-opencode");

        assert_eq!(
            resolve_tab_name_for_agent(&branch, Some(services.agent_resolver())),
            "patch-step-over-opencode"
        );
    }

    #[tokio::test]
    async fn route_message_rejects_reserved_parent_alias_without_recording() {
        let services = crate::services::Services::test();
        let from = agent_name("root");
        let parent_alias = Address::Agent(agent_name("parent"));

        let outcome = route_message(&services, &parent_alias, &from, "hello", "summary").await;

        assert!(matches!(outcome, DeliveryOutcome::Failed { .. }));
        let messages = services
            .inbox_store
            .drain_unread("parent")
            .expect("inbox drain should succeed");
        assert!(messages.is_empty());
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
    fn test_routing_tmux_target_uses_exact_window_id_for_leaf_delivery() {
        let routing = serde_json::json!({
            "window_id": "@7",
            "parent_tab": "TL"
        });

        assert_eq!(routing_tmux_target(&routing), Some("@7".to_string()));
    }

    #[test]
    fn test_exit_reconciliation_matches_window_or_pane_generation() {
        let window_routing = RoutingInfo::window(
            crate::services::tmux_ipc::WindowId::parse("@7").expect("valid window id"),
        );
        assert!(routing_matches_tmux_target(&window_routing, "@7"));
        assert!(!routing_matches_tmux_target(&window_routing, "%7"));

        let pane_routing = RoutingInfo::pane(
            crate::services::tmux_ipc::PaneId::parse("%42").expect("valid pane id"),
            "TL",
        );
        assert!(routing_matches_tmux_target(&pane_routing, "%42"));
        assert!(!routing_matches_tmux_target(&pane_routing, "@7"));
    }

    #[tokio::test]
    async fn test_deliver_no_registry_preserves_durable_inbox() {
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
        assert_eq!(
            result,
            DeliveryResult::Durable,
            "stale or unavailable panes must reject tmux while preserving the inbox message"
        );

        let drained = services.inbox_store.drain_unread("agent-1").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].from_agent, "test");
        assert_eq!(drained[0].content, "hello");
        assert_eq!(drained[0].summary.as_deref(), Some("summary"));
    }

    #[tokio::test(start_paused = true)]
    async fn consumer_retries_failed_delivery_then_succeeds() {
        let agent = "retry-then-succeed-agent";
        GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message("[MERGE READY] PR #42"))
            .await
            .unwrap();

        let (calls, inject) = flaky_injector(2);
        run_inbox_consumer(agent.to_string(), inject).await;

        assert_eq!(
            calls.load(AtomicOrdering::SeqCst),
            3,
            "two failures then success"
        );
        assert_eq!(GLOBAL_AGENT_INBOX.queue_depth(agent).await, 0);
        assert!(!GLOBAL_AGENT_INBOX.is_consumer_active(agent).await);
    }

    #[tokio::test(start_paused = true)]
    async fn consumer_abandons_message_after_exhausting_attempts() {
        let agent = "abandon-after-max-agent";
        let body = "[MERGE READY] PR #99";
        GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message(body))
            .await
            .unwrap();

        let (calls, inject) = flaky_injector(u32::MAX);
        run_inbox_consumer(agent.to_string(), inject).await;

        assert_eq!(calls.load(AtomicOrdering::SeqCst), MAX_DELIVERY_ATTEMPTS);
        assert_eq!(GLOBAL_AGENT_INBOX.queue_depth(agent).await, 0);
        assert!(!GLOBAL_AGENT_INBOX.is_consumer_active(agent).await);

        // Abandoned, not marked recently-delivered: the same event is accepted again.
        let retry = GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message(body))
            .await
            .unwrap();
        assert!(!retry.dropped_as_duplicate);
    }

    /// Regression test for #543: a failed delivery must not end the consumer while
    /// later messages are still queued.
    #[tokio::test(start_paused = true)]
    async fn consumer_survives_failure_and_drains_remaining_messages() {
        let agent = "survives-failure-agent";
        GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message("[MERGE READY] PR #1"))
            .await
            .unwrap();
        GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message("[PR READY] PR #2"))
            .await
            .unwrap();

        let (_calls, inject) = flaky_injector(1);
        run_inbox_consumer(agent.to_string(), inject).await;

        assert_eq!(
            GLOBAL_AGENT_INBOX.queue_depth(agent).await,
            0,
            "consumer must drain the queue despite the first attempt failing"
        );
    }

    /// A panicking injector must be treated as a failed attempt, not kill the
    /// consumer and strand the queue with `consumer_active == true`.
    #[tokio::test(start_paused = true)]
    async fn panicking_injector_is_retried_not_fatal() {
        let agent = "panicking-injector-agent";
        GLOBAL_AGENT_INBOX
            .enqueue(agent, tmux_message("[MERGE READY] PR #7"))
            .await
            .unwrap();

        let calls = Arc::new(AtomicU32::new(0));
        let counter = calls.clone();
        let inject = move |_message| {
            let seen = counter.fetch_add(1, AtomicOrdering::SeqCst) + 1;
            Box::pin(async move {
                if seen == 1 {
                    panic!("simulated panic inside tmux injection");
                }
                Ok(())
            })
                as std::pin::Pin<Box<dyn std::future::Future<Output = anyhow::Result<()>> + Send>>
        };

        run_inbox_consumer(agent.to_string(), inject).await;

        assert_eq!(calls.load(AtomicOrdering::SeqCst), 2, "panic retried once");
        assert_eq!(GLOBAL_AGENT_INBOX.queue_depth(agent).await, 0);
        assert!(!GLOBAL_AGENT_INBOX.is_consumer_active(agent).await);
    }
}
