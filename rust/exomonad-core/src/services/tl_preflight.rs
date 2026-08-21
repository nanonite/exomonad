//! Shared validation for TL spawn-preflight runtime path rules.

pub const TL_PREFLIGHT_RUNTIME_PATHS_ENV: &str = "EXOMONAD_TL_PREFLIGHT_RUNTIME_PATHS";

/// Runtime paths owned by ExoMonad and its harnesses rather than project source.
pub const BUILTIN_TL_PREFLIGHT_RUNTIME_PATHS: &[&str] = &[
    ".chainlink/",
    ".exo/",
    ".claude/settings.local.json",
    ".codex/",
    ".opencode/",
    "opencode.json",
];

/// Normalize and validate one configured runtime path.
pub fn normalize_tl_preflight_runtime_path(raw: &str) -> Result<String, String> {
    let trimmed = raw.trim().trim_start_matches("./").trim_end_matches('/');
    if trimmed.is_empty()
        || trimmed.starts_with('/')
        || trimmed.split('/').any(|component| component == "..")
    {
        return Err(format!(
            "invalid TL preflight runtime path {raw:?}: expected a non-empty relative path without '..'"
        ));
    }
    Ok(format!("{trimmed}/"))
}

/// Resolve built-in and environment-configured runtime path rules.
pub fn configured_tl_preflight_runtime_paths() -> Result<Vec<String>, String> {
    let mut paths = BUILTIN_TL_PREFLIGHT_RUNTIME_PATHS
        .iter()
        .map(|path| normalize_tl_preflight_runtime_path(path))
        .collect::<Result<Vec<_>, _>>()?;
    match std::env::var(TL_PREFLIGHT_RUNTIME_PATHS_ENV) {
        Ok(configured) if configured.trim().is_empty() => {}
        Ok(configured) => {
            for path in configured.split(',') {
                let normalized = normalize_tl_preflight_runtime_path(path)?;
                if !paths.contains(&normalized) {
                    paths.push(normalized);
                }
            }
        }
        Err(std::env::VarError::NotPresent) => {}
        Err(error) => {
            return Err(format!("invalid {TL_PREFLIGHT_RUNTIME_PATHS_ENV}: {error}"));
        }
    }
    Ok(paths)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_path_validation_is_strict_and_normalized() {
        assert_eq!(
            normalize_tl_preflight_runtime_path(" ./runtime/state/ "),
            Ok("runtime/state/".to_string())
        );
        for invalid in ["", " ", "/absolute/path", "runtime/../source"] {
            assert!(normalize_tl_preflight_runtime_path(invalid).is_err());
        }
    }
}
