#[cfg(debug_assertions)]
#[test]
fn debug_binary_exposes_mock_watcher() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_exomonad"))
        .args(["serve", "--help"])
        .output()
        .expect("run debug exomonad binary");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("--mock-watcher"), "{stdout}");
}

#[cfg(not(debug_assertions))]
#[test]
fn release_binary_rejects_mock_watcher() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_exomonad"))
        .args(["serve", "--mock-watcher"])
        .output()
        .expect("run release exomonad binary");
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("unexpected argument") || stderr.contains("invalid"),
        "{stderr}"
    );
}
