use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use nix::fcntl::{Flock, FlockArg};

const EXOMONAD_CODEX_HOOKS_BEGIN: &str = "# BEGIN EXOMONAD CODEX HOOKS";
const EXOMONAD_CODEX_HOOKS_END: &str = "# END EXOMONAD CODEX HOOKS";
const CODEX_HOOK_TIMEOUT_SEC: u64 = 600;

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
timeout = {hook_timeout}
async = false

[[hooks.PostToolUse]]
matcher = "*"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "exomonad hook post-tool-use --runtime codex"
timeout = {hook_timeout}
async = false

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "exomonad hook stop --runtime codex"
timeout = {hook_timeout}
async = false

{mcp_servers}
"#;

pub fn render_codex_config(
    agent_name: &str,
    role: &str,
    instructions: &str,
    model: Option<&str>,
    extra_mcp_servers: &HashMap<String, Value>,
    exomonad_binary: &Path,
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

    let exomonad_binary = exomonad_binary.display().to_string();
    let hook_command_prefix = crate::util::shell_quote(&exomonad_binary);

    CODEX_CONFIG_TEMPLATE
        .replace("{model_config}", &model_config_toml(model))
        .replace(
            "{instructions}",
            &escape_multiline_basic_string(instructions),
        )
        .replace(
            "exomonad hook pre-tool-use --runtime codex",
            &format!("{hook_command_prefix} hook pre-tool-use --runtime codex"),
        )
        .replace(
            "exomonad hook post-tool-use --runtime codex",
            &format!("{hook_command_prefix} hook post-tool-use --runtime codex"),
        )
        .replace(
            "exomonad hook stop --runtime codex",
            &format!("{hook_command_prefix} hook stop --runtime codex"),
        )
        .replace("{hook_timeout}", &CODEX_HOOK_TIMEOUT_SEC.to_string())
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
    update_codex_user_config(config_path, |existing| {
        let cleaned = strip_exomonad_codex_hooks_block(existing);

        let project_str = project_path.display().to_string();
        let header = format!("[projects.\"{}\"]", escape_toml_quoted_key(&project_str));
        let already_trusted = cleaned.lines().any(|l| l.trim() == header);

        let mut next = cleaned.trim_end().to_string();
        if !already_trusted {
            if !next.is_empty() {
                next.push_str("\n\n");
            }
            next.push_str(&format!("{header}\ntrust_level = \"trusted\""));
        }
        next.push('\n');
        Ok(next)
    })
}

pub fn install_codex_hook_trust(
    user_config_path: &Path,
    worktree_config_path: &Path,
) -> std::io::Result<()> {
    let config = std::fs::read_to_string(worktree_config_path)?;
    let hook_specs = codex_hook_specs(&config)?;
    let key_source = worktree_config_path.display().to_string();

    update_codex_user_config(user_config_path, |existing| {
        let mut root = parse_user_config(existing)?;
        let state = hooks_state_table(&mut root);
        for spec in &hook_specs {
            let key = format!("{}:{}:0:0", key_source, spec.event_label);
            let mut entry = toml::map::Map::new();
            entry.insert(
                "trusted_hash".to_string(),
                toml::Value::String(compute_codex_hook_hash(spec)?),
            );
            state.insert(key, toml::Value::Table(entry));
        }
        toml::to_string_pretty(&root).map_err(to_io_invalid_data)
    })
}

fn update_codex_user_config(
    config_path: &Path,
    update: impl FnOnce(&str) -> std::io::Result<String>,
) -> std::io::Result<()> {
    let parent = config_path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("Codex config path has no parent: {}", config_path.display()),
        )
    })?;
    std::fs::create_dir_all(parent)?;

    let lock_path = parent.join(".exomonad-config.lock");
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .open(lock_path)?;
    let _lock = lock_exclusive(lock_file)?;

    let existing = std::fs::read_to_string(config_path).unwrap_or_default();
    let next = update(&existing)?;
    if next == existing {
        return Ok(());
    }
    write_atomic(config_path, &next)
}

fn lock_exclusive(file: File) -> std::io::Result<Flock<File>> {
    Flock::lock(file, FlockArg::LockExclusive)
        .map_err(|(_, error)| std::io::Error::from_raw_os_error(error as i32))
}

fn write_atomic(path: &Path, content: &str) -> std::io::Result<()> {
    let parent = path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("Path has no parent: {}", path.display()),
        )
    })?;
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(content.as_bytes())?;
    temp.flush()?;
    temp.persist(path).map(|_| ()).map_err(|error| error.error)
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

#[derive(Debug)]
struct CodexHookSpec {
    event_label: &'static str,
    matcher: Option<String>,
    command: String,
    timeout_sec: Option<u64>,
    r#async: bool,
    status_message: Option<String>,
}

#[derive(Serialize)]
struct NormalizedHookIdentity {
    event_name: String,
    #[serde(flatten)]
    group: MatcherGroup,
}

#[derive(Serialize)]
struct MatcherGroup {
    #[serde(skip_serializing_if = "Option::is_none")]
    matcher: Option<String>,
    hooks: Vec<HookHandlerConfig>,
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum HookHandlerConfig {
    Command {
        command: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_windows: Option<String>,
        #[serde(rename = "timeout", skip_serializing_if = "Option::is_none")]
        timeout_sec: Option<u64>,
        #[serde(rename = "async")]
        r#async: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        status_message: Option<String>,
    },
}

fn codex_hook_specs(config: &str) -> std::io::Result<Vec<CodexHookSpec>> {
    let root: toml::Value = toml::from_str(config).map_err(to_io_invalid_data)?;
    [
        ("PreToolUse", "pre_tool_use"),
        ("PostToolUse", "post_tool_use"),
        ("Stop", "stop"),
    ]
    .into_iter()
    .map(|(event_name, event_label)| codex_hook_spec(&root, event_name, event_label))
    .collect()
}

fn codex_hook_spec(
    root: &toml::Value,
    event_name: &str,
    event_label: &'static str,
) -> std::io::Result<CodexHookSpec> {
    let group = root
        .get("hooks")
        .and_then(|hooks| hooks.get(event_name))
        .and_then(toml::Value::as_array)
        .and_then(|groups| groups.first())
        .ok_or_else(|| missing_hook(event_name))?;
    let handler = group
        .get("hooks")
        .and_then(toml::Value::as_array)
        .and_then(|hooks| hooks.first())
        .ok_or_else(|| missing_hook(event_name))?;
    let command = handler
        .get("command")
        .and_then(toml::Value::as_str)
        .ok_or_else(|| missing_hook(event_name))?
        .to_string();
    let timeout_sec = handler
        .get("timeout")
        .and_then(toml::Value::as_integer)
        .map(u64::try_from)
        .transpose()
        .map_err(to_io_invalid_data)?;

    Ok(CodexHookSpec {
        event_label,
        matcher: group
            .get("matcher")
            .and_then(toml::Value::as_str)
            .map(ToOwned::to_owned),
        command,
        timeout_sec,
        r#async: handler
            .get("async")
            .and_then(toml::Value::as_bool)
            .unwrap_or(false),
        status_message: handler
            .get("status_message")
            .and_then(toml::Value::as_str)
            .map(ToOwned::to_owned),
    })
}

fn compute_codex_hook_hash(spec: &CodexHookSpec) -> std::io::Result<String> {
    let identity = NormalizedHookIdentity {
        event_name: spec.event_label.to_string(),
        group: MatcherGroup {
            matcher: spec.matcher.clone(),
            hooks: vec![HookHandlerConfig::Command {
                command: spec.command.clone(),
                command_windows: None,
                timeout_sec: spec.timeout_sec,
                r#async: spec.r#async,
                status_message: spec.status_message.clone(),
            }],
        },
    };
    let value = toml::Value::try_from(identity).map_err(to_io_invalid_data)?;
    Ok(version_for_toml(&value))
}

fn version_for_toml(value: &toml::Value) -> String {
    let json = serde_json::to_value(value).unwrap_or(Value::Null);
    let canonical = canonical_json(json);
    let serialized = serde_json::to_vec(&canonical).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(serialized);
    let hash = hasher.finalize();
    let hex = hash
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!("sha256:{hex}")
}

fn canonical_json(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(canonical_json).collect()),
        Value::Object(values) => Value::Object(
            values
                .into_iter()
                .map(|(key, value)| (key, canonical_json(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect(),
        ),
        value => value,
    }
}

fn parse_user_config(existing: &str) -> std::io::Result<toml::Value> {
    if existing.trim().is_empty() {
        Ok(toml::Value::Table(toml::map::Map::new()))
    } else {
        toml::from_str(existing).map_err(to_io_invalid_data)
    }
}

fn hooks_state_table(root: &mut toml::Value) -> &mut toml::map::Map<String, toml::Value> {
    let root = ensure_table(root);
    let hooks = root
        .entry("hooks")
        .or_insert_with(|| toml::Value::Table(toml::map::Map::new()));
    let hooks = ensure_table(hooks);
    let state = hooks
        .entry("state")
        .or_insert_with(|| toml::Value::Table(toml::map::Map::new()));
    ensure_table(state)
}

fn ensure_table(value: &mut toml::Value) -> &mut toml::map::Map<String, toml::Value> {
    if !value.is_table() {
        *value = toml::Value::Table(toml::map::Map::new());
    }
    value
        .as_table_mut()
        .expect("value was converted to a table")
}

fn missing_hook(event_name: &str) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        format!("Codex config is missing {event_name} command hook"),
    )
}

fn to_io_invalid_data(error: impl std::fmt::Display) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, error.to_string())
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

    fn test_exomonad_binary() -> &'static Path {
        Path::new("/usr/local/bin/exomonad")
    }

    #[test]
    fn renders_codex_config_with_hooks_and_mcp() {
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
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
            Some("/usr/local/bin/exomonad hook pre-tool-use --runtime codex")
        );
        assert_eq!(
            parsed["hooks"]["Stop"][0]["hooks"][0]["command"].as_str(),
            Some("/usr/local/bin/exomonad hook stop --runtime codex")
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

        let config =
            render_codex_config("agent", "tl", "Plan.", None, &extra, test_exomonad_binary());

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
            test_exomonad_binary(),
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
            test_exomonad_binary(),
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

    #[test]
    fn install_codex_hook_trust_writes_state_entries() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = dir.path().join("worktree/.codex/config.toml");
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
        );
        std::fs::write(&worktree_config_path, config).unwrap();

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let result = std::fs::read_to_string(&user_config_path).unwrap();
        let parsed: toml::Value = toml::from_str(&result).expect("valid user config TOML");
        let key_source = worktree_config_path.display().to_string();
        for event in ["pre_tool_use", "post_tool_use", "stop"] {
            let key = format!("{key_source}:{event}:0:0");
            let hash = parsed["hooks"]["state"][&key]["trusted_hash"]
                .as_str()
                .expect("trusted hash written");
            assert!(hash.starts_with("sha256:"));
            assert_eq!(hash.len(), "sha256:".len() + 64);
        }
    }

    #[test]
    #[ignore = "requires the installed codex CLI"]
    fn codex_hook_hash_matches_installed_codex_cli() {
        let dir = tempfile::tempdir().unwrap();
        let repo_path = dir.path().join("repo");
        let codex_home = dir.path().join("codex-home");
        let user_config_path = codex_home.join("config.toml");
        let worktree_config_path = repo_path.join(".codex/config.toml");
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();

        trust_codex_project(&user_config_path, &repo_path).unwrap();
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
        );
        std::fs::write(&worktree_config_path, config).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let key = format!("{}:pre_tool_use:0:0", worktree_config_path.display());
        let trusted_hash = read_trusted_hook_hash(&user_config_path, &key);
        let codex_hook = read_codex_hook_metadata(&codex_home, &repo_path, &key);

        assert_eq!(
            codex_hook["currentHash"].as_str(),
            Some(trusted_hash.as_str())
        );
        assert_eq!(codex_hook["trustStatus"].as_str(), Some("trusted"));
    }

    #[test]
    fn install_codex_hook_trust_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = dir.path().join("worktree/.codex/config.toml");
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();
        let config = render_codex_config(
            "worker-1-codex",
            "dev",
            "Use ExoMonad tools.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
        );
        std::fs::write(&worktree_config_path, config).unwrap();

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_first = std::fs::read_to_string(&user_config_path).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_second = std::fs::read_to_string(&user_config_path).unwrap();

        assert_eq!(after_first, after_second);
    }

    fn read_trusted_hook_hash(user_config_path: &Path, key: &str) -> String {
        let user_config = std::fs::read_to_string(user_config_path).unwrap();
        let parsed: toml::Value = toml::from_str(&user_config).unwrap();
        parsed["hooks"]["state"][key]["trusted_hash"]
            .as_str()
            .expect("trusted hash written")
            .to_string()
    }

    fn read_codex_hook_metadata(codex_home: &Path, cwd: &Path, key: &str) -> Value {
        use std::io::{BufRead, BufReader};
        use std::sync::mpsc;

        let initialize = json!({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "exomonad-parity-test", "version": "0.0.0"},
                "capabilities": {"experimentalApi": true}
            }
        });
        let list_hooks = json!({
            "id": 2,
            "method": "hooks/list",
            "params": {"cwds": [cwd]}
        });
        let mut child = std::process::Command::new("codex")
            .arg("app-server")
            .current_dir(cwd)
            .env("CODEX_HOME", codex_home)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .expect("codex CLI must be installed on PATH");
        let stdout = child.stdout.take().expect("stdout piped");
        let (stdout_tx, stdout_rx) = mpsc::channel();
        let stdout_reader = std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if stdout_tx.send(line).is_err() {
                    break;
                }
            }
        });

        let mut stdout_lines = Vec::new();
        let stdin = child.stdin.as_mut().expect("stdin piped");
        writeln!(stdin, "{initialize}").unwrap();
        stdin.flush().unwrap();
        read_app_server_response(&stdout_rx, &mut stdout_lines, 1);
        writeln!(stdin, "{list_hooks}").unwrap();
        stdin.flush().unwrap();
        let list_response = read_app_server_response(&stdout_rx, &mut stdout_lines, 2);

        let _ = child.kill();
        let _ = child.wait();
        let _ = stdout_reader.join();

        find_hook_metadata(&list_response, key)
            .cloned()
            .unwrap_or_else(|| {
                panic!(
                    "codex hook metadata for {key} not found\nstdout:\n{}",
                    stdout_lines.join("\n")
                )
            })
    }

    fn read_app_server_response(
        stdout_rx: &std::sync::mpsc::Receiver<String>,
        stdout_lines: &mut Vec<String>,
        id: i64,
    ) -> Value {
        loop {
            let line = stdout_rx
                .recv_timeout(std::time::Duration::from_secs(10))
                .unwrap_or_else(|error| {
                    panic!(
                        "timed out waiting for codex app-server response {id}: {error}\nstdout:\n{}",
                        stdout_lines.join("\n")
                    )
                });
            if let Ok(value) = serde_json::from_str::<Value>(&line) {
                stdout_lines.push(line);
                if value["id"].as_i64() == Some(id) {
                    return value;
                }
            } else {
                stdout_lines.push(line);
            }
        }
    }

    fn find_hook_metadata<'a>(value: &'a Value, key: &str) -> Option<&'a Value> {
        match value {
            Value::Array(values) => values
                .iter()
                .find_map(|value| find_hook_metadata(value, key)),
            Value::Object(values) => {
                if values.get("key").and_then(Value::as_str) == Some(key) {
                    Some(value)
                } else {
                    values
                        .values()
                        .find_map(|value| find_hook_metadata(value, key))
                }
            }
            _ => None,
        }
    }
}
