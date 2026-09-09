use super::discovery::DiscoveredResource;
use super::inspection_build::InspectionContext;
use super::service::VerifiedCleanupService;
use super::types::*;

impl VerifiedCleanupService {
    pub(super) async fn inspect_candidate(
        &self,
        resource: DiscoveredResource,
        context: InspectionContext<'_>,
    ) -> CleanupCandidate {
        self.inspect_candidate_facts(resource, context)
            .await
            .into_candidate()
    }
}
