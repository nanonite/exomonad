use assert_cmd::Command;
use std::fs;
use std::path::Path;
use tempfile::TempDir;

fn git(path: &Path, args: &[&str]) {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(path)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("launch Git fixture command");
    assert!(output.status.success(), "git {args:?} failed");
}

fn git_output(path: &Path, args: &[&str]) -> Vec<u8> {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(path)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .expect("launch Git fixture command");
    assert!(output.status.success(), "git {args:?} failed");
    output.stdout
}

fn fixture() -> TempDir {
    let temp = tempfile::tempdir().expect("temporary directory");
    git(temp.path(), &["init", "-q", "-b", "main"]);
    git(temp.path(), &["config", "user.name", "legacy CLI test"]);
    git(
        temp.path(),
        &["config", "user.email", "legacy-cli@example.invalid"],
    );
    fs::write(temp.path().join("tracked.txt"), "base\n").unwrap();
    fs::write(temp.path().join(".gitignore"), ".exo/\n").unwrap();
    git(temp.path(), &["add", "tracked.txt", ".gitignore"]);
    git(temp.path(), &["commit", "-q", "-m", "fixture"]);
    temp
}

#[test]
fn compiled_local_cli_inspects_and_applies_fingerprint_without_a_server() {
    let temp = fixture();
    let repo = temp.path();
    fs::write(repo.join("tracked.txt"), "staged\n").unwrap();
    git(repo, &["add", "tracked.txt"]);
    fs::write(repo.join("tracked.txt"), "unstaged\n").unwrap();
    fs::write(repo.join("untracked.txt"), "untracked\n").unwrap();
    let index = fs::read(repo.join(".git/index")).unwrap();
    let status = git_output(repo, &["status", "--porcelain"]);
    let lock = repo.join(".git/index.lock");
    fs::write(&lock, b"legacy lock content retained").unwrap();

    let inspect = Command::cargo_bin("exomonad")
        .unwrap()
        .args(["git-lock", "inspect", "--project-root"])
        .arg(repo)
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();
    let inspect = String::from_utf8(inspect).unwrap();
    assert!(inspect.contains("index_lock_file"));
    assert!(inspect.contains("writers-stopped"));
    let fingerprint = inspect
        .lines()
        .find_map(|line| line.trim().strip_prefix("\"recovery_fingerprint\": \""))
        .and_then(|line| line.strip_suffix("\","))
        .expect("inspect prints a recovery fingerprint")
        .to_string();
    assert_eq!(fingerprint.len(), 64);
    assert_eq!(fs::read(&lock).unwrap(), b"legacy lock content retained");
    assert_eq!(fs::read(repo.join(".git/index")).unwrap(), index);
    assert_eq!(git_output(repo, &["status", "--porcelain"]), status);
    assert!(
        !repo.join(".exo/ledger/segments").exists(),
        "inspect is read-only"
    );

    Command::cargo_bin("exomonad")
        .unwrap()
        .args(["git-lock", "apply", "--project-root"])
        .arg(repo)
        .args(["--expected-fingerprint", &fingerprint, "--writers-stopped"])
        .assert()
        .success()
        .stdout(predicates::str::contains("Legacy Git lock recovered"));
    assert!(!lock.exists());
    assert_eq!(fs::read(repo.join(".git/index")).unwrap(), index);
    assert_eq!(git_output(repo, &["status", "--porcelain"]), status);
    let quarantine = fs::read_dir(repo.join(".git/exomonad-mutation/quarantine"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("legacy-")
        })
        .expect("legacy lock retained in quarantine");
    assert_eq!(
        fs::read(quarantine).unwrap(),
        b"legacy lock content retained"
    );

    fs::write(&lock, b"replacement").unwrap();
    Command::cargo_bin("exomonad")
        .unwrap()
        .args(["git-lock", "apply", "--project-root"])
        .arg(repo)
        .args(["--expected-fingerprint", &fingerprint, "--writers-stopped"])
        .assert()
        .success()
        .stdout(predicates::str::contains("Already applied: true"));
    assert_eq!(fs::read(&lock).unwrap(), b"replacement");
}
