use std::env;
use std::ffi::OsStr;
use std::fs;
use std::io::{self, Error, ErrorKind};
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

const EXCLUDED_DIRECTORIES: &[&str] = &[".venv", "tests", "__pycache__"];

fn collect_tl_loop_files(
    directory: &Path,
    directories: &mut Vec<PathBuf>,
    files: &mut Vec<PathBuf>,
) -> io::Result<()> {
    directories.push(directory.to_path_buf());
    let mut entries = fs::read_dir(directory)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(|entry| entry.path());

    for entry in entries {
        let path = entry.path();
        let file_name = entry.file_name();
        let file_type = entry.file_type()?;

        if file_type.is_dir() {
            if EXCLUDED_DIRECTORIES
                .iter()
                .any(|excluded| file_name == OsStr::new(excluded))
            {
                continue;
            }
            collect_tl_loop_files(&path, directories, files)?;
        } else if file_type.is_file() && !file_name.to_string_lossy().ends_with(".pyc") {
            files.push(path);
        }
    }

    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = env::var_os("CARGO_MANIFEST_DIR")
        .ok_or_else(|| Error::new(ErrorKind::NotFound, "CARGO_MANIFEST_DIR is not set"))
        .map(PathBuf::from)?;
    let repository_root = manifest_dir
        .parent()
        .and_then(|path| path.parent())
        .ok_or_else(|| {
            Error::new(
                ErrorKind::InvalidInput,
                "exomonad must be nested under the repository root",
            )
        })?;
    let tl_loop_source = repository_root.join("tl_loop");
    let archive_builder = repository_root.join("scripts/build_tl_loop_archive.py");
    let interpreter_resolver = repository_root.join("scripts/resolve_tl_loop_python.py");
    let interpreter_policy = tl_loop_source.join("interpreter_policy.toml");
    let archive_output = env::var_os("OUT_DIR")
        .ok_or_else(|| Error::new(ErrorKind::NotFound, "OUT_DIR is not set"))
        .map(PathBuf::from)?
        .join("tl_loop.pyz");

    println!("cargo:rerun-if-changed={}", archive_builder.display());
    println!("cargo:rerun-if-changed={}", interpreter_resolver.display());
    println!("cargo:rerun-if-changed={}", interpreter_policy.display());
    println!("cargo:rerun-if-env-changed=EXOMONAD_TL_LOOP_PYTHON");

    let mut source_directories = Vec::new();
    let mut source_files = Vec::new();
    collect_tl_loop_files(&tl_loop_source, &mut source_directories, &mut source_files)?;
    source_directories.sort();
    for source_directory in source_directories {
        println!("cargo:rerun-if-changed={}", source_directory.display());
    }
    source_files.sort();
    for source_file in source_files {
        println!("cargo:rerun-if-changed={}", source_file.display());
    }

    let resolver = Command::new("python3")
        .arg(&interpreter_resolver)
        .arg("--policy")
        .arg(&interpreter_policy)
        .current_dir(repository_root)
        .output()?;
    if !resolver.status.success() {
        return Err(Error::other(format!(
            "tl_loop interpreter resolver failed: {}",
            String::from_utf8_lossy(&resolver.stderr)
        ))
        .into());
    }
    let controller = String::from_utf8(resolver.stdout)?.trim().to_owned();
    if controller.is_empty() {
        return Err(Error::new(
            ErrorKind::InvalidData,
            "tl_loop interpreter resolver returned empty output",
        )
        .into());
    }
    println!("cargo:warning=TL controller build interpreter: {controller}");

    let build = Command::new(&controller)
        .arg(&archive_builder)
        .arg("--source")
        .arg(&tl_loop_source)
        .arg("--output")
        .arg(&archive_output)
        .current_dir(repository_root)
        .output()?;
    if !build.status.success() {
        return Err(Error::other(format!(
            "tl_loop archive build failed: {}",
            String::from_utf8_lossy(&build.stderr)
        ))
        .into());
    }

    Ok(())
}
