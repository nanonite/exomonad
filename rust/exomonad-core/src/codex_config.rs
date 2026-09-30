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
sandbox_mode = "workspace-write"
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

{sandbox_workspace_write}
"#;

pub fn render_codex_config(
    agent_name: &str,
    role: &str,
    instructions: &str,
    model: Option<&str>,
    extra_mcp_servers: &HashMap<String, Value>,
    exomonad_binary: &Path,
    project_root: &Path,
) -> String {
    render_codex_config_with_effort(
        agent_name,
        role,
        instructions,
        model,
        None,
        extra_mcp_servers,
        exomonad_binary,
        project_root,
    )
}

pub fn render_codex_config_with_effort(
    agent_name: &str,
    role: &str,
    instructions: &str,
    model: Option<&str>,
    effort: Option<&str>,
    extra_mcp_servers: &HashMap<String, Value>,
    exomonad_binary: &Path,
    project_root: &Path,
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
        .replace("{model_config}", &model_config_toml(model, effort))
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
        .replace(
            "{sandbox_workspace_write}",
            &sandbox_workspace_write_toml(role, project_root),
        )
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
        Ok((next, ()))
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
        toml::to_string_pretty(&root)
            .map(|next| (next, ()))
            .map_err(to_io_invalid_data)
    })
}

/// One `[hooks.state]` key ExoMonad generated for a project config, paired with
/// the `trusted_hash` ExoMonad generates for it right now.
#[derive(Debug)]
struct OwnedHookTrustKey {
    key: String,
    trusted_hash: String,
}

/// The outcome of [`uninstall_codex_hook_trust`], so a caller can report what
/// was deleted and — more importantly — everything left behind and why.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct HookTrustRemoval {
    /// How many exact keys ExoMonad derived from the generated config and
    /// looked for. Entries that were already absent are not errors.
    pub checked: usize,
    /// Exact keys deleted because ExoMonad still recognized them as its own.
    pub removed: Vec<String>,
    /// Exact keys left in place because ExoMonad could not prove it still owns
    /// them. Never silently rewritten or dropped.
    pub preserved: Vec<PreservedHookTrust>,
    /// True when the removal emptied `[hooks.state]` and ExoMonad pruned the
    /// now-empty `hooks` scaffolding it had just emptied.
    pub pruned_empty_hooks_state: bool,
}

/// A hook-trust entry ExoMonad deliberately refused to delete.
#[derive(Debug, PartialEq, Eq)]
pub struct PreservedHookTrust {
    pub key: String,
    pub reason: HookTrustPreserveReason,
}

/// Why ExoMonad left a `[hooks.state]` entry in place.
#[derive(Debug, PartialEq, Eq)]
pub enum HookTrustPreserveReason {
    /// The key is one ExoMonad would own, but the recorded `trusted_hash` no
    /// longer matches the hash ExoMonad generates from the generated config
    /// today, so the entry is user-modified rather than ExoMonad residue.
    HashMismatch { expected: String, found: String },
    /// The entry is not shaped like `[hooks.state.<key>].trusted_hash = "<hash>"`.
    Malformed { detail: String },
}

/// Removes the Codex hook trust [`install_codex_hook_trust`] seeded for
/// `worktree_config_path` from the Codex user config — and only that.
///
/// Removal is the exact inverse of installation:
///
/// - Candidate keys are derived from the generated `.codex/config.toml`
///   itself, so a sibling worktree, a different handler index, or a path that
///   merely shares a prefix is never a candidate.
/// - An exact candidate is deleted only when its recorded `trusted_hash`
///   equals the hash ExoMonad generates from that config today. A drifted hash
///   means the hook was edited after installation, so the entry is user state
///   and is preserved.
/// - Entries ExoMonad cannot parse are preserved and reported rather than
///   silently rewritten. Unrelated keys are never even inspected, so they
///   survive byte-identically up to the existing [`toml::to_string_pretty`]
///   writer contract that installation already uses.
///
/// The read-modify-write runs under the same `.exomonad-config.lock` sidecar
/// flock and the same atomic writer as installation, so a concurrent install
/// cannot interleave with a concurrent uninstall. Removal is idempotent:
/// uninstalling an already-uninstalled config removes nothing and leaves the
/// file untouched.
///
/// Fails closed. If the generated config is missing, unreadable, or has no
/// ExoMonad hooks, or if the user config is not parseable TOML or has a
/// `[hooks.state]` that is not a table, this returns an error with an
/// actionable message and leaves the user config unchanged.
pub fn uninstall_codex_hook_trust(
    user_config_path: &Path,
    worktree_config_path: &Path,
) -> std::io::Result<HookTrustRemoval> {
    let owned = owned_hook_trust_keys(worktree_config_path)?;
    update_codex_user_config(user_config_path, |existing| {
        let mut root = parse_user_config(existing)?;
        let report = remove_owned_hook_trust(user_config_path, &mut root, &owned)?;
        toml::to_string_pretty(&root)
            .map(|next| (next, report))
            .map_err(to_io_invalid_data)
    })
}

/// The exact `[hooks.state]` keys ExoMonad generated for `worktree_config_path`,
/// each paired with the hash ExoMonad generates for it now.
fn owned_hook_trust_keys(worktree_config_path: &Path) -> std::io::Result<Vec<OwnedHookTrustKey>> {
    let config = std::fs::read_to_string(worktree_config_path)
        .map_err(|error| unreadable_generated_config(worktree_config_path, error))?;
    let hook_specs = codex_hook_specs(&config)?;
    let key_source = worktree_config_path.display().to_string();
    hook_specs
        .iter()
        .map(|spec| {
            Ok(OwnedHookTrustKey {
                key: format!("{key_source}:{}:0:0", spec.event_label),
                trusted_hash: compute_codex_hook_hash(spec)?,
            })
        })
        .collect()
}

fn remove_owned_hook_trust(
    user_config_path: &Path,
    root: &mut toml::Value,
    owned: &[OwnedHookTrustKey],
) -> std::io::Result<HookTrustRemoval> {
    let mut report = HookTrustRemoval {
        checked: owned.len(),
        ..HookTrustRemoval::default()
    };
    if let Some(state) = hooks_state_table_for_removal(user_config_path, root)? {
        for candidate in owned {
            remove_owned_candidate(state, candidate, &mut report);
        }
    }
    if !report.removed.is_empty() {
        report.pruned_empty_hooks_state = prune_empty_hooks_state(root);
    }
    Ok(report)
}

fn remove_owned_candidate(
    state: &mut toml::map::Map<String, toml::Value>,
    candidate: &OwnedHookTrustKey,
    report: &mut HookTrustRemoval,
) {
    let Some(entry) = state.get(&candidate.key) else {
        return;
    };
    match recorded_hook_trust(entry) {
        RecordedHookTrust::Hash(found) if found == candidate.trusted_hash => {
            state.remove(&candidate.key);
            report.removed.push(candidate.key.clone());
        }
        RecordedHookTrust::Hash(found) => report.preserve(
            candidate,
            HookTrustPreserveReason::HashMismatch {
                expected: candidate.trusted_hash.clone(),
                found,
            },
        ),
        RecordedHookTrust::Unreadable(detail) => {
            report.preserve(candidate, HookTrustPreserveReason::Malformed { detail })
        }
    }
}

/// What a `[hooks.state]` entry claims, without judging whether ExoMonad owns it.
enum RecordedHookTrust {
    Hash(String),
    Unreadable(String),
}

fn recorded_hook_trust(entry: &toml::Value) -> RecordedHookTrust {
    let Some(table) = entry.as_table() else {
        return RecordedHookTrust::Unreadable(format!(
            "entry is {} instead of a table with a trusted_hash string",
            entry.type_str()
        ));
    };
    match table.get("trusted_hash") {
        None => RecordedHookTrust::Unreadable("entry has no trusted_hash field".to_string()),
        Some(toml::Value::String(found)) => RecordedHookTrust::Hash(found.clone()),
        Some(other) => RecordedHookTrust::Unreadable(format!(
            "trusted_hash is {} instead of a string",
            other.type_str()
        )),
    }
}

/// Drops `[hooks.state]` — and `[hooks]` with it — once ExoMonad's own removal
/// has emptied it, so the atomic writer does not leave two header-only tables
/// behind for Codex to parse. Only ever runs on a state table ExoMonad just
/// emptied; a state table that still holds anything (ExoMonad's or the user's)
/// keeps both headers.
fn prune_empty_hooks_state(root: &mut toml::Value) -> bool {
    let Some(root_table) = root.as_table_mut() else {
        return false;
    };
    let Some(hooks) = root_table
        .get_mut("hooks")
        .and_then(toml::Value::as_table_mut)
    else {
        return false;
    };
    let emptied = hooks
        .get("state")
        .and_then(toml::Value::as_table)
        .is_some_and(|state| state.is_empty());
    if !emptied {
        return false;
    }
    hooks.remove("state");
    if hooks.is_empty() {
        root_table.remove("hooks");
    }
    true
}

impl HookTrustRemoval {
    fn preserve(&mut self, candidate: &OwnedHookTrustKey, reason: HookTrustPreserveReason) {
        self.preserved.push(PreservedHookTrust {
            key: candidate.key.clone(),
            reason,
        });
    }
}

impl std::fmt::Display for HookTrustRemoval {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "checked {} ExoMonad Codex hook trust {}; removed {}; preserved {}{}",
            self.checked,
            plural(self.checked, "entry", "entries"),
            self.removed.len(),
            self.preserved.len(),
            if self.pruned_empty_hooks_state {
                "; pruned empty [hooks.state]"
            } else {
                ""
            },
        )
    }
}

impl std::fmt::Display for PreservedHookTrust {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}: ", self.key)?;
        match &self.reason {
            HookTrustPreserveReason::HashMismatch { expected, found } => write!(
                formatter,
                "trusted_hash {found} does not match the ExoMonad-generated {expected}, so the \
                 hook was modified after ExoMonad installed it and the record is yours. Restore \
                 the generated .codex/config.toml and reinstall to re-claim it, or delete this \
                 entry by hand if the hook is retired."
            ),
            HookTrustPreserveReason::Malformed { detail } => write!(
                formatter,
                "{detail}, so ExoMonad cannot parse it as a record it wrote. Repair or delete \
                 this entry by hand; ExoMonad left it untouched."
            ),
        }
    }
}

fn plural(count: usize, singular: &'static str, plural: &'static str) -> &'static str {
    if count == 1 {
        singular
    } else {
        plural
    }
}

fn unreadable_generated_config(
    worktree_config_path: &Path,
    error: std::io::Error,
) -> std::io::Error {
    if error.kind() != std::io::ErrorKind::NotFound {
        return std::io::Error::new(
            error.kind(),
            format!(
                "Failed to read the generated Codex project config {}: {error}",
                worktree_config_path.display()
            ),
        );
    }
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!(
            "Cannot remove ExoMonad Codex hook trust for {}: the generated project config is \
             gone, so ExoMonad cannot recompute the hashes it wrote and will not guess which \
             [hooks.state] keys are its own. Delete {} before removing trust, or inspect the \
             historical entries by hand before removing them.",
            worktree_config_path.display(),
            worktree_config_path.display()
        ),
    )
}

fn update_codex_user_config<T>(
    config_path: &Path,
    update: impl FnOnce(&str) -> std::io::Result<(String, T)>,
) -> std::io::Result<T> {
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
        .truncate(false)
        .write(true)
        .open(lock_path)?;
    let _lock = lock_exclusive(lock_file)?;

    let existing = std::fs::read_to_string(config_path).unwrap_or_default();
    let (next, outcome) = update(&existing)?;
    if next != existing {
        write_atomic(config_path, &next)?;
    }
    Ok(outcome)
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

/// The existing `[hooks.state]` table, if the user config has one. Never
/// creates scaffolding — a removal must not add tables it does not own, and
/// must be a no-op when the user config never had hook state.
///
/// Fails closed when `[hooks.state]` exists but is not a table: rewriting it
/// would destroy configuration ExoMonad cannot interpret. A `hooks` value that
/// is not a table at all (for example `[[hooks.Stop]]` groups) simply has no
/// state table, so it is left alone rather than treated as an error.
fn hooks_state_table_for_removal<'a>(
    user_config_path: &Path,
    root: &'a mut toml::Value,
) -> std::io::Result<Option<&'a mut toml::map::Map<String, toml::Value>>> {
    let Some(state) = root
        .as_table_mut()
        .and_then(|root| root.get_mut("hooks"))
        .and_then(toml::Value::as_table_mut)
        .and_then(|hooks| hooks.get_mut("state"))
    else {
        return Ok(None);
    };
    if !state.is_table() {
        return Err(malformed_hooks_state(user_config_path, state));
    }
    Ok(state.as_table_mut())
}

fn malformed_hooks_state(user_config_path: &Path, state: &toml::Value) -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::InvalidData,
        format!(
            "Codex user config {} has [hooks.state] as {} instead of a table of trusted hashes. \
             ExoMonad will not rewrite configuration it cannot parse. Repair or delete \
             [hooks.state] by hand, then retry the hook trust removal.",
            user_config_path.display(),
            state.type_str()
        ),
    )
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

fn writable_roots_for_role(role: &str) -> &'static [&'static str] {
    match role {
        "root" | "tl" => &[".exo", ".git"],
        "reviewer" => &[
            ".exo/events",
            ".exo/tmp",
            "target",
            "rust/target",
            "dist-newstyle",
            "haskell/dist-newstyle",
            ".stack-work",
            ".cache",
        ],
        _ => &["."],
    }
}

/// Renders the `[sandbox_workspace_write]` table Codex resolves `sandbox_mode
/// = "workspace-write"` against (see `codex-rs/config/src/types.rs`,
/// `SandboxWorkspaceWrite`). `writable_roots` must be absolute paths.
///
/// `writable_roots` still enforces docs/decisions/agent-sandbox-profiles.md:
/// root/tl may only touch `.exo`/`.git`, reviewers only build/event
/// directories, dev/worker get the full worktree.
///
/// `network_access = false` matches docs/decisions/agent-sandbox-profiles.md:
/// root/tl may only touch `.exo`/`.git`, reviewers only build/event
/// directories, dev/worker get the full worktree. Denying network makes
/// Codex's bwrap sandbox unshare the network namespace and configure a
/// loopback-only interface, which requires `capability net_admin` inside an
/// unprivileged user namespace. A host whose AppArmor `unprivileged_userns`
/// transition profile does not grant that will see
/// `bwrap: loopback: Failed RTM_NEWADDR`; chainlink #1085/#1086 briefly
/// worked around this by flipping `network_access` to `true`, but that was
/// reverted once chainlink #1087 found the real fix: attach a dedicated
/// `/etc/apparmor.d/bwrap-userns` profile to `/usr/bin/bwrap` itself
/// (`profile bwrap_userns /usr/bin/bwrap flags=(unconfined) { userns, }`)
/// rather than editing the `unprivileged_userns` transition profile — the
/// restriction only applies to processes AppArmor treats as unconfined, and
/// a named profile (even one that behaves like unconfined) sidesteps it
/// instead of trying to patch it. See the ADR's 2026-09-16 update for the
/// full trail; this is a host policy gap to document and fix at the OS
/// level (chainlink #1085's non-blocking preflight-check follow-on can now
/// recommend this exact profile), not a reason to widen this config.
fn sandbox_workspace_write_toml(role: &str, project_root: &Path) -> String {
    let writable_roots = writable_roots_for_role(role)
        .iter()
        .map(|relative| {
            let absolute = if *relative == "." {
                project_root.to_path_buf()
            } else {
                project_root.join(relative)
            };
            toml::Value::String(absolute.display().to_string())
        })
        .collect();

    let mut sandbox_workspace_write = toml::map::Map::new();
    sandbox_workspace_write.insert(
        "writable_roots".to_string(),
        toml::Value::Array(writable_roots),
    );
    sandbox_workspace_write.insert("network_access".to_string(), toml::Value::Boolean(false));

    let mut root = toml::map::Map::new();
    root.insert(
        "sandbox_workspace_write".to_string(),
        toml::Value::Table(sandbox_workspace_write),
    );
    toml::to_string_pretty(&toml::Value::Table(root))
        .expect("Codex sandbox_workspace_write config should serialize")
        .trim()
        .to_string()
}

fn model_config_toml(model: Option<&str>, effort: Option<&str>) -> String {
    if model.filter(|value| !value.is_empty()).is_none()
        && effort.filter(|value| !value.is_empty()).is_none()
    {
        return String::new();
    }

    let mut root = toml::map::Map::new();
    if let Some(model) = model.filter(|value| !value.is_empty()) {
        root.insert("model".to_string(), toml::Value::String(model.to_string()));
    }
    if let Some(effort) = effort.filter(|value| !value.is_empty()) {
        root.insert(
            "model_reasoning_effort".to_string(),
            toml::Value::String(effort.to_string()),
        );
    }
    let mut rendered =
        toml::to_string(&toml::Value::Table(root)).expect("Codex model config should serialize");
    rendered.push('\n');
    rendered
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

    fn test_project_root() -> &'static Path {
        Path::new("/tmp/exomonad-test-project")
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
            test_project_root(),
        );

        assert!(config.contains("approval_policy = \"never\""));
        assert!(config.contains("sandbox_mode = \"workspace-write\""));
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
        assert_eq!(parsed["sandbox_mode"].as_str(), Some("workspace-write"));
        assert!(parsed.get("default_permissions").is_none());
        assert!(parsed.get("permissions").is_none());
        assert_eq!(
            parsed["sandbox_workspace_write"]["writable_roots"]
                .as_array()
                .unwrap(),
            &[toml::Value::String(
                test_project_root().display().to_string()
            )],
            "dev role gets the whole worktree writable"
        );
        assert_eq!(
            parsed["sandbox_workspace_write"]["network_access"].as_bool(),
            Some(false),
            "network_access stays false per docs/decisions/agent-sandbox-profiles.md; \
             chainlink #1087 found the real host fix (bwrap-userns AppArmor profile) \
             instead of widening this"
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
            test_exomonad_binary(),
            test_project_root(),
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
            test_exomonad_binary(),
            test_project_root(),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.2"));
        assert!(config.starts_with("model = \"gpt-5.2\"\n\napproval_policy"));
    }

    #[test]
    fn renders_effort_when_provided() {
        let config = render_codex_config_with_effort(
            "worker-1-codex",
            "worker",
            "Use ExoMonad tools.",
            Some("gpt-5.2"),
            Some("high"),
            &HashMap::new(),
            test_exomonad_binary(),
            test_project_root(),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.2"));
        assert_eq!(parsed["model_reasoning_effort"].as_str(), Some("high"));
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
            test_project_root(),
        );

        let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
        assert!(parsed.get("model").is_none());
    }

    #[test]
    fn maps_roles_to_scoped_writable_roots() {
        let root = test_project_root();
        for (role, expected_relative) in [
            ("root", &[".exo", ".git"][..]),
            ("tl", &[".exo", ".git"][..]),
            (
                "reviewer",
                &[
                    ".exo/events",
                    ".exo/tmp",
                    "target",
                    "rust/target",
                    "dist-newstyle",
                    "haskell/dist-newstyle",
                    ".stack-work",
                    ".cache",
                ][..],
            ),
            ("worker", &["."][..]),
            ("dev", &["."][..]),
            ("custom-dev-role", &["."][..]),
        ] {
            let config = render_codex_config(
                "agent",
                role,
                "Use ExoMonad tools.",
                None,
                &HashMap::new(),
                test_exomonad_binary(),
                root,
            );
            let parsed: toml::Value = toml::from_str(&config).expect("valid Codex config TOML");
            assert_eq!(
                parsed["sandbox_mode"].as_str(),
                Some("workspace-write"),
                "role {role} should stay in workspace-write, not fall back to a stricter \
                 default via an unrecognized profile"
            );
            let expected: Vec<toml::Value> = expected_relative
                .iter()
                .map(|relative| {
                    let absolute = if *relative == "." {
                        root.to_path_buf()
                    } else {
                        root.join(relative)
                    };
                    toml::Value::String(absolute.display().to_string())
                })
                .collect();
            assert_eq!(
                parsed["sandbox_workspace_write"]["writable_roots"]
                    .as_array()
                    .unwrap(),
                &expected,
                "role {role} should get its scoped writable roots"
            );
            assert_eq!(
                parsed["sandbox_workspace_write"]["network_access"].as_bool(),
                Some(false),
                "role {role} should keep network denied per docs/decisions/agent-sandbox-profiles.md; \
                 chainlink #1087 found the real host fix instead of widening this"
            );
        }
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
            test_project_root(),
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
            &repo_path,
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
    #[ignore = "requires the installed codex CLI"]
    fn codex_hook_hash_matches_codex_cli_for_root_role() {
        // Mirrors what rust/exomonad/src/init.rs:write_codex_root_config does
        // when the user spawns a codex root TL. The dev-role test covers leaves;
        // this one covers the path the operator's first-run experience hits.
        let dir = tempfile::tempdir().unwrap();
        let repo_path = dir.path().join("repo");
        let codex_home = dir.path().join("codex-home");
        let user_config_path = codex_home.join("config.toml");
        let worktree_config_path = repo_path.join(".codex/config.toml");
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();

        trust_codex_project(&user_config_path, &repo_path).unwrap();
        let config = render_codex_config(
            "root",
            "root",
            "Codex root TL placeholder instructions.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
            &repo_path,
        );
        std::fs::write(&worktree_config_path, config).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        for event in ["pre_tool_use", "post_tool_use", "stop"] {
            let key = format!("{}:{event}:0:0", worktree_config_path.display());
            let trusted_hash = read_trusted_hook_hash(&user_config_path, &key);
            let codex_hook = read_codex_hook_metadata(&codex_home, &repo_path, &key);
            assert_eq!(
                codex_hook["currentHash"].as_str(),
                Some(trusted_hash.as_str()),
                "hash mismatch for {event} on root role — codex sees a different hash than \
                 install_codex_hook_trust wrote, which fires the 'hooks need review' prompt"
            );
            assert_eq!(
                codex_hook["trustStatus"].as_str(),
                Some("trusted"),
                "trustStatus is not 'trusted' for {event} — even with matching hash codex thinks \
                 the hook is untrusted, likely a key-format or path-canonicalization mismatch"
            );
        }
    }

    #[test]
    #[ignore = "requires the installed codex CLI"]
    fn codex_hook_trust_survives_pre_seeded_projects_table() {
        // Mirrors what tests/e2e/reviewer-convergence-loop/run.sh does:
        // run.sh manually writes a [projects."$REPO_DIR"] trust_level = "trusted"
        // entry into $CODEX_HOME/config.toml *before* exomonad init runs. Then
        // init's trust_codex_project + install_codex_hook_trust must coexist
        // with that pre-seeded entry without losing the hook state. This case
        // is what fires "3 hooks need review" in the live e2e but is NOT
        // covered by codex_hook_hash_matches_codex_cli_for_root_role (which
        // starts from an empty $CODEX_HOME).
        let dir = tempfile::tempdir().unwrap();
        let repo_path = dir.path().join("repo");
        let codex_home = dir.path().join("codex-home");
        let user_config_path = codex_home.join("config.toml");
        let worktree_config_path = repo_path.join(".codex/config.toml");
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();
        std::fs::create_dir_all(&codex_home).unwrap();

        // Pre-seed exactly as run.sh does, before any exomonad call.
        let pre_seeded = format!(
            "[projects.\"{}\"]\ntrust_level = \"trusted\"\n",
            repo_path.display()
        );
        std::fs::write(&user_config_path, pre_seeded).unwrap();

        // What exomonad init does for a codex root TL, in order:
        trust_codex_project(&user_config_path, &repo_path).unwrap();
        let config = render_codex_config(
            "root",
            "root",
            "Codex root TL placeholder instructions.",
            None,
            &HashMap::new(),
            test_exomonad_binary(),
            &repo_path,
        );
        std::fs::write(&worktree_config_path, config).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        for event in ["pre_tool_use", "post_tool_use", "stop"] {
            let key = format!("{}:{event}:0:0", worktree_config_path.display());
            let trusted_hash = read_trusted_hook_hash(&user_config_path, &key);
            let codex_hook = read_codex_hook_metadata(&codex_home, &repo_path, &key);
            assert_eq!(
                codex_hook["currentHash"].as_str(),
                Some(trusted_hash.as_str()),
                "hash mismatch for {event} when [projects] table was pre-seeded"
            );
            assert_eq!(
                codex_hook["trustStatus"].as_str(),
                Some("trusted"),
                "trustStatus is not 'trusted' for {event} after pre-seeded [projects] table — \
                 this is the bug fired by tests/e2e/reviewer-convergence-loop"
            );
        }
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
            test_project_root(),
        );
        std::fs::write(&worktree_config_path, config).unwrap();

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_first = std::fs::read_to_string(&user_config_path).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_second = std::fs::read_to_string(&user_config_path).unwrap();

        assert_eq!(after_first, after_second);
    }

    // ---- ExoMonad Codex hook trust removal (chainlink #1123) ----

    const UNRELATED_STATE_KEY: &str = "/elsewhere/.codex/config.toml:stop:0:0";

    fn generated_worktree_config_path(root: &Path) -> PathBuf {
        root.join("worktree/.codex/config.toml")
    }

    fn write_generated_codex_config(worktree_config_path: &Path) {
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();
        std::fs::write(
            worktree_config_path,
            render_codex_config(
                "worker-1-codex",
                "dev",
                "Use ExoMonad tools.",
                None,
                &HashMap::new(),
                test_exomonad_binary(),
                test_project_root(),
            ),
        )
        .unwrap();
    }

    fn hook_trust_key(worktree_config_path: &Path, event: &str) -> String {
        format!("{}:{event}:0:0", worktree_config_path.display())
    }

    fn read_user_config(user_config_path: &Path) -> toml::Value {
        let raw = std::fs::read_to_string(user_config_path).unwrap();
        toml::from_str(&raw).expect("valid user config TOML")
    }

    fn state_entries(user_config_path: &Path) -> toml::map::Map<String, toml::Value> {
        read_user_config(user_config_path)["hooks"]["state"]
            .as_table()
            .cloned()
            .unwrap_or_default()
    }

    fn state_keys(user_config_path: &Path) -> Vec<String> {
        state_entries(user_config_path).keys().cloned().collect()
    }

    fn hash_entry(hash: &str) -> toml::Value {
        let mut entry = toml::map::Map::new();
        entry.insert(
            "trusted_hash".to_string(),
            toml::Value::String(hash.to_string()),
        );
        toml::Value::Table(entry)
    }

    fn set_state_entry(user_config_path: &Path, key: &str, entry: toml::Value) {
        let mut parsed = read_user_config(user_config_path);
        let state = parsed
            .get_mut("hooks")
            .and_then(|hooks| hooks.as_table_mut())
            .and_then(|hooks| hooks.get_mut("state"))
            .and_then(toml::Value::as_table_mut)
            .expect("[hooks.state] exists after install");
        state.insert(key.to_string(), entry);
        std::fs::write(user_config_path, toml::to_string_pretty(&parsed).unwrap()).unwrap();
    }

    fn seed_user_config(user_config_path: &Path, extra_entries: &[(String, toml::Value)]) {
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        let mut parsed = toml::map::Map::new();
        parsed.insert(
            "model".to_string(),
            toml::Value::String("gpt-5.5".to_string()),
        );
        let mut state = toml::map::Map::new();
        for (key, entry) in extra_entries {
            state.insert(key.clone(), entry.clone());
        }
        parsed.insert(
            "hooks".to_string(),
            toml::Value::Table(toml::map::Map::from_iter([(
                "state".to_string(),
                toml::Value::Table(state),
            )])),
        );
        std::fs::write(user_config_path, toml::to_string_pretty(&parsed).unwrap()).unwrap();
    }

    #[test]
    fn uninstall_codex_hook_trust_removes_only_its_exact_keys() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(
            &user_config_path,
            &[(
                UNRELATED_STATE_KEY.to_string(),
                hash_entry("sha256:elsewhere"),
            )],
        );

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        assert_eq!(state_keys(&user_config_path).len(), 4);

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(removal.checked, 3, "one candidate per ExoMonad hook event");
        assert_eq!(removal.removed.len(), 3);
        assert!(removal.preserved.is_empty());
        assert!(
            !removal.pruned_empty_hooks_state,
            "an entry ExoMonad does not own keeps [hooks.state] alive"
        );
        assert_eq!(
            state_keys(&user_config_path),
            vec![UNRELATED_STATE_KEY.to_string()]
        );
        assert_eq!(
            read_user_config(&user_config_path)["hooks"]["state"][UNRELATED_STATE_KEY]
                ["trusted_hash"]
                .as_str(),
            Some("sha256:elsewhere")
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_prunes_the_state_table_it_emptied() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(&user_config_path, "model = \"gpt-5.5\"\n").unwrap();

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        assert_eq!(state_keys(&user_config_path).len(), 3);
        assert!(std::fs::read_to_string(&user_config_path)
            .unwrap()
            .contains("[hooks"));

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(removal.removed.len(), 3);
        assert!(
            removal.pruned_empty_hooks_state,
            "removal emptied [hooks.state], so both headers must go"
        );
        let raw = std::fs::read_to_string(&user_config_path).unwrap();
        assert!(
            !raw.contains("hooks"),
            "no header-only [hooks]/[hooks.state] scaffolding should survive: {raw}"
        );
        assert_eq!(
            read_user_config(&user_config_path)["model"].as_str(),
            Some("gpt-5.5"),
            "unrelated user configuration survives the removal"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_preserves_user_modified_hashes() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let modified_key = hook_trust_key(&worktree_config_path, "post_tool_use");
        set_state_entry(
            &user_config_path,
            &modified_key,
            hash_entry("sha256:edited-by-hand"),
        );

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(
            removal.removed.len(),
            2,
            "the two untouched events are ExoMonad's own"
        );
        assert_eq!(removal.preserved.len(), 1);
        let preserved = &removal.preserved[0];
        assert_eq!(preserved.key, modified_key);
        let HookTrustPreserveReason::HashMismatch { expected, found } = &preserved.reason else {
            panic!("expected a hash mismatch, got {:?}", preserved.reason);
        };
        assert_eq!(found, "sha256:edited-by-hand");
        assert!(
            expected.starts_with("sha256:") && expected.len() == "sha256:".len() + 64,
            "the diagnostic reports the hash ExoMonad generates today: {expected}"
        );
        assert_eq!(
            state_entries(&user_config_path)
                .get(&modified_key)
                .and_then(|entry| entry["trusted_hash"].as_str()),
            Some("sha256:edited-by-hand"),
            "a record the user edited is user state, not ExoMonad residue"
        );
        assert!(!removal.pruned_empty_hooks_state);
    }

    #[test]
    fn uninstall_codex_hook_trust_preserves_malformed_entries() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);

        let mut malformed = toml::map::Map::new();
        malformed.insert("trusted_hash".to_string(), toml::Value::Integer(7));
        let pre_tool_key = hook_trust_key(&worktree_config_path, "pre_tool_use");
        let stop_key = hook_trust_key(&worktree_config_path, "stop");
        seed_user_config(
            &user_config_path,
            &[
                (pre_tool_key.clone(), toml::Value::Table(malformed)),
                (
                    stop_key.clone(),
                    toml::Value::String("not-a-table".to_string()),
                ),
                (
                    UNRELATED_STATE_KEY.to_string(),
                    toml::Value::Table(toml::map::Map::new()),
                ),
            ],
        );

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert!(
            removal.removed.is_empty(),
            "nothing matches, so nothing is removed: {:?}",
            removal.removed
        );
        assert_eq!(removal.preserved.len(), 2);
        for preserved in &removal.preserved {
            assert!(
                matches!(preserved.reason, HookTrustPreserveReason::Malformed { .. }),
                "{:?} should be reported as malformed",
                preserved.reason
            );
        }
        assert_eq!(removal.preserved[0].key, pre_tool_key);
        assert_eq!(removal.preserved[1].key, stop_key);
        assert_eq!(
            state_entries(&user_config_path).len(),
            3,
            "unreadable and unrelated entries all survive untouched"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_never_removes_a_path_prefix_match() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        // Every one of these is byte-adjacent to a real key: a sibling worktree
        // under the same parent, a different handler index, and a path that
        // merely starts with the real key source.
        let key_source = worktree_config_path.display().to_string();
        let near_misses = [
            format!("{key_source}:pre_tool_use:0:1"),
            format!("{key_source}:pre_tool_use:1:0"),
            format!("{key_source}-old/.codex/config.toml:pre_tool_use:0:0"),
            dir.path()
                .join("worktree/.codex/config.toml.bak")
                .display()
                .to_string()
                + ":stop:0:0",
        ];
        for near_miss in &near_misses {
            set_state_entry(&user_config_path, near_miss, hash_entry("sha256:neighbour"));
        }

        uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        for near_miss in &near_misses {
            assert!(
                state_entries(&user_config_path).contains_key(near_miss),
                "{near_miss} is not an exact ExoMonad key and must survive"
            );
        }
    }

    #[test]
    fn uninstall_codex_hook_trust_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let first = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_first = std::fs::read_to_string(&user_config_path).unwrap();
        let second = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let after_second = std::fs::read_to_string(&user_config_path).unwrap();

        assert_eq!(first.removed.len(), 3);
        assert_eq!(
            second,
            HookTrustRemoval {
                checked: 3,
                ..Default::default()
            }
        );
        assert_eq!(
            after_first, after_second,
            "a second removal must not rewrite the file"
        );
    }

    #[test]
    fn uninstall_then_install_restores_the_same_hook_trust() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let installed = state_entries(&user_config_path);
        uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(
            state_entries(&user_config_path),
            installed,
            "removal is exactly symmetric with installation"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_preserves_unrelated_codex_configuration() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(
            &user_config_path,
            "model = \"gpt-5.5\"\n\n\
             [projects.\"/tmp/my-project\"]\n\
             trust_level = \"trusted\"\n\n\
             [mcp_servers.docs]\n\
             command = \"npx\"\n\n\
             [hooks.state.\"/elsewhere/.codex/config.toml:stop:0:0\"]\n\
             trusted_hash = \"sha256:elsewhere\"\n",
        )
        .unwrap();

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let parsed = read_user_config(&user_config_path);
        assert_eq!(parsed["model"].as_str(), Some("gpt-5.5"));
        assert_eq!(
            parsed["projects"]["/tmp/my-project"]["trust_level"].as_str(),
            Some("trusted")
        );
        assert_eq!(
            parsed["mcp_servers"]["docs"]["command"].as_str(),
            Some("npx")
        );
        assert_eq!(
            parsed["hooks"]["state"][UNRELATED_STATE_KEY]["trusted_hash"].as_str(),
            Some("sha256:elsewhere")
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_ignores_a_hooks_value_without_state() {
        // A user config may legitimately hold Codex hook groups as an array;
        // that `hooks` has no `state` table to prune, and removal must not
        // rewrite it into one.
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        let seeded = "[[hooks.Stop]]\nmatcher = \"stop\"\n";
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(&user_config_path, seeded).unwrap();

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(
            removal,
            HookTrustRemoval {
                checked: 3,
                ..Default::default()
            }
        );
        assert_eq!(std::fs::read_to_string(&user_config_path).unwrap(), seeded);
    }

    #[test]
    fn uninstall_codex_hook_trust_is_a_no_op_without_a_user_config() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        assert_eq!(
            removal,
            HookTrustRemoval {
                checked: 3,
                ..Default::default()
            }
        );
        assert!(
            !user_config_path.exists(),
            "removal must not conjure a Codex user config"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_fails_closed_without_the_generated_config() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        let seeded = "model = \"gpt-5.5\"\n";
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(&user_config_path, seeded).unwrap();

        let error = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path)
            .expect_err("removal without the generated config must fail closed");

        assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
        let message = error.to_string();
        assert!(message.contains(&worktree_config_path.display().to_string()));
        assert!(
            message.contains("cannot recompute"),
            "the diagnostic must explain why ownership is unprovable: {message}"
        );
        assert_eq!(
            std::fs::read_to_string(&user_config_path).unwrap(),
            seeded,
            "a failed removal leaves the user config byte-for-byte unchanged"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_fails_closed_on_a_generated_config_without_hooks() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        std::fs::create_dir_all(worktree_config_path.parent().unwrap()).unwrap();
        std::fs::write(&worktree_config_path, "model = \"gpt-5.5\"\n").unwrap();
        seed_user_config(
            &user_config_path,
            &[(
                UNRELATED_STATE_KEY.to_string(),
                hash_entry("sha256:elsewhere"),
            )],
        );

        let error = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path)
            .expect_err("a config ExoMonad did not generate is not removable");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("missing"));
        assert_eq!(
            state_entries(&user_config_path),
            toml::map::Map::from_iter([(
                UNRELATED_STATE_KEY.to_string(),
                hash_entry("sha256:elsewhere")
            )]),
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_fails_closed_on_malformed_user_config() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        let malformed = "model = \"unterminated\n";
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(&user_config_path, malformed).unwrap();

        let error = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path)
            .expect_err("ExoMonad must not rewrite a user config it cannot parse");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        assert_eq!(
            std::fs::read_to_string(&user_config_path).unwrap(),
            malformed,
            "a failed removal leaves the user config byte-for-byte unchanged"
        );
    }

    #[test]
    fn uninstall_codex_hook_trust_fails_closed_when_hooks_state_is_not_a_table() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        let seeded = "hooks = { state = \"not a table\" }\n";
        std::fs::create_dir_all(user_config_path.parent().unwrap()).unwrap();
        std::fs::write(&user_config_path, seeded).unwrap();

        let error = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path)
            .expect_err("ExoMonad must not reinterpret [hooks.state] of another type");

        assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
        let message = error.to_string();
        assert!(message.contains("[hooks.state]"), "{message}");
        assert!(
            message.contains("by hand"),
            "the diagnostic must be actionable: {message}"
        );
        assert_eq!(std::fs::read_to_string(&user_config_path).unwrap(), seeded);
    }

    #[test]
    fn removal_diagnostics_name_the_key_and_the_reason() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);
        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        let modified_key = hook_trust_key(&worktree_config_path, "stop");
        set_state_entry(
            &user_config_path,
            &modified_key,
            hash_entry("sha256:edited-by-hand"),
        );

        let removal = uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let summary = removal.to_string();
        assert!(summary.contains("checked 3"), "{summary}");
        assert!(summary.contains("removed 2"), "{summary}");
        assert!(summary.contains("preserved 1"), "{summary}");
        let detail = removal.preserved[0].to_string();
        assert!(detail.contains(&modified_key), "{detail}");
        assert!(detail.contains("sha256:edited-by-hand"), "{detail}");
        assert!(detail.contains("does not match"), "{detail}");
    }

    #[test]
    fn concurrent_installs_of_distinct_worktrees_do_not_lose_entries() {
        const WORKTREES: usize = 6;

        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        seed_user_config(
            &user_config_path,
            &[(
                UNRELATED_STATE_KEY.to_string(),
                hash_entry("sha256:elsewhere"),
            )],
        );

        let handles: Vec<_> = (0..WORKTREES)
            .map(|index| {
                let user_config_path = user_config_path.clone();
                let worktree_config_path =
                    generated_worktree_config_path(&dir.path().join(format!("w{index}")));
                write_generated_codex_config(&worktree_config_path);
                std::thread::spawn(move || {
                    install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
                })
            })
            .collect();
        for handle in handles {
            handle.join().unwrap();
        }

        let entries = state_entries(&user_config_path);
        assert_eq!(
            entries.len(),
            WORKTREES * 3 + 1,
            "the shared lock must serialize the read-modify-write so no wave loses entries"
        );
        for index in 0..WORKTREES {
            let worktree_config_path =
                generated_worktree_config_path(&dir.path().join(format!("w{index}")));
            for event in ["pre_tool_use", "post_tool_use", "stop"] {
                let key = hook_trust_key(&worktree_config_path, event);
                let hash = entries[&key]["trusted_hash"].as_str().unwrap();
                assert!(hash.starts_with("sha256:"), "{key} was left half-written");
            }
        }
        assert!(entries.contains_key(UNRELATED_STATE_KEY));
    }

    #[test]
    fn concurrent_install_and_uninstall_of_one_worktree_stay_consistent() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(
            &user_config_path,
            &[(
                UNRELATED_STATE_KEY.to_string(),
                hash_entry("sha256:elsewhere"),
            )],
        );

        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let handles: Vec<_> = (0..8)
            .map(|index| {
                let user_config_path = user_config_path.clone();
                let worktree_config_path = worktree_config_path.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    for _ in 0..10 {
                        if index % 2 == 0 {
                            install_codex_hook_trust(&user_config_path, &worktree_config_path)
                                .unwrap();
                        } else {
                            uninstall_codex_hook_trust(&user_config_path, &worktree_config_path)
                                .unwrap();
                        }
                    }
                })
            })
            .collect();
        for handle in handles {
            handle.join().unwrap();
        }

        let entries = state_entries(&user_config_path);
        assert!(
            entries.contains_key(UNRELATED_STATE_KEY),
            "unrelated entries survive every interleaving"
        );
        for (key, entry) in &entries {
            let hash = entry["trusted_hash"]
                .as_str()
                .unwrap_or_else(|| panic!("{key} was left half-written"));
            assert!(hash.starts_with("sha256:"), "{key} was left half-written");
        }
        let own_keys: Vec<_> = entries
            .keys()
            .filter(|key| key.contains(&worktree_config_path.display().to_string()))
            .cloned()
            .collect();
        assert!(
            own_keys.is_empty() || own_keys.len() == 3,
            "a concurrent install/uninstall wave leaves the worktree's three keys \
             all present or all absent, never a partial set: {own_keys:?}"
        );
    }

    #[test]
    fn hook_trust_updates_go_through_the_shared_lock_and_leave_no_temp_files() {
        let dir = tempfile::tempdir().unwrap();
        let user_config_path = dir.path().join("codex-home/config.toml");
        let worktree_config_path = generated_worktree_config_path(dir.path());
        write_generated_codex_config(&worktree_config_path);
        seed_user_config(&user_config_path, &[]);

        install_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();
        uninstall_codex_hook_trust(&user_config_path, &worktree_config_path).unwrap();

        let mut leftovers: Vec<_> = std::fs::read_dir(user_config_path.parent().unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().display().to_string())
            .collect();
        leftovers.sort();
        assert_eq!(
            leftovers,
            vec![
                ".exomonad-config.lock".to_string(),
                "config.toml".to_string()
            ],
            "the atomic writer must leave no temp file behind, and every update must \
             go through the shared .exomonad-config.lock sidecar"
        );
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
