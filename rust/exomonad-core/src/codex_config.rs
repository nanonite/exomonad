use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const EXOMONAD_CODEX_HOOKS_BEGIN: &str = "# BEGIN EXOMONAD CODEX HOOKS";
const EXOMONAD_CODEX_HOOKS_END: &str = "# END EXOMONAD CODEX HOOKS";

pub const CODEX_CONFIG_TEMPLATE: &str = r#"{model_config}approval_policy = "never"
developer_instructions = """
{instructions}
"""

[features]
hooks = true

[[hooks.PreToolUse]]
matcher = "*"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "exomonad hook pre-tool-use --runtime codex"
timeout = 600
async = false

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "exomonad hook post-tool-use --runtime codex"
timeout = 600
async = false

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "exomonad hook stop --runtime codex"
timeout = 600
async = false

{mcp_servers}
"#;

pub fn render_codex_config(
    agent_name: &str,
    role: &str,
    instructions: &str,
    model: Option<&str>,
    extra_mcp_servers: &HashMap<String, Value>,
) -> String {
    let mut mcp_servers = toml::map::Map::new();
    mcp_servers.insert(
        "exomonad".to_string(),
        toml::Value::Table(exomonad_mcp_server(agent_name, role)),
    );

    let mut extra_names = extra_mcp_servers.keys().collect::<Vec<_>>();
    extra_names.sort();
    for name in extra_names {
        if name == "exomonad" {
            continue;
        }
        if let Some(server) = extra_mcp_servers
            .get(name)
            .and_then(extra_mcp_server_to_toml)
        {
            mcp_servers.insert(name.clone(), toml::Value::Table(server));
        }
    }

    CODEX_CONFIG_TEMPLATE
        .replace("{model_config}", &model_config_toml(model))
        .replace(
            "{instructions}",
            &escape_multiline_basic_string(instructions),
        )
        .replace("{mcp_servers}", &mcp_servers_to_toml(&mcp_servers))
}

pub fn codex_user_config_path() -> Option<PathBuf> {
    std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .or_else(|| dirs::home_dir().map(|home| home.join(".codex")))
        .map(|home| home.join("config.toml"))
}

/// Mark `project_path` as trusted in the Codex user config so Codex loads the
/// project-local `.codex/config.toml` (hooks + MCP) without prompting.
/// Also strips any legacy global exomonad hook block to prevent duplicate hook execution.
pub fn trust_codex_project(config_path: &Path, project_path: &Path) -> std::io::Result<()> {
    if let Some(parent) = config_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let existing = std::fs::read_to_string(config_path).unwrap_or_default();
    let cleaned = strip_exomonad_codex_hooks_block(&existing);

    let project_str = project_path.display().to_string();
    let header = format!("[projects.\"{}\"]", escape_toml_quoted_key(&project_str));
    let already_trusted = cleaned.lines().any(|l| l.trim() == header);

    let mut next = cleaned.trim_end().to_string();
    if already_trusted {
        if next == existing.trim_end() {
            return Ok(());
        }
    } else {
        if !next.is_empty() {
            next.push_str("\n\n");
        }
        next.push_str(&format!("{header}\ntrust_level = \"trusted\"\n"));
    }
    next.push('\n');
    std::fs::write(config_path, next)
}

fn strip_exomonad_codex_hooks_block(input: &str) -> String {
    let mut output = Vec::new();
    let mut in_block = false;
    for line in input.lines() {
        if line.trim() == EXOMONAD_CODEX_HOOKS_BEGIN {
            in_block = true;
            continue;
        }
        if line.trim() == EXOMONAD_CODEX_HOOKS_END {
            in_block = false;
            continue;
        }
        if !in_block {
            output.push(line);
        }
    }
    output.join("\n")
}

fn escape_toml_quoted_key(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn model_config_toml(model: Option<&str>) -> String {
    match model.filter(|value| !value.is_empty()) {
        Some(model) => {
            let mut root = toml::map::Map::new();
            root.insert("model".to_string(), toml::Value::String(model.to_string()));
            let mut rendered = toml::to_string(&toml::Value::Table(root))
                .expect("Codex model config should serialize");
            rendered.push('\n');
            rendered
        }
        None => String::new(),
    }
}

fn exomonad_mcp_server(agent_name: &str, role: &str) -> toml::map::Map<String, toml::Value> {
    let mut server = toml::map::Map::new();
    server.insert(
        "command".to_string(),
        toml::Value::String("exomonad".to_string()),
    );
    server.insert(
        "args".to_string(),
        toml::Value::Array(
            ["mcp-stdio", "--role", role, "--name", agent_name]
                .into_iter()
                .map(|value| toml::Value::String(value.to_string()))
                .collect(),
        ),
    );
    server
}

fn extra_mcp_server_to_toml(value: &Value) -> Option<toml::map::Map<String, toml::Value>> {
    let Value::Object(object) = value else {
        return None;
    };

    let mut server = toml::map::Map::new();
    for (key, value) in object {
        if key == "type" {
            continue;
        }
        if let Some(value) = json_to_toml(value) {
            server.insert(key.clone(), value);
        }
    }

    (!server.is_empty()).then_some(server)
}

fn json_to_toml(value: &Value) -> Option<toml::Value> {
    match value {
        Value::Null => None,
        Value::Bool(value) => Some(toml::Value::Boolean(*value)),
        Value::Number(value) => value
            .as_i64()
            .map(toml::Value::Integer)
            .or_else(|| value.as_f64().map(toml::Value::Float)),
        Value::String(value) => Some(toml::Value::String(value.clone())),
        Value::Array(values) => values
            .iter()
            .map(json_to_toml)
            .collect::<Option<Vec<_>>>()
            .map(toml::Value::Array),
        Value::Object(values) => {
            let mut table = toml::map::Map::new();
            for (key, value) in values {
                if let Some(value) = json_to_toml(value) {
                    table.insert(key.clone(), value);
                }
            }
            Some(toml::Value::Table(table))
        }
    }
}

fn mcp_servers_to_toml(servers: &toml::map::Map<String, toml::Value>) -> String {
    let mut root = toml::map::Map::new();
    root.insert(
        "mcp_servers".to_string(),
        toml::Value::Table(servers.clone()),
    );
    toml::to_string_pretty(&toml::Value::Table(root))
        .expect("Codex MCP server config should serialize")
        .trim()
        .to_string()
}

fn escape_multiline_basic_string(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace("\"\"\"", "\\\"\\\"\\\"")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn renders_codex_config_with_hooks_and_mcp() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
        );

        assert!(config.contains("approval_policy = \"never\""));
        assert!(config.contains("developer_instructions = \"\"\"\nUse ExoMonad tools.\n\"\"\""));
        assert!(config.contains("[features]\nhooks = true"));

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert_eq!(
            parsed["mcp_servers"]["exomonad"]["command"].as_str(),
            Some("exomonad")
        );
        assert_eq!(
            parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"].as_str(),
            Some("exomonad hook pre-tool-use --runtime codex")
        );
        assert_eq!(
            parsed["hooks"]["Stop"][0]["hooks"][0]["command"].as_str(),
            Some("exomonad hook stop --runtime codex")
        );
    }

    #[test]
    fn renders_extra_mcp_servers_as_codex_tables() {
        let mut extra = HashMap::new();
        extra.insert(
            "docs".to_string(),
            json!({
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                "env": {"DOCS_ROOT": "/tmp/docs"}
            }),
        );

        let config = render_codex_config("agent", "tl", "Plan.", None, &extra);

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        let docs = &parsed["mcp_servers"]["docs"];
        assert_eq!(docs["command"].as_str(), Some("npx"));
        assert_eq!(
            docs["args"].as_array().unwrap(),
            &[
                toml::Value::String("-y".to_string()),
                toml::Value::String("@modelcontextprotocol/server-filesystem".to_string()),
            ]
        );
        assert_eq!(docs["env"]["DOCS_ROOT"].as_str(), Some("/tmp/docs"));
        assert!(docs.get("type").is_none());
    }

    #[test]
    fn renders_model_when_provided() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            Some("gpt-5.2"),
            &HashMap::new(),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.2"));
        assert!(config.starts_with("model = \"gpt-5.2\"\n\napproval_policy"));
    }

    #[test]
    fn omits_model_when_not_provided() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert!(parsed.get("model").is_none());
    }

    #[test]
    fn trust_codex_project_appends_trust_entry() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("config.toml");
        let project_path = Path::new("/tmp/my-project");

        std::fs::write(&config_path, "model = \"gpt-5.5\"\n").unwrap();
        trust_codex_project(&config_path, project_path).unwrap();

        let result = std::fs::read_to_string(&config_path).unwrap();
        assert!(result.contains("[projects.\"/tmp/my-project\"]"));
        assert!(result.contains("trust_level = \"trusted\""));
        let parsed: toml::Value = toml::from_str(&result).expect("valid TOML after trust");
        assert_eq!(
            parsed["projects"]["/tmp/my-project"]["trust_level"].as_str(),
            Some("trusted")
        );
    }

    #[test]
    fn trust_codex_project_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("config.toml");
        let project_path = Path::new("/tmp/my-project");

        trust_codex_project(&config_path, project_path).unwrap();
        let after_first = std::fs::read_to_string(&config_path).unwrap();
        trust_codex_project(&config_path, project_path).unwrap();
        let after_second = std::fs::read_to_string(&config_path).unwrap();

        assert_eq!(after_first, after_second);
        assert_eq!(
            after_first
                .lines()
                .filter(|l| l.contains("trust_level"))
                .count(),
            1
        );
    }

    #[test]
    fn trust_codex_project_strips_legacy_global_hook_block() {
        let dir = tempfile::tempdir().unwrap();
        let config_path = dir.path().join("config.toml");
        let legacy = "model = \"gpt-5.5\"\n\n\
            # BEGIN EXOMONAD CODEX HOOKS\n\
            [[hooks.Stop]]\n\
            # END EXOMONAD CODEX HOOKS\n\
            \napproval_policy = \"never\"\n";
        std::fs::write(&config_path, legacy).unwrap();

        trust_codex_project(&config_path, Path::new("/tmp/p")).unwrap();

        let result = std::fs::read_to_string(&config_path).unwrap();
        assert!(!result.contains("BEGIN EXOMONAD CODEX HOOKS"));
        assert!(result.contains("trust_level = \"trusted\""));
        toml::from_str::<toml::Value>(&result).expect("valid TOML after cleanup");
    }
}
