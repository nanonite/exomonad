//! Liveness probes must distinguish a live tmux target from a stale one.
//!
//! These tests drive a real tmux server. Each test creates its own uniquely
//! named session and kills it on the way out, so they are safe to run inside an
//! existing tmux session. When the `tmux` binary is unavailable the test is
//! skipped rather than failed.

use exomonad_core::domain::RoutingInfo;
use exomonad_core::services::event_log::EventLog;
use exomonad_core::services::tmux_ipc::{routing_target_alive, PaneId, TmuxIpc, WindowId};
use serde_json::json;
use std::process::Command;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

/// A tmux session created for one test, torn down on drop.
struct TestSession {
    name: String,
}

fn test_tmux_socket() -> &'static str {
    static SOCKET: OnceLock<String> = OnceLock::new();
    SOCKET.get_or_init(|| format!("exo-liveness-{}", std::process::id()))
}

fn test_tmux_command() -> Command {
    let mut command = Command::new("tmux");
    command.args(["-L", test_tmux_socket()]);
    command
}

fn configure_test_tmux_socket() {
    std::env::set_var("EXOMONAD_TMUX_SOCKET", test_tmux_socket());
}

impl TestSession {
    fn create(name: &str) -> Self {
        configure_test_tmux_socket();
        let status = test_tmux_command()
            .args([
                "new-session",
                "-d",
                "-s",
                name,
                "-n",
                "probe",
                "sh",
                "-c",
                "sleep 300",
            ])
            .status()
            .expect("tmux new-session");
        assert!(status.success(), "failed to create tmux session {name}");
        Self {
            name: name.to_string(),
        }
    }

    /// First window id (`@N`) of this session.
    fn window_id(&self) -> WindowId {
        let id = self.query("#{window_id}");
        WindowId::parse(&id).expect("tmux reported a valid window id")
    }

    /// First pane id (`%N`) of this session.
    fn pane_id(&self) -> PaneId {
        let id = self.query("#{pane_id}");
        PaneId::parse(&id).expect("tmux reported a valid pane id")
    }

    fn query(&self, format: &str) -> String {
        let output = test_tmux_command()
            .args(["list-panes", "-t", &self.name, "-F", format])
            .output()
            .expect("tmux list-panes");
        assert!(output.status.success(), "tmux list-panes failed");
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .next()
            .expect("session has at least one pane")
            .trim()
            .to_string()
    }

    fn ipc(&self) -> TmuxIpc {
        TmuxIpc::new(&self.name)
    }

    fn new_shell_window(&self, name: &str) -> WindowId {
        let output = test_tmux_command()
            .args([
                "new-window",
                "-d",
                "-t",
                &self.name,
                "-P",
                "-F",
                "#{window_id}",
                "-n",
                name,
            ])
            .output()
            .expect("tmux new-window");
        assert!(output.status.success(), "tmux new-window failed");
        WindowId::parse(String::from_utf8_lossy(&output.stdout).trim())
            .expect("tmux reported a valid window id")
    }

    fn set_remain_on_exit(&self, window_id: &WindowId) {
        let target = format!("{}:{}", self.name, window_id);
        let status = test_tmux_command()
            .args(["set-window-option", "-t", &target, "remain-on-exit", "on"])
            .status()
            .expect("tmux set-window-option");
        assert!(status.success(), "tmux set-window-option failed");
    }

    fn respawn_exiting_shell(&self, window_id: &WindowId) {
        let target = format!("{}:{}", self.name, window_id);
        let status = test_tmux_command()
            .args(["respawn-window", "-k", "-t", &target, "sh -c 'exit 0'"])
            .status()
            .expect("tmux respawn-window");
        assert!(status.success(), "tmux respawn-window failed");
    }
}

impl Drop for TestSession {
    fn drop(&mut self) {
        let _ = test_tmux_command()
            .args(["kill-session", "-t", &self.name])
            .status();
    }
}

fn tmux_available() -> bool {
    test_tmux_command()
        .arg("-V")
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
}

#[tokio::test]
async fn window_exists_reports_true_for_a_live_window() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-live-window");
    let window = session.window_id();

    assert!(session
        .ipc()
        .window_exists(&window)
        .await
        .expect("probe succeeds"));
}

#[tokio::test]
async fn window_exists_reports_false_for_an_id_that_was_never_created() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-absent-window");
    let absent = WindowId::parse("@99999").expect("syntactically valid window id");

    assert!(
        !session
            .ipc()
            .window_exists(&absent)
            .await
            .expect("probe succeeds"),
        "a window id that exists nowhere on the server must not read as alive"
    );
}

#[tokio::test]
async fn window_exists_reports_false_after_the_window_is_killed() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-killed-window");
    let extra = test_tmux_command()
        .args([
            "new-window",
            "-d",
            "-t",
            &session.name,
            "-P",
            "-F",
            "#{window_id}",
            "sh",
            "-c",
            "sleep 300",
        ])
        .output()
        .expect("tmux new-window");
    assert!(extra.status.success(), "tmux new-window failed");
    let extra_id = WindowId::parse(String::from_utf8_lossy(&extra.stdout).trim())
        .expect("tmux reported a valid window id");

    assert!(session
        .ipc()
        .window_exists(&extra_id)
        .await
        .expect("probe succeeds"));

    let status = test_tmux_command()
        .args(["kill-window", "-t", extra_id.as_str()])
        .status()
        .expect("tmux kill-window");
    assert!(status.success(), "tmux kill-window failed");

    assert!(
        !session
            .ipc()
            .window_exists(&extra_id)
            .await
            .expect("probe succeeds"),
        "a killed window must not read as alive"
    );
}

#[tokio::test]
async fn window_exists_is_scoped_to_the_probing_session() {
    if !tmux_available() {
        return;
    }
    let own = TestSession::create("exo-liveness-scope-own");
    let other = TestSession::create("exo-liveness-scope-other");
    let foreign_window = other.window_id();

    assert!(
        !own.ipc()
            .window_exists(&foreign_window)
            .await
            .expect("probe succeeds"),
        "a window owned by a different session must not read as alive"
    );
}

#[tokio::test]
async fn pane_exists_reports_false_for_an_id_that_was_never_created() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-absent-pane");
    let live = session.pane_id();
    let absent = PaneId::parse("%99999").expect("syntactically valid pane id");

    assert!(session
        .ipc()
        .pane_exists(&live)
        .await
        .expect("probe succeeds"));
    assert!(
        !session
            .ipc()
            .pane_exists(&absent)
            .await
            .expect("probe succeeds"),
        "a pane id that exists nowhere on the server must not read as alive"
    );
}

#[tokio::test]
async fn routing_target_alive_reports_false_for_stale_routing() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-stale-routing");
    let stale = RoutingInfo::window(WindowId::parse("@99999").expect("valid window id"));

    assert!(
        !routing_target_alive(&stale, &session.ipc())
            .await
            .expect("probe succeeds"),
        "stale routing metadata must not keep an exited owner marked live"
    );
}

#[tokio::test]
async fn routing_target_alive_reports_true_for_current_routing() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-current-routing");
    let routing = RoutingInfo::window(session.window_id());

    assert!(routing_target_alive(&routing, &session.ipc())
        .await
        .expect("probe succeeds"));
}

#[tokio::test]
async fn routing_target_process_alive_rejects_dead_command_in_retained_window() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-dead-command");
    let window = session.new_shell_window("dead-command");
    let routing = RoutingInfo::window(window.clone());
    let ipc = session.ipc();

    assert!(ipc.window_exists(&window).await.expect("probe succeeds"));
    assert!(ipc
        .routing_target_process_alive(&routing)
        .await
        .expect("process probe succeeds"));

    session.set_remain_on_exit(&window);
    session.respawn_exiting_shell(&window);

    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        assert!(
            ipc.window_exists(&window).await.expect("probe succeeds"),
            "the retained window must remain addressable"
        );
        if !ipc
            .routing_target_process_alive(&routing)
            .await
            .expect("process probe succeeds")
        {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "dead pane was not observed in time"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn wait_for_window_process_rejects_a_failed_tmux_command() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-startup-failure");
    let window = session.new_shell_window("failed-command");
    let ipc = session.ipc();

    session.set_remain_on_exit(&window);
    session.respawn_exiting_shell(&window);

    assert!(
        !ipc.wait_for_window_process(&window, Duration::from_millis(500))
            .await
            .expect("process probe succeeds"),
        "a retained pane whose command exited must not be reported as live"
    );
}

#[tokio::test]
async fn wait_for_window_process_accepts_a_live_tmux_command() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-startup-live");
    let window = session.new_shell_window("live-command");

    assert!(session
        .ipc()
        .wait_for_window_process(&window, Duration::from_millis(500))
        .await
        .expect("process probe succeeds"));
}

#[tokio::test]
async fn live_tmux_dispatch_event_preserves_intent_and_sequence() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-correlated-dispatch");
    let window = session.new_shell_window("correlated-dispatch");
    let marker_dir = tempfile::tempdir().expect("marker directory");
    let marker = marker_dir.path().join("spawned.json");
    let command = format!(
        "printf '%s' '{{\"child_agent\":\"worker-live\",\"intent_id\":\"intent-live\"}}' > {} ; sleep 300",
        marker.display()
    );
    let target = format!("{}:{}", session.name, window);
    let status = test_tmux_command()
        .args(["respawn-window", "-k", "-t", &target, "sh", "-c", &command])
        .status()
        .expect("tmux respawn-window");
    assert!(status.success(), "tmux respawn-window failed");

    let ipc = session.ipc();
    assert!(ipc
        .wait_for_window_process(&window, Duration::from_millis(500))
        .await
        .expect("process probe succeeds"));
    let deadline = Instant::now() + Duration::from_secs(2);
    while !marker.exists() {
        assert!(Instant::now() < deadline, "spawn marker was not written");
        tokio::time::sleep(Duration::from_millis(20)).await;
    }

    let payload: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&marker).expect("read spawn marker"))
            .expect("parse spawn marker");
    let log = EventLog::open(marker_dir.path().join("events")).expect("open event log");
    let (_, run_seq) = log
        .append_with_seq("agent.spawned", "root", &payload)
        .expect("append correlated spawn event");

    assert_eq!(run_seq, 1);
    let events = log.ledger().read_events().expect("read event ledger");
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].event.event_type, "agent.spawned");
    assert_eq!(events[0].event.run_seq, Some(run_seq));
    assert_eq!(events[0].event.data["intent_id"], json!("intent-live"));
    assert_eq!(events[0].event.data["child_agent"], json!("worker-live"));
}
