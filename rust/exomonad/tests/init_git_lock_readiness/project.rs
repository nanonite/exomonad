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
    fs::write(project.join("tracked.txt"), "base\n").unwrap();
    git(project, &["add", "tracked.txt"]);
    git(project, &["commit", "-q", "-m", "fixture"]);
}

pub fn install_project_runtime(project: &Path, home: &Path) {
    let repository = repository_root();
    install_devswarm_wasm(project, &repository);
    build_controller_archive(home, &repository);
    verify_controller_archive(home, &repository);
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
    Command::new(env!("CARGO_BIN_EXE_exomonad"))
        .args(["init", "--continue", "--session", session])
        .current_dir(project)
        .env("HOME", home)
        .env("TMUX_TMPDIR", socket_dir)
        .env("CODEX_HOME", home.join(".codex"))
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
