//! Isolated tmux servers for tests.
//!
//! `tmux -L <name>` writes its socket into the shared `/tmp/tmux-<uid>/`
//! directory, and tmux leaves that socket file behind when the server exits.
//! Names that are unique per test process therefore accumulate forever and
//! interleave with the operator's own `default` socket.
//!
//! This guard names the socket with an absolute path (`tmux -S <path>`) inside
//! a temp directory it owns. `EXOMONAD_TMUX_SOCKET` is pointed at the same
//! absolute path, which production code maps to `-S`. Dropping the guard runs
//! `kill-server` on that exact socket and then removes the directory, so no
//! socket can outlive the test, even when the test panics.
//!
//! A Unix domain socket path is limited to 108 bytes including the terminating
//! NUL. Test scratch directories live under `TMPDIR`, which can be deeply
//! nested, so both the directory prefix and the socket file name stay short.
//! A path that still cannot fit panics with the measured length instead of
//! letting tmux report `File name too long`.

use std::ffi::OsString;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, MutexGuard};

const UNIX_SOCKET_PATH_LIMIT: usize = 108;
const SOCKET_FILE_NAME: &str = "s.sock";
const DIRECTORY_PREFIX: &str = "e";

/// Serializes tests that name a tmux server, because `EXOMONAD_TMUX_SOCKET` is
/// process-global and the lib test binary runs test functions concurrently.
static ENV_TURN: Mutex<()> = Mutex::new(());

/// A tmux server whose socket lives inside a temp directory owned by this guard.
///
/// Constructing the guard arms cleanup before the first tmux call: `Drop` always
/// runs `tmux -S <socket> kill-server`, so a panic or early return afterwards
/// cannot leave the server or its socket behind.
pub struct TmuxTestServer {
    directory: Option<tempfile::TempDir>,
    socket_path: PathBuf,
    previous_socket: Option<OsString>,
    /// `Some(previous)` when this guard set `TMUX_TMPDIR`; the inner `Option` is
    /// the value to restore (or remove when it was unset).
    previous_tmux_tmpdir: Option<Option<OsString>>,
    // Held last so the process-global environment stays owned until cleanup is done.
    _turn: MutexGuard<'static, ()>,
}

impl TmuxTestServer {
    /// Create the guard, point `EXOMONAD_TMUX_SOCKET` at its absolute socket
    /// path, and take the turn that serializes env-mutating tmux tests.
    pub fn new() -> Self {
        let turn = ENV_TURN
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let directory = tempfile::Builder::new()
            .prefix(DIRECTORY_PREFIX)
            .tempdir()
            .expect("create isolated tmux socket directory");
        let socket_path = directory.path().join(SOCKET_FILE_NAME);
        assert_socket_path_fits(&socket_path);
        let previous_socket = std::env::var_os("EXOMONAD_TMUX_SOCKET");
        std::env::set_var("EXOMONAD_TMUX_SOCKET", &socket_path);
        Self {
            directory: Some(directory),
            socket_path,
            previous_socket,
            previous_tmux_tmpdir: None,
            _turn: turn,
        }
    }

    /// Like [`new`](Self::new), but names the socket the way tmux does under a
    /// private `TMUX_TMPDIR`: `<tmp>/tmux-<uid>/default`.
    ///
    /// A test that drives the shipped binary needs this because some production
    /// paths run a raw `tmux` command without `-S`; pointing both
    /// `EXOMONAD_TMUX_SOCKET` and `TMUX_TMPDIR` at the same absolute path keeps
    /// those commands on the guard's server instead of the operator's default
    /// server. The socket is still inside the guard's temp directory and the
    /// guard still kills it with `-S <path>`.
    pub fn with_tmux_default_socket() -> Self {
        let turn = ENV_TURN
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let directory = tempfile::Builder::new()
            .prefix(DIRECTORY_PREFIX)
            .tempdir()
            .expect("create isolated tmux socket directory");
        let tmux_dir = directory.path().join(format!(
            "tmux-{}",
            directory
                .path()
                .metadata()
                .expect("temp dir metadata")
                .uid()
        ));
        std::fs::create_dir_all(&tmux_dir).expect("create private TMUX_TMPDIR");
        // tmux refuses a socket directory with group/other permissions.
        std::fs::set_permissions(&tmux_dir, std::fs::Permissions::from_mode(0o700))
            .expect("restrict private TMUX_TMPDIR permissions");
        let socket_path = tmux_dir.join("default");
        assert_socket_path_fits(&socket_path);
        let previous_socket = std::env::var_os("EXOMONAD_TMUX_SOCKET");
        let previous_tmux_tmpdir = std::env::var_os("TMUX_TMPDIR");
        std::env::set_var("EXOMONAD_TMUX_SOCKET", &socket_path);
        std::env::set_var("TMUX_TMPDIR", directory.path());
        Self {
            directory: Some(directory),
            socket_path,
            previous_socket,
            previous_tmux_tmpdir: Some(previous_tmux_tmpdir),
            _turn: turn,
        }
    }

    /// Absolute path of this server's socket, for `tmux -S <path>`.
    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    /// The socket path as a string, for `Command::args`.
    pub fn socket(&self) -> &str {
        self.socket_path
            .to_str()
            .expect("tmux socket path is valid UTF-8")
    }

    /// A `tmux -S <this socket>` command, detached from any enclosing session.
    pub fn command(&self) -> Command {
        let mut command = Command::new("tmux");
        command
            .args(["-S", self.socket()])
            .env_remove("TMUX")
            .env_remove("TMUX_PANE");
        command
    }

    /// The server process id while it is running.
    pub fn server_pid(&self) -> Option<u32> {
        let output = self
            .command()
            .args(["display-message", "-p", "#{pid}"])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        String::from_utf8_lossy(&output.stdout).trim().parse().ok()
    }

    /// Whether a tmux client can still reach this exact server.
    pub fn is_reachable(&self) -> bool {
        socket_reachable(&self.socket_path)
    }
}

impl Default for TmuxTestServer {
    fn default() -> Self {
        Self::new()
    }
}

impl Drop for TmuxTestServer {
    fn drop(&mut self) {
        // Only this exact socket is touched; the operator's `default` server is
        // unreachable from an absolute `-S` path selected here.
        let _ = self
            .command()
            .arg("kill-server")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if let Some(previous) = self.previous_socket.take() {
            std::env::set_var("EXOMONAD_TMUX_SOCKET", previous);
        } else {
            std::env::remove_var("EXOMONAD_TMUX_SOCKET");
        }
        if let Some(previous) = self.previous_tmux_tmpdir.take() {
            if let Some(previous) = previous {
                std::env::set_var("TMUX_TMPDIR", previous);
            } else {
                std::env::remove_var("TMUX_TMPDIR");
            }
        }
        // Dropping the directory removes the socket file even if tmux left it
        // behind, which tmux does after both kill-server and a last-session exit.
        self.directory.take();
    }
}

fn assert_socket_path_fits(socket_path: &Path) {
    let length = socket_path.as_os_str().as_bytes().len();
    assert!(
        length < UNIX_SOCKET_PATH_LIMIT,
        "tmux test socket path is {length} bytes, over the \
         {UNIX_SOCKET_PATH_LIMIT}-byte Unix socket limit including the NUL terminator: {}",
        socket_path.display()
    );
}

/// Whether a tmux client can connect to `socket_path`.
pub fn socket_reachable(socket_path: &Path) -> bool {
    Command::new("tmux")
        .args(["-S", socket_path.to_str().expect("socket path is UTF-8")])
        .arg("list-sessions")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::panic::{catch_unwind, AssertUnwindSafe};
    use std::time::{Duration, Instant};

    fn tmux_available() -> bool {
        Command::new("tmux")
            .arg("-V")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    fn start_seed_session(server: &TmuxTestServer) {
        let status = server
            .command()
            .args([
                "new-session",
                "-d",
                "-s",
                "seed",
                "-n",
                "seed",
                "sleep",
                "30",
            ])
            .status()
            .expect("start isolated tmux server");
        assert!(status.success(), "isolated tmux server starts");
    }

    fn wait_for_process_exit(pid: u32) -> bool {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if !Path::new(&format!("/proc/{pid}")).exists() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        !Path::new(&format!("/proc/{pid}")).exists()
    }

    #[test]
    fn drop_kills_server_and_removes_socket() {
        if !tmux_available() {
            return;
        }
        let server = TmuxTestServer::new();
        start_seed_session(&server);
        let pid = server.server_pid().expect("server reports its pid");
        let socket_path = server.socket_path().to_path_buf();
        assert!(server.is_reachable(), "server is reachable before drop");
        assert!(socket_path.exists(), "socket file exists before drop");

        drop(server);

        assert!(
            !socket_path.exists(),
            "dropping the guard removes its socket file: {}",
            socket_path.display()
        );
        assert!(
            !socket_reachable(&socket_path),
            "the server is unreachable after drop"
        );
        assert!(
            wait_for_process_exit(pid),
            "dropping the guard kills the tmux server process {pid}"
        );
    }

    #[test]
    fn drop_cleans_up_when_the_test_body_panics() {
        if !tmux_available() {
            return;
        }
        let observed = std::sync::Arc::new(Mutex::new(None));
        let observed_in_panic = observed.clone();

        let result = catch_unwind(AssertUnwindSafe(|| {
            let server = TmuxTestServer::new();
            start_seed_session(&server);
            let pid = server.server_pid().expect("server reports its pid");
            *observed_in_panic.lock().unwrap() = Some((server.socket_path().to_path_buf(), pid));
            panic!("intentional panic exercising the tmux guard");
        }));

        assert!(result.is_err(), "the intentional panic propagates");
        let (socket_path, pid) = observed
            .lock()
            .unwrap()
            .clone()
            .expect("the guard observed its socket before panicking");
        assert!(
            !socket_path.exists(),
            "unwinding removes the socket file: {}",
            socket_path.display()
        );
        assert!(
            !socket_reachable(&socket_path),
            "the server is unreachable after unwinding"
        );
        assert!(
            wait_for_process_exit(pid),
            "unwinding kills the tmux server process {pid}"
        );
    }

    #[test]
    fn contract_tests_never_name_a_tmux_socket_with_l() {
        let rust_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("test-support lives under the rust workspace root")
            .to_path_buf();
        let mut files = Vec::new();
        collect_rust_sources(&rust_root, &mut files);

        let mut violations = Vec::new();
        for path in files {
            if is_exempt(&path) {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&path) else {
                continue;
            };
            violations.extend(find_socket_name_violations(&path, &text));
        }

        assert!(
            violations.is_empty(),
            "tests must name tmux sockets with `-S <absolute TempDir path>`, not `-L` \
             or a bare `exo-` socket name:\n{}",
            violations.join("\n")
        );
    }

    fn is_exempt(path: &Path) -> bool {
        // Production mapping: a bare EXOMONAD_TMUX_SOCKET stays `-L`.
        path.ends_with("exomonad-core/src/services/tmux_ipc.rs")
            // This contract itself names the forbidden patterns.
            || path.ends_with("exomonad-test-support/src/tmux.rs")
    }

    fn collect_rust_sources(directory: &Path, files: &mut Vec<PathBuf>) {
        let Ok(entries) = std::fs::read_dir(directory) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                if path.file_name().is_some_and(|name| name == "target") {
                    continue;
                }
                collect_rust_sources(&path, files);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                files.push(path);
            }
        }
    }

    fn find_socket_name_violations(path: &Path, text: &str) -> Vec<String> {
        let lines: Vec<&str> = text.lines().collect();
        let mut violations = Vec::new();
        for (index, line) in lines.iter().enumerate() {
            if line.trim_start().starts_with("//") {
                continue;
            }
            if line.contains("\"-L\"") {
                violations.push(format!(
                    "{}:{}: `-L` socket name",
                    path.display(),
                    index + 1
                ));
            }
            if !line.contains("\"exo-") {
                continue;
            }
            // Only socket construction is forbidden; `exo-` session names are fine.
            // A socket value is built on the same line as its `socket`/`SOCKET`
            // binding or its `.sock` file name.
            let lowered = line.to_ascii_lowercase();
            if lowered.contains("socket") || lowered.contains(".sock") {
                violations.push(format!(
                    "{}:{}: bare `exo-` tmux socket name; use TmuxTestServer",
                    path.display(),
                    index + 1
                ));
            }
        }
        violations
    }
}
