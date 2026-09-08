use super::discovery::DiscoveredResource;
use super::service::VerifiedCleanupService;
use super::types::*;
use crate::services::repo::RepositoryIdentity;

impl VerifiedCleanupService {
    pub(super) async fn inspect_candidate(
        &self,
        resource: DiscoveredResource,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
        current_branch: Option<&str>,
    ) -> CleanupCandidate {
        self.inspect_candidate_facts(resource, repository, repository_error, current_branch)
            .await
            .into_candidate()
    }
}
