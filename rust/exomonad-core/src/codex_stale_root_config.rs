//! Recognition and retirement of a stale ExoMonad-generated project-root
//! `.codex/config.toml`.
//!
//! ExoMonad used to write a Codex config straight into the *project root* when
//! the root agent was Codex. It stopped: [`crate::codex_config`] now writes every
//! generated Codex config under an agent directory (`.exo/agents/<companion>/`
//! for companions, `.exo/agents/<name>/` for spawned agents), because a config at
//! the project root is applied by Codex to *every* session opened in the project,
//! not just to the agent it was written for. Projects set up by an older ExoMonad
//! still carry the old file, and nothing removes it, because the file is
//! gitignored and because a project-root `.codex/config.toml` may equally be a
//! file the operator owns. Chainlink #1145.
//!
//! What the stale file did to an operator: it pinned every Codex session in the
//! project to the retired root TL — `approval_policy = "never"`,
//! `sandbox_mode = "workspace-write"` with `[sandbox_workspace_write]
//! network_access = false`, the ExoMonad pre/post/stop hooks, and the `exomonad`
//! MCP server registered as the root role whose `PreToolUse` guard denies edits.
//! With `network_access = false`, every socket failed with `EPERM`, so
//! `opencode --standalone` died in `listen(2)` and the session could not ask for
//! escalation.
//!
//! # Recognition
//!
//! A project-root `.codex/config.toml` is recognized as ExoMonad-generated only
//! when **two independent signals** are both present:
//!
//! 1. A `mcp_servers.exomonad` entry whose command is an `exomonad` binary and
//!    whose args are ExoMonad's own `mcp-stdio --role <role> --name <name>` form.
//! 2. At least one `command` under `hooks` that is `<exomonad binary> hook
//!    <pre-tool-use|post-tool-use|stop> --runtime codex`, matching the three
//!    entries [`CODEX_HOOKS`] renders.
//!
//! Only `hooks` subtrees are searched for hook commands, never free text, so
//! prose in `developer_instructions` that quotes a hook command is not evidence.
//!
//! A file that does not parse as TOML is *not* recognized: ExoMonad only claims
//! what it can read back as what it wrote.
//!
//! # Never silent
//!
//! [`enforce`] fails and names the file, the exact `mv` command that retires it,
//! and the flag that performs that move. ExoMonad never edits or deletes a
//! project-root `.codex/config.toml` on its own — the operator may own it — and
//! never silently rewrites one. Retirement moves the file aside; it never
//! overwrites an existing backup.
//!
//! Retirement does not reclaim the Codex *hook trust* the retired config left in
//! the Codex user config. That residue is only reachable through the explicit
//! `exomonad codex-prune-trust` command (see
//! [`crate::codex_trust_maintenance`]), and the refusal message points at it.

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use tracing::{info, warn};

use crate::codex_config::CODEX_HOOKS;

/// The project-root Codex config location older ExoMonad versions wrote.
pub const PROJECT_ROOT_CODEX_CONFIG: &str = ".codex/config.toml";

/// The CLI flag that lets `init`/`new` retire the stale config instead of failing.
pub const RETIRE_FLAG: &str = "--retire-stale-codex-root-config";

/// Suffix marking a retired stale config, numbered so no backup is ever clobbered.
const RETIRED_SUFFIX: &str = ".exomonad-stale";

/// Roles whose project-root Codex config ExoMonad retired. Only these two ever
/// ran as the project-wide TL, so only these two are claimed; any other role is
/// reported but never touched.
const RETIRED_PROJECT_ROOT_ROLES: [&str; 2] = ["tl", "root"];

/// How many `mv` candidates the backup search will try before giving up.
const MAX_BACKUP_ATTEMPTS: u32 = 64;

/// A project-root `.codex/config.toml` ExoMonad recognizes as its own.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StaleProjectRootConfig {
    /// The absolute path of the recognized file.
    pub path: PathBuf,
    /// The role ExoMonad registered the `exomonad` MCP server under.
    pub role: String,
    /// The agent name ExoMonad registered the `exomonad` MCP server under.
    pub agent_name: String,
    /// Every ExoMonad Codex hook command found under `hooks`, in document order.
    pub hook_commands: Vec<String>,
}

/// The `mcp_servers.exomonad` registration ExoMonad rendered.
#[derive(Debug, Clone, PartialEq, Eq)]
struct McpRegistration {
    role: String,
    agent_name: String,
}

/// Recognize an ExoMonad-generated project-root `.codex/config.toml`.
///
/// Returns `Ok(None)` when the file is absent, does not parse as TOML, or shows
/// only one of the two ownership signals. An unreadable file is an error, not a
/// silent pass: ExoMonad will not start a session in a project whose root Codex
/// config it could not inspect.
pub fn detect(project_root: &Path) -> Result<Option<StaleProjectRootConfig>> {
    let path = project_root.join(PROJECT_ROOT_CODEX_CONFIG);
    let contents = match std::fs::read_to_string(&path) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(error).with_context(|| {
                format!(
                    "Failed to read {}: ExoMonad will not assume a config it cannot read is \
                     not one it wrote",
                    path.display()
                )
            });
        }
    };

    // ExoMonad only claims a config it can read back as the config it wrote.
    let Ok(parsed) = toml::from_str::<toml::Value>(&contents) else {
        return Ok(None);
    };

    let Some(registration) = exomonad_mcp_registration(&parsed) else {
        return Ok(None);
    };
    let hook_commands = exomonad_codex_hook_commands(&parsed);
    if hook_commands.is_empty() {
        return Ok(None);
    }

    Ok(Some(StaleProjectRootConfig {
        path,
        role: registration.role,
        agent_name: registration.agent_name,
        hook_commands,
    }))
}

/// Refuse to proceed while `project_root` carries an ExoMonad-generated
/// project-root Codex config for a retired root role.
///
/// Without `allow_retire` this fails loudly, naming the file, the exact `mv`
/// command that retires it, and [`RETIRE_FLAG`]. With `allow_retire` it moves the
/// file to the first unused backup path and continues. A project-root config that
/// is not ExoMonad's, or that ExoMonad recognizes for a role it never retired at
/// the project root, is left completely untouched.
pub fn enforce(project_root: &Path, allow_retire: bool) -> Result<()> {
    let Some(stale) = detect(project_root)? else {
        return Ok(());
    };
    if !RETIRED_PROJECT_ROOT_ROLES.contains(&stale.role.as_str()) {
        warn!(
            path = %stale.path.display(),
            role = %stale.role,
            agent_name = %stale.agent_name,
            hooks = stale.hook_commands.len(),
            "Found ExoMonad hooks and an exomonad MCP server in a project-root Codex config for \
             a role ExoMonad never configured at the project root. Codex applies that file to \
             every session opened in the project; ExoMonad does not claim or edit it."
        );
        return Ok(());
    }

    let backup_path = unused_backup_path(&stale.path)?;
    if !allow_retire {
        anyhow::bail!("{}", refusal(project_root, &stale, &backup_path));
    }
    retire_to(&stale.path, &backup_path)?;
    info!(
        stale = %stale.path.display(),
        backup = %backup_path.display(),
        role = %stale.role,
        agent_name = %stale.agent_name,
        hooks = stale.hook_commands.len(),
        "Retired a stale ExoMonad-generated project-root Codex config; run `exomonad \
         codex-prune-trust` to inspect the Codex hook trust it left behind"
    );
    Ok(())
}

/// The first unused `<config>.exomonad-stale.<n>.bak` path beside `config_path`.
///
/// Numbered from 1 and probed against the filesystem, so the path the refusal
/// message prints is one `mv` will not clobber.
fn unused_backup_path(config_path: &Path) -> Result<PathBuf> {
    let directory = config_path
        .parent()
        .context("project-root Codex config has no parent")?;
    let name = config_path
        .file_name()
        .and_then(OsStr::to_str)
        .context("project-root Codex config has no file name")?;
    for attempt in 1..=MAX_BACKUP_ATTEMPTS {
        let candidate = directory.join(format!("{name}{RETIRED_SUFFIX}.{attempt}.bak"));
        if !candidate.exists() {
            return Ok(candidate);
        }
    }
    anyhow::bail!(
        "Cannot find an unused backup path beside {}: {} candidates named config.toml{}.<n>.bak \
         already exist. Move or delete them by hand, then run this again.",
        config_path.display(),
        MAX_BACKUP_ATTEMPTS,
        RETIRED_SUFFIX
    )
}

/// Move `config_path` to `backup_path`, refusing to overwrite anything.
fn retire_to(config_path: &Path, backup_path: &Path) -> Result<()> {
    if backup_path.exists() {
        anyhow::bail!(
            "Refusing to retire {}: the backup path {} already exists.",
            config_path.display(),
            backup_path.display()
        );
    }
    std::fs::rename(config_path, backup_path).with_context(|| {
        format!(
            "Failed to move {} to {}",
            config_path.display(),
            backup_path.display()
        )
    })?;
    Ok(())
}

/// The refusal an operator reads, naming the file, the evidence, and both repairs.
fn refusal(project_root: &Path, stale: &StaleProjectRootConfig, backup_path: &Path) -> String {
    let relative = relative_display(project_root, &stale.path);
    let backup = relative_display(project_root, backup_path);
    let hooks =
        crate::codex_config::plural(stale.hook_commands.len(), "hook command", "hook commands");
    format!(
        "Refusing to start: {relative} is a stale ExoMonad-generated project-root Codex config \
         (chainlink #1145).\n\n\
         Codex applies that file to EVERY session opened in this project, so it pins each one to \
         the root TL ExoMonad retired: approval_policy = \"never\", sandbox_mode = \
         \"workspace-write\" with [sandbox_workspace_write] network_access = false, the ExoMonad \
         pre/post/stop {hooks}, and the exomonad MCP server registered as --role {role} --name \
         {agent_name}. With network_access = false every socket fails with EPERM, so \
         `opencode --standalone` cannot bind and the session cannot ask for escalation.\n\n\
         Evidence in {relative}: an `exomonad` MCP server registered for role {role} and name \
         {agent_name}, plus {hooks}.\n\n\
         ExoMonad will not silently edit or delete a project-root .codex/config.toml, because the \
         file may be yours. Retire it with exactly this command:\n\n    \
         mv {relative} {backup}\n\n\
         or re-run with {retire_flag} to perform that same move. Afterwards, `exomonad \
         codex-prune-trust` prints the Codex hook trust the retired config left behind; it writes \
         nothing until you pass --apply.",
        hooks = hooks,
        role = stale.role,
        agent_name = stale.agent_name,
        relative = relative,
        backup = backup,
        retire_flag = RETIRE_FLAG,
    )
}

/// Render a path relative to the project root so the message stays copy-pasteable.
fn relative_display(project_root: &Path, path: &Path) -> String {
    path.strip_prefix(project_root)
        .unwrap_or(path)
        .display()
        .to_string()
}

/// The `mcp_servers.exomonad` registration, if this config carries ExoMonad's own.
fn exomonad_mcp_registration(root: &toml::Value) -> Option<McpRegistration> {
    let server = root.get("mcp_servers")?.get("exomonad")?;
    let command = server.get("command")?.as_str()?;
    if binary_name(command) != Some("exomonad") {
        return None;
    }
    let args = server
        .get("args")?
        .as_array()?
        .iter()
        .filter_map(toml::Value::as_str)
        .collect::<Vec<_>>();
    if !args.contains(&"mcp-stdio") {
        return None;
    }
    Some(McpRegistration {
        role: flag_value(&args, "--role")?.to_string(),
        agent_name: flag_value(&args, "--name")?.to_string(),
    })
}

/// Every `<exomonad binary> hook <event> --runtime codex` command under `hooks`.
///
/// Only `hooks` subtrees are walked, so a `developer_instructions` paragraph that
/// quotes a hook command is never mistaken for one ExoMonad wrote.
fn exomonad_codex_hook_commands(root: &toml::Value) -> Vec<String> {
    let Some(hooks) = root.get("hooks") else {
        return Vec::new();
    };
    let mut commands = Vec::new();
    collect_command_values(hooks, &mut commands);
    commands
        .into_iter()
        .filter(|command| is_exomonad_codex_hook_command(command))
        .map(str::to_string)
        .collect()
}

/// Push every `command` string found anywhere below `value`, in document order.
fn collect_command_values<'a>(value: &'a toml::Value, out: &mut Vec<&'a str>) {
    match value {
        toml::Value::Table(table) => {
            for (key, child) in table {
                if key == "command" {
                    if let Some(command) = child.as_str() {
                        out.push(command);
                    }
                    continue;
                }
                collect_command_values(child, out);
            }
        }
        toml::Value::Array(items) => {
            for item in items {
                collect_command_values(item, out);
            }
        }
        _ => {}
    }
}

/// True when `command` is one of the three Codex hook commands ExoMonad renders.
fn is_exomonad_codex_hook_command(command: &str) -> bool {
    binary_name(command) == Some("exomonad")
        && CODEX_HOOKS
            .iter()
            .any(|event| command.contains(&format!("hook {} --runtime codex", event.command)))
}

/// The executable name a shell command runs, past any quoting.
fn binary_name(command: &str) -> Option<&str> {
    let token = command.split_whitespace().next()?;
    let unquoted = token.trim_matches(|c| c == '\'' || c == '"');
    Path::new(unquoted).file_name().and_then(OsStr::to_str)
}

/// The value following `flag` in a rendered argument list.
fn flag_value<'a>(args: &[&'a str], flag: &str) -> Option<&'a str> {
    let index = args.iter().position(|arg| *arg == flag)?;
    args.get(index + 1).copied()
}

#[cfg(test)]
mod tests;
