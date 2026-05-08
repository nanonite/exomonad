use anyhow::{Context, Result};
use exomonad::config::Config;
use std::path::PathBuf;
use tracing::{info, warn};

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
