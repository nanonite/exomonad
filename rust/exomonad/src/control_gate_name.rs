//! One canonical wire encoding for a gate name on the control route.
//!
//! Per-slice gate names embed the slice id (`tl-dispatch-failed-<slice>`,
//! `tl-dispatch-ownership-conflict-<slice>`, `tl-post-merge-<slice>`), and a
//! slice id may itself contain `/`. `/` is the control route's level separator,
//! so such a gate name cannot be written as a raw path segment. The control
//! route therefore carries exactly one encoding:
//!
//! * A gate name is percent-encoded with the RFC 3986 unreserved set
//!   (`A-Z a-z 0-9 - . _ ~`) preserved and every other byte written as an
//!   uppercase `%XX` triplet. `/` becomes `%2F`.
//! * The route captures `{gate_name}` as one level, so the router decodes the
//!   segment back to the raw name before the handler runs. `encode` is the
//!   exact inverse of that decode.
//! * The CLI takes the raw name directly, so `--name` and the decoded path
//!   segment name the same gate. Encoding is a wire concern, not an identity.
//!
//! This is the same shape as the `topic_vocabulary` segment codec applied to a
//! whole name instead of a topic level. A segment is bounded by `/`, so a raw
//! `/` in the name is data that must survive the round trip rather than a level
//! boundary that must be rejected.

/// The control route template that carries an encoded gate name.
pub const ROUTE: &str = "/runs/{run_id}/gates/{gate_name}";

/// A gate name that cannot be addressed on the control route.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateNameError {
    /// The name is empty.
    Empty,
    /// The name is `.` or `..`.
    PathNavigation,
    /// The name contains NUL or another control character.
    ControlCharacter,
}

impl std::fmt::Display for GateNameError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Empty => formatter.write_str("gate name must not be empty"),
            Self::PathNavigation => formatter.write_str("gate name must not be path navigation"),
            Self::ControlCharacter => {
                formatter.write_str("gate name must not contain NUL or control characters")
            }
        }
    }
}

impl std::error::Error for GateNameError {}

/// Percent-encode one raw gate name into a single path segment.
///
/// The result contains no `/`, so it matches exactly one route level, and the
/// router decodes it back to `name` exactly. The encoding is injective: a
/// literal `%2F` in a name becomes `%252F`, so `a/b` and `a%2Fb` never share a
/// path.
pub fn encode(name: &str) -> String {
    urlencoding::encode(name).into_owned()
}

/// Validate one raw gate name.
///
/// A gate name is an argv value and a JSON key, never a filesystem path, so a
/// `/` inside it is legal and is what makes a per-slice gate addressable. Only
/// the shapes that would be ambiguous or unrepresentable are refused.
pub fn validate(name: &str) -> Result<(), GateNameError> {
    if name.is_empty() {
        return Err(GateNameError::Empty);
    }
    if name == "." || name == ".." {
        return Err(GateNameError::PathNavigation);
    }
    if name.contains(|character: char| character == '\0' || character.is_control()) {
        return Err(GateNameError::ControlCharacter);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_slash_in_a_gate_name_encodes_into_one_path_level() {
        let encoded = encode("tl-dispatch-failed-feat/auth");

        assert_eq!(encoded, "tl-dispatch-failed-feat%2Fauth");
        assert!(
            !encoded.contains('/'),
            "an encoded name is a single route level"
        );
    }

    #[test]
    fn an_ordinary_gate_name_encodes_to_itself() {
        for name in [
            "tl-dispatch-failed-leaf-a",
            "tl-dispatch-ownership-conflict-leaf-a",
            "tl-post-merge-leaf-a",
        ] {
            assert_eq!(encode(name), name, "an ordinary gate name stays readable");
        }
    }

    #[test]
    fn a_literal_escape_encodes_to_a_distinct_path() {
        assert_eq!(
            encode("tl-dispatch-failed-feat%2Fauth"),
            "tl-dispatch-failed-feat%252Fauth"
        );
        assert_ne!(
            encode("tl-dispatch-failed-feat/auth"),
            encode("tl-dispatch-failed-feat%2Fauth"),
            "a slash and a literal escape must not collide on the wire"
        );
    }

    #[test]
    fn an_empty_or_navigating_gate_name_is_refused() {
        assert_eq!(validate(""), Err(GateNameError::Empty));
        assert_eq!(validate("."), Err(GateNameError::PathNavigation));
        assert_eq!(validate(".."), Err(GateNameError::PathNavigation));
    }

    #[test]
    fn a_control_character_in_a_gate_name_is_refused() {
        assert_eq!(validate("gate\nname"), Err(GateNameError::ControlCharacter));
        assert_eq!(validate("gate\0name"), Err(GateNameError::ControlCharacter));
    }

    #[test]
    fn a_slash_is_a_legal_gate_name_character() {
        assert_eq!(validate("tl-dispatch-failed-feat/auth"), Ok(()));
        assert_eq!(
            validate("tl-dispatch-failed-feat/../.."),
            Ok(()),
            "a slash is data, so a slice id that spells navigation is still just a name"
        );
    }
}
