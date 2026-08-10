use anyhow::Result;
use exomonad_core::services::{import_sources, ImportOptions, ImportSummary, SourceFormat};
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
