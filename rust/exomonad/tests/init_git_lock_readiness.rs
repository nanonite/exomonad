//! Disposable actual-binary coverage for init's managed orphan-lock recovery.

#[path = "init_git_lock_readiness/mod.rs"]
mod fixture;

use fixture::*;
use std::fs;
use std::path::Path;
use std::process::Output;

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
    preserve_unrelated_edits(&project);

    let index = fs::read(project.join(".git/index")).unwrap();
    let status = git_output(&project, &["status", "--porcelain"]);
    let lock = record_managed_orphan_lock(&project).await;
    assert_eq!(fs::read(&lock).unwrap(), b"");
    assert!(has_lock_owner_proof(&project));

    let tmux_session = create_exited_tl_session(&socket_dir, &session);
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
        drop(tmux_session);
        return;
    }
    assert!(
        !lock.exists(),
        "init did not quarantine the proven orphan lock"
    );
    assert_init_recovered(&first, "init --continue after orphan recovery");
    assert_recovery_preserved_state(&project, &lock, &index, &status);
    assert_restarted_controller(&socket_dir, &session, &old_tl);

    let live_tl = window_id(&socket_dir, &session, "TL");
    let repeat = run_init(&project, &home, &socket_dir, &session);
    assert_init_recovered(&repeat, "repeated init --continue");
    assert_exactly_one_live_role_window(&socket_dir, &session, "TL");
    assert_eq!(window_id(&socket_dir, &session, "TL"), live_tl);
    assert_eq!(quarantine_count(&project), 1);
    drop(tmux_session);
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
        text.contains("Recovered a proven orphan native Git lock"),
        "{action} omitted recovery evidence: {text}"
    );
}
