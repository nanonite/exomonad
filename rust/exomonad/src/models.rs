use anyhow::{bail, Context, Result};
use std::process::Stdio;

pub async fn run(harness: Option<String>, provider: Option<String>) -> Result<()> {
    match harness.as_deref() {
        None => run_all(provider).await,
        Some("opencode") => run_opencode(provider).await,
        Some("gemini") => run_gemini(),
        Some("claude") => run_claude(),
        Some(other) => bail!("Unknown harness: {other}. Valid: opencode, gemini, claude"),
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
        std::process::exit(status.code().unwrap_or(1));
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
    Ok(())
}

async fn run_all(provider: Option<String>) -> Result<()> {
    println!("# opencode");
    run_opencode(provider).await?;
    println!();
    println!("# gemini");
    run_gemini()?;
    println!();
    println!("# claude");
    run_claude()?;
    Ok(())
}
