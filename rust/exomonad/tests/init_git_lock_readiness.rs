//! Disposable actual-binary coverage for init's managed orphan-lock recovery.

#[path = "init_git_lock_readiness/mod.rs"]
mod fixture;

use fixture::*;
use std::fs;
use std::path::Path;
use std::process::{ExitStatus, Output};

#[cfg(target_os = "linux")]
#[tokio::test]
async fn init_continue_recovers_managed_orphan_and_attaches_once() {
    let temp = tempfile::tempdir().expect("temporary fixture root");
    let project = temp.path().join("repo");
    let home = temp.path().join("home");
    let socket_dir = temp.path().join("tmux");
    let session = format!("exo-lock-recovery-{}", std::process::id());
    fs::create_dir_all(&project).unwrap();
    fs::create_dir_all(&home).unwrap();
    fs::create_dir_all(&socket_dir).unwrap();
    init_git_repo(&project);
    install_project_runtime(&project, &home);
    configure_project(&project, &session);
    seed_runnable_checkpoint(&project);
    preserve_unrelated_edits(&project);
    let tmux_session = create_exited_tl_session(&socket_dir, &session, &project).await;

    let index = fs::read(project.join(".git/index")).unwrap();
    let status = git_output(&project, &["status", "--porcelain"]);
    let lock = record_managed_orphan_lock(&project).await;
    assert_eq!(fs::read(&lock).unwrap(), b"");
    assert!(has_lock_owner_proof(&project));

    let old_tl = window_id(&socket_dir, &session, "TL");
    let first = run_init(&project, &home, &socket_dir, &session);
    if process_inspection_was_denied(&first) {
        assert_limited_inspection_refusal(
            &first,
            &project,
            &socket_dir,
            &session,
            &lock,
            &index,
            &status,
            &old_tl,
        );
        eprintln!(
            "actual init safely refused recovery because /proc descriptor visibility is restricted"
        );
        return;
    }
    assert!(
        !lock.exists(),
        "init did not quarantine the proven orphan lock"
    );
    assert_init_recovered(&first, "init --continue after orphan recovery");
    assert_recovery_preserved_state(&project, &lock, &index, &status);
    assert_restarted_controller(&socket_dir, &session, &old_tl);
    let controller_target = assert_exact_controller_ownership(
        &project,
        &socket_dir,
        &session,
        &window_id(&socket_dir, &session, "TL"),
    )
    .await;

    let live_tl = window_id(&socket_dir, &session, "TL");
    let repeat = run_init(&project, &home, &socket_dir, &session);
    assert_init_repeat_is_idempotent(&repeat);
    assert_exactly_one_live_role_window(&socket_dir, &session, "TL");
    assert_eq!(window_id(&socket_dir, &session, "TL"), live_tl);
    assert_eq!(quarantine_count(&project), 1);
    assert_eq!(
        fs::read(project.join(".exo/tl-loop/root/controller-target.json")).unwrap(),
        controller_target,
        "repeat init must preserve exact root pane ownership and epoch"
    );
    drop(tmux_session);
}

async fn assert_exact_controller_ownership(
    project: &Path,
    socket_dir: &Path,
    session: &str,
    expected_window_id: &str,
) -> Vec<u8> {
    let path = project.join(".exo/tl-loop/root/controller-target.json");
    let bytes = fs::read(&path).expect("successful init persists exact controller ownership");
    let owner: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(owner["schema_version"], 1);
    assert_eq!(owner["run_id"], "root");
    assert_eq!(owner["tmux_session"], session);
    assert_eq!(owner["window_id"], expected_window_id);
    assert_eq!(
        owner["controller_epoch"],
        fs::read_to_string(project.join(".exo/tl-loop/root.controller-epoch"))
            .unwrap()
            .trim()
    );

    let panes = tmux(
        socket_dir,
        &[
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}",
        ],
    );
    assert_success(&panes, "inspect exact root controller pane");
    let pane_output = String::from_utf8_lossy(&panes.stdout).into_owned();
    let pane = pane_output
        .lines()
        .filter_map(|line| {
            let fields = line.split('\t').collect::<Vec<_>>();
            (fields.len() == 5 && fields[0] == session && fields[1] == expected_window_id)
                .then_some(fields)
        })
        .next()
        .expect("the recorded root window has an exact pane");
    assert_eq!(owner["pane_id"], pane[2]);
    assert_eq!(owner["pane_pid"].as_u64(), pane[3].parse::<u64>().ok());
    assert_eq!(pane[4], "0", "the persisted controller pane must be live");
    let pane_pid = pane[3].parse::<u32>().unwrap();
    assert_eq!(
        owner["pane_start_ticks"].as_u64(),
        Some(
            exomonad_core::services::tmux_ipc::process_start_ticks(pane_pid)
                .await
                .unwrap()
        )
    );

    let server = tmux(socket_dir, &["display-message", "-p", "#{pid}"]);
    assert_success(&server, "inspect exact tmux server process");
    let server_pid = String::from_utf8_lossy(&server.stdout)
        .trim()
        .parse::<u32>()
        .unwrap();
    assert_eq!(owner["tmux_server_pid"].as_u64(), Some(server_pid.into()));
    assert_eq!(
        owner["tmux_server_start_ticks"].as_u64(),
        Some(
            exomonad_core::services::tmux_ipc::process_start_ticks(server_pid)
                .await
                .unwrap()
        )
    );
    let run: serde_json::Value =
        serde_json::from_slice(&fs::read(project.join(".exo/tl-loop/root/run.json")).unwrap())
            .unwrap();
    assert_eq!(owner["run_id"], run["run_id"]);
    bytes
}

#[cfg(target_os = "linux")]
#[tokio::test]
#[ignore = "invoked only by the disposable init-recovery harness"]
async fn create_managed_orphan_lock_fixture_for_e2e() {
    let project = std::env::var_os("EXOMONAD_INIT_LOCK_FIXTURE_PROJECT")
        .map(std::path::PathBuf::from)
        .expect("EXOMONAD_INIT_LOCK_FIXTURE_PROJECT must name the disposable repository");
    let lock = record_managed_orphan_lock(&project).await;
    assert_eq!(fs::read(&lock).unwrap(), b"");
    assert!(has_lock_owner_proof(&project));
}

#[cfg(target_os = "linux")]
#[tokio::test]
#[ignore = "invoked only by the disposable init-recovery harness"]
async fn assert_managed_lock_quarantined_once_for_e2e() {
    let project = std::env::var_os("EXOMONAD_INIT_LOCK_FIXTURE_PROJECT")
        .map(std::path::PathBuf::from)
        .expect("EXOMONAD_INIT_LOCK_FIXTURE_PROJECT must name the disposable repository");
    assert!(!project.join(".git/index.lock").exists());
    assert_eq!(quarantine_count(&project), 1);
    assert_eq!(fs::read(quarantined_lock(&project)).unwrap(), b"");
}

fn process_inspection_was_denied(output: &Output) -> bool {
    let diagnostic = combined_output(output);
    diagnostic.contains("Git mutation readiness is uncertain")
        && diagnostic.contains("permission denied")
}

fn assert_limited_inspection_refusal(
    output: &Output,
    project: &Path,
    socket_dir: &Path,
    session: &str,
    lock: &Path,
    index: &[u8],
    status: &str,
    old_tl: &str,
) {
    assert!(!output.status.success());
    let diagnostic = combined_output(output);
    assert!(diagnostic.contains(&lock.display().to_string()));
    assert!(diagnostic.contains("exomonad init --continue"));
    assert!(lock.exists());
    assert!(!has_quarantine_receipt(project));
    assert_eq!(fs::read(project.join(".git/index")).unwrap(), index);
    assert_eq!(git_output(project, &["status", "--porcelain"]), status);
    assert_eq!(window_id(socket_dir, session, "TL"), old_tl);
    assert_eq!(window_count(socket_dir, session, "Server"), 0);
    assert_eq!(window_count(socket_dir, session, "Watcher"), 0);
}

fn assert_recovery_preserved_state(project: &Path, lock: &Path, index: &[u8], status: &str) {
    assert!(!lock.exists());
    assert_eq!(fs::read(quarantined_lock(project)).unwrap(), b"");
    assert_eq!(fs::read(project.join(".git/index")).unwrap(), index);
    assert_eq!(git_output(project, &["status", "--porcelain"]), status);
}

fn assert_restarted_controller(socket_dir: &Path, session: &str, old_tl: &str) {
    for role in ["Server", "Watcher", "TL"] {
        assert_exactly_one_live_role_window(socket_dir, session, role);
    }
    assert_ne!(window_id(socket_dir, session, "TL"), old_tl);
}

fn assert_init_recovered(output: &Output, action: &str) {
    let text = combined_output(output);
    let detached_attach = text.contains("open terminal failed: not a terminal");
    assert!(
        output.status.success() || detached_attach,
        "{action}: {text}"
    );
    assert!(
        text.matches("Recovered a proven orphan native Git lock")
            .count()
            == 1,
        "{action} should report the one completed lock recovery: {text}"
    );
}

fn assert_init_repeat_is_idempotent(output: &Output) {
    let text = combined_output(output);
    let detached_attach = text.contains("open terminal failed: not a terminal");
    assert!(
        output.status.success() || detached_attach,
        "repeated init --continue: {text}"
    );
}

#[cfg(unix)]
#[test]
fn repeated_init_accepts_no_second_lock_recovery_message() {
    use std::os::unix::process::ExitStatusExt;

    let output = Output {
        status: ExitStatus::from_raw(0),
        stdout: b"Git mutation readiness is clear; reusing the active controller.\n".to_vec(),
        stderr: Vec::new(),
    };
    assert_init_repeat_is_idempotent(&output);
}
