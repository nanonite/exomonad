use serde_json::Value;
use std::collections::HashMap;

pub const CODEX_HOOKS_JSON: &str = r#"{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "exomonad hook pre-tool-use --runtime codex"
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
            "command": "exomonad hook post-tool-use --runtime codex"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "exomonad hook stop --runtime codex"
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
}
