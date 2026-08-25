//! Verb-first topic parsing and durable `in/` guidance mapping.
//!
//! Topics are an addressing view. The durable guidance queue remains the only
//! writer and authority for `in/` delivery.

use super::guidance_queue::{
    GuidanceBatchRequest, GuidanceIdentity, GuidanceItemInput, QueueClass,
};
use anyhow::{bail, Result};

const IN_VERB: &str = "in";

/// Decode one serialized topic segment according to the topic contract.
pub fn decode_segment(encoded: &str) -> Result<String> {
    if encoded.is_empty() {
        bail!("topic segments must not be empty")
    }
    let mut decoded = String::with_capacity(encoded.len());
    let mut chars = encoded.chars();
    while let Some(character) = chars.next() {
        match character {
            '+' | '#' => bail!("topic wildcard must be percent-encoded"),
            '%' => decoded.push(decode_escape(&mut chars)?),
            '\0' => bail!("topic segments must not contain NUL"),
            _ => decoded.push(character),
        }
    }
    validate_decoded_segment(&decoded)?;
    Ok(decoded)
}

fn decode_escape(chars: &mut impl Iterator<Item = char>) -> Result<char> {
    let first = chars.next();
    let second = chars.next();
    match (first, second) {
        (Some('2'), Some('3')) => Ok('#'),
        (Some('2'), Some('5')) => Ok('%'),
        (Some('2'), Some('B')) => Ok('+'),
        _ => bail!("topic percent escape must be one of %23, %25, or %2B"),
    }
}

/// Encode one raw topic segment using the canonical uppercase escapes.
pub fn encode_segment(raw: &str) -> Result<String> {
    validate_decoded_segment(raw)?;
    let mut encoded = String::with_capacity(raw.len());
    for character in raw.chars() {
        match character {
            '+' => encoded.push_str("%2B"),
            '#' => encoded.push_str("%23"),
            '%' => encoded.push_str("%25"),
            _ => encoded.push(character),
        }
    }
    Ok(encoded)
}

fn validate_decoded_segment(segment: &str) -> Result<()> {
    if segment.is_empty() || segment == "." || segment == ".." {
        bail!("topic segments must be non-empty and not path navigation")
    }
    if segment.contains('/') || segment.chars().any(|character| character == '\0') {
        bail!("topic segments must not contain slash or NUL")
    }
    Ok(())
}

/// Parse and validate a serialized topic into decoded segments.
pub fn parse_topic(topic: &str) -> Result<Vec<String>> {
    let segments = topic
        .split('/')
        .map(decode_segment)
        .collect::<Result<Vec<_>>>()?;
    if segments.len() < 3 {
        bail!("topic must contain verb, category, and noun")
    }
    if !matches!(segments[0].as_str(), "in" | "obs" | "signal") {
        bail!("unsupported topic verb `{}`", segments[0])
    }
    Ok(segments)
}

/// Serialize decoded segments into one canonical topic.
pub fn serialize_topic(segments: &[&str]) -> Result<String> {
    if segments.len() < 3 {
        bail!("topic must contain verb, category, and noun")
    }
    if !matches!(segments[0], "in" | "obs" | "signal") {
        bail!("unsupported topic verb `{}`", segments[0])
    }
    segments
        .iter()
        .map(|segment| encode_segment(segment))
        .collect::<Result<Vec<_>>>()
        .map(|encoded| encoded.join("/"))
}

/// A durable guidance destination represented by an `in/` topic.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InTopic {
    pub agent_id: String,
    pub queue_class: QueueClass,
}

impl InTopic {
    /// Parse exactly `in/agent/<agent_id>/{steering|follow_up}`.
    pub fn parse(topic: &str) -> Result<Self> {
        let segments = parse_topic(topic)?;
        if segments.len() != 4 || segments[0] != IN_VERB || segments[1] != "agent" {
            bail!("in topic must be in/agent/<agent_id>/{{steering|follow_up}}")
        }
        Ok(Self {
            agent_id: segments[2].clone(),
            queue_class: QueueClass::parse(&segments[3])?,
        })
    }

    pub fn topic(&self) -> Result<String> {
        serialize_topic(&[
            IN_VERB,
            "agent",
            self.agent_id.as_str(),
            self.queue_class.as_str(),
        ])
    }

    pub fn into_request(
        self,
        items: Vec<GuidanceItemInput>,
        identity: GuidanceIdentity,
        idempotency_key: Option<String>,
        source_message_id: Option<i64>,
    ) -> GuidanceBatchRequest {
        GuidanceBatchRequest {
            agent_id: self.agent_id,
            queue_class: self.queue_class,
            items,
            identity,
            idempotency_key,
            source_message_id,
        }
    }
}

/// An observation topic that names one existing ledger event identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObsTopic {
    pub event_type: String,
}

impl ObsTopic {
    /// Parse exactly `obs/event/<event_type>`.
    ///
    /// The registry counterpart is checked by the observability contract gate;
    /// this parser only enforces the topic shape and preserves the identity.
    pub fn parse(topic: &str) -> Result<Self> {
        let segments = parse_topic(topic)?;
        if segments.len() != 3 || segments[0] != "obs" || segments[1] != "event" {
            bail!("obs topic must be obs/event/<event_type>")
        }
        Ok(Self {
            event_type: segments[2].clone(),
        })
    }

    pub fn topic(&self) -> Result<String> {
        serialize_topic(&["obs", "event", self.event_type.as_str()])
    }
}

const PARK_CAUSES: &[&str] = &[
    "retries_exhausted",
    "budget_exhausted",
    "no_capable_harness",
    "schedule_deadlock",
    "review_stuck",
    "review_rounds_exhausted",
    "harness_switch_requested",
    "stall_detected",
];

/// An immediate park signal. It is a presentation of durable TL state, not a
/// queue item and not a gate mutation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParkSignal {
    pub run_id: String,
    pub cause: String,
}

impl ParkSignal {
    /// Parse exactly `signal/park/<run_id>/<cause>`.
    pub fn parse(topic: &str) -> Result<Self> {
        let segments = parse_topic(topic)?;
        if segments.len() != 4 || segments[0] != "signal" || segments[1] != "park" {
            bail!("park signal must be signal/park/<run_id>/<cause>")
        }
        if !PARK_CAUSES.contains(&segments[3].as_str()) {
            bail!("unsupported park cause `{}`", segments[3])
        }
        Ok(Self {
            run_id: segments[2].clone(),
            cause: segments[3].clone(),
        })
    }

    pub fn topic(&self) -> Result<String> {
        serialize_topic(&["signal", "park", self.run_id.as_str(), self.cause.as_str()])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{GuidanceItemInput, InboxStore};
    use anyhow::Result;
    use serde_json::Value;
    use tempfile::TempDir;

    fn item(content: &str) -> GuidanceItemInput {
        GuidanceItemInput {
            from_agent: "operator".to_string(),
            content: content.to_string(),
            summary: None,
            injection_options: Value::Null,
        }
    }

    #[test]
    fn in_topic_decodes_and_round_trips_reserved_characters() -> Result<()> {
        let destination = InTopic::parse("in/agent/codex%2Breview%231%25/steering")?;
        assert_eq!(destination.agent_id, "codex+review#1%");
        assert_eq!(destination.queue_class, QueueClass::Steering);
        assert_eq!(
            destination.topic()?,
            "in/agent/codex%2Breview%231%25/steering"
        );
        Ok(())
    }

    #[test]
    fn in_topic_maps_directly_to_the_durable_queue() -> Result<()> {
        let directory = TempDir::new()?;
        let store = InboxStore::open(directory.path())?;
        let result = store.enqueue_in_topic(
            "in/agent/agent-a/follow_up",
            vec![item("follow up")],
            GuidanceIdentity::default(),
            Some("topic-key".to_string()),
            None,
        )?;

        assert!(result.created);
        assert_eq!(result.batch.agent_id, "agent-a");
        assert_eq!(result.batch.queue_class, QueueClass::FollowUp);
        assert_eq!(result.batch.items[0].content, "follow up");
        assert!(!store.has_unread("agent-a")?);
        Ok(())
    }

    #[test]
    fn obs_topic_preserves_the_existing_event_identity() -> Result<()> {
        let topic = ObsTopic::parse("obs/event/pr.review")?;
        assert_eq!(topic.event_type, "pr.review");
        assert_eq!(topic.topic()?, "obs/event/pr.review");
        Ok(())
    }

    #[test]
    fn park_signal_maps_to_a_closed_cause_without_queue_semantics() -> Result<()> {
        let signal = ParkSignal::parse("signal/park/run%2B1/review_stuck")?;
        assert_eq!(signal.run_id, "run+1");
        assert_eq!(signal.cause, "review_stuck");
        assert_eq!(signal.topic()?, "signal/park/run%2B1/review_stuck");
        Ok(())
    }

    #[test]
    fn parser_rejects_other_verbs_shapes_and_unescaped_wildcards() {
        for topic in [
            "out/agent/agent-a/steering",
            "in/agent/agent-a/steering/extra",
            "in/agent/agent-a/out",
            "in/agent/agent+a/steering",
            "in/agent/agent%2b/steering",
            "in//agent-a/steering",
            "signal/park/run/unknown_cause",
            "signal/gate/run/tl-timeout",
        ] {
            assert!(
                InTopic::parse(topic).is_err(),
                "accepted invalid topic {topic}"
            );
        }
    }
}
