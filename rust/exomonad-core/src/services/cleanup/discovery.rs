use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::AgentName;
use crate::services::agent_control::Topology;
use crate::services::agent_resolver::AgentIdentityRecord;
use anyhow::{bail, Context, Result};
use std::path::PathBuf;
use tokio::fs;

pub(super) struct DiscoveredResource {
    pub(super) id: String,
    pub(super) agent_dir: PathBuf,
    pub(super) worktree_path: Option<PathBuf>,
    pub(super) identity: Option<AgentIdentityRecord>,
    pub(super) identity_error: Option<String>,
    pub(super) resolver_only: bool,
    pub(super) recovery_receipt: bool,
}

impl VerifiedCleanupService {
    pub(super) async fn discover_resources(
        &self,
        request: &CleanupRequest,
    ) -> Result<Vec<DiscoveredResource>> {
        let agents_dir = self.project_dir.join(".exo/agents");
        let mut resources = Vec::new();
        let resolver_records = self.resolver.all().await;
        let recovery_candidates = self.in_progress_identity_keys().await?;
        let agent_entries = match fs::read_dir(&agents_dir).await {
            Ok(entries) => Some(entries),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| format!("read {}", agents_dir.display()));
            }
        };
        if let Some(mut entries) = agent_entries {
            while let Some(entry) = entries.next_entry().await? {
                let file_type = entry.file_type().await?;
                if !file_type.is_dir() || file_type.is_symlink() {
                    continue;
                }
                let Some(entry_name) = entry.file_name().to_str().map(str::to_owned) else {
                    continue;
                };
                let (identity, identity_error) = self.authoritative_identity(&entry_name).await;
                resources.push(DiscoveredResource {
                    id: entry_name,
                    agent_dir: entry.path(),
                    worktree_path: identity.as_ref().and_then(|identity| {
                        (identity.topology == Topology::WorktreePerAgent)
                            .then(|| resolve_path(&self.project_dir, &identity.working_dir))
                    }),
                    identity,
                    identity_error,
                    resolver_only: false,
                    recovery_receipt: false,
                });
            }
        }

        let worktrees_dir = self.project_dir.join(".exo/worktrees");
        let worktree_entries = match fs::read_dir(&worktrees_dir).await {
            Ok(entries) => Some(entries),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => {
                return Err(error).with_context(|| format!("read {}", worktrees_dir.display()));
            }
        };
        if let Some(mut entries) = worktree_entries {
            while let Some(entry) = entries.next_entry().await? {
                let file_type = entry.file_type().await?;
                if !file_type.is_dir() || file_type.is_symlink() {
                    continue;
                }
                self.attach_worktree_resource(&mut resources, entry.path(), &resolver_records)
                    .await;
            }
        }

        self.append_resolver_only_resources(
            &mut resources,
            &resolver_records,
            &recovery_candidates,
        );

        resources.retain(|resource| {
            requested_target_matches(
                request.target.as_deref(),
                &resource.id,
                resource.identity.as_ref(),
            )
        });
        if let Some(target) = &request.target {
            if resources.is_empty() && !recovery_candidates.contains(target) {
                bail!("managed cleanup target {:?} was not found", target);
            }
        }
        resources.sort_by(|left, right| left.id.cmp(&right.id));
        Ok(resources)
    }

    async fn authoritative_identity(
        &self,
        entry_name: &str,
    ) -> (Option<AgentIdentityRecord>, Option<String>) {
        let identity_path = self
            .project_dir
            .join(".exo/agents")
            .join(entry_name)
            .join("identity.json");
        let disk_contents = fs::read_to_string(&identity_path).await;
        let disk_identity = disk_contents
            .as_ref()
            .ok()
            .and_then(|contents| serde_json::from_str::<AgentIdentityRecord>(contents).ok());
        let resolver_identity = if let Ok(name) = AgentName::try_from_str(entry_name) {
            self.resolver.get(&name).await
        } else if let Some(identity) = &disk_identity {
            self.resolver.get(&identity.agent_name).await
        } else {
            None
        };

        match (disk_identity, resolver_identity) {
            (Some(disk), Some(canonical)) if disk == canonical => (Some(canonical), None),
            (Some(_), Some(canonical)) => (
                Some(canonical),
                Some("identity.json disagrees with the AgentResolver record".to_string()),
            ),
            (Some(_), None) => (
                None,
                Some("identity is not registered in the AgentResolver".to_string()),
            ),
            (None, Some(canonical)) => (
                Some(canonical),
                Some("identity.json is missing or malformed".to_string()),
            ),
            (None, None) => (
                None,
                Some("identity.json is missing or malformed".to_string()),
            ),
        }
    }

    async fn attach_worktree_resource(
        &self,
        resources: &mut Vec<DiscoveredResource>,
        path: PathBuf,
        resolver_records: &[AgentIdentityRecord],
    ) {
        let worktree_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default();
        let canonical_path = fs::canonicalize(&path).await.ok();
        let mut match_index = None;
        for (index, resource) in resources.iter().enumerate() {
            if resource.id == worktree_name && resource.worktree_path.is_none() {
                match_index = Some(index);
                break;
            }
            let Some(candidate_path) = &resource.worktree_path else {
                continue;
            };
            if candidate_path == &path
                || fs::canonicalize(candidate_path)
                    .await
                    .ok()
                    .zip(canonical_path.as_ref())
                    .is_some_and(|(candidate, actual)| candidate == *actual)
            {
                match_index = Some(index);
                break;
            }
        }
        if let Some(index) = match_index {
            resources[index].worktree_path = Some(path);
            return;
        }

        let Some(id) = path.file_name().and_then(|name| name.to_str()) else {
            return;
        };
        let matching_identity = resolver_records
            .iter()
            .find(|identity| {
                identity.topology == Topology::WorktreePerAgent
                    && resolve_path(&self.project_dir, &identity.working_dir) == path
            })
            .cloned();
        resources.push(DiscoveredResource {
            id: id.to_string(),
            agent_dir: matching_identity
                .as_ref()
                .map(|identity| {
                    self.project_dir
                        .join(".exo/agents")
                        .join(identity.agent_name.as_str())
                })
                .unwrap_or_else(|| self.project_dir.join(".exo/agents").join(id)),
            worktree_path: Some(path),
            identity: matching_identity,
            identity_error: Some("worktree is not backed by a verified identity".to_string()),
            resolver_only: false,
            recovery_receipt: false,
        });
    }

    fn append_resolver_only_resources(
        &self,
        resources: &mut Vec<DiscoveredResource>,
        resolver_records: &[AgentIdentityRecord],
        recovery_candidates: &std::collections::HashSet<String>,
    ) {
        for identity in resolver_records {
            let agent_dir = self
                .project_dir
                .join(".exo/agents")
                .join(identity.agent_name.as_str());
            let worktree_path = (identity.topology == Topology::WorktreePerAgent)
                .then(|| resolve_path(&self.project_dir, &identity.working_dir));
            let represented = resources.iter().any(|resource| {
                resource.id == identity.agent_name.as_str()
                    || resource.agent_dir == agent_dir
                    || resource.identity.as_ref() == Some(identity)
                    || resource
                        .worktree_path
                        .as_ref()
                        .zip(worktree_path.as_ref())
                        .is_some_and(|(resource_path, expected_path)| {
                            resource_path == expected_path
                        })
            });
            if represented {
                continue;
            }
            resources.push(DiscoveredResource {
                id: identity.agent_name.to_string(),
                agent_dir,
                worktree_path,
                identity: Some(identity.clone()),
                identity_error: None,
                resolver_only: true,
                recovery_receipt: recovery_candidates.contains(identity.agent_name.as_str()),
            });
        }
    }
}
