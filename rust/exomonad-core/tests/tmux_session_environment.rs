//! A tmux session's first window must be born with the session environment.
//!
//! tmux snapshots a pane's environment when it spawns the pane's process and never
//! re-reads it, so `set-environment -t <session>` issued after `new-session` returns
//! reaches every window created later and never the one `new-session` already
//! created. A new session starts from an empty session environment and falls back to
//! the tmux server's global environment, which the server captured from whichever
//! process started it — another workspace, a parallel scenario, a developer's own
//! session. The first window is the one `exomonad init` renames to `Server` and sends
//! `exomonad serve` into, so the leak reaches every agent the server spawns.
//!
//! Each test starts its own tmux server, from a process carrying a `CODEX_HOME` no
//! session under test asks for, and kills that server on the way out. A tmux server
//! outlives the process that started it, so a server shared between these tests would
//! leave a live session behind on the host — the very residue
//! `tests/e2e/lib/python-tl.sh` exists to keep runs off. `TMUX_TMPDIR` and
//! `EXOMONAD_TMUX_SOCKET` are process-global, so the tests take turns. Every test is
//! skipped when the `tmux` binary is unavailable.

use exomonad_core::services::tmux_ipc::{TmuxIpc, WindowId};
use std::process::Command;
use std::sync::{Mutex, MutexGuard};
use tempfile::TempDir;

/// The Codex home the tmux server under test captured when it was started.
const SERVER_CODEX_HOME: &str = "/exomonad-test-foreign-codex-home";

/// The Codex home the sessions under test are created with.
const SESSION_CODEX_HOME: &str = "/exomonad-test-run-codex-home";

/// Serialises the tests, because the socket that names a tmux server is process-global.
static SERVERS: Mutex<()> = Mutex::new(());

fn tmux_socket() -> String {
    std::env::var("EXOMONAD_TMUX_SOCKET").expect("TestServer::start names the test socket")
}

fn test_tmux_command() -> Command {
    let mut command = Command::new("tmux");
    command.args(["-L", &tmux_socket()]);
    command
}

fn tmux_available() -> bool {
    Command::new("tmux")
        .arg("-V")
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn test_tmux_output(args: &[&str]) -> String {
    let output = test_tmux_command().args(args).output().expect("tmux");
    assert!(
        output.status.success(),
        "tmux {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr).trim()
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

/// Read one variable out of a tmux environment. tmux exits non-zero for an unknown name
/// and prefixes a `-` to a name that is explicitly unset, so both are reported as the
/// absence of a value rather than as a failure.
fn read_environment(args: &[&str], name: &str) -> Option<String> {
    let output = test_tmux_command().args(args).output().expect("tmux");
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .strip_prefix(&format!("{name}="))
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn session_codex_home(session: &str) -> Option<String> {
    read_environment(
        &["show-environment", "-t", session, "CODEX_HOME"],
        "CODEX_HOME",
    )
}

fn global_codex_home() -> Option<String> {
    read_environment(&["show-environment", "-g", "CODEX_HOME"], "CODEX_HOME")
}

/// The Codex home the process tmux actually spawned in a window's pane resolved.
///
/// This is the observation a session-environment read cannot make. `show-environment`
/// answers for the session and the window, both of which follow later `set-environment`
/// writes, so it reports the value `init` wrote while the pane runs on another one.
fn pane_process_codex_home(window: &str) -> Option<String> {
    let pid: u32 = test_tmux_output(&["display-message", "-p", "-t", window, "#{pane_pid}"])
        .parse()
        .expect("tmux reported a pane pid");
    let environ = std::fs::read(format!("/proc/{pid}/environ"))
        .expect("/proc must expose the spawned pane's environment");
    environ
        .split(|byte| *byte == 0)
        .filter_map(|entry| std::str::from_utf8(entry).ok())
        .find_map(|entry| entry.strip_prefix("CODEX_HOME="))
        .map(str::to_owned)
}

/// A tmux server that captures a foreign `CODEX_HOME` and is killed when it goes out of
/// scope. It holds the turn for as long as it lives, because `TMUX_TMPDIR` and
/// `EXOMONAD_TMUX_SOCKET` are both process-global.
///
/// The server gets its own `TMUX_TMPDIR` so its socket never lands in the host's shared
/// tmux directory: tmux exits without unlinking that socket, so a run that used the
/// host's directory would leave one more dead file there every time.
struct TestServer {
    // Both fields are held only for their `Drop`: the guard releases the turn, and the
    // temp dir takes the dead socket with it.
    #[allow(dead_code)]
    turn: MutexGuard<'static, ()>,
    #[allow(dead_code)]
    tmpdir: TempDir,
    socket: String,
}

impl TestServer {
    fn start(tag: &str) -> Option<Self> {
        if !tmux_available() {
            return None;
        }
        let turn = SERVERS.lock().unwrap_or_else(|error| error.into_inner());
        let tmpdir = tempfile::tempdir().expect("temp dir for the test tmux server");
        let socket = format!("exo-session-env-{tag}");
        std::env::set_var("TMUX_TMPDIR", tmpdir.path());
        std::env::set_var("EXOMONAD_TMUX_SOCKET", &socket);

        // This command starts the server, so the `CODEX_HOME` on it is what the server's
        // global environment holds for the rest of the test.
        let status = Command::new("tmux")
            .args([
                "-L",
                &socket,
                "new-session",
                "-d",
                "-s",
                "seed",
                "-n",
                "seed",
            ])
            .env("CODEX_HOME", SERVER_CODEX_HOME)
            .status()
            .expect("tmux new-session");
        assert!(
            status.success(),
            "the seed session could not start the test tmux server"
        );
        // `new_session` spawns the server's default shell and these tests read that
        // shell's environment. Pin it to a shell that stays alive on a pane tty whatever
        // the host's `default-shell` happens to be.
        test_tmux_output(&["set-option", "-g", "default-shell", "/bin/sh"]);
        test_tmux_output(&["set-option", "-g", "default-command", ""]);
        assert_eq!(
            global_codex_home().as_deref(),
            Some(SERVER_CODEX_HOME),
            "the test tmux server must capture a Codex home no session under test asks for"
        );

        Some(Self {
            turn,
            tmpdir,
            socket,
        })
    }
}

impl Drop for TestServer {
    fn drop(&mut self) {
        let _ = Command::new("tmux")
            .args(["-L", &self.socket, "kill-server"])
            .status();
        std::env::remove_var("EXOMONAD_TMUX_SOCKET");
        std::env::remove_var("TMUX_TMPDIR");
    }
}

/// A session created for one test, killed on drop.
struct TestSession {
    name: String,
    first_window: WindowId,
}

impl TestSession {
    async fn create(tag: &str, cwd: &TempDir, environment: &[(&str, &str)]) -> Self {
        let name = format!("exo-session-env-{tag}");
        let first_window = TmuxIpc::new_session(&name, cwd.path(), environment)
            .await
            .expect("tmux new-session");
        Self { name, first_window }
    }
}

impl Drop for TestSession {
    fn drop(&mut self) {
        let _ = test_tmux_command()
            .args(["kill-session", "-t", &self.name])
            .status();
    }
}

/// The ordering `exomonad init` used before `new_session` took an environment: the
/// session is created first and `set-environment` writes the Codex home afterwards.
///
/// The session environment then reads back correctly, which is why the defect survived
/// a `tmux show-environment` check, while the pane `init` renames to `Server` and sends
/// `exomonad serve` into keeps the tmux server's captured Codex home — and so does every
/// agent that process spawns.
#[tokio::test]
async fn setting_the_session_environment_after_the_first_window_is_too_late() {
    let Some(_server) = TestServer::start("late") else {
        return;
    };
    let cwd = tempfile::tempdir().expect("temp dir");
    let session = TestSession::create("late", &cwd, &[]).await;

    test_tmux_output(&[
        "set-environment",
        "-t",
        &session.name,
        "CODEX_HOME",
        SESSION_CODEX_HOME,
    ]);

    assert_eq!(
        session_codex_home(&session.name).as_deref(),
        Some(SESSION_CODEX_HOME),
        "the session environment reports the value that was written into it"
    );
    assert_eq!(
        pane_process_codex_home(session.first_window.as_str()).as_deref(),
        Some(SERVER_CODEX_HOME),
        "the first window's pane was spawned before set-environment ran, so it keeps the \
         tmux server's captured Codex home while the session environment reports another"
    );
}

/// `exomonad init` hands `CODEX_HOME` to `new-session` itself, so the window it renames
/// to `Server` is born with it. The tmux server here was started by another process with
/// a different Codex home, which is the situation the defect needs.
#[tokio::test]
async fn the_first_window_pane_is_born_with_the_new_session_environment() {
    let Some(_server) = TestServer::start("born") else {
        return;
    };
    let cwd = tempfile::tempdir().expect("temp dir");
    let session = TestSession::create("born", &cwd, &[("CODEX_HOME", SESSION_CODEX_HOME)]).await;

    assert_eq!(
        pane_process_codex_home(session.first_window.as_str()).as_deref(),
        Some(SESSION_CODEX_HOME),
        "a pane spawned with the session environment must not fall back to the tmux \
         server's captured Codex home"
    );
    assert_eq!(
        session_codex_home(&session.name).as_deref(),
        Some(SESSION_CODEX_HOME),
        "the session environment carries the Codex home too, so windows created later \
         inherit it"
    );
    assert_eq!(
        global_codex_home().as_deref(),
        Some(SERVER_CODEX_HOME),
        "handing a Codex home to new-session must not rewrite the shared server's global \
         environment, which every other workspace's session falls back to"
    );
}

/// An operator who exported no `CODEX_HOME` gets no `-e`, so `new-session` runs with the
/// flags it always ran with and the session is still created.
#[tokio::test]
async fn a_session_created_without_an_environment_is_unchanged() {
    let Some(_server) = TestServer::start("bare") else {
        return;
    };
    let cwd = tempfile::tempdir().expect("temp dir");
    let session = TestSession::create("bare", &cwd, &[]).await;

    test_tmux_output(&["has-session", "-t", &session.name]);
    assert_eq!(
        pane_process_codex_home(session.first_window.as_str()).as_deref(),
        Some(SERVER_CODEX_HOME),
        "no Codex home was requested, so the pane keeps resolving the tmux server's own \
         captured value rather than one invented for it"
    );
    assert_eq!(
        session_codex_home(&session.name),
        None,
        "a session created without an environment has no Codex home of its own"
    );
}
