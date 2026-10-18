use exomonad_core::domain::{AgentName, BirthBranch};
use exomonad_core::effects::{EffectContext, ProcessEffects};
use exomonad_core::handlers::ProcessHandler;
use exomonad_core::services::EventLog;
use exomonad_proto::effects::process::RunRequest;
use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

pub async fn record_managed_orphan_lock(project: &Path) -> PathBuf {
    let script = project.join(".exo/fixture-bin/git");
    fs::create_dir_all(script.parent().unwrap()).unwrap();
    fs::write(
        &script,
        "#!/bin/sh\nexec 9>\"$PWD/.git/index.lock\"\nexec /bin/sleep 30\n",
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
    }
    run_timed_managed_git_process(project, script).await;
    project.join(".git/index.lock")
}

async fn run_timed_managed_git_process(project: &Path, script: PathBuf) {
    let handler = ProcessHandler::with_project_dir(project.to_path_buf());
    let context = EffectContext {
        agent_name: AgentName::try_from_str("root").unwrap(),
        birth_branch: BirthBranch::try_from_str("main").unwrap(),
        working_dir: project.to_path_buf(),
    };
    let request = RunRequest {
        command: script.to_string_lossy().into_owned(),
        args: vec!["add".into(), "tracked.txt".into()],
        working_dir: ".".into(),
        env: HashMap::new(),
        timeout_ms: 700,
    };
    let error = handler
        .run(request, &context)
        .await
        .unwrap_err()
        .to_string();
    assert!(
        error.contains("timed out"),
        "managed owner must be stopped by the timeout supervisor: {error}"
    );
}

pub fn has_lock_owner_proof(project: &Path) -> bool {
    EventLog::open(project.join(".exo/logs"))
        .unwrap()
        .ledger()
        .read_events()
        .unwrap()
        .iter()
        .any(|record| record.event.event_type == "git.native_lock.held")
}

pub fn quarantined_lock(project: &Path) -> PathBuf {
    let events = EventLog::open(project.join(".exo/logs"))
        .unwrap()
        .ledger()
        .read_events()
        .unwrap();
    let matches = events
        .iter()
        .filter(|record| record.event.event_type == "git.native_lock.quarantined")
        .collect::<Vec<_>>();
    assert_eq!(matches.len(), 1, "expected one durable quarantine receipt");
    PathBuf::from(matches[0].event.data["quarantine_file"].as_str().unwrap())
}

pub fn quarantine_count(project: &Path) -> usize {
    EventLog::open(project.join(".exo/logs"))
        .unwrap()
        .ledger()
        .read_events()
        .unwrap()
        .iter()
        .filter(|record| record.event.event_type == "git.native_lock.quarantined")
        .count()
}

pub fn has_quarantine_receipt(project: &Path) -> bool {
    quarantine_count(project) > 0
}
