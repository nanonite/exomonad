//! Shared test scaffolding for the ExoMonad workspace.
//!
//! This unpublished crate is consumed only as a dev-dependency.

use anyhow::{bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Output;

/// Git environment variables that can redirect repository discovery or object
/// and index writes away from a command's working directory.
pub const GIT_REPOSITORY_ENV_VARS: &[&str] = &[
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
];

/// Add the ExoMonad fixture Git environment policy to a command.
pub trait ScrubGitRepositoryEnv {
    /// Remove inherited Git variables that can redirect fixture operations.
    fn scrub_git_repository_env(&mut self) -> &mut Self;
}

impl ScrubGitRepositoryEnv for std::process::Command {
    fn scrub_git_repository_env(&mut self) -> &mut Self {
        for variable in GIT_REPOSITORY_ENV_VARS {
            self.env_remove(variable);
        }
        self
    }
}

impl ScrubGitRepositoryEnv for tokio::process::Command {
    fn scrub_git_repository_env(&mut self) -> &mut Self {
        for variable in GIT_REPOSITORY_ENV_VARS {
            self.env_remove(variable);
        }
        self
    }
}

/// Prove that Git resolves `fixture_root` to a repository inside that root.
///
/// This must be called after `git init` and immediately before any fixture
/// mutation. Both paths are canonicalized so symlink and prefix tricks cannot
/// bypass the component-aware containment check.
pub fn assert_fixture_git_root(fixture_root: &Path) -> Result<()> {
    let expected_root = fs::canonicalize(fixture_root).with_context(|| {
        format!(
            "failed to canonicalize fixture root {}",
            fixture_root.display()
        )
    })?;
    let output = std::process::Command::new("git")
        .current_dir(&expected_root)
        .args(["rev-parse", "--show-toplevel"])
        .scrub_git_repository_env()
        .output()
        .context("failed to run scrubbed fixture git root check")?;

    if !output.status.success() {
        bail!(
            "fixture git root check failed for {}: {}",
            expected_root.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let resolved_root = String::from_utf8_lossy(&output.stdout);
    let resolved_root = Path::new(resolved_root.trim());
    let resolved_root = fs::canonicalize(resolved_root).with_context(|| {
        format!(
            "failed to canonicalize git-resolved fixture root {}",
            resolved_root.display()
        )
    })?;

    if !resolved_root.starts_with(&expected_root) {
        bail!(
            "fixture git root escaped its fixture directory: expected {} or a child, resolved {}",
            expected_root.display(),
            resolved_root.display()
        );
    }

    Ok(())
}

/// Initialize a standalone test repository with the scrubbed Git environment.
pub fn init_fixture_git_repository(fixture_root: &Path) -> Result<()> {
    fs::create_dir_all(fixture_root).with_context(|| {
        format!(
            "failed to create fixture repository directory {}",
            fixture_root.display()
        )
    })?;
    let output = std::process::Command::new("git")
        .args(["init", "--quiet"])
        .current_dir(fixture_root)
        .scrub_git_repository_env()
        .output()
        .context("failed to initialize fixture git repository")?;
    if !output.status.success() {
        bail!(
            "fixture git init failed at {}: {}",
            fixture_root.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    assert_fixture_git_root(fixture_root)
}

/// Run a mutating or inspecting Git command against a fixture repository.
///
/// The root guard runs before every command, and the command receives the same
/// scrubbed environment as the guard. Fixture identity and remote configuration
/// supplied in `args` are intentionally preserved.
pub fn run_fixture_git_command(fixture_root: &Path, args: &[&str]) -> Result<Output> {
    assert_fixture_git_root(fixture_root)?;
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(fixture_root)
        .scrub_git_repository_env()
        .output()
        .with_context(|| format!("failed to run fixture git command: git {args:?}"))?;
    if !output.status.success() {
        bail!(
            "fixture git command failed at {}: git {:?}: {}",
            fixture_root.display(),
            args,
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(output)
}

/// Return the git repository root containing `path`, if any.
pub fn git_repository_root(path: &Path) -> Option<PathBuf> {
    let output = std::process::Command::new("git")
        .args(["-C"])
        .arg(path)
        .args(["rev-parse", "--show-toplevel"])
        .scrub_git_repository_env()
        .output()
        .expect("failed to run git repository detection");
    if output.status.success() {
        return Some(PathBuf::from(
            String::from_utf8_lossy(&output.stdout).trim(),
        ));
    }
    None
}

/// Create a temp directory outside any git repository.
pub fn tempdir_outside_any_repo() -> tempfile::TempDir {
    let mut rejected = Vec::new();
    let candidates = [
        tempfile::tempdir(),
        tempfile::tempdir_in("/tmp"),
        tempfile::tempdir_in("/var/tmp"),
    ];

    for result in candidates {
        let Ok(dir) = result else {
            continue;
        };
        if let Some(repo) = git_repository_root(dir.path()) {
            rejected.push(format!(
                "{} (found .git at {})",
                dir.path().display(),
                repo.display()
            ));
            continue;
        }
        return dir;
    }

    panic!(
        "failed to create a test directory outside any git repository; rejected={rejected:?}, TMPDIR={:?}",
        std::env::var("TMPDIR").ok(),
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use serial_test::serial;
    use std::env;
    use std::fs;

    struct EnvironmentRestore {
        values: Vec<(&'static str, Option<String>)>,
    }

    impl EnvironmentRestore {
        fn set_hostile(external_root: &Path) -> Self {
            let git_dir = external_root.join(".git");
            let values = GIT_REPOSITORY_ENV_VARS
                .iter()
                .map(|&variable| (variable, env::var(variable).ok()))
                .collect();
            let assignments = [
                ("GIT_DIR", git_dir.clone()),
                ("GIT_WORK_TREE", external_root.to_path_buf()),
                ("GIT_INDEX_FILE", git_dir.join("index")),
                ("GIT_COMMON_DIR", git_dir.clone()),
                ("GIT_OBJECT_DIRECTORY", git_dir.join("objects")),
                ("GIT_ALTERNATE_OBJECT_DIRECTORIES", git_dir.join("objects")),
            ];
            for (variable, value) in assignments {
                env::set_var(variable, value);
            }
            Self { values }
        }
    }

    impl Drop for EnvironmentRestore {
        fn drop(&mut self) {
            for (variable, value) in &self.values {
                match value {
                    Some(value) => env::set_var(variable, value),
                    None => env::remove_var(variable),
                }
            }
        }
    }

    fn git_output(root: &Path, args: &[&str]) -> Output {
        std::process::Command::new("git")
            .args(args)
            .current_dir(root)
            .scrub_git_repository_env()
            .output()
            .expect("git command should start")
    }

    fn tracked_files(root: &Path) -> Vec<u8> {
        git_output(root, &["ls-files", "-z"]).stdout
    }

    #[test]
    fn fixture_git_root_guard_rejects_parent_repository() {
        let parent = tempdir_outside_any_repo();
        init_fixture_git_repository(parent.path()).unwrap();
        let nested = parent.path().join("nested-fixture");
        fs::create_dir(&nested).unwrap();

        let error =
            assert_fixture_git_root(&nested).expect_err("parent repository must be rejected");
        assert!(error.to_string().contains("escaped its fixture directory"));
    }

    #[test]
    #[serial]
    fn fixture_git_isolated_from_hostile_external_repository_environment() {
        let external = tempdir_outside_any_repo();
        let fixture = tempdir_outside_any_repo();
        init_fixture_git_repository(external.path()).unwrap();
        fs::write(external.path().join("keep.txt"), "external\n").unwrap();
        run_fixture_git_command(external.path(), &["config", "user.name", "External"]).unwrap();
        run_fixture_git_command(
            external.path(),
            &["config", "user.email", "external@example.com"],
        )
        .unwrap();
        run_fixture_git_command(external.path(), &["add", "keep.txt"]).unwrap();
        run_fixture_git_command(external.path(), &["commit", "-m", "external init"]).unwrap();

        let external_head = fs::read(external.path().join(".git/HEAD")).unwrap();
        let external_config = fs::read(external.path().join(".git/config")).unwrap();
        let external_index = fs::read(external.path().join(".git/index")).unwrap();
        let external_tracked = tracked_files(external.path());
        let _hostile_environment = EnvironmentRestore::set_hostile(external.path());

        init_fixture_git_repository(fixture.path()).unwrap();
        fs::write(fixture.path().join("fixture.txt"), "fixture\n").unwrap();
        run_fixture_git_command(fixture.path(), &["config", "user.name", "Fixture"]).unwrap();
        run_fixture_git_command(
            fixture.path(),
            &["config", "user.email", "fixture@example.com"],
        )
        .unwrap();
        run_fixture_git_command(fixture.path(), &["add", "fixture.txt"]).unwrap();
        run_fixture_git_command(fixture.path(), &["commit", "-m", "fixture init"]).unwrap();

        assert!(fixture.path().join(".git/HEAD").exists());
        assert_eq!(
            fs::read(external.path().join(".git/HEAD")).unwrap(),
            external_head
        );
        assert_eq!(
            fs::read(external.path().join(".git/config")).unwrap(),
            external_config
        );
        assert_eq!(
            fs::read(external.path().join(".git/index")).unwrap(),
            external_index
        );
        assert_eq!(tracked_files(external.path()), external_tracked);
        let fixture_config = fs::read_to_string(fixture.path().join(".git/config")).unwrap();
        assert!(fixture_config.contains("name = Fixture"));
        assert!(fixture_config.contains("email = fixture@example.com"));
    }
}
