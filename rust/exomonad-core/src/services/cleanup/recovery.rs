use super::discovery::DiscoveredResource;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::{
    pr_metadata_value, CleanupBranchActionStatus, CleanupLiveness, CleanupRecoveredProvenance,
};
use crate::domain::{AgentName, BirthBranch, BranchName, Slug};
use crate::services::agent_control::{AgentIdentity, AgentType, Topology};
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::immutable_ledger::LedgerRecord;
use crate::services::pr_registry::{read_published_heads, PublicationProvenance, PublishedHead};
use crate::services::tmux_ipc::TmuxIpc;
use anyhow::Context;
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn recovered_liveness(
        &self,
        identity: &AgentIdentityRecord,
    ) -> CleanupLiveness {
        let Some(session) = self.tmux_session.as_deref() else {
            return CleanupLiveness::Unknown;
        };
        if session.trim().is_empty() {
            return CleanupLiveness::Unknown;
        }
        let windows = match TmuxIpc::new(session).list_windows().await {
            Ok(windows) => windows,
            Err(_) => return CleanupLiveness::Unknown,
        };
        if windows.iter().any(|window| {
            window.window_name == identity.display_name
                || window.window_name == identity.agent_name.as_str()
        }) {
            CleanupLiveness::Live
        } else {
            CleanupLiveness::Dead
        }
    }

    pub(super) async fn revalidate_recovered_liveness(
        &self,
        identity: &AgentIdentityRecord,
    ) -> anyhow::Result<()> {
        match self.recovered_liveness(identity).await {
            CleanupLiveness::Dead => Ok(()),
            CleanupLiveness::Live => {
                anyhow::bail!("recovered agent has a live tmux window")
            }
            CleanupLiveness::Unknown => {
                anyhow::bail!("recovered agent liveness is unknown")
            }
        }
    }

    pub(super) async fn recover_residual_resource(
        &self,
        path: &std::path::Path,
        resolver_records: &[AgentIdentityRecord],
        recovery_entries: &[super::types::CleanupReceiptEntry],
    ) -> Option<DiscoveredResource> {
        let canonical = fs::canonicalize(path).await.ok()?;
        let worktree_root = fs::canonicalize(self.project_dir.join(".exo/worktrees"))
            .await
            .ok()?;
        if canonical.parent() != Some(worktree_root.as_path())
            || !path_within(&worktree_root, &canonical)
            || fs::symlink_metadata(path.join(".git")).await.is_ok()
        {
            return None;
        }
        if !residual_contents_are_safe(path).await.ok()? {
            return None;
        }
        if workspace_git_root(path).await.ok().flatten()? != self.project_dir {
            return None;
        }
        let id = path.file_name()?.to_str()?.to_string();
        if resolver_records
            .iter()
            .any(|identity| identity.agent_name.as_str() == id)
        {
            return None;
        }
        let event_log = self.event_log.as_ref()?;
        let events = event_log.ledger().read_resolved_events().ok()?;
        let (branch, agent_type) = recovered_spawn_evidence(&events, &id)?;
        let publications = read_published_heads(&self.project_dir)
            .await
            .ok()?
            .into_iter()
            .filter(|publication| {
                publication.provenance == PublicationProvenance::LedgerOwned
                    && publication.author_agent.as_deref() == Some(id.as_str())
                    && publication.head_branch == branch
            })
            .collect::<Vec<_>>();
        let [publication] = publications.as_slice() else {
            return None;
        };
        if !publication_evidence(&events, &id, publication)
            || !finished_invocation_evidence(&events, &id, publication)
        {
            return None;
        }
        let repository = crate::services::repo::get_repository_identity(&self.project_dir)
            .await
            .ok()?;
        let branch_name = BranchName::try_from_str(&branch).ok()?;
        let local_head = local_branch_state(&self.project_dir, &branch)
            .await
            .ok()??;
        let remote_head = remote_branch_state(&self.project_dir, &repository.remote_name, &branch)
            .await
            .ok()?;
        let receipt_proves_remote_deletion = recovery_entries.iter().any(|entry| {
            entry
                .identity_snapshot
                .as_ref()
                .is_some_and(|identity| identity.agent_name.as_str() == id)
                && entry.recovered_provenance.is_some()
                && entry
                    .actions
                    .iter()
                    .any(|action| action == "delete_remote_branch")
                && entry.branch.as_ref().is_some_and(|evidence| {
                    evidence.remote.status == CleanupBranchActionStatus::Deleted
                        && evidence.remote_name.as_deref() == Some(repository.remote_name.as_str())
                        && evidence.remote_branch.as_deref() == Some(branch.as_str())
                        && evidence.remote_head_sha.as_deref()
                            == Some(publication.head_sha.as_str())
                })
        });
        let remote_matches_publication =
            remote_head.as_deref() == Some(publication.head_sha.as_str());
        if local_head != publication.head_sha
            || !(remote_matches_publication
                || remote_head.is_none() && receipt_proves_remote_deletion)
        {
            return None;
        }
        let forgejo = self.forgejo.as_ref()?;
        let pull_requests = forgejo
            .find_pull_requests_by_head(&repository.owner, &repository.repo, &branch_name)
            .await
            .ok()?;
        let [pull_request] = pull_requests.as_slice() else {
            return None;
        };
        let closed_unmerged =
            !pull_request.merged && pull_request.state.eq_ignore_ascii_case("closed");
        if (!pull_request.merged && !closed_unmerged)
            || pull_request.head_ref.as_str() != branch
            || pull_request.base_ref.as_str() != publication.base_branch
            || pull_request.head_sha.as_deref() != Some(publication.head_sha.as_str())
            || pr_metadata_value(&pull_request.body, "Authoring-Agent").as_deref()
                != Some(id.as_str())
            || pr_metadata_value(&pull_request.body, "Birth-Branch").as_deref()
                != Some(branch.as_str())
        {
            return None;
        }
        let agent_name = AgentName::try_from_str(&id).ok()?;
        let identity_parts = AgentIdentity::from_internal_name(&id);
        if identity_parts.agent_type() != agent_type || identity_parts.internal_name() != agent_name
        {
            return None;
        }
        let identity = AgentIdentityRecord {
            agent_name: agent_name.clone(),
            slug: Slug::try_from_str(identity_parts.slug()).ok()?,
            agent_type,
            birth_branch: BirthBranch::try_from_str(&branch).ok()?,
            parent_branch: BirthBranch::try_from_str(&publication.base_branch).ok()?,
            working_dir: canonical
                .strip_prefix(&self.project_dir)
                .ok()?
                .to_path_buf(),
            display_name: identity_parts.display_name(),
            topology: Topology::WorktreePerAgent,
            model: None,
            effort: None,
            ledger_owned: false,
            slice_id: publication.slice_id.clone(),
        };
        let mut evidence = CleanupRecoveredProvenance {
            identity_sources: vec![
                "published-heads.json:author_agent".to_string(),
                "ledger:agent.spawned.child_agent".to_string(),
                "residual path basename".to_string(),
            ],
            branch_sources: vec![
                "ledger:agent.spawned.branch".to_string(),
                "published-heads.json:head_branch".to_string(),
                "git:refs/heads exact head".to_string(),
                "git:remote exact head".to_string(),
            ],
            pull_request_sources: vec![
                "ledger:pr.published".to_string(),
                "published-heads.json".to_string(),
                "Forgejo unique merged or closed-unmerged pull request".to_string(),
                "Forgejo PR body: Authoring-Agent".to_string(),
                "Forgejo PR body: Birth-Branch".to_string(),
            ],
            liveness_sources: vec![
                "ledger:agent.invocation.finished".to_string(),
                "resolver identity absent".to_string(),
                "agent directory absent".to_string(),
                "configured tmux session window inventory".to_string(),
            ],
        };
        if receipt_proves_remote_deletion {
            evidence
                .branch_sources
                .push("cleanup receipt: exact remote deletion proof".to_string());
        }
        tracing::info!(
            agent = %identity.agent_name,
            branch = %identity.birth_branch,
            pr_number = publication.pr_number,
            "Recovered verified cleanup provenance for residual worktree state"
        );
        Some(DiscoveredResource {
            id: id.clone(),
            agent_dir: self.project_dir.join(".exo/agents").join(&id),
            worktree_path: Some(path.to_path_buf()),
            identity: Some(identity),
            identity_error: None,
            resolver_only: false,
            recovery_receipt: false,
            recovered_provenance: Some(evidence),
        })
    }

    pub(super) async fn revalidate_recovered_residual(
        &self,
        candidate: &super::types::CleanupCandidate,
    ) -> anyhow::Result<()> {
        let identity = candidate
            .identity
            .as_ref()
            .context("recovered identity is unavailable")?;
        if candidate.id != identity.agent_name.as_str()
            || candidate.agent_name != identity.agent_name.as_str()
        {
            anyhow::bail!("recovered identity no longer matches its candidate");
        }
        self.revalidate_recovered_liveness(identity).await?;
        if self.resolver.get(&identity.agent_name).await.is_some() {
            anyhow::bail!("resolver identity appeared for recovered cleanup target");
        }
        if fs::symlink_metadata(&candidate.agent_dir).await.is_ok() {
            anyhow::bail!("agent identity directory appeared for recovered cleanup target");
        }
        let Some(path) = &candidate.worktree_path else {
            anyhow::bail!("recovered residual path is unavailable");
        };
        if !path_within(&self.project_dir.join(".exo/worktrees"), path) {
            anyhow::bail!("recovered residual path escaped the managed root");
        }
        let metadata = match fs::symlink_metadata(path).await {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error).context("inspect recovered residual path"),
        };
        if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
            anyhow::bail!("recovered residual path is not a real directory");
        }
        let canonical = fs::canonicalize(path)
            .await
            .context("canonicalize recovered residual")?;
        let worktree_root = fs::canonicalize(self.project_dir.join(".exo/worktrees"))
            .await
            .context("canonicalize managed worktree root")?;
        if canonical.parent() != Some(worktree_root.as_path()) {
            anyhow::bail!("recovered residual path changed since planning");
        }
        if resolve_path(&self.project_dir, &identity.working_dir) != canonical {
            anyhow::bail!("recovered identity path changed since planning");
        }
        if fs::symlink_metadata(path.join(".git")).await.is_ok() {
            anyhow::bail!("recovered residual became a git worktree");
        }
        if !residual_contents_are_safe(path).await? {
            anyhow::bail!("recovered residual contains non-ExoMonad files");
        }
        if workspace_git_root(path).await.ok().flatten() != Some(self.project_dir.clone()) {
            anyhow::bail!("recovered residual repository context changed");
        }
        Ok(())
    }
}

async fn residual_contents_are_safe(path: &std::path::Path) -> anyhow::Result<bool> {
    let metadata = fs::symlink_metadata(path.join(".exo")).await?;
    if !metadata.file_type().is_dir() || metadata.file_type().is_symlink() {
        return Ok(false);
    }
    let mut entries = fs::read_dir(path).await?;
    while let Some(entry) = entries.next_entry().await? {
        if entry.file_name() != ".exo" {
            return Ok(false);
        }
    }
    Ok(true)
}

fn recovered_spawn_evidence(
    events: &[LedgerRecord],
    agent_name: &str,
) -> Option<(String, AgentType)> {
    let matching = events.iter().filter(|record| {
        record.event.event_type == "agent.spawned"
            && record
                .event
                .data
                .get("child_agent")
                .and_then(|value| value.as_str())
                == Some(agent_name)
    });
    let mut evidence = None;
    for record in matching {
        let branch = record
            .event
            .data
            .get("branch")
            .and_then(|value| value.as_str())?;
        let topology = record
            .event
            .data
            .get("topology")
            .and_then(|value| value.as_str())?;
        if topology != "worktreeperagent" && topology != "worktree_per_agent" {
            return None;
        }
        let agent_type = AgentType::from_dir_name(agent_name);
        let event_type = record
            .event
            .data
            .get("agent_type")
            .and_then(|value| value.as_str())?;
        if event_type != agent_type.suffix()
            && !event_type.eq_ignore_ascii_case(&format!("{agent_type:?}"))
        {
            return None;
        }
        match &evidence {
            Some((known_branch, known_type))
                if known_branch != branch || *known_type != agent_type =>
            {
                return None
            }
            None => evidence = Some((branch.to_string(), agent_type)),
            _ => {}
        }
    }
    evidence
}

fn publication_evidence(
    events: &[LedgerRecord],
    agent_name: &str,
    publication: &PublishedHead,
) -> bool {
    events.iter().any(|record| {
        record.event.event_type == "pr.published"
            && (record.event.agent_id.as_deref() == Some(agent_name)
                || record
                    .event
                    .data
                    .get("agent_id")
                    .and_then(|value| value.as_str())
                    == Some(agent_name))
            && record
                .event
                .data
                .get("pr_number")
                .and_then(|value| value.as_u64())
                == Some(publication.pr_number)
            && record
                .event
                .data
                .get("head_branch")
                .and_then(|value| value.as_str())
                == Some(publication.head_branch.as_str())
            && record
                .event
                .data
                .get("base_branch")
                .and_then(|value| value.as_str())
                == Some(publication.base_branch.as_str())
            && record
                .event
                .data
                .get("head_sha")
                .and_then(|value| value.as_str())
                == Some(publication.head_sha.as_str())
    })
}

fn finished_invocation_evidence(
    events: &[LedgerRecord],
    agent_name: &str,
    publication: &PublishedHead,
) -> bool {
    let Some(invocation_id) = publication.invocation_id.as_deref() else {
        return false;
    };
    events.iter().any(|record| {
        record.event.event_type == "agent.invocation.finished"
            && record.event.agent_id.as_deref() == Some(agent_name)
            && record
                .event
                .data
                .get("invocation_id")
                .and_then(|value| value.as_str())
                == Some(invocation_id)
            && record
                .event
                .data
                .get("outcome")
                .and_then(|value| value.as_str())
                == Some("finished")
            && record
                .event
                .data
                .get("status")
                .and_then(|value| value.as_str())
                != Some("running")
            && optional_string_matches(
                &record.event.data,
                "branch",
                publication.head_branch.as_str(),
            )
            && optional_string_matches(
                &record.event.data,
                "head_sha",
                publication.head_sha.as_str(),
            )
            && publication.slice_id.as_deref().is_none_or(|slice_id| {
                optional_string_matches(&record.event.data, "slice_id", slice_id)
            })
            && optional_u64_matches(&record.event.data, "pr_number", publication.pr_number)
    })
}

fn optional_string_matches(data: &serde_json::Value, key: &str, expected: &str) -> bool {
    data.get(key)
        .and_then(|value| value.as_str())
        .is_none_or(|value| value == expected)
}

fn optional_u64_matches(data: &serde_json::Value, key: &str, expected: u64) -> bool {
    data.get(key)
        .and_then(|value| value.as_u64())
        .is_none_or(|value| value == expected)
}
