//! Deterministic process-interruption points for the compiled dispatch E2E.
//!
//! The acceptance harness needs to stop the *real* standalone `serve` process
//! at exact durable boundaries instead of sleeping and racing a ledger reader.
//! A harness opts in by setting all three variables before starting `serve`:
//!
//! * `EXOMONAD_E2E_INTERRUPT_AT` - the boundary name to stop at.
//! * `EXOMONAD_E2E_INTERRUPT_SCOPE` - the exact dispatch intent id to match.
//! * `EXOMONAD_E2E_INTERRUPT_DIR` - where the reached-marker is written.
//! * `EXOMONAD_E2E_INTERRUPT_ACTION` - `abort` (default) or `pause`.
//!
//! When the requested boundary and scope match, the process writes a small
//! JSON marker, flushes it, prints one diagnostic line and then aborts (or
//! pauses). Aborting is deliberate: it produces the same abrupt process death a
//! crash would, so the restart path cannot benefit from in-process cleanup. The
//! hook never changes an authorization, a gate, a ledger row or a launch
//! decision; it only runs after the named durable boundary has already been
//! crossed.
//!
//! Production processes do not set these variables, so the hook is inert. It is
//! also inert when only some of the variables are present, which prevents an
//! accidental single-variable export from crashing a real server.

use std::fs;
use std::io::Write;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const INTERRUPT_AT: &str = "EXOMONAD_E2E_INTERRUPT_AT";
const INTERRUPT_SCOPE: &str = "EXOMONAD_E2E_INTERRUPT_SCOPE";
const INTERRUPT_DIR: &str = "EXOMONAD_E2E_INTERRUPT_DIR";
const INTERRUPT_ACTION: &str = "EXOMONAD_E2E_INTERRUPT_ACTION";
const PAUSE_TIMEOUT: Duration = Duration::from_secs(600);

/// Stop (or pause) the current process when the configured boundary and scope
/// match.
///
/// The default action aborts the process, which is the abrupt death a server
/// crash produces. `EXOMONAD_E2E_INTERRUPT_ACTION=pause` writes the marker and
/// then waits for `<dir>/<boundary>.release` (or the pause timeout) so a harness
/// can kill the *controller* process at the same exact boundary before the host
/// resumes. Both actions happen only after the named durable state is written.
pub(crate) fn interrupt_at(boundary: &str, scope: &str) {
    let Ok(target) = std::env::var(INTERRUPT_AT) else {
        return;
    };
    let Ok(expected_scope) = std::env::var(INTERRUPT_SCOPE) else {
        return;
    };
    let Ok(directory) = std::env::var(INTERRUPT_DIR) else {
        return;
    };
    if !should_interrupt(target.trim(), expected_scope.trim(), boundary, scope)
        || directory.trim().is_empty()
    {
        return;
    }
    let directory = Path::new(directory.trim());
    write_marker(directory, boundary, scope);
    eprintln!(
        "EXOMONAD_E2E_INTERRUPT boundary={boundary} scope={scope} pid={}",
        std::process::id()
    );
    if std::env::var(INTERRUPT_ACTION).is_ok_and(|action| action.trim() == "pause") {
        wait_for_release(directory, boundary);
        return;
    }
    std::process::abort();
}

fn wait_for_release(directory: &Path, boundary: &str) {
    let release = directory.join(format!("{boundary}.release"));
    let started = SystemTime::now();
    while !release.exists() {
        if started
            .elapsed()
            .is_ok_and(|elapsed| elapsed >= PAUSE_TIMEOUT)
        {
            std::process::abort();
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn should_interrupt(
    configured_boundary: &str,
    configured_scope: &str,
    boundary: &str,
    scope: &str,
) -> bool {
    !configured_boundary.is_empty()
        && !configured_scope.is_empty()
        && configured_boundary == boundary
        && configured_scope == scope
}

fn write_marker(directory: &Path, boundary: &str, scope: &str) {
    // A marker failure must not hide the requested interruption: the harness
    // proves the boundary from the process death as well as from this file.
    let _ = fs::create_dir_all(directory);
    let marker = directory.join(format!("{boundary}.json"));
    let temporary = directory.join(format!(".{boundary}.{}.tmp", std::process::id()));
    let recorded_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or_default();
    let payload = serde_json::json!({
        "boundary": boundary,
        "scope": scope,
        "pid": std::process::id(),
        "recorded_at": recorded_at,
    });
    let Ok(mut file) = fs::File::create(&temporary) else {
        return;
    };
    if writeln!(file, "{payload}").is_err() || file.sync_all().is_err() {
        return;
    }
    drop(file);
    let _ = fs::rename(&temporary, &marker);
}

#[cfg(test)]
mod tests {
    use super::should_interrupt;

    #[test]
    fn both_boundary_and_scope_must_match() {
        assert!(should_interrupt(
            "launch_before_adoption",
            "blocked-resume:abc",
            "launch_before_adoption",
            "blocked-resume:abc"
        ));
        assert!(!should_interrupt(
            "launch_before_adoption",
            "blocked-resume:abc",
            "authorization_persisted",
            "blocked-resume:abc"
        ));
        assert!(!should_interrupt(
            "launch_before_adoption",
            "blocked-resume:abc",
            "launch_before_adoption",
            "blocked-resume:other"
        ));
    }

    #[test]
    fn empty_configuration_never_interrupts() {
        assert!(!should_interrupt("", "scope", "boundary", "scope"));
        assert!(!should_interrupt("boundary", "", "boundary", "scope"));
        assert!(!should_interrupt("", "", "boundary", "scope"));
    }
}
