#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeKind {
    Codex,
    Gemini,
    OpenCode,
    Unknown,
}

pub fn runtime_from_agent_name(agent_name: &str) -> RuntimeKind {
    if agent_name.ends_with("-codex") {
        RuntimeKind::Codex
    } else if agent_name.ends_with("-opencode") {
        RuntimeKind::OpenCode
    } else if agent_name.ends_with("-gemini") || !agent_name.contains('-') {
        RuntimeKind::Gemini
    } else {
        RuntimeKind::Unknown
    }
}

pub fn positive_consumption_signal(runtime: RuntimeKind, before: &str, after: &str) -> bool {
    let before = normalize(before);
    let after = normalize(after);
    if before == after {
        return false;
    }

    match runtime {
        RuntimeKind::Codex => codex_signal(&before, &after),
        RuntimeKind::Gemini => gemini_signal(&before, &after),
        RuntimeKind::OpenCode => opencode_signal(&before, &after),
        RuntimeKind::Unknown => false,
    }
}

fn normalize(text: &str) -> String {
    text.replace('\r', "").to_ascii_lowercase()
}

fn codex_signal(before: &str, after: &str) -> bool {
    gained_marker(before, after, &["assistant", "tokens", "codex", "thinking"])
}

fn gemini_signal(before: &str, after: &str) -> bool {
    gained_marker(before, after, &["gemini", "model", "responding", "turn"])
}

fn opencode_signal(before: &str, after: &str) -> bool {
    gained_marker(
        before,
        after,
        &["opencode", "assistant", "session", "tokens"],
    )
}

fn gained_marker(before: &str, after: &str, markers: &[&str]) -> bool {
    markers
        .iter()
        .any(|marker| after.contains(marker) && !before.contains(marker))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_positive_signal_detects_new_assistant_turn() {
        assert!(positive_consumption_signal(
            RuntimeKind::Codex,
            "user input pending",
            "user input pending
assistant
I can help with that",
        ));
    }

    #[test]
    fn gemini_positive_signal_detects_model_response() {
        assert!(positive_consumption_signal(
            RuntimeKind::Gemini,
            "> prompt",
            "> prompt
Gemini
response text",
        ));
    }

    #[test]
    fn opencode_positive_signal_detects_session_response() {
        assert!(positive_consumption_signal(
            RuntimeKind::OpenCode,
            "waiting",
            "waiting
opencode session
assistant response",
        ));
    }

    #[test]
    fn unknown_runtime_falls_back_to_timeout_without_false_positive() {
        assert!(!positive_consumption_signal(
            RuntimeKind::Unknown,
            "before",
            "before
assistant response",
        ));
    }
}
