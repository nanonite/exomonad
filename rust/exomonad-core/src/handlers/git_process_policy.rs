//! Guard Git commands launched through process.run from mutating nested repositories.

use crate::effects::{EffectError, EffectResult};
use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

const MAX_GIT_CONFIG_ENV_ITEMS: usize = 128;

pub(crate) fn validate_git_command(
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
