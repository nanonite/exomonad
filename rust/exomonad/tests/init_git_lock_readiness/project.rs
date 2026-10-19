use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

pub fn init_git_repo(project: &Path) {
    git(project, &["init", "-q", "-b", "main"]);
    git(
        project,
        &["config", "user.name", "init lock readiness test"],
    );
    git(
        project,
        &["config", "user.email", "init-lock@example.invalid"],
    );
    git(
        project,
        &[
            "remote",
            "add",
            "origin",
            "https://forge.example.test/test/repository.git",
        ],
    );
    fs::write(project.join("tracked.txt"), "base\n").unwrap();
    git(project, &["add", "tracked.txt"]);
    git(project, &["commit", "-q", "-m", "fixture"]);
}

pub fn install_project_runtime(project: &Path, home: &Path) {
    let repository = repository_root();
    install_devswarm_wasm(project, &repository);
    install_controller_inputs(project, &repository);
    build_controller_archive(home, &repository);
    verify_controller_archive(home, &repository);
}

pub fn seed_runnable_checkpoint(project: &Path) {
    let repository = repository_root();
    let script = r#"
import sys
from pathlib import Path
import shutil
from tl_loop.tests.test_plan_reconciliation import _project

project = Path(sys.argv[1])
_project(
    project,
    candidate_issue_id=None,
    include_completed_spawn_journal=True,
    legacy_completed_dispatch=True,
    legacy_integration_coordinator_binding=True,
    terminal_aggregate_handoff=True,
)
shutil.rmtree(project / ".exo" / "ledger", ignore_errors=True)
"#;
    let status = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(project)
        .env("PYTHONPATH", repository)
        .status()
        .expect("seed runnable checkpoint");
    assert!(status.success(), "runnable checkpoint creation failed");
}

fn install_controller_inputs(project: &Path, repository: &Path) {
    let exo = project.join(".exo");
    fs::create_dir_all(exo.join("tl-loop")).unwrap();
    for name in [
        "harness_policy.toml",
        "review-policy.toml",
        "harness_capability.toml",
    ] {
        fs::copy(repository.join(".exo").join(name), exo.join(name)).unwrap();
    }
    let plan = br#"{
  "run_id": "root",
  "budgets": {"tokens": 120000, "wall_seconds": 1800},
  "plan": {
    "workers": [],
    "leaves": [{"name": "hold", "task": "hold for init readiness", "chainlink_issue_id": null}],
    "sub_tls": []
  }
}
"#;
    let plan_path = exo.join("tl-loop/plan.json");
    let snapshot_path = exo.join("tl-loop/plan.snapshot");
    fs::write(&plan_path, plan).unwrap();
    fs::write(&snapshot_path, plan).unwrap();
    let digest = format!("{:x}\n", Sha256::digest(plan));
    fs::write(exo.join("tl-loop/plan.snapshot.sha256"), digest).unwrap();
}

fn install_devswarm_wasm(project: &Path, repository: &Path) {
    let source = repository.join(".exo/wasm/wasm-guest-devswarm.wasm");
    assert!(source.is_file(), "build devswarm WASM before this test");
    let target = project.join(".exo/wasm/wasm-guest-devswarm.wasm");
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::copy(source, target).unwrap();
}

fn build_controller_archive(home: &Path, repository: &Path) {
    fs::create_dir_all(home.join(".exo")).unwrap();
    let status = Command::new("python3")
        .arg(repository.join("scripts/build_tl_loop_archive.py"))
        .args(["--source"])
        .arg(repository.join("tl_loop"))
        .args(["--output"])
        .arg(home.join(".exo/tl_loop.pyz"))
        .status()
        .expect("build isolated controller archive");
    assert!(status.success(), "isolated controller archive build failed");
}

fn verify_controller_archive(home: &Path, repository: &Path) {
    let status = Command::new("python3")
        .arg(repository.join("scripts/check_tl_loop_archive.py"))
        .arg(home.join(".exo/tl_loop.pyz"))
        .args(["--source"])
        .arg(repository.join("tl_loop"))
        .status()
        .expect("validate isolated controller archive");
    assert!(
        status.success(),
        "isolated controller archive validation failed"
    );
}

pub fn configure_project(project: &Path, session: &str) {
    let exo = project.join(".exo");
    fs::create_dir_all(exo.join("tl-loop")).unwrap();
    fs::write(
        exo.join("config.toml"),
        format!(
            "default_role = \"devswarm\"\nwasm_name = \"devswarm\"\nwasm_dir = \".exo/wasm\"\nshell_command = \"bash\"\ntmux_session = \"{session}\"\nyolo = true\nport = 0\n"
        ),
    )
    .unwrap();
}

pub fn preserve_unrelated_edits(project: &Path) {
    fs::write(project.join("tracked.txt"), "staged edit\n").unwrap();
    git(project, &["add", "tracked.txt"]);
    fs::write(project.join("tracked.txt"), "unstaged edit\n").unwrap();
    fs::write(project.join("untracked.txt"), "keep this\n").unwrap();
}

pub fn run_init(project: &Path, home: &Path, socket_dir: &Path, session: &str) -> Output {
    run_init_mode(project, home, socket_dir, session, "--continue")
}

pub fn run_init_mode(
    project: &Path,
    home: &Path,
    socket_dir: &Path,
    session: &str,
    mode: &str,
) -> Output {
    Command::new(env!("CARGO_BIN_EXE_exomonad"))
        .args(["init", mode, "--session", session])
        .current_dir(project)
        .env("HOME", home)
        .env("TMUX_TMPDIR", socket_dir)
        .env("CODEX_HOME", home.join(".codex"))
        .env("EXOMONAD_TL_PREFLIGHT_ACK", "1")
        .output()
        .expect("run the built exomonad binary")
}

pub fn git(project: &Path, args: &[&str]) {
    let output = Command::new("git")
        .args(args)
        .current_dir(project)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("run Git fixture command");
    assert_success(&output, "Git fixture command");
}

pub fn git_output(project: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .args(args)
        .current_dir(project)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("read Git fixture state");
    assert_success(&output, "read Git fixture state");
    String::from_utf8(output.stdout).unwrap()
}

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

pub fn combined_output(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

pub fn assert_success(output: &Output, action: &str) {
    assert!(
        output.status.success(),
        "{action} failed: {}",
        combined_output(output)
    );
}
