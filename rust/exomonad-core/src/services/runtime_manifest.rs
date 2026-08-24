//! Shared runtime compatibility identities exposed by the server and init.

use serde::Serialize;

pub const RUNTIME_MANIFEST_VERSION: u32 = 1;
pub const RUNTIME_PROTOCOL_VERSION: u32 = 1;
pub const PUBLICATION_REGISTRY_SCHEMA_VERSION: u32 = 2;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RuntimeManifest<'a> {
    pub manifest_version: u32,
    pub server_build: &'a str,
    pub controller_build: &'a str,
    pub protocol_version: u32,
    pub publication_registry_schema_version: u32,
}

impl<'a> RuntimeManifest<'a> {
    pub fn new(server_build: &'a str, controller_build: &'a str) -> Self {
        Self {
            manifest_version: RUNTIME_MANIFEST_VERSION,
            server_build,
            controller_build,
            protocol_version: RUNTIME_PROTOCOL_VERSION,
            publication_registry_schema_version: PUBLICATION_REGISTRY_SCHEMA_VERSION,
        }
    }
}
