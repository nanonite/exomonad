//! Non-blocking preflight check for Codex's Linux sandbox (`bwrap`) prerequisites.
//!
//! Some Linux hosts (Ubuntu 24.04+ with AppArmor's `unprivileged_userns`
//! restriction enabled) block `bwrap`'s unprivileged user-namespace creation
//! unless a dedicated AppArmor profile is attached to the `bwrap` binary
//! itself — see `docs/decisions/agent-sandbox-profiles.md`. This probe
//! detects that class of failure and renders an actionable warning. It never
//! blocks or fails a run: Codex is one of several optional harnesses, and a
//! Claude- or OpenCode-only project has no reason to care about this at all.

use std::io::ErrorKind;
use std::process::Command;

/// Outcome of probing whether this host can run Codex's `bwrap` sandbox.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CodexSandboxCapability {
    /// `bwrap` successfully created an unprivileged user+network namespace.
    Ok,
    /// `bwrap` is not installed; Codex will likely fail to sandbox at all.
    BwrapNotFound,
    /// `bwrap` ran but failed in the specific class this probe targets:
    /// unprivileged user-namespace creation blocked, matching the AppArmor
    /// `unprivileged_userns` restriction on this class of Linux host.
    UserNamespaceRestricted { stderr: String },
    /// `bwrap` failed for some other reason; not precise enough to
    /// recommend a specific fix.
    Inconclusive { stderr: String },
}

/// Only meaningful on Linux — `bwrap`/AppArmor do not apply on macOS or
/// Windows, where Codex uses an entirely different sandbox mechanism.
pub fn applies_to_this_host() -> bool {
    cfg!(target_os = "linux")
}

/// Run the actual probe: a minimal, harmless `bwrap` invocation that
/// exercises exactly the failure class documented in
/// `docs/decisions/agent-sandbox-profiles.md` (unshare a network namespace,
/// bind-mount `/` read-only, run `/bin/true`, exit).
pub fn probe_codex_sandbox_capability() -> CodexSandboxCapability {
    if !applies_to_this_host() {
        return CodexSandboxCapability::Ok;
    }

    let output = Command::new("bwrap")
        .args([
            "--unshare-net",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ])
        .output();

    match output {
        Ok(output) => {
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            classify_bwrap_result(output.status.success(), &stderr)
        }
        Err(err) if err.kind() == ErrorKind::NotFound => CodexSandboxCapability::BwrapNotFound,
        Err(err) => CodexSandboxCapability::Inconclusive {
            stderr: err.to_string(),
        },
    }
}

/// Pure classification, separated from the actual subprocess call so it can
/// be unit tested without depending on host `bwrap`/AppArmor state.
fn classify_bwrap_result(success: bool, stderr: &str) -> CodexSandboxCapability {
    if success {
        return CodexSandboxCapability::Ok;
    }
    let restricted = stderr.contains("Failed RTM_NEWADDR") || stderr.contains("setting up uid map");
    if restricted {
        CodexSandboxCapability::UserNamespaceRestricted {
            stderr: stderr.to_string(),
        }
    } else {
        CodexSandboxCapability::Inconclusive {
            stderr: stderr.to_string(),
        }
    }
}

/// Render an actionable warning for a non-`Ok` capability, or `None` when
/// the host is fine (the common case — callers should print nothing then).
pub fn codex_sandbox_warning(capability: &CodexSandboxCapability) -> Option<String> {
    match capability {
        CodexSandboxCapability::Ok => None,
        CodexSandboxCapability::BwrapNotFound => Some(
            "warning: `bwrap` (bubblewrap) was not found on this host. Codex-spawned agents \
             need it to sandbox shell commands and will likely fail to run any. Install it \
             (e.g. `sudo apt install bubblewrap` on Debian/Ubuntu) — this does not block \
             exomonad; Codex is optional, Claude and OpenCode are unaffected."
                .to_string(),
        ),
        CodexSandboxCapability::UserNamespaceRestricted { stderr } => Some(format!(
            "warning: Codex's Linux sandbox (bwrap) could not create an unprivileged user \
             namespace on this host:\n\
             \n\
             {stderr}\n\
             \n\
             This is commonly caused by Ubuntu 24.04+'s AppArmor `unprivileged_userns` \
             restriction. Codex-spawned agents will fail to run real shell commands until \
             this is fixed. Recommended fix — attach a dedicated AppArmor profile to \
             `bwrap` itself (this does not disable any system-wide AppArmor restriction):\n\
             \n\
             sudo tee /etc/apparmor.d/bwrap-userns > /dev/null <<'EOF'\n\
             abi <abi/4.0>,\n\
             include <tunables/global>\n\
             \n\
             profile bwrap_userns /usr/bin/bwrap flags=(unconfined) {{\n\
             \x20 userns,\n\
             \x20 include if exists <local/bwrap>\n\
             }}\n\
             EOF\n\
             sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns\n\
             \n\
             See docs/decisions/agent-sandbox-profiles.md for the full trail. This does not \
             block exomonad — Codex is optional; Claude and OpenCode are unaffected."
        )),
        CodexSandboxCapability::Inconclusive { stderr } => Some(format!(
            "warning: a preflight check of Codex's Linux sandbox (bwrap) failed in an \
             unrecognized way:\n\
             \n\
             {stderr}\n\
             \n\
             Codex-spawned agents may not work correctly. This does not block exomonad — \
             Codex is optional; Claude and OpenCode are unaffected. See \
             docs/decisions/agent-sandbox-profiles.md if you hit sandbox errors from a \
             spawned Codex agent."
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_success() {
        assert_eq!(
            classify_bwrap_result(true, ""),
            CodexSandboxCapability::Ok
        );
    }

    #[test]
    fn classifies_loopback_failure_as_userns_restricted() {
        let stderr = "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n";
        assert_eq!(
            classify_bwrap_result(false, stderr),
            CodexSandboxCapability::UserNamespaceRestricted {
                stderr: stderr.to_string()
            }
        );
    }

    #[test]
    fn classifies_uid_map_failure_as_userns_restricted() {
        let stderr = "bwrap: setting up uid map: Permission denied\n";
        assert_eq!(
            classify_bwrap_result(false, stderr),
            CodexSandboxCapability::UserNamespaceRestricted {
                stderr: stderr.to_string()
            }
        );
    }

    #[test]
    fn classifies_unrecognized_failure_as_inconclusive() {
        let stderr = "bwrap: something else entirely went wrong\n";
        assert_eq!(
            classify_bwrap_result(false, stderr),
            CodexSandboxCapability::Inconclusive {
                stderr: stderr.to_string()
            }
        );
    }

    #[test]
    fn ok_capability_has_no_warning() {
        assert_eq!(codex_sandbox_warning(&CodexSandboxCapability::Ok), None);
    }

    #[test]
    fn bwrap_not_found_warning_mentions_install_and_non_blocking() {
        let warning = codex_sandbox_warning(&CodexSandboxCapability::BwrapNotFound).unwrap();
        assert!(warning.contains("bubblewrap"));
        assert!(warning.contains("does not block exomonad"));
    }

    #[test]
    fn user_namespace_restricted_warning_includes_remediation_profile() {
        let capability = CodexSandboxCapability::UserNamespaceRestricted {
            stderr: "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted".to_string(),
        };
        let warning = codex_sandbox_warning(&capability).unwrap();
        assert!(warning.contains("/etc/apparmor.d/bwrap-userns"));
        assert!(warning.contains("profile bwrap_userns /usr/bin/bwrap"));
        assert!(warning.contains("apparmor_parser -r"));
        assert!(warning.contains("does not block exomonad"));
        assert!(warning.contains("Failed RTM_NEWADDR"));
    }

    #[test]
    fn inconclusive_warning_is_non_blocking_and_includes_stderr() {
        let capability = CodexSandboxCapability::Inconclusive {
            stderr: "bwrap: something else entirely went wrong".to_string(),
        };
        let warning = codex_sandbox_warning(&capability).unwrap();
        assert!(warning.contains("something else entirely went wrong"));
        assert!(warning.contains("does not block exomonad"));
    }

    #[test]
    #[ignore = "exercises the real host bwrap/AppArmor state"]
    fn probe_runs_against_real_host() {
        // Not asserting a specific outcome — this documents that the probe
        // never panics and always returns a classification, run manually to
        // sanity-check the real subprocess invocation on a given host.
        let _ = probe_codex_sandbox_capability();
    }
}
