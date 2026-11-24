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
//!
//! `captured_mini_hook_payloads_normalize_to_tool_input_and_response` replays the
//! pre/post/stop payloads an interactive `opencode mini` v2.3.2 session produced
//! (captured in `fixtures/opencode_v2_mini_hook_events.jsonl`) through the
//! production adapter.
//!
//! `mini_interactive_hooks_when_enabled` drives a real `opencode mini` session in
//! a private tmux server and checks hook delivery, a role deny, the per-turn
//! `stop`, and the process-end `worker-exit` that `/exit` produces.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const PROBE_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE";
const PROBE_MODEL_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE_MODEL";
const DEFAULT_PROBE_MODEL: &str = "opencode-go/beam-free";

/// The hook events OpenCode v2.3.2 delivered for one code-mode `execute` whose
/// script called `tools.shell` and `tools.write`, captured with
/// `experimental.full_codemode: true`. `shell` has no `after` event because the
/// probe hook denied it, so the recorded sequence is also the evidence that a
/// deny reaches an inner call.
const CODEMODE_HOOK_EVENTS: &str = include_str!("fixtures/opencode_v2_codemode_hook_events.jsonl");
const MINI_HOOK_EVENTS: &str = include_str!("fixtures/opencode_v2_mini_hook_events.jsonl");

const PRE_TOOL_USE_ARGV: &str = "hook pre-tool-use --runtime opencode";
const POST_TOOL_USE_ARGV: &str = "hook post-tool-use --runtime opencode";
const STOP_ARGV: &str = "hook stop --runtime opencode";
const WORKER_EXIT_ARGV: &str = "hook worker-exit --runtime opencode";

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

/// The captured interactive `opencode mini` payloads must reach the internal
/// shape the role hooks read: `args` as `tool_input`, `output` as
/// `tool_response`, and the event's session id for the terminal stop hook.
#[test]
fn captured_mini_hook_payloads_normalize_to_tool_input_and_response() {
    use exomonad_core::{normalize_opencode_hook_payload, HookEventType};

    let events: Vec<serde_json::Value> = MINI_HOOK_EVENTS
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).expect("fixture line is JSON"))
        .collect();

    let before = events
        .iter()
        .find(|event| event["event"] == "before")
        .expect("captured pre-tool-use event");
    let after = events
        .iter()
        .find(|event| event["event"] == "after")
        .expect("captured post-tool-use event");
    let stop = events
        .iter()
        .find(|event| event["event"] == "stop")
        .expect("captured terminal event");

    let pre = normalize_opencode_hook_payload(
        HookEventType::PreToolUse,
        serde_json::json!({
            "tool_name": before["tool"],
            "session_id": before["session_id"],
            "call_id": before["id"],
            "args": before["input"],
        }),
    );
    assert_eq!(pre["tool_name"], "shell");
    assert_eq!(pre["tool_input"], before["input"]);
    assert_eq!(pre["tool_use_id"], before["id"]);
    assert_eq!(pre["hook_event_name"], "PreToolUse");
    assert_eq!(pre["session_id"], before["session_id"]);

    let post = normalize_opencode_hook_payload(
        HookEventType::PostToolUse,
        serde_json::json!({
            "tool_name": after["tool"],
            "session_id": after["session_id"],
            "call_id": after["id"],
            "args": after["input"],
            "output": after["output"],
        }),
    );
    assert_eq!(post["tool_name"], "shell");
    assert_eq!(post["tool_response"], after["output"]);
    assert_eq!(post["tool_use_id"], after["id"]);
    assert_eq!(post["hook_event_name"], "PostToolUse");

    let terminal = normalize_opencode_hook_payload(HookEventType::Stop, stop["payload"].clone());
    assert_eq!(terminal["hook_event_name"], "Stop");
    assert_eq!(terminal["session_id"], stop["payload"]["data"]["sessionID"]);
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

fn tmux_available() -> bool {
    Command::new("tmux")
        .arg("-V")
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn read_or_empty(path: &Path) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

fn wait_until(timeout: Duration, predicate: impl Fn() -> bool) -> bool {
    let start = Instant::now();
    loop {
        if predicate() {
            return true;
        }
        if start.elapsed() >= timeout {
            return false;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

/// A tmux server on a private `-S` socket, so the probe never touches the
/// operator's default server. Dropping it kills only that private server.
struct PrivateTmux {
    socket: PathBuf,
}

impl PrivateTmux {
    fn new(root: &Path) -> Self {
        Self {
            socket: root.join("t.sock"),
        }
    }

    fn run(&self, args: &[&str]) -> Output {
        Command::new("tmux")
            .arg("-S")
            .arg(&self.socket)
            .args(args)
            .env_remove("TMUX")
            .env_remove("TMUX_PANE")
            .output()
            .expect("run tmux")
    }

    fn session_alive(&self, session: &str) -> bool {
        self.run(&["has-session", "-t", session]).status.success()
    }

    fn pane(&self, session: &str) -> String {
        let output = self.run(&["capture-pane", "-p", "-S", "-500", "-t", session]);
        String::from_utf8_lossy(&output.stdout).into_owned()
    }
}

impl Drop for PrivateTmux {
    fn drop(&mut self) {
        let _ = self.run(&["kill-server"]);
    }
}

fn launch_mini(root: &Path, bin_dir: &Path, hook_log: &Path, prompt: &str, tmux: &PrivateTmux) {
    let xdg = root.join("xdg");
    std::fs::create_dir_all(&xdg).expect("create config home");
    let launcher = root.join("launch-mini.sh");
    std::fs::write(
        &launcher,
        format!(
            "#!/usr/bin/env bash\nset -e\nexport PATH={bin}:$PATH\nexport HOOK_LOG={log}\nexport EXOMONAD_ROLE=worker\nexport XDG_CONFIG_HOME={xdg}\nexec opencode mini --standalone --model {model} --agent exomonad-worker --prompt {prompt}\n",
            bin = bin_dir.display(),
            log = hook_log.display(),
            xdg = xdg.display(),
            model = probe_model(),
            prompt = shell_single_quote(prompt),
        ),
    )
    .expect("write mini launcher");

    let output = tmux.run(&[
        "new-session",
        "-d",
        "-s",
        "mini",
        "-x",
        "140",
        "-y",
        "40",
        "-c",
        &root.display().to_string(),
        &format!("bash {}", launcher.display()),
    ]);
    assert!(
        output.status.success(),
        "failed to start the private tmux session: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', r"'\''"))
}

/// Interactive `opencode mini` (the task-less companion form) against the free
/// probe model. Proves the same plugin path `opencode run` uses also holds for
/// the interactive session, and that a worker companion is not closed mid-turn:
/// the terminal execution event is a `stop`, while `worker-exit` is delivered
/// once when the user quits with `/exit`.
#[test]
fn mini_interactive_hooks_when_enabled() {
    if !probe_enabled() {
        eprintln!("skipping interactive OpenCode mini probe: set {PROBE_ENV}=1 to enable");
        return;
    }
    let Some(version) = probe_version_or_skip() else {
        return;
    };
    if !tmux_available() {
        eprintln!("skipping interactive OpenCode mini probe: `tmux` is not available");
        return;
    }
    eprintln!("running interactive OpenCode mini probe against {version}");

    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path().to_path_buf();
    write_generated_plugin(&root);

    std::fs::write(
        root.join("opencode.json"),
        r#"{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["./.exo/opencode-plugin"],
  "permission": "allow",
  "agent": {
    "exomonad-worker": {}
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
    *'"tool_name":"shell"'*EXO_MINI_DENY_ME*)
      printf '%s\n' '{"continue":false,"stopReason":"probe deny: EXO_MINI_DENY_ME blocked in mini"}'
      exit 2
      ;;
  esac
fi
printf '%s\n' '{"continue":true}'
"#,
    );

    let tmux = PrivateTmux::new(&root);
    launch_mini(
        &root,
        &bin_dir,
        &hook_log,
        "Run exactly two bash tool calls, one after another: first `touch mini-allowed.txt`; \
         second `echo EXO_MINI_DENY_ME > mini-denied.txt`. Then reply DONE even if one is blocked.",
        &tmux,
    );

    let turn_finished = wait_until(Duration::from_secs(300), || {
        read_or_empty(&hook_log).contains(&format!("argv: {STOP_ARGV}"))
    });
    let log = read_or_empty(&hook_log);
    let pane = tmux.pane("mini");
    assert!(
        turn_finished,
        "the mini session never finished a prompt turn; hooks:\n{log}\npane:\n{pane}"
    );

    let calls = hook_calls(&log);
    let pre: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == PRE_TOOL_USE_ARGV)
        .collect();
    let post: Vec<&HookCall> = calls
        .iter()
        .filter(|call| call.argv == POST_TOOL_USE_ARGV)
        .collect();

    let allowed = pre
        .iter()
        .find(|call| {
            call.input["args"]["command"]
                .as_str()
                .is_some_and(|command| command.contains("mini-allowed.txt"))
        })
        .unwrap_or_else(|| {
            panic!("mini did not fire pre-tool-use for the allowed shell call; log:\n{log}\npane:\n{pane}")
        });
    assert_eq!(
        allowed.input["tool_name"], "shell",
        "the hook payload must carry the provider tool id for the adapter; log:\n{log}"
    );
    let allowed_id = allowed.input["call_id"].as_str().unwrap_or_default();
    let allowed_post = post
        .iter()
        .find(|call| call.input["call_id"] == allowed_id)
        .unwrap_or_else(|| {
            panic!("mini did not fire post-tool-use for the allowed shell call; log:\n{log}\npane:\n{pane}")
        });
    assert!(
        allowed_post.input.get("output").is_some(),
        "the post-tool-use payload must carry the tool response; log:\n{log}"
    );
    assert!(
        log.contains(&format!("argv: {STOP_ARGV}")),
        "a completed mini turn must report stop, not a process exit; log:\n{log}"
    );
    assert!(
        !log.contains(&format!("argv: {WORKER_EXIT_ARGV}")),
        "worker-exit must not fire while the mini session is still alive; log:\n{log}"
    );

    let denied: Vec<&HookCall> = pre
        .iter()
        .copied()
        .filter(|call| {
            call.input["args"]["command"]
                .as_str()
                .is_some_and(|command| command.contains("EXO_MINI_DENY_ME"))
        })
        .collect();
    assert!(
        !denied.is_empty(),
        "the model never attempted the denied command; log:\n{log}\npane:\n{pane}"
    );
    let denied_ids: Vec<&str> = denied
        .iter()
        .map(|call| call.input["call_id"].as_str().unwrap_or_default())
        .collect();
    assert!(
        !post
            .iter()
            .any(|call| denied_ids.contains(&call.input["call_id"].as_str().unwrap_or_default())),
        "a denied shell call must never reach post-tool-use; log:\n{log}"
    );
    assert!(
        !root.join("mini-denied.txt").exists(),
        "the denied shell command must not have run; pane:\n{pane}"
    );
    assert!(
        root.join("mini-allowed.txt").exists(),
        "the allowed shell command must have run; pane:\n{pane}"
    );

    assert!(
        tmux.session_alive("mini"),
        "mini exited before the user quit"
    );

    let sent = tmux.run(&["send-keys", "-t", "mini", "-l", "/exit"]);
    assert!(sent.status.success(), "failed to type /exit into mini");
    let entered = tmux.run(&["send-keys", "-t", "mini", "Enter"]);
    assert!(entered.status.success(), "failed to submit /exit");

    let exit_delivered = wait_until(Duration::from_secs(60), || {
        read_or_empty(&hook_log).contains(&format!("argv: {WORKER_EXIT_ARGV}"))
    });
    let log = read_or_empty(&hook_log);
    assert!(
        exit_delivered,
        "quitting mini with /exit must deliver worker-exit for a worker companion; hooks:\n{log}\npane:\n{}",
        tmux.pane("mini")
    );
    assert_eq!(
        log.matches(&format!("argv: {WORKER_EXIT_ARGV}")).count(),
        1,
        "worker-exit must be delivered exactly once from the process-end hook; log:\n{log}"
    );
    assert!(
        wait_until(Duration::from_secs(15), || !tmux.session_alive("mini")),
        "the mini session must be gone after /exit"
    );

    for line in log.lines().filter(|line| line.starts_with("argv: ")) {
        eprintln!("observed {line}");
    }
    eprintln!(
        "mini allowed shell ran: {}; denied shell ran: {}",
        root.join("mini-allowed.txt").exists(),
        root.join("mini-denied.txt").exists()
    );
}
