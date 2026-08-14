use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let repository_root = manifest_dir
        .parent()
        .and_then(|path| path.parent())
        .expect("exomonad must be nested under the repository root");
    let tl_loop_source = repository_root.join("tl_loop");
    let archive_builder = repository_root.join("scripts/build_tl_loop_archive.py");
    let interpreter_resolver = repository_root.join("scripts/resolve_tl_loop_python.py");
    let interpreter_policy = tl_loop_source.join("interpreter_policy.toml");
    let archive_output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("tl_loop.pyz");

    println!("cargo:rerun-if-changed={}", tl_loop_source.display());
    println!("cargo:rerun-if-changed={}", archive_builder.display());
    println!("cargo:rerun-if-changed={}", interpreter_resolver.display());
    println!("cargo:rerun-if-changed={}", interpreter_policy.display());
    println!("cargo:rerun-if-env-changed=EXOMONAD_TL_LOOP_PYTHON");

    let resolver = Command::new("python3")
        .arg(&interpreter_resolver)
        .current_dir(repository_root)
        .output()
        .expect("failed to resolve the tl_loop build interpreter");
    if !resolver.status.success() {
        panic!(
            "tl_loop interpreter resolver failed: {}",
            String::from_utf8_lossy(&resolver.stderr)
        );
    }
    let controller = String::from_utf8(resolver.stdout)
        .expect("tl_loop interpreter resolver emitted invalid UTF-8")
        .trim()
        .to_owned();
    assert!(
        !controller.is_empty(),
        "tl_loop interpreter resolver returned empty output"
    );

    let build = Command::new(&controller)
        .arg(&archive_builder)
        .arg("--source")
        .arg(&tl_loop_source)
        .arg("--output")
        .arg(&archive_output)
        .current_dir(repository_root)
        .output()
        .unwrap_or_else(|error| {
            panic!("failed to build tl_loop archive with {controller}: {error}")
        });
    if !build.status.success() {
        panic!(
            "tl_loop archive build failed: {}",
            String::from_utf8_lossy(&build.stderr)
        );
    }
}
