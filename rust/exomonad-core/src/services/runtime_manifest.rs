//! Shared runtime compatibility identities exposed by the server and init.

use serde::Serialize;

pub const RUNTIME_MANIFEST_VERSION: u32 = 1;
pub const RUNTIME_PROTOCOL_VERSION: u32 = 1;
pub const PUBLICATION_REGISTRY_SCHEMA_VERSION: u32 = 2;
pub const PLAN_COMPATIBILITY_VERSION: u32 = 2;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RuntimeManifest<'a> {
    pub manifest_version: u32,
    pub server_build: &'a str,
    pub controller_build: &'a str,
    pub protocol_version: u32,
    pub publication_registry_schema_version: u32,
    pub plan_compatibility_version: u32,
}

impl<'a> RuntimeManifest<'a> {
    pub fn new(server_build: &'a str, controller_build: &'a str) -> Self {
        Self {
            manifest_version: RUNTIME_MANIFEST_VERSION,
            server_build,
            controller_build,
            protocol_version: RUNTIME_PROTOCOL_VERSION,
            publication_registry_schema_version: PUBLICATION_REGISTRY_SCHEMA_VERSION,
            plan_compatibility_version: PLAN_COMPATIBILITY_VERSION,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{RuntimeManifest, PLAN_COMPATIBILITY_VERSION};

    #[test]
    fn runtime_manifest_publishes_state_compatibility_separately_from_builds() {
        let manifest = RuntimeManifest::new("server-build", "controller-build");
        let serialized = serde_json::to_value(manifest).unwrap();

        assert_eq!(serialized["server_build"], "server-build");
        assert_eq!(serialized["controller_build"], "controller-build");
        assert_eq!(
            serialized["plan_compatibility_version"],
            PLAN_COMPATIBILITY_VERSION
        );
    }
}
