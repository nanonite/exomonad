//! Shared continuation context for session resumption and task handoff.
//!
//! This scaffold establishes the module boundary. Typed adapters and
//! deterministic rendering are added by the continuation implementation waves.

/// Marker for the shared continuation service namespace.
#[derive(Debug, Clone, Copy, Default, Eq, PartialEq)]
pub struct Continuation;
