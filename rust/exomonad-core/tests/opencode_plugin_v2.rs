//! Integration checks for the generated OpenCode plugin package.
//!
//! The shape assertions are unit-level and always run. The end-to-end probes are
//! opt-in because they launch the real `opencode` CLI and a model; they skip
//! cleanly when either is unavailable.
//!
//! `captured_codemode_inner_calls_normalize_to_guarded_ids` replays the hook
//! sequence a real OpenCode v2.3.2 code-mode `execute` produced (captured in
//! `fixtures/opencode_v2_codemode_hook_events.jsonl`) through the production
//! adapter, so a mapping change that collapses an inner call into the outer
//! `execute` id fails without a model call.
//!
//! `codemode_inner_calls_are_hooked_individually_when_enabled` re-measures that
//! sequence against the real CLI with the production plugin, and fails when
//! inner `tools.<name>` calls stop reaching `execute.before`.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const PROBE_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE";
const PROBE_MODEL_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE_MODEL";
const DEFAULT_PROBE_MODEL: &str = "opencode-go/beam-free";

/// The hook events OpenCode v2.3.2 delivered for one code-mode `execute` whose
/// script called `tools.shell` and `tools.write`, captured with
/// `experimental.full_codemode: true`. `shell` has no `after` event because the
/// probe hook denied it, so the recorded sequence is also the evidence that a
/// deny reaches an inner call.
const CODEMODE_HOOK_EVENTS: &str = include_str!("fixtures/opencode_v2_codemode_hook_events.jsonl");

const PRE_TOOL_USE_ARGV: &str = "hook pre-tool-use --runtime opencode";
const POST_TOOL_USE_ARGV: &str = "hook post-tool-use --runtime opencode";

fn write_generated_plugin(dir: &Path) {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build current-thread runtime");
    runtime.block_on(async {
        exomonad_core::services::agent_control::AgentControlService::<
            exomonad_core::services::Services,
        >::write_opencode_plugin_files(dir)
        .await
        .expect("write generated OpenCode plugin");
    });
}

fn probe_enabled() -> bool {
    std::env::var(PROBE_ENV).as_deref() == Ok("1")
}

fn probe_version_or_skip() -> Option<String> {
    let version = Command::new("opencode").arg("--version").output();
    match version {
        Ok(output) if output.status.success() => {
            Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
        }
        _ => {
            eprintln!("skipping real OpenCode probe: `opencode` is not available");
            None
        }
    }
}

fn probe_model() -> String {
    std::env::var(PROBE_MODEL_ENV).unwrap_or_else(|_| DEFAULT_PROBE_MODEL.to_string())
}

fn probe_path(bin_dir: &Path) -> String {
    format!(
        "{}:{}",
        bin_dir.display(),
        std::env::var("PATH").unwrap_or_default()
    )
}

fn write_executable(path: &Path, script: &str) {
    std::fs::write(path, script).expect("write shim");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).expect("chmod shim");
    }
}

fn shim_dir(root: &Path, shim: &str) -> PathBuf {
    let bin_dir = root.join("bin");
    std::fs::create_dir_all(&bin_dir).expect("create shim dir");
    write_executable(&bin_dir.join("exomonad"), shim);
    bin_dir
}

/// Launch one one-shot OpenCode session in `root` against the free probe model.
fn launch_probe(root: &Path, bin_dir: &Path, hook_log: &Path, prompt: &str) -> Output {
    let config_home = root.join("xdg");
    std::fs::create_dir_all(&config_home).expect("create config home");
    Command::new("timeout")
        .arg("300")
        .args([
            "opencode",
            "run",
            "--standalone",
            "--auto",
            "--print-logs",
            "--log-level",
            "warn",
            "--format",
            "json",
            "--model",
            &probe_model(),
        ])
        .arg(prompt)
        .current_dir(root)
        .env("PWD", root)
        .env("PATH", probe_path(bin_dir))
        .env("XDG_CONFIG_HOME", &config_home)
        .env("HOOK_LOG", hook_log)
        .output()
        .expect("launch opencode")
}

#[derive(Debug)]
struct HookCall {
    argv: String,
    input: serde_json::Value,
}

/// Parse the paired `argv:` / `stdin:` lines the probe shim appends per call.
fn hook_calls(log: &str) -> Vec<HookCall> {
    let mut calls = Vec::new();
    let mut pending_argv: Option<String> = None;
    for line in log.lines() {
        if let Some(argv) = line.strip_prefix("argv: ") {
            pending_argv = Some(argv.to_string());
        } else if let Some(body) = line.strip_prefix("stdin: ") {
            let Some(argv) = pending_argv.take() else {
                continue;
            };
            let Ok(input) = serde_json::from_str(body) else {
                continue;
            };
            calls.push(HookCall { argv, input });
        }
    }
    calls
}

fn tool_names<'a>(calls: &'a [&HookCall]) -> Vec<&'a str> {
    calls
        .iter()
        .map(|call| call.input["tool_name"].as_str().unwrap_or_default())
        .collect()
}

#[test]
fn generated_plugin_files_match_production_constants() {
    let tmp = tempfile::tempdir().expect("tempdir");
    write_generated_plugin(tmp.path());

    let index = std::fs::read_to_string(tmp.path().join(".exo/opencode-plugin/index.ts"))
        .expect("read generated index.ts");
    let package = std::fs::read_to_string(tmp.path().join(".exo/opencode-plugin/package.json"))
        .expect("read generated package.json");

    assert_eq!(index, exomonad_core::opencode_plugin::OPENCODE_PLUGIN_TS);
    assert_eq!(
        package,
        exomonad_core::opencode_plugin::OPENCODE_PLUGIN_PKG_JSON
    );
}

/// The captured real-CLI sequence must survive the production adapter as one
/// PreToolUse per call, each under its own id. If the table ever collapses an
/// inner call onto `execute` (or stops recognizing one), the role guardrail for
/// `shell`/`write` stops matching and this fails without a model call.
#[test]
fn captured_codemode_inner_calls_normalize_to_guarded_ids() {
    use exomonad_core::{
        normalize_opencode_hook_payload, opencode_unknown_tool_deny, HookEventType,
    };

    let before: Vec<serde_json::Value> = CODEMODE_HOOK_EVENTS
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| -> serde_json::Value {
            serde_json::from_str(line).expect("fixture line is JSON")
        })
        .filter(|event| event["event"] == "before")
        .collect();

    assert_eq!(
        before
            .iter()
            .map(|event| &event["tool"])
            .collect::<Vec<_>>(),
        ["execute", "shell", "write"],
        "fixture must record the captured execute call followed by its two inner calls"
    );

    let mut call_ids = Vec::new();
    for event in &before {
        let tool = event["tool"].as_str().expect("tool");
        let call_id = event["id"].as_str().expect("call id");
        let raw = serde_json::json!({
            "tool_name": tool,
            "args": event["input"],
            "call_id": call_id,
            "session_id": "ses_codemode_capture",
        });

        assert!(
            opencode_unknown_tool_deny(HookEventType::PreToolUse, &raw).is_none(),
            "captured code-mode id `{tool}` must not be denied as unknown"
        );

        let normalized = normalize_opencode_hook_payload(HookEventType::PreToolUse, raw);
        assert_eq!(
            normalized["tool_name"], tool,
            "captured code-mode id `{tool}` must reach PreToolUse under its own canonical id"
        );
        assert_eq!(normalized["tool_use_id"], call_id);
        assert_eq!(
            normalized["tool_input"], event["input"],
            "the inner call keeps its own arguments"
        );
        call_ids.push(call_id.to_string());
    }

    call_ids.sort_unstable();
    call_ids.dedup();
    assert_eq!(
        call_ids.len(),
        3,
        "each captured call has a distinct call id, so inner calls are hooked individually"
    );
}

#[test]
fn generated_plugin_loads_and_invokes_hooks_under_real_cli_when_enabled() {
    if !probe_enabled() {
        eprintln!("skipping real OpenCode probe: set {PROBE_ENV}=1 to enable");
        return;
    }
    let Some(version) = probe_version_or_skip() else {
        return;
    };
    eprintln!("running real OpenCode probe against {version}");

    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().to_path_buf();
    write_generated_plugin(&root);

    std::fs::write(
        root.join("opencode.json"),
        r#"{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["./.exo/opencode-plugin"],
  "permission": "allow"
}
"#,
    )
    .expect("write opencode.json");

    let _ = Command::new("git")
        .args(["init", "-q"])
        .current_dir(&root)
        .status();

    let hook_log = root.join("hooks.log");
    let bin_dir = shim_dir(
        &root,
        r#"#!/usr/bin/env bash
input=$(cat)
{
  printf 'argv: %s\n' "$*"
  printf 'stdin: %s\n' "$input"
} >> "${HOOK_LOG:?HOOK_LOG must be set}"
printf '%s\n' '{"continue":true}'
"#,
    );

    let output = launch_probe(
        &root,
        &bin_dir,
        &hook_log,
        "Run exactly one bash tool call that prints EXO_PLUGIN_PROBE, then reply DONE.",
    );

    assert!(
        output.status.success(),
        "opencode probe failed: status={:?}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let log = std::fs::read_to_string(&hook_log)
        .unwrap_or_else(|_| String::from("plugin did not invoke the exomonad hook"));
    let calls = hook_calls(&log);
    let pre: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == PRE_TOOL_USE_ARGV)
        .collect();
    let post: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == POST_TOOL_USE_ARGV)
        .collect();

    assert!(
        tool_names(&pre).contains(&"shell"),
        "missing pre-tool-use hook for the shell call; log:\n{log}"
    );
    assert!(
        tool_names(&post).contains(&"shell"),
        "missing post-tool-use hook for the shell call; log:\n{log}"
    );
}

#[test]
fn codemode_inner_calls_are_hooked_individually_when_enabled() {
    if !probe_enabled() {
        eprintln!("skipping real OpenCode code-mode probe: set {PROBE_ENV}=1 to enable");
        return;
    }
    let Some(version) = probe_version_or_skip() else {
        return;
    };
    eprintln!("running real OpenCode code-mode probe against {version}");

    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().to_path_buf();
    write_generated_plugin(&root);

    // `execute` exists in the default tool set, but its inner tool catalog is
    // empty unless full code mode is enabled. The probe enables it so the model
    // can wrap `shell` and `write` inside one `execute` script.
    std::fs::write(
        root.join("opencode.json"),
        r#"{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["./.exo/opencode-plugin"],
  "permission": "allow",
  "experimental": {
    "full_codemode": true
  }
}
"#,
    )
    .expect("write opencode.json");

    let _ = Command::new("git")
        .args(["init", "-q"])
        .current_dir(&root)
        .status();

    let hook_log = root.join("hooks.log");
    let bin_dir = shim_dir(
        &root,
        r#"#!/usr/bin/env bash
input=$(cat)
{
  printf 'argv: %s\n' "$*"
  printf 'stdin: %s\n' "$input"
} >> "${HOOK_LOG:?HOOK_LOG must be set}"
if [ "${1:-}" = "hook" ] && [ "${2:-}" = "pre-tool-use" ]; then
  case "$input" in
    *'"tool_name":"shell"'*EXO_DENY_ME*)
      printf '%s\n' '{"continue":false,"stopReason":"probe deny: shell blocked inside execute"}'
      exit 2
      ;;
  esac
fi
printf '%s\n' '{"continue":true}'
"#,
    );

    let output = launch_probe(
        &root,
        &bin_dir,
        &hook_log,
        "You must use the code-mode `execute` tool exactly once for this task. \
         Its JavaScript must call tools.shell with command \"echo EXO_DENY_ME\" and \
         separately call tools.write to create allowed-code-mode.txt with content \
         \"allowed write\". Use Promise.allSettled so both are attempted, print each \
         result, then reply DONE.",
    );

    assert!(
        output.status.success(),
        "opencode code-mode probe failed: status={:?}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let log = std::fs::read_to_string(&hook_log)
        .unwrap_or_else(|_| String::from("plugin did not invoke the exomonad hook"));
    let calls = hook_calls(&log);
    let pre: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == PRE_TOOL_USE_ARGV)
        .collect();
    let pre_tools = tool_names(&pre);

    assert_eq!(
        pre_tools.first(),
        Some(&"execute"),
        "the model must have called `execute` first; observed {pre_tools:?}; log:\n{log}"
    );
    assert!(
        pre_tools.contains(&"shell"),
        "inner tools.shell call did not reach execute.before under its own id; \
         observed {pre_tools:?}; log:\n{log}"
    );
    assert!(
        pre_tools.contains(&"write"),
        "inner tools.write call did not reach execute.before under its own id; \
         observed {pre_tools:?}; log:\n{log}"
    );

    let mut call_ids: Vec<&str> = pre
        .iter()
        .map(|call| call.input["call_id"].as_str().unwrap_or_default())
        .collect();
    call_ids.sort_unstable();
    call_ids.dedup();
    assert_eq!(
        call_ids.len(),
        pre.len(),
        "each hooked call has its own call id; log:\n{log}"
    );

    let post: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == POST_TOOL_USE_ARGV)
        .collect();
    let post_tools = tool_names(&post);
    assert!(
        !post_tools.contains(&"shell"),
        "a denied inner shell call must never run, so it has no post-tool-use hook; \
         observed {post_tools:?}; log:\n{log}"
    );
    assert!(
        post_tools.contains(&"write"),
        "the allowed inner write must have run; observed {post_tools:?}; log:\n{log}"
    );
    assert_eq!(
        std::fs::read_to_string(root.join("allowed-code-mode.txt")).ok(),
        Some("allowed write".to_string()),
        "the allowed inner write must have created its file"
    );
    assert!(
        String::from_utf8_lossy(&output.stdout)
            .contains("probe deny: shell blocked inside execute"),
        "the deny reason must surface to the model; stdout:\n{}",
        String::from_utf8_lossy(&output.stdout)
    );
}
