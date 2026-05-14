use serde_json::Value;
use sha2::Digest;
use std::collections::HashMap;
use std::path::Path;

pub const CODEX_HOOKS_JSON: &str = r#"{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "exomonad hook pre-tool-use --runtime codex",
            "timeout": 600,
            "async": false
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "exomonad hook post-tool-use --runtime codex",
            "timeout": 600,
            "async": false
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "exomonad hook stop --runtime codex",
            "timeout": 600,
            "async": false
          }
        ]
      }
    ]
  }
}"#;

pub const CODEX_CONFIG_TEMPLATE: &str = r#"{model_config}approval_policy = "never"
developer_instructions = """
{instructions}
"""

[features]
hooks = true

{hook_state}
{mcp_servers}
"#;

pub fn render_codex_config(
    agent_name: &str,
    role: &str,
    instructions: &str,
    model: Option<&str>,
    extra_mcp_servers: &HashMap<String, Value>,
    hooks_json_path: &Path,
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
        .replace(
            "{hook_state}",
            &codex_generated_hook_state_toml(hooks_json_path),
        )
        .replace("{mcp_servers}", &mcp_servers_to_toml(&mcp_servers))
}

fn codex_generated_hook_state_toml(hooks_json_path: &Path) -> String {
    let source = hooks_json_path.display().to_string();
    let hooks = [
        (
            "pre_tool_use",
            Some("*"),
            "exomonad hook pre-tool-use --runtime codex",
        ),
        (
            "post_tool_use",
            Some("*"),
            "exomonad hook post-tool-use --runtime codex",
        ),
        ("stop", None, "exomonad hook stop --runtime codex"),
    ];

    let mut state = String::new();
    for (index, (event_key, matcher, command)) in hooks.into_iter().enumerate() {
        let key = format!("{source}:{event_key}:0:0");
        let hash = generated_command_hook_hash(event_key, matcher, command);
        if index > 0 {
            state.push('\n');
        }
        state.push_str(&format!(
            "[hooks.state.\"{}\"]\ntrusted_hash = \"{}\"\n",
            escape_toml_quoted_key(&key),
            hash
        ));
    }
    state.push('\n');
    state
}

fn generated_command_hook_hash(event_name: &str, matcher: Option<&str>, command: &str) -> String {
    let mut handler = serde_json::Map::new();
    handler.insert("type".to_string(), Value::String("command".to_string()));
    handler.insert("command".to_string(), Value::String(command.to_string()));
    handler.insert("timeout".to_string(), Value::Number(600.into()));
    handler.insert("async".to_string(), Value::Bool(false));

    let mut group = serde_json::Map::new();
    if let Some(matcher) = matcher {
        group.insert("matcher".to_string(), Value::String(matcher.to_string()));
    }
    group.insert(
        "hooks".to_string(),
        Value::Array(vec![Value::Object(handler)]),
    );

    let mut identity = serde_json::Map::new();
    identity.insert(
        "event_name".to_string(),
        Value::String(event_name.to_string()),
    );
    identity.insert("group".to_string(), Value::Object(group));

    version_for_json(&Value::Object(identity))
}

fn version_for_json(value: &Value) -> String {
    let canonical = canonical_json(value);
    let serialized = serde_json::to_vec(&canonical).expect("canonical JSON should serialize");
    let hash = sha2::Sha256::digest(serialized);
    let hex = hash
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!("sha256:{hex}")
}

fn canonical_json(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut sorted = serde_json::Map::new();
            let mut keys = map.keys().collect::<Vec<_>>();
            keys.sort();
            for key in keys {
                if let Some(value) = map.get(key) {
                    sorted.insert(key.clone(), canonical_json(value));
                }
            }
            Value::Object(sorted)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonical_json).collect()),
        other => other.clone(),
    }
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
    fn renders_codex_config_with_exomonad_mcp_and_instructions() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            Path::new("/tmp/repo/.codex/hooks.json"),
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
            parsed["mcp_servers"]["exomonad"]["args"]
                .as_array()
                .unwrap(),
            &[
                toml::Value::String("mcp-stdio".to_string()),
                toml::Value::String("--role".to_string()),
                toml::Value::String("dev".to_string()),
                toml::Value::String("--name".to_string()),
                toml::Value::String("worker-1-codex".to_string()),
            ]
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

        let config = render_codex_config(
            "agent",
            "tl",
            "Plan.",
            None,
            &extra,
            Path::new("/tmp/repo/.codex/hooks.json"),
        );

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
            Path::new("/tmp/repo/.codex/hooks.json"),
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
            Path::new("/tmp/repo/.codex/hooks.json"),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert!(parsed.get("model").is_none());
    }

    #[test]
    fn renders_trusted_state_for_generated_hooks() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            Path::new("/tmp/repo/.codex/hooks.json"),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        let state = parsed["hooks"]["state"]
            .as_table()
            .expect("hook state table");
        for key in [
            "/tmp/repo/.codex/hooks.json:pre_tool_use:0:0",
            "/tmp/repo/.codex/hooks.json:post_tool_use:0:0",
            "/tmp/repo/.codex/hooks.json:stop:0:0",
        ] {
            let trusted_hash = state[key]["trusted_hash"].as_str().expect("trusted hash");
            assert!(trusted_hash.starts_with("sha256:"));
        }
    }

    #[test]
    fn trusted_hook_hashes_match_generated_hooks_json() {
        let hooks: Value = serde_json::from_str(CODEX_HOOKS_JSON).expect("valid hooks json");
        let hooks = hooks["hooks"].as_object().expect("hooks object");

        for (codex_name, event_key) in [
            ("PreToolUse", "pre_tool_use"),
            ("PostToolUse", "post_tool_use"),
            ("Stop", "stop"),
        ] {
            let group = hooks[codex_name][0].clone();
            let mut identity = serde_json::Map::new();
            identity.insert(
                "event_name".to_string(),
                Value::String(event_key.to_string()),
            );
            identity.insert("group".to_string(), group);

            let actual = version_for_json(&Value::Object(identity));
            let expected = match event_key {
                "pre_tool_use" => generated_command_hook_hash(
                    "pre_tool_use",
                    Some("*"),
                    "exomonad hook pre-tool-use --runtime codex",
                ),
                "post_tool_use" => generated_command_hook_hash(
                    "post_tool_use",
                    Some("*"),
                    "exomonad hook post-tool-use --runtime codex",
                ),
                "stop" => {
                    generated_command_hook_hash("stop", None, "exomonad hook stop --runtime codex")
                }
                _ => unreachable!(),
            };

            assert_eq!(actual, expected, "hash mismatch for {codex_name}");
        }
    }
}
