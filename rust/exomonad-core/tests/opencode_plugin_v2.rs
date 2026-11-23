//! Integration checks for the generated OpenCode plugin package.
//!
//! The shape assertions are unit-level and always run. The end-to-end probe is
//! opt-in because it launches the real `opencode` CLI and a model; it skips
//! cleanly when either is unavailable.

use std::path::Path;
use std::process::Command;

const PROBE_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE";
const PROBE_MODEL_ENV: &str = "EXOMONAD_OPENCODE_V2_PROBE_MODEL";
const DEFAULT_PROBE_MODEL: &str = "opencode-go/beam-free";

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

#[test]
fn generated_plugin_loads_and_invokes_hooks_under_real_cli_when_enabled() {
    if std::env::var(PROBE_ENV).as_deref() != Ok("1") {
        eprintln!("skipping real OpenCode probe: set {PROBE_ENV}=1 to enable");
        return;
    }

    let version = match Command::new("opencode").arg("--version").output() {
        Ok(output) if output.status.success() => {
            String::from_utf8_lossy(&output.stdout).trim().to_string()
        }
        _ => {
            eprintln!("skipping real OpenCode probe: `opencode` is not available");
            return;
        }
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

    let bin_dir = root.join("bin");
    std::fs::create_dir_all(&bin_dir).expect("create shim dir");
    let hook_log = root.join("hooks.log");
    let shim = bin_dir.join("exomonad");
    std::fs::write(
        &shim,
        r#"#!/usr/bin/env bash
input=$(cat)
{
  printf 'argv: %s\n' "$*"
  printf 'stdin: %s\n' "$input"
} >> "${HOOK_LOG:?HOOK_LOG must be set}"
printf '%s\n' '{"continue":true}'
"#,
    )
    .expect("write shim");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755))
            .expect("chmod shim");
    }

    let config_home = root.join("xdg");
    std::fs::create_dir_all(&config_home).expect("create config home");
    let path = format!(
        "{}:{}",
        bin_dir.display(),
        std::env::var("PATH").unwrap_or_default()
    );
    let model = std::env::var(PROBE_MODEL_ENV).unwrap_or_else(|_| DEFAULT_PROBE_MODEL.to_string());

    let output = Command::new("timeout")
        .arg("240")
        .args([
            "opencode",
            "run",
            "--standalone",
            "--auto",
            "--print-logs",
            "--log-level",
            "warn",
            "--model",
            &model,
            "Run exactly one bash tool call that prints EXO_PLUGIN_PROBE, then reply DONE.",
        ])
        .current_dir(&root)
        .env("PWD", &root)
        .env("PATH", path)
        .env("XDG_CONFIG_HOME", &config_home)
        .env("HOOK_LOG", &hook_log)
        .output()
        .expect("launch opencode");

    assert!(
        output.status.success(),
        "opencode probe failed: status={:?}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );

    let log = std::fs::read_to_string(&hook_log)
        .unwrap_or_else(|_| String::from("plugin did not invoke the exomonad hook"));
    assert!(
        log.contains("argv: hook pre-tool-use --runtime opencode"),
        "missing pre-tool-use hook; log:\n{log}"
    );
    assert!(
        log.contains("argv: hook post-tool-use --runtime opencode"),
        "missing post-tool-use hook; log:\n{log}"
    );
}
