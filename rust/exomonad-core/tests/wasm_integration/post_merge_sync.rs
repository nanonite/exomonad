use super::{wasm_binary_bytes, MockLogHandler};
use exomonad_core::handlers::ProcessHandler;
use exomonad_core::{AgentName, BirthBranch, EffectContext, EffectRegistry, PluginManager};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tempfile::TempDir;

fn git(directory: &Path, arguments: &[&str]) {
    let output = std::process::Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .output()
        .expect("execute fixture Git command");
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        arguments,
        String::from_utf8_lossy(&output.stderr)
    );
}

fn git_output(directory: &Path, arguments: &[&str]) -> String {
    let output = std::process::Command::new("git")
        .args(arguments)
        .current_dir(directory)
        .output()
        .expect("execute fixture Git query");
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        arguments,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

struct SyncFixture {
    _root: TempDir,
    repository: PathBuf,
    base_sha: String,
    merged_sha: String,
    dirty_files: Vec<(PathBuf, &'static str)>,
}

fn sync_fixture(conflicting_edit: bool) -> SyncFixture {
    let root = tempfile::tempdir().expect("temporary Git fixture");
    let repository = root.path().join("parent");
    let seed = root.path().join("seed");
    let forgejo = root.path().join("forgejo.git");
    let origin = root.path().join("origin.git");
    let worker = root.path().join("worker");
    std::fs::create_dir_all(&seed).expect("create seed repository");

    git(
        root.path(),
        &["init", "--bare", "--initial-branch=main", "forgejo.git"],
    );
    git(
        root.path(),
        &["init", "--bare", "--initial-branch=main", "origin.git"],
    );
    git(&seed, &["init", "--initial-branch=setup"]);
    git(&seed, &["config", "user.name", "Integration Test"]);
    git(
        &seed,
        &["config", "user.email", "integration@example.invalid"],
    );
    std::fs::write(seed.join("tracked.txt"), "base\n").expect("write base file");
    git(&seed, &["add", "--", "tracked.txt"]);
    git(&seed, &["commit", "-m", "base"]);
    let base_sha = git_output(&seed, &["rev-parse", "HEAD"]);
    git(&seed, &["branch", "main", &base_sha]);

    let forgejo_url = forgejo.to_string_lossy().into_owned();
    let origin_url = origin.to_string_lossy().into_owned();
    git(&seed, &["remote", "add", "forgejo", &forgejo_url]);
    git(&seed, &["remote", "add", "origin", &origin_url]);
    git(&seed, &["config", "exomonad.remote", "forgejo"]);
    git(&seed, &["push", "forgejo", "main"]);
    git(&seed, &["push", "origin", "main"]);
    git(
        &seed,
        &["update-ref", "refs/remotes/forgejo/main", &base_sha],
    );
    let repository_arg = repository
        .to_str()
        .expect("temporary repository path is UTF-8");
    git(&seed, &["worktree", "add", repository_arg, "main"]);

    git(root.path(), &["clone", &forgejo_url, "worker"]);
    git(&worker, &["config", "user.name", "Integration Test"]);
    git(
        &worker,
        &["config", "user.email", "integration@example.invalid"],
    );
    if conflicting_edit {
        std::fs::write(worker.join("tracked.txt"), "remote change\n")
            .expect("write remote conflict");
        git(&worker, &["add", "--", "tracked.txt"]);
    } else {
        std::fs::write(worker.join("child.txt"), "merged child\n").expect("write child change");
        git(&worker, &["add", "--", "child.txt"]);
    }
    git(&worker, &["commit", "-m", "merged child"]);
    git(&worker, &["push", "origin", "main"]);
    let merged_sha = git_output(&worker, &["rev-parse", "HEAD"]);

    let dirty_files = if conflicting_edit {
        let path = repository.join("tracked.txt");
        let contents = "operator edit\n";
        std::fs::write(&path, contents).expect("write conflicting local edit");
        vec![(path, contents)]
    } else {
        let unstaged = repository.join("tracked.txt");
        let staged = repository.join("operator-staged.txt");
        let untracked = repository.join("operator-notes.txt");
        let unstaged_contents = "unstaged operator edit\n";
        let staged_contents = "staged operator edit\n";
        let untracked_contents = "untracked operator note\n";
        std::fs::write(&unstaged, unstaged_contents).expect("write unrelated unstaged edit");
        std::fs::write(&staged, staged_contents).expect("write unrelated staged edit");
        git(&repository, &["add", "--", "operator-staged.txt"]);
        std::fs::write(&untracked, untracked_contents).expect("write unrelated untracked edit");
        vec![
            (unstaged, unstaged_contents),
            (staged, staged_contents),
            (untracked, untracked_contents),
        ]
    };

    SyncFixture {
        _root: root,
        repository,
        base_sha,
        merged_sha,
        dirty_files,
    }
}

async fn sync_plugin(repository: &Path) -> PluginManager {
    let mut registry = EffectRegistry::new();
    registry.register_owned(ProcessHandler::with_project_dir(repository.to_path_buf()));
    registry.register_owned(MockLogHandler);
    let context = EffectContext {
        agent_name: AgentName::try_from_str("root").expect("root agent name"),
        birth_branch: BirthBranch::root().expect("root branch"),
        working_dir: repository.to_path_buf(),
    };
    PluginManager::new(&wasm_binary_bytes(), Arc::new(registry), context)
        .await
        .expect("load compiled production WASM with the managed Git process handler")
}

async fn call_parent_sync(plugin: &PluginManager, fixture: &SyncFixture) -> Value {
    plugin
        .call(
            "handle_mcp_call",
            &json!({
                "role": "tl",
                "toolName": "post_merge_parent_sync",
                "toolArgs": {
                    "child_id": "architecture-stage",
                    "pr_number": 46,
                    "repository": "org/repo",
                    "parent_branch": "main",
                    "merged_head_sha": fixture.merged_sha,
                    "expected_base_sha": fixture.base_sha,
                    "lane_epoch": 1
                }
            }),
        )
        .await
        .expect("execute compiled parent synchronization tool")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_uses_selected_remote_preserves_unrelated_edits_and_adopts_replay() {
    let fixture = sync_fixture(false);
    let plugin = sync_plugin(&fixture.repository).await;

    let first = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(first["success"], true, "first sync must succeed: {first:#}");
    assert_eq!(first["result"]["remote_name"], "forgejo");
    assert_eq!(first["result"]["remote_head_sha"], fixture.merged_sha);
    assert_eq!(first["result"]["parent_commit_sha"], fixture.merged_sha);
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        fixture.merged_sha
    );
    for (path, expected) in &fixture.dirty_files {
        assert_eq!(
            std::fs::read_to_string(path).expect("read preserved operator file"),
            *expected
        );
    }
    let status = git_output(&fixture.repository, &["status", "--porcelain"]);
    assert!(
        status.contains(" M tracked.txt"),
        "unstaged edit remains: {status}"
    );
    assert!(
        status.contains("A  operator-staged.txt"),
        "staged edit remains staged: {status}"
    );
    assert!(
        status.contains("?? operator-notes.txt"),
        "untracked file remains: {status}"
    );

    let replay = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        replay["success"], true,
        "replay must adopt the synced head: {replay:#}"
    );
    assert_eq!(replay["result"]["remote_name"], "forgejo");
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        fixture.merged_sha,
        "replay must not create another merge commit"
    );
    for (path, expected) in &fixture.dirty_files {
        assert_eq!(
            std::fs::read_to_string(path).expect("read preserved operator file"),
            *expected
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_holds_a_conflicting_dirty_edit_without_overwriting_it() {
    let fixture = sync_fixture(true);
    let plugin = sync_plugin(&fixture.repository).await;

    let output = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        output["success"], false,
        "conflicting edit must block sync: {output:#}"
    );
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        fixture.base_sha,
        "a refused fast-forward must not advance local HEAD"
    );
    assert_eq!(
        std::fs::read_to_string(&fixture.dirty_files[0].0).expect("read conflicting operator edit"),
        fixture.dirty_files[0].1,
        "a refused fast-forward must preserve the user's edit"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_rejects_the_wrong_checked_out_branch_before_fetching() {
    let fixture = sync_fixture(false);
    git(&fixture.repository, &["switch", "-c", "wrong-parent"]);
    let local_head = git_output(&fixture.repository, &["rev-parse", "HEAD"]);
    let plugin = sync_plugin(&fixture.repository).await;

    let output = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        output["success"], false,
        "wrong branch must block sync: {output:#}"
    );
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        local_head
    );
    assert_eq!(
        git_output(
            &fixture.repository,
            &["rev-parse", "refs/remotes/forgejo/main"]
        ),
        fixture.base_sha,
        "wrong-branch rejection must happen before fetch"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_holds_divergent_local_and_remote_histories() {
    let fixture = sync_fixture(false);
    git(
        &fixture.repository,
        &["config", "user.name", "Integration Test"],
    );
    git(
        &fixture.repository,
        &["config", "user.email", "integration@example.invalid"],
    );
    std::fs::write(fixture.repository.join("local-only.txt"), "local history\n")
        .expect("write local-only commit");
    git(&fixture.repository, &["add", "--", "local-only.txt"]);
    git(&fixture.repository, &["commit", "-m", "local divergence"]);
    let local_head = git_output(&fixture.repository, &["rev-parse", "HEAD"]);
    let plugin = sync_plugin(&fixture.repository).await;

    let output = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        output["success"], false,
        "divergence must block sync: {output:#}"
    );
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        local_head,
        "failed ff-only integration must retain the local commit"
    );
    assert_eq!(
        git_output(
            &fixture.repository,
            &["rev-parse", "refs/remotes/forgejo/main"]
        ),
        fixture.merged_sha,
        "the selected remote observation remains recorded locally"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_reports_fetch_failure_without_changing_local_head() {
    let fixture = sync_fixture(false);
    let unavailable_remote = fixture._root.path().join("unavailable.git");
    let unavailable_url = unavailable_remote.to_string_lossy().into_owned();
    git(
        &fixture.repository,
        &["remote", "set-url", "forgejo", &unavailable_url],
    );
    let plugin = sync_plugin(&fixture.repository).await;

    let output = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        output["success"], false,
        "unavailable remote must block sync: {output:#}"
    );
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        fixture.base_sha,
        "fetch failure must leave the local branch unchanged"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn compiled_parent_sync_preserves_unowned_native_index_lock_evidence() {
    let fixture = sync_fixture(false);
    let lock_output = git_output(
        &fixture.repository,
        &["rev-parse", "--git-path", "index.lock"],
    );
    let index_lock = if Path::new(&lock_output).is_absolute() {
        PathBuf::from(lock_output)
    } else {
        fixture.repository.join(lock_output)
    };
    std::fs::write(&index_lock, b"").expect("write unowned empty index lock fixture");
    let plugin = sync_plugin(&fixture.repository).await;

    let output = call_parent_sync(&plugin, &fixture).await;
    assert_eq!(
        output["success"], false,
        "unowned lock must block sync: {output:#}"
    );
    assert!(
        index_lock.exists(),
        "unproven lock evidence must not be deleted"
    );
    assert_eq!(
        git_output(&fixture.repository, &["rev-parse", "HEAD"]),
        fixture.base_sha
    );
}
