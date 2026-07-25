use anyhow::{bail, Context, Result};
use std::process::Stdio;

pub async fn run(harness: Option<String>, provider: Option<String>) -> Result<()> {
    match harness.as_deref() {
        None => run_all(provider).await,
        Some("opencode") => run_opencode(provider).await,
        Some("gemini") => run_gemini(),
        Some("claude") => run_claude(),
        Some("codex") => run_codex(),
        Some(other) => {
            bail!("Unknown harness: {other}. Valid: opencode, gemini, claude, codex")
        }
    }
}

async fn run_opencode(provider: Option<String>) -> Result<()> {
    let mut cmd = tokio::process::Command::new("opencode");
    cmd.arg("models");
    if let Some(p) = provider {
        cmd.arg(p);
    }
    let status = cmd
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .await
        .context("Failed to spawn `opencode models` — is opencode on PATH?")?;
    if !status.success() {
        bail!("`opencode models` exited {status}");
    }
    Ok(())
}

fn run_gemini() -> Result<()> {
    println!("gemini-2.5-pro");
    println!("gemini-2.0-flash");
    println!("gemini-2.0-flash-lite");
    println!("Note: Gemini does not expose model discovery. List may be stale.");
    Ok(())
}

fn run_claude() -> Result<()> {
    println!("claude-opus-4-7");
    println!("claude-sonnet-4-6");
    println!("claude-haiku-4-5-20251001");
    println!("Use shorthand (opus, sonnet, haiku) or full ID with --tl-model.");
    println!(
        "Note: `claude --help` exposes no model-catalog subcommand. Static list may be stale."
    );
    Ok(())
}

/// Live model catalog via `codex debug models`, falling back to a static list
/// if the CLI is missing, errors, or returns an unparseable catalog.
fn run_codex() -> Result<()> {
    match codex_model_catalog() {
        Ok(models) if !models.is_empty() => {
            for model in models {
                println!("{model}");
            }
            Ok(())
        }
        Ok(_) => {
            println!("codex: live catalog was empty, falling back to static list.");
            print_codex_static_list();
            Ok(())
        }
        Err(error) => {
            println!("codex: live catalog unavailable ({error:#}), falling back to static list.");
            print_codex_static_list();
            Ok(())
        }
    }
}

fn print_codex_static_list() {
    println!("gpt-5.2-codex");
    println!("gpt-5.1-codex");
    println!("gpt-5.1-codex-max");
    println!("gpt-5.1-codex-mini");
    println!("gpt-5-codex");
    println!("Note: static fallback list; may be stale.");
}

/// Shells out to `codex debug models` and parses its JSON catalog.
fn codex_model_catalog() -> Result<Vec<String>> {
    let output = std::process::Command::new("codex")
        .args(["debug", "models"])
        .output()
        .context("Failed to spawn `codex debug models` — is codex on PATH?")?;
    if !output.status.success() {
        bail!(
            "`codex debug models` exited {}: {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }
    parse_codex_catalog(&output.stdout)
}

/// Extracts `slug` (the value accepted by `--tl-model`/`--worker-model`) plus
/// `display_name` for each entry in a `codex debug models` JSON catalog.
fn parse_codex_catalog(json: &[u8]) -> Result<Vec<String>> {
    let catalog: serde_json::Value =
        serde_json::from_slice(json).context("Failed to parse `codex debug models` JSON output")?;
    let models = catalog
        .get("models")
        .and_then(|m| m.as_array())
        .context("`codex debug models` JSON missing `models` array")?;
    Ok(models
        .iter()
        .filter_map(|model| {
            let slug = model.get("slug")?.as_str()?;
            match model.get("display_name").and_then(|d| d.as_str()) {
                Some(display) if display != slug => Some(format!("{slug} — {display}")),
                _ => Some(slug.to_string()),
            }
        })
        .collect())
}

async fn run_all(provider: Option<String>) -> Result<()> {
    println!("# opencode");
    if let Err(error) = run_opencode(provider).await {
        println!("opencode: unavailable ({error:#})");
    }
    println!();
    println!("# gemini");
    run_gemini()?;
    println!();
    println!("# claude");
    run_claude()?;
    println!();
    println!("# codex");
    run_codex()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_codex_catalog_prefers_display_name_when_distinct() {
        let json = br#"{"models":[{"slug":"gpt-5.6-sol","display_name":"GPT-5.6-Sol"}]}"#;
        let models = parse_codex_catalog(json).unwrap();
        assert_eq!(models, vec!["gpt-5.6-sol — GPT-5.6-Sol".to_string()]);
    }

    #[test]
    fn parse_codex_catalog_falls_back_to_slug_when_display_name_matches_or_missing() {
        let json = br#"{"models":[
            {"slug":"gpt-5-codex","display_name":"gpt-5-codex"},
            {"slug":"gpt-5.1-codex-mini"}
        ]}"#;
        let models = parse_codex_catalog(json).unwrap();
        assert_eq!(
            models,
            vec!["gpt-5-codex".to_string(), "gpt-5.1-codex-mini".to_string()]
        );
    }

    #[test]
    fn parse_codex_catalog_skips_entries_missing_slug() {
        let json = br#"{"models":[{"display_name":"no slug here"},{"slug":"gpt-5-codex"}]}"#;
        let models = parse_codex_catalog(json).unwrap();
        assert_eq!(models, vec!["gpt-5-codex".to_string()]);
    }

    #[test]
    fn parse_codex_catalog_rejects_missing_models_array() {
        let json = br#"{"not_models":[]}"#;
        assert!(parse_codex_catalog(json).is_err());
    }

    #[test]
    fn parse_codex_catalog_rejects_invalid_json() {
        assert!(parse_codex_catalog(b"not json").is_err());
    }

    #[test]
    fn parse_codex_catalog_handles_empty_models_array() {
        let json = br#"{"models":[]}"#;
        let models = parse_codex_catalog(json).unwrap();
        assert!(models.is_empty());
    }
}
