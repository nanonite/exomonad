//! Guard Git commands launched through process.run from mutating nested repositories.

use super::git_snapshot::GitEntrySnapshot;
use crate::effects::{EffectError, EffectResult};
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::process::Command;
use std::sync::OnceLock;

const MAX_GIT_CONFIG_ENV_ITEMS: usize = 128;
const MAX_GIT_ALIAS_EXPANSIONS: usize = 8;

#[cfg(test)]
pub(crate) fn validate_git_command(
    args: &[String],
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<()> {
    prepare_git_command(args, directory, request_env).map(|_| ())
}

pub(crate) fn prepare_git_command(
    args: &[String],
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<Vec<String>> {
    let resolved = resolve_git_aliases(args, directory, request_env)?;
    if command_overrides_git_target(&resolved) {
        return Err(alias_refusal(
            "Git path overrides introduced by a Git alias are not allowed; use working_dir for the assigned target",
        ));
    }
    validate_git_command_policy(&resolved, directory, request_env)?;
    protect_reference_mutation(&resolved, directory, request_env)
}

pub(crate) fn command_overrides_git_target(args: &[String]) -> bool {
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if is_target_override(argument) || config_option_overrides_worktree(argument) {
            return true;
        }
        if (argument == "-c" || argument == "--config-env")
            && args
                .get(index + 1)
                .is_some_and(|value| is_worktree_config_assignment(value))
        {
            return true;
        }
        if !argument.starts_with('-') {
            return false;
        }
        index += usize::from(global_option_takes_value(argument));
        index += 1;
    }
    false
}

fn is_target_override(argument: &str) -> bool {
    argument == "-C"
        || argument.starts_with("-C")
        || argument == "--git-dir"
        || argument.starts_with("--git-dir=")
        || argument == "--work-tree"
        || argument.starts_with("--work-tree=")
}

fn config_option_overrides_worktree(argument: &str) -> bool {
    if let Some(value) = argument.strip_prefix("--config-env=") {
        return is_worktree_config_assignment(value);
    }
    argument
        .strip_prefix("-c")
        .filter(|value| !value.is_empty())
        .is_some_and(is_worktree_config_assignment)
}

fn is_worktree_config_assignment(value: &str) -> bool {
    value
        .split_once('=')
        .is_some_and(|(key, _)| key.trim().eq_ignore_ascii_case("core.worktree"))
}

fn validate_git_command_policy(
    args: &[String],
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<()> {
    let Some((command_index, command)) = git_subcommand(args) else {
        return Ok(());
    };
    let command_args = &args[command_index + 1..];
    if command == "submodule" || command.starts_with("submodule--") {
        return validate_submodule_command(command, command_args);
    }
    if command == "worktree" && first_subcommand(command_args) != Some("list") {
        return Err(recursive_git_refusal(
            "direct git worktree mutations must use GitWorktreeService",
        ));
    }
    if matches!(command, "clone" | "init" | "init-db") {
        return Err(recursive_git_refusal(
            "git clone and init are not allowed inside an assigned worktree",
        ));
    }
    if command == "clean" && removes_nested_repositories(command_args) {
        return Err(recursive_git_refusal(
            "git clean cannot use multiple force flags to remove nested repositories",
        ));
    }

    if matches!(command, "fetch" | "pull" | "push" | "checkout" | "switch") {
        if recursive_config_override(args) || env_sets_recursive_git_config(request_env) {
            return Err(recursive_git_refusal(
                "managed Git process cannot override recursive submodule behavior",
            ));
        }
        if recurse_option_enabled(command_args) {
            return Err(recursive_git_refusal(
                "recursive Git operations cannot mutate nested repositories",
            ));
        }
        if !recurse_option_disabled(command_args)
            && effective_recursion_config_enables(command, directory, request_env)?
        {
            return Err(recursive_git_refusal(
                "Git configuration enables recursive mutation of nested repositories",
            ));
        }
    }
    Ok(())
}

fn protect_reference_mutation(
    args: &[String],
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<Vec<String>> {
    let Some((command_index, command)) = git_subcommand(args) else {
        return Ok(args.to_vec());
    };
    if !matches!(command, "add" | "commit" | "update-index") {
        return Ok(args.to_vec());
    }
    let snapshot = read_gitlink_snapshot(directory, request_env, None)?;
    snapshot
        .validate_gitlink_index_identity()
        .map_err(|reason| {
            recursive_git_refusal(&format!("reference Git mutation refused: {reason}"))
        })?;
    let reference_paths = snapshot
        .entries()
        .iter()
        .filter_map(|(path, entry)| {
            entry
                .head
                .as_ref()
                .is_some_and(|head| head.mode == 0o160000)
                .then_some(path.as_slice())
        })
        .collect::<Vec<_>>();
    if reference_paths.is_empty() {
        return Ok(args.to_vec());
    }
    match command {
        "update-index" => Err(recursive_git_refusal(
            "direct update-index is refused in a repository with reference gitlinks; use the scoped Git service",
        )),
        "commit" if commit_restages_paths(&args[command_index + 1..]) => Err(
            recursive_git_refusal(
                "commit options that stage worktree paths are refused in a repository with reference gitlinks; stage ordinary workspace paths first",
            ),
        ),
        "add" => append_reference_exclusions(args, command_index, &reference_paths),
        _ => Ok(args.to_vec()),
    }
}

pub(crate) fn validate_gitlink_index_identity(
    directory: &Path,
    request_env: &HashMap<String, String>,
    index_file: Option<&Path>,
) -> EffectResult<()> {
    read_gitlink_snapshot(directory, request_env, index_file)?
        .validate_gitlink_index_identity()
        .map_err(|reason| {
            recursive_git_refusal(&format!("reference Git mutation refused: {reason}"))
        })
}

pub(crate) fn is_git_commit(args: &[String]) -> bool {
    git_subcommand(args).is_some_and(|(_, command)| command == "commit")
}

fn resolve_git_aliases(
    args: &[String],
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<Vec<String>> {
    let mut resolved = args.to_vec();
    let mut seen = HashSet::new();
    let mut expanded = false;
    for _ in 0..MAX_GIT_ALIAS_EXPANSIONS {
        let Some((command_index, command)) = git_subcommand(&resolved) else {
            return Ok(resolved);
        };
        if is_git_builtin(command)? {
            return Ok(resolved);
        }
        if !seen.insert(command.to_string()) {
            return Err(alias_refusal("Git alias expansion contains a cycle"));
        }
        let Some(value) = configured_git_alias(&resolved, command_index, directory, request_env)?
        else {
            if expanded {
                return Err(alias_refusal(
                    "Git alias resolves to an external or unknown command that cannot be checked",
                ));
            }
            return Ok(resolved);
        };
        if value.trim_start().starts_with('!') {
            return Err(alias_refusal(
                "shell Git aliases are not allowed through managed process.run",
            ));
        }
        let expansion = split_git_alias(&value)
            .filter(|tokens| !tokens.is_empty())
            .ok_or_else(|| alias_refusal("Git alias has an invalid or empty expansion"))?;
        if expansion[0].starts_with('!') {
            return Err(alias_refusal(
                "shell Git aliases are not allowed through managed process.run",
            ));
        }
        let mut replacement = expansion;
        replacement.extend_from_slice(&resolved[command_index + 1..]);
        resolved.splice(command_index..=command_index, replacement);
        expanded = true;
    }
    Err(alias_refusal(
        "Git alias expansion exceeded its bounded depth",
    ))
}

fn configured_git_alias(
    args: &[String],
    command_index: usize,
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<Option<String>> {
    let mut command = Command::new("git");
    append_git_config_options(&mut command, args, command_index);
    let alias_key = format!("alias.{}", args[command_index]);
    let output = command
        .args(["config", "--null", "--get", alias_key.as_str()])
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_OBJECT_DIRECTORY_RELATIVE")
        .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        .output()
        .map_err(|_| alias_refusal("Git alias configuration could not be inspected"))?;
    if output.status.code() == Some(1) {
        return Ok(None);
    }
    if !output.status.success() {
        return Err(alias_refusal(
            "Git alias configuration could not be inspected",
        ));
    }
    let bytes = output.stdout.strip_suffix(&[0]).unwrap_or(&output.stdout);
    let value = String::from_utf8(bytes.to_vec())
        .map_err(|_| alias_refusal("Git alias configuration is not valid UTF-8"))?;
    Ok(Some(value))
}

fn append_git_config_options(command: &mut Command, args: &[String], command_index: usize) {
    let mut index = 0;
    while index < command_index {
        let argument = args[index].as_str();
        match argument {
            "-c" | "--config-env" => {
                if let Some(value) = args.get(index + 1) {
                    command.arg(argument).arg(value);
                }
                index += 2;
            }
            _ if argument.starts_with("--config-env=")
                || (argument.starts_with("-c") && argument.len() > 2) =>
            {
                command.arg(argument);
                index += 1;
            }
            _ => index += 1 + usize::from(global_option_takes_value(argument)),
        }
    }
}

fn is_git_builtin(command: &str) -> EffectResult<bool> {
    static BUILTINS: OnceLock<Result<HashSet<String>, String>> = OnceLock::new();
    match BUILTINS.get_or_init(read_git_builtins) {
        Ok(builtins) => Ok(builtins.contains(command)),
        Err(reason) => Err(alias_refusal(reason)),
    }
}

fn read_git_builtins() -> Result<HashSet<String>, String> {
    let output = Command::new("git")
        .arg("--list-cmds=builtins")
        .output()
        .map_err(|_| "Git builtin command list could not be inspected".to_string())?;
    if !output.status.success() {
        return Err("Git builtin command list could not be inspected".to_string());
    }
    let commands = std::str::from_utf8(&output.stdout)
        .map_err(|_| "Git builtin command list is not valid UTF-8".to_string())?;
    Ok(commands.lines().map(str::to_string).collect())
}

fn split_git_alias(value: &str) -> Option<Vec<String>> {
    let mut tokens = Vec::new();
    let mut token = String::new();
    let mut quote = None;
    let mut escaped = false;
    let mut started = false;
    for character in value.chars() {
        if character.is_control() {
            return None;
        }
        if escaped {
            token.push(character);
            escaped = false;
            started = true;
            continue;
        }
        if character == '\\' && quote != Some('\'') {
            escaped = true;
            started = true;
            continue;
        }
        if matches!(character, '\'' | '"') {
            match quote {
                Some(open) if open == character => quote = None,
                None => quote = Some(character),
                _ => token.push(character),
            }
            started = true;
            continue;
        }
        if character.is_whitespace() && quote.is_none() {
            if started {
                tokens.push(std::mem::take(&mut token));
                started = false;
            }
            continue;
        }
        token.push(character);
        started = true;
    }
    if escaped || quote.is_some() {
        return None;
    }
    if started {
        tokens.push(token);
    }
    Some(tokens)
}

fn alias_refusal(message: &str) -> EffectError {
    EffectError::custom("git_mutation_unavailable", message)
}

fn read_gitlink_snapshot(
    directory: &Path,
    request_env: &HashMap<String, String>,
    index_file: Option<&Path>,
) -> EffectResult<GitEntrySnapshot> {
    let index = git_readonly(
        directory,
        request_env,
        &["ls-files", "--stage", "-z"],
        index_file,
    )?;
    let head = match git_readonly(
        directory,
        request_env,
        &["ls-tree", "-r", "-z", "HEAD"],
        None,
    ) {
        Ok(head) => head,
        Err(_) if unborn_branch_exists(directory, request_env)? => Vec::new(),
        Err(error) => return Err(error),
    };
    GitEntrySnapshot::parse(&index, &head, &[]).map_err(|reason| {
        recursive_git_refusal(&format!(
            "reference Git state could not be verified: {reason}"
        ))
    })
}

fn unborn_branch_exists(
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<bool> {
    let output = Command::new("git")
        .args(["symbolic-ref", "--quiet", "HEAD"])
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        .output()
        .map_err(|_| recursive_git_refusal("Git HEAD could not be inspected"))?;
    Ok(output.status.success())
}

fn git_readonly(
    directory: &Path,
    request_env: &HashMap<String, String>,
    args: &[&str],
    index_file: Option<&Path>,
) -> EffectResult<Vec<u8>> {
    let mut command = Command::new("git");
    command
        .args(args)
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES");
    if let Some(index_file) = index_file {
        command.env("GIT_INDEX_FILE", index_file);
    }
    let output = command
        .output()
        .map_err(|_| recursive_git_refusal("reference Git state could not be inspected"))?;
    if output.status.success() {
        Ok(output.stdout)
    } else {
        Err(recursive_git_refusal(
            "reference Git state could not be inspected",
        ))
    }
}

fn commit_restages_paths(args: &[String]) -> bool {
    if args.iter().any(|argument| {
        matches!(
            argument.as_str(),
            "-a" | "--all"
                | "--include"
                | "-i"
                | "--only"
                | "-o"
                | "--interactive"
                | "--patch"
                | "-p"
                | "--pathspec-from-file"
                | "--pathspec-file-nul"
        ) || argument.starts_with("--pathspec-from-file=")
    }) {
        return true;
    }
    commit_has_pathspec(args)
}

fn commit_has_pathspec(args: &[String]) -> bool {
    let mut index = 0;
    while index < args.len() {
        let argument = args[index].as_str();
        if argument == "--" || !argument.starts_with('-') {
            return true;
        }
        if argument.starts_with("--") {
            index += 1 + commit_long_option_takes_value(argument);
        } else if commit_short_option_restages_paths(argument) {
            return true;
        } else {
            index += 1 + commit_short_option_takes_value(argument);
        }
    }
    false
}

fn commit_long_option_takes_value(argument: &str) -> usize {
    let option = argument.split_once('=').map_or(argument, |(name, _)| name);
    if argument.contains('=') {
        return 0;
    }
    usize::from(matches!(
        option,
        "--message"
            | "--file"
            | "--author"
            | "--date"
            | "--reuse-message"
            | "--reedit-message"
            | "--fixup"
            | "--squash"
            | "--cleanup"
            | "--template"
            | "--trailer"
            | "--pathspec-from-file"
    ))
}

fn commit_short_option_restages_paths(argument: &str) -> bool {
    for character in argument.chars().skip(1) {
        if matches!(character, 'a' | 'i' | 'o' | 'p') {
            return true;
        }
        if matches!(character, 'm' | 'F' | 'C' | 'c' | 't') {
            return false;
        }
    }
    false
}

fn commit_short_option_takes_value(argument: &str) -> usize {
    let mut characters = argument.chars().skip(1).peekable();
    while let Some(character) = characters.next() {
        if matches!(character, 'm' | 'F' | 'C' | 'c' | 't') {
            return usize::from(characters.peek().is_none());
        }
        if character == 'S' {
            return 0;
        }
    }
    0
}

fn append_reference_exclusions(
    args: &[String],
    command_index: usize,
    references: &[&[u8]],
) -> EffectResult<Vec<String>> {
    if args.iter().any(|argument| {
        argument == "--pathspec-from-file" || argument.starts_with("--pathspec-from-file=")
    }) {
        return Err(recursive_git_refusal(
            "git add pathspec files cannot be verified against reference boundaries",
        ));
    }
    let command_args = &args[command_index + 1..];
    let has_delimiter = command_args.iter().any(|argument| argument == "--");
    let has_pathspec = pathspecs_after_options(command_args);
    let mut prepared = args.to_vec();
    if !has_delimiter {
        prepared.push("--".into());
    }
    if !has_pathspec {
        prepared.push(".".into());
    }
    for reference in references {
        let path = std::str::from_utf8(reference).map_err(|_| {
            recursive_git_refusal("non-UTF-8 reference path cannot be excluded safely from git add")
        })?;
        prepared.push(format!(":(top,exclude,literal){path}"));
    }
    Ok(prepared)
}

fn pathspecs_after_options(args: &[String]) -> bool {
    if let Some(delimiter) = args.iter().position(|argument| argument == "--") {
        return delimiter + 1 < args.len();
    }
    args.iter().any(|argument| !argument.starts_with('-'))
}

fn recursive_git_refusal(message: &str) -> EffectError {
    EffectError::custom("git_mutation_unavailable", message)
}

fn validate_submodule_command(command: &str, args: &[String]) -> EffectResult<()> {
    if command == "submodule" && matches!(first_subcommand(args), Some("status" | "summary")) {
        return Ok(());
    }
    Err(recursive_git_refusal(
        "only read-only git submodule status and summary are allowed through process.run",
    ))
}

fn first_subcommand(args: &[String]) -> Option<&str> {
    args.iter()
        .map(String::as_str)
        .find(|argument| !argument.starts_with('-'))
}

fn git_subcommand(args: &[String]) -> Option<(usize, &str)> {
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if !argument.starts_with('-') || argument == "-" {
            return Some((index, argument));
        }
        if argument == "--" {
            let index = index + 1;
            return args.get(index).map(|command| (index, command.as_str()));
        }
        index += 1 + usize::from(global_option_takes_value(argument));
    }
    None
}

fn recursive_config_override(args: &[String]) -> bool {
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if !argument.starts_with('-') || argument == "-" {
            return false;
        }
        if let Some(value) = config_option_value(args, index, argument) {
            if is_recursive_git_config_assignment(value) {
                return true;
            }
        }
        index += 1 + usize::from(global_option_takes_value(argument));
    }
    false
}

fn config_option_value<'a>(args: &'a [String], index: usize, argument: &'a str) -> Option<&'a str> {
    match argument {
        "-c" | "--config-env" => args.get(index + 1).map(String::as_str),
        _ if argument.starts_with("--config-env=") => argument
            .strip_prefix("--config-env=")?
            .split_once('=')
            .map(|(key, _)| key),
        _ if argument.starts_with("-c") && argument.len() > 2 => Some(&argument[2..]),
        _ => None,
    }
}

fn is_recursive_git_config_assignment(assignment: &str) -> bool {
    let key = assignment
        .split_once('=')
        .map_or(assignment, |(key, _)| key);
    is_recursive_git_config_key(key)
}

fn is_recursive_git_config_key(key: &str) -> bool {
    let normalized = key
        .chars()
        .filter(|character| !matches!(character, '-' | '_'))
        .flat_map(char::to_lowercase)
        .collect::<String>();
    matches!(
        normalized.as_str(),
        "submodule.recurse"
            | "fetch.recursesubmodules"
            | "push.recursesubmodules"
            | "checkout.recursesubmodules"
    )
}

fn env_sets_recursive_git_config(environment: &HashMap<String, String>) -> bool {
    if environment
        .keys()
        .any(|key| key.eq_ignore_ascii_case("GIT_CONFIG_PARAMETERS"))
    {
        return true;
    }
    let Some(count) = environment
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case("GIT_CONFIG_COUNT"))
        .map(|(_, value)| value)
    else {
        return false;
    };
    let Ok(count) = count.parse::<usize>() else {
        return true;
    };
    if count > MAX_GIT_CONFIG_ENV_ITEMS {
        return true;
    }
    (0..count).any(|index| {
        environment
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(&format!("GIT_CONFIG_KEY_{index}")))
            .is_some_and(|(_, value)| is_recursive_git_config_key(value))
    })
}

fn recurse_option_enabled(args: &[String]) -> bool {
    args.iter()
        .take_while(|argument| argument.as_str() != "--")
        .any(|argument| {
            let argument = argument.as_str();
            argument == "--recurse-submodules"
                || argument
                    .strip_prefix("--recurse-submodules=")
                    .is_some_and(|value| !is_disabled_config_value(value))
        })
}

fn recurse_option_disabled(args: &[String]) -> bool {
    args.iter()
        .take_while(|argument| argument.as_str() != "--")
        .any(|argument| {
            let argument = argument.as_str();
            argument == "--no-recurse-submodules"
                || argument
                    .strip_prefix("--recurse-submodules=")
                    .is_some_and(is_disabled_config_value)
        })
}

fn is_disabled_config_value(value: &str) -> bool {
    matches!(
        value.to_ascii_lowercase().as_str(),
        "false" | "no" | "off" | "0"
    )
}

fn removes_nested_repositories(args: &[String]) -> bool {
    let mut force_count = 0;
    let mut index = 0;
    while let Some(argument) = args.get(index).map(String::as_str) {
        if argument == "--" {
            break;
        }
        if argument.starts_with("--for") {
            force_count += 1;
        } else if argument.starts_with('-') && !argument.starts_with("--") {
            force_count += argument[1..].chars().filter(|flag| *flag == 'f').count();
            if argument[1..].contains('e') {
                index += 1;
            }
        }
        if force_count >= 2 {
            return true;
        }
        index += 1;
    }
    false
}

fn effective_recursion_config_enables(
    command: &str,
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<bool> {
    let mut keys = vec!["submodule.recurse"];
    match command {
        "fetch" => keys.push("fetch.recurseSubmodules"),
        "push" => keys.push("push.recurseSubmodules"),
        "checkout" | "switch" => keys.push("checkout.recurseSubmodules"),
        "pull" => {
            keys.push("fetch.recurseSubmodules");
            keys.push("checkout.recurseSubmodules");
        }
        _ => {}
    }
    for key in keys {
        if config_value_enables_recursion(key, directory, request_env)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn config_value_enables_recursion(
    key: &str,
    directory: &Path,
    request_env: &HashMap<String, String>,
) -> EffectResult<bool> {
    let output = Command::new("git")
        .args(["config", "--get", key])
        .current_dir(directory)
        .envs(request_env)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR")
        .env_remove("GIT_INDEX_FILE")
        .env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_OBJECT_DIRECTORY_RELATIVE")
        .env_remove("GIT_QUARANTINE_PATH")
        .env_remove("GIT_SHALLOW_FILE")
        .output()
        .map_err(|_| recursion_config_unavailable())?;
    if output.status.code() == Some(1) {
        return Ok(false);
    }
    if !output.status.success() {
        return Err(recursion_config_unavailable());
    }
    let value = std::str::from_utf8(&output.stdout)
        .map_err(|_| recursion_config_unavailable())?
        .trim();
    if value.is_empty() {
        return Err(recursion_config_unavailable());
    }
    Ok(config_value_enables_recursive_mutation(key, value))
}

fn config_value_enables_recursive_mutation(key: &str, value: &str) -> bool {
    let value = value.to_ascii_lowercase();
    if is_disabled_config_value(&value) {
        return false;
    }
    if key.eq_ignore_ascii_case("push.recurseSubmodules") && value == "check" {
        return false;
    }
    true
}

fn recursion_config_unavailable() -> EffectError {
    EffectError::custom(
        "git_mutation_unavailable",
        "recursive Git configuration could not be checked; no process was started",
    )
}

fn global_option_takes_value(argument: &str) -> bool {
    matches!(
        argument,
        "-c" | "--config-env"
            | "--namespace"
            | "--super-prefix"
            | "--git-dir"
            | "--work-tree"
            | "-C"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn isolated_config_environment() -> HashMap<String, String> {
        HashMap::from([
            ("GIT_CONFIG_NOSYSTEM".into(), "1".into()),
            ("GIT_CONFIG_GLOBAL".into(), "/dev/null".into()),
        ])
    }

    #[test]
    fn process_git_command_policy_honors_effective_and_injected_recursive_config() {
        let directory = tempfile::tempdir().unwrap();
        let environment = isolated_config_environment();
        let initialized = Command::new("git")
            .args(["init", "-q", "-b", "main"])
            .current_dir(directory.path())
            .output()
            .unwrap();
        assert!(initialized.status.success());
        let configured = Command::new("git")
            .args(["config", "--local", "submodule.recurse", "true"])
            .current_dir(directory.path())
            .output()
            .unwrap();
        assert!(configured.status.success());

        assert!(validate_git_command(&["fetch".into()], directory.path(), &environment).is_err());
        assert!(validate_git_command(
            &["fetch".into(), "--no-recurse-submodules".into()],
            directory.path(),
            &environment,
        )
        .is_ok());
        assert!(validate_git_command(
            &[
                "fetch".into(),
                "--".into(),
                "--no-recurse-submodules".into()
            ],
            directory.path(),
            &environment,
        )
        .is_err());
        assert!(validate_git_command(
            &[
                "-c".into(),
                "submodule.recurse=false".into(),
                "fetch".into()
            ],
            directory.path(),
            &environment,
        )
        .is_err());

        let injected = HashMap::from([
            ("GIT_CONFIG_COUNT".into(), "1".into()),
            ("GIT_CONFIG_KEY_0".into(), "fetch.recurseSubmodules".into()),
            ("GIT_CONFIG_VALUE_0".into(), "no".into()),
        ]);
        let injected = environment
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .chain(injected)
            .collect::<HashMap<_, _>>();
        assert!(validate_git_command(&["fetch".into()], directory.path(), &injected).is_err());

        let update = Command::new("git")
            .args(["config", "--local", "submodule.recurse", "false"])
            .current_dir(directory.path())
            .output()
            .unwrap();
        assert!(update.status.success());
        assert!(validate_git_command(&["fetch".into()], directory.path(), &environment).is_ok());
    }
}
