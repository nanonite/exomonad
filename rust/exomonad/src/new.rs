use anyhow::{Context, Result};
use exomonad::config::Config;
use std::io::Write;
use std::path::{Path, PathBuf};
use tracing::{info, warn};

const TANGLED_WORKFLOW_FRAMING: &str = r#"engine: nixery
when:
  - event: [push, manual]
    branch: ["*"]
  - event: [pull_request]
    branch: [main]
clone:
  depth: 1
  submodules: false
"#;

#[derive(Clone, Copy)]
enum ProjectLanguage {
    Rust,
    Haskell,
    Python,
    Node,
    Generic,
}

/// Initialize a new exomonad project in the current directory.
/// Creates .exo/config.toml, .gitignore entries, copies WASM, and rules template.
pub async fn run(_name: Option<String>) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let config_path = cwd.join(".exo/config.toml");

    if config_path.exists() {
        anyhow::bail!("ExoMonad project already exists (found .exo/config.toml)");
    }

    info!("Initializing new ExoMonad project");
    std::fs::create_dir_all(cwd.join(".exo"))?;
    std::fs::write(
        &config_path,
        "# ExoMonad project config
# All fields are optional — see CLAUDE.md for full reference.

# default_role = \"tl\"
# tmux_session = \"my-project\"
# model = \"sonnet\"

# Tangled CI integration (fill in once per workspace, then run exomonad init)
# tangled_knot_url = \"ws://localhost:5555\"
# tangled_spindle_url = \"ws://localhost:6555\"
# tangled_owner_did = \"did:plc:yourDID\"
# tangled_knot_container = \"tangled-knot-knot-1\"
# tangled_spindle_db = \"/absolute/path/to/spindle.db\"
",
    )?;

    let prs_path = cwd.join(".exo/prs.json");
    if !prs_path.exists() {
        std::fs::write(&prs_path, "{\"prs\":{},\"next_number\":1}\n")?;
        info!("Created .exo/prs.json (empty PR registry)");
    }

    let policy_path = cwd.join(".exo/review-policy.toml");
    if !policy_path.exists() {
        std::fs::write(
            &policy_path,
            "# Review policy for the worktree event watcher and merge gate.
# All fields are optional — defaults shown below.

min_review_rounds = 1
reviewer_max_rounds = 2
reviewer_max_wait_seconds = 1200
review_freshness_window_secs = 1200
external_review_threshold = 300
external_review_paths = [\"proto/**\", \"rust/exomonad-core/src/handlers/**\"]
reviewer_max_rate_limit_retries = 2
require_second_reviewer_complexity = false
complexity_line_threshold = 500
",
        )?;
        info!("Created .exo/review-policy.toml (default review policy)");
    }

    if let Err(error) = scaffold_tangled_workflow(&cwd) {
        warn!(
            error = %error,
            "Failed to scaffold .tangled/workflows/ci.yml; continuing"
        );
    }

    // Add gitignore entries
    crate::init::ensure_gitignore(&cwd)?;

    // Resolve config
    let config = Config::discover()?;

    // Copy WASM if it doesn't exist yet (same logic as init.rs)
    let wasm_filename = format!("wasm-guest-{}.wasm", config.wasm_name);
    let wasm_path = config.wasm_dir.join(&wasm_filename);
    if !wasm_path.exists() {
        let roles_dir = cwd.join(".exo/roles");
        if roles_dir.is_dir() {
            info!(path = %wasm_path.display(), "WASM not found, building...");
            exomonad::recompile::run_recompile(
                &config.wasm_name,
                &cwd,
                config.flake_ref.as_deref(),
            )
            .await?;
        } else if let Ok(home) = std::env::var("HOME") {
            let home = PathBuf::from(home);
            // Fall back to globally installed WASM from ~/.exo/wasm/
            let global_wasm = home.join(".exo/wasm").join(&wasm_filename);
            if global_wasm.exists() {
                info!(
                    src = %global_wasm.display(),
                    dst = %wasm_path.display(),
                    "Copying WASM from global install"
                );
                std::fs::create_dir_all(&config.wasm_dir)?;
                std::fs::copy(&global_wasm, &wasm_path)?;
            } else {
                warn!(
                    path = %wasm_path.display(),
                    "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
                );
            }
        } else {
            warn!(
                path = %wasm_path.display(),
                "No WASM found locally or at ~/.exo/wasm/. Run 'just install-all' in the exomonad repo, or copy roles: cp -r /path/to/exomonad/.exo/roles .exo/roles"
            );
        }
    }

    // Write hook configuration
    let binary_path = exomonad_core::find_exomonad_binary();
    exomonad_core::hooks::HookConfig::write_persistent(&cwd, &binary_path, None, None)
        .context("Failed to write hook configuration")?;
    info!("Hook configuration written to .claude/settings.local.json");

    // Copy Claude rules template if available and not already present (same logic as init.rs)
    {
        let rules_dest = cwd.join(".claude/rules/exomonad.md");
        if !rules_dest.exists() {
            // Resolution: project-local .exo/rules/ → global ~/.exo/rules/
            let local_template = cwd.join(".exo/rules/exomonad.md");
            let global_template = std::env::var("HOME")
                .ok()
                .map(|h| PathBuf::from(h).join(".exo/rules/exomonad.md"));

            let source = if local_template.exists() {
                Some(local_template)
            } else {
                global_template.filter(|p| p.exists())
            };

            if let Some(src) = source {
                std::fs::create_dir_all(cwd.join(".claude/rules"))?;
                std::fs::copy(&src, &rules_dest)?;
                info!(
                    src = %src.display(),
                    "Copied Claude rules to .claude/rules/exomonad.md"
                );
            }
        }
    }

    info!("Project initialized. Run `exomonad init` to start a session.");
    Ok(())
}

pub(crate) fn scaffold_tangled_workflow(project_dir: &Path) -> std::io::Result<()> {
    let workflow_path = project_dir.join(".tangled/workflows/ci.yml");
    if workflow_path.exists() {
        info!(
            path = %workflow_path.display(),
            "tangled workflow already present, leaving untouched"
        );
        return Ok(());
    }

    let parent = workflow_path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!("Path has no parent: {}", workflow_path.display()),
        )
    })?;
    std::fs::create_dir_all(parent)?;

    let content = tangled_workflow_content(detect_language(project_dir)?);
    let mut temp = tempfile::NamedTempFile::new_in(parent)?;
    temp.write_all(content.as_bytes())?;
    temp.flush()?;
    temp.persist(&workflow_path)
        .map(|_| ())
        .map_err(|e| e.error)?;
    info!(
        path = %workflow_path.display(),
        "Created Tangled CI workflow scaffold"
    );
    Ok(())
}

fn detect_language(project_dir: &Path) -> std::io::Result<ProjectLanguage> {
    if project_dir.join("Cargo.toml").exists() {
        return Ok(ProjectLanguage::Rust);
    }
    if project_dir.join("cabal.project").exists() || has_root_cabal_file(project_dir)? {
        return Ok(ProjectLanguage::Haskell);
    }
    if project_dir.join("pyproject.toml").exists() || project_dir.join("setup.py").exists() {
        return Ok(ProjectLanguage::Python);
    }
    if project_dir.join("package.json").exists() {
        return Ok(ProjectLanguage::Node);
    }
    Ok(ProjectLanguage::Generic)
}

fn has_root_cabal_file(project_dir: &Path) -> std::io::Result<bool> {
    for entry in std::fs::read_dir(project_dir)? {
        let entry = entry?;
        if entry.file_type()?.is_file()
            && entry
                .path()
                .extension()
                .is_some_and(|extension| extension == "cabal")
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn tangled_workflow_content(language: ProjectLanguage) -> String {
    let dependency_block = match language {
        ProjectLanguage::Rust => "dependencies:\n  nixpkgs:\n    - rustup\n    - pkg-config\n",
        ProjectLanguage::Haskell => {
            "dependencies:\n  nixpkgs:\n    - ghc\n    - cabal-install\n    - pkg-config\n"
        }
        ProjectLanguage::Python => "dependencies:\n  nixpkgs:\n    - python3\n",
        ProjectLanguage::Node => "dependencies:\n  nixpkgs:\n    - nodejs\n",
        ProjectLanguage::Generic => {
            "dependencies:\n  nixpkgs: [] # TODO: Add the nixpkgs needed by this workspace.\n"
        }
    };

    format!(
        "{TANGLED_WORKFLOW_FRAMING}{dependency_block}steps:\n  - name: customize-ci\n    command: |\n      # TODO: Replace this placeholder with workspace-specific build and test commands.\n      # See CLAUDE.md Configuration and docs/decisions for Tangled CI notes.\n"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const REQUIRED_FRAMING: &str = "engine: nixery\nwhen:\n  - event: [push, manual]\n    branch: [\"*\"]\n  - event: [pull_request]\n    branch: [main]\nclone:\n  depth: 1\n  submodules: false\n";

    #[test]
    fn scaffolds_rust_tangled_workflow() {
        assert_language_scaffold(&[("Cargo.toml", "[package]\n")], &["rustup", "pkg-config"]);
    }

    #[test]
    fn scaffolds_haskell_tangled_workflow_from_cabal_project() {
        assert_language_scaffold(
            &[("cabal.project", "packages: .\n")],
            &["ghc", "cabal-install", "pkg-config"],
        );
    }

    #[test]
    fn scaffolds_haskell_tangled_workflow_from_root_cabal_file() {
        assert_language_scaffold(
            &[("example.cabal", "cabal-version: 3.0\n")],
            &["ghc", "cabal-install", "pkg-config"],
        );
    }

    #[test]
    fn scaffolds_python_tangled_workflow() {
        assert_language_scaffold(&[("pyproject.toml", "[project]\n")], &["python3"]);
    }

    #[test]
    fn scaffolds_node_tangled_workflow() {
        assert_language_scaffold(&[("package.json", "{}\n")], &["nodejs"]);
    }

    #[test]
    fn scaffolds_generic_tangled_workflow() {
        let content = assert_language_scaffold(&[], &["TODO: Add the nixpkgs"]);
        assert!(content.contains("nixpkgs: [] # TODO"));
    }

    #[test]
    fn scaffold_is_idempotent() {
        let dir = tempfile::tempdir().unwrap();
        scaffold_tangled_workflow(dir.path()).unwrap();
        let workflow_path = dir.path().join(".tangled/workflows/ci.yml");
        std::fs::write(&workflow_path, "custom: true\n").unwrap();

        scaffold_tangled_workflow(dir.path()).unwrap();

        assert_eq!(
            std::fs::read_to_string(workflow_path).unwrap(),
            "custom: true\n"
        );
    }

    #[test]
    fn ignores_language_markers_below_workspace_root() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("vendor")).unwrap();
        std::fs::write(dir.path().join("vendor/Cargo.toml"), "[package]\n").unwrap();

        scaffold_tangled_workflow(dir.path()).unwrap();

        let content = read_workflow(dir.path());
        assert!(content.contains("TODO: Add the nixpkgs"));
        assert!(!content.contains("rustup"));
    }

    fn assert_language_scaffold(markers: &[(&str, &str)], expected: &[&str]) -> String {
        let dir = tempfile::tempdir().unwrap();
        for (path, content) in markers {
            std::fs::write(dir.path().join(path), content).unwrap();
        }

        scaffold_tangled_workflow(dir.path()).unwrap();

        let content = read_workflow(dir.path());
        assert!(content.contains(REQUIRED_FRAMING));
        assert!(content.contains("steps:\n  - name: customize-ci\n"));
        assert!(content.contains("# TODO: Replace this placeholder"));
        serde_yaml::from_str::<serde_yaml::Value>(&content).unwrap();
        for needle in expected {
            assert!(content.contains(needle), "missing {needle} in:\n{content}");
        }
        content
    }

    fn read_workflow(project_dir: &Path) -> String {
        let path = project_dir.join(".tangled/workflows/ci.yml");
        assert!(path.exists());
        std::fs::read_to_string(path).unwrap()
    }
}
