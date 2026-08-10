use anyhow::{bail, Context, Result};
use exomonad_core::services::{
    drop_expired_segments, import_sources, ImportOptions, ImportSummary, SourceFormat,
};
use std::path::{Path, PathBuf};
use std::process::Command;

pub fn run(
    project_dir: &Path,
    sources: Vec<PathBuf>,
    format: String,
    dry_run: bool,
    rebuild: bool,
) -> Result<()> {
    let summary: ImportSummary = import_sources(&ImportOptions {
        project_dir: project_dir.to_path_buf(),
        sources,
        format: SourceFormat::parse(&format)?,
        dry_run,
        rebuild,
    })?;
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}

pub fn drop_segments(project_dir: &Path, older_than_seconds: u64, dry_run: bool) -> Result<()> {
    let segments = drop_expired_segments(
        project_dir,
        std::time::Duration::from_secs(older_than_seconds),
        dry_run,
    )?;
    println!("{}", serde_json::to_string_pretty(&segments)?);
    Ok(())
}

pub fn export(project_dir: &Path, mode: String, output: PathBuf) -> Result<()> {
    let database = project_dir.join(".exo/analysis/atlas.db");
    let script =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../scripts/compile_failure_atlas.py");
    let output = if output.is_absolute() {
        output
    } else {
        project_dir.join(output)
    };
    let result = Command::new("python3")
        .arg(script)
        .arg("--database")
        .arg(database)
        .arg("--output")
        .arg(output)
        .arg("--mode")
        .arg(mode)
        .output()
        .context("run Python Failure Atlas compiler")?;
    if !result.status.success() {
        bail!(
            "Failure Atlas export failed: {}",
            String::from_utf8_lossy(&result.stderr).trim()
        );
    }
    print!("{}", String::from_utf8_lossy(&result.stdout));
    Ok(())
}

pub fn measure(
    project_dir: &Path,
    preregistration: Option<PathBuf>,
    output: PathBuf,
    require_ready: bool,
    judge_models: Vec<String>,
) -> Result<()> {
    let database = project_dir.join(".exo/analysis/atlas.db");
    let script =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../scripts/failure_atlas_measure.py");
    let output = if output.is_absolute() {
        output
    } else {
        project_dir.join(output)
    };
    let mut command = Command::new("python3");
    command
        .arg(script)
        .arg("--database")
        .arg(database)
        .arg("--output")
        .arg(output);
    if let Some(preregistration) = preregistration {
        command.arg("--preregistration").arg(preregistration);
    }
    if require_ready {
        command.arg("--require-ready");
    }
    for judge_model in judge_models {
        command.arg("--judge-model").arg(judge_model);
    }
    let result = command
        .output()
        .context("run Failure Atlas measurement pipeline")?;
    if !result.status.success() {
        bail!(
            "Failure Atlas measurement failed: {}",
            String::from_utf8_lossy(&result.stderr).trim()
        );
    }
    print!("{}", String::from_utf8_lossy(&result.stdout));
    Ok(())
}
