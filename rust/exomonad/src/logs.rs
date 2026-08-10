use anyhow::Result;
use exomonad_core::services::{
    drop_expired_segments, import_sources, ImportOptions, ImportSummary, SourceFormat,
};
use std::path::{Path, PathBuf};

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
