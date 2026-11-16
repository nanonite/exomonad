use crate::opencode_models;
use anyhow::{bail, Context, Result};

pub async fn run(harness: Option<String>, provider: Option<String>) -> Result<()> {
    match harness.as_deref() {
        None => run_all(provider).await,
        Some("opencode") => run_opencode(provider),
        Some("claude") => run_claude(),
        Some("codex") => run_codex(),
        Some(other) => {
            bail!("Unknown harness: {other}. Valid: opencode, claude, codex")
        }
    }
}

/// The OpenCode model listing printed by `exomonad models`.
///
/// One `# opencode <version>` header per committed table, followed by that
/// table's model ids. The committed table is the only source: no `opencode`
/// subprocess runs, so the listing cannot disagree with what init validates.
pub fn opencode_model_listing(provider: Option<&str>) -> Result<Vec<String>> {
    let mut lines = Vec::new();
    for table in opencode_models::tables()? {
        lines.push(format!("# opencode {}", table.version));
        for id in table.models.keys() {
            if let Some(provider) = provider {
                let matches_provider =
                    id.split_once('/').map(|(prefix, _)| prefix) == Some(provider);
                if !matches_provider {
                    continue;
                }
            }
            lines.push(id.clone());
        }
    }
    Ok(lines)
}

fn print_opencode_listing(provider: Option<&str>) -> Result<()> {
    for line in opencode_model_listing(provider)? {
        println!("{line}");
    }
    Ok(())
}

fn run_opencode(provider: Option<String>) -> Result<()> {
    print_opencode_listing(provider.as_deref())
}

fn run_claude() -> Result<()> {
    println!("claude-opus-4-7");
    println!("claude-sonnet-4-6");
    println!("claude-haiku-5-5");
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
    print_opencode_listing(provider.as_deref())?;
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
    use std::collections::BTreeSet;

    #[test]
    fn opencode_listing_matches_the_table_for_every_committed_version() {
        let lines = opencode_model_listing(None).unwrap();
        let mut headers = Vec::new();
        let mut listed = BTreeSet::new();
        for line in &lines {
            match line.strip_prefix("# opencode ") {
                Some(version) => headers.push(version.to_string()),
                None => {
                    listed.insert(line.clone());
                }
            }
        }

        let tables = opencode_models::tables().unwrap();
        let expected = tables
            .iter()
            .flat_map(|table| table.models.keys().cloned())
            .collect::<BTreeSet<_>>();
        let expected_headers = tables
            .iter()
            .map(|table| table.version.clone())
            .collect::<Vec<_>>();
        assert_eq!(listed, expected);
        assert_eq!(headers, expected_headers);
    }

    #[test]
    fn opencode_listing_filters_by_provider() {
        let lines = opencode_model_listing(Some("opencode-go")).unwrap();
        assert!(lines
            .iter()
            .any(|line| line == "opencode-go/deepseek-v4.1-flash"));
        assert!(lines
            .iter()
            .all(|line| { line.starts_with("# opencode ") || line.starts_with("opencode-go/") }));
        assert!(!lines.iter().any(|line| line.starts_with("opencode/")));
    }

    #[test]
    fn opencode_listing_keeps_a_header_when_the_provider_has_no_models() {
        let lines = opencode_model_listing(Some("no-such-provider")).unwrap();
        assert!(lines.iter().all(|line| line.starts_with("# opencode ")));
        assert_eq!(lines.len(), opencode_models::tables().unwrap().len());
    }

    #[test]
    fn opencode_listing_has_no_duplicate_ids_across_tables() {
        let lines = opencode_model_listing(None).unwrap();
        let ids = lines
            .iter()
            .filter(|line| !line.starts_with("# "))
            .collect::<Vec<_>>();
        let unique = ids.iter().copied().collect::<BTreeSet<_>>();
        assert_eq!(ids.len(), unique.len());
    }

    #[test]
    fn opencode_listing_orders_ids_within_each_table() {
        let lines = opencode_model_listing(None).unwrap();
        let mut current = Vec::new();
        let mut per_table = Vec::new();
        for line in &lines {
            if line.starts_with("# opencode ") {
                if !current.is_empty() {
                    per_table.push(std::mem::take(&mut current));
                }
            } else {
                current.push(line.clone());
            }
        }
        if !current.is_empty() {
            per_table.push(current);
        }
        assert!(!per_table.is_empty());
        for ids in per_table {
            assert!(ids.windows(2).all(|pair| pair[0] < pair[1]));
        }
    }

    #[test]
    fn opencode_provider_filter_matches_a_whole_provider_segment() {
        let lines = opencode_model_listing(Some("opencode")).unwrap();
        let ids = lines
            .iter()
            .filter(|line| !line.starts_with("# "))
            .collect::<Vec<_>>();
        assert!(!ids.is_empty());
        assert!(ids.iter().all(|id| id.starts_with("opencode/")));
        assert!(!ids.iter().any(|id| id.starts_with("opencode-go/")));
    }

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
